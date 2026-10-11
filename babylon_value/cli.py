"""Command-line interface.

    python -m babylon_value demo                       synthetic backtest (mechanics only)
    python -m babylon_value screen  --data DIR --as-of YYYY-MM-DD
    python -m babylon_value explain --data DIR --as-of YYYY-MM-DD --ticker XYZ
    python -m babylon_value export-synthetic --out DIR
"""

from __future__ import annotations

import argparse
from datetime import date

from .backtest import equal_weight_benchmark, run_backtest
from .config import StrategyConfig
from .data.csv_io import export_csv_directory, load_csv_directory
from .data.synthetic import generate_market
from .gates import STAGE_ORDER, build_entry_pipeline
from .journal import JsonlJournal
from .logic import EvaluationContext
from .screening import screen

SYNTHETIC_WARNING = (
    "NOTE: synthetic data. Prices in this market mean-revert to fair value by construction, so these numbers show\n"
    "that the machinery works - not that the strategy earns alpha. Test the hypothesis on real point-in-time data."
)


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
        sub.add_argument("--data", required=True, help="directory with securities.csv, fundamentals.csv, prices.csv")
        sub.add_argument("--as-of", required=True, type=date.fromisoformat)
        sub.add_argument("--risk-free", type=float, default=0.04)
        if name == "screen":
            sub.add_argument("--top", type=int, default=25)
        else:
            sub.add_argument("--ticker", required=True)

    export = commands.add_parser("export-synthetic", help="write a synthetic market as CSV files")
    export.add_argument("--out", required=True)
    export.add_argument("--companies", type=int, default=400)
    export.add_argument("--seed", type=int, default=7)

    args = parser.parse_args(argv)
    config = StrategyConfig().validate()
    if args.command == "demo":
        return _demo(args, config)
    if args.command == "screen":
        return _screen(args, config)
    if args.command == "explain":
        return _explain(args, config)
    export_csv_directory(generate_market(args.companies, seed=args.seed), args.out)
    print(f"wrote securities.csv, fundamentals.csv and prices.csv to {args.out}")
    return 0


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
