"""Point-in-time data access.

A provider must only ever reveal what was public on the decision date:

* fundamentals are filtered by ``available_date`` (the filing date), and when
  a report is restated the restatement replaces the original only from the
  day it was published;
* prices are the last observation on or before the decision date;
* the universe includes every security listed on that date, *including*
  those that later went bankrupt or were delisted (no survivorship bias).

Share counts and prices must be on the same basis. Prices here are raw
(unadjusted), so a reported share count is scaled by every stock split
between its date and the decision date. Under US GAAP a split that happens
before a report is filed is already reflected in that report, so annual
reports are scaled for splits after their filing date.
"""

from __future__ import annotations

import dataclasses
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Mapping, Protocol, Sequence

from ..models import FinancialPeriod, MarketData, SecurityProfile, SecuritySnapshot


@dataclass(frozen=True)
class PriceBar:
    date: date
    close: float
    volume: float           # shares traded per day
    dividend: float = 0.0   # cash dividend per share going ex on this date


@dataclass(frozen=True)
class Split:
    ex_date: date
    ratio: float            # new shares per old share: 2.0 for 2-for-1, 0.1 for 1-for-10


@dataclass(frozen=True)
class ShareCount:
    as_of: date             # date the count refers to (e.g. a 10-Q cover date)
    available_date: date    # date it was filed
    shares: float


@dataclass(frozen=True)
class QualityFlag:
    start: date
    end: date | None        # None: still open
    reason: str


class DataProvider(Protocol):
    def universe(self, as_of: date) -> list[str]: ...

    def snapshot(self, ticker: str, as_of: date) -> SecuritySnapshot | None: ...

    def price(self, ticker: str, as_of: date) -> PriceBar | None: ...

    def dividends(self, ticker: str, start: date, end: date) -> float: ...

    def splits(self, ticker: str, start: date, end: date) -> list[Split]: ...


def point_in_time_history(periods: Iterable[FinancialPeriod], as_of: date) -> tuple[FinancialPeriod, ...]:
    """The latest published version of each fiscal year, most recent year first."""
    latest_version: dict[date, FinancialPeriod] = {}
    for period in sorted(periods, key=lambda p: p.available_date):
        if period.available_date > as_of:
            break
        latest_version[period.fiscal_year_end] = period
    return tuple(sorted(latest_version.values(), key=lambda p: p.fiscal_year_end, reverse=True))


class InMemoryProvider:
    def __init__(
        self,
        profiles: Mapping[str, SecurityProfile],
        fundamentals: Mapping[str, Sequence[FinancialPeriod]],
        prices: Mapping[str, Sequence[PriceBar]],
        splits: Mapping[str, Sequence[Split]] | None = None,
        share_counts: Mapping[str, Sequence[ShareCount]] | None = None,
        quality_flags: Mapping[str, Sequence[QualityFlag]] | None = None,
        metadata: Mapping | None = None,
        adv_window_days: int = 90,
        listing_grace_days: int = 7,
    ):
        self.profiles = dict(profiles)
        self.fundamentals = {t: sorted(periods, key=lambda p: p.available_date) for t, periods in fundamentals.items()}
        self.bars = {t: sorted(bars, key=lambda b: b.date) for t, bars in prices.items()}
        self.split_events = {t: sorted(s, key=lambda x: x.ex_date) for t, s in (splits or {}).items()}
        self.share_counts = {t: sorted(c, key=lambda x: (x.as_of, x.available_date)) for t, c in (share_counts or {}).items()}
        self.quality_flags = {t: list(flags) for t, flags in (quality_flags or {}).items()}
        self.metadata = dict(metadata or {})
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

    def splits(self, ticker: str, start: date, end: date) -> list[Split]:
        """Splits going ex in (start, end]."""
        return [s for s in self.split_events.get(ticker, ()) if start < s.ex_date <= end]

    def split_factor(self, ticker: str, after: date, up_to: date) -> float:
        return math.prod(s.ratio for s in self.splits(ticker, after, up_to))

    def history(self, ticker: str, as_of: date) -> tuple[FinancialPeriod, ...]:
        history = point_in_time_history(self.fundamentals.get(ticker, ()), as_of)
        if ticker not in self.split_events:
            return history
        adjusted = []
        for period in history:
            factor = self.split_factor(ticker, period.available_date, as_of)
            if factor != 1.0:
                period = dataclasses.replace(period, shares_outstanding=period.shares_outstanding * factor)
            adjusted.append(period)
        return tuple(adjusted)

    def current_shares(self, ticker: str, as_of: date, history: Sequence[FinancialPeriod] = ()) -> float | None:
        """Most recent reported share count public on ``as_of``, if it is newer
        than the latest annual report; scaled for later splits."""
        counts = [c for c in self.share_counts.get(ticker, ()) if c.available_date <= as_of]
        if not counts:
            return None
        latest = counts[-1]
        if history and latest.as_of < history[0].fiscal_year_end:
            return None
        return latest.shares * self.split_factor(ticker, latest.as_of, as_of)

    def active_flags(self, ticker: str, as_of: date) -> tuple[str, ...]:
        return tuple(f.reason for f in self.quality_flags.get(ticker, ())
                     if f.start <= as_of and (f.end is None or as_of <= f.end))

    def snapshot(self, ticker: str, as_of: date) -> SecuritySnapshot | None:
        profile = self.profiles.get(ticker)
        bar = self.price(ticker, as_of)
        if profile is None or bar is None:
            return None
        history = self.history(ticker, as_of)
        market = MarketData(
            as_of=bar.date,
            price=bar.close,
            avg_daily_dollar_volume=self._adv(ticker, as_of),
            shares_outstanding=self.current_shares(ticker, as_of, history),
        )
        return SecuritySnapshot(profile=profile, market=market, history=history,
                                quality_flags=self.active_flags(ticker, as_of))

    def _adv(self, ticker: str, as_of: date) -> float:
        dates = self._dates[ticker]
        window = self.bars[ticker][bisect_left(dates, as_of - self._adv_window):bisect_right(dates, as_of)]
        if not window:
            return math.nan
        return sum(b.close * b.volume for b in window) / len(window)
