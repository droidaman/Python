"""Point-in-time data access.

A provider must only ever reveal what was public on the decision date:

* fundamentals are filtered by ``available_date`` (the filing date), and when
  a report is restated the restatement replaces the original only from the
  day it was published;
* prices are the last observation on or before the decision date;
* the universe includes every security listed on that date, *including*
  those that later went bankrupt or were delisted (no survivorship bias).
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Protocol, Sequence

from ..models import FinancialPeriod, MarketData, SecurityProfile, SecuritySnapshot


@dataclass(frozen=True)
class PriceBar:
    date: date
    close: float
    volume: float           # shares traded per day
    dividend: float = 0.0   # cash dividend per share going ex on this date


class DataProvider(Protocol):
    def universe(self, as_of: date) -> list[str]: ...

    def snapshot(self, ticker: str, as_of: date) -> SecuritySnapshot | None: ...

    def price(self, ticker: str, as_of: date) -> PriceBar | None: ...

    def dividends(self, ticker: str, start: date, end: date) -> float: ...


class InMemoryProvider:
    def __init__(
        self,
        profiles: Mapping[str, SecurityProfile],
        fundamentals: Mapping[str, Sequence[FinancialPeriod]],
        prices: Mapping[str, Sequence[PriceBar]],
        adv_window_days: int = 90,
        listing_grace_days: int = 7,
    ):
        self.profiles = dict(profiles)
        self.fundamentals = {t: sorted(periods, key=lambda p: p.available_date) for t, periods in fundamentals.items()}
        self.bars = {t: sorted(bars, key=lambda b: b.date) for t, bars in prices.items()}
        self._dates = {t: [b.date for b in bars] for t, bars in self.bars.items()}
        self._adv_window = timedelta(days=adv_window_days)
        self._grace = listing_grace_days

    def universe(self, as_of: date) -> list[str]:
        """Securities with a recent price on ``as_of`` - listed on that date."""
        listed = []
        for ticker in sorted(self.profiles):
            bar = self.price(ticker, as_of)
            if bar is not None and (as_of - bar.date).days <= self._grace:
                listed.append(ticker)
        return listed

    def price(self, ticker: str, as_of: date) -> PriceBar | None:
        dates = self._dates.get(ticker)
        if not dates:
            return None
        index = bisect_right(dates, as_of)
        return self.bars[ticker][index - 1] if index else None

    def dividends(self, ticker: str, start: date, end: date) -> float:
        """Dividends per share going ex in (start, end]."""
        dates = self._dates.get(ticker)
        if not dates:
            return 0.0
        bars = self.bars[ticker][bisect_right(dates, start):bisect_right(dates, end)]
        return sum(b.dividend for b in bars if math.isfinite(b.dividend))

    def history(self, ticker: str, as_of: date) -> tuple[FinancialPeriod, ...]:
        latest_version: dict[date, FinancialPeriod] = {}
        for period in self.fundamentals.get(ticker, ()):
            if period.available_date > as_of:
                break
            latest_version[period.fiscal_year_end] = period
        return tuple(sorted(latest_version.values(), key=lambda p: p.fiscal_year_end, reverse=True))

    def snapshot(self, ticker: str, as_of: date) -> SecuritySnapshot | None:
        profile = self.profiles.get(ticker)
        bar = self.price(ticker, as_of)
        if profile is None or bar is None:
            return None
        market = MarketData(as_of=bar.date, price=bar.close, avg_daily_dollar_volume=self._adv(ticker, as_of))
        return SecuritySnapshot(profile=profile, market=market, history=self.history(ticker, as_of))

    def _adv(self, ticker: str, as_of: date) -> float:
        dates = self._dates[ticker]
        window = self.bars[ticker][bisect_left(dates, as_of - self._adv_window):bisect_right(dates, as_of)]
        if not window:
            return math.nan
        return sum(b.close * b.volume for b in window) / len(window)
