"""KCEX web spot API client, reverse-engineered from www.kcex.com.

Transport rules that matter for money:

* GET requests retry on 429 / 5xx / network errors with a short backoff.
* POST and DELETE never retry automatically: a retried order is a duplicate order.
* 406 means the WAF rejected the User-Agent (curl's default UA gets 406, a browser UA
  and python-requests pass as of 2026-09-04). It is reported as a KcexError with a hint.
* 401 means the web session token is missing or dead. Run ``python -m kcex.cli login``.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable
from urllib.parse import urlencode

import requests

DEFAULT_BASE = "https://www.kcex.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
RETRY_STATUS = {429, 500, 502, 503, 504}
GET_RETRIES = 3
RETRY_BACKOFF_S = (0.5, 1.0, 2.0)

ORDER_TYPE = {
    "LIMIT_ORDER": "1",
    "POST_ONLY": "2",
    "IOC": "3",
    "FOK": "4",
    "MARKET_ORDER": "5",
    "STOP_LIMIT": "100",
    "STOP_MARKET": "103",
}

TRADE_TYPE_BUY = "BUY"
TRADE_TYPE_SELL = "SELL"


class KcexError(RuntimeError):
    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}

    @property
    def status(self) -> int | None:
        value = self.payload.get("status")
        return int(value) if value is not None else None


class KcexClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
        user_device: str | None = None,
        language: str = "en-US",
        timeout: float = 20.0,
        session: requests.Session | None = None,
        user_agent: str | None = None,
        retries: int = GET_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = (base_url or os.getenv("KCEX_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.token = token if token is not None else os.getenv("KCEX_TOKEN", "")
        self.user_device = user_device if user_device is not None else os.getenv("KCEX_USER_DEVICE", "")
        self.language = language or os.getenv("KCEX_LANGUAGE", "en-US")
        self.user_agent = user_agent or os.getenv("KCEX_USER_AGENT") or DEFAULT_USER_AGENT
        self.timeout = timeout
        self.session = session or requests.Session()
        self.retries = max(1, retries)
        self._sleep = sleep

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "user-agent": self.user_agent,
            "language": self.language,
            "accept-language": self.language,
            "platform": os.getenv("KCEX_PLATFORM", "WEB"),
            "version": "1.0.0",
            "origin": self.base_url,
            "referer": f"{self.base_url}/exchange/BTC_USDT",
        }
        if self.token:
            headers["authorization"] = self.token
        if self.user_device:
            headers["user-device"] = self.user_device
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None) -> Any:
        method = method.upper()
        # Writes are never retried: a duplicated POST is a duplicated order.
        attempts = self.retries if method == "GET" else 1
        for attempt in range(attempts):
            last = attempt + 1 >= attempts
            try:
                response = self.session.request(
                    method,
                    self._url(path),
                    params=params,
                    json=json,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if last:
                    raise KcexError(f"{method} {path} network error: {exc}", {"status": None}) from exc
                self._sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
                continue

            status = int(getattr(response, "status_code", 0) or 0)
            if status == 406:
                raise KcexError(
                    f"{method} {path} blocked by WAF (406): set KCEX_USER_AGENT to a browser User-Agent",
                    {"status": 406},
                )
            if status == 401:
                raise KcexError(
                    f"{method} {path} unauthorized (401): session token missing or expired; "
                    "run: python -m kcex.cli login",
                    {"status": 401},
                )
            if status in RETRY_STATUS and not last:
                self._sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
                continue
            if status >= 400:
                raise KcexError(f"{method} {path} failed: HTTP {status}", {"status": status})

            payload = response.json()
            if not isinstance(payload, dict):
                return payload
            code = payload.get("code")
            if code not in (0, 200, "0", "200", None) and payload.get("success") is not True:
                raise KcexError(f"{method} {path} failed: {payload.get('msg') or payload}", payload)
            return payload
        raise KcexError(f"{method} {path} failed after {attempts} attempts", {"status": None})

    def get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        return self.request("GET", path, params=clean or None)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self.request("POST", path, json=body)

    def delete(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        return self.request("DELETE", path, params=clean or None)

    def ping(self) -> Any:
        return self.get("/spot/api/common/ping")

    def ticker(self, symbol: str = "BTC_USDT") -> Any:
        return self.get("/spot/api/market-2/spot/market/v2/web/symbol/ticker", symbol=symbol)

    def symbol_trade_rules(self, symbol: str = "BTC_USDT") -> Any:
        return self.get("/spot/api/market-2/spot/market/v2/web/symbol/trade", symbol=symbol)

    def depth(self, symbol: str = "BTC_USDT", price_precision: str = "0.01") -> Any:
        return self.get(
            "/spot/api/spot/market/depth",
            symbol=symbol,
            pricePrecision=price_precision,
        )

    def public_deals(self, symbol: str = "BTC_USDT") -> Any:
        return self.get("/spot/api/spot/market/deals", symbol=symbol)

    def kline(
        self,
        symbol: str = "BTC_USDT",
        *,
        interval: str = "Min15",
        start: int,
        end: int,
        open_price_mode: str = "LAST_CLOSE",
    ) -> Any:
        return self.get(
            "/spot/api/spot/market/kline",
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            openPriceMode=open_price_mode,
        )

    def user_info(self) -> Any:
        return self.get("/uc/user_api/user_info")

    def ws_token(self) -> Any:
        return self.get("/uc/user_api/ws_token")

    def balances(self, currencies: str = "BTC,USDT") -> Any:
        return self.get("/spot/api/spot/asset/currency/balances", currency=currencies)

    def open_orders(
        self,
        *,
        page_num: int = 1,
        page_size: int = 100,
        order_types: str = "1,2,3,4,5,100,101,102,103,20",
        states: str = "0,1,3",
    ) -> Any:
        return self.get(
            "/spot/api/spot/order/current/orders/v2",
            orderTypes=order_types,
            pageNum=page_num,
            pageSize=page_size,
            states=states,
        )

    def order_history(
        self,
        *,
        page_num: int = 1,
        page_size: int = 100,
        **params: Any,
    ) -> Any:
        return self.get(
            "/spot/api/spot/order/history/orders/v2",
            pageNum=page_num,
            pageSize=page_size,
            **params,
        )

    def my_deals(
        self,
        currency: str = "BTC",
        market: str = "USDT",
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        page_num: int = 1,
        page_size: int = 1000,
    ) -> Any:
        return self.get(
            "/spot/api/spot/deal/deals",
            currency=currency,
            market=market,
            startTime=start_time,
            endTime=end_time,
            needPage="false",
            pageNum=page_num,
            pageSize=page_size,
        )

    def place_limit(
        self,
        *,
        currency: str,
        market: str,
        side: str,
        price: str,
        quantity: str,
        order_type: str = "LIMIT_ORDER",
        extra: dict[str, Any] | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "currency": currency,
            "market": market,
            "tradeType": side.upper(),
            "price": str(price),
            "quantity": str(quantity),
            "orderType": order_type,
        }
        if extra:
            body.update(extra)
        return self.post("/spot/api/spot/order/place", body)

    def place_market(
        self,
        *,
        currency: str,
        market: str,
        side: str,
        price: str,
        quantity: str | None = None,
        amount: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "currency": currency,
            "market": market,
            "tradeType": side.upper(),
            "price": str(price),
            "orderType": "MARKET_ORDER",
        }
        if quantity is not None:
            body["quantity"] = str(quantity)
        if amount is not None:
            body["amount"] = str(amount)
        if extra:
            body.update(extra)
        return self.post("/spot/api/spot/v4/order/place", body)

    def place_trigger(
        self,
        *,
        currency: str,
        market: str,
        side: str,
        trigger_price: str,
        trigger_type: str,
        quantity: str,
        amount: str,
        price: str | None = None,
        market_order: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "currency": currency,
            "market": market,
            "tradeType": side.upper(),
            "triggerType": trigger_type,
            "triggerPrice": str(trigger_price),
            "quantity": str(quantity),
            "amount": str(amount),
            "orderType": 103 if market_order else 100,
        }
        if not market_order:
            if price is None:
                raise ValueError("limit trigger orders require price")
            body["price"] = str(price)
        if extra:
            body.update(extra)
        return self.post("/spot/api/spot/order/place/trigger/v2", body)

    def cancel_order(self, order_id: str) -> Any:
        qs = urlencode({"orderId": order_id})
        return self.request("DELETE", f"/spot/api/spot/order/cancel/v2?{qs}")
