"""Sell discipline for securities already owned.

Order of precedence:

1. FREEZE - data is missing or untrustworthy: never trade blind; alert a human.
2. SELL   - the investment thesis is broken (a safety, competence or
            trickster gate now fails). Safety comes before cost.
3. SELL   - fair value reached (exit thresholds are looser than entry
            thresholds, so positions are not churned around a boundary).
4. SELL   - Graham's rule: a 50% gain, or two years without the value being
            realised - unless the security would still be bought today.
5. TRIM   - the position has grown beyond the maximum weight.
6. HOLD   - otherwise.

There is deliberately no price stop-loss. A deep value strategy buys what
others are selling; a stop-loss would sell the strategy's own bargains at the
worst moment. Risk is controlled by the gates, diversification and caps.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .config import StrategyConfig
from .logic import EvaluationContext, Gate, GateResult, Outcome
from .metrics import MetricUnavailable, enterprise_value, market_cap, net_current_asset_value
from .models import SecuritySnapshot
from .portfolio import PortfolioState, Position
from .principles import Principle


class ExitAction(Enum):
    HOLD = "HOLD"
    SELL = "SELL"
    TRIM = "TRIM"
    FREEZE = "FREEZE"


@dataclass(frozen=True)
class ExitDecision:
    ticker: str
    action: ExitAction
    reason: str
    principle: Principle

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "action": self.action.value, "reason": self.reason,
                "principle": self.principle.value}


def evaluate_holding(
    position: Position,
    snap: SecuritySnapshot | None,
    state: PortfolioState,
    ctx: EvaluationContext,
    entry_pipeline: Gate,
    thesis_pipeline: Gate,
) -> ExitDecision:
    cfg = ctx.config
    ticker = position.ticker

    def decide(action: ExitAction, reason: str, principle: Principle) -> ExitDecision:
        return ExitDecision(ticker, action, reason, principle)

    if snap is None:
        return decide(ExitAction.FREEZE, "no data available; never trade blind - needs human review",
                      Principle.INTEGRITY)

    thesis = thesis_pipeline.evaluate(snap, ctx)
    integrity = thesis.children[0] if thesis.children else thesis
    if not integrity.passed:
        return decide(ExitAction.FREEZE, f"data integrity: {_reasons(integrity)}", Principle.INTEGRITY)
    if thesis.outcome is Outcome.ERROR:
        return decide(ExitAction.FREEZE, f"thesis cannot be determined: {_reasons(thesis)}", Principle.INTEGRITY)
    if thesis.outcome is Outcome.FAIL:
        return decide(ExitAction.SELL, f"thesis broken: {_reasons(thesis)}", Principle.CURE_4)

    try:
        cheap, valuation = still_cheap(snap, cfg)
    except MetricUnavailable as exc:
        return decide(ExitAction.FREEZE, f"valuation cannot be determined: {exc}", Principle.INTEGRITY)
    if not cheap:
        return decide(ExitAction.SELL, f"fair value reached ({valuation})", Principle.MARGIN_OF_SAFETY)

    still_qualifies = entry_pipeline.evaluate(snap, ctx).passed
    value = state.position_value(ticker)
    gain = value / position.cost_basis - 1 if position.cost_basis > 0 else 0.0
    if gain >= cfg.exits.graham_profit_target and not still_qualifies:
        return decide(ExitAction.SELL, f"Graham profit target: up {gain:.0%} and no longer passes every entry gate",
                      Principle.MARGIN_OF_SAFETY)
    held_days = (ctx.as_of - position.opened_on).days
    if held_days >= cfg.exits.max_holding_days and not still_qualifies:
        return decide(ExitAction.SELL, f"held {held_days} days without the value being realised, and no longer "
                                       "passes every entry gate", Principle.MARGIN_OF_SAFETY)

    weight = value / state.nav
    if weight > cfg.portfolio.max_position_weight:
        return decide(ExitAction.TRIM, f"weight {weight:.1%} exceeds the {cfg.portfolio.max_position_weight:.0%} cap",
                      Principle.CURE_4)
    return decide(ExitAction.HOLD, f"thesis intact and still below fair value ({valuation})", Principle.CURE_3)


def still_cheap(snap: SecuritySnapshot, cfg: StrategyConfig) -> tuple[bool, str]:
    """Below fair value on earnings or on assets, judged on the exit thresholds."""
    p = snap.latest
    cheap = False
    parts = []
    if p.ebit > 0:
        ev = enterprise_value(snap)
        if ev <= 0:
            cheap = True
            parts.append("enterprise value below zero")
        else:
            multiple = ev / p.ebit
            cheap = cheap or multiple < cfg.exits.exit_ev_to_ebit
            parts.append(f"EV/EBIT {multiple:.1f}, exit at {cfg.exits.exit_ev_to_ebit:.1f}")
    else:
        parts.append("operating earnings not positive")
    ncav = net_current_asset_value(p)
    if ncav > 0:
        price_to_ncav = market_cap(snap) / ncav
        cheap = cheap or price_to_ncav < cfg.exits.exit_market_cap_to_ncav
        parts.append(f"market cap/NCAV {price_to_ncav:.2f}")
    return cheap, "; ".join(parts)


def _reasons(result: GateResult, limit: int = 3) -> str:
    failures = result.failures()
    text = "; ".join(f"{leaf.gate}: {leaf.reason}" for leaf in failures[:limit])
    if len(failures) > limit:
        text += f" (+{len(failures) - limit} more)"
    return text
