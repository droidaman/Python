"""Orders and the pre-trade risk gates every order must pass.

The engine already sizes orders inside every limit; these checks are an
independent second line of defence. Some rules here are hard-coded on
purpose and cannot be configured away:

* long only - a sell can never exceed the shares held (no short selling);
* no leverage - buys must be paid from cash above the reserve;
* whole shares and limit orders only, priced near the reference price;
* a buy is only possible for a security that passed every entry gate today.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Collection, Mapping, Sequence

from .metrics import is_finite_number
from .principles import Principle

if TYPE_CHECKING:
    from .config import StrategyConfig
    from .costs import CostModel
    from .portfolio import PortfolioState

_EPSILON = 1e-6


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Order:
    ticker: str
    sector: str
    side: Side
    shares: int
    limit_price: float
    reference_price: float
    reason: str
    principle: Principle

    @property
    def notional(self) -> float:
        return self.shares * self.limit_price

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "side": self.side.value,
            "shares": self.shares,
            "limit_price": round(self.limit_price, 4),
            "reference_price": round(self.reference_price, 4),
            "reason": self.reason,
            "principle": self.principle.value,
        }


@dataclass(frozen=True)
class OrderDecision:
    order: Order
    approved: bool
    violations: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {**self.order.to_dict(), "approved": self.approved, "violations": list(self.violations)}


def limit_price_for(side: Side, reference_price: float, band: float) -> float:
    """Limit prices sit half the sanity band away from the reference price."""
    offset = band / 2
    return reference_price * (1 + offset) if side is Side.BUY else reference_price * (1 - offset)


def _basic_violations(order: Order, side: Side, config: "StrategyConfig") -> list[str]:
    violations = []
    if order.side is not side:
        violations.append(f"expected a {side.value} order")
    if not isinstance(order.ticker, str) or not order.ticker:
        violations.append("missing ticker")
    if type(order.shares) is not int or order.shares <= 0:
        violations.append("shares must be a positive whole number")
    if not is_finite_number(order.reference_price) or order.reference_price <= 0:
        violations.append("invalid reference price")
    elif not is_finite_number(order.limit_price) or order.limit_price <= 0:
        violations.append("invalid limit price")
    elif abs(order.limit_price / order.reference_price - 1) > config.portfolio.limit_price_band + 1e-9:
        violations.append("limit price outside the sanity band around the reference price")
    return violations


def check_sells(
    orders: Sequence[Order],
    state: "PortfolioState",
    adv: Mapping[str, float],
    config: "StrategyConfig",
) -> list[OrderDecision]:
    decisions = []
    pending: dict[str, int] = defaultdict(int)
    for order in orders:
        violations = _basic_violations(order, Side.SELL, config)
        if not violations:
            held = state.positions[order.ticker].shares if order.ticker in state.positions else 0
            if pending[order.ticker] + order.shares > held:
                violations.append(f"sells {pending[order.ticker] + order.shares} shares but {held} are held "
                                  "(short selling is never allowed)")
            liquidity = adv.get(order.ticker)
            if not is_finite_number(liquidity) or order.notional > config.costs.max_participation_of_adv * liquidity + _EPSILON:
                violations.append("exceeds the liquidity cap")
            closes_position = pending[order.ticker] + order.shares == held
            if order.notional < config.costs.min_trade_value and not closes_position:
                violations.append("below the minimum trade size")
        approved = not violations
        if approved:
            pending[order.ticker] += order.shares
        decisions.append(OrderDecision(order, approved, tuple(violations)))
    return decisions


def check_buys(
    orders: Sequence[Order],
    state: "PortfolioState",
    adv: Mapping[str, float],
    approved_tickers: Collection[str],
    excluded_tickers: Collection[str],
    config: "StrategyConfig",
    cost_model: "CostModel",
) -> list[OrderDecision]:
    p, c = config.portfolio, config.costs
    nav = state.nav
    spendable = state.cash - p.cash_reserve * nav
    position_values = {ticker: state.position_value(ticker) for ticker in state.positions}
    sector_values = state.sector_values()
    decisions = []
    for order in orders:
        violations = _basic_violations(order, Side.BUY, config)
        cash_needed = 0.0
        if not violations:
            cash_needed = order.notional + cost_model.commission(order.shares)
            if order.ticker not in approved_tickers:
                violations.append("security did not pass every entry gate on this date")
            if order.ticker in excluded_tickers:
                violations.append("security was sold, trimmed or frozen in this run")
            if cash_needed > spendable + _EPSILON:
                violations.append("would dip into the cash reserve or require borrowing")
            if order.notional > p.max_order_fraction_of_nav * nav + _EPSILON:
                violations.append("exceeds the maximum order size")
            if position_values.get(order.ticker, 0.0) + order.notional > p.max_position_weight * nav + _EPSILON:
                violations.append("would exceed the maximum position weight")
            if sector_values.get(order.sector, 0.0) + order.notional > p.max_sector_weight * nav + _EPSILON:
                violations.append("would exceed the maximum sector weight")
            liquidity = adv.get(order.ticker)
            if not is_finite_number(liquidity) or order.notional > c.max_participation_of_adv * liquidity + _EPSILON:
                violations.append("exceeds the liquidity cap")
            if order.notional < c.min_trade_value:
                violations.append("below the minimum trade size")
        approved = not violations
        if approved:
            spendable -= cash_needed
            position_values[order.ticker] = position_values.get(order.ticker, 0.0) + order.notional
            sector_values[order.sector] = sector_values.get(order.sector, 0.0) + order.notional
        decisions.append(OrderDecision(order, approved, tuple(violations)))
    return decisions
