"""Exercises NorgateDataSource against the real norgatedata package, with only
the calls to the local Norgate Data Updater replaced. Skipped when the
package (and numpy) are not installed."""

import importlib.util
import unittest
from datetime import date
from unittest import mock

HAVE_NORGATE = importlib.util.find_spec("norgatedata") is not None and importlib.util.find_spec("numpy") is not None

PRICE_FIELDS = [("Date", "M8[D]"), ("Open", "f8"), ("High", "f8"), ("Low", "f8"), ("Close", "f8"), ("Volume", "f8"),
                ("Turnover", "f8"), ("Unadjusted Close", "f8"), ("Dividend", "f8")]


def recarray(rows, fields=PRICE_FIELDS):
    import numpy as np
    return np.array(rows, dtype=fields).view(np.recarray)


@unittest.skipUnless(HAVE_NORGATE, "norgatedata is not installed")
class NorgateDataSourceTests(unittest.TestCase):
    def setUp(self):
        import norgatedata
        self.nd = norgatedata
        raw = recarray([("2025-04-14", 10, 10, 10, 10.0, 1000.0, 1e4, 10.0, 0.0),
                        ("2025-04-15", 5, 5, 5, 5.0, 2000.0, 1e4, 5.0, 0.25)])
        adjusted = recarray([("2025-04-14", 5, 5, 5, 5.0, 2000.0, 1e4, 10.0, 0.0),
                             ("2025-04-15", 5, 5, 5, 5.0, 2000.0, 1e4, 5.0, 0.25)])
        adjust = norgatedata.StockPriceAdjustmentType

        def prices(symbol, stock_price_adjustment_setting, **kwargs):
            self.assertEqual(kwargs["timeseriesformat"], "numpy-recarray")
            return raw if stock_price_adjustment_setting is adjust.NONE else adjusted

        listed = recarray([("2025-04-14", 1), ("2025-04-15", 0)], [("Date", "M8[D]"), ("Major Exchange Listed", "i4")])
        patches = {
            "status": mock.Mock(return_value=True),
            "database_symbols": mock.Mock(return_value=["GOOD", "OLD-201909"]),
            "security_name": mock.Mock(return_value="Good Corp Common"),
            "exchange_name": mock.Mock(return_value="NYSE"),
            "subtype1": mock.Mock(return_value="Equity"),
            "subtype2": mock.Mock(return_value="Operating/Holding Company"),
            "subtype3": mock.Mock(return_value=None),
            "currency": mock.Mock(return_value="USD"),
            "first_quoted_date": mock.Mock(return_value="2010-01-04"),
            "last_quoted_date": mock.Mock(return_value=None),
            "price_timeseries": mock.Mock(side_effect=prices),
            "major_exchange_listed_timeseries": mock.Mock(return_value=listed),
        }
        for name, replacement in patches.items():
            patcher = mock.patch.object(norgatedata, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        from babylon_value.data.norgate import NorgateDataSource, derive_splits
        self.source = NorgateDataSource()
        self.derive_splits = derive_splits

    def test_metadata(self):
        self.assertEqual(self.source.symbols("US Equities"), ["GOOD", "OLD-201909"])
        security = self.source.security("OLD-201909", "US Equities Delisted")
        self.assertEqual((security.base_ticker, security.subtype3, security.first_quoted, security.last_quoted),
                         ("OLD", "", date(2010, 1, 4), None))

    def test_bars_join_unadjusted_and_capital_adjusted_series(self):
        bars = self.source.daily_bars("GOOD", date(2025, 1, 1), date(2025, 12, 31))
        self.assertEqual([(b.date, b.close, b.adjusted_close, b.volume, b.dividend) for b in bars],
                         [(date(2025, 4, 14), 10.0, 5.0, 1000.0, 0.0), (date(2025, 4, 15), 5.0, 5.0, 2000.0, 0.25)])
        self.assertEqual([(s.ex_date, s.ratio) for s in self.derive_splits(bars)], [(date(2025, 4, 15), 2.0)])

    def test_listing_status_and_missing_fields(self):
        self.assertEqual(self.source.major_exchange_listed("GOOD", date(2025, 1, 1), date(2025, 12, 31)),
                         {date(2025, 4, 14): True, date(2025, 4, 15): False})
        self.nd.price_timeseries.side_effect = lambda *a, **k: recarray([("2025-04-14", 1.0)],
                                                                        [("Date", "M8[D]"), ("Close", "f8")])
        with self.assertRaisesRegex(ValueError, "missing"):
            self.source.daily_bars("GOOD", date(2025, 1, 1), date(2025, 12, 31))

    def test_refuses_to_start_without_the_updater(self):
        from babylon_value.data.norgate import NorgateDataSource
        self.nd.status.return_value = False
        with self.assertRaisesRegex(RuntimeError, "not running"):
            NorgateDataSource()


if __name__ == "__main__":
    unittest.main()
