import unittest
from datetime import date

from babylon_value.config import StrategyConfig
from babylon_value.costs import CostModel
from babylon_value.execution import PaperBroker
from babylon_value.orders import Order, Side, check_buys, check_sells
from babylon_value.portfolio import PortfolioState, Position
from babylon_value.principles import Principle

CONFIG = StrategyConfig().validate()
COSTS = CostModel(CONFIG.costs)
OPENED = date(2024, 1, 31)


def buy(ticker="NEW", shares=300, limit=10.1, ref=10.0, sector="Industrials"):
    return Order(ticker, sector, Side.BUY, shares, limit, ref, "test", Principle.MARGIN_OF_SAFETY)


def sell(shares, ticker="HELD", limit=49.5, ref=50.0):
    return Order(ticker, "Industrials", Side.SELL, shares, limit, ref, "test", Principle.CURE_4)


def state(cash=100_000.0):
    # NAV 105,000: reserve 10,500, so 89,500 may be spent.
    return PortfolioState(cash, {"HELD": Position("HELD", "Industrials", 100, 5_000.0, OPENED)}, {"HELD": 50.0})


class BuyGateTests(unittest.TestCase):
    def check(self, orders, approved=None, adv=5_000_000.0, excluded=()):
        tickers = {o.ticker for o in orders}
        return check_buys(orders, state(), {t: adv for t in tickers}, approved if approved is not None else tickers,
                          excluded, CONFIG, COSTS)

    def violations(self, order, **kwargs):
        return " | ".join(self.check([order], **kwargs)[0].violations)

    def test_sound_order_is_approved(self):
        self.assertTrue(self.check([buy()])[0].approved)

    def test_only_securities_that_passed_the_gates_can_be_bought(self):
        self.assertIn("entry gate", self.violations(buy(), approved=set()))
        self.assertIn("sold, trimmed or frozen", self.violations(buy(), excluded={"NEW"}))

    def test_whole_shares_and_sane_limit_prices_only(self):
        self.assertIn("whole number", self.violations(buy(shares=1.5)))
        self.assertIn("whole number", self.violations(buy(shares=0)))
        self.assertIn("sanity band", self.violations(buy(limit=10.5)))

    def test_size_and_liquidity_caps(self):
        self.assertIn("maximum position weight", self.violations(buy(shares=800)))
        self.assertIn("maximum order size", self.violations(buy(shares=800)))
        self.assertIn("liquidity cap", self.violations(buy(), adv=20_000.0))
        self.assertIn("minimum trade size", self.violations(buy(shares=50)))

    def test_cash_reserve_is_never_spent_and_nothing_is_borrowed(self):
        orders = [buy(ticker=f"T{i:02d}", shares=594, sector=f"S{i:02d}") for i in range(15)]
        decisions = self.check(orders)
        self.assertTrue(all(d.approved for d in decisions[:14]))
        self.assertFalse(decisions[14].approved)
        self.assertIn("cash reserve", " ".join(decisions[14].violations))

    def test_sector_cap(self):
        orders = [buy(ticker=f"T{i}", shares=594, sector="Energy") for i in range(5)]
        decisions = self.check(orders)
        self.assertTrue(all(d.approved for d in decisions[:4]))
        self.assertIn("sector weight", " ".join(decisions[4].violations))


class SellGateTests(unittest.TestCase):
    def check(self, orders):
        return check_sells(orders, state(), {"HELD": 5_000_000.0}, CONFIG)

    def test_closing_a_position_is_approved(self):
        self.assertTrue(self.check([sell(100)])[0].approved)

    def test_short_selling_is_impossible(self):
        self.assertIn("short selling", " ".join(self.check([sell(150)])[0].violations))
        decisions = self.check([sell(60), sell(60)])
        self.assertTrue(decisions[0].approved)
        self.assertFalse(decisions[1].approved)

    def test_dust_sales_are_skipped_unless_they_close_the_position(self):
        self.assertIn("minimum trade size", " ".join(self.check([sell(10)])[0].violations))


class PaperBrokerTests(unittest.TestCase):
    def test_refuses_to_lend(self):
        broker = PaperBroker(1_000.0, COSTS)
        self.assertIsNone(broker.submit(buy(shares=300), OPENED))
        self.assertEqual(broker.cash(), 1_000.0)
        self.assertEqual(broker.positions(), {})

    def test_refuses_to_short(self):
        broker = PaperBroker(10_000.0, COSTS)
        self.assertIsNone(broker.submit(sell(1), OPENED))
        broker.submit(buy(shares=100), OPENED)
        self.assertIsNone(broker.submit(sell(101, ticker="NEW", limit=9.9, ref=10.0), OPENED))

    def test_fill_includes_slippage_and_commission(self):
        broker = PaperBroker(10_000.0, COSTS)
        fill = broker.submit(buy(shares=100), OPENED)
        self.assertAlmostEqual(fill.price, 10.0 * 1.0015)
        self.assertAlmostEqual(broker.positions()["NEW"].cost_basis, 100 * 10.015 + 1.0)
        self.assertAlmostEqual(broker.cash(), 10_000.0 - 100 * 10.015 - 1.0)


if __name__ == "__main__":
    unittest.main()
