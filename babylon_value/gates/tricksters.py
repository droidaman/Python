"""Law 5: "Gold flees the man who would force it to impossible earnings or
who followeth the alluring advice of tricksters and schemers." """

from __future__ import annotations

from ..logic import LeafGate, Verdict
from ..metrics import beneish_m_score, dividend_yield, free_cash_flow, market_cap, share_growth
from ..principles import Principle


class BeneishGate(LeafGate):
    name = "beneish_m_score"
    principle = Principle.LAW_5

    def check(self, snap, ctx):
        m, components = beneish_m_score(snap.history)
        maximum = ctx.config.tricksters.max_beneish_m
        passed = m <= maximum
        verdict = "no sign of" if passed else "likely"
        return Verdict(passed, f"M-score {m:.2f} ({verdict} earnings manipulation; threshold {maximum:.2f})",
                       {"m_score": m, **components})


class DilutionGate(LeafGate):
    """Management that keeps printing shares is spending the owners' gold."""

    name = "dilution"
    principle = Principle.LAW_5

    def check(self, snap, ctx):
        growth = share_growth(snap.history)
        maximum = ctx.config.tricksters.max_annual_share_growth
        return Verdict(growth <= maximum, f"share count changed {growth:+.1%} (maximum {maximum:+.1%})",
                       {"share_growth": growth})


class DividendSustainabilityGate(LeafGate):
    """A yield that is too good to be true usually is."""

    name = "dividend_sustainability"
    principle = Principle.LAW_5

    def check(self, snap, ctx):
        cfg = ctx.config.tricksters
        p = snap.latest
        if p.dividends_paid == 0:
            return Verdict(True, "no dividend to sustain")
        dy = dividend_yield(snap)
        fcf = free_cash_flow(p)
        metrics = {"dividend_yield": dy, "free_cash_flow": fcf}
        if dy > cfg.max_dividend_yield:
            return Verdict(False, f"dividend yield {dy:.1%} is implausibly high (max {cfg.max_dividend_yield:.0%})",
                           metrics)
        if fcf <= 0 or p.dividends_paid > cfg.max_dividend_to_fcf * fcf:
            return Verdict(False, "dividend is not covered by free cash flow", metrics)
        return Verdict(True, f"dividend yield {dy:.1%}, covered by free cash flow", metrics)


class PennyStockGate(LeafGate):
    name = "penny_stock"
    principle = Principle.LAW_5

    def check(self, snap, ctx):
        cfg = ctx.config.tricksters
        price, cap = snap.market.price, market_cap(snap)
        metrics = {"price": price, "market_cap": cap}
        if price < cfg.min_share_price:
            return Verdict(False, f"price {price:.2f} is below {cfg.min_share_price:.2f}", metrics)
        if cap < cfg.min_market_cap:
            return Verdict(False, f"market cap {cap:,.0f} is below {cfg.min_market_cap:,.0f}", metrics)
        return Verdict(True, f"price {price:.2f}, market cap {cap:,.0f}", metrics)
