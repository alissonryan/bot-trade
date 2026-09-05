# bot/types.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass(frozen=True)
class SymbolRules:
    """Trading rules for one symbol, read once from ``symbol_trade_rules``.

    Keys observed on 2026-09-04 for BTC_USDT: ``ps`` price scale, ``qs`` quantity scale,
    ``tfr``/``mfr`` taker/maker fee rates, ``mi``/``ma`` market-order min/max amount in
    USDT and ``li``/``la`` the same for limit orders (semantics inferred from the values,
    not documented by the venue).
    """

    price_scale: int = 2
    qty_scale: int = 5
    min_amount: float = 0.0
    max_amount: float | None = None
    taker_fee: float = 0.0
    maker_fee: float = 0.0

    @classmethod
    def from_trade_rules(cls, payload: Any) -> "SymbolRules":
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}

        def _num(key: str, default: float | None) -> float | None:
            value = data.get(key)
            if value is None or value == "":
                return default
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        return cls(
            price_scale=int(_num("ps", 2) or 2),
            qty_scale=int(_num("qs", 5) or 5),
            min_amount=float(_num("mi", 0.0) or 0.0),
            max_amount=_num("ma", None),
            taker_fee=float(_num("tfr", 0.0) or 0.0),
            maker_fee=float(_num("mfr", 0.0) or 0.0),
        )


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

    def compact(self) -> dict[str, Any]:
        """The fields worth keeping in the audit row to re-read a decision later."""
        return {
            "ts_ms": self.ts_ms,
            "last": self.last,
            "bid": self.bid,
            "ask": self.ask,
            "spread": self.spread,
            "atr": self.atr,
            "free_usdt": self.free_usdt,
            "bot_qty": self.bot_qty,
            "bot_avg_entry": self.bot_avg_entry,
            "ws_ok": self.ws_ok,
            "stale": self.stale,
            "bars": len(self.bars_15m),
        }


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
