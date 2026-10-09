"""Unit tests for UpstoxAdapter order placement and execution routing."""
import unittest
from unittest.mock import MagicMock, patch

from backend.upstox_adapter import UpstoxAdapter, UpstoxStaleTokenError
from backend.execution import get_broker_adapter


class TestUpstoxExecution(unittest.TestCase):
    def test_upstox_adapter_place_order(self):
        adapter = UpstoxAdapter(api_key="test_key", access_token="test_token")
        self.assertTrue(adapter.configured)

        with patch.object(adapter, "_request", return_value={"order_id": "UP-12345"}) as mock_req:
            res = adapter.place_order(
                symbol="RELIANCE",
                action="BUY",
                quantity=10,
                order_type="LIMIT",
                exchange="NSE",
                product="CNC",
                price=2950.0,
                instrument_token="NSE_EQ|INE002A01018",
            )
            self.assertEqual(res["order_id"], "UP-12345")
            self.assertEqual(res["broker"], "upstox")
            mock_req.assert_called_once()
            call_args = mock_req.call_args
            self.assertEqual(call_args[0][0], "/order/place")
            payload = call_args[1]["data"]
            self.assertEqual(payload["quantity"], 10)
            self.assertEqual(payload["product"], "D")
            self.assertEqual(payload["transaction_type"], "BUY")
            self.assertEqual(payload["order_type"], "LIMIT")
            self.assertEqual(payload["instrument_token"], "NSE_EQ|INE002A01018")

    def test_upstox_adapter_reconcile_orders(self):
        adapter = UpstoxAdapter(access_token="test_token")
        fake_orders = [
            {"order_id": "UP-1", "status": "complete", "filled_quantity": 10},
            {"order_id": "UP-2", "status": "open", "filled_quantity": 0},
        ]
        with patch.object(adapter, "_request", return_value={"orders": fake_orders}):
            orders = adapter.orders()
            self.assertEqual(len(orders), 2)
            self.assertEqual(orders[0]["order_id"], "UP-1")

    @patch("backend.execution.BROKER_ROUTING", "upstox")
    @patch("backend.execution.UPSTOX_ORDER_ACCESS_TOKEN", "valid_upstox_order_token")
    def test_get_broker_adapter_routes_to_upstox(self):
        adapter = get_broker_adapter(user_id=1)
        self.assertIsInstance(adapter, UpstoxAdapter)
        self.assertTrue(adapter.configured)

    def test_upstox_stream_auth_expired_raises(self):
        from backend.upstox_stream import UpstoxStream, UpstoxAuthExpiredError
        streamer = UpstoxStream(on_ticks=lambda ticks: None, access_token="expired_token")
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch("requests.get", return_value=mock_resp):
            with self.assertRaises(UpstoxAuthExpiredError):
                streamer.authorized_url()


if __name__ == "__main__":
    unittest.main()
