# Babylon Value

A fail-closed, rules-based **deep value** equity strategy built on Graham, Greenblatt and Carlisle,
and on the Five Laws of Gold and Seven Cures for a Lean Purse from George S. Clason's *The Richest Man
in Babylon*.

Every rule is a small logic gate that answers PASS, FAIL or ERROR. ERROR spreads through AND and OR, so
only an explicit PASS can move capital, and anything the system can't verify is treated as a no.

**Read the plan first: [docs/PLAN.md](docs/PLAN.md)**. It covers each principle and the rule it maps to,
the full gate specification, the sell discipline, execution safety, bias controls and the validation
roadmap.

## Quick start

Python 3.10+ with no third-party dependencies.

```bash
python -m unittest discover -s tests -t .           # 79 tests

python -m babylon_value demo                         # synthetic backtest (mechanics only)
python -m babylon_value export-synthetic --out data/synthetic
python -m babylon_value screen  --data data/synthetic --as-of 2020-06-30
python -m babylon_value explain --data data/synthetic --as-of 2020-06-30 --ticker SYN070
```

`explain` prints the full gate tree for one security, showing why it was or wasn't eligible:

```
[PASS ] entry: all 2 checks passed
  [PASS ] data_integrity: all 5 checks passed
  ...
    [PASS ] guard_thy_treasure: all 5 checks passed
      [PASS ] altman_z: Altman Z 3.59 is clear of the distress zone (< 1.81)
  ...
      [PASS ] margin_of_safety: satisfied by cheap_on_earnings
        [PASS ] cheap_on_earnings: all 2 checks passed
          [PASS ] acquirers_multiple: EV/EBIT 5.0 vs maximum 8.0
          [PASS ] earnings_yield_hurdle: earnings yield 19.8% vs hurdle 8.0%
        [FAIL ] net_net: market cap / NCAV 3.66 vs maximum 0.67
      [PASS ] piotroski: F-score 6/9 vs minimum 6
```

## Using your own data

Put three CSV files in a directory (schema in `babylon_value/data/csv_io.py`):

* `securities.csv`: ticker, name, sector, exchange, security_type, country
* `fundamentals.csv`: one row per annual report version, **including the date it was published**
  (`available_date`). This is what prevents look-ahead bias.
* `prices.csv`: ticker, date, close, volume, dividend. Include securities that later delisted.

Blank cells load as missing (never zero), and the integrity gates reject them.

## From Python

```python
from datetime import date
from babylon_value import StrategyConfig, run_backtest, equal_weight_benchmark
from babylon_value.data import load_csv_directory

provider = load_csv_directory("data/my_market")
result = run_backtest(provider, StrategyConfig(), date(2010, 6, 1), date(2024, 12, 31),
                      initial_cash=100_000, monthly_contribution=1_000, risk_free_rate=0.04,
                      benchmark=equal_weight_benchmark(provider))
print(result.summary())
```

## Status and caveats

Phase 1 of the [roadmap](docs/PLAN.md#10-roadmap) is complete: the engine, gates, paper broker,
backtester and tests. There is **no live broker**. The synthetic market used by `demo` assumes mean
reversion by construction, so its numbers show that the machinery works, not that the strategy earns
alpha. Validate on real point-in-time data, then paper trade, before risking money. This is not
investment advice.
