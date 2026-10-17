import json
import math
import tempfile
import unittest
from datetime import date, timedelta

from babylon_value.data.edgar import SecClient, parse_annual_reports, parse_public_float, parse_share_counts, \
    profile_from_submissions, sic_sector
from babylon_value.data.provider import point_in_time_history
from babylon_value.gates import build_entry_pipeline
from babylon_value.models import NUMERIC_FIELDS
from babylon_value.net import HttpError, RateLimiter

from .factories import AS_OF, good_period, make_context, make_snapshot
from .sec_fixtures import FactsBuilder, annual_filing, company_facts, submissions


def latest_version(periods, year_end):
    return max((p for p in periods if p.fiscal_year_end == year_end), key=lambda p: p.available_date)


class AnnualReportTests(unittest.TestCase):
    def test_round_trip_matches_the_reported_values(self):
        history = point_in_time_history(parse_annual_reports(company_facts()), AS_OF)
        self.assertGreaterEqual(len(history), 6)
        for k in range(5):
            expected, parsed = good_period(k), history[k]
            self.assertEqual(parsed.fiscal_year_end, expected.fiscal_year_end)
            for name in NUMERIC_FIELDS:
                self.assertAlmostEqual(getattr(parsed, name), getattr(expected, name), places=2, msg=f"{name} t-{k}")

    def test_parsed_company_passes_every_gate_like_the_original(self):
        history = point_in_time_history(parse_annual_reports(company_facts()), AS_OF)
        result = build_entry_pipeline().evaluate(make_snapshot(history), make_context())
        self.assertTrue(result.passed, result.render())

    def test_fourth_quarter_figures_are_not_mistaken_for_the_year(self):
        history = point_in_time_history(parse_annual_reports(company_facts()), AS_OF)
        self.assertAlmostEqual(history[0].revenue, good_period(0).revenue)

    def test_restatement_is_visible_only_from_its_filing_date(self):
        builder = FactsBuilder()
        for k in (2, 1):
            annual_filing(builder, k)
        fy2023 = good_period(1).fiscal_year_end
        fy2024_10k_filed = good_period(0).available_date
        # The FY2024 10-K restates FY2023 revenue downwards.
        builder.add("Revenues", 900e6, fy2023, "0000000001-25-000009", fy2024_10k_filed,
                    start=fy2023 - timedelta(days=364))
        periods = parse_annual_reports(builder.data)
        before = point_in_time_history(periods, fy2024_10k_filed - timedelta(days=1))
        after = point_in_time_history(periods, fy2024_10k_filed)
        self.assertAlmostEqual(before[0].revenue, good_period(1).revenue)
        self.assertEqual(after[0].fiscal_year_end, fy2023)
        self.assertAlmostEqual(after[0].revenue, 900e6)

    def test_ten_q_figures_never_enter_annual_reports(self):
        builder = FactsBuilder()
        annual_filing(builder, 0)
        fye = good_period(0).fiscal_year_end
        builder.add("Assets", 1.0, fye + timedelta(days=90), "0000000001-25-000100", fye + timedelta(days=120),
                    form="10-Q")
        periods = parse_annual_reports(builder.data)
        self.assertEqual({p.fiscal_year_end for p in periods}, {fye, good_period(1).fiscal_year_end})

    def _single(self, edit):
        builder = FactsBuilder()
        accn = annual_filing(builder, 0)
        edit(builder, accn)
        return latest_version(parse_annual_reports(builder.data), good_period(0).fiscal_year_end)

    def test_revenue_fallback_tag(self):
        def edit(b, accn):
            revenue = good_period(0).revenue
            b.remove("Revenues").add("RevenueFromContractWithCustomerExcludingAssessedTax", revenue,
                                     good_period(0).fiscal_year_end, accn, good_period(0).available_date,
                                     start=good_period(0).fiscal_year_end - timedelta(days=364))
        self.assertAlmostEqual(self._single(edit).revenue, good_period(0).revenue)

    def test_sga_built_from_its_parts(self):
        def edit(b, accn):
            p = good_period(0)
            start = p.fiscal_year_end - timedelta(days=364)
            b.remove("SellingGeneralAndAdministrativeExpense")
            b.add("GeneralAndAdministrativeExpense", 120e6, p.fiscal_year_end, accn, p.available_date, start=start)
            b.add("SellingAndMarketingExpense", 80e6, p.fiscal_year_end, accn, p.available_date, start=start)
        self.assertAlmostEqual(self._single(edit).sga_expense, 200e6)

    def test_total_liabilities_derived_when_not_tagged(self):
        def edit(b, accn):
            p = good_period(0)
            b.remove("Liabilities").add("LiabilitiesAndStockholdersEquity", p.total_assets, p.fiscal_year_end, accn,
                                        p.available_date)
        self.assertAlmostEqual(self._single(edit).total_liabilities, good_period(0).total_liabilities)

    def test_overlapping_debt_tags_take_the_larger_reading(self):
        def edit(b, accn):
            p = good_period(0)
            b.add("LongTermDebt", 150e6, p.fiscal_year_end, accn, p.available_date)
        self.assertAlmostEqual(self._single(edit).total_debt, 150e6)

    def test_interest_expense_is_zero_only_without_debt(self):
        def no_interest(b, accn):
            b.remove("InterestExpense")
        self.assertTrue(math.isnan(self._single(no_interest).interest_expense))

        def no_interest_no_debt(b, accn):
            b.remove("InterestExpense").remove("LongTermDebtNoncurrent").remove("LongTermDebtCurrent")
        period = self._single(no_interest_no_debt)
        self.assertEqual(period.interest_expense, 0.0)
        self.assertEqual(period.total_debt, 0.0)

    def test_missing_required_item_stays_missing(self):
        self.assertTrue(math.isnan(self._single(lambda b, accn: b.remove("AssetsCurrent")).current_assets))

    def test_conflicting_values_in_one_filing_are_rejected(self):
        def edit(b, accn):
            p = good_period(0)
            b.add("AssetsCurrent", 1.0, p.fiscal_year_end, accn, p.available_date)
        self.assertTrue(math.isnan(self._single(edit).current_assets))

    def test_other_currencies_are_ignored(self):
        def edit(b, accn):
            p = good_period(0)
            b.add("Assets", 5.0, p.fiscal_year_end, accn, p.available_date, unit="EUR")
        self.assertAlmostEqual(self._single(edit).total_assets, good_period(0).total_assets)


class CoverPageTests(unittest.TestCase):
    def test_share_counts_and_public_float(self):
        data = company_facts(years=2)
        counts = parse_share_counts(data)
        self.assertEqual(len(counts), 2)
        self.assertEqual(counts[-1].as_of, good_period(0).fiscal_year_end + timedelta(days=45))
        self.assertEqual(counts[-1].available_date, good_period(0).available_date)
        floats = parse_public_float(data)
        self.assertEqual(floats[-1].as_of, date(2024, 6, 30))

    def test_ambiguous_share_counts_are_skipped(self):
        builder = FactsBuilder()
        accn = annual_filing(builder, 0)
        p = good_period(0)
        builder.add("EntityCommonStockSharesOutstanding", 5e6, p.fiscal_year_end + timedelta(days=45), accn,
                    p.available_date, unit="shares", taxonomy="dei")
        self.assertEqual(parse_share_counts(builder.data), [])


class ProfileTests(unittest.TestCase):
    def test_operating_us_company(self):
        profile = profile_from_submissions("GOOD", "Nasdaq", submissions())
        self.assertEqual((profile.sector, profile.country, profile.exchange, profile.security_type),
                         ("Industrials", "US", "NASDAQ", "common_stock"))

    def test_foreign_headquarters_and_odd_tickers(self):
        self.assertEqual(profile_from_submissions("GOOD", "NYSE", submissions(state="E9")).country, "E9")
        self.assertEqual(profile_from_submissions("GOOD.WS", "NYSE", submissions()).security_type, "other")
        self.assertEqual(profile_from_submissions("FUND", "NYSE", submissions(entity_type="other")).security_type, "other")

    def test_sic_mapping(self):
        self.assertEqual(sic_sector(6022), "Financials")       # state commercial banks
        self.assertEqual(sic_sector(6798), "Real Estate")      # REITs
        self.assertEqual(sic_sector(4911), "Utilities")        # electric services
        self.assertEqual(sic_sector(2834), "Health Care")      # pharmaceuticals
        self.assertEqual(sic_sector(7372), "Information Technology")
        self.assertEqual(sic_sector(6770), "Financials")       # blank checks (SPACs)
        self.assertEqual(sic_sector(9995), "")                 # non-operating: unclassified


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        return self.responses.pop(0)


class SecClientTests(unittest.TestCase):
    def client(self, responses, clock=lambda: 1_000_000.0):
        self.sleeps = []
        self.transport = FakeTransport(responses)
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        return SecClient("Jane Doe jane@example.com", self.cache.name, transport=self.transport, clock=clock,
                         sleep=self.sleeps.append)

    def test_user_agent_is_required(self):
        for agent in ("", "babylon", "jane@example.com"):
            with self.assertRaises(ValueError):
                SecClient(agent, tempfile.gettempdir())

    def test_parses_ticker_list_and_sends_user_agent(self):
        body = json.dumps({"fields": ["cik", "name", "ticker", "exchange"],
                           "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"], [1, "Odd", None, "OTC"]]}).encode()
        client = self.client([(200, body)])
        records = client.company_tickers()
        self.assertEqual([(r.cik, r.ticker, r.exchange) for r in records], [(320193, "AAPL", "Nasdaq")])
        self.assertEqual(self.transport.calls[0][1]["User-Agent"], "Jane Doe jane@example.com")

    def test_retries_rate_limiting_then_caches(self):
        client = self.client([(429, b""), (503, b""), (200, b'{"facts": {}}')])
        self.assertEqual(client.company_facts(1), {"facts": {}})
        self.assertEqual(len(self.transport.calls), 3)
        self.assertIn(1, self.sleeps)   # exponential backoff
        self.assertEqual(client.company_facts(1), {"facts": {}})   # served from the cache
        self.assertEqual(len(self.transport.calls), 3)

    def test_expired_cache_is_refetched(self):
        now = [1_000_000.0]
        client = self.client([(200, b'{"a": 1}'), (200, b'{"a": 2}')], clock=lambda: now[0])
        self.assertEqual(client.submissions(1), {"a": 1})
        now[0] += 2 * 86_400
        self.assertEqual(client.submissions(1), {"a": 2})

    def test_missing_company_and_hard_errors(self):
        client = self.client([(404, b""), (403, b"")])
        self.assertIsNone(client.company_facts(1))
        with self.assertRaises(HttpError):
            client.company_facts(2)

    def test_rate_limiter_spaces_requests(self):
        now, slept = [0.0], []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds
        limiter = RateLimiter(4.0, clock=lambda: now[0], sleep=sleep)
        for _ in range(3):
            limiter.wait()
        self.assertEqual(slept, [0.25, 0.25])


if __name__ == "__main__":
    unittest.main()
