"""Run the entry pipeline over a universe and rank the survivors."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .data.provider import DataProvider
from .gates import first_failing_stage
from .logic import EvaluationContext, Gate, GateResult
from .models import SecuritySnapshot
from .ranking import RankedCandidate, rank_candidates


@dataclass
class ScreenResult:
    ranked: list[RankedCandidate] = field(default_factory=list)
    results: dict[str, GateResult] = field(default_factory=dict)
    funnel: Counter = field(default_factory=Counter)   # stage that stopped each security; "eligible" passed
    unrankable: list[str] = field(default_factory=list)


def screen(
    provider: DataProvider,
    pipeline: Gate,
    ctx: EvaluationContext,
    tickers: Iterable[str],
    snapshots: Mapping[str, SecuritySnapshot | None] | None = None,
) -> ScreenResult:
    out = ScreenResult()
    snapshots = snapshots or {}
    eligible = []
    for ticker in tickers:
        snap = snapshots[ticker] if ticker in snapshots else provider.snapshot(ticker, ctx.as_of)
        if snap is None:
            out.funnel["no_data"] += 1
            continue
        result = pipeline.evaluate(snap, ctx)
        out.results[ticker] = result
        if result.passed:
            eligible.append(snap)
        else:
            out.funnel[first_failing_stage(result)] += 1
    out.ranked = rank_candidates(eligible)
    ranked = {c.ticker for c in out.ranked}
    out.unrankable = sorted(s.ticker for s in eligible if s.ticker not in ranked)
    out.funnel["eligible"] = len(out.ranked)
    return out
