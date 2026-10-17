"""Synthetic, reproducible market data for tests and demonstrations.

The generator builds a universe of fictional companies (tickers SYN000, ...)
with internally consistent annual reports and month-end prices. Several
archetypes exist to exercise the gates: financials (outside the circle of
competence), distressed firms (some go bankrupt and delist), earnings
manipulators, serial diluters and micro-caps.

Prices are fair value multiplied by a mean-reverting mispricing term. That
mean reversion is *assumed* here - it is the deep value hypothesis itself -
so a synthetic backtest shows that the machinery works, never that the
strategy earns alpha. Only real point-in-time data can test that.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

from ..dates import month_ends
from ..models import FinancialPeriod, SecurityProfile
from .provider import InMemoryProvider, PriceBar

ARCHETYPES = (
    ("steady", 0.68),
    ("financial", 0.08),
    ("distressed", 0.08),
    ("manipulator", 0.05),
    ("diluter", 0.05),
    ("microcap", 0.06),
)
SECTORS = (
    "Industrials", "Consumer Discretionary", "Consumer Staples", "Materials",
    "Energy", "Health Care", "Information Technology", "Communication Services",
)
REPORT_LAG_DAYS = 75
TAX_RATE = 0.21


def generate_market(n_companies: int = 300, start_year: int = 2008, years: int = 15, seed: int = 7) -> InMemoryProvider:
    """Annual reports for fiscal years start_year .. start_year+years-1 and
    month-end prices from January start_year to December start_year+years."""
    rng = random.Random(seed)
    months = month_ends(date(start_year, 1, 1), date(start_year + years, 12, 31))
    market = _market_factor(rng, len(months))
    profiles, fundamentals, prices = {}, {}, {}
    for i in range(n_companies):
        archetype = _pick(rng, ARCHETYPES)
        ticker = f"SYN{i:03d}"
        profiles[ticker] = _profile(rng, ticker, archetype)
        fundamentals[ticker] = _reports(rng, archetype, start_year, years)
        prices[ticker] = _prices(rng, archetype, fundamentals[ticker], months, market)
    return InMemoryProvider(profiles, fundamentals, prices)


def _pick(rng: random.Random, weighted) -> str:
    roll, total = rng.random(), 0.0
    for name, weight in weighted:
        total += weight
        if roll < total:
            return name
    return weighted[-1][0]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _market_factor(rng: random.Random, n: int) -> list[float]:
    """Market-wide valuation swings: a mean-reverting log level."""
    level, out = 0.0, []
    for _ in range(n):
        level = 0.97 * level + rng.gauss(0.0, 0.035)
        out.append(level)
    return out


def _profile(rng: random.Random, ticker: str, archetype: str) -> SecurityProfile:
    sector = "Financials" if archetype == "financial" else rng.choice(SECTORS)
    if archetype == "microcap" and rng.random() < 0.5:
        exchange = "OTC"
    else:
        exchange = rng.choice(("NYSE", "NASDAQ"))
    return SecurityProfile(ticker, f"Synthetic Company {ticker[3:]}", sector, exchange, "common_stock")


def _reports(rng: random.Random, archetype: str, start_year: int, years: int) -> list[FinancialPeriod]:
    distressed = archetype == "distressed"
    revenue = rng.lognormvariate(math.log(20e6 if archetype == "microcap" else 600e6), 0.6)
    gross_margin = rng.uniform(0.25, 0.55)
    dep_rate = rng.uniform(0.02, 0.05)
    op_margin = rng.uniform(0.0, 0.05) if distressed else rng.uniform(0.06, 0.16)
    margin_drift = -0.02 if distressed else 0.0
    growth = rng.uniform(-0.08, -0.02) if distressed else rng.uniform(-0.02, 0.07)
    turnover = rng.uniform(0.8, 1.4)
    liability_ratio = rng.uniform(0.75, 0.9) if distressed else rng.uniform(0.25, 0.5)
    debt_share = rng.uniform(0.3, 0.6)
    receivable_rate = rng.uniform(0.10, 0.15)
    inventory_rate = rng.uniform(0.08, 0.15)
    cash_rate = rng.uniform(0.05, 0.12)
    ppe_rate = rng.uniform(0.22, 0.32)
    intangible_rate = rng.uniform(0.0, 0.08)
    retained_share = rng.uniform(0.5, 0.8)
    payout = rng.choice((0.0, 0.0, 0.25, 0.4))
    shares = rng.uniform(5e6, 20e6) if archetype == "microcap" else rng.uniform(30e6, 150e6)
    share_drift = 0.15 if archetype == "diluter" else -0.01

    periods = []
    for k in range(years):
        fiscal_year_end = date(start_year + k, 12, 31)
        revenue *= math.exp(rng.gauss(growth, 0.06))
        op_margin = _clamp(op_margin + margin_drift + rng.gauss(0.0, 0.01), -0.15, 0.30)
        gm = _clamp(gross_margin + rng.gauss(0.0, 0.01), 0.10, 0.80)
        cost_of_revenue = revenue * (1 - gm)
        depreciation = revenue * dep_rate
        ebit = revenue * op_margin
        sga = revenue * gm - depreciation - ebit
        if sga < 0.02 * revenue:
            sga = 0.02 * revenue
            ebit = revenue * gm - depreciation - sga

        total_assets = revenue / turnover * math.exp(rng.gauss(0.0, 0.03))
        total_liabilities = total_assets * _clamp(liability_ratio + rng.gauss(0.0, 0.02), 0.05, 0.97)
        total_debt = total_liabilities * debt_share
        long_term_debt = total_debt * 0.75
        current_liabilities = total_liabilities * 0.4
        interest = total_debt * 0.05
        pretax = ebit - interest
        net_income = pretax * (1 - TAX_RATE) if pretax > 0 else pretax
        receivables = revenue * receivable_rate * math.exp(rng.gauss(0.0, 0.03))
        operating_cash = net_income + depreciation + revenue * rng.gauss(0.0, 0.01)
        if archetype == "manipulator" and k >= years - 4:
            # Aggressive revenue recognition: receivables balloon, cash does not arrive.
            receivables *= 1.0 + 0.6 * (k - (years - 5))
            operating_cash = -0.5 * abs(net_income)
        cash = total_assets * cash_rate
        inventory = revenue * inventory_rate
        current_assets = cash + receivables + inventory
        if current_assets > 0.5 * total_assets:
            scale = 0.5 * total_assets / current_assets
            cash, receivables, inventory = cash * scale, receivables * scale, inventory * scale
            current_assets = cash + receivables + inventory
        equity = total_assets - total_liabilities
        if k:
            shares *= math.exp(share_drift + rng.gauss(0.0, 0.005))

        periods.append(FinancialPeriod(
            fiscal_year_end=fiscal_year_end,
            available_date=fiscal_year_end + timedelta(days=REPORT_LAG_DAYS),
            revenue=revenue,
            cost_of_revenue=cost_of_revenue,
            sga_expense=sga,
            depreciation=depreciation,
            ebit=ebit,
            interest_expense=interest,
            net_income=net_income,
            cash_from_operations=operating_cash,
            capital_expenditure=depreciation * rng.uniform(0.8, 1.3),
            dividends_paid=max(net_income, 0.0) * payout,
            cash_and_equivalents=cash,
            receivables=receivables,
            current_assets=current_assets,
            ppe_net=total_assets * ppe_rate,
            intangible_assets=total_assets * intangible_rate,
            total_assets=total_assets,
            current_liabilities=current_liabilities,
            long_term_debt=long_term_debt,
            total_debt=total_debt,
            total_liabilities=total_liabilities,
            retained_earnings=equity * retained_share if equity > 0 else equity,
            shareholders_equity=equity,
            shares_outstanding=shares,
        ))
    return periods


def _prices(rng: random.Random, archetype: str, periods: list[FinancialPeriod], months: list[date],
            market: list[float]) -> list[PriceBar]:
    fair_multiple = rng.uniform(9.0, 14.0)
    beta = rng.uniform(0.7, 1.3)
    mispricing = rng.gauss(0.0, 0.25)
    daily_turnover = rng.uniform(0.0005, 0.002) if archetype == "microcap" else rng.uniform(0.002, 0.006)
    by_year = {p.fiscal_year_end.year: p for p in periods}
    first_year = min(by_year)
    bankrupt_at = None
    if archetype == "distressed" and rng.random() < 0.5:
        bankrupt_at = rng.randrange(len(months) // 2, len(months))

    bars = []
    for index, month in enumerate(months):
        if bankrupt_at is not None and index > bankrupt_at:
            break
        # The market prices last year's economics (it knows them before they are filed).
        report = by_year.get(month.year - 1, by_year[first_year])
        mispricing = 0.97 * mispricing + rng.gauss(0.0, 0.06)
        fair_multiple *= math.exp(rng.gauss(0.0, 0.03))   # what the business is worth also changes
        fair_ev = fair_multiple * max(report.ebit, 0.02 * report.revenue)
        fair_price = max((fair_ev - report.total_debt + report.cash_and_equivalents) / report.shares_outstanding, 0.25)
        log_level = mispricing + beta * market[index]
        if bankrupt_at is not None and bankrupt_at - index < 12:
            log_level -= 0.25 * (12 - (bankrupt_at - index))
        price = round(fair_price * math.exp(log_level), 4)
        dividend = report.dividends_paid / report.shares_outstanding / 4 if month.month in (3, 6, 9, 12) else 0.0
        bars.append(PriceBar(month, price, report.shares_outstanding * daily_turnover, dividend))
    return bars
