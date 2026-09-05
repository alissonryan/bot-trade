"""OpenRouter call and TradeIntent parsing.

Every failure has a name (``ThinkResult.reason``) so the audit can tell a dead API key
from a timeout from a model that answered garbage. The daily budget is charged with the
cost OpenRouter reports (``usage.cost``, requested with ``usage: {include: true}``) and
falls back to ``LLM_FALLBACK_COST_USD`` when the response carries no cost.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

import requests

from bot.settings import Settings
from bot.types import Snapshot, TradeIntent

log = logging.getLogger(__name__)

ACTIONS = {"BUY", "SELL", "HOLD"}
REGIMES = {"trend", "range", "shock", "unknown"}
FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)

SYSTEM = (
    "You are a BTC/USDT spot decision module. "
    "Reply with JSON only: action (BUY|SELL|HOLD), confidence (0-1), "
    "reason (<=240 chars), regime (trend|range|shock|unknown). "
    "Do not output quantity, stop, or price. HOLD if unsure."
)

REASON_OK = "ok"
REASON_BUDGET = "llm_budget"
REASON_CONFIG = "llm_config"
REASON_TIMEOUT = "llm_timeout"
REASON_NETWORK = "llm_network"
REASON_BAD_RESPONSE = "llm_bad_response"
REASON_EMPTY = "llm_empty"
REASON_PARSE = "llm_parse"


@dataclass
class Budget:
    spent_usd: float
    cap_usd: float
    day: str
    calls: int = 0

    def remaining(self) -> float:
        return self.cap_usd - self.spent_usd

    def spend(self, usd: float) -> None:
        self.spent_usd += max(0.0, float(usd))
        self.calls += 1

    def roll_day(self, day: str) -> None:
        if day != self.day:
            self.day = day
            self.spent_usd = 0.0
            self.calls = 0


@dataclass
class ThinkResult:
    intent: TradeIntent | None
    reason: str
    cost_usd: float = 0.0
    cost_source: str = "none"
    http_status: int | None = None
    model: str = ""
    raw: str | None = None

    def as_audit(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "cost_usd": round(self.cost_usd, 6),
            "cost_source": self.cost_source,
            "http_status": self.http_status,
            "model": self.model,
        }


def parse_intent(text: str | None) -> TradeIntent | None:
    if not isinstance(text, str) or not text.strip():
        return None
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


def request_body(snap: Snapshot, settings: Settings) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": settings.llm_model,
        "temperature": 0,
        "max_tokens": settings.llm_max_tokens,
        "usage": {"include": True},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": _user_payload(snap)},
        ],
    }
    if settings.llm_json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def _cost_from(payload: Any, settings: Settings) -> tuple[float, str]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if isinstance(usage, dict) and usage.get("cost") is not None:
        try:
            return float(usage["cost"]), "usage"
        except (TypeError, ValueError):
            pass
    return settings.llm_fallback_cost_usd, "fallback"


def think_result(
    snap: Snapshot,
    settings: Settings,
    budget: Budget,
    *,
    http_post: Callable[..., Any] | None = None,
) -> ThinkResult:
    model = settings.llm_model
    if budget.remaining() <= 0:
        return ThinkResult(None, REASON_BUDGET, model=model)
    if not settings.openrouter_api_key or not model:
        return ThinkResult(None, REASON_CONFIG, model=model)
    post = http_post or requests.post
    url = f"{settings.openrouter_base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = post(url, headers=headers, json=request_body(snap, settings), timeout=45)
    except requests.Timeout as exc:
        log.warning("llm timeout: %s", exc)
        return ThinkResult(None, REASON_TIMEOUT, model=model)
    except Exception as exc:  # noqa: BLE001 - network layer; named in the audit
        log.warning("llm network error: %s: %s", type(exc).__name__, exc)
        return ThinkResult(None, REASON_NETWORK, model=model)

    status = getattr(resp, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status is not None and status >= 400:
        log.warning("llm http %s", status)
        return ThinkResult(None, f"llm_http_{status}", http_status=status, model=model)
    try:
        payload = resp.json()
        text = payload["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        log.warning("llm bad response: %s: %s", type(exc).__name__, exc)
        return ThinkResult(None, REASON_BAD_RESPONSE, http_status=status, model=model)

    cost, source = _cost_from(payload, settings)
    budget.spend(cost)
    if not isinstance(text, str) or not text.strip():
        return ThinkResult(None, REASON_EMPTY, cost, source, status, model)
    intent = parse_intent(text)
    if intent is None:
        log.warning("llm parse failed: %r", text[:200])
        return ThinkResult(None, REASON_PARSE, cost, source, status, model, raw=text[:500])
    return ThinkResult(intent, REASON_OK, cost, source, status, model, raw=text[:500])


def think(
    snap: Snapshot,
    settings: Settings,
    budget: Budget,
    *,
    http_post: Callable[..., Any] | None = None,
) -> TradeIntent | None:
    """Compatibility wrapper: the intent only. Prefer think_result() to keep the reason."""
    return think_result(snap, settings, budget, http_post=http_post).intent
