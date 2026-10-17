import tempfile
import unittest
from datetime import date

from babylon_value.backtest import equal_weight_benchmark, run_backtest
from babylon_value.config import StrategyConfig
from babylon_value.data.csv_io import export_csv_directory, load_csv_directory
from babylon_value.data.provider import InMemoryProvider, PriceBar
from babylon_value.data.synthetic import generate_market
from babylon_value.dates import month_ends
from babylon_value.models import SecurityProfile

from .factories import good_history

CONFIG = StrategyConfig().validate()


class SyntheticBacktestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = generate_market(n_companies=150, start_year=2008, years=10, seed=5)
        cls.kwargs = dict(start=date(2013, 6, 1), end=date(2018, 12, 31), monthly_contribution=500.0,
                          benchmark=equal_weight_benchmark(cls.provider))
        cls.result = run_backtest(cls.provider, CONFIG, **cls.kwargs)

    def test_bookkeeping_invariants(self):
        r = self.result
        self.assertEqual(len(r.nav), len(r.dates))
        self.assertEqual(len(r.returns), len(r.dates) - 1)
        self.assertTrue(all(nav > 0 for nav in r.nav))
        self.assertTrue(all(fraction >= -1e-9 for fraction in r.cash_fraction))
        self.assertEqual(len(r.reports), sum(1 for d in r.dates if d.month in CONFIG.portfolio.rebalance_months))
        self.assertAlmostEqual(r.contributions, 500.0 * (len(r.dates) - 1))
        self.assertTrue(any(report.fills for report in r.reports))
        for key in ("cagr", "volatility", "sharpe", "max_drawdown", "beta", "alpha", "benchmark_cagr"):
            self.assertIn(key, r.metrics)

    def test_deterministic(self):
        again = run_backtest(self.provider, CONFIG, **self.kwargs)
        self.assertEqual(self.result.nav, again.nav)


class DelistingTests(unittest.TestCase):
    def test_delisted_holding_is_closed_at_a_haircut(self):
        months = month_ends(date(2024, 1, 1), date(2025, 12, 31))
        profiles = {t: SecurityProfile(t, t, "Industrials", "NYSE", "common_stock") for t in ("GONE", "STAY")}
        fundamentals = {t: good_history(years=6) for t in profiles}
        prices = {
            "STAY": [PriceBar(m, 10.0, 100_000.0) for m in months],
            "GONE": [PriceBar(m, 10.0, 100_000.0) for m in months if m <= date(2025, 7, 31)],
        }
        provider = InMemoryProvider(profiles, fundamentals, prices)
        result = run_backtest(provider, CONFIG, date(2025, 3, 1), date(2025, 12, 31))
        self.assertEqual(result.delistings, ["2025-08-31 GONE"])
        bought = [f for f in result.reports[0].fills if f.ticker == "GONE"]
        self.assertEqual(len(bought), 1)
        august = result.dates.index(date(2025, 8, 31))
        self.assertLess(result.returns[august - 1], -0.005)


class CsvRoundTripTests(unittest.TestCase):
    def test_export_and_reload_preserve_the_market(self):
        provider = generate_market(n_companies=20, start_year=2015, years=6, seed=2)
        with tempfile.TemporaryDirectory() as directory:
            export_csv_directory(provider, directory)
            loaded = load_csv_directory(directory)
        as_of = date(2020, 6, 30)
        self.assertEqual(provider.universe(as_of), loaded.universe(as_of))
        for ticker in provider.universe(as_of):
            self.assertEqual(provider.snapshot(ticker, as_of), loaded.snapshot(ticker, as_of))


if __name__ == "__main__":
    unittest.main()
