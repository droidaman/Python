import dataclasses
import math
import unittest
from datetime import timedelta

from babylon_value.config import StrategyConfig, ValueConfig
from babylon_value.gates import build_entry_pipeline, build_thesis_pipeline, first_failing_stage
from babylon_value.gates.value import margin_of_safety_gate
from babylon_value.logic import LookAheadBiasError, Outcome

from .factories import AS_OF, M, good_history, good_period, make_context, make_snapshot


class EntryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = build_entry_pipeline()
        self.ctx = make_context()

    def assert_stopped(self, snap, stage, gate, ctx=None):
        result = self.pipeline.evaluate(snap, ctx or self.ctx)
        self.assertFalse(result.passed, result.render())
        self.assertEqual(first_failing_stage(result), stage, result.render())
        self.assertIn(gate, [leaf.gate for leaf in result.failures()], result.render())

    def test_healthy_cheap_company_passes_every_gate(self):
        result = self.pipeline.evaluate(make_snapshot(), self.ctx)
        self.assertTrue(result.passed, result.render())
        self.assertTrue(build_thesis_pipeline().evaluate(make_snapshot(), self.ctx).passed)

    # --- Failsafe: data integrity -------------------------------------------------
    def test_missing_value_is_rejected_not_guessed(self):
        self.assert_stopped(make_snapshot(good_history(receivables=math.nan)), "data_integrity", "completeness")

    def test_short_history(self):
        self.assert_stopped(make_snapshot(good_history(years=4)), "data_integrity", "history_depth")

    def test_stale_price(self):
        self.assert_stopped(make_snapshot(price_date=AS_OF - timedelta(days=30)), "data_integrity", "freshness")

    def test_balance_sheet_that_does_not_add_up(self):
        self.assert_stopped(make_snapshot(good_history(shareholders_equity=500 * M)), "data_integrity",
                            "accounting_identity")

    def test_future_report_halts_instead_of_rejecting(self):
        leaked = good_history(available_date=AS_OF + timedelta(days=1))
        with self.assertRaises(LookAheadBiasError):
            self.pipeline.evaluate(make_snapshot(leaked), self.ctx)

    # --- Law 4: circle of competence ------------------------------------------------
    def test_financials_are_outside_the_circle(self):
        self.assert_stopped(make_snapshot(sector="Financials"), "circle_of_competence", "sector")

    def test_otc_listing_and_etfs_are_outside_the_circle(self):
        self.assert_stopped(make_snapshot(exchange="OTC"), "circle_of_competence", "exchange")
        self.assert_stopped(make_snapshot(security_type="etf"), "circle_of_competence", "security_type")

    # --- Cure 4: guard thy treasure ------------------------------------------------------
    def test_heavy_debt(self):
        history = good_history(total_liabilities=800 * M, shareholders_equity=200 * M, total_debt=500 * M)
        self.assert_stopped(make_snapshot(history), "guard_thy_treasure", "leverage")

    def test_weak_liquidity(self):
        self.assert_stopped(make_snapshot(good_history(current_liabilities=300 * M)), "guard_thy_treasure", "liquidity")

    def test_erratic_earnings(self):
        history = list(good_history())
        for k in (3, 4):
            history[k] = dataclasses.replace(history[k], ebit=-10 * M)
        self.assert_stopped(make_snapshot(history), "guard_thy_treasure", "earnings_stability")

    # --- Law 5: tricksters and impossible earnings ----------------------------------------
    def test_earnings_manipulation(self):
        history = good_history(receivables=300 * M, cash_from_operations=-50 * M)
        self.assert_stopped(make_snapshot(history), "no_tricksters", "beneish_m_score")

    def test_dilution(self):
        self.assert_stopped(make_snapshot(good_history(shares_outstanding=120 * M)), "no_tricksters", "dilution")

    def test_dividend_not_covered_by_cash_flow(self):
        self.assert_stopped(make_snapshot(good_history(dividends_paid=200 * M)), "no_tricksters",
                            "dividend_sustainability")

    def test_penny_stock(self):
        self.assert_stopped(make_snapshot(price=1.5), "no_tricksters", "penny_stock")

    # --- Cure 2: control expenditures ------------------------------------------------------
    def test_illiquid_stock(self):
        self.assert_stopped(make_snapshot(adv=100_000), "control_expenditures", "tradability")

    # --- Law 2 / margin of safety -----------------------------------------------------------
    def test_expensive_stock(self):
        self.assert_stopped(make_snapshot(price=20.0), "profitable_employment", "acquirers_multiple")

    def test_earnings_yield_must_beat_twice_the_risk_free_rate(self):
        # EV/EBIT 6.25 -> earnings yield 16%; a 9% risk-free rate sets an 18% hurdle.
        self.assert_stopped(make_snapshot(), "profitable_employment", "earnings_yield_hurdle", make_context(risk_free_rate=0.09))

    def test_value_trap_filter(self):
        strict = StrategyConfig(value=ValueConfig(min_piotroski_score=9))
        self.assert_stopped(make_snapshot(), "profitable_employment", "piotroski", make_context(config=strict))


class MarginOfSafetyTests(unittest.TestCase):
    def test_profitable_net_net_qualifies_on_assets(self):
        # Cash-rich: EV is negative, so the earnings path cannot judge it, but the
        # market values it at half of net current assets.
        latest = dataclasses.replace(
            good_period(0), current_assets=800 * M, cash_and_equivalents=400 * M, receivables=200 * M,
            ppe_net=100 * M, intangible_assets=0.0, total_liabilities=300 * M, shareholders_equity=700 * M,
        )
        snap = make_snapshot([latest, *good_history()[1:]], price=2.5)   # market cap 250M vs NCAV 500M
        result = margin_of_safety_gate().evaluate(snap, make_context())
        self.assertIs(result.outcome, Outcome.PASS, result.render())
        self.assertIn("net_net", result.reason)

    def test_neither_cheap_on_earnings_nor_on_assets(self):
        result = margin_of_safety_gate().evaluate(make_snapshot(price=20.0), make_context())
        self.assertIs(result.outcome, Outcome.FAIL)


if __name__ == "__main__":
    unittest.main()
