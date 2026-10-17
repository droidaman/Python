"""Ranking the securities that passed every gate.

The score is the plain average of four value factors' percentile ranks, each
weighted equally so no subjective weighting enters. Ties are broken by the
Piotroski score and then alphabetically, so the order is fully deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping

from .metrics import MetricUnavailable, enterprise_value, free_cash_flow, market_cap, net_current_asset_value, \
    piotroski_f_score, tangible_equity
from .models import SecuritySnapshot

FACTORS = ("earnings_yield", "ncav_yield", "tangible_book_yield", "fcf_yield")


@dataclass(frozen=True)
class RankedCandidate:
    snapshot: SecuritySnapshot
    rank: int
    score: float
    piotroski: int
    factors: Mapping[str, float]

    @property
    def ticker(self) -> str:
        return self.snapshot.ticker

    @property
    def sector(self) -> str:
        return self.snapshot.profile.sector

    @property
    def price(self) -> float:
        return self.snapshot.market.price

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "rank": self.rank,
            "score": round(self.score, 6),
            "piotroski": self.piotroski,
            "factors": {k: (round(v, 6) if math.isfinite(v) else str(v)) for k, v in self.factors.items()},
        }


def value_factors(snap: SecuritySnapshot) -> dict[str, float]:
    """Higher is cheaper for every factor."""
    p = snap.latest
    cap = market_cap(snap)
    if cap <= 0 or p.ebit <= 0:
        raise MetricUnavailable("market cap and operating earnings must be positive to rank")
    ev = enterprise_value(snap)
    return {
        # A business whose cash exceeds its market value plus debt is cheaper
        # than any positive multiple; it ranks at the top of this factor.
        "earnings_yield": p.ebit / ev if ev > 0 else math.inf,
        "ncav_yield": net_current_asset_value(p) / cap,
        "tangible_book_yield": tangible_equity(p) / cap,
        "fcf_yield": free_cash_flow(p) / cap,
    }


def rank_candidates(snapshots: Iterable[SecuritySnapshot]) -> list[RankedCandidate]:
    """Rank eligible securities, best first. Any security that cannot be
    ranked is dropped: unrankable means unbuyable."""
    rows = []
    for snap in snapshots:
        try:
            factors = value_factors(snap)
            f_score, _ = piotroski_f_score(snap.history)
        except MetricUnavailable:
            continue
        if any(math.isnan(v) for v in factors.values()):
            continue
        rows.append((snap, factors, f_score))
    if not rows:
        return []
    percentiles = {name: _percentiles([factors[name] for _, factors, _ in rows]) for name in FACTORS}
    scored = []
    for i, (snap, factors, f_score) in enumerate(rows):
        score = sum(percentiles[name][i] for name in FACTORS) / len(FACTORS)
        scored.append((score, f_score, snap, factors))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2].ticker))
    return [
        RankedCandidate(snapshot=snap, rank=i + 1, score=score, piotroski=f_score, factors=factors)
        for i, (score, f_score, snap, factors) in enumerate(scored)
    ]


def _percentiles(values: list[float]) -> list[float]:
    """Percentile rank in [0, 1], ties sharing their average rank."""
    n = len(values)
    if n == 1:
        return [1.0]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    start = 0
    while start < n:
        end = start
        while end + 1 < n and values[order[end + 1]] == values[order[start]]:
            end += 1
        shared = (start + end) / 2 / (n - 1)
        for k in range(start, end + 1):
            ranks[order[k]] = shared
        start = end + 1
    return ranks
