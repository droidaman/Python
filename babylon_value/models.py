"""Point-in-time data model.

A :class:`SecuritySnapshot` is everything the strategy is allowed to know
about one security on one decision date: its profile, its latest price and
the annual reports that had been *published* by then (``available_date``),
most recent first. Monetary amounts are in a single currency, unscaled.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date


@dataclass(frozen=True)
class FinancialPeriod:
    """One fiscal year of reported fundamentals."""

    fiscal_year_end: date
    available_date: date            # when the report became public - the look-ahead guard
    # Income statement and cash flow (fiscal-year totals)
    revenue: float
    cost_of_revenue: float
    sga_expense: float
    depreciation: float
    ebit: float
    interest_expense: float
    net_income: float
    cash_from_operations: float
    capital_expenditure: float      # cash spent, as a positive number
    dividends_paid: float           # cash paid, as a positive number
    # Balance sheet (at fiscal-year end)
    cash_and_equivalents: float
    receivables: float
    current_assets: float
    ppe_net: float
    intangible_assets: float        # goodwill plus other intangibles
    total_assets: float
    current_liabilities: float
    long_term_debt: float
    total_debt: float               # all interest-bearing debt, short and long term
    total_liabilities: float
    retained_earnings: float
    shareholders_equity: float      # total equity attributable to shareholders, incl. preferred
    shares_outstanding: float
    preferred_equity: float = 0.0
    minority_interest: float = 0.0


NUMERIC_FIELDS: tuple[str, ...] = tuple(
    f.name for f in fields(FinancialPeriod) if f.name not in ("fiscal_year_end", "available_date")
)

# Fields that can legitimately be negative are the ones that measure results:
# EBIT, net income, operating cash flow, retained earnings and equity.
NON_NEGATIVE_FIELDS: frozenset[str] = frozenset(NUMERIC_FIELDS) - {
    "ebit",
    "net_income",
    "cash_from_operations",
    "retained_earnings",
    "shareholders_equity",
}


@dataclass(frozen=True)
class SecurityProfile:
    ticker: str
    name: str
    sector: str
    exchange: str
    security_type: str      # e.g. "common_stock", "etf", "preferred", "adr", "spac"
    country: str = "US"


@dataclass(frozen=True)
class MarketData:
    as_of: date                     # date of the price observation
    price: float
    avg_daily_dollar_volume: float


@dataclass(frozen=True)
class SecuritySnapshot:
    profile: SecurityProfile
    market: MarketData
    history: tuple[FinancialPeriod, ...]   # most recent fiscal year first

    @property
    def ticker(self) -> str:
        return self.profile.ticker

    @property
    def latest(self) -> FinancialPeriod:
        return self.history[0]
