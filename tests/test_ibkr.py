import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from babylon_value.config import StrategyConfig
from babylon_value.costs import CostModel
from babylon_value.data.provider import InMemoryProvider, PriceBar
from babylon_value.engine import Rebalancer, RunStatus
from babylon_value.errors import HaltError
from babylon_value.execution import PaperBroker
from babylon_value.ibkr import NEW_YORK, DryRunBroker, IbExecution, IbkrPaperBroker, IbOpenOrder, IbOrderState, \
    IbPosition, PaperOnlyError, order_reference, round_limit_price
from babylon_value.models import SecurityProfile
from babylon_value.orders import Order, Side
from babylon_value.principles import Principle

from .factories import AS_OF, good_history

CONFIG = StrategyConfig().validate()
TUESDAY_MORNING = datetime(2025, 6, 24, 11, 0, tzinfo=NEW_YORK)
SATURDAY = datetime(2025, 6, 28, 11, 0, tzinfo=NEW_YORK)


class FakeGateway:
    def __init__(self, accounts=("DU1234567",), cash="100000", positions=(), open_orders=(), executions=(),
                 fill="all", position_lag=0):
        self.accounts = list(accounts)
        self.cash = cash
        self.held = list(positions)
        self.open = list(open_orders)
        self.done = list(executions)
        self.fill = fill            # "all", "half" or "none"
        self.position_lag = position_lag   # sleeps before a fill shows up in positions
        self.placed, self.cancelled, self.slept = [], [], 0.0
        self._states = {}
        self._pending = []

    def managed_accounts(self):
        return self.accounts

    def account_values(self, account):
        values = [("NetLiquidation", "123", "USD"), ("TotalCashValue", "999", "BASE")]
        if self.cash is not None:
            values.append(("TotalCashValue", self.cash, "USD"))
        return values

    def positions(self, account):
        for update in [u for u in self._pending if u[0] <= 0]:
            self._pending.remove(update)
            _, symbol, change, price = update
            current = next((p for p in self.held if p.symbol == symbol), None)
            quantity = (current.quantity if current else 0.0) + change
            self.held = [p for p in self.held if p.symbol != symbol]
            if quantity:
                self.held.append(IbPosition(symbol, "STK", "USD", quantity, price))
        return self.held

    def open_orders(self, account):
        return self.open

    def executions(self, account):
        return self.done

    def place_limit_order(self, account, symbol, side, quantity, limit_price, order_ref):
        handle = len(self.placed)
        self.placed.append((account, symbol, side, quantity, limit_price, order_ref))
        filled = {"all": quantity, "half": quantity // 2, "none": 0}[self.fill]
        status = "Filled" if filled == quantity else "Submitted"
        if filled:
            self._pending.append([self.position_lag, symbol, filled if side == "BUY" else -filled, limit_price])
        self._states[handle] = IbOrderState(status, filled, limit_price if filled else 0.0, 0.35 if filled else 0.0)
        return handle

    def order_state(self, handle):
        return self._states[handle]

    def cancel_order(self, handle):
        self.cancelled.append(handle)
        state = self._states[handle]
        self._states[handle] = IbOrderState("Cancelled", state.filled, state.avg_price, state.commission)

    def daily_bars(self, symbol, duration):
        return []

    def sleep(self, seconds):
        self.slept += seconds
        for update in self._pending:
            update[0] -= 1


def order(side=Side.BUY, shares=100, limit=10.1049, ticker="GOOD"):
    return Order(ticker, "Industrials", side, shares, limit, 10.0, "test", Principle.MARGIN_OF_SAFETY)


class IbkrBrokerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = Path(self.dir.name) / "ledger.json"

    def broker(self, gateway, now=TUESDAY_MORNING, **kwargs):
        return IbkrPaperBroker(gateway, self.ledger, CostModel(CONFIG.costs), now=lambda: now, poll_interval=1.0,
                               fill_timeout=5.0, **kwargs)

    def write_ledger(self, entries):
        self.ledger.write_text(json.dumps(entries))

    # --- paper only --------------------------------------------------------
    def test_refuses_live_accounts(self):
        with self.assertRaises(PaperOnlyError):
            self.broker(FakeGateway(accounts=["U7654321"]))
        with self.assertRaises(ValueError):
            self.broker(FakeGateway(accounts=["DU1", "DU2"]))
        self.assertEqual(self.broker(FakeGateway(accounts=["DU1", "DU2"]), account="DU2").account, "DU2")

    # --- preflight -----------------------------------------------------------
    def test_preflight_passes_on_a_clean_account(self):
        self.broker(FakeGateway()).preflight(AS_OF)

    def test_open_orders_halt(self):
        with self.assertRaisesRegex(HaltError, "still open"):
            self.broker(FakeGateway(open_orders=[IbOpenOrder("GOOD", "BV-x", "Submitted")])).preflight(AS_OF)

    def test_closed_market_halts(self):
        with self.assertRaisesRegex(HaltError, "regular session"):
            self.broker(FakeGateway(), now=SATURDAY).preflight(AS_OF)
        self.broker(FakeGateway(), now=SATURDAY, require_market_open=False).preflight(AS_OF)

    def test_missing_cash_balance_halts(self):
        with self.assertRaisesRegex(HaltError, "cash"):
            self.broker(FakeGateway(cash=None)).cash()

    # --- reconciliation -----------------------------------------------------------
    def test_positions_must_match_the_ledger(self):
        held = [IbPosition("GOOD", "STK", "USD", 100.0, 10.5)]
        with self.assertRaisesRegex(HaltError, "not in the ledger"):
            self.broker(FakeGateway(positions=held)).positions()
        self.write_ledger({"GOOD": {"shares": 50, "sector": "Industrials", "opened_on": "2025-03-31"}})
        with self.assertRaisesRegex(HaltError, "split or manual trade"):
            self.broker(FakeGateway(positions=held)).positions()
        with self.assertRaisesRegex(HaltError, "not held"):
            self.broker(FakeGateway()).positions()

    def test_matching_positions_carry_ledger_details(self):
        self.write_ledger({"GOOD": {"shares": 100, "sector": "Industrials", "opened_on": "2025-03-31"}})
        positions = self.broker(FakeGateway(positions=[IbPosition("GOOD", "STK", "USD", 100.0, 10.5)])).positions()
        self.assertEqual(positions["GOOD"].opened_on, date(2025, 3, 31))
        self.assertEqual(positions["GOOD"].sector, "Industrials")
        self.assertAlmostEqual(positions["GOOD"].cost_basis, 1050.0)

    def test_shorts_fractions_and_derivatives_halt(self):
        for position, message in ((IbPosition("GOOD", "STK", "USD", -10.0, 10.0), "never shorts"),
                                  (IbPosition("GOOD", "STK", "USD", 10.5, 10.0), "fractional"),
                                  (IbPosition("GOOD", "OPT", "USD", 1.0, 10.0), "only US stocks")):
            with self.assertRaisesRegex(HaltError, message):
                self.broker(FakeGateway(positions=[position])).positions()

    def test_corrupt_ledger_halts(self):
        self.ledger.write_text("{not json")
        with self.assertRaisesRegex(HaltError, "corrupt"):
            self.broker(FakeGateway()).positions()

    # --- orders -------------------------------------------------------------------
    def test_filled_buy_updates_the_ledger(self):
        gateway = FakeGateway()
        fill = self.broker(gateway).submit(order(), AS_OF)
        self.assertEqual((fill.shares, fill.price, fill.commission), (100, 10.10, 0.35))
        _, symbol, side, quantity, limit, ref = gateway.placed[0]
        self.assertEqual((symbol, side, quantity, limit), ("GOOD", "BUY", 100, 10.10))   # rounded down to a cent
        self.assertEqual(ref, order_reference(order(), AS_OF))
        ledger = json.loads(self.ledger.read_text())
        self.assertEqual(ledger["GOOD"], {"shares": 100, "sector": "Industrials", "opened_on": AS_OF.isoformat()})

    def test_partial_fill_is_cancelled_and_recorded(self):
        gateway = FakeGateway(fill="half")
        fill = self.broker(gateway).submit(order(), AS_OF)
        self.assertEqual(fill.shares, 50)
        self.assertEqual(gateway.cancelled, [0])
        self.assertEqual(json.loads(self.ledger.read_text())["GOOD"]["shares"], 50)

    def test_unfilled_order_is_cancelled(self):
        gateway = FakeGateway(fill="none")
        self.assertIsNone(self.broker(gateway).submit(order(), AS_OF))
        self.assertEqual(gateway.cancelled, [0])
        self.assertFalse(self.ledger.exists())

    def test_an_order_is_never_sent_twice_in_a_day(self):
        done = IbExecution(order_reference(order(), AS_OF), "GOOD", "BUY", 100.0, 10.1, 0.35)
        gateway = FakeGateway(executions=[done])
        self.assertIsNone(self.broker(gateway).submit(order(), AS_OF))
        self.assertEqual(gateway.placed, [])

    def test_sell_closes_the_ledger_entry(self):
        self.write_ledger({"GOOD": {"shares": 100, "sector": "Industrials", "opened_on": "2025-03-31"}})
        gateway = FakeGateway()
        self.broker(gateway).submit(order(Side.SELL, limit=9.9001), AS_OF)
        self.assertEqual(gateway.placed[0][4], 9.91)   # sell limits round up
        self.assertEqual(json.loads(self.ledger.read_text()), {})

    def test_unknown_symbol_sends_nothing(self):
        gateway = FakeGateway()

        def unknown(*args):
            raise LookupError("no such stock")
        gateway.place_limit_order = unknown
        self.assertIsNone(self.broker(gateway).submit(order(), AS_OF))
        self.assertFalse(self.ledger.exists())

    def test_limit_rounding(self):
        self.assertEqual(round_limit_price(Side.BUY, 10.109), 10.10)
        self.assertEqual(round_limit_price(Side.SELL, 10.101), 10.11)
        self.assertEqual(round_limit_price(Side.BUY, 0.123456), 0.1234)
        self.assertEqual(round_limit_price(Side.BUY, 10.10), 10.10)

    def test_position_reports_that_trail_a_fill_are_waited_for(self):
        gateway = FakeGateway(position_lag=2)
        broker = self.broker(gateway)
        broker.submit(order(), AS_OF)
        self.assertEqual(broker.positions()["GOOD"].shares, 100)
        stuck = FakeGateway(position_lag=99)
        broker = self.broker(stuck)
        broker.submit(order(), AS_OF)
        with self.assertRaises(HaltError):
            broker.positions()

    def test_dry_run_sends_nothing(self):
        gateway = FakeGateway()
        dry = DryRunBroker(self.broker(gateway))
        self.assertIsNone(dry.submit(order(), AS_OF))
        self.assertEqual(gateway.placed, [])
        self.assertEqual(len(dry.planned), 1)


class IbkrEngineTests(unittest.TestCase):
    """The rebalancer driving the IB broker end to end against a fake gateway."""

    def test_rebalance_buys_then_reruns_cleanly(self):
        months = [date(2025, m, 30 if m in (4, 6) else 31 if m != 2 else 28) for m in range(1, 7)]
        provider = InMemoryProvider({"GOOD": SecurityProfile("GOOD", "Good Corp", "Industrials", "NYSE", "common_stock")},
                                    {"GOOD": good_history(years=6)},
                                    {"GOOD": [PriceBar(m, 10.0, 500_000.0) for m in months]})
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.json"
            gateway = FakeGateway()
            broker = IbkrPaperBroker(gateway, ledger, CostModel(CONFIG.costs), now=lambda: TUESDAY_MORNING)
            report = Rebalancer(CONFIG, provider, broker).run(AS_OF, 0.04)
            self.assertIs(report.status, RunStatus.COMPLETED, report.summary())
            self.assertEqual(len(gateway.placed), 1)
            shares = gateway.placed[0][3]

            # The account now holds what the ledger says; a rerun reconciles and does not buy again.
            gateway.cash = str(100_000 - shares * 10.1)
            again = Rebalancer(CONFIG, provider, broker).run(AS_OF, 0.04)
            self.assertIs(again.status, RunStatus.COMPLETED, again.summary())
            self.assertEqual(len(gateway.placed), 1)

            # A manual trade in the account halts the next run before anything is sent.
            gateway.held.append(IbPosition("XYZ", "STK", "USD", 5.0, 20.0))
            halted = Rebalancer(CONFIG, provider, broker).run(AS_OF, 0.04)
            self.assertIs(halted.status, RunStatus.HALTED)
            self.assertIn("ledger", halted.halt_reason)
            self.assertEqual(len(gateway.placed), 1)

    def test_paper_broker_satisfies_the_same_protocol(self):
        PaperBroker(1.0, CostModel(CONFIG.costs)).preflight(AS_OF)


if __name__ == "__main__":
    unittest.main()
