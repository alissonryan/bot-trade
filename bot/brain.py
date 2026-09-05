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
# The completion hit max_tokens before producing any content. Separated from
# REASON_EMPTY because it is a configuration problem with a specific fix, and
# because it degrades to a forced HOLD that otherwise looks like a real decision.
REASON_TRUNCATED = "llm_truncated"
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


def _first_json_object(raw: str) -> str | None:
    """The first brace-balanced ``{...}`` in ``raw``, ignoring braces inside strings.

    Models append things after the object (observed live: a valid object followed
    by a stray ``"}``). Slicing to the *last* ``}`` swallows that garbage and the
    whole decision is dropped as unparseable, which degrades to a forced HOLD.
    """
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    return None


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
        sliced = _first_json_object(raw)
        if sliced is None:
            return None
        try:
            data = json.loads(sliced)
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
        finish = None
        try:
            finish = payload["choices"][0].get("finish_reason")
        except Exception:  # noqa: BLE001
            pass
        if finish == "length":
            # A reasoning model spends max_tokens on its reasoning before any
            # content, so every cycle would degrade to a forced HOLD while still
            # being charged. Say so loudly instead of looking like a decision.
            log.error(
                "llm returned no content and stopped at max_tokens=%d; raise LLM_MAX_TOKENS "
                "or pick a non-reasoning model (every cycle is a forced HOLD and still costs)",
                settings.llm_max_tokens,
            )
            return ThinkResult(None, REASON_TRUNCATED, cost, source, status, model)
        log.warning("llm returned an empty completion")
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
