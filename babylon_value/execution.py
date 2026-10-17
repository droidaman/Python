"""Brokers. Only a paper broker ships with the strategy.

A live broker adapter must implement :class:`Broker` and keep the same
guarantees as :class:`PaperBroker`: it refuses short sales and refuses any
buy the cash balance cannot pay for, independently of the order gates.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .costs import CostModel
from .metrics import is_finite_number
from .orders import Order, Side
from .portfolio import Position


@dataclass(frozen=True)
class Fill:
    ticker: str
    side: Side
    shares: int
    price: float
    reference_price: float
    commission: float
    as_of: date

    @property
    def slippage(self) -> float:
        return abs(self.price - self.reference_price) * self.shares

    @property
    def cost(self) -> float:
        return self.commission + self.slippage

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "side": self.side.value,
            "shares": self.shares,
            "price": round(self.price, 4),
            "commission": round(self.commission, 2),
            "as_of": self.as_of.isoformat(),
        }


class Broker(Protocol):
    def preflight(self, as_of: date) -> None:
        """Raise HaltError if the account is not in a state that is safe to trade."""

    def cash(self) -> float: ...

    def positions(self) -> dict[str, Position]: ...

    def submit(self, order: Order, as_of: date) -> Fill | None: ...


class PaperBroker:
    """In-memory broker for backtests and paper trading. Fills limit orders at
    the reference price plus modelled slippage."""

    def __init__(self, cash: float, cost_model: CostModel):
        if not is_finite_number(cash) or cash < 0:
            raise ValueError("starting cash must be a non-negative number")
        self._cash = float(cash)
        self._positions: dict[str, Position] = {}
        self._costs = cost_model
        self.fills: list[Fill] = []
        self.rejections: list[tuple[Order, str]] = []

    def preflight(self, as_of: date) -> None:
        pass

    def cash(self) -> float:
        return self._cash

    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def deposit(self, amount: float) -> None:
        if not is_finite_number(amount) or amount < 0:
            raise ValueError("deposits must be non-negative numbers")
        self._cash += amount

    def submit(self, order: Order, as_of: date) -> Fill | None:
        if type(order.shares) is not int or order.shares <= 0:
            return self._reject(order, "shares must be a positive whole number")
        price = self._costs.fill_price(order.side, order.reference_price)
        commission = self._costs.commission(order.shares)
        existing = self._positions.get(order.ticker)
        if order.side is Side.BUY:
            if price > order.limit_price:
                return self._reject(order, "limit price not reached")
            total = order.shares * price + commission
            if total > self._cash + 1e-9:
                return self._reject(order, "insufficient cash; this broker never lends")
            self._cash -= total
            if existing:
                self._positions[order.ticker] = dataclasses.replace(
                    existing, shares=existing.shares + order.shares, cost_basis=existing.cost_basis + total)
            else:
                self._positions[order.ticker] = Position(order.ticker, order.sector, order.shares, total, as_of)
        else:
            if existing is None or existing.shares < order.shares:
                return self._reject(order, "cannot sell shares that are not held; no short selling")
            if price < order.limit_price:
                return self._reject(order, "limit price not reached")
            self._cash += order.shares * price - commission
            self._reduce(existing, order.shares)
        fill = Fill(order.ticker, order.side, order.shares, price, order.reference_price, commission, as_of)
        self.fills.append(fill)
        return fill

    def liquidate(self, ticker: str, price: float, as_of: date) -> Fill | None:
        """Forced sale outside the normal order flow (e.g. a delisting in a backtest)."""
        existing = self._positions.get(ticker)
        if existing is None:
            return None
        price = price if is_finite_number(price) and price > 0 else 0.0
        commission = self._costs.commission(existing.shares) if price > 0 else 0.0
        self._cash += max(0.0, existing.shares * price - commission)
        self._reduce(existing, existing.shares)
        fill = Fill(ticker, Side.SELL, existing.shares, price, price, commission, as_of)
        self.fills.append(fill)
        return fill

    def apply_split(self, ticker: str, ratio: float, price_after: float) -> None:
        """Apply a stock split; a fractional share is paid out as cash in lieu."""
        existing = self._positions.get(ticker)
        if existing is None:
            return
        if not is_finite_number(ratio) or ratio <= 0:
            raise ValueError(f"invalid split ratio {ratio!r} for {ticker}")
        exact = existing.shares * ratio
        whole = math.floor(exact + 1e-9)
        price_after = price_after if is_finite_number(price_after) and price_after > 0 else 0.0
        self._cash += (exact - whole) * price_after
        if whole <= 0:
            del self._positions[ticker]
        else:
            self._positions[ticker] = dataclasses.replace(
                existing, shares=whole, cost_basis=existing.cost_basis * whole / exact)

    def _reduce(self, position: Position, shares: int) -> None:
        remaining = position.shares - shares
        if remaining == 0:
            del self._positions[position.ticker]
        else:
            self._positions[position.ticker] = dataclasses.replace(
                position, shares=remaining, cost_basis=position.cost_basis * remaining / position.shares)

    def _reject(self, order: Order, reason: str) -> None:
        self.rejections.append((order, reason))
        return None
