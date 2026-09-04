# bot/types.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass
class Snapshot:
    ts_ms: int
    last: float
    bid: float
    ask: float
    spread: float
    bars_15m: list[Bar]
    atr: float | None
    free_usdt: float
    bot_qty: float
    bot_avg_entry: float | None
    ws_ok: bool
    stale: bool
    last_intent_action: str | None = None
    last_bot_pnl_usdt: float = 0.0


@dataclass
class TradeIntent:
    action: str
    confidence: float
    reason: str
    regime: str


@dataclass
class GateResult:
    ok: bool
    rule: str
    action: str
    qty: str | None = None
    notional: float | None = None
    stop_price: str | None = None
