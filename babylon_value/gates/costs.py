"""Cure 2: "Control thy expenditures." A bargain that costs too much to buy
or sell is not a bargain."""

from __future__ import annotations

from ..logic import LeafGate, Verdict
from ..principles import Principle


class TradabilityGate(LeafGate):
    name = "tradability"
    principle = Principle.CURE_2

    def check(self, snap, ctx):
        adv = snap.market.avg_daily_dollar_volume
        minimum = ctx.config.costs.min_avg_daily_dollar_volume
        return Verdict(adv >= minimum, f"average daily volume {adv:,.0f} vs minimum {minimum:,.0f}",
                       {"avg_daily_dollar_volume": adv})
