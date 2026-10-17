"""Interactive Brokers: paper trading only.

Requires TWS or IB Gateway running and logged in to a *paper* account, with
the API enabled (paper ports by default: TWS 7497, IB Gateway 4002), and the
optional ``ib_async`` package (``pip install ib_async``).

Safety properties, independent of the engine's own checks:

* Paper only: every account this adapter touches must be an IBKR paper
  account (IDs starting with "D"). There is no live-trading switch.
* Ledger reconciliation: the strategy keeps its own ledger of what it owns.
  If the account holds anything the ledger does not explain - a manual trade,
  a stock split, an option, a short, a fractional share - the run halts
  until a human reconciles it.
* No duplicates: every order carries a deterministic reference; an order whose
  reference already executed today is never sent again.
* No dangling orders: unfilled limit orders are cancelled after a timeout, and
  a run will not start while any order is open.
* Day limit orders only, within regular trading hours.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from .costs import CostModel
from .errors import HaltError
from .execution import Fill
from .metrics import is_finite_number
from .orders import Order, Side
from .portfolio import Position

NEW_YORK = ZoneInfo("America/New_York")
DONE_STATES = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})
REGULAR_SESSION = (clock_time(9, 30), clock_time(15, 50))   # stop 10 minutes before the close


class PaperOnlyError(RuntimeError):
    """The adapter was pointed at something other than a paper account."""


class _LedgerMismatch(HaltError):
    pass


# ---------------------------------------------------------------------------- gateway

@dataclass(frozen=True)
class IbPosition:
    symbol: str
    sec_type: str
    currency: str
    quantity: float
    avg_cost: float     # per share, including commissions


@dataclass(frozen=True)
class IbOpenOrder:
    symbol: str
    order_ref: str
    status: str


@dataclass(frozen=True)
class IbExecution:
    order_ref: str
    symbol: str
    side: str           # BUY or SELL
    shares: float
    price: float
    commission: float


@dataclass(frozen=True)
class IbOrderState:
    status: str
    filled: float
    avg_price: float
    commission: float


class IbGateway(Protocol):
    """The handful of IB operations the strategy needs."""

    def managed_accounts(self) -> list[str]: ...

    def account_values(self, account: str) -> list[tuple[str, str, str]]: ...   # (tag, value, currency)

    def positions(self, account: str) -> list[IbPosition]: ...

    def open_orders(self, account: str) -> list[IbOpenOrder]: ...

    def executions(self, account: str) -> list[IbExecution]: ...   # today's

    def place_limit_order(self, account: str, symbol: str, side: str, quantity: int, limit_price: float,
                          order_ref: str) -> object: ...

    def order_state(self, handle: object) -> IbOrderState: ...

    def cancel_order(self, handle: object) -> None: ...

    def daily_bars(self, symbol: str, duration: str) -> list[tuple[date, float, float]]: ...   # (date, close, volume)

    def sleep(self, seconds: float) -> None: ...


class IbAsyncGateway:
    """:class:`IbGateway` implemented with the ``ib_async`` library."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4002, client_id: int = 17, timeout: float = 10.0):
        try:
            import ib_async
        except ImportError as exc:
            raise ImportError("Interactive Brokers support needs the ib_async package: pip install ib_async") from exc
        self._lib = ib_async
        self.ib = ib_async.IB()
        self.ib.connect(host, port, clientId=client_id, timeout=timeout)
        self._contracts: dict[str, object] = {}

    def close(self) -> None:
        self.ib.disconnect()

    def managed_accounts(self) -> list[str]:
        return list(self.ib.managedAccounts())

    def account_values(self, account: str) -> list[tuple[str, str, str]]:
        return [(v.tag, v.value, v.currency) for v in self.ib.accountSummary(account)]

    def positions(self, account: str) -> list[IbPosition]:
        return [IbPosition(p.contract.symbol, p.contract.secType, p.contract.currency, float(p.position),
                           float(p.avgCost)) for p in self.ib.positions(account)]

    def open_orders(self, account: str) -> list[IbOpenOrder]:
        return [IbOpenOrder(t.contract.symbol, t.order.orderRef or "", t.orderStatus.status)
                for t in self.ib.reqAllOpenOrders()
                if (not t.order.account or t.order.account == account) and t.orderStatus.status not in DONE_STATES]

    def executions(self, account: str) -> list[IbExecution]:
        fills = self.ib.reqExecutions(self._lib.ExecutionFilter(acctCode=account))
        return [IbExecution(f.execution.orderRef or "", f.contract.symbol,
                            "BUY" if f.execution.side == "BOT" else "SELL", float(f.execution.shares),
                            float(f.execution.price), _commission(f)) for f in fills]

    def place_limit_order(self, account, symbol, side, quantity, limit_price, order_ref):
        order = self._lib.LimitOrder(side, quantity, limit_price, tif="DAY", orderRef=order_ref, account=account,
                                     outsideRth=False)
        return self.ib.placeOrder(self._contract(symbol), order)

    def order_state(self, trade) -> IbOrderState:
        status = trade.orderStatus
        return IbOrderState(status.status, float(status.filled), float(status.avgFillPrice or 0.0),
                            sum(_commission(f) for f in trade.fills))

    def cancel_order(self, trade) -> None:
        self.ib.cancelOrder(trade.order)

    def daily_bars(self, symbol: str, duration: str) -> list[tuple[date, float, float]]:
        bars = self.ib.reqHistoricalData(self._contract(symbol), endDateTime="", durationStr=duration,
                                         barSizeSetting="1 day", whatToShow="TRADES", useRTH=True, formatDate=1)
        out = []
        for bar in bars:
            day = bar.date.date() if isinstance(bar.date, datetime) else bar.date
            out.append((day, float(bar.close), float(bar.volume)))
        return out

    def sleep(self, seconds: float) -> None:
        self.ib.sleep(seconds)

    def _contract(self, symbol: str):
        if symbol not in self._contracts:
            qualified = self.ib.qualifyContracts(self._lib.Stock(symbol, "SMART", "USD"))
            if not qualified or qualified[0] is None:
                raise LookupError(f"Interactive Brokers does not recognise a unique US stock {symbol!r}")
            self._contracts[symbol] = qualified[0]
        return self._contracts[symbol]


def _commission(fill) -> float:
    report = getattr(fill, "commissionReport", None)
    value = getattr(report, "commission", 0.0) if report else 0.0
    # IB uses a huge sentinel until the commission report arrives.
    return float(value) if is_finite_number(value) and abs(value) < 1e9 else 0.0


# ---------------------------------------------------------------------------- ledger

class Ledger:
    """What the strategy believes it owns: shares, sector and opening date per
    ticker. Written atomically after every fill."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for ticker, entry in data.items():
                if (type(entry["shares"]) is not int or entry["shares"] <= 0 or not isinstance(entry["sector"], str)
                        or not isinstance(ticker, str)):
                    raise ValueError(ticker)
                date.fromisoformat(entry["opened_on"])
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HaltError(f"the ledger {self.path} is unreadable or corrupt ({exc}); restore it from a backup") from exc
        return data

    def save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def record_fill(self, ticker: str, sector: str, side: Side, shares: int, as_of: date) -> None:
        data = self.load()
        entry = data.get(ticker)
        if side is Side.BUY:
            if entry is None:
                data[ticker] = {"shares": shares, "sector": sector, "opened_on": as_of.isoformat()}
            else:
                entry["shares"] += shares
        else:
            remaining = (entry["shares"] if entry else 0) - shares
            if remaining > 0:
                entry["shares"] = remaining
            else:
                data.pop(ticker, None)
        self.save(data)


# ---------------------------------------------------------------------------- broker

def order_reference(order: Order, as_of: date) -> str:
    return f"BV-{as_of:%Y%m%d}-{order.side.value}-{order.ticker}-{order.shares}"


def round_limit_price(side: Side, price: float) -> float:
    """Exchange tick: one cent, or 0.01 cent under $1. Buys round down and
    sells round up, so rounding never makes a limit more aggressive."""
    step = 0.01 if price >= 1.0 else 0.0001
    units = price / step
    units = math.floor(units + 1e-9) if side is Side.BUY else math.ceil(units - 1e-9)
    return round(units * step, 4)


def new_york_now() -> datetime:
    return datetime.now(NEW_YORK)


class IbkrPaperBroker:
    def __init__(
        self,
        gateway: IbGateway,
        ledger_path: str | Path,
        cost_model: CostModel,
        account: str | None = None,
        fill_timeout: float = 120.0,
        poll_interval: float = 1.0,
        reconcile_attempts: int = 5,
        require_market_open: bool = True,
        now: Callable[[], datetime] = new_york_now,
    ):
        accounts = gateway.managed_accounts()
        if account is None:
            if len(accounts) != 1:
                raise ValueError(f"the gateway manages {len(accounts)} accounts; choose one with --account")
            account = accounts[0]
        if account not in accounts:
            raise ValueError(f"account {account} is not managed by this gateway")
        if not account.startswith("D"):
            raise PaperOnlyError(f"account {account} is not an IBKR paper account (paper IDs start with 'D'); "
                                 "this adapter only ever trades paper accounts")
        self.gateway = gateway
        self.account = account
        self.ledger = Ledger(ledger_path)
        self.costs = cost_model
        self.fill_timeout = fill_timeout
        self.poll_interval = poll_interval
        self.reconcile_attempts = reconcile_attempts
        self.require_market_open = require_market_open
        self._now = now

    # --- Broker protocol ---------------------------------------------------

    def preflight(self, as_of: date) -> None:
        open_orders = self.gateway.open_orders(self.account)
        if open_orders:
            names = ", ".join(sorted({o.symbol for o in open_orders}))
            raise HaltError(f"orders are still open in the account ({names}); cancel them or wait before rerunning")
        if self.require_market_open:
            now = self._now().astimezone(NEW_YORK)
            if now.weekday() >= 5 or not REGULAR_SESSION[0] <= now.time() <= REGULAR_SESSION[1]:
                raise HaltError(f"the US market is not in its regular session at {now:%a %H:%M} New York time")
        self.cash()
        self.positions()

    def cash(self) -> float:
        for tag, value, currency in self.gateway.account_values(self.account):
            if tag == "TotalCashValue" and currency == "USD":
                try:
                    amount = float(value)
                except ValueError:
                    break
                if is_finite_number(amount):
                    return amount
        raise HaltError("the account did not report a USD cash balance")

    def positions(self) -> dict[str, Position]:
        """Account positions, which must match the ledger. Position reports can
        trail a fill by a moment, so a mismatch is re-checked a few times
        before the run halts."""
        for attempt in range(self.reconcile_attempts):
            try:
                return self._reconciled_positions()
            except _LedgerMismatch:
                if attempt == self.reconcile_attempts - 1:
                    raise
                self.gateway.sleep(self.poll_interval)
        raise AssertionError("unreachable")

    def _reconciled_positions(self) -> dict[str, Position]:
        held: dict[str, IbPosition] = {}
        for p in self.gateway.positions(self.account):
            if p.quantity == 0:
                continue
            if p.sec_type != "STK" or p.currency != "USD":
                raise HaltError(f"the account holds {p.sec_type} {p.symbol} in {p.currency}; the strategy account "
                                "must hold only US stocks it bought itself")
            if p.quantity < 0 or p.quantity != int(p.quantity):
                raise HaltError(f"the position in {p.symbol} is {p.quantity} shares; this strategy never shorts or "
                                "holds fractional shares")
            held[p.symbol] = p
        ledger = self.ledger.load()
        unknown = sorted(set(held) - set(ledger))
        missing = sorted(set(ledger) - set(held))
        changed = sorted(t for t in held if t in ledger and int(held[t].quantity) != ledger[t]["shares"])
        if unknown or missing or changed:
            details = "; ".join(part for part in (
                f"not in the ledger: {', '.join(unknown)}" if unknown else "",
                f"in the ledger but not held: {', '.join(missing)}" if missing else "",
                f"share counts differ (a split or manual trade?): {', '.join(changed)}" if changed else "",
            ) if part)
            raise _LedgerMismatch(f"account positions do not match the strategy ledger ({details}); "
                                  "reconcile before trading")
        return {t: Position(t, ledger[t]["sector"], int(p.quantity), p.avg_cost * p.quantity,
                            date.fromisoformat(ledger[t]["opened_on"])) for t, p in sorted(held.items())}

    def submit(self, order: Order, as_of: date) -> Fill | None:
        reference = order_reference(order, as_of)
        if any(e.order_ref == reference for e in self.gateway.executions(self.account)):
            return None   # already executed today; never send the same order twice
        limit = round_limit_price(order.side, order.limit_price)
        try:
            handle = self.gateway.place_limit_order(self.account, order.ticker, order.side.value, order.shares,
                                                    limit, reference)
        except LookupError:   # IB does not recognise the symbol: nothing was sent
            return None
        state = self._wait(handle, self.fill_timeout)
        if state.status not in DONE_STATES:
            self.gateway.cancel_order(handle)
            state = self._wait(handle, 10 * self.poll_interval)
        filled = int(state.filled)
        if filled <= 0:
            return None
        self.ledger.record_fill(order.ticker, order.sector, order.side, filled, as_of)
        price = state.avg_price if state.avg_price > 0 else limit
        return Fill(order.ticker, order.side, filled, price, order.reference_price, state.commission, as_of)

    def _wait(self, handle: object, seconds: float) -> IbOrderState:
        state = self.gateway.order_state(handle)
        for _ in range(max(1, math.ceil(seconds / self.poll_interval))):
            if state.status in DONE_STATES:
                break
            self.gateway.sleep(self.poll_interval)
            state = self.gateway.order_state(handle)
        return state


class DryRunBroker:
    """Reads the real account but never sends an order; records what it would send."""

    def __init__(self, inner):
        self.inner = inner
        self.planned: list[Order] = []

    def preflight(self, as_of: date) -> None:
        self.inner.preflight(as_of)

    def cash(self) -> float:
        return self.inner.cash()

    def positions(self) -> dict[str, Position]:
        return self.inner.positions()

    def submit(self, order: Order, as_of: date) -> None:
        self.planned.append(order)
        return None
