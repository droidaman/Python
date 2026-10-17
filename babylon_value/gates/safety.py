"""Cure 4: "Guard thy treasures from loss" and Law 3: gold clings to the
cautious owner. Mathon the gold lender: "Better a little caution than a
great regret." Also Cure 5: own productive, tangible property."""

from __future__ import annotations

from ..logic import LeafGate, Verdict
from ..metrics import altman_z, current_ratio, ratio, tangible_equity
from ..principles import Principle


class AltmanZGate(LeafGate):
    name = "altman_z"
    principle = Principle.CURE_4

    def check(self, snap, ctx):
        z, components = altman_z(snap)
        minimum = ctx.config.safety.min_altman_z
        passed = z >= minimum
        verdict = "clear of" if passed else "inside"
        return Verdict(passed, f"Altman Z {z:.2f} is {verdict} the distress zone (< {minimum:.2f})",
                       {"altman_z": z, **components})


class LiquidityGate(LeafGate):
    name = "liquidity"
    principle = Principle.CURE_4

    def check(self, snap, ctx):
        cr = current_ratio(snap.latest)
        minimum = ctx.config.safety.min_current_ratio
        return Verdict(cr >= minimum, f"current ratio {cr:.2f} vs minimum {minimum:.2f}", {"current_ratio": cr})


class LeverageGate(LeafGate):
    """Debt is how sound businesses die in bad years (Dabasir the camel trader
    learned the same lesson about debt the hard way)."""

    name = "leverage"
    principle = Principle.CURE_4

    def check(self, snap, ctx):
        cfg = ctx.config.safety
        p = snap.latest
        if p.shareholders_equity <= 0:
            return Verdict(False, "book equity is not positive")
        debt_to_equity = ratio(p.total_debt, p.shareholders_equity, "debt / equity")
        metrics = {"debt_to_equity": debt_to_equity}
        if debt_to_equity > cfg.max_debt_to_equity:
            return Verdict(False, f"debt/equity {debt_to_equity:.2f} exceeds {cfg.max_debt_to_equity:.2f}", metrics)
        if p.interest_expense > 0:
            coverage = ratio(p.ebit, p.interest_expense, "interest coverage")
            metrics["interest_coverage"] = coverage
            if coverage < cfg.min_interest_coverage:
                return Verdict(False, f"EBIT covers interest only {coverage:.1f}x (minimum "
                                      f"{cfg.min_interest_coverage:.1f}x)", metrics)
        return Verdict(True, f"debt/equity {debt_to_equity:.2f}, interest well covered", metrics)


class EarningsStabilityGate(LeafGate):
    name = "earnings_stability"
    principle = Principle.CURE_4

    def check(self, snap, ctx):
        cfg = ctx.config.safety
        window = snap.history[: cfg.earnings_lookback_years]
        if len(window) < cfg.earnings_lookback_years:
            return Verdict(False, f"only {len(window)} year(s) of earnings to judge")
        profitable = sum(1 for p in window if p.ebit > 0)
        metrics = {"profitable_years": profitable}
        if snap.latest.ebit <= 0:
            return Verdict(False, "latest year's operating earnings are not positive", metrics)
        passed = profitable >= cfg.min_profitable_years
        return Verdict(passed, f"operating profit in {profitable} of the last {len(window)} years "
                               f"(minimum {cfg.min_profitable_years})", metrics)


class TangibleBackingGate(LeafGate):
    """Cure 5 - "make of thy dwelling a profitable investment" - read as: own
    a claim on real, productive property, not only on accounting goodwill."""

    name = "tangible_backing"
    principle = Principle.CURE_5

    def check(self, snap, ctx):
        value = tangible_equity(snap.latest)
        return Verdict(value > 0, f"tangible equity {value:,.0f}", {"tangible_equity": value})
