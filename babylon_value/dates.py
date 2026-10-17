from __future__ import annotations

import calendar
from datetime import date


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def month_ends(start: date, end: date) -> list[date]:
    """Every calendar month-end in [start, end]."""
    out = []
    year, month = start.year, start.month
    while True:
        current = month_end(year, month)
        if current > end:
            return out
        if current >= start:
            out.append(current)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
