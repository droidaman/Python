"""Historical risk-free rate: the 3-month US Treasury bill (FRED series TB3MS).

TB3MS is a monthly average published early in the following month, so the
rate used on a decision date is the latest month that had been published by
then - never the month still in progress.
"""

from __future__ import annotations

import csv
import io
import math
from bisect import bisect_right
from datetime import date, timedelta
from typing import Iterable

FRED_TB3MS_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=TB3MS"
PUBLICATION_LAG = timedelta(days=35)


def parse_fred_csv(text: str) -> list[tuple[date, float]]:
    """FRED CSV (first column a date, second a percentage; '.' when missing)
    as (month start, rate as a decimal) pairs."""
    rows = list(csv.reader(io.StringIO(text)))
    out = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        try:
            day, value = date.fromisoformat(row[0].strip()), float(row[1])
        except ValueError:
            continue
        if math.isfinite(value):
            out.append((day, value / 100.0))
    return sorted(out)


class RiskFreeCurve:
    def __init__(self, observations: Iterable[tuple[date, float]]):
        self.observations = sorted(observations)
        self._dates = [d for d, _ in self.observations]

    def rate_at(self, as_of: date) -> float:
        """Latest monthly rate published by ``as_of``; NaN if none was (which
        makes the engine halt rather than guess)."""
        index = bisect_right(self._dates, as_of - PUBLICATION_LAG)
        return self.observations[index - 1][1] if index else math.nan

    def __bool__(self) -> bool:
        return bool(self.observations)
