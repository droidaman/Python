import unittest
from datetime import date

from babylon_value.backtest import equal_weight_benchmark, run_backtest
from babylon_value.config import StrategyConfig
from babylon_value.data.synthetic import generate_market
from babylon_value.report import render_html


class ReportTests(unittest.TestCase):
    def test_report_contains_results_and_notes(self):
        provider = generate_market(n_companies=80, start_year=2008, years=8, seed=4)
        result = run_backtest(provider, StrategyConfig(), date(2013, 6, 1), date(2015, 12, 31),
                              benchmark=equal_weight_benchmark(provider))
        page = render_html(result, "Test report", ["synthetic <data>"], "Equal-weight")
        self.assertIn("<svg", page)
        self.assertIn("synthetic &lt;data&gt;", page)          # notes are escaped
        self.assertEqual(page.count("<td>2014</td>"), 1)        # one calendar-year row
        self.assertEqual(page.count("completed</td>"), len(result.reports))


if __name__ == "__main__":
    unittest.main()
