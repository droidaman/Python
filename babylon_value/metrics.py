"""Financial metrics used by the gates.

Every function either returns a finite number or raises MetricUnavailable.
None of them ever substitutes a default for missing or invalid data.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from .models import FinancialPeriod, SecuritySnapshot


class MetricUnavailable(ValueError):
    """A metric cannot be computed from the data available."""


def is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def finite(value, what: str) -> float:
    if not is_finite_number(value):
        raise MetricUnavailable(f"{what} is missing or not a finite number")
    return float(value)


def ratio(numerator, denominator, what: str) -> float:
    num = finite(numerator, f"{what} numerator")
    den = finite(denominator, f"{what} denominator")
    if den == 0.0:
        raise MetricUnavailable(f"{what} has a zero denominator")
    return num / den


# --- Valuation ---------------------------------------------------------------

def market_cap(snap: "SecuritySnapshot") -> float:
    return finite(snap.market.price, "price") * finite(snap.latest.shares_outstanding, "shares outstanding")


def enterprise_value(snap: "SecuritySnapshot") -> float:
    """Market cap + debt + preferred + minority interest - cash: the price of the whole business."""
    p = snap.latest
    return (
        market_cap(snap)
        + finite(p.total_debt, "total debt")
        + finite(p.preferred_equity, "preferred equity")
        + finite(p.minority_interest, "minority interest")
        - finite(p.cash_and_equivalents, "cash")
    )


def net_current_asset_value(p: "FinancialPeriod") -> float:
    """Graham's NCAV: current assets less *all* liabilities and preferred stock."""
    return finite(p.current_assets, "current assets") - finite(p.total_liabilities, "total liabilities") - finite(
        p.preferred_equity, "preferred equity"
    )


def tangible_equity(p: "FinancialPeriod") -> float:
    return finite(p.shareholders_equity, "equity") - finite(p.intangible_assets, "intangibles") - finite(
        p.preferred_equity, "preferred equity"
    )


def free_cash_flow(p: "FinancialPeriod") -> float:
    return finite(p.cash_from_operations, "operating cash flow") - finite(p.capital_expenditure, "capex")


def gross_margin(p: "FinancialPeriod") -> float:
    return ratio(p.revenue - p.cost_of_revenue, p.revenue, "gross margin")


def current_ratio(p: "FinancialPeriod") -> float:
    return ratio(p.current_assets, p.current_liabilities, "current ratio")


def dividend_yield(snap: "SecuritySnapshot") -> float:
    return ratio(snap.latest.dividends_paid, market_cap(snap), "dividend yield")


def share_growth(history: Sequence["FinancialPeriod"]) -> float:
    if len(history) < 2:
        raise MetricUnavailable("share growth needs two annual reports")
    return ratio(history[0].shares_outstanding, history[1].shares_outstanding, "share growth") - 1.0


# --- Distress, quality and manipulation scores ----------------------------------

def altman_z(snap: "SecuritySnapshot") -> tuple[float, dict[str, float]]:
    """Altman (1968) Z-score for public companies."""
    p = snap.latest
    x1 = ratio(p.current_assets - p.current_liabilities, p.total_assets, "working capital / assets")
    x2 = ratio(p.retained_earnings, p.total_assets, "retained earnings / assets")
    x3 = ratio(p.ebit, p.total_assets, "EBIT / assets")
    x4 = ratio(market_cap(snap), p.total_liabilities, "market equity / liabilities")
    x5 = ratio(p.revenue, p.total_assets, "sales / assets")
    z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
    return z, {"x1": x1, "x2": x2, "x3": x3, "x4": x4, "x5": x5}


PIOTROSKI_SIGNALS = (
    "roa_positive",
    "cfo_positive",
    "roa_improving",
    "cash_earnings_exceed_accruals",
    "leverage_not_rising",
    "liquidity_improving",
    "no_dilution",
    "margin_improving",
    "turnover_improving",
)


def piotroski_f_score(history: Sequence["FinancialPeriod"]) -> tuple[int, dict[str, bool]]:
    """Piotroski (2000) F-score, 0-9. Uses beginning-of-year assets as in the paper."""
    if len(history) < 3:
        raise MetricUnavailable("the Piotroski F-score needs three annual reports")
    cur, prev, prev2 = history[0], history[1], history[2]
    roa = ratio(cur.net_income, prev.total_assets, "ROA")
    roa_prev = ratio(prev.net_income, prev2.total_assets, "prior ROA")
    cfo_to_assets = ratio(cur.cash_from_operations, prev.total_assets, "CFO / assets")
    leverage = ratio(cur.long_term_debt, (cur.total_assets + prev.total_assets) / 2, "leverage")
    leverage_prev = ratio(prev.long_term_debt, (prev.total_assets + prev2.total_assets) / 2, "prior leverage")
    turnover = ratio(cur.revenue, prev.total_assets, "asset turnover")
    turnover_prev = ratio(prev.revenue, prev2.total_assets, "prior asset turnover")
    signals = {
        "roa_positive": roa > 0,
        "cfo_positive": cur.cash_from_operations > 0,
        "roa_improving": roa > roa_prev,
        "cash_earnings_exceed_accruals": cfo_to_assets > roa,
        # Piotroski scores a fall in leverage. A company with no long-term debt
        # in either year also scores: being debt-free is not a weakness.
        "leverage_not_rising": leverage < leverage_prev or (leverage == 0 and leverage_prev == 0),
        "liquidity_improving": current_ratio(cur) > current_ratio(prev),
        "no_dilution": cur.shares_outstanding <= prev.shares_outstanding,
        "margin_improving": gross_margin(cur) > gross_margin(prev),
        "turnover_improving": turnover > turnover_prev,
    }
    return sum(signals.values()), signals


def beneish_m_score(history: Sequence["FinancialPeriod"]) -> tuple[float, dict[str, float]]:
    """Beneish (1999) eight-variable M-score: the probability of earnings manipulation."""
    if len(history) < 2:
        raise MetricUnavailable("the Beneish M-score needs two annual reports")
    c, p = history[0], history[1]
    dsri = ratio(ratio(c.receivables, c.revenue, "receivables / sales"),
                 ratio(p.receivables, p.revenue, "prior receivables / sales"), "DSRI")
    gmi = ratio(gross_margin(p), gross_margin(c), "GMI")
    aqi = ratio(1 - ratio(c.current_assets + c.ppe_net, c.total_assets, "hard assets / assets"),
                1 - ratio(p.current_assets + p.ppe_net, p.total_assets, "prior hard assets / assets"), "AQI")
    sgi = ratio(c.revenue, p.revenue, "SGI")
    depi = ratio(ratio(p.depreciation, p.depreciation + p.ppe_net, "prior depreciation rate"),
                 ratio(c.depreciation, c.depreciation + c.ppe_net, "depreciation rate"), "DEPI")
    sgai = ratio(ratio(c.sga_expense, c.revenue, "SG&A / sales"),
                 ratio(p.sga_expense, p.revenue, "prior SG&A / sales"), "SGAI")
    lvgi = ratio(ratio(c.current_liabilities + c.long_term_debt, c.total_assets, "leverage"),
                 ratio(p.current_liabilities + p.long_term_debt, p.total_assets, "prior leverage"), "LVGI")
    tata = ratio(c.net_income - c.cash_from_operations, c.total_assets, "TATA")
    m = (
        -4.84
        + 0.920 * dsri
        + 0.528 * gmi
        + 0.404 * aqi
        + 0.892 * sgi
        + 0.115 * depi
        - 0.172 * sgai
        + 4.679 * tata
        - 0.327 * lvgi
    )
    components = {"DSRI": dsri, "GMI": gmi, "AQI": aqi, "SGI": sgi, "DEPI": depi, "SGAI": sgai, "LVGI": lvgi, "TATA": tata}
    return m, components
