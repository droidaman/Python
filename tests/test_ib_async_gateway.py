"""Exercises IbAsyncGateway against the real ib_async classes, with only the
network connection replaced. Skipped when ib_async is not installed."""

import importlib.util
import unittest
from datetime import date, datetime
from unittest import mock

HAVE_IB_ASYNC = importlib.util.find_spec("ib_async") is not None


class FakeIB:
    def __init__(self):
        self.connected_with = None
        self.placed = []
        self.cancelled = []

    def connect(self, host, port, clientId, timeout):
        self.connected_with = (host, port, clientId, timeout)

    def disconnect(self):
        self.connected_with = None

    def managedAccounts(self):
        return ["DU1234567"]

    def accountSummary(self, account):
        import ib_async
        return [ib_async.AccountValue(account, "TotalCashValue", "100000.5", "USD", "")]

    def positions(self, account):
        import ib_async
        return [ib_async.Position(account, ib_async.Stock("GOOD", "SMART", "USD"), 100.0, 10.5)]

    def reqAllOpenOrders(self):
        return self.placed

    def reqExecutions(self, execution_filter):
        import ib_async
        execution = ib_async.Execution(side="BOT", shares=100.0, price=10.1, orderRef="BV-ref")
        report = ib_async.CommissionReport(commission=0.35)
        return [ib_async.Fill(ib_async.Stock("GOOD", "SMART", "USD"), execution, report, datetime(2025, 6, 24))]

    def qualifyContracts(self, contract):
        return [contract]

    def placeOrder(self, contract, order):
        import ib_async
        trade = ib_async.Trade(contract, order, ib_async.OrderStatus(status="Submitted"))
        self.placed.append(trade)
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def reqHistoricalData(self, contract, **kwargs):
        import ib_async
        self.history_request = kwargs
        return [ib_async.BarData(date=date(2025, 6, 23), close=10.0, volume=12345.0)]

    def sleep(self, seconds):
        pass


@unittest.skipUnless(HAVE_IB_ASYNC, "ib_async is not installed")
class IbAsyncGatewayTests(unittest.TestCase):
    def setUp(self):
        import ib_async
        from babylon_value.ibkr import IbAsyncGateway
        self.fake = FakeIB()
        with mock.patch.object(ib_async, "IB", return_value=self.fake):
            self.gateway = IbAsyncGateway("127.0.0.1", 4002, 17)

    def test_connects_and_reads_the_account(self):
        self.assertEqual(self.fake.connected_with, ("127.0.0.1", 4002, 17, 10.0))
        self.assertEqual(self.gateway.managed_accounts(), ["DU1234567"])
        self.assertIn(("TotalCashValue", "100000.5", "USD"), self.gateway.account_values("DU1234567"))
        position = self.gateway.positions("DU1234567")[0]
        self.assertEqual((position.symbol, position.sec_type, position.quantity), ("GOOD", "STK", 100.0))

    def test_orders_are_day_limit_orders_with_a_reference(self):
        trade = self.gateway.place_limit_order("DU1234567", "GOOD", "BUY", 100, 10.1, "BV-ref")
        order = trade.order
        self.assertEqual((order.action, order.totalQuantity, order.lmtPrice, order.orderType),
                         ("BUY", 100, 10.1, "LMT"))
        self.assertEqual((order.tif, order.orderRef, order.account, order.outsideRth), ("DAY", "BV-ref", "DU1234567", False))
        self.assertEqual(self.gateway.order_state(trade).status, "Submitted")
        self.assertEqual([o.symbol for o in self.gateway.open_orders("DU1234567")], ["GOOD"])
        self.gateway.cancel_order(trade)
        self.assertEqual(self.fake.cancelled, [order])

    def test_executions_and_bars(self):
        execution = self.gateway.executions("DU1234567")[0]
        self.assertEqual((execution.order_ref, execution.side, execution.shares, execution.commission),
                         ("BV-ref", "BUY", 100.0, 0.35))
        self.assertEqual(self.gateway.daily_bars("GOOD", "2 Y"), [(date(2025, 6, 23), 10.0, 12345.0)])
        self.assertEqual(self.fake.history_request["whatToShow"], "TRADES")
        self.assertEqual(self.fake.history_request["barSizeSetting"], "1 day")


if __name__ == "__main__":
    unittest.main()
