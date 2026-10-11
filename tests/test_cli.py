import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from babylon_value import cli
from babylon_value.data.csv_io import export_csv_directory
from babylon_value.data.synthetic import generate_market
from babylon_value.ibkr import NEW_YORK


def run(*argv):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(list(argv))
    return code, output.getvalue()


class PaperTradeRefusalTests(unittest.TestCase):
    """Every refusal happens before any connection to Interactive Brokers."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.data = Path(self.dir.name)
        export_csv_directory(generate_market(n_companies=5, start_year=2015, years=6, seed=1), self.data)
        connect = mock.patch.object(cli, "_gateway", side_effect=AssertionError("must not connect"))
        connect.start()
        self.addCleanup(connect.stop)

    def mark_live(self, built):
        (self.data / "dataset.json").write_text(json.dumps({"purpose": "live", "built": built}))

    def paper_trade(self, today, *extra):
        with mock.patch("babylon_value.ibkr.new_york_now", return_value=datetime(*today, 11, 0, tzinfo=NEW_YORK)):
            return run("paper-trade", "--data", str(self.data), "--risk-free", "0.04", *extra)

    def test_research_datasets_are_refused(self):
        code, out = self.paper_trade((2025, 6, 24))
        self.assertEqual(code, 1)
        self.assertIn("not a live dataset", out)

    def test_stale_datasets_are_refused(self):
        self.mark_live("2025-06-20")
        code, out = self.paper_trade((2025, 6, 24))
        self.assertEqual(code, 1)
        self.assertIn("rebuild", out)

    def test_trading_outside_the_rebalance_calendar_is_refused(self):
        self.mark_live("2025-10-14")
        code, out = self.paper_trade((2025, 10, 14))
        self.assertEqual(code, 1)
        self.assertIn("fixed calendar", out)

    def test_backtest_refuses_live_datasets(self):
        self.mark_live("2025-06-24")
        code, out = run("backtest", "--data", str(self.data), "--start", "2021-06-01", "--end", "2021-12-31")
        self.assertEqual(code, 1)
        self.assertIn("live trading only", out)

    def test_build_requires_an_sec_user_agent(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            code, out = run("build-dataset", "--sec-user-agent", "", "--out", str(self.data))
        self.assertEqual(code, 1)
        self.assertIn("User-Agent", out)


if __name__ == "__main__":
    unittest.main()
