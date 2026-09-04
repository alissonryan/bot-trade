from __future__ import annotations

from bot.hub import Hub
from kcex.ws import DealEvent


def encode_tick(hub: Hub) -> dict:
    return {
        "type": "tick",
        "symbol": hub.symbol or "BTC_USDT",
        "last": hub.last,
        "bid": hub.bid,
        "ask": hub.ask,
        "ts_ms": hub.ts_ms,
    }


def encode_deal(event: DealEvent) -> dict:
    return {
        "type": "deal",
        "symbol": event.symbol,
        "price": event.price,
        "qty": event.qty,
        "side": event.side,
        "ts_ms": event.ts_ms,
    }
