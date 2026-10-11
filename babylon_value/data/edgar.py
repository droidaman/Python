"""SEC EDGAR: company profiles and point-in-time annual reports from XBRL.

Sources (all free; the SEC asks for a User-Agent naming you and your email,
and at most 10 requests per second):

* https://www.sec.gov/files/company_tickers_exchange.json  ticker, CIK, exchange
* https://data.sec.gov/submissions/CIK##########.json      SIC code, address, entity type
* https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json   every XBRL fact filed

How annual reports are built
----------------------------
Every fact carries the accession number and the date the filing was made.
For each fiscal year-end, the filings that reported values for it (the 10-K
itself, later 10-Ks restating it as a comparative, 10-K/As) are applied in
filing order, and each one produces a new *version* of that year, available
from its filing date. So a restatement is only visible from the day it was
published - never before.

Mapping XBRL tags to fields
---------------------------
* Required fields use a list of tags in priority order. If none is filed the
  value stays missing (NaN) and the integrity gates reject the company.
* A few items are legitimately absent when they are zero (dividends, preferred
  stock, minority interest, intangibles, PP&E): XBRL filers tag every line on
  the face of their statements, so an untagged line does not exist.
* Debt is the riskiest field to get wrong, because understating it flatters
  both safety and value. When debt tags overlap, the larger interpretation is
  used: overstating debt can only make a stock look worse.
* Interest expense is assumed zero only when no debt is reported at all.
"""

from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Mapping

from ..models import FinancialPeriod, SecurityProfile
from ..net import FileCache, JsonClient, RateLimiter, Transport
from .provider import ShareCount

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

ANNUAL_FORMS = frozenset({"10-K", "10-K/A"})
COVER_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
ANNUAL_DAYS = (350, 380)            # 52/53-week fiscal years included
PLAIN_TICKER = re.compile(r"^[A-Z]{1,5}$")

US_STATES = frozenset("""
AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR
PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR
""".split())

_EXCHANGES = {"nasdaq": "NASDAQ", "nyse": "NYSE", "nyse american": "NYSE American", "nyse mkt": "NYSE American",
              "amex": "NYSE American"}

# ---------------------------------------------------------------------------- tag tables

REQUIRED_TAGS: dict[str, tuple[str, ...]] = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet",
                "SalesRevenueServicesNet"),
    "cost_of_revenue": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices",
                        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"),
    "depreciation": ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
                     "DepreciationAmortizationAndAccretionNet", "Depreciation"),
    "net_income": ("NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"),
    "cash_from_operations": ("NetCashProvidedByUsedInOperatingActivities",
                             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "capital_expenditure": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
                            "PaymentsForCapitalImprovements"),
    "cash_and_equivalents": ("CashAndCashEquivalentsAtCarryingValue", "Cash"),
    "receivables": ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent",
                    "AccountsNotesAndLoansReceivableNetCurrent"),
    "current_assets": ("AssetsCurrent",),
    "total_assets": ("Assets",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "retained_earnings": ("RetainedEarningsAccumulatedDeficit",),
    "shareholders_equity": ("StockholdersEquity",),
    "shares_outstanding": ("CommonStockSharesOutstanding", "dei:EntityCommonStockSharesOutstanding",
                           "WeightedAverageNumberOfDilutedSharesOutstanding"),
}

ZERO_IF_ABSENT_TAGS: dict[str, tuple[str, ...]] = {
    "dividends_paid": ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"),
    "ppe_net": ("PropertyPlantAndEquipmentNet",
                "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"),
    "preferred_equity": ("PreferredStockValue", "PreferredStockValueOutstanding"),
    "minority_interest": ("MinorityInterest",),
}

OPERATING_INCOME = ("OperatingIncomeLoss",)
PRETAX_INCOME = ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                 "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments")
SGA = ("SellingGeneralAndAdministrativeExpense",)
GENERAL_ADMIN = ("GeneralAndAdministrativeExpense",)
SELLING = ("SellingAndMarketingExpense", "SellingExpense")
INTANGIBLES_WITH_GOODWILL = ("IntangibleAssetsNetIncludingGoodwill",)
GOODWILL = ("Goodwill",)
INTANGIBLES_EX_GOODWILL = ("IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet")
LIABILITIES = ("Liabilities",)
LIABILITIES_AND_EQUITY = ("LiabilitiesAndStockholdersEquity",)
LONG_TERM_DEBT_NONCURRENT = ("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
                             "LongTermNotesPayable", "LongTermLineOfCredit", "SeniorLongTermNotes")
LONG_TERM_DEBT_CURRENT = ("LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent", "DebtCurrent")
LONG_TERM_DEBT_TOTAL = ("LongTermDebt",)
SHORT_TERM_DEBT = ("ShortTermBorrowings", "CommercialPaper", "LinesOfCreditCurrent", "NotesPayableCurrent")
FINANCE_LEASES = ("FinanceLeaseLiabilityNoncurrent", "FinanceLeaseLiabilityCurrent")
INTEREST_EXPENSE = ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt", "InterestAndDebtExpense")

SHARE_CONCEPTS = frozenset({"CommonStockSharesOutstanding", "dei:EntityCommonStockSharesOutstanding",
                            "WeightedAverageNumberOfDilutedSharesOutstanding"})


def _all_concepts() -> frozenset[str]:
    names = set(SHARE_CONCEPTS)
    for table in (REQUIRED_TAGS, ZERO_IF_ABSENT_TAGS):
        for tags in table.values():
            names.update(tags)
    for tags in (OPERATING_INCOME, PRETAX_INCOME, SGA, GENERAL_ADMIN, SELLING, INTANGIBLES_WITH_GOODWILL, GOODWILL,
                 INTANGIBLES_EX_GOODWILL, LIABILITIES, LIABILITIES_AND_EQUITY, LONG_TERM_DEBT_NONCURRENT,
                 LONG_TERM_DEBT_CURRENT, LONG_TERM_DEBT_TOTAL, SHORT_TERM_DEBT, FINANCE_LEASES, INTEREST_EXPENSE):
        names.update(tags)
    return frozenset(names)


CONCEPTS = _all_concepts()
_CONFLICT = object()   # the same filing reported two different values for one item


# ---------------------------------------------------------------------------- client

@dataclass(frozen=True)
class TickerRecord:
    cik: int
    name: str
    ticker: str
    exchange: str


@dataclass(frozen=True)
class PublicFloat:
    as_of: date
    available_date: date
    value: float


class SecClient:
    def __init__(
        self,
        user_agent: str,
        cache_dir: str | Path,
        transport: Transport | None = None,
        requests_per_second: float = 8.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        agent = (user_agent or "").strip()
        if "@" not in agent or len(agent.split()) < 2:
            raise ValueError("the SEC requires a User-Agent naming you and your email, e.g. 'Jane Doe jane@example.com' "
                             "(set SEC_USER_AGENT)")
        if requests_per_second > 10:
            raise ValueError("the SEC allows at most 10 requests per second")
        self._http = JsonClient({"User-Agent": agent}, RateLimiter(requests_per_second, sleep=sleep),
                                FileCache(cache_dir), transport, clock=clock, sleep=sleep)

    def company_tickers(self, max_age: float = 86_400) -> list[TickerRecord]:
        data = self._http.get_json(TICKERS_URL, max_age) or {}
        columns = data.get("fields", [])
        try:
            index = {name: columns.index(name) for name in ("cik", "name", "ticker", "exchange")}
        except ValueError:
            raise ValueError("unexpected company_tickers_exchange.json layout") from None
        records = []
        for row in data.get("data", []):
            ticker = row[index["ticker"]]
            if isinstance(ticker, str) and ticker:
                records.append(TickerRecord(int(row[index["cik"]]), str(row[index["name"]] or ""), ticker.upper(),
                                            str(row[index["exchange"]] or "")))
        return records

    def submissions(self, cik: int, max_age: float = 86_400) -> dict | None:
        return self._http.get_json(SUBMISSIONS_URL.format(cik=cik), max_age)

    def company_facts(self, cik: int, max_age: float = 86_400) -> dict | None:
        return self._http.get_json(FACTS_URL.format(cik=cik), max_age)


# ---------------------------------------------------------------------------- profiles

def normalize_exchange(name: str) -> str:
    return _EXCHANGES.get((name or "").strip().lower(), (name or "").strip())


def profile_from_submissions(ticker: str, exchange: str, submissions: Mapping) -> SecurityProfile:
    sic_text = str(submissions.get("sic") or "")
    sector = sic_sector(int(sic_text)) if sic_text.isdigit() else ""
    address = (submissions.get("addresses") or {}).get("business") or {}
    state = str(address.get("stateOrCountry") or "").upper()
    country = "US" if state in US_STATES else (state or "unknown")
    operating = submissions.get("entityType") == "operating"
    security_type = "common_stock" if operating and PLAIN_TICKER.match(ticker) else "other"
    return SecurityProfile(ticker, str(submissions.get("name") or ticker), sector, normalize_exchange(exchange),
                           security_type, country)


def sic_sector(sic: int) -> str:
    """Map a US SIC code to a GICS-style sector name. Unknown codes map to ''
    (unclassified), which the sector gate rejects."""
    special = {2834: "Health Care", 2835: "Health Care", 2836: "Health Care", 2833: "Health Care",
               2844: "Consumer Staples", 5122: "Health Care", 5912: "Consumer Staples", 6798: "Real Estate",
               8731: "Health Care"}
    if sic in special:
        return special[sic]
    ranges = (
        (100, 999, "Consumer Staples"), (1000, 1199, "Materials"), (1200, 1399, "Energy"), (1400, 1499, "Materials"),
        (1500, 1799, "Industrials"), (2000, 2199, "Consumer Staples"), (2200, 2399, "Consumer Discretionary"),
        (2400, 2499, "Materials"), (2500, 2599, "Consumer Discretionary"), (2600, 2699, "Materials"),
        (2700, 2799, "Communication Services"), (2800, 2899, "Materials"), (2900, 2999, "Energy"),
        (3000, 3099, "Materials"), (3100, 3199, "Consumer Discretionary"), (3200, 3399, "Materials"),
        (3400, 3499, "Industrials"), (3570, 3579, "Information Technology"), (3500, 3599, "Industrials"),
        (3600, 3699, "Information Technology"), (3710, 3716, "Consumer Discretionary"), (3700, 3799, "Industrials"),
        (3840, 3851, "Health Care"), (3800, 3899, "Information Technology"), (3900, 3999, "Consumer Discretionary"),
        (4000, 4799, "Industrials"), (4800, 4899, "Communication Services"), (4950, 4959, "Industrials"),
        (4900, 4999, "Utilities"), (5140, 5149, "Consumer Staples"), (5000, 5199, "Industrials"),
        (5400, 5499, "Consumer Staples"), (5200, 5999, "Consumer Discretionary"), (6500, 6599, "Real Estate"),
        (6000, 6799, "Financials"), (7000, 7099, "Consumer Discretionary"), (7200, 7299, "Consumer Discretionary"),
        (7370, 7379, "Information Technology"), (7300, 7399, "Industrials"), (7500, 7599, "Consumer Discretionary"),
        (7800, 7899, "Communication Services"), (7900, 7999, "Consumer Discretionary"), (8000, 8099, "Health Care"),
        (8200, 8299, "Consumer Discretionary"), (8100, 8199, "Industrials"), (8700, 8799, "Industrials"),
    )
    for low, high, sector in ranges:
        if low <= sic <= high:
            return sector
    return ""


# ---------------------------------------------------------------------------- facts

@dataclass(frozen=True)
class _Fact:
    concept: str
    value: float
    start: date | None
    end: date
    accession: str
    filed: date
    form: str


def _facts(company_facts: Mapping, concepts: Iterable[str], forms: frozenset[str]) -> dict[str, list[_Fact]]:
    wanted = set(concepts)
    out: dict[str, list[_Fact]] = defaultdict(list)
    for taxonomy in ("us-gaap", "dei"):
        for concept, body in ((company_facts.get("facts") or {}).get(taxonomy) or {}).items():
            key = concept if taxonomy == "us-gaap" else f"dei:{concept}"
            if key not in wanted:
                continue
            unit = "shares" if key in SHARE_CONCEPTS else "USD"
            for item in ((body or {}).get("units") or {}).get(unit, []):
                fact = _parse_fact(key, item)
                if fact is not None and fact.form in forms:
                    out[key].append(fact)
    return out


def _parse_fact(concept: str, item: Mapping) -> _Fact | None:
    value = item.get("val")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    try:
        start = date.fromisoformat(item["start"]) if item.get("start") else None
        return _Fact(concept, float(value), start, date.fromisoformat(item["end"]), str(item["accn"]),
                     date.fromisoformat(item["filed"]), str(item.get("form", "")))
    except (KeyError, TypeError, ValueError):
        return None


def parse_annual_reports(company_facts: Mapping) -> list[FinancialPeriod]:
    """Every published version of every fiscal year, oldest filing first."""
    facts = _facts(company_facts, CONCEPTS, ANNUAL_FORMS)
    assets = facts.get("Assets", [])
    fiscal_year_ends = {f.end for f in assets}
    primary_year_end: dict[str, date] = {}
    filed: dict[str, date] = {}
    for fact in assets:
        primary_year_end[fact.accession] = max(primary_year_end.get(fact.accession, fact.end), fact.end)
    for items in facts.values():
        for fact in items:
            filed[fact.accession] = min(filed.get(fact.accession, fact.filed), fact.filed)

    # reported[year_end][accession][concept] = value
    reported: dict[date, dict[str, dict[str, object]]] = defaultdict(lambda: defaultdict(dict))
    for concept, items in facts.items():
        for fact in items:
            if concept.startswith("dei:"):
                # Cover-page facts are dated at the cover, not the year-end.
                year_end = primary_year_end.get(fact.accession)
                if year_end is None:
                    continue
            else:
                year_end = fact.end
                if year_end not in fiscal_year_ends:
                    continue
                if fact.start is not None and not ANNUAL_DAYS[0] <= (fact.end - fact.start).days <= ANNUAL_DAYS[1]:
                    continue
            values = reported[year_end][fact.accession]
            previous = values.get(concept)
            values[concept] = fact.value if previous is None or previous == fact.value else _CONFLICT

    periods = []
    for year_end in sorted(reported):
        merged: dict[str, object] = {}
        for accession in sorted(reported[year_end], key=lambda a: (filed[a], a)):
            merged.update(reported[year_end][accession])
            periods.append(_period(year_end, filed[accession], merged))
    return periods


def _period(year_end: date, available: date, values: Mapping[str, object]) -> FinancialPeriod:
    def first(tags: tuple[str, ...]) -> float | None:
        for tag in tags:
            value = values.get(tag)
            if value is _CONFLICT:
                return math.nan
            if value is not None:
                return float(value)
        return None

    def required(tags: tuple[str, ...]) -> float:
        value = first(tags)
        return math.nan if value is None else value

    def zero_if_absent(tags: tuple[str, ...]) -> float:
        value = first(tags)
        return 0.0 if value is None else value

    fields = {name: required(tags) for name, tags in REQUIRED_TAGS.items()}
    fields.update({name: zero_if_absent(tags) for name, tags in ZERO_IF_ABSENT_TAGS.items()})

    # Debt: take the larger of the overlapping interpretations.
    noncurrent, current = first(LONG_TERM_DEBT_NONCURRENT), first(LONG_TERM_DEBT_CURRENT)
    total_long_term, short_term = first(LONG_TERM_DEBT_TOTAL), first(SHORT_TERM_DEBT)
    leases = sum(zero_if_absent((tag,)) for tag in FINANCE_LEASES)
    total_debt = max((noncurrent or 0.0) + (current or 0.0) + (short_term or 0.0),
                     (total_long_term or 0.0) + (short_term or 0.0)) + leases
    fields["total_debt"] = total_debt
    fields["long_term_debt"] = noncurrent if noncurrent is not None else max(0.0, (total_long_term or 0.0) - (current or 0.0))

    interest = first(INTEREST_EXPENSE)
    if interest is None:
        interest = 0.0 if total_debt == 0 else math.nan
    fields["interest_expense"] = interest

    # EBIT: operating income; otherwise pre-tax income plus interest expense.
    ebit = first(OPERATING_INCOME)
    if ebit is None:
        pretax = first(PRETAX_INCOME)
        ebit = pretax + interest if pretax is not None and math.isfinite(interest) else math.nan
    fields["ebit"] = ebit

    sga = first(SGA)
    if sga is None:
        general = first(GENERAL_ADMIN)
        sga = general + (first(SELLING) or 0.0) if general is not None else math.nan
    fields["sga_expense"] = sga

    combined = first(INTANGIBLES_WITH_GOODWILL)
    fields["intangible_assets"] = combined if combined is not None else (
        zero_if_absent(GOODWILL) + zero_if_absent(INTANGIBLES_EX_GOODWILL))

    liabilities = first(LIABILITIES)
    if liabilities is None:
        total = first(LIABILITIES_AND_EQUITY)
        # Anything that is not shareholders' equity or minority interest
        # (including mezzanine equity) is counted as a liability.
        liabilities = (total - fields["shareholders_equity"] - fields["minority_interest"]
                       if total is not None else math.nan)
    fields["total_liabilities"] = liabilities

    return FinancialPeriod(fiscal_year_end=year_end, available_date=available, **fields)


def parse_share_counts(company_facts: Mapping) -> list[ShareCount]:
    """Cover-page share counts from 10-K and 10-Q filings: fresher than the
    annual balance sheet. A filing reporting several different counts
    (multiple share classes) is skipped rather than guessed at."""
    by_accession: dict[str, list[_Fact]] = defaultdict(list)
    for fact in _facts(company_facts, {"dei:EntityCommonStockSharesOutstanding"}, COVER_FORMS).get(
            "dei:EntityCommonStockSharesOutstanding", []):
        by_accession[fact.accession].append(fact)
    counts = []
    for items in by_accession.values():
        if len({(f.end, f.value) for f in items}) == 1 and items[0].value > 0:
            counts.append(ShareCount(items[0].end, items[0].filed, items[0].value))
    return sorted(counts, key=lambda c: (c.as_of, c.available_date))


def parse_public_float(company_facts: Mapping) -> list[PublicFloat]:
    """Market value of shares held by non-affiliates, from 10-K cover pages."""
    found = []
    for item in (((company_facts.get("facts") or {}).get("dei") or {}).get("EntityPublicFloat") or {}).get(
            "units", {}).get("USD", []):
        fact = _parse_fact("dei:EntityPublicFloat", item)
        if fact is not None and fact.form in ANNUAL_FORMS and fact.value > 0:
            found.append(PublicFloat(fact.end, fact.filed, fact.value))
    return sorted(found, key=lambda f: (f.as_of, f.available_date))
