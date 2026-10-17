import dataclasses
import math
import unittest

from babylon_value.metrics import MetricUnavailable, altman_z, beneish_m_score, enterprise_value, \
    net_current_asset_value, piotroski_f_score, ratio

from .factories import M, good_history, make_snapshot


class MetricTests(unittest.TestCase):
    def test_enterprise_value(self):
        # 10.00 x 100M shares + 100M debt - 100M cash
        self.assertAlmostEqual(enterprise_value(make_snapshot()), 1000 * M)

    def test_ncav(self):
        self.assertAlmostEqual(net_current_asset_value(good_history()[0]), 50 * M)

    def test_altman_z(self):
        z, parts = altman_z(make_snapshot())
        expected = 1.2 * 0.25 + 1.4 * 0.45 + 3.3 * 0.16 + 0.6 * (1000 / 350) + 1.0 * 1.0
        self.assertAlmostEqual(z, expected)
        self.assertAlmostEqual(parts["x4"], 1000 / 350)

    def test_piotroski(self):
        score, signals = piotroski_f_score(good_history())
        self.assertEqual(score, 8)
        self.assertFalse(signals["turnover_improving"])  # revenue and assets grow at the same rate

    def test_piotroski_does_not_penalise_debt_free_companies(self):
        history = [dataclasses.replace(p, long_term_debt=0.0, total_debt=0.0) for p in good_history()]
        _, signals = piotroski_f_score(history)
        self.assertTrue(signals["leverage_not_rising"])

    def test_beneish_clean_company(self):
        m, parts = beneish_m_score(good_history())
        self.assertAlmostEqual(parts["DSRI"], 1.0)
        self.assertLess(m, -2.5)

    def test_beneish_flags_ballooning_receivables_and_accruals(self):
        history = good_history(receivables=300 * M, cash_from_operations=-50 * M)
        m, _ = beneish_m_score(history)
        self.assertGreater(m, -1.78)

    def test_missing_data_raises_instead_of_guessing(self):
        with self.assertRaises(MetricUnavailable):
            ratio(1.0, 0.0, "x")
        with self.assertRaises(MetricUnavailable):
            ratio(math.nan, 1.0, "x")
        with self.assertRaises(MetricUnavailable):
            piotroski_f_score(good_history(years=2))


if __name__ == "__main__":
    unittest.main()
