"""Read-only Zerodha Kite market-data adapter.

Credentials are read by callers from environment variables and are never
persisted. Live order execution remains deliberately unavailable.
"""
import csv
import hashlib
import io
import json
from datetime import date
from typing import Dict, List
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class BrokerUnavailableError(RuntimeError):
    pass


class StaleBrokerTokenError(RuntimeError):
    pass


class ZerodhaAdapter:
    base_url = "https://api.kite.trade"

    def __init__(self, api_key: str, access_token: str = "", api_secret: str = ""):
        self.api_key = api_key
        self.access_token = access_token
        self.api_secret = api_secret

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.access_token)

    def _request(self, path: str, params: Dict = None, csv_response: bool = False, method: str = "GET"):
        if not self.configured:
            raise RuntimeError("Set KITE_API_KEY and KITE_ACCESS_TOKEN before syncing market data")
        url = f"{self.base_url}{path}"
        body = None
        if params and method == "GET":
            url += "?" + urlencode(params)
        elif params:
            body = urlencode(params).encode()
        request = Request(url, data=body, headers={"X-Kite-Version": "3", "Authorization": f"token {self.api_key}:{self.access_token}", "User-Agent": "NiveshAI/1.0", "Content-Type":"application/x-www-form-urlencoded"}, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise StaleBrokerTokenError("Kite session is invalid or expired; interactive login is required") from exc
            raise BrokerUnavailableError(f"Kite HTTP failure: {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise BrokerUnavailableError("Kite is unreachable; no order state was changed locally") from exc
        if csv_response:
            return list(csv.DictReader(io.StringIO(body)))
        payload = json.loads(body)
        if payload.get("status") != "success":
            raise RuntimeError(payload.get("message", "Kite request failed"))
        return payload["data"]

    def login_url(self) -> str:
        if not self.api_key: raise RuntimeError("Set KITE_API_KEY first")
        return f"https://kite.zerodha.com/connect/login?v=3&api_key={self.api_key}"

    def create_session(self, request_token: str) -> Dict:
        if not self.api_key or not self.api_secret: raise RuntimeError("KITE_API_KEY and KITE_API_SECRET are required")
        checksum=hashlib.sha256(f"{self.api_key}{request_token}{self.api_secret}".encode()).hexdigest()
        request=Request(f"{self.base_url}/session/token",data=urlencode({"api_key":self.api_key,"request_token":request_token,"checksum":checksum}).encode(),headers={"X-Kite-Version":"3","Content-Type":"application/x-www-form-urlencoded","User-Agent":"NiveshAI/1.0"},method="POST")
        with urlopen(request,timeout=30) as response: payload=json.loads(response.read().decode())
        if payload.get("status")!="success": raise RuntimeError(payload.get("message","Kite session exchange failed"))
        return payload["data"]

    def instruments(self) -> List[Dict]:
        return self._request("/instruments", csv_response=True)

    def historical(self, instrument_token: int, interval: str, start: date, end: date) -> List[Dict]:
        data = self._request(f"/instruments/historical/{instrument_token}/{interval}", {"from": start.isoformat(), "to": end.isoformat(), "continuous": 0, "oi": 0})
        return [{"timestamp": row[0], "open": row[1], "high": row[2], "low": row[3], "close": row[4], "volume": row[5], "oi": row[6] if len(row) > 6 else 0} for row in data.get("candles", [])]

    def quotes(self, instruments: List[str]) -> Dict:
        result = {}
        for start in range(0, len(instruments), 500):
            batch = instruments[start:start + 500]
            query = [("i", item) for item in batch]
            if not self.configured:
                raise RuntimeError("Zerodha market-data access is not configured")
            url = f"{self.base_url}/quote?{urlencode(query)}"
            request = Request(url, headers={"X-Kite-Version": "3", "Authorization": f"token {self.api_key}:{self.access_token}", "User-Agent": "NiveshAI/1.0"})
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            result.update(payload.get("data", {}))
        return result

    def place_order(self, symbol: str, action: str, quantity: int, order_type: str = "LIMIT", exchange: str = "NSE", product: str = "CNC", price: float = 0) -> Dict:
        if order_type not in {"LIMIT","SL"}: raise ValueError("Only price-protected LIMIT/SL orders are enabled")
        params={"tradingsymbol":symbol,"exchange":exchange,"transaction_type":action,"order_type":order_type,"quantity":int(quantity),"product":product,"validity":"DAY","price":float(price),"tag":"NIVESH_AI"}
        return self._request("/orders/regular",params,method="POST")

    def orders(self) -> List[Dict]:
        return self._request("/orders")

    def positions(self) -> Dict:
        return self._request("/portfolio/positions")

    def margins(self, segment: str = "equity") -> Dict:
        return self._request(f"/user/margins/{segment}")

    def cancel_order(self, order_id: str, variety: str = "regular") -> Dict:
        return self._request(f"/orders/{variety}/{order_id}",method="DELETE")

    def modify_order(self, order_id: str, quantity: int, price: float, order_type: str = "LIMIT", variety: str = "regular") -> Dict:
        if order_type not in {"LIMIT","SL"}: raise ValueError("Only LIMIT/SL modification is enabled")
        return self._request(f"/orders/{variety}/{order_id}",{"quantity":int(quantity),"price":float(price),"order_type":order_type,"validity":"DAY"},method="PUT")
