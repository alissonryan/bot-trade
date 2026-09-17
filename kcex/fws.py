"""Public KCEX perpetual-futures WebSocket (``wss://www.kcex.com/fapi/edge``).

Frame shapes were captured without authentication on 2026-09-16. Samples live in
``tests/fixtures/kcex_fut_ws_frames.jsonl`` and the notes in ``docs/kcex-futures-api.md``.
Nothing beyond those samples is assumed.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any

DEFAULT_FUT_WS_URL = "wss://www.kcex.com/fapi/edge"
CHANNELS = ("sub.ticker", "sub.deal", "sub.depth", "sub.fair.price")
PING_INTERVAL_S = 15.0
RECV_TIMEOUT_S = 1.0
# Inferred, not documented by the venue: every captured T=2 printed at the ask
# (aggressive buy) and every T=1 at the bid (aggressive sell).
DEAL_SIDE = {2: "buy", 1: "sell"}


@dataclass(frozen=True)
class FutTicker:
    ts_ms: int
    last: float
    bid: float
    ask: float
    fair: float
    index: float
    funding_rate: float


@dataclass(frozen=True)
class FutDeal:
    ts_ms: int
    price: float
    vol: int
    side: str


@dataclass(frozen=True)
class FutDepth:
    ts_ms: int
    version: int
    bids: tuple[tuple[float, int], ...]
    asks: tuple[tuple[float, int], ...]


@dataclass(frozen=True)
class FutFair:
    ts_ms: int
    price: float


FutEvent = FutTicker | FutDeal | FutDepth | FutFair


def subscribe_messages(symbol: str) -> list[dict[str, Any]]:
    return [{"method": method, "param": {"symbol": symbol}} for method in CHANNELS]


def ping_message() -> dict[str, str]:
    return {"method": "ping"}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_deal(data: Any) -> FutDeal | None:
    if not isinstance(data, dict):
        return None
    price, vol, ts = _num(data.get("p")), _num(data.get("v")), _num(data.get("t"))
    side = DEAL_SIDE.get(data.get("T"))
    if price is None or price <= 0 or vol is None or vol <= 0 or ts is None or side is None:
        return None
    return FutDeal(int(ts), price, int(vol), side)


def parse_levels(raw: Any) -> tuple[tuple[float, int], ...]:
    out: list[tuple[float, int]] = []
    for level in raw if isinstance(raw, list) else []:
        if not isinstance(level, list) or len(level) < 2:
            continue
        price, vol = _num(level[0]), _num(level[1])
        if price is None or price <= 0 or vol is None or vol < 0:
            continue
        out.append((price, int(vol)))
    return tuple(out)


def parse_frame(msg: Any) -> list[FutEvent]:
    if not isinstance(msg, dict):
        return []
    channel, data = msg.get("channel"), msg.get("data")
    ts = int(_num(msg.get("ts")) or 0)
    if channel == "push.ticker" and isinstance(data, dict):
        last = _num(data.get("lastPrice"))
        if last is None or last <= 0:
            return []
        return [FutTicker(
            ts_ms=int(_num(data.get("timestamp")) or ts),
            last=last,
            bid=_num(data.get("bid1")) or 0.0,
            ask=_num(data.get("ask1")) or 0.0,
            fair=_num(data.get("fairPrice")) or 0.0,
            index=_num(data.get("indexPrice")) or 0.0,
            funding_rate=_num(data.get("fundingRate")) or 0.0,
        )]
    if channel == "push.deal":
        items = data if isinstance(data, list) else [data]
        return [deal for deal in (parse_deal(item) for item in items) if deal is not None]
    if channel == "push.depth" and isinstance(data, dict):
        version = _num(data.get("version"))
        if version is None:
            return []
        return [FutDepth(ts, int(version), parse_levels(data.get("bids")), parse_levels(data.get("asks")))]
    if channel == "push.fair.price" and isinstance(data, dict):
        price = _num(data.get("price"))
        return [FutFair(ts, price)] if price is not None and price > 0 else []
    return []


def parse_text(text: str) -> list[FutEvent]:
    try:
        return parse_frame(json.loads(text))
    except json.JSONDecodeError:
        return []


class OrderBook:
    """REST snapshot plus contiguous WS deltas. Any version gap unsyncs the book."""

    def __init__(self) -> None:
        self.version: int | None = None
        self._bids: dict[float, int] = {}
        self._asks: dict[float, int] = {}

    @property
    def synced(self) -> bool:
        return self.version is not None

    def load_snapshot(self, version: int, bids, asks) -> None:
        self.version = int(version)
        self._bids = {price: vol for price, vol in bids if vol > 0}
        self._asks = {price: vol for price, vol in asks if vol > 0}

    def apply(self, delta: FutDepth) -> bool:
        if self.version is None:
            return False
        if delta.version <= self.version:
            return True
        if delta.version != self.version + 1:
            self.version = None
            self._bids.clear()
            self._asks.clear()
            return False
        for side, levels in ((self._bids, delta.bids), (self._asks, delta.asks)):
            for price, vol in levels:
                if vol <= 0:
                    side.pop(price, None)
                else:
                    side[price] = vol
        self.version = delta.version
        return True

    def best_bid(self) -> float | None:
        return max(self._bids) if self._bids else None

    def best_ask(self) -> float | None:
        return min(self._asks) if self._asks else None

    def levels(self, side: str) -> list[tuple[float, int]]:
        if side == "bid":
            return sorted(self._bids.items(), key=lambda level: -level[0])
        return sorted(self._asks.items(), key=lambda level: level[0])


class PublicFuturesWs:
    def __init__(self, url: str, symbol: str, connect):
        self.url = url
        self.symbol = symbol
        self._connect = connect

    def pump(self, *, on_event, on_error, max_messages: int | None = None) -> None:
        sock = self._connect(self.url)
        try:
            for message in subscribe_messages(self.symbol):
                sock.send(json.dumps(message))
            last_ping = time.monotonic()
            n = 0
            while max_messages is None or n < max_messages:
                now = time.monotonic()
                if now - last_ping >= PING_INTERVAL_S:
                    sock.send(json.dumps(ping_message()))
                    last_ping = now
                try:
                    raw = sock.recv(timeout=RECV_TIMEOUT_S)
                except TimeoutError:
                    continue
                except Exception as exc:
                    on_error(exc)
                    return
                n += 1
                if not isinstance(raw, str):
                    continue
                for event in parse_text(raw):
                    on_event(event)
        finally:
            try:
                sock.close()
            except Exception:
                pass


def default_connect(url: str):
    from websockets.sync.client import connect

    return connect(url, open_timeout=10)
