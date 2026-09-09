"""Future integration boundary for Zerodha Kite Connect.

No credentials or API calls are included. Implement this interface only after
creating a Kite Connect app and reviewing exchange/broker compliance rules.
"""
from typing import Dict, List


class ZerodhaAdapter:
    def __init__(self, api_key: str, access_token: str):
        self.api_key = api_key
        self.access_token = access_token

    def quotes(self, instruments: List[str]) -> Dict:
        raise NotImplementedError("Zerodha market-data access is intentionally not configured")

    def place_order(self, symbol: str, action: str, quantity: int, order_type: str = "MARKET") -> Dict:
        raise NotImplementedError("Live order execution is intentionally disabled")
