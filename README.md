# Babylon Value

A fail-closed, rules-based **deep value** equity strategy for US stocks, built on Graham, Greenblatt and
Carlisle, and on the Five Laws of Gold and Seven Cures for a Lean Purse from George S. Clason's *The Richest
Man in Babylon*. Fundamentals come from **SEC EDGAR**, backtest prices from **Norgate Data**, and paper trading
runs through **Interactive Brokers**.

Every rule is a small logic gate that answers PASS, FAIL or ERROR. ERROR spreads through AND and OR, so
only an explicit PASS can move capital, and anything the system can't verify is treated as a no.

**Read the plan first: [docs/PLAN.md](docs/PLAN.md)**. It covers each principle and its rule, the gate
specification, the sell discipline, execution safety, bias controls, the live data and broker design
(§12), the historical data design (§13) and the roadmap.

## Quick start (no accounts needed)

Python 3.10+. The core has no third-party dependencies.

```bash
python -m unittest discover -s tests -t .
python -m babylon_value demo                         # synthetic backtest (mechanics only)
python -m babylon_value export-synthetic --out data/synthetic
python -m babylon_value screen  --data data/synthetic --as-of 2020-06-30
python -m babylon_value explain --data data/synthetic --as-of 2020-06-30 --ticker SYN070
```

`explain` prints the full gate tree for one security, showing why it was or wasn't eligible.

## Paper trading with Interactive Brokers

Run these on your own computer, where IB Gateway (or TWS) runs.

1. **Install the IB library:** `pip install ib_async` (or `pip install -e ".[ibkr]"`).
2. **Start IB Gateway and log in to your *paper* account.** In *Configure → Settings → API → Settings*:
   enable socket clients, untick *Read-Only API*, and note the port (IB Gateway paper defaults to **4002**,
   TWS paper to **7497**; pass `--ib-port` if yours differs). The adapter refuses any account that isn't a
   paper account (IDs starting with "D").
3. **Identify yourself to the SEC** (required by the SEC's fair-access policy):
   `export SEC_USER_AGENT="Your Name your.email@example.com"`
4. **Build today's dataset.** The first run takes a while (thousands of SEC requests at 8/second, cached
   afterwards), then IB prices for the companies that pass every check that needs no prices:
   ```bash
   python -m babylon_value build-dataset --out data/live
   python -m babylon_value build-dataset --out data/live --tickers AAPL,MSFT,CAT --limit 50   # quick trial
   ```
   It reports how many companies were skipped and why, and any data-quality flags (e.g. a share count that
   may predate a stock split).
5. **Preview, then trade.** You must pass today's short-term Treasury yield as the risk-free rate.
   ```bash
   python -m babylon_value paper-trade --data data/live --risk-free 0.042 --dry-run   # any day: plan only
   python -m babylon_value paper-trade --data data/live --risk-free 0.042             # rebalance months only
   ```
   Real orders go out only in **March, June, September and December**, during regular market hours, from a
   dataset built within the last day. Every run is logged to `journal/paper.jsonl`.

Day-to-day controls:

* `touch KILL_SWITCH` halts every run before anything is sent; delete the file to resume.
* The strategy's own record of its holdings lives in `paper_ledger.json`, so **back it up**. If the account
  ever differs from it, runs halt until you check `python -m babylon_value reconcile`. After a stock split
  you have verified, `reconcile --accept-broker-quantities` updates the share counts. Positions that exist on
  only one side must be fixed by hand.
* Use a dedicated paper account: anything the strategy didn't buy itself halts the run.

## Backtesting with Norgate Data

Run these on a Windows PC with the Norgate Data Updater running and a US Stocks subscription at Platinum or
Diamond level (needed for delisted stocks and listing history).

1. `pip install norgatedata` (or `pip install -e ".[norgate]"`), and set `SEC_USER_AGENT` as above.
2. **Build the research dataset.** It reads current and delisted US stocks from Norgate, matches each to its
   SEC filings, and downloads historical T-bill rates from FRED. The first build makes many thousands of SEC
   requests, which are cached afterwards.
   ```bash
   python -m babylon_value build-history --out data/history --start 2012-01-01
   python -m babylon_value build-history --out data/trial --tickers CAT,DE,AAPL,MSFT   # quick trial
   ```
   Read the coverage report it prints, especially the share of delisted stocks included.
3. **Backtest.** The rules need five years of XBRL financials, which begin in 2009–2011, so start around 2014:
   ```bash
   python -m babylon_value backtest --data data/history --start 2014-06-01 --end 2025-09-30
   python -m babylon_value backtest --data data/history --start 2014-06-01 --end 2025-09-30 --delisting-haircut 0
   ```
   The two runs bracket the effect of delistings: takeovers usually pay close to the last price, while
   failures lose much more. Results are compared with the S&P 500 total return.

Datasets built for paper trading from IB prices are split-adjusted, and the backtester refuses them.

## Status and caveats

The tooling for phases 1–3 of the [roadmap](docs/PLAN.md#10-roadmap) is complete: the engine, gates, EDGAR
adapter, Norgate history builder and IB paper trading. The adapters are tested against responses in each
service's real format, and against the real `ib_async` and `norgatedata` libraries with only the connection
replaced, but haven't yet been run against the live services. Start with a `--dry-run` and small `--tickers`
builds. The synthetic `demo` assumes mean reversion by construction, so it shows that the machinery works, not
that the strategy earns alpha. This is not investment advice.
