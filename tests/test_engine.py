import dataclasses
import unittest
from datetime import date, timedelta
from functools import lru_cache

from babylon_value.config import StrategyConfig
from babylon_value.costs import CostModel
from babylon_value.data.synthetic import generate_market
from babylon_value.engine import Rebalancer, RunStatus
from babylon_value.execution import PaperBroker
from babylon_value.journal import MemoryJournal
from babylon_value.orders import Side
from babylon_value.portfolio import Position

CONFIG = StrategyConfig().validate()
AS_OF = date(2013, 6, 30)


@lru_cache(maxsize=1)
def market():
    return generate_market(n_companies=200, start_year=2008, years=8, seed=3)


class Delegate:
    def __init__(self, inner):
        self.inner = inner

    def universe(self, as_of):
        return self.inner.universe(as_of)

    def snapshot(self, ticker, as_of):
        return self.inner.snapshot(ticker, as_of)

    def price(self, ticker, as_of):
        return self.inner.price(ticker, as_of)

    def dividends(self, ticker, start, end):
        return self.inner.dividends(ticker, start, end)


class LeakyProvider(Delegate):
    """Reveals next year's annual report before it was published."""

    def snapshot(self, ticker, as_of):
        snap = self.inner.snapshot(ticker, as_of)
        if snap is None or not snap.history:
            return snap
        future = dataclasses.replace(snap.history[0], fiscal_year_end=date(as_of.year, 12, 31),
                                     available_date=as_of + timedelta(days=30))
        return dataclasses.replace(snap, history=(future,) + snap.history)


class EmptyProvider(Delegate):
    def universe(self, as_of):
        return []


class ShrunkProvider(Delegate):
    def universe(self, as_of):
        return self.inner.universe(as_of)[:20]


def run(provider=None, cash=100_000.0, **kwargs):
    broker = PaperBroker(cash, CostModel(CONFIG.costs))
    rebalancer = Rebalancer(CONFIG, provider or market(), broker, **kwargs)
    return rebalancer, broker, rebalancer.run(AS_OF, 0.04)


class RebalanceTests(unittest.TestCase):
    def test_buys_only_eligible_securities_within_every_limit(self):
        _, broker, report = run()
        self.assertIs(report.status, RunStatus.COMPLETED, report.summary())
        buys = [f for f in report.fills if f.side is Side.BUY]
        self.assertGreater(len(buys), 0, report.summary())
        eligible = {c.ticker for c in report.eligible}
        self.assertTrue({f.ticker for f in buys} <= eligible)

        prices = {t: market().price(t, AS_OF).close for t in broker.positions()}
        nav = broker.cash() + sum(p.shares * prices[t] for t, p in broker.positions().items())
        self.assertGreaterEqual(broker.cash(), CONFIG.portfolio.cash_reserve * nav - 1.0)
        sectors = {}
        for ticker, position in broker.positions().items():
            value = position.shares * prices[ticker]
            self.assertLessEqual(value / nav, CONFIG.portfolio.max_position_weight + 1e-9)
            sectors[position.sector] = sectors.get(position.sector, 0.0) + value
        self.assertTrue(all(v / nav <= CONFIG.portfolio.max_sector_weight + 1e-9 for v in sectors.values()))

    def test_runs_are_deterministic(self):
        first, second = run()[2], run()[2]
        self.assertEqual([f.to_dict() for f in first.fills], [f.to_dict() for f in second.fills])
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_every_run_is_journaled_with_the_config_fingerprint(self):
        journal = MemoryJournal()
        run(journal=journal)
        self.assertEqual(journal.events[0]["config_fingerprint"], CONFIG.fingerprint())
        self.assertTrue(journal.events[0]["rejections"])


class HaltTests(unittest.TestCase):
    def assert_halted(self, report, text):
        self.assertIs(report.status, RunStatus.HALTED)
        self.assertIn(text, report.halt_reason)
        self.assertEqual(report.fills, [])

    def test_kill_switch(self):
        self.assert_halted(run(kill_switch=lambda: True)[2], "kill switch engaged")

    def test_unreadable_kill_switch_counts_as_engaged(self):
        def broken():
            raise OSError("permission denied")
        self.assert_halted(run(kill_switch=broken)[2], "could not be read")

    def test_implausible_risk_free_rate(self):
        broker = PaperBroker(100_000.0, CostModel(CONFIG.costs))
        report = Rebalancer(CONFIG, market(), broker).run(AS_OF, float("nan"))
        self.assert_halted(report, "risk-free")

    def test_look_ahead_bias(self):
        self.assert_halted(run(LeakyProvider(market()))[2], "LookAheadBiasError")

    def test_empty_universe(self):
        self.assert_halted(run(EmptyProvider(market()))[2], "universe is empty")

    def test_collapsing_universe(self):
        rebalancer, _, first = run()
        self.assertIs(first.status, RunStatus.COMPLETED)
        rebalancer.provider = ShrunkProvider(market())
        self.assert_halted(rebalancer.run(AS_OF, 0.04), "shrank")

    def test_unexpected_defect_is_journaled_then_raised(self):
        class Broken(Delegate):
            def snapshot(self, ticker, as_of):
                raise RuntimeError("feed parser crashed")
        journal = MemoryJournal()
        with self.assertRaises(RuntimeError):
            run(Broken(market()), journal=journal)
        self.assertEqual(journal.events[0]["status"], "HALTED")
        self.assertIn("feed parser crashed", journal.events[0]["halt_reason"])

    def test_holding_that_cannot_be_priced(self):
        broker = PaperBroker(100_000.0, CostModel(CONFIG.costs))
        broker._positions["GHOST"] = Position("GHOST", "Industrials", 10, 100.0, AS_OF)
        report = Rebalancer(CONFIG, market(), broker).run(AS_OF, 0.04)
        self.assert_halted(report, "cannot be valued")


if __name__ == "__main__":
    unittest.main()
