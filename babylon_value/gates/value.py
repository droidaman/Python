"""Deep value: buy only with a margin of safety, and Law 2: put gold to work
only where it earns well above the safe alternative."""

from __future__ import annotations

from ..logic import AllOf, AnyOf, LeafGate, Verdict
from ..metrics import MetricUnavailable, enterprise_value, is_finite_number, market_cap, net_current_asset_value, \
    piotroski_f_score
from ..principles import Principle


class AcquirersMultipleGate(LeafGate):
    """EV/EBIT - what a buyer of the whole business pays for its operating
    earnings (Carlisle, "Deep Value"; Greenblatt's earnings yield)."""

    name = "acquirers_multiple"
    principle = Principle.MARGIN_OF_SAFETY

    def check(self, snap, ctx):
        ebit = snap.latest.ebit
        if ebit <= 0:
            return Verdict(False, "operating earnings are not positive, so the multiple is undefined")
        ev = enterprise_value(snap)
        if ev <= 0:
            return Verdict(False, "enterprise value is not positive; excluded until the data is verified by hand")
        multiple = ev / ebit
        maximum = ctx.config.value.max_ev_to_ebit
        return Verdict(multiple <= maximum, f"EV/EBIT {multiple:.1f} vs maximum {maximum:.1f}", {"ev_to_ebit": multiple})


class EarningsYieldHurdleGate(LeafGate):
    """Graham: a stock's earnings yield should be at least twice the yield on
    a safe bond, or the gold would be better employed in the bond."""

    name = "earnings_yield_hurdle"
    principle = Principle.LAW_2

    def check(self, snap, ctx):
        rf = ctx.risk_free_rate
        if not is_finite_number(rf) or not 0.0 <= rf < 1.0:
            raise MetricUnavailable(f"risk-free rate {rf!r} is invalid")
        ebit, ev = snap.latest.ebit, enterprise_value(snap)
        if ebit <= 0 or ev <= 0:
            return Verdict(False, "earnings yield is undefined")
        earnings_yield = ebit / ev
        hurdle = ctx.config.value.min_earnings_yield_vs_risk_free * rf
        return Verdict(earnings_yield >= hurdle, f"earnings yield {earnings_yield:.1%} vs hurdle {hurdle:.1%}",
                       {"earnings_yield": earnings_yield, "hurdle": hurdle})


class NetNetGate(LeafGate):
    """Graham's net-net: the market values the company below its current
    assets net of every liability. Only profitable net-nets qualify."""

    name = "net_net"
    principle = Principle.MARGIN_OF_SAFETY

    def check(self, snap, ctx):
        if snap.latest.ebit <= 0:
            return Verdict(False, "unprofitable net-nets are excluded")
        ncav = net_current_asset_value(snap.latest)
        if ncav <= 0:
            return Verdict(False, "net current asset value is not positive")
        price_to_ncav = market_cap(snap) / ncav
        maximum = ctx.config.value.max_market_cap_to_ncav
        return Verdict(price_to_ncav <= maximum, f"market cap / NCAV {price_to_ncav:.2f} vs maximum {maximum:.2f}",
                       {"market_cap_to_ncav": price_to_ncav})


class PiotroskiGate(LeafGate):
    """Cheap is not enough: cheap and deteriorating is a value trap. Law 3 -
    follow the advice of those wise in gold's handling (here, published
    evidence rather than tips)."""

    name = "piotroski"
    principle = Principle.LAW_3

    def check(self, snap, ctx):
        score, signals = piotroski_f_score(snap.history)
        minimum = ctx.config.value.min_piotroski_score
        missing = [name for name, ok in signals.items() if not ok]
        detail = f" (missing: {', '.join(missing)})" if missing else ""
        return Verdict(score >= minimum, f"F-score {score}/9 vs minimum {minimum}{detail}", {"f_score": score})


def margin_of_safety_gate() -> AnyOf:
    """A security is a bargain on earnings (cheap EV/EBIT that also clears the
    bond-yield hurdle) OR on assets (a profitable net-net)."""
    return AnyOf("margin_of_safety", Principle.MARGIN_OF_SAFETY, [
        AllOf("cheap_on_earnings", Principle.MARGIN_OF_SAFETY, [AcquirersMultipleGate(), EarningsYieldHurdleGate()]),
        NetNetGate(),
    ])
