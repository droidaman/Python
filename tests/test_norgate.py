import contextlib
import io
import math
import tempfile
import unittest
from datetime import date, timedelta

from babylon_value import cli
from babylon_value.config import StrategyConfig
from babylon_value.data.csv_io import load_csv_directory
from babylon_value.data.edgar import TickerRecord, normalize_company_name, parse_entity_names
from babylon_value.data.norgate import DailyBar, NorgateSecurity, build_history_dataset, derive_splits, listed_days, \
    monthly_bars, rebase
from babylon_value.data.provider import Split
from babylon_value.data.rates import RiskFreeCurve, parse_fred_csv

from .sec_fixtures import company_facts, submissions

CONFIG = StrategyConfig().validate()


def weekdays(start, end):
    day, out = start, []
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def bars(start, end, close=10.0, volume=500_000.0, split_on=None, split_ratio=2.0):
    """Daily bars; after ``split_on`` the traded price divides by the ratio and
    volume multiplies by it, while the capital-adjusted series is continuous."""
    out = []
    for day in weekdays(start, end):
        after = split_on is not None and day >= split_on
        raw = close / split_ratio if after else close
        adjusted = close / split_ratio if split_on is not None else close
        out.append(DailyBar(day, raw, adjusted, volume * split_ratio if after else volume, 0.0))
    return out


class PriceProcessingTests(unittest.TestCase):
    def test_splits_are_derived_from_the_capital_adjustment(self):
        self.assertEqual(derive_splits(bars(date(2024, 1, 1), date(2024, 6, 30), split_on=date(2024, 4, 15))),
                         [Split(date(2024, 4, 15), 2.0)])
        reverse = bars(date(2024, 1, 1), date(2024, 6, 30), split_on=date(2024, 3, 1), split_ratio=0.1)
        self.assertEqual(derive_splits(reverse), [Split(date(2024, 3, 1), 0.1)])
        noisy = [DailyBar(date(2024, 1, 2), 10.0, 5.0000001, 1.0, 0.0), DailyBar(date(2024, 1, 3), 10.0, 5.0, 1.0, 0.0)]
        self.assertEqual(derive_splits(noisy), [])
        stock_dividend = [DailyBar(date(2024, 1, 2), 10.5, 10.0, 1.0, 0.0), DailyBar(date(2024, 1, 3), 10.0, 10.0, 1.0, 0.0)]
        self.assertEqual(derive_splits(stock_dividend), [Split(date(2024, 1, 3), 1.05)])

    def test_monthly_bars_keep_dollar_volume_through_a_split(self):
        daily = bars(date(2024, 4, 1), date(2024, 4, 30), split_on=date(2024, 4, 15))
        daily[3] = DailyBar(daily[3].date, daily[3].close, daily[3].adjusted_close, daily[3].volume, 0.25)
        (april,) = monthly_bars(daily)
        self.assertEqual((april.date, april.close, april.dividend), (date(2024, 4, 30), 5.0, 0.25))
        self.assertAlmostEqual(april.close * april.volume, 5_000_000.0)   # $5M a day before and after the split

    def test_days_off_a_major_exchange_are_dropped(self):
        daily = bars(date(2024, 1, 1), date(2024, 3, 31))
        listed = {date(2024, 1, 2): True, date(2024, 2, 15): False}
        kept = listed_days(daily, listed)
        self.assertEqual((kept[0].date, kept[-1].date), (date(2024, 1, 2), date(2024, 2, 14)))
        self.assertEqual(listed_days(daily, None), daily)

    def test_rebase_moves_share_counts_between_bases(self):
        splits = [Split(date(2024, 4, 15), 2.0)]
        self.assertEqual(rebase(100.0, date(2024, 1, 1), date(2024, 6, 1), splits), 200.0)
        self.assertEqual(rebase(200.0, date(2024, 6, 1), date(2024, 1, 1), splits), 100.0)


class NameAndRateTests(unittest.TestCase):
    def test_company_names_normalise_across_sources(self):
        pairs = [("General Electric Co Common", "GENERAL ELECTRIC CO"), ("AT&T Inc.", "AT&T INC"),
                 ("Alphabet Inc Class A", "ALPHABET INC"), ("The Gap, Incorporated", "GAP INC")]
        for norgate_name, sec_name in pairs:
            self.assertEqual(normalize_company_name(norgate_name), normalize_company_name(sec_name))

    def test_entity_name_file(self):
        names = parse_entity_names("GENERAL ELECTRIC CO:0000040545:\nGeneral Electric Co.:0000040545:\nODD LINE\n")
        self.assertEqual(names, {"GENERAL ELECTRIC CO": frozenset({40545})})

    def test_risk_free_rate_is_used_only_once_published(self):
        curve = RiskFreeCurve(parse_fred_csv("observation_date,TB3MS\n2020-04-01,0.14\n2020-05-01,.\n"
                                             "2020-05-01,0.13\n2020-06-01,0.16\n"))
        self.assertAlmostEqual(curve.rate_at(date(2020, 6, 30)), 0.0013)    # May, published early June
        self.assertAlmostEqual(curve.rate_at(date(2020, 7, 31)), 0.0016)
        self.assertTrue(math.isnan(curve.rate_at(date(2020, 4, 30))))      # nothing published yet: halt


class FakeNorgate:
    def __init__(self):
        self.securities = {}
        self.daily = {}
        self.listed = {}

    def add(self, symbol, name, delisted=False, subtype2="Operating/Holding Company", first=date(2010, 1, 4),
            last=None, daily=(), listed=None):
        self.securities[symbol] = NorgateSecurity(symbol, name, "NYSE", "Equity", subtype2, "", "USD", first, last,
                                                  delisted)
        self.daily[symbol] = list(daily)
        self.listed[symbol] = listed

    def symbols(self, database):
        delisted = database.endswith("Delisted")
        return [s for s, sec in self.securities.items() if sec.delisted == delisted]

    def security(self, symbol, database):
        return self.securities[symbol]

    def daily_bars(self, symbol, start, end):
        return [b for b in self.daily[symbol] if start <= b.date <= end]

    def major_exchange_listed(self, symbol, start, end):
        return self.listed[symbol]

    def index_levels(self, symbol, start, end):
        return [(d, 1000.0 * 1.0005 ** i) for i, d in enumerate(weekdays(start, end))]


class FakeSec:
    def __init__(self, tickers, names, facts):
        self.tickers, self.names, self.facts = tickers, names, facts

    def company_tickers(self):
        return [TickerRecord(cik, "x", ticker, "NYSE") for ticker, cik in self.tickers.items()]

    def entity_names(self):
        return self.names

    def submissions(self, cik):
        return submissions()

    def company_facts(self, cik):
        return self.facts.get(cik)


class HistoryBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        norgate = FakeNorgate()
        norgate.add("GOOD", "Good Corp Common", daily=bars(date(2014, 1, 1), date(2025, 9, 30), split_on=date(2025, 4, 15)))
        norgate.add("OLDCO-201909", "Old Company Inc Common", delisted=True, last=date(2019, 9, 13),
                    daily=bars(date(2014, 1, 1), date(2019, 9, 13)))
        norgate.add("FAKE-201812", "Fake Match Corp", delisted=True, last=date(2018, 12, 14),
                    daily=bars(date(2014, 1, 1), date(2018, 12, 14)))
        norgate.add("AMBIG-201701", "Common Name Inc", delisted=True, last=date(2017, 1, 13),
                    daily=bars(date(2014, 1, 1), date(2017, 1, 13)))
        norgate.add("MOVED-202001", "Moved Away Corp", delisted=True, last=date(2020, 1, 17),
                    daily=bars(date(2014, 1, 1), date(2020, 1, 17)),
                    listed={date(2010, 1, 4): True, date(2018, 7, 2): False})
        norgate.add("PINK", "Pink Sheet Corp", daily=bars(date(2014, 1, 1), date(2025, 9, 30)),
                    listed={date(2010, 1, 4): False})
        norgate.add("ETFX", "Some Index Fund", subtype2="Exchange Traded Fund")
        names = {normalize_company_name(n): frozenset(c) for n, c in (
            ("OLD COMPANY INC", {2}), ("FAKE MATCH CORP", {3}), ("COMMON NAME INC", {4, 5}),
            ("MOVED AWAY CORP", {6}), ("PINK SHEET CORP", {7}))}
        facts = {1: company_facts(years=9, latest_fiscal_year=2024, cik=1),
                 2: company_facts(years=5, latest_fiscal_year=2018, cik=2),
                 3: company_facts(years=5, latest_fiscal_year=2017, cik=3, public_float=1e11),   # a far bigger company
                 6: company_facts(years=5, latest_fiscal_year=2017, cik=6),
                 7: company_facts(years=5, latest_fiscal_year=2017, cik=7)}
        cls.sec = FakeSec({"GOOD": 1}, names, facts)
        cls.directory = tempfile.TemporaryDirectory()
        rates = [(date(2015, 1, 1) + timedelta(days=31 * i), 0.02) for i in range(140)]
        cls.summary = build_history_dataset(cls.sec, norgate, cls.directory.name, date(2016, 1, 1), date(2025, 9, 30),
                                            CONFIG, risk_free=rates)
        cls.provider = load_csv_directory(cls.directory.name)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_universe_and_identity_matching(self):
        self.assertEqual(sorted(self.provider.profiles), ["GOOD", "MOVED-202001", "OLDCO-201909"])
        excluded = self.summary.excluded
        self.assertIn("not verified", excluded["FAKE-201812"])
        self.assertIn("several SEC filers", excluded["AMBIG-201701"])
        self.assertIn("never listed on a major exchange", excluded["PINK"])
        self.assertNotIn("ETFX", excluded)       # never in scope
        self.assertEqual(self.summary.counts["skipped: not an operating company's common stock"], 1)
        self.assertAlmostEqual(self.summary.delisted_coverage, 0.5)
        self.assertEqual(self.provider.metadata["delisted_coverage"], 0.5)

    def test_split_dates_and_share_basis(self):
        self.assertEqual(self.provider.split_events["GOOD"], [Split(date(2025, 4, 15), 2.0)])
        before = self.provider.snapshot("GOOD", date(2025, 3, 31))
        after = self.provider.snapshot("GOOD", date(2025, 6, 30))
        self.assertAlmostEqual(before.market.price * before.latest.shares_outstanding,
                               after.market.price * after.latest.shares_outstanding)

    def test_otc_period_counts_as_delisted(self):
        last = self.provider.bars["MOVED-202001"][-1]
        self.assertEqual(last.date, date(2018, 6, 29))

    def test_rates_and_benchmark_are_stored(self):
        self.assertEqual(len(self.provider.risk_free), 140)
        self.assertGreater(self.provider.benchmark_return(date(2020, 1, 31), date(2020, 2, 28)), 0)

    def test_backtest_command_runs_on_the_dataset(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["backtest", "--data", self.directory.name, "--start", "2019-03-01", "--end", "2025-09-30"])
        text = output.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("delistings while held: 1", text)       # OLDCO was bought, then delisted
        self.assertIn("Delisted stocks covered by the dataset: 50%", text)
        self.assertIn("Benchmark: $SPXTR", text)


if __name__ == "__main__":
    unittest.main()
