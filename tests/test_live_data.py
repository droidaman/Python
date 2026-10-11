import tempfile
import unittest
from datetime import date, timedelta

from babylon_value.backtest import run_backtest
from babylon_value.config import StrategyConfig
from babylon_value.data.csv_io import export_csv_directory, load_csv_directory
from babylon_value.data.edgar import TickerRecord
from babylon_value.data.live import REVISION_FLAG, build_live_dataset, select_universe
from babylon_value.data.provider import InMemoryProvider, PriceBar, QualityFlag, ShareCount, Split
from babylon_value.dates import month_ends
from babylon_value.gates import build_entry_pipeline
from babylon_value.logic import EvaluationContext
from babylon_value.models import SecurityProfile
from babylon_value.screening import screen

from .factories import AS_OF, good_history, make_context
from .sec_fixtures import company_facts, submissions

CONFIG = StrategyConfig().validate()
PROFILE = SecurityProfile("GOOD", "Good Corp", "Industrials", "NYSE", "common_stock")


def weekdays(start, end):
    day, out = start, []
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


class SplitAndShareCountTests(unittest.TestCase):
    def provider(self, **kwargs):
        bars = [PriceBar(m, 10.0, 500_000.0) for m in month_ends(date(2024, 1, 1), date(2025, 12, 31))]
        return InMemoryProvider({"GOOD": PROFILE}, {"GOOD": good_history(years=6)}, {"GOOD": bars}, **kwargs)

    def test_reported_share_counts_are_scaled_for_later_splits(self):
        provider = self.provider(splits={"GOOD": [Split(date(2025, 4, 15), 2.0)]})
        before = provider.snapshot("GOOD", date(2025, 3, 31))
        after = provider.snapshot("GOOD", AS_OF)
        self.assertAlmostEqual(before.latest.shares_outstanding, 100e6)
        self.assertAlmostEqual(after.latest.shares_outstanding, 200e6)
        # Every year is restated to the same basis, so a split is not mistaken for dilution.
        self.assertAlmostEqual(after.history[1].shares_outstanding, 2 * before.history[1].shares_outstanding)

    def test_fresher_share_count_is_used_once_public(self):
        provider = self.provider(share_counts={"GOOD": [ShareCount(date(2025, 4, 25), date(2025, 5, 5), 95e6)]})
        self.assertIsNone(provider.snapshot("GOOD", date(2025, 5, 4)).market.shares_outstanding)
        self.assertAlmostEqual(provider.snapshot("GOOD", AS_OF).market.shares_outstanding, 95e6)

    def test_quality_flags_apply_for_their_date_range(self):
        provider = self.provider(quality_flags={"GOOD": [QualityFlag(date(2025, 5, 1), date(2025, 5, 31), "bad")]})
        pipeline = build_entry_pipeline()
        self.assertTrue(pipeline.evaluate(provider.snapshot("GOOD", AS_OF), make_context()).passed)
        flagged = pipeline.evaluate(provider.snapshot("GOOD", date(2025, 5, 31)), make_context(date(2025, 5, 31)))
        self.assertIn("data_quality", [leaf.gate for leaf in flagged.failures()])

    def test_backtest_carries_holdings_through_a_split(self):
        months = month_ends(date(2024, 1, 1), date(2025, 12, 31))
        split_day = date(2025, 7, 15)
        bars = [PriceBar(m, 10.0 if m < split_day else 5.0, 500_000.0 if m < split_day else 1e6) for m in months]
        provider = InMemoryProvider({"GOOD": PROFILE}, {"GOOD": good_history(years=6)}, {"GOOD": bars},
                                    splits={"GOOD": [Split(date(2025, 7, 15), 2.0)]})
        result = run_backtest(provider, CONFIG, date(2025, 3, 1), date(2025, 12, 31))
        july = result.dates.index(date(2025, 7, 31)) - 1   # returns[i] is the month ending dates[i + 1]
        self.assertAlmostEqual(result.returns[july], 0.0, places=4)   # no fake loss on the split
        self.assertTrue(all(abs(r) < 0.01 for r in result.returns))

    def test_round_trip_of_optional_files(self):
        provider = self.provider(splits={"GOOD": [Split(date(2025, 4, 15), 2.0)]},
                                 share_counts={"GOOD": [ShareCount(date(2025, 4, 25), date(2025, 5, 5), 95e6)]},
                                 quality_flags={"GOOD": [QualityFlag(date(2025, 5, 1), None, "check, please")]},
                                 metadata={"purpose": "research"})
        with tempfile.TemporaryDirectory() as directory:
            export_csv_directory(provider, directory)
            loaded = load_csv_directory(directory)
        self.assertEqual(loaded.snapshot("GOOD", AS_OF), provider.snapshot("GOOD", AS_OF))
        self.assertEqual(loaded.metadata, {"purpose": "research"})


class FakeSec:
    def __init__(self, companies):
        self.companies = companies   # ticker -> (cik, exchange, submissions, facts)

    def company_tickers(self):
        return [TickerRecord(cik, "x", ticker, exchange) for ticker, (cik, exchange, _, _) in self.companies.items()]

    def submissions(self, cik):
        return next(s for c, _, s, _ in self.companies.values() if c == cik)

    def company_facts(self, cik):
        return next(f for c, _, _, f in self.companies.values() if c == cik)


class FakeBars:
    def __init__(self, prices):
        self.prices = prices   # ticker -> price, or a function of the date
        self.requests = []

    def daily_bars(self, symbol, duration):
        self.requests.append(symbol)
        price = self.prices[symbol]
        return [(d, price(d) if callable(price) else price, 100_000.0)
                for d in weekdays(date(2023, 7, 3), AS_OF)]


class LiveDatasetTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.sec = FakeSec({
            "GOOD": (1, "NYSE", submissions(), company_facts(cik=1)),
            # Same business, but IB's prices sit a third lower: a 3-for-1 split after the last filing.
            "SPLT": (2, "Nasdaq", submissions(name="Split Corp"), company_facts(cik=2)),
            "BANK": (3, "NYSE", submissions(name="Bank Corp", sic="6022"), company_facts(cik=3)),
            "ABRD": (4, "NYSE", submissions(name="Abroad Corp", state="E9"), company_facts(cik=4)),
            "OTCX": (5, "OTC", submissions(name="Pink Corp"), company_facts(cik=5)),
        })

    def build(self, bars, previous=None, sec=None, as_of=AS_OF):
        return build_live_dataset(sec or self.sec, bars, self.dir.name, as_of, CONFIG, previous=previous,
                                  pause_between_price_requests=0.0, sleep=lambda s: None)

    def test_selection_keeps_one_plain_ticker_per_company(self):
        records = [TickerRecord(1, "A", "ABC", "NYSE"), TickerRecord(1, "A", "ABCB", "NYSE"),
                   TickerRecord(2, "B", "BRK.B", "NYSE"), TickerRecord(3, "C", "PNK", "OTC"),
                   TickerRecord(4, "D", "HELD", "OTC")]
        chosen = select_universe(records, CONFIG, always_include={"HELD"})
        self.assertEqual([r.ticker for r in chosen], ["ABC", "HELD"])

    def test_build_screens_and_flags_a_stale_share_count(self):
        bars = FakeBars({"GOOD": 10.0, "SPLT": 10.0 / 3})
        summary = self.build(bars)
        self.assertEqual(sorted(bars.requests), ["GOOD", "SPLT"])        # the bank and foreign firm were never priced
        self.assertIn("BANK", summary.skipped)
        self.assertIn("ABRD", summary.skipped)
        self.assertNotIn("OTCX", summary.skipped)                        # never even considered
        self.assertIn("public float", summary.flagged["SPLT"][0])

        provider = load_csv_directory(self.dir.name)
        self.assertEqual(provider.metadata["purpose"], "live")
        result = screen(provider, build_entry_pipeline(), EvaluationContext(AS_OF, CONFIG, 0.04),
                        provider.universe(AS_OF))
        self.assertEqual([c.ticker for c in result.ranked], ["GOOD"])
        self.assertIn("data_quality", [leaf.gate for leaf in result.results["SPLT"].failures()])

        with self.assertRaisesRegex(ValueError, "live trading only"):
            run_backtest(provider, CONFIG, date(2025, 1, 1), AS_OF)

    def test_throttled_price_requests_are_retried(self):
        class Flaky(FakeBars):
            def daily_bars(self, symbol, duration):
                self.requests.append(symbol)
                if self.requests.count(symbol) == 1:
                    return []
                return super().daily_bars(symbol, duration)
        bars = Flaky({"GOOD": 10.0, "SPLT": 10.0})
        summary = self.build(bars)
        self.assertEqual(summary.counts["with IB prices"], 2)

    def test_price_revision_is_flagged_until_a_newer_share_count_is_filed(self):
        self.build(FakeBars({"GOOD": 10.0, "SPLT": 10.0}))
        first = load_csv_directory(self.dir.name)
        # IB now reports every past price halved: a 2-for-1 split happened.
        summary = self.build(FakeBars({"GOOD": 5.0, "SPLT": 10.0}), previous=first)
        # Both guards catch it: prices were revised, and price x shares now sits below the public float.
        self.assertTrue(any(r.startswith(REVISION_FLAG) for r in summary.flagged["GOOD"]))
        self.assertTrue(any("public float" in r for r in summary.flagged["GOOD"]))
        second = load_csv_directory(self.dir.name)
        # Still open on the next build while no newer share count exists.
        summary = self.build(FakeBars({"GOOD": 5.0, "SPLT": 10.0}), previous=second)
        self.assertTrue(any(r.startswith(REVISION_FLAG) for r in summary.flagged["GOOD"]))

        # A few days later the company files a 10-Q with the post-split count: the flag closes.
        facts = company_facts(cik=1)
        facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"].append(
            {"end": (AS_OF + timedelta(days=1)).isoformat(), "val": 200e6, "accn": "0000000001-25-000077",
             "fy": 2025, "fp": "Q2", "form": "10-Q", "filed": (AS_OF + timedelta(days=3)).isoformat()})
        sec = FakeSec({**self.sec.companies, "GOOD": (1, "NYSE", submissions(), facts)})
        third = load_csv_directory(self.dir.name)
        later = AS_OF + timedelta(days=4)
        summary = self.build(FakeBars({"GOOD": 5.0, "SPLT": 10.0}), previous=third, sec=sec, as_of=later)
        self.assertNotIn("GOOD", summary.flagged)
        provider = load_csv_directory(self.dir.name)
        self.assertEqual(provider.snapshot("GOOD", later).quality_flags, ())
        self.assertAlmostEqual(provider.snapshot("GOOD", later).market.shares_outstanding, 200e6)


if __name__ == "__main__":
    unittest.main()
