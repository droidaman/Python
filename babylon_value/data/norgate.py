"""Historical research dataset: SEC EDGAR fundamentals plus Norgate Data prices.

Requires a Norgate Data US Stocks subscription at Platinum or Diamond level
(for delisted stocks and major-exchange listing history), the Norgate Data
Updater (NDU) running on Windows, and ``pip install norgatedata``.

What makes the backtest honest
------------------------------
* Delisted stocks: both "US Equities" and "US Equities Delisted" are read, so
  companies that went bust or were taken over are part of the universe.
* Unadjusted prices, exact splits: prices are stored as they traded. Each
  split's ratio is the step change in (unadjusted close / capital-adjusted
  close), which captures splits, reverse splits, bonus issues and stock
  dividends exactly; the provider rescales reported share counts with them.
* Listing status: a stock counts as listed only on days Norgate shows it on a
  major exchange. Moving to the OTC market is treated as a delisting.
* Identity: the SEC maps only *current* tickers to companies (CIKs). Delisted
  stocks are matched by company name, accepted only when the name is unique
  and the match is verified: the company's 10-Ks fall in the stock's trading
  life, and price x share count agrees with the public float the company
  reported. Unverifiable stocks are excluded and counted in the coverage
  report, because excluding them can flatter results.
* Monthly bars: the backtest rebalances on month-ends, so daily data is stored
  as month-end bars carrying the month's average daily dollar volume and the
  month's dividends. Splits keep their exact dates.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Callable, Collection, Iterable, Protocol, Sequence

from ..config import StrategyConfig
from ..models import FinancialPeriod, SecurityProfile
from .csv_io import export_csv_directory
from .edgar import PLAIN_TICKER, PublicFloat, company_from_submissions, normalize_company_name, normalize_exchange, \
    parse_annual_reports, parse_public_float, parse_share_counts
from .provider import InMemoryProvider, PriceBar, QualityFlag, ShareCount, Split

DATABASES = ("US Equities", "US Equities Delisted")
DELISTED_SYMBOL = re.compile(r"^(?P<base>.+)-(?P<yearmonth>\d{6})$")
PRICE_LEAD = timedelta(days=730)        # prices before the window: liquidity history and float checks
SPLIT_TOLERANCE = 1e-3
FLOAT_BOUNDS = (0.8, 50.0)              # market value / public float for a verified match
FLOAT_FLAG = "price x share count at the public-float date is below the float the company reported"


@dataclass(frozen=True)
class NorgateSecurity:
    symbol: str
    name: str
    exchange: str
    subtype1: str
    subtype2: str
    subtype3: str
    currency: str
    first_quoted: date | None
    last_quoted: date | None
    delisted: bool

    @property
    def base_ticker(self) -> str:
        match = DELISTED_SYMBOL.match(self.symbol)
        return match.group("base") if match and self.delisted else self.symbol


@dataclass(frozen=True)
class DailyBar:
    date: date
    close: float            # as traded
    adjusted_close: float   # adjusted for capital events (splits etc.) only
    volume: float           # shares, as traded
    dividend: float         # per share as traded, on the entitlement date (day before ex-date)


class NorgateSource(Protocol):
    def symbols(self, database: str) -> list[str]: ...

    def security(self, symbol: str, database: str) -> NorgateSecurity: ...

    def daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]: ...

    def major_exchange_listed(self, symbol: str, start: date, end: date) -> dict[date, bool] | None: ...

    def index_levels(self, symbol: str, start: date, end: date) -> list[tuple[date, float]]: ...


class NorgateDataSource:
    """:class:`NorgateSource` implemented with the ``norgatedata`` package."""

    def __init__(self):
        try:
            import norgatedata
        except ImportError as exc:
            raise ImportError("Norgate Data support needs the norgatedata package (pip install norgatedata) and the "
                              "Norgate Data Updater running on Windows") from exc
        if not norgatedata.status():
            raise RuntimeError("the Norgate Data Updater (NDU) is not running on this computer")
        self.nd = norgatedata

    def symbols(self, database: str) -> list[str]:
        return list(self.nd.database_symbols(database) or [])

    def security(self, symbol: str, database: str) -> NorgateSecurity:
        nd = self.nd
        return NorgateSecurity(
            symbol, nd.security_name(symbol) or "", nd.exchange_name(symbol) or "", nd.subtype1(symbol) or "",
            nd.subtype2(symbol) or "", nd.subtype3(symbol) or "", nd.currency(symbol) or "",
            _day(nd.first_quoted_date(symbol)), _day(nd.last_quoted_date(symbol)), database.endswith("Delisted"))

    def daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        adjustment = self.nd.StockPriceAdjustmentType
        raw = self._prices(symbol, adjustment.NONE, start, end, ("Close", "Volume", "Dividend"))
        adjusted = self._prices(symbol, adjustment.CAPITAL, start, end, ("Close",))
        adjusted_close = {_day(d): float(c) for d, c in zip(adjusted["Date"], adjusted["Close"])}
        bars = []
        for d, close, volume, dividend in zip(raw["Date"], raw["Close"], raw["Volume"], raw["Dividend"]):
            day = _day(d)
            bars.append(DailyBar(day, float(close), adjusted_close.get(day, math.nan), float(volume),
                                 float(dividend) if math.isfinite(dividend) else 0.0))
        return bars

    def major_exchange_listed(self, symbol: str, start: date, end: date) -> dict[date, bool] | None:
        try:
            data = self.nd.major_exchange_listed_timeseries(symbol, start_date=start.isoformat(),
                                                            end_date=end.isoformat(), timeseriesformat="numpy-recarray")
        except Exception:   # needs a Platinum or Diamond subscription
            return None
        value = [name for name in data.dtype.names if name != "Date"][0]
        return {_day(d): bool(v) for d, v in zip(data["Date"], data[value])}

    def index_levels(self, symbol: str, start: date, end: date) -> list[tuple[date, float]]:
        data = self._prices(symbol, self.nd.StockPriceAdjustmentType.NONE, start, end, ("Close",))
        return [(_day(d), float(c)) for d, c in zip(data["Date"], data["Close"])]

    def _prices(self, symbol, adjustment, start: date, end: date, fields: Sequence[str]):
        data = self.nd.price_timeseries(symbol, stock_price_adjustment_setting=adjustment,
                                        padding_setting=self.nd.PaddingType.NONE, start_date=start.isoformat(),
                                        end_date=end.isoformat(), timeseriesformat="numpy-recarray")
        missing = [f for f in ("Date", *fields) if f not in (data.dtype.names or ())]
        if missing:
            raise ValueError(f"Norgate returned fields {data.dtype.names} for {symbol}; missing {missing}")
        return data


def _day(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    return date.fromisoformat(text) if text else None


# ---------------------------------------------------------------------------- price processing

def derive_splits(bars: Sequence[DailyBar]) -> list[Split]:
    """Capital events from the step changes in unadjusted / adjusted close."""
    factors = [(b.date, b.close / b.adjusted_close) for b in bars
               if _positive(b.close) and _positive(b.adjusted_close)]
    splits = []
    for (_, before), (day, after) in zip(factors, factors[1:]):
        ratio = before / after
        if abs(ratio - 1) > SPLIT_TOLERANCE:
            splits.append(Split(day, _snap(ratio)))
    return splits


def _snap(ratio: float) -> float:
    """2.0000003 -> 2.0, 0.0999998 -> 0.1; leave genuinely odd ratios alone."""
    simple = Fraction(ratio).limit_denominator(20)
    return float(simple) if abs(float(simple) / ratio - 1) < 0.002 else round(ratio, 6)


def listed_days(bars: Sequence[DailyBar], listed: dict[date, bool] | None) -> list[DailyBar]:
    """Keep only days on which the stock was on a major exchange."""
    if listed is None:
        return list(bars)
    status, known = False, sorted(listed.items())
    kept, index = [], 0
    for bar in bars:
        while index < len(known) and known[index][0] <= bar.date:
            status = known[index][1]
            index += 1
        if status:
            kept.append(bar)
    return kept


def monthly_bars(bars: Sequence[DailyBar]) -> list[PriceBar]:
    """One bar per month: the last close, the month's average daily dollar
    volume (expressed as shares at that close) and the month's dividends."""
    months: dict[tuple[int, int], list[DailyBar]] = {}
    for bar in bars:
        if _positive(bar.close):
            months.setdefault((bar.date.year, bar.date.month), []).append(bar)
    out = []
    for key in sorted(months):
        rows = months[key]
        last = rows[-1]
        traded = [b.close * b.volume for b in rows if math.isfinite(b.volume) and b.volume >= 0]
        dollar_volume = sum(traded) / len(traded) if traded else math.nan
        out.append(PriceBar(last.date, last.close, dollar_volume / last.close,
                            sum(b.dividend for b in rows if math.isfinite(b.dividend))))
    return out


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


# ---------------------------------------------------------------------------- share basis

def rebase(shares: float, basis: date, target: date, splits: Sequence[Split]) -> float:
    """Express a share count reported on ``basis`` on the share basis of ``target``."""
    factor = 1.0
    for split in splits:
        if basis < split.ex_date <= target:
            factor *= split.ratio
        elif target < split.ex_date <= basis:
            factor /= split.ratio
    return shares * factor


def shares_on(day: date, counts: Sequence[ShareCount], periods: Sequence[FinancialPeriod],
              splits: Sequence[Split]) -> float | None:
    """Best share count for ``day``, on that day's share basis."""
    covers = [c for c in counts if c.as_of <= day]
    if covers:
        latest = max(covers, key=lambda c: (c.as_of, c.available_date))
        return rebase(latest.shares, latest.as_of, day, splits)
    annual = [p for p in periods if p.fiscal_year_end <= day and _positive(p.shares_outstanding)]
    if annual:
        latest = max(annual, key=lambda p: (p.fiscal_year_end, p.available_date))
        return rebase(latest.shares_outstanding, latest.available_date, day, splits)
    return None


def float_ratios(floats: Sequence[PublicFloat], daily: Sequence[DailyBar], counts: Sequence[ShareCount],
                 periods: Sequence[FinancialPeriod], splits: Sequence[Split]) -> list[tuple[PublicFloat, float]]:
    """(float, market value / float) wherever both can be computed."""
    closes = sorted((b.date, b.close) for b in daily if _positive(b.close))
    out = []
    for public in floats:
        prior = [(d, c) for d, c in closes if d <= public.as_of and (public.as_of - d).days <= 7]
        shares = shares_on(public.as_of, counts, periods, splits)
        if prior and shares:
            out.append((public, prior[-1][1] * shares / public.value))
    return out


def float_flags(ratios: Sequence[tuple[PublicFloat, float]]) -> list[QualityFlag]:
    """Flag each 10-K period whose share count disagrees with the reported
    float, from that 10-K's filing until the next one."""
    ordered = sorted(ratios, key=lambda item: item[0].available_date)
    flags = []
    for index, (public, ratio) in enumerate(ordered):
        if ratio < FLOAT_BOUNDS[0]:
            following = ordered[index + 1][0].available_date - timedelta(days=1) if index + 1 < len(ordered) else None
            flags.append(QualityFlag(public.available_date, following,
                                     f"{FLOAT_FLAG} on {public.as_of} ({ratio:.2f}x): share count unreliable"))
    return flags


# ---------------------------------------------------------------------------- the build

@dataclass
class HistorySummary:
    start: date
    end: date
    counts: Counter = field(default_factory=Counter)
    excluded: dict[str, str] = field(default_factory=dict)

    @property
    def delisted_coverage(self) -> float:
        eligible = self.counts["delisted listings in scope"]
        return self.counts["delisted listings included"] / eligible if eligible else math.nan

    def text(self) -> str:
        lines = [f"History dataset {self.start} -> {self.end}"]
        lines += [f"  {key:<44} {value:>7}" for key, value in sorted(self.counts.items())]
        if math.isfinite(self.delisted_coverage):
            lines.append(f"  Delisted stocks covered: {self.delisted_coverage:.0%}. Delisted stocks that could not be "
                         "matched to SEC filings are left out, which can flatter backtest results.")
        return "\n".join(lines)


def listing_problem(security: NorgateSecurity, start: date, end: date) -> str | None:
    if security.currency != "USD":
        return "not traded in USD"
    if (security.subtype1, security.subtype2, security.subtype3) != ("Equity", "Operating/Holding Company", ""):
        return "not an operating company's common stock"
    if not PLAIN_TICKER.match(security.base_ticker):
        return "share class or unusual ticker"
    if security.last_quoted is not None and security.last_quoted < start:
        return "stopped trading before the window"
    if security.first_quoted is not None and security.first_quoted > end:
        return "listed after the window"
    return None


def _base(symbol: str, database: str) -> str:
    match = DELISTED_SYMBOL.match(symbol)
    return match.group("base") if match and database.endswith("Delisted") else symbol


def _overlaps(a: NorgateSecurity, b: NorgateSecurity) -> bool:
    a_start, a_end = a.first_quoted or date.min, a.last_quoted or date.max
    b_start, b_end = b.first_quoted or date.min, b.last_quoted or date.max
    return a_start <= b_end and b_start <= a_end


def build_history_dataset(
    sec,
    norgate: NorgateSource,
    out_dir: str | Path,
    start: date,
    end: date,
    config: StrategyConfig,
    symbols: Collection[str] | None = None,
    limit: int | None = None,
    include_delisted: bool = True,
    benchmark_symbol: str | None = "$SPXTR",
    risk_free: Iterable[tuple[date, float]] | None = None,
    log: Callable[[str], None] = lambda message: None,
) -> HistorySummary:
    config.validate()
    summary = HistorySummary(start, end)
    counts = summary.counts

    # 1. Listings in scope: operating companies' common stock that traded in the window.
    listings: list[NorgateSecurity] = []
    for database in DATABASES if include_delisted else DATABASES[:1]:
        for symbol in norgate.symbols(database):
            if symbols is not None and _base(symbol, database) not in symbols:
                continue
            security = norgate.security(symbol, database)
            problem = listing_problem(security, start, end)
            if problem:
                counts[f"skipped: {problem}"] += 1
                continue
            listings.append(security)
            if security.delisted:
                counts["delisted listings in scope"] += 1
    listings.sort(key=lambda s: s.symbol)
    if limit is not None:
        listings = listings[:limit]
    counts["listings in scope"] = len(listings)

    # 2. Which SEC filer is each listing? Current tickers first, then verified names.
    by_ticker = {r.ticker: r.cik for r in sec.company_tickers()}
    names: dict[str, frozenset[int]] | None = None
    matched: dict[str, tuple[int, str]] = {}
    for security in listings:
        if not security.delisted and security.base_ticker in by_ticker:
            matched[security.symbol] = (by_ticker[security.base_ticker], "ticker")
            continue
        if names is None:
            log("  loading SEC entity names for delisted companies")
            names = sec.entity_names()
        ciks = names.get(normalize_company_name(security.name), frozenset())
        if len(ciks) == 1:
            matched[security.symbol] = (next(iter(ciks)), "name")
        else:
            reason = "no SEC filer with this name" if not ciks else "name matches several SEC filers"
            _exclude(summary, security, reason)

    # One listing per company at any time (share classes, duplicate listings).
    kept: dict[int, list[NorgateSecurity]] = {}
    for security in sorted((s for s in listings if s.symbol in matched),
                           key=lambda s: (s.first_quoted or date.min, s.symbol)):
        cik = matched[security.symbol][0]
        if any(_overlaps(security, other) for other in kept.get(cik, [])):
            _exclude(summary, security, "another listing of the same company is used")
            continue
        kept.setdefault(cik, []).append(security)

    # 3. Fundamentals, prices and verification.
    profiles, fundamentals, prices, splits, share_counts, flags = {}, {}, {}, {}, {}, {}
    chosen = sorted((s for group in kept.values() for s in group), key=lambda s: s.symbol)
    for index, security in enumerate(chosen, start=1):
        if index % 250 == 0:
            log(f"  companies: {index}/{len(chosen)}")
        cik, method = matched[security.symbol]
        submissions = sec.submissions(cik)
        if submissions is None:
            _exclude(summary, security, "no SEC submissions")
            continue
        company = company_from_submissions(submissions)
        if not company.operating:
            _exclude(summary, security, "SEC filer is not an operating company")
            continue
        facts = sec.company_facts(cik)
        periods = parse_annual_reports(facts) if facts else []
        if not periods:
            _exclude(summary, security, "no XBRL annual reports")
            continue
        counts_for = parse_share_counts(facts)
        floats = parse_public_float(facts)

        daily = norgate.daily_bars(security.symbol, start - PRICE_LEAD, end)
        events = derive_splits(daily)
        ratios = float_ratios(floats, daily, counts_for, periods, events)
        if method == "name":
            problem = _verify_name_match(security, periods, ratios)
            if problem:
                _exclude(summary, security, f"name match not verified: {problem}")
                continue
        listed = listed_days(daily, norgate.major_exchange_listed(security.symbol, start - PRICE_LEAD, end))
        bars = monthly_bars(listed)
        if not bars:
            _exclude(summary, security, "never listed on a major exchange in the window")
            continue

        ticker = security.symbol
        profiles[ticker] = SecurityProfile(ticker, company.name or security.name, company.sector,
                                           normalize_exchange(security.exchange), "common_stock", company.country)
        fundamentals[ticker], prices[ticker], share_counts[ticker] = periods, bars, counts_for
        if events:
            splits[ticker] = events
        found = float_flags(ratios)
        if found:
            flags[ticker] = found
            counts["included with data-quality flags"] += 1
        counts[f"included (matched by {method})"] += 1
        if security.delisted:
            counts["delisted listings included"] += 1

    benchmark = []
    if benchmark_symbol:
        try:
            benchmark = [(d, level) for d, level in norgate.index_levels(benchmark_symbol, start, end) if _positive(level)]
        except Exception as exc:   # e.g. no index subscription: fall back to an equal-weight benchmark
            log(f"  benchmark {benchmark_symbol} unavailable ({exc}); backtests will use an equal-weight benchmark")
    rates = sorted(risk_free or [])
    provider = InMemoryProvider(
        profiles, fundamentals, prices, splits=splits, share_counts=share_counts, quality_flags=flags,
        risk_free=rates, benchmark=benchmark,
        metadata={
            "purpose": "research",
            "price_basis": "unadjusted",
            "window": [start.isoformat(), end.isoformat()],
            "config_fingerprint": config.fingerprint(),
            "sources": {"fundamentals": "SEC EDGAR XBRL company facts",
                        "prices": "Norgate Data (unadjusted; splits derived from capital adjustment)",
                        "benchmark": benchmark_symbol if benchmark else "equal-weight universe",
                        "risk_free": "FRED TB3MS" if rates else "none"},
            "coverage": dict(counts),
            "delisted_coverage": None if math.isnan(summary.delisted_coverage) else round(summary.delisted_coverage, 4),
        })
    export_csv_directory(provider, out_dir)
    return summary


def _verify_name_match(security: NorgateSecurity, periods: Sequence[FinancialPeriod],
                       ratios: Sequence[tuple[PublicFloat, float]]) -> str | None:
    first, last = security.first_quoted or date.min, security.last_quoted or date.max
    reports_until = security.last_quoted + timedelta(days=400) if security.last_quoted else date.max
    if not any(first <= p.available_date <= reports_until for p in periods):
        return "the company's annual reports do not fall in the stock's trading life"
    during = [ratio for public, ratio in ratios if first <= public.as_of <= last]
    if not during:
        return "no public float to check the share count against"
    outside = [r for r in during if not FLOAT_BOUNDS[0] <= r <= FLOAT_BOUNDS[1]]
    if outside:
        return f"price x share count is {outside[0]:.2f}x the reported public float"
    return None


def _exclude(summary: HistorySummary, security: NorgateSecurity, reason: str) -> None:
    summary.excluded[security.symbol] = reason
    summary.counts[f"excluded: {reason.split(':')[0]}"] += 1
