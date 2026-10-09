"""Upstox API v2 Order & Portfolio Execution Adapter."""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class UpstoxBrokerUnavailableError(RuntimeError):
    pass


class UpstoxStaleTokenError(RuntimeError):
    pass


class UpstoxAdapter:
    base_url = "https://api.upstox.com/v2"

    def __init__(self, api_key: str = "", access_token: str = "", api_secret: str = ""):
        self.api_key = api_key
        self.access_token = access_token
        self.api_secret = api_secret

    @property
    def configured(self) -> bool:
        return bool(self.access_token)

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": "TradeMind/1.0",
        }

    def _request(self, path: str, params: Optional[Dict] = None, data: Optional[Dict] = None, method: str = "GET") -> Any:
        if not self.configured:
            raise RuntimeError("Set UPSTOX_ACCESS_TOKEN before calling Upstox order APIs")
        url = f"{self.base_url}{path}"
        body = None
        if params and method == "GET":
            url += "?" + urlencode(params)
        if data is not None:
            body = json.dumps(data).encode("utf-8")

        request = Request(url, data=body, headers=self._headers(), method=method)
        try:
            with urlopen(request, timeout=30) as response:
                content = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise UpstoxStaleTokenError(f"Upstox session invalid or expired (HTTP {exc.code}); re-authentication required") from exc
            raise UpstoxBrokerUnavailableError(f"Upstox HTTP failure: {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise UpstoxBrokerUnavailableError("Upstox is unreachable; no order state was changed locally") from exc

        payload = json.loads(content)
        if isinstance(payload, dict):
            if payload.get("status") not in {"success", "ok"} and "data" not in payload:
                raise RuntimeError(payload.get("message", "Upstox request failed"))
            return payload.get("data", payload)
        return payload

    def login_url(self, client_id: str, redirect_uri: str, state: str = "") -> str:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
        }
        if state:
            params["state"] = state
        return f"https://api.upstox.com/v2/login/authorization/dialog?{urlencode(params)}"

    def create_session(self, code: str, client_id: str, client_secret: str, redirect_uri: str) -> Dict:
        """Exchange authorization code for an access token."""
        url = f"{self.base_url}/login/authorization/token"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        payload = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        body = urlencode(payload).encode("utf-8")
        req = Request(url, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=30) as response:
            res = json.loads(response.read().decode("utf-8"))
        if "access_token" not in res and not res.get("data", {}).get("access_token"):
            raise RuntimeError(res.get("message", "Upstox token exchange failed"))
        return res.get("data", res)

    def place_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        order_type: str = "LIMIT",
        exchange: str = "NSE",
        product: str = "CNC",
        price: float = 0,
        instrument_token: str = "",
    ) -> Dict:
        if order_type not in {"LIMIT", "SL"}:
            raise ValueError("Only price-protected LIMIT/SL orders are enabled")

        # Upstox product codes: D for delivery (CNC), I for intraday (MIS)
        prod = "I" if product.upper() in {"MIS", "INTRADAY", "I"} else "D"
        if instrument_token:
            instr_key = instrument_token
        elif "|" in symbol:
            instr_key = symbol
        elif exchange.upper() in {"NFO", "BFO"} or bool(re.search(r"\d+(CE|PE)$", symbol.upper())) or symbol.upper().endswith("FUT"):
            prefix = "BSE_FO" if exchange.upper() == "BFO" else "NSE_FO"
            instr_key = f"{prefix}|{symbol.upper()}"
        else:
            instr_key = f"{exchange.upper()}_EQ|{symbol.upper()}"

        payload = {
            "quantity": int(quantity),
            "product": prod,
            "validity": "DAY",
            "price": float(price),
            "tag": "TradeMind",
            "instrument_token": instr_key,
            "order_type": order_type.upper(),
            "transaction_type": action.upper(),
            "disclosed_quantity": 0,
            "trigger_price": float(price) if order_type == "SL" else 0.0,
            "is_amo": False,
        }
        res = self._request("/order/place", data=payload, method="POST")
        order_id = res.get("order_id") if isinstance(res, dict) else str(res)
        return {"order_id": order_id, "broker": "upstox", "raw": res}

    def orders(self) -> List[Dict]:
        res = self._request("/order/retrieve-all")
        if isinstance(res, list):
            return res
        return res.get("orders", []) if isinstance(res, dict) else []

    def positions(self) -> Dict:
        return self._request("/portfolio/short-term-positions")

    def cancel_order(self, order_id: str) -> Dict:
        return self._request(f"/order/cancel?order_id={order_id}", method="DELETE")

    def modify_order(self, order_id: str, quantity: int, price: float, order_type: str = "LIMIT") -> Dict:
        if order_type not in {"LIMIT", "SL"}:
            raise ValueError("Only LIMIT/SL modification is enabled")
        payload = {
            "order_id": order_id,
            "quantity": int(quantity),
            "price": float(price),
            "order_type": order_type.upper(),
            "validity": "DAY",
        }
        return self._request("/order/modify", data=payload, method="PUT")
