from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


DEFAULT_WS_URL = "wss://wbs.kcex.com/ws?platform=web"


@dataclass(frozen=True)
class TickerEvent:
    last: float
    ts_ms: int
    symbol: str


@dataclass(frozen=True)
class DealEvent:
    price: float
    qty: float
    side: str
    ts_ms: int
    symbol: str


@dataclass(frozen=True)
class DepthEvent:
    bid: float | None
    ask: float | None
    symbol: str


def subscribe_message(symbol: str) -> dict[str, Any]:
    return {
        "method": "SUBSCRIPTION",
        "params": [
            f"spot@public.miniTicker@{symbol}@UTC+0",
            f"spot@public.aggre.deals@{symbol}",
            f"spot@public.limit.precision.depth@{symbol}@0.01",
        ],
        "id": 3,
    }


def ping_message() -> dict[str, str]:
    return {"method": "PING"}


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_frame(msg: dict[str, Any] | None) -> TickerEvent | DealEvent | DepthEvent | None:
    if not isinstance(msg, dict):
        return None
    if msg.get("msg") == "PONG":
        return None
    if "code" in msg and "c" not in msg:
        return None
    channel = str(msg.get("c") or "")
    data = msg.get("d") if isinstance(msg.get("d"), dict) else {}
    symbol = str(msg.get("s") or data.get("s") or "")
    if channel.startswith("spot@public.miniTicker@"):
        last = _f(data.get("p"))
        if last is None:
            return None
        ts = int(msg.get("t") or data.get("t") or 0)
        return TickerEvent(last=last, ts_ms=ts, symbol=symbol)
    if channel.startswith("spot@public.aggre.deals@"):
        deals = data.get("deals") or []
        if not deals:
            return None
        row = deals[0]
        price = _f(row.get("p"))
        qty = _f(row.get("q")) or 0.0
        if price is None:
            return None
        side = "buy" if int(row.get("T") or 0) == 1 else "sell"
        return DealEvent(
            price=price,
            qty=qty,
            side=side,
            ts_ms=int(row.get("t") or 0),
            symbol=symbol,
        )
    if "limit.precision.depth" in channel:
        bid = None
        ask = None
        bids = data.get("bids") or data.get("bestBids") or []
        asks = data.get("asks") or data.get("bestAsks") or []
        if bids and isinstance(bids[0], dict):
            bid = _f(bids[0].get("p"))
        if asks and isinstance(asks[0], dict):
            ask = _f(asks[0].get("p"))
        if bid is None and ask is None:
            return None
        return DepthEvent(bid=bid, ask=ask, symbol=symbol)
    return None


def parse_text(text: str) -> TickerEvent | DealEvent | DepthEvent | None:
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(msg, dict):
        return None
    return parse_frame(msg)
