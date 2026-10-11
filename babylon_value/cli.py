"""Command-line interface.

Research
    python -m babylon_value demo                       synthetic backtest (mechanics only)
    python -m babylon_value screen   --data DIR --as-of YYYY-MM-DD
    python -m babylon_value explain  --data DIR --as-of YYYY-MM-DD --ticker XYZ
    python -m babylon_value backtest --data DIR --start YYYY-MM-DD --end YYYY-MM-DD
    python -m babylon_value export-synthetic --out DIR

Historical data (SEC EDGAR + Norgate Data, Windows with the Norgate Data Updater)
    python -m babylon_value build-history --out DIR --start YYYY-MM-DD

Paper trading (SEC EDGAR + Interactive Brokers paper account)
    python -m babylon_value build-dataset --out DIR
    python -m babylon_value paper-trade   --data DIR --risk-free 0.04 [--dry-run]
    python -m babylon_value reconcile     [--accept-broker-quantities]
"""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

from .backtest import backtest_problem, equal_weight_benchmark, run_backtest
from .config import StrategyConfig
from .costs import CostModel
from .data.csv_io import export_csv_directory, load_csv_directory
from .data.synthetic import generate_market
from .engine import Rebalancer, RunStatus, file_kill_switch
from .gates import STAGE_ORDER, build_entry_pipeline
from .journal import JsonlJournal
from .logic import EvaluationContext
from .screening import screen

SYNTHETIC_WARNING = (
    "NOTE: synthetic data. Prices in this market mean-revert to fair value by construction, so these numbers show\n"
    "that the machinery works - not that the strategy earns alpha. Test the hypothesis on real point-in-time data."
)
MAX_DATASET_AGE_DAYS = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="babylon_value", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="run a backtest on synthetic data")
    demo.add_argument("--companies", type=int, default=400)
    demo.add_argument("--seed", type=int, default=7)
    demo.add_argument("--start-year", type=int, default=2008)
    demo.add_argument("--years", type=int, default=15)
    demo.add_argument("--initial-cash", type=float, default=100_000.0)
    demo.add_argument("--contribution", type=float, default=1_000.0,
                      help="monthly contribution (Law 1: at least a tenth of what you earn)")
    demo.add_argument("--risk-free", type=float, default=0.04)
    demo.add_argument("--journal", help="write the decision journal (JSON Lines) to this path")

    for name, text in (("screen", "rank the securities that pass every gate"),
                       ("explain", "show every gate verdict for one security")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--data", required=True, help="dataset directory (see babylon_value/data/csv_io.py)")
        sub.add_argument("--as-of", required=True, type=date.fromisoformat)
        sub.add_argument("--risk-free", type=float, default=0.04)
        if name == "screen":
            sub.add_argument("--top", type=int, default=25)
        else:
            sub.add_argument("--ticker", required=True)

    backtest = commands.add_parser("backtest", help="backtest on a dataset of unadjusted prices plus splits")
    backtest.add_argument("--data", required=True)
    backtest.add_argument("--start", required=True, type=date.fromisoformat)
    backtest.add_argument("--end", required=True, type=date.fromisoformat)
    backtest.add_argument("--initial-cash", type=float, default=100_000.0)
    backtest.add_argument("--contribution", type=float, default=0.0)
    backtest.add_argument("--risk-free", type=float,
                          help="constant risk-free rate; only used when the dataset has no risk_free.csv")
    backtest.add_argument("--delisting-haircut", type=float, default=0.30,
                          help="loss applied when a holding delists (default 0.30; also try 0 to bracket the result)")
    backtest.add_argument("--journal")

    history = commands.add_parser("build-history", help="build a backtest dataset from SEC EDGAR and Norgate Data")
    history.add_argument("--out", default="data/history")
    history.add_argument("--start", type=date.fromisoformat, default=date(2012, 1, 1),
                         help="first date of the backtest window (XBRL financials begin around 2009-2011)")
    history.add_argument("--end", type=date.fromisoformat, default=date.today())
    history.add_argument("--sec-user-agent", default=os.environ.get("SEC_USER_AGENT"))
    history.add_argument("--cache-dir", default=".cache/sec")
    history.add_argument("--tickers", help="comma-separated tickers to restrict the build (for testing)")
    history.add_argument("--limit", type=int, help="include at most this many listings (for testing)")
    history.add_argument("--no-delisted", action="store_true", help="omit delisted stocks (introduces survivorship bias)")
    history.add_argument("--benchmark", default="$SPXTR", help="Norgate index symbol for the benchmark")

    export = commands.add_parser("export-synthetic", help="write a synthetic market as CSV files")
    export.add_argument("--out", required=True)
    export.add_argument("--companies", type=int, default=400)
    export.add_argument("--seed", type=int, default=7)

    build = commands.add_parser("build-dataset", help="fetch SEC EDGAR fundamentals and IB prices for a live run")
    build.add_argument("--out", default="data/live")
    build.add_argument("--sec-user-agent", default=os.environ.get("SEC_USER_AGENT"),
                       help="your name and email, as the SEC requires (or set SEC_USER_AGENT)")
    build.add_argument("--cache-dir", default=".cache/sec")
    build.add_argument("--tickers", help="comma-separated tickers to restrict the universe (for testing)")
    build.add_argument("--limit", type=int, help="consider at most this many companies (for testing)")
    build.add_argument("--ledger", default="paper_ledger.json", help="holdings in this ledger are always included")
    _ib_arguments(build)

    trade = commands.add_parser("paper-trade", help="run one rebalance against the IBKR paper account")
    trade.add_argument("--data", default="data/live")
    trade.add_argument("--risk-free", type=float, required=True,
                       help="current yield on short-term US Treasuries, e.g. 0.042")
    trade.add_argument("--dry-run", action="store_true", help="read the account and plan orders, but send none")
    trade.add_argument("--ledger", default="paper_ledger.json")
    trade.add_argument("--journal", default="journal/paper.jsonl")
    trade.add_argument("--kill-switch-file", default="KILL_SWITCH",
                       help="if this file exists, the run halts before sending anything")
    trade.add_argument("--account", help="IBKR paper account ID (starts with D)")
    _ib_arguments(trade)

    reconcile = commands.add_parser("reconcile", help="compare the IBKR paper account with the strategy ledger")
    reconcile.add_argument("--ledger", default="paper_ledger.json")
    reconcile.add_argument("--account")
    reconcile.add_argument("--accept-broker-quantities", action="store_true",
                           help="after a verified stock split, copy the broker's share counts into the ledger")
    _ib_arguments(reconcile)

    args = parser.parse_args(argv)
    config = StrategyConfig().validate()
    handlers = {"demo": _demo, "screen": _screen, "explain": _explain, "backtest": _backtest,
                "export-synthetic": _export, "build-dataset": _build_dataset, "paper-trade": _paper_trade,
                "reconcile": _reconcile, "build-history": _build_history}
    return handlers[args.command](args, config)


def _ib_arguments(parser) -> None:
    parser.add_argument("--ib-host", default="127.0.0.1")
    parser.add_argument("--ib-port", type=int, default=4002, help="IB Gateway paper 4002, TWS paper 7497")
    parser.add_argument("--ib-client-id", type=int, default=17)


def _gateway(args):
    from .ibkr import IbAsyncGateway
    return IbAsyncGateway(args.ib_host, args.ib_port, args.ib_client_id)


# ---------------------------------------------------------------------------- research

def _demo(args, config: StrategyConfig) -> int:
    provider = generate_market(args.companies, args.start_year, args.years, args.seed)
    first_rebalance_year = args.start_year + config.integrity.min_history_years
    result = run_backtest(
        provider, config,
        start=date(first_rebalance_year, 6, 1),
        end=date(args.start_year + args.years, 12, 31),
        initial_cash=args.initial_cash,
        monthly_contribution=args.contribution,
        risk_free_rate=args.risk_free,
        benchmark=equal_weight_benchmark(provider),
        journal=JsonlJournal(args.journal) if args.journal else None,
    )
    print(SYNTHETIC_WARNING)
    print()
    print(result.summary())
    print()
    print("Most recent rebalance:")
    print(result.reports[-1].summary())
    return 0


def _screen(args, config: StrategyConfig) -> int:
    provider = load_csv_directory(args.data)
    ctx = EvaluationContext(args.as_of, config, args.risk_free)
    result = screen(provider, build_entry_pipeline(), ctx, provider.universe(args.as_of))
    print(f"Screen as of {args.as_of} - config {config.fingerprint()}")
    print("Funnel (where each security was stopped):")
    for stage in (*STAGE_ORDER, "no_data"):
        if result.funnel.get(stage):
            print(f"  {stage:<22} {result.funnel[stage]:>6}")
    print(f"  {'eligible':<22} {result.funnel.get('eligible', 0):>6}")
    print()
    print(f"{'rank':>4}  {'ticker':<8} {'sector':<24} {'score':>6} {'F':>2} {'EV/EBIT':>8} {'price':>10}")
    for candidate in result.ranked[: args.top]:
        ey = candidate.factors["earnings_yield"]
        multiple = f"{1 / ey:.1f}" if 0 < ey < float("inf") else "<0"
        print(f"{candidate.rank:>4}  {candidate.ticker:<8} {candidate.sector[:24]:<24} {candidate.score:>6.2f} "
              f"{candidate.piotroski:>2} {multiple:>8} {candidate.price:>10.2f}")
    return 0


def _explain(args, config: StrategyConfig) -> int:
    provider = load_csv_directory(args.data)
    snap = provider.snapshot(args.ticker, args.as_of)
    if snap is None:
        print(f"no data for {args.ticker} on {args.as_of}")
        return 1
    result = build_entry_pipeline().evaluate(snap, EvaluationContext(args.as_of, config, args.risk_free))
    print(result.render())
    return 0 if result.passed else 2


def _backtest(args, config: StrategyConfig) -> int:
    from .data.rates import RiskFreeCurve

    provider = load_csv_directory(args.data)
    problem = backtest_problem(provider)
    if problem:
        print(f"refused: {problem}")
        return 1
    if provider.risk_free:
        risk_free = RiskFreeCurve(provider.risk_free).rate_at
    elif args.risk_free is not None:
        risk_free = args.risk_free
    else:
        print("refused: the dataset has no risk_free.csv; pass a constant with --risk-free")
        return 1
    benchmark = provider.benchmark_return if provider.benchmark else equal_weight_benchmark(provider)
    try:
        result = run_backtest(provider, config, args.start, args.end, initial_cash=args.initial_cash,
                              monthly_contribution=args.contribution, risk_free_rate=risk_free,
                              benchmark=benchmark, delisting_haircut=args.delisting_haircut,
                              journal=JsonlJournal(args.journal) if args.journal else None)
    except ValueError as exc:
        print(f"refused: {exc}")
        return 1
    print(result.summary())
    coverage = provider.metadata.get("delisted_coverage")
    if coverage is not None:
        print(f"  Delisted stocks covered by the dataset: {coverage:.0%}"
              + ("" if coverage >= 0.9 else " - the rest are missing, which can flatter these results"))
    print(f"  Benchmark: {provider.metadata.get('sources', {}).get('benchmark', 'equal-weight universe')}")
    return 0


def _build_history(args, config: StrategyConfig) -> int:
    from .data.edgar import SecClient
    from .data.norgate import NorgateDataSource, build_history_dataset
    from .data.rates import FRED_TB3MS_URL, parse_fred_csv
    from .net import HttpError, JsonClient, RateLimiter

    if not args.sec_user_agent:
        print("refused: the SEC requires a User-Agent with your name and email; pass --sec-user-agent "
              "'Jane Doe jane@example.com' or set SEC_USER_AGENT")
        return 1
    sec = SecClient(args.sec_user_agent, args.cache_dir)
    try:
        body = JsonClient({"User-Agent": args.sec_user_agent}, RateLimiter(1.0)).get_bytes(FRED_TB3MS_URL)
        rates = parse_fred_csv((body or b"").decode("utf-8"))
    except HttpError as exc:
        print(f"warning: could not fetch T-bill rates from FRED ({exc}); backtests will need --risk-free")
        rates = []
    tickers = {t.strip().upper() for t in args.tickers.split(",")} if args.tickers else None
    summary = build_history_dataset(sec, NorgateDataSource(), args.out, args.start, args.end, config,
                                    symbols=tickers, limit=args.limit, include_delisted=not args.no_delisted,
                                    benchmark_symbol=args.benchmark or None, risk_free=rates, log=print)
    print(summary.text())
    return 0


def _export(args, config: StrategyConfig) -> int:
    export_csv_directory(generate_market(args.companies, seed=args.seed), args.out)
    print(f"wrote a synthetic dataset to {args.out}")
    return 0


# ---------------------------------------------------------------------------- paper trading

def _build_dataset(args, config: StrategyConfig) -> int:
    from .data.edgar import SecClient
    from .data.live import build_live_dataset
    from .ibkr import Ledger, new_york_now

    if not args.sec_user_agent:
        print("refused: the SEC requires a User-Agent with your name and email; pass --sec-user-agent "
              "'Jane Doe jane@example.com' or set SEC_USER_AGENT")
        return 1
    sec = SecClient(args.sec_user_agent, args.cache_dir)
    out = Path(args.out)
    previous = load_csv_directory(out) if (out / "dataset.json").exists() else None
    holdings = set(Ledger(args.ledger).load())
    tickers = {t.strip().upper() for t in args.tickers.split(",")} if args.tickers else None
    gateway = _gateway(args)
    try:
        summary = build_live_dataset(sec, gateway, out, new_york_now().date(), config, tickers=tickers,
                                     always_include=holdings, limit=args.limit, previous=previous, log=print)
    finally:
        gateway.close()
    print(summary.text())
    return 0


def _paper_trade(args, config: StrategyConfig) -> int:
    from .ibkr import DryRunBroker, IbkrPaperBroker, new_york_now

    today = new_york_now().date()
    provider = load_csv_directory(args.data)
    meta = provider.metadata
    if meta.get("purpose") != "live":
        print(f"refused: {args.data} is not a live dataset; create one with build-dataset")
        return 1
    age = (today - date.fromisoformat(meta["built"])).days
    if age > MAX_DATASET_AGE_DAYS:
        print(f"refused: the dataset was built {age} days ago; rebuild it with build-dataset first")
        return 1
    if not args.dry_run and today.month not in config.portfolio.rebalance_months:
        months = ", ".join(date(2000, m, 1).strftime("%B") for m in config.portfolio.rebalance_months)
        print(f"refused: rebalancing happens on a fixed calendar ({months}) - no market timing. "
              "Use --dry-run to preview the orders.")
        return 1

    gateway = _gateway(args)
    try:
        broker = IbkrPaperBroker(gateway, args.ledger, CostModel(config.costs), account=args.account,
                                 require_market_open=not args.dry_run)
        if args.dry_run:
            broker = DryRunBroker(broker)
        rebalancer = Rebalancer(config, provider, broker, journal=JsonlJournal(args.journal),
                                kill_switch=file_kill_switch(args.kill_switch_file))
        report = rebalancer.run(today, args.risk_free)
    finally:
        gateway.close()
    print(report.summary())
    if args.dry_run:
        print("\nDRY RUN - nothing was sent. Planned orders (buys assume the sells have not happened):")
        for order in broker.planned:
            print(f"  {order.side.value:<4} {order.shares:>7} {order.ticker:<6} limit {order.limit_price:>9.2f}  "
                  f"{order.reason}")
    return 0 if report.status is RunStatus.COMPLETED else 3


def _reconcile(args, config: StrategyConfig) -> int:
    from .ibkr import Ledger

    gateway = _gateway(args)
    try:
        accounts = gateway.managed_accounts()
        account = args.account or (accounts[0] if len(accounts) == 1 else None)
        if account is None or not account.startswith("D"):
            print("refused: choose a paper account (ID starting with D) with --account")
            return 1
        held = {p.symbol: int(p.quantity) for p in gateway.positions(account) if p.quantity}
    finally:
        gateway.close()
    ledger = Ledger(args.ledger)
    entries = ledger.load()
    differences = sorted(t for t in set(held) | set(entries) if held.get(t) != entries.get(t, {}).get("shares"))
    if not differences:
        print("the account matches the ledger")
        return 0
    for ticker in differences:
        print(f"  {ticker:<6} broker {held.get(ticker, 0):>8}   ledger {entries.get(ticker, {}).get('shares', 0):>8}")
    if args.accept_broker_quantities:
        for ticker in differences:
            if ticker in held and ticker in entries and held[ticker] > 0:
                entries[ticker]["shares"] = held[ticker]
                print(f"  updated {ticker} to {held[ticker]} shares")
        ledger.save(entries)
        print("positions that exist on only one side were left for you to resolve by hand")
    return 2
