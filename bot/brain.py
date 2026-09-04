from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

import requests

from bot.settings import Settings
from bot.types import Snapshot, TradeIntent

ACTIONS = {"BUY", "SELL", "HOLD"}
REGIMES = {"trend", "range", "shock", "unknown"}
FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)

SYSTEM = (
    "You are a BTC/USDT spot decision module. "
    "Reply with JSON only: action (BUY|SELL|HOLD), confidence (0-1), "
    "reason (<=240 chars), regime (trend|range|shock|unknown). "
    "Do not output quantity, stop, or price. HOLD if unsure."
)


@dataclass
class Budget:
    spent_usd: float
    cap_usd: float
    day: str

    def remaining(self) -> float:
        return self.cap_usd - self.spent_usd

    def spend(self, usd: float) -> None:
        self.spent_usd += usd

    def roll_day(self, day: str) -> None:
        if day != self.day:
            self.day = day
            self.spent_usd = 0.0


def parse_intent(text: str) -> TradeIntent | None:
    raw = text.strip()
    m = FENCE.search(raw)
    if m:
        raw = m.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action", "")).upper()
    if action not in ACTIONS:
        return None
    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    conf = min(max(conf, 0.0), 1.0)
    reason = str(data.get("reason", ""))[:240]
    regime = str(data.get("regime", "unknown")).lower()
    if regime not in REGIMES:
        regime = "unknown"
    return TradeIntent(action, conf, reason, regime)


def _user_payload(snap: Snapshot) -> str:
    last_bars = [
        {"t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c}
        for b in snap.bars_15m[-20:]
    ]
    return json.dumps(
        {
            "last": snap.last,
            "bid": snap.bid,
            "ask": snap.ask,
            "spread": snap.spread,
            "atr": snap.atr,
            "free_usdt": snap.free_usdt,
            "bot_qty": snap.bot_qty,
            "bot_avg_entry": snap.bot_avg_entry,
            "last_intent": snap.last_intent_action,
            "last_bot_pnl_usdt": snap.last_bot_pnl_usdt,
            "bars_15m": last_bars,
        },
        separators=(",", ":"),
    )


def think(
    snap: Snapshot,
    settings: Settings,
    budget: Budget,
    *,
    http_post: Callable[..., Any] | None = None,
) -> TradeIntent | None:
    if budget.remaining() <= 0:
        return None
    if not settings.openrouter_api_key or not settings.llm_model:
        return None
    post = http_post or requests.post
    url = f"{settings.openrouter_base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.llm_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": _user_payload(snap)},
        ],
    }
    try:
        resp = post(url, headers=headers, json=body, timeout=45)
        resp.raise_for_status()
        payload = resp.json()
        text = payload["choices"][0]["message"]["content"]
    except Exception:
        return None
    budget.spend(0.02)
    return parse_intent(text)
