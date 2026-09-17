"""The LLM has the final word on LONG/SHORT/CLOSE/HOLD. Code owns size, stop and limits.

Budget handling mirrors bot.brain.think_result: reserve durably BEFORE the HTTP dispatch,
settle to the real cost after, keep the reservation as the charge when the outcome is unknown.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from bot.brain import (
    FENCE,
    REASON_BAD_RESPONSE,
    REASON_BUDGET,
    REASON_BUDGET_STATE,
    REASON_CONFIG,
    REASON_EMPTY,
    REASON_INVALID_RESERVE,
    REASON_NETWORK,
    REASON_OK,
    REASON_PARSE,
    REASON_SETTLEMENT_FAILED,
    REASON_TIMEOUT,
    REASON_TRUNCATED,
    Budget,
    _cost_from,
    _cost_from_http_error,
    _first_json_object,
)
from bot.settings import Settings
from fut.settings import FutSettings
from fut.types import FutIntent

log = logging.getLogger(__name__)

ACTIONS = {"LONG", "SHORT", "CLOSE", "HOLD"}
REASON_INVALID_ACTION = "llm_invalid_action"
SYSTEM = (
    "You are a BTC_USDT perpetual futures decision module on a seconds horizon. "
    "Reply with JSON only: action (LONG|SHORT|CLOSE|HOLD), confidence (0-1), reason (<=240 chars). "
    "LONG or SHORT only when position is flat; CLOSE only with an open position. "
    "Do not output size, stop or price. A round trip costs about 2 bps plus slippage. HOLD if unsure."
)


@dataclass
class LlmDecision:
    intent: FutIntent | None
    reason: str
    cost_usd: float = 0.0
    cost_source: str = "none"
    http_status: int | None = None
    model: str = ""
    raw: str | None = None
    request: dict[str, Any] | None = None
    latency_ms: int = 0

    def as_audit(self) -> dict[str, Any]:
        return {"reason": self.reason, "cost_usd": round(self.cost_usd, 8), "cost_source": self.cost_source,
                "http_status": self.http_status, "model": self.model, "raw": self.raw,
                "request": self.request, "latency_ms": self.latency_ms}


def request_body(state: dict[str, Any], llm: Settings, *, reasoning: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": llm.llm_model,
        "temperature": 0,
        "max_tokens": llm.llm_max_tokens,
        "usage": {"include": True},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(state, separators=(",", ":"), default=str)},
        ],
    }
    if not reasoning:
        body["reasoning"] = {"enabled": False}
    if llm.llm_json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def parse_action(text: str | None) -> FutIntent | None:
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    fenced = FENCE.search(raw)
    if fenced:
        raw = fenced.group(1)
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
    action = str(data.get("action", "")).strip().upper()
    if action not in ACTIONS:
        return None
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence):
        return None
    return FutIntent(action, min(max(confidence, 0.0), 1.0), str(data.get("reason", ""))[:240])


def valid_for_position(intent: FutIntent, has_position: bool) -> bool:
    if intent.action in ("LONG", "SHORT"):
        return not has_position
    if intent.action == "CLOSE":
        return has_position
    return True


def decide(state: dict[str, Any], *, has_position: bool, settings: FutSettings, budget: Budget, store,
           http_post: Callable[..., Any] | None = None, clock: Callable[[], float] = time.monotonic) -> LlmDecision:
    llm = settings.llm
    model = llm.llm_model
    started = clock()

    def done(intent, reason, cost=0.0, source="none", status=None, raw=None, request=None) -> LlmDecision:
        return LlmDecision(intent, reason, cost, source, status, model, raw, request,
                           int((clock() - started) * 1000))

    if budget.blocked_reason:
        return done(None, budget.blocked_reason)
    if not budget.is_valid():
        return done(None, REASON_BUDGET_STATE)
    if budget.remaining() <= 0:
        return done(None, REASON_BUDGET)
    if not llm.openrouter_api_key or not model:
        return done(None, REASON_CONFIG)
    reserve = llm.llm_fallback_cost_usd
    if not math.isfinite(reserve) or reserve <= 0:
        return done(None, REASON_INVALID_RESERVE)

    reserved_durably = False
    if store is not None:
        try:
            admitted = store.reserve_budget(today=budget.day, cap_usd=budget.cap_usd, reserve_usd=reserve)
        except Exception as exc:  # noqa: BLE001 - untrustworthy budget state blocks spend
            log.error("budget state untrustworthy, refusing LLM spend: %s", exc)
            return done(None, REASON_BUDGET_STATE)
        if not admitted:
            return done(None, REASON_BUDGET)
        reserved_durably = True
    elif reserve > budget.remaining() + 1e-9:
        return done(None, REASON_BUDGET)
    budget.spend(reserve)

    def settle(actual: float) -> None:
        delta = actual - reserve
        budget.settle(delta)
        if reserved_durably:
            try:
                store.settle_budget(day=budget.day, delta_usd=delta)
            except Exception as exc:  # noqa: BLE001 - ledger may understate spend; block further calls
                log.error("budget settlement failed; blocking further LLM spend: %s", exc)
                budget.blocked_reason = REASON_SETTLEMENT_FAILED

    body = request_body(state, llm, reasoning=settings.llm_reasoning)
    post = http_post or requests.post
    headers = {"Authorization": f"Bearer {llm.openrouter_api_key}", "Content-Type": "application/json"}
    try:
        resp = post(f"{llm.openrouter_base_url}/chat/completions", headers=headers, json=body,
                    timeout=settings.llm_timeout_s)
    except requests.Timeout:
        return done(None, REASON_TIMEOUT, reserve, "fallback_uncertain", request=body)
    except Exception as exc:  # noqa: BLE001 - network layer, named in the audit
        log.warning("llm network error: %s: %s", type(exc).__name__, exc)
        return done(None, REASON_NETWORK, reserve, "fallback_uncertain", request=body)

    status = getattr(resp, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status is not None and status >= 400:
        real = _cost_from_http_error(resp)
        if real is not None:
            settle(real)
            return done(None, f"llm_http_{status}", real, "usage", status, request=body)
        return done(None, f"llm_http_{status}", reserve, "fallback_uncertain", status, request=body)
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return done(None, REASON_BAD_RESPONSE, reserve, "fallback_uncertain", status, request=body)

    cost, source = _cost_from(payload, llm)
    settle(cost)
    try:
        choice = payload["choices"][0]
        text = choice["message"]["content"]
    except Exception:  # noqa: BLE001
        return done(None, REASON_BAD_RESPONSE, cost, source, status, request=body)
    if not isinstance(text, str) or not text.strip():
        reason = REASON_TRUNCATED if choice.get("finish_reason") == "length" else REASON_EMPTY
        return done(None, reason, cost, source, status, request=body)
    intent = parse_action(text)
    if intent is None:
        return done(None, REASON_PARSE, cost, source, status, raw=text[:500], request=body)
    if not valid_for_position(intent, has_position):
        return done(None, REASON_INVALID_ACTION, cost, source, status, raw=text[:500], request=body)
    return done(intent, REASON_OK, cost, source, status, raw=text[:500], request=body)
