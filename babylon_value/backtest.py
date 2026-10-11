"""Monthly, point-in-time backtester.

Each month-end: apply stock splits to holdings, credit dividends (Cure 3 -
they are reinvested at the next rebalance), add the periodic contribution (Law 1 - a part of all you earn is
yours to keep), close any holding that has been delisted at its last price
less a haircut (survivorship bias - Shumway, 1997, finds delisting returns
around -30%), rebalance on the configured months and mark to market.

Returns are time-weighted so contributions do not flatter performance.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from .config import StrategyConfig
from .costs import CostModel
from .data.provider import DataProvider
from .dates import month_ends
from .engine import RebalanceReport, Rebalancer, RunStatus
from .execution import PaperBroker
from .journal import Journal

BenchmarkFn = Callable[[date, date], "float | None"]


@dataclass
class BacktestResult:
    config_fingerprint: str
    dates: list[date] = field(default_factory=list)
    nav: list[float] = field(default_factory=list)
    cash_fraction: list[float] = field(default_factory=list)
    holdings: list[int] = field(default_factory=list)
    returns: list[float] = field(default_factory=list)            # time-weighted, one per month after the first
    benchmark_returns: list[float | None] = field(default_factory=list)
    risk_free_monthly: list[float] = field(default_factory=list)
    contributions: float = 0.0
    costs_paid: float = 0.0
    delistings: list[str] = field(default_factory=list)
    reports: list[RebalanceReport] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        m = self.metrics
        halted = sum(1 for r in self.reports if r.status is RunStatus.HALTED)
        lines = [
            f"Backtest {self.dates[0]} -> {self.dates[-1]} ({len(self.returns)} months), config {self.config_fingerprint}",
            f"  Final NAV            {self.nav[-1]:>14,.0f}   (contributions {self.contributions:,.0f})",
            f"  CAGR (time-weighted) {_pct(m.get('cagr'))}",
            f"  Benchmark CAGR       {_pct(m.get('benchmark_cagr'))}",
            f"  Volatility           {_pct(m.get('volatility'))}",
            f"  Sharpe ratio         {_num(m.get('sharpe'))}",
            f"  Max drawdown         {_pct(m.get('max_drawdown'))}",
            f"  Beta vs benchmark    {_num(m.get('beta'))}",
            f"  Jensen's alpha (ann.) {_pct(m.get('alpha'))}",
            f"  Average holdings     {_num(m.get('avg_holdings'), 1)}   average cash {_pct(m.get('avg_cash_fraction'))}",
            f"  Costs paid           {self.costs_paid:>14,.0f}",
            f"  Rebalances           {len(self.reports)} ({halted} halted); delistings while held: {len(self.delistings)}",
        ]
        return "\n".join(lines)


def run_backtest(
    provider: DataProvider,
    config: StrategyConfig,
    start: date,
    end: date,
    initial_cash: float = 100_000.0,
    monthly_contribution: float = 0.0,
    risk_free_rate: float | Callable[[date], float] = 0.04,
    benchmark: BenchmarkFn | None = None,
    delisting_haircut: float = 0.30,
    journal: Journal | None = None,
) -> BacktestResult:
    config.validate()
    metadata = getattr(provider, "metadata", {})
    if metadata.get("purpose") == "live" or metadata.get("price_basis") == "split_adjusted":
        raise ValueError("this dataset's prices are split-adjusted to its build date, so historical market caps "
                         "would be wrong around every split; it is for live trading only. Backtests need "
                         "unadjusted prices plus a splits file.")
    if not 0.0 <= delisting_haircut <= 1.0:
        raise ValueError("delisting_haircut must be in [0, 1]")
    broker = PaperBroker(initial_cash, CostModel(config.costs))
    rebalancer = Rebalancer(config, provider, broker, journal=journal)
    rf_at = risk_free_rate if callable(risk_free_rate) else (lambda _d, rate=risk_free_rate: rate)
    result = BacktestResult(config_fingerprint=rebalancer.fingerprint)

    dates = month_ends(start, end)
    if len(dates) < 2:
        raise ValueError("a backtest needs at least two month-ends")
    previous: date | None = None
    for current in dates:
        flow = 0.0
        if previous is not None:
            _apply_corporate_actions(broker, provider, previous, current)
            if monthly_contribution > 0:
                broker.deposit(monthly_contribution)
                flow = monthly_contribution
                result.contributions += monthly_contribution

        for ticker in sorted(broker.positions()):
            bar = provider.price(ticker, current)
            if bar is None or (current - bar.date).days > config.integrity.max_price_age_days:
                last = bar.close if bar is not None else 0.0
                broker.liquidate(ticker, last * (1 - delisting_haircut), current)
                result.delistings.append(f"{current} {ticker}")

        if current.month in config.portfolio.rebalance_months:
            result.reports.append(rebalancer.run(current, rf_at(current)))

        nav = broker.cash() + sum(p.shares * provider.price(t, current).close for t, p in broker.positions().items())
        if previous is not None:
            result.returns.append((nav - flow) / result.nav[-1] - 1)
            result.benchmark_returns.append(benchmark(previous, current) if benchmark else None)
            result.risk_free_monthly.append((1 + rf_at(current)) ** (1 / 12) - 1)
        result.dates.append(current)
        result.nav.append(nav)
        result.cash_fraction.append(broker.cash() / nav if nav > 0 else math.nan)
        result.holdings.append(len(broker.positions()))
        previous = current

    result.costs_paid = sum(f.cost for f in broker.fills)
    result.metrics = performance_metrics(result)
    return result


def performance_metrics(result: BacktestResult, periods_per_year: int = 12) -> dict[str, float]:
    r = result.returns
    if len(r) < 2:
        return {}
    years = len(r) / periods_per_year
    growth = math.prod(1 + x for x in r)
    excess = [x - f for x, f in zip(r, result.risk_free_monthly)]
    sd = statistics.stdev(r)
    metrics = {
        "months": float(len(r)),
        "total_return": growth - 1,
        "cagr": growth ** (1 / years) - 1,
        "volatility": sd * math.sqrt(periods_per_year),
        "sharpe": statistics.fmean(excess) / statistics.stdev(excess) * math.sqrt(periods_per_year)
        if statistics.stdev(excess) > 0 else math.nan,
        "max_drawdown": _max_drawdown(r),
        "avg_holdings": statistics.fmean(result.holdings),
        "avg_cash_fraction": statistics.fmean(result.cash_fraction),
    }
    paired = [(x, b, f) for x, b, f in zip(r, result.benchmark_returns, result.risk_free_monthly) if b is not None]
    if len(paired) >= 12:
        strat = [x - f for x, _, f in paired]
        bench = [b - f for _, b, f in paired]
        variance = statistics.variance(bench)
        if variance > 0:
            beta = statistics.covariance(strat, bench) / variance
            alpha = statistics.fmean(strat) - beta * statistics.fmean(bench)
            metrics["beta"] = beta
            metrics["alpha"] = alpha * periods_per_year
        bench_growth = math.prod(1 + b for _, b, _ in paired)
        metrics["benchmark_cagr"] = bench_growth ** (periods_per_year / len(paired)) - 1
    return metrics


def equal_weight_benchmark(provider, delisting_haircut: float = 0.30, max_price_age_days: int = 7) -> BenchmarkFn:
    """Total return of an equal-weighted portfolio of every listed security,
    including the ones that delist during the period."""

    def period_return(start: date, end: date) -> float | None:
        returns = []
        for ticker in provider.universe(start):
            first = provider.price(ticker, start)
            last = provider.price(ticker, end)
            if first is None or last is None or first.close <= 0:
                continue
            shares, cash = 1.0, 0.0
            cursor = start
            for split in _splits(provider, ticker, start, end):
                cash += shares * provider.dividends(ticker, cursor, split.ex_date - timedelta(days=1))
                shares *= split.ratio
                cursor = split.ex_date - timedelta(days=1)
            cash += shares * provider.dividends(ticker, cursor, end)
            closing = last.close
            if (end - last.date).days > max_price_age_days:
                closing *= 1 - delisting_haircut
            returns.append((shares * closing + cash) / first.close - 1)
        return statistics.fmean(returns) if returns else None

    return period_return


def _splits(provider, ticker: str, start: date, end: date):
    splits = getattr(provider, "splits", None)
    return splits(ticker, start, end) if splits else []


def _apply_corporate_actions(broker: PaperBroker, provider, start: date, end: date) -> None:
    """Splits and dividends for each holding in (start, end], in date order.
    A dividend on a split's ex-date is per post-split share."""
    for ticker in sorted(broker.positions()):
        cursor = start
        for split in _splits(provider, ticker, start, end):
            _credit_dividends(broker, provider, ticker, cursor, split.ex_date - timedelta(days=1))
            bar = provider.price(ticker, end)
            broker.apply_split(ticker, split.ratio, bar.close if bar is not None else 0.0)
            cursor = split.ex_date - timedelta(days=1)
        _credit_dividends(broker, provider, ticker, cursor, end)


def _credit_dividends(broker: PaperBroker, provider, ticker: str, start: date, end: date) -> None:
    position = broker.positions().get(ticker)
    if position is None or end <= start:
        return
    per_share = provider.dividends(ticker, start, end)
    if per_share > 0:
        broker.deposit(position.shares * per_share)


def _max_drawdown(returns: list[float]) -> float:
    level = peak = 1.0
    worst = 0.0
    for x in returns:
        level *= 1 + x
        peak = max(peak, level)
        worst = min(worst, level / peak - 1)
    return worst


def _pct(value) -> str:
    return f"{value:>14.2%}" if isinstance(value, float) and math.isfinite(value) else f"{'n/a':>14}"


def _num(value, digits: int = 2) -> str:
    return f"{value:>14.{digits}f}" if isinstance(value, float) and math.isfinite(value) else f"{'n/a':>14}"
