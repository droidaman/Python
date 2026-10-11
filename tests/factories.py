"""Hand-built companies with known, checkable numbers.

``good_history`` describes a healthy, improving, cheap business: at a $10
share price it passes every entry gate (EV/EBIT 6.25, F-score 8, Altman Z
about 4.2, Beneish M about -2.67).
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

from babylon_value.config import StrategyConfig
from babylon_value.logic import EvaluationContext
from babylon_value.models import FinancialPeriod, MarketData, SecurityProfile, SecuritySnapshot

AS_OF = date(2025, 6, 30)
M = 1_000_000.0


def good_period(years_back: int, latest_fiscal_year: int = 2024) -> FinancialPeriod:
    k = years_back
    g = 0.95 ** k
    revenue = 1000 * M * g
    margin = 0.40 - 0.005 * k
    ebit = revenue * (0.16 - 0.004 * k)
    depreciation = 40 * M * g
    cost_of_revenue = revenue * (1 - margin)
    net_income = (ebit - 5 * M) * 0.79
    total_assets = 1000 * M * g
    total_liabilities = total_assets * (0.35 + 0.01 * k)
    fiscal_year_end = date(latest_fiscal_year - k, 12, 31)
    return FinancialPeriod(
        fiscal_year_end=fiscal_year_end,
        available_date=fiscal_year_end + timedelta(days=60),
        revenue=revenue,
        cost_of_revenue=cost_of_revenue,
        sga_expense=revenue - cost_of_revenue - depreciation - ebit,
        depreciation=depreciation,
        ebit=ebit,
        interest_expense=5 * M,
        net_income=net_income,
        cash_from_operations=net_income + depreciation + 10 * M * g,
        capital_expenditure=45 * M * g,
        dividends_paid=30 * M * g,
        cash_and_equivalents=100 * M * g,
        receivables=120 * M * g,
        current_assets=400 * M * g,
        ppe_net=300 * M * g,
        intangible_assets=50 * M * g,
        total_assets=total_assets,
        current_liabilities=150 * M * g * (1 + 0.02 * k),
        long_term_debt=80 * M * g * (1 + 0.03 * k),
        total_debt=100 * M * g * (1 + 0.03 * k),
        total_liabilities=total_liabilities,
        retained_earnings=450 * M * g,
        shareholders_equity=total_assets - total_liabilities,
        shares_outstanding=100 * M * (1 + 0.01 * k),
    )


def good_history(years: int = 5, latest_fiscal_year: int = 2024, **latest_overrides) -> tuple[FinancialPeriod, ...]:
    history = [good_period(k, latest_fiscal_year) for k in range(years)]
    if latest_overrides:
        history[0] = dataclasses.replace(history[0], **latest_overrides)
    return tuple(history)


def make_snapshot(
    history=None,
    price: float = 10.0,
    adv: float = 5 * M,
    price_date: date = AS_OF,
    ticker: str = "GOOD",
    sector: str = "Industrials",
    exchange: str = "NYSE",
    security_type: str = "common_stock",
) -> SecuritySnapshot:
    return SecuritySnapshot(
        profile=SecurityProfile(ticker, f"{ticker} Corp", sector, exchange, security_type),
        market=MarketData(price_date, price, adv),
        history=good_history() if history is None else tuple(history),
    )


def make_context(as_of: date = AS_OF, risk_free_rate: float = 0.045, config: StrategyConfig | None = None):
    return EvaluationContext(as_of, (config or StrategyConfig()).validate(), risk_free_rate)
