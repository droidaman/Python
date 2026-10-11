"""One rebalance, end to end.

    pre-flight -> value the book -> exits (sells) -> re-read cash ->
    entry gates over the universe -> rank -> size buys -> order gates -> buys

Failsafe behaviour:

* The configuration is validated before the engine will start.
* Pre-flight checks halt the run (no orders at all) on a kill switch, an
  implausible risk-free rate, an empty or collapsing universe, or a holding
  that cannot be priced.
* Look-ahead bias anywhere halts the run.
* Execution is two-phase: buys are sized from the cash the broker reports
  *after* the sells, never from expected proceeds.
* If too many orders in a batch fail the pre-trade gates, the whole batch is
  withheld: the engine's sizing and the gates disagreeing means a defect.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Callable

from .config import StrategyConfig
from .costs import CostModel
from .data.provider import DataProvider
from .errors import HaltError
from .execution import Broker, Fill
from .exits import ExitAction, ExitDecision, evaluate_holding
from .gates import STAGE_ORDER, build_entry_pipeline, build_thesis_pipeline
from .journal import Journal, NullJournal
from .logic import EvaluationContext, LookAheadBiasError
from .metrics import is_finite_number
from .orders import Order, OrderDecision, Side, check_buys, check_sells, limit_price_for
from .portfolio import PortfolioState
from .principles import Principle
from .ranking import RankedCandidate
from .screening import screen


class RunStatus(Enum):
    COMPLETED = "COMPLETED"
    HALTED = "HALTED"


@dataclass
class RebalanceReport:
    as_of: date
    config_fingerprint: str
    risk_free_rate: float
    status: RunStatus = RunStatus.COMPLETED
    halt_reason: str = ""
    universe_size: int = 0
    nav_before: float = math.nan
    nav_after: float = math.nan
    funnel: dict[str, int] = field(default_factory=dict)
    eligible: list[RankedCandidate] = field(default_factory=list)
    rejections: dict[str, list[str]] = field(default_factory=dict)
    exits: list[ExitDecision] = field(default_factory=list)
    order_decisions: list[OrderDecision] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "event": "rebalance",
            "as_of": self.as_of.isoformat(),
            "config_fingerprint": self.config_fingerprint,
            "risk_free_rate": self.risk_free_rate,
            "status": self.status.value,
            "halt_reason": self.halt_reason,
            "universe_size": self.universe_size,
            "nav_before": _round(self.nav_before),
            "nav_after": _round(self.nav_after),
            "funnel": dict(self.funnel),
            "eligible": [c.to_dict() for c in self.eligible],
            "rejections": self.rejections,
            "exits": [d.to_dict() for d in self.exits],
            "orders": [d.to_dict() for d in self.order_decisions],
            "fills": [f.to_dict() for f in self.fills],
            "alerts": list(self.alerts),
        }

    def summary(self) -> str:
        funnel = ", ".join(f"{stage} {self.funnel[stage]}" for stage in (*STAGE_ORDER, "no_data")
                           if self.funnel.get(stage))
        actions: dict[str, int] = {}
        for decision in self.exits:
            actions[decision.action.value] = actions.get(decision.action.value, 0) + 1
        buys = sum(1 for f in self.fills if f.side is Side.BUY)
        sells = sum(1 for f in self.fills if f.side is Side.SELL)
        rejected = sum(1 for d in self.order_decisions if not d.approved)
        lines = [
            f"Rebalance {self.as_of} [{self.status.value}] config {self.config_fingerprint}",
            f"  NAV {self.nav_before:,.0f} -> {self.nav_after:,.0f}",
            f"  Universe {self.universe_size}; eligible {self.funnel.get('eligible', 0)}; "
            f"stopped at: {funnel or 'none'}",
            f"  Holdings reviewed: {', '.join(f'{k} {v}' for k, v in sorted(actions.items())) or 'none'}",
            f"  Fills: {buys} buy(s), {sells} sell(s); {rejected} order(s) rejected by pre-trade gates",
        ]
        if self.halt_reason:
            lines.append(f"  HALT: {self.halt_reason}")
        lines.extend(f"  ! {alert}" for alert in self.alerts if not alert.startswith("HALTED"))
        return "\n".join(lines)


def file_kill_switch(path: str) -> Callable[[], bool]:
    """A kill switch that is engaged whenever ``path`` exists."""
    return lambda: os.path.exists(path)


class Rebalancer:
    def __init__(
        self,
        config: StrategyConfig,
        provider: DataProvider,
        broker: Broker,
        journal: Journal | None = None,
        kill_switch: Callable[[], bool] | None = None,
    ):
        self.config = config.validate()
        self.provider = provider
        self.broker = broker
        self.journal = journal or NullJournal()
        self.kill_switch = kill_switch
        self.cost_model = CostModel(config.costs)
        self.entry_pipeline = build_entry_pipeline()
        self.thesis_pipeline = build_thesis_pipeline()
        self.fingerprint = config.fingerprint()
        self._previous_universe_size: int | None = None

    def run(self, as_of: date, risk_free_rate: float) -> RebalanceReport:
        report = RebalanceReport(as_of, self.fingerprint, risk_free_rate)
        try:
            self._run(EvaluationContext(as_of, self.config, risk_free_rate), report)
        except (HaltError, LookAheadBiasError) as exc:
            report.status = RunStatus.HALTED
            report.halt_reason = f"{type(exc).__name__}: {exc}"
            report.alerts.append(f"HALTED - {exc}")
        except Exception as exc:
            # A defect: stop, but leave an audit record of everything done so far.
            report.status = RunStatus.HALTED
            report.halt_reason = f"unexpected {type(exc).__name__}: {exc}"
            self.journal.record(report.to_dict())
            raise
        if math.isnan(report.nav_after):
            report.nav_after = report.nav_before
        self.journal.record(report.to_dict())
        return report

    # ------------------------------------------------------------------ phases

    def _run(self, ctx: EvaluationContext, report: RebalanceReport) -> None:
        self._preflight(ctx)
        universe = self._universe(ctx.as_of)
        report.universe_size = len(universe)

        state = self._portfolio_state(ctx.as_of)
        report.nav_before = state.nav
        if not state.nav > 0:
            raise HaltError("portfolio NAV is not positive")

        # Exits are judged before any new buying.
        snapshots = {t: self.provider.snapshot(t, ctx.as_of) for t in sorted(state.positions)}
        report.exits = [
            evaluate_holding(state.positions[t], snapshots[t], state, ctx, self.entry_pipeline, self.thesis_pipeline)
            for t in sorted(state.positions)
        ]
        for decision in report.exits:
            if decision.action is ExitAction.FREEZE:
                report.alerts.append(f"{decision.ticker} frozen for human review: {decision.reason}")
        adv = {t: s.market.avg_daily_dollar_volume for t, s in snapshots.items() if s is not None}
        sells = check_sells(self._sell_orders(report.exits, state, adv, report), state, adv, self.config)
        self._enforce_rejection_limit(sells, report)
        sell_costs = self._execute(sells, ctx.as_of, report)

        # Two-phase execution: size buys from the cash actually held now.
        state = self._portfolio_state(ctx.as_of)
        excluded = {d.ticker for d in report.exits if d.action is not ExitAction.HOLD}

        result = screen(self.provider, self.entry_pipeline, ctx, universe, snapshots)
        report.funnel = dict(result.funnel)
        report.eligible = result.ranked
        report.rejections = {t: [f"{leaf.gate}: {leaf.reason}" for leaf in r.failures()]
                             for t, r in result.results.items() if not r.passed}
        if result.unrankable:
            report.alerts.append(f"passed every gate but could not be ranked: {', '.join(result.unrankable)}")

        budget = self.config.costs.max_cost_per_rebalance * state.nav - sell_costs
        buy_orders = self._buy_orders(result.ranked, state, excluded, budget, report)
        approved = {c.ticker for c in result.ranked}
        adv = {c.ticker: c.snapshot.market.avg_daily_dollar_volume for c in result.ranked}
        buys = check_buys(buy_orders, state, adv, approved, excluded, self.config, self.cost_model)
        self._enforce_rejection_limit(buys, report)
        self._execute(buys, ctx.as_of, report)
        report.nav_after = self._portfolio_state(ctx.as_of).nav

    def _preflight(self, ctx: EvaluationContext) -> None:
        if self.kill_switch is not None:
            try:
                engaged = self.kill_switch()
            except Exception as exc:  # a kill switch that cannot be read counts as engaged
                raise HaltError(f"kill switch could not be read ({exc})") from exc
            if engaged:
                raise HaltError("kill switch engaged")
        rf = ctx.risk_free_rate
        if not is_finite_number(rf) or not 0.0 <= rf <= 0.20:
            raise HaltError(f"implausible risk-free rate {rf!r} (expected 0 to 0.20; use 0 when rates are negative)")
        self.broker.preflight(ctx.as_of)   # account state, open orders, ledger reconciliation

    def _universe(self, as_of: date) -> list[str]:
        universe = sorted(set(self.provider.universe(as_of)))
        if not universe:
            raise HaltError("the universe is empty - data feed failure?")
        previous = self._previous_universe_size
        if previous and len(universe) < previous * (1 - self.config.governance.max_universe_shrink):
            raise HaltError(f"universe shrank from {previous} to {len(universe)} securities - data feed failure?")
        self._previous_universe_size = len(universe)
        return universe

    def _portfolio_state(self, as_of: date) -> PortfolioState:
        cash = self.broker.cash()
        if not is_finite_number(cash) or cash < -1e-6:
            raise HaltError(f"broker reports invalid cash {cash!r}")
        positions = self.broker.positions()
        prices = {}
        for ticker, position in positions.items():
            if type(position.shares) is not int or position.shares <= 0:
                raise HaltError(f"broker reports an invalid position in {ticker}")
            bar = self.provider.price(ticker, as_of)
            if (bar is None or not is_finite_number(bar.close) or bar.close <= 0
                    or (as_of - bar.date).days > self.config.integrity.max_price_age_days):
                raise HaltError(f"no fresh price for holding {ticker}; the portfolio cannot be valued")
            prices[ticker] = bar.close
        return PortfolioState(cash, positions, prices)

    # ------------------------------------------------------------------ orders

    def _sell_orders(self, exits, state: PortfolioState, adv, report: RebalanceReport) -> list[Order]:
        p, c = self.config.portfolio, self.config.costs
        orders = []
        for decision in exits:
            if decision.action not in (ExitAction.SELL, ExitAction.TRIM):
                continue
            position = state.positions[decision.ticker]
            price = state.prices[decision.ticker]
            limit = limit_price_for(Side.SELL, price, p.limit_price_band)
            if decision.action is ExitAction.SELL:
                shares = position.shares
            else:
                excess = state.position_value(decision.ticker) - p.target_weight * state.nav
                shares = min(position.shares, math.ceil(excess / price))
            liquidity = adv.get(decision.ticker, math.nan)
            capacity = math.floor(c.max_participation_of_adv * liquidity / limit) if is_finite_number(liquidity) else 0
            if shares > capacity:
                report.alerts.append(f"{decision.ticker}: sale limited to {capacity} of {shares} shares by liquidity; "
                                     "the rest follows at the next rebalance")
                shares = capacity
            if shares <= 0 or (shares * limit < c.min_trade_value and shares != position.shares):
                continue
            orders.append(Order(decision.ticker, position.sector, Side.SELL, shares, limit, price,
                                decision.reason, decision.principle))
        return orders

    def _buy_orders(self, ranked, state: PortfolioState, excluded, budget: float,
                    report: RebalanceReport) -> list[Order]:
        p, c = self.config.portfolio, self.config.costs
        nav = state.nav
        target_value = p.target_weight * nav
        spendable = state.cash - p.cash_reserve * nav     # Cure 6: the reserve is never spent
        sector_values = state.sector_values()
        orders: list[Order] = []

        def plan(candidate: RankedCandidate, desired: float, reason: str, principle: Principle) -> bool:
            """Append a buy if one fits every limit; False once the cost budget is spent."""
            nonlocal spendable, budget
            limit = limit_price_for(Side.BUY, candidate.price, p.limit_price_band)
            room = min(
                desired,
                spendable,
                p.max_order_fraction_of_nav * nav,
                p.max_position_weight * nav - state.position_value(candidate.ticker),
                p.max_sector_weight * nav - sector_values.get(candidate.sector, 0.0),
                c.max_participation_of_adv * candidate.snapshot.market.avg_daily_dollar_volume,
            )
            shares = int(room // limit) if room > 0 else 0
            while shares > 0 and shares * limit + self.cost_model.commission(shares) > spendable:
                shares -= 1
            if shares <= 0 or shares * limit < c.min_trade_value:
                return True
            cost = self.cost_model.estimate(shares, limit)
            if cost > budget:
                report.alerts.append("rebalance cost budget reached; remaining buys wait for the next rebalance")
                return False
            orders.append(Order(candidate.ticker, candidate.sector, Side.BUY, shares, limit, candidate.price,
                                reason, principle))
            spendable -= shares * limit + self.cost_model.commission(shares)
            budget -= cost
            sector_values[candidate.sector] = sector_values.get(candidate.sector, 0.0) + shares * limit
            return True

        # New positions, best-ranked first, until every slot is filled.
        open_slots = p.target_positions - len(state.positions)
        for candidate in ranked:
            if open_slots <= 0:
                break
            if candidate.ticker in state.positions or candidate.ticker in excluded:
                continue
            placed = len(orders)
            if not plan(candidate, target_value, f"new position: rank {candidate.rank}, value score "
                                                 f"{candidate.score:.2f}", Principle.MARGIN_OF_SAFETY):
                return orders
            open_slots -= len(orders) - placed

        # Cure 3: put idle gold to work in holdings that would still be bought today.
        for candidate in ranked:
            if candidate.ticker not in state.positions or candidate.ticker in excluded:
                continue
            gap = target_value - state.position_value(candidate.ticker)
            if gap >= c.min_trade_value and not plan(
                    candidate, gap, "top up to target weight: still passes every entry gate", Principle.CURE_3):
                break
        return orders

    def _enforce_rejection_limit(self, decisions: list[OrderDecision], report: RebalanceReport) -> None:
        report.order_decisions.extend(decisions)
        if not decisions:
            return
        rejected = sum(1 for d in decisions if not d.approved)
        if rejected / len(decisions) > self.config.governance.max_order_rejection_ratio:
            raise HaltError(f"{rejected} of {len(decisions)} orders failed the pre-trade gates; batch withheld")

    def _execute(self, decisions: list[OrderDecision], as_of: date, report: RebalanceReport) -> float:
        costs = 0.0
        for decision in decisions:
            order = decision.order
            if not decision.approved:
                report.alerts.append(f"{order.side.value} {order.ticker} rejected by pre-trade gates: "
                                     + "; ".join(decision.violations))
                continue
            fill = self.broker.submit(order, as_of)
            if fill is None:
                report.alerts.append(f"broker did not fill {order.side.value} {order.shares} {order.ticker}")
                continue
            report.fills.append(fill)
            costs += fill.cost
        return costs


def _round(value: float) -> float | None:
    return round(value, 2) if is_finite_number(value) else None
