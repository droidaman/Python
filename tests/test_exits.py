import math
import unittest
from datetime import timedelta

from babylon_value.exits import ExitAction, evaluate_holding
from babylon_value.gates import build_entry_pipeline, build_thesis_pipeline
from babylon_value.portfolio import PortfolioState, Position
from babylon_value.principles import Principle

from .factories import AS_OF, M, good_history, make_context, make_snapshot


class ExitTests(unittest.TestCase):
    def decide(self, snap, price=10.0, cost_basis=10_000.0, days_held=100, cash=200_000.0):
        position = Position("GOOD", "Industrials", 1_000, cost_basis, AS_OF - timedelta(days=days_held))
        state = PortfolioState(cash, {"GOOD": position}, {"GOOD": price})
        return evaluate_holding(position, snap, state, make_context(), build_entry_pipeline(), build_thesis_pipeline())

    def test_intact_bargain_is_held(self):
        self.assertIs(self.decide(make_snapshot()).action, ExitAction.HOLD)

    def test_missing_or_bad_data_freezes_instead_of_trading(self):
        self.assertIs(self.decide(None).action, ExitAction.FREEZE)
        self.assertIs(self.decide(make_snapshot(good_history(revenue=math.nan))).action, ExitAction.FREEZE)

    def test_broken_thesis_sells(self):
        history = good_history(total_liabilities=800 * M, shareholders_equity=200 * M, total_debt=500 * M)
        decision = self.decide(make_snapshot(history))
        self.assertIs(decision.action, ExitAction.SELL)
        self.assertIs(decision.principle, Principle.CURE_4)
        self.assertIn("leverage", decision.reason)

    def test_fair_value_reached_sells(self):
        decision = self.decide(make_snapshot(price=30.0), price=30.0)   # EV/EBIT 18.75 >= 15
        self.assertIs(decision.action, ExitAction.SELL)
        self.assertIn("fair value", decision.reason)

    def test_hysteresis_keeps_a_holding_between_entry_and_exit_thresholds(self):
        # EV/EBIT 8.75: too dear to buy today, not dear enough to sell.
        self.assertIs(self.decide(make_snapshot(price=14.0), price=14.0, cost_basis=13_000.0).action, ExitAction.HOLD)

    def test_graham_profit_target(self):
        decision = self.decide(make_snapshot(price=14.0), price=14.0, cost_basis=9_000.0)
        self.assertIs(decision.action, ExitAction.SELL)
        self.assertIn("Graham", decision.reason)

    def test_graham_time_limit(self):
        decision = self.decide(make_snapshot(price=14.0), price=14.0, cost_basis=13_000.0, days_held=800)
        self.assertIs(decision.action, ExitAction.SELL)
        self.assertIn("800 days", decision.reason)

    def test_time_limit_does_not_sell_a_holding_that_would_be_bought_today(self):
        self.assertIs(self.decide(make_snapshot(), days_held=800).action, ExitAction.HOLD)

    def test_oversized_position_is_trimmed(self):
        self.assertIs(self.decide(make_snapshot(), cash=50_000.0).action, ExitAction.TRIM)


if __name__ == "__main__":
    unittest.main()
