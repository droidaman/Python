"""Build the dataset for a live (paper) trading run: SEC EDGAR fundamentals
plus Interactive Brokers daily prices.

IB's historical prices are adjusted for stock splits up to today, while SEC
share counts are as reported in each filing. Today's price and the latest
share count are on the same basis unless the company split its stock after
that filing - and then the market cap is off by the split ratio. A 3-for-1
split would make a company look three times cheaper than it is, so three
checks raise data-quality flags, which block buying and freeze holdings:

1. Public float: the market value of the shares at the 10-K's public-float
   date (IB price x latest share count) cannot be meaningfully below the
   public float the company itself reported, since the float is only part of
   all shares. If it is, the share count is probably from before a split.
2. Price revisions: when the dataset is refreshed, IB's prices for dates
   already stored should not change. If they do, IB re-adjusted them for a new
   split; the flag stays until the company files a newer share count.
3. Unexplained jumps: a day-over-day move of 3x or more suggests an
   unadjusted split or bad data.

Prices adjusted this way are only valid at the build date, so the dataset is
marked "live" and the backtester refuses it.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Collection, Iterable

from ..config import StrategyConfig
from ..gates import build_fundamental_prefilter, first_failing_stage, profile_stage
from ..logic import EvaluationContext
from ..models import MarketData, SecuritySnapshot
from .csv_io import export_csv_directory
from .edgar import PLAIN_TICKER, PublicFloat, SecClient, TickerRecord, normalize_exchange, parse_annual_reports, \
    parse_public_float, parse_share_counts, profile_from_submissions
from .provider import InMemoryProvider, PriceBar, QualityFlag, point_in_time_history

FLOAT_TOLERANCE = 0.80          # computed value may sit 20% below the float (buybacks, timing)
REVISION_TOLERANCE = 0.02
JUMP_RATIO = 3.0
REVISION_FLAG = "IB prices for past dates changed since the previous build (probably a stock split)"


@dataclass
class BuildSummary:
    as_of: date
    counts: Counter = field(default_factory=Counter)
    skipped: dict[str, str] = field(default_factory=dict)
    flagged: dict[str, list[str]] = field(default_factory=dict)

    def text(self) -> str:
        lines = [f"Dataset built for {self.as_of}"]
        lines += [f"  {key:<34} {value:>6}" for key, value in sorted(self.counts.items())]
        for ticker, reasons in sorted(self.flagged.items()):
            lines.append(f"  FLAG {ticker}: {'; '.join(reasons)}")
        return "\n".join(lines)


def select_universe(records: Iterable[TickerRecord], config: StrategyConfig, tickers: Collection[str] | None = None,
                    always_include: Collection[str] = ()) -> list[TickerRecord]:
    """One plain common-stock ticker per company (CIK) on an allowed exchange.
    Securities the strategy already holds are always kept."""
    allowed = config.competence.allowed_exchanges
    by_cik: dict[int, list[TickerRecord]] = {}
    for record in records:
        keep = record.ticker in always_include or (
            normalize_exchange(record.exchange) in allowed and PLAIN_TICKER.match(record.ticker)
            and (tickers is None or record.ticker in tickers))
        if keep:
            by_cik.setdefault(record.cik, []).append(record)
    chosen = []
    for options in by_cik.values():
        held = [r for r in options if r.ticker in always_include]
        chosen.append(min(held or options, key=lambda r: r.ticker))
    return sorted(chosen, key=lambda r: r.ticker)


def build_live_dataset(
    sec: SecClient,
    bars_source,
    out_dir: str | Path,
    as_of: date,
    config: StrategyConfig,
    tickers: Collection[str] | None = None,
    always_include: Collection[str] = (),
    limit: int | None = None,
    previous: InMemoryProvider | None = None,
    bar_duration: str = "2 Y",
    pause_between_price_requests: float = 1.0,
    pause_before_retry: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda message: None,
) -> BuildSummary:
    config.validate()
    summary = BuildSummary(as_of)
    records = select_universe(sec.company_tickers(), config, tickers, always_include)
    if limit is not None:
        records = [r for r in records if r.ticker in always_include] + \
                  [r for r in records if r.ticker not in always_include][:limit]
    summary.counts["companies considered"] = len(records)

    ctx = EvaluationContext(as_of, config, 0.0)
    profile_check, prefilter = profile_stage(), build_fundamental_prefilter()
    profiles, fundamentals, share_counts, floats = {}, {}, {}, {}
    for index, record in enumerate(records, start=1):
        if index % 250 == 0:
            log(f"  SEC filings: {index}/{len(records)}")
        held = record.ticker in always_include
        submissions = sec.submissions(record.cik)
        if submissions is None:
            _skip(summary, record.ticker, "no SEC submissions")
            continue
        profile = profile_from_submissions(record.ticker, record.exchange, submissions)
        placeholder = MarketData(as_of, math.nan, math.nan)
        if not held:
            result = profile_check.evaluate(SecuritySnapshot(profile, placeholder, ()), ctx)
            if not result.passed:
                _skip(summary, record.ticker, "outside the circle of competence: " + result.failures()[0].reason)
                continue
        facts = sec.company_facts(record.cik)
        if facts is None:
            _skip(summary, record.ticker, "no XBRL financial data")
            continue
        periods = parse_annual_reports(facts)
        if not held:
            snap = SecuritySnapshot(profile, placeholder, point_in_time_history(periods, as_of))
            result = prefilter.evaluate(snap, ctx)
            if not result.passed:
                _skip(summary, record.ticker, f"fails {first_failing_stage(result)} before prices are needed")
                continue
        profiles[record.ticker] = profile
        fundamentals[record.ticker] = periods
        share_counts[record.ticker] = parse_share_counts(facts)
        floats[record.ticker] = parse_public_float(facts)
    summary.counts["passed checks that need no prices"] = len(profiles)

    bars: dict[str, list[PriceBar]] = {}
    for index, ticker in enumerate(sorted(profiles), start=1):
        if index % 50 == 0:
            log(f"  IB prices: {index}/{len(profiles)}")
        raw, error = _fetch_bars(bars_source, ticker, bar_duration)
        if not raw:   # IB throttles bursts of history requests: wait and try once more
            sleep(pause_before_retry)
            raw, error = _fetch_bars(bars_source, ticker, bar_duration)
        if not raw:
            _skip(summary, ticker, f"no IB price history ({error or 'empty'})")
        series = [PriceBar(day, close, volume) for day, close, volume in raw
                  if is_positive(close) and is_positive(volume, allow_zero=True)]
        if series:
            bars[ticker] = series
        sleep(pause_between_price_requests)
    summary.counts["with IB prices"] = len(bars)

    provider = InMemoryProvider(profiles, fundamentals, bars, share_counts=share_counts,
                                metadata={"purpose": "live", "price_basis": "split_adjusted",
                                          "built": as_of.isoformat(), "config_fingerprint": config.fingerprint(),
                                          "sources": {"fundamentals": "SEC EDGAR XBRL company facts",
                                                      "prices": "Interactive Brokers daily TRADES bars"}})
    flags = {}
    for ticker in sorted(bars):
        found = quality_flags(provider, ticker, as_of, floats.get(ticker, []), previous)
        if found:
            flags[ticker] = found
        active = [f.reason for f in found if f.end is None or f.end >= as_of]
        if active:
            summary.flagged[ticker] = active
    provider.quality_flags = flags
    summary.counts["flagged for data quality"] = sum(1 for reasons in summary.flagged.values() if reasons)
    export_csv_directory(provider, out_dir)
    return summary


def quality_flags(provider: InMemoryProvider, ticker: str, as_of: date, floats: list[PublicFloat],
                  previous: InMemoryProvider | None) -> list[QualityFlag]:
    flags: list[QualityFlag] = []
    series = provider.bars.get(ticker, [])

    for before, after in zip(series, series[1:]):
        change = after.close / before.close
        if change >= JUMP_RATIO or change <= 1 / JUMP_RATIO:
            flags.append(QualityFlag(after.date, None, f"price moved {change:.2f}x on {after.date} without an "
                                                       "explanation (unadjusted split or bad data?)"))
            break

    public = [f for f in floats if f.available_date <= as_of]
    if public:
        latest = public[-1]
        bar = provider.price(ticker, latest.as_of)
        history = provider.history(ticker, as_of)
        shares = provider.current_shares(ticker, as_of, history) or (history[0].shares_outstanding if history else None)
        if bar is not None and (latest.as_of - bar.date).days <= 7 and shares and math.isfinite(shares):
            implied = bar.close * shares
            if implied < FLOAT_TOLERANCE * latest.value:
                flags.append(QualityFlag(as_of, None,
                                         f"price x share count on {latest.as_of} ({implied:,.0f}) is below the reported "
                                         f"public float ({latest.value:,.0f}): the share count probably predates a split"))

    if previous is not None:
        newest_count = max((c.available_date for c in provider.share_counts.get(ticker, [])), default=None)
        for flag in previous.quality_flags.get(ticker, []):
            if flag.end is None and flag.reason.startswith(REVISION_FLAG):
                resolved = newest_count is not None and newest_count > flag.start
                flags.append(QualityFlag(flag.start, newest_count if resolved else None, flag.reason))
        old = {b.date: b.close for b in previous.bars.get(ticker, [])}
        revised = [b for b in series if b.date in old and abs(b.close / old[b.date] - 1) > REVISION_TOLERANCE]
        if revised:
            ratio = old[revised[0].date] / revised[0].close
            flags.append(QualityFlag(as_of, None, f"{REVISION_FLAG}: {revised[0].date} was {old[revised[0].date]:.2f}, "
                                                  f"now {revised[0].close:.2f} ({ratio:.2f}x); verify the share count"))
    return flags


def _fetch_bars(source, ticker: str, duration: str):
    try:
        return source.daily_bars(ticker, duration), None
    except Exception as exc:   # one unpriceable security must not stop the build
        return [], exc


def is_positive(value, allow_zero: bool = False) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and (value >= 0 if allow_zero else value > 0)


def _skip(summary: BuildSummary, ticker: str, reason: str) -> None:
    summary.skipped[ticker] = reason
    summary.counts["skipped: " + reason.split(":")[0].split(" (")[0]] += 1
