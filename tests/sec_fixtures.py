"""SEC EDGAR responses in their real JSON layout, built from the healthy test
company in factories.py so parsed results can be checked against known values."""

from __future__ import annotations

from datetime import date, timedelta

from .factories import good_period

BALANCE_TAGS = {
    "cash_and_equivalents": "CashAndCashEquivalentsAtCarryingValue",
    "receivables": "AccountsReceivableNetCurrent",
    "current_assets": "AssetsCurrent",
    "ppe_net": "PropertyPlantAndEquipmentNet",
    "intangible_assets": "IntangibleAssetsNetIncludingGoodwill",
    "total_assets": "Assets",
    "current_liabilities": "LiabilitiesCurrent",
    "long_term_debt": "LongTermDebtNoncurrent",
    "total_liabilities": "Liabilities",
    "retained_earnings": "RetainedEarningsAccumulatedDeficit",
    "shareholders_equity": "StockholdersEquity",
}
FLOW_TAGS = {
    "revenue": "Revenues",
    "cost_of_revenue": "CostOfRevenue",
    "sga_expense": "SellingGeneralAndAdministrativeExpense",
    "depreciation": "DepreciationDepletionAndAmortization",
    "ebit": "OperatingIncomeLoss",
    "interest_expense": "InterestExpense",
    "net_income": "NetIncomeLoss",
    "cash_from_operations": "NetCashProvidedByUsedInOperatingActivities",
    "capital_expenditure": "PaymentsToAcquirePropertyPlantAndEquipment",
    "dividends_paid": "PaymentsOfDividends",
}


class FactsBuilder:
    def __init__(self, cik: int = 1, name: str = "Good Corp"):
        self.data = {"cik": cik, "entityName": name, "facts": {}}

    def add(self, concept, value, end, accn, filed, form="10-K", start=None, unit="USD", taxonomy="us-gaap"):
        entry = {"end": end.isoformat(), "val": value, "accn": accn, "fy": end.year, "fp": "FY", "form": form,
                 "filed": filed.isoformat()}
        if start is not None:
            entry["start"] = start.isoformat()
        units = self.data["facts"].setdefault(taxonomy, {}).setdefault(concept, {"label": concept, "units": {}})["units"]
        units.setdefault(unit, []).append(entry)
        return self

    def remove(self, concept, taxonomy="us-gaap"):
        self.data["facts"].get(taxonomy, {}).pop(concept, None)
        return self


def annual_filing(builder: FactsBuilder, years_back: int, latest_fiscal_year: int = 2024,
                  shares_override: float | None = None, public_float: float | None = None) -> str:
    """One 10-K: two balance sheets, three years of flows, cover-page facts."""
    current = good_period(years_back, latest_fiscal_year)
    fye = current.fiscal_year_end
    filed = current.available_date
    accn = f"0000000001-{fye.year % 100 + 1:02d}-000001"
    for k in (0, 1):
        period = good_period(years_back + k, latest_fiscal_year)
        for field, tag in BALANCE_TAGS.items():
            builder.add(tag, getattr(period, field), period.fiscal_year_end, accn, filed)
        builder.add("LongTermDebtCurrent", period.total_debt - period.long_term_debt, period.fiscal_year_end, accn, filed)
        builder.add("CommonStockSharesOutstanding", period.shares_outstanding, period.fiscal_year_end, accn, filed,
                    unit="shares")
    for k in (0, 1, 2):
        period = good_period(years_back + k, latest_fiscal_year)
        start = period.fiscal_year_end - timedelta(days=364)
        for field, tag in FLOW_TAGS.items():
            builder.add(tag, getattr(period, field), period.fiscal_year_end, accn, filed, start=start)
    # A fourth-quarter figure inside the 10-K must not be mistaken for the year.
    builder.add("Revenues", current.revenue / 4, fye, accn, filed, start=fye - timedelta(days=91))
    cover = fye + timedelta(days=45)
    builder.add("EntityCommonStockSharesOutstanding", shares_override or current.shares_outstanding, cover, accn,
                filed, unit="shares", taxonomy="dei")
    builder.add("EntityPublicFloat", public_float or 0.8 * 10.0 * current.shares_outstanding,
                date(fye.year, 6, 30), accn, filed, taxonomy="dei")
    return accn


def company_facts(years: int = 6, latest_fiscal_year: int = 2024, cik: int = 1, **kwargs) -> dict:
    builder = FactsBuilder(cik)
    for k in reversed(range(years)):
        annual_filing(builder, k, latest_fiscal_year, **kwargs)
    return builder.data


def submissions(name="Good Corp", sic="3560", state="OH", entity_type="operating") -> dict:
    return {"cik": "0000000001", "entityType": entity_type, "sic": sic, "sicDescription": "General industrial machinery",
            "name": name, "tickers": [], "exchanges": [],
            "addresses": {"business": {"stateOrCountry": state, "city": "Somewhere"}}}
