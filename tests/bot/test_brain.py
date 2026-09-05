from pathlib import Path
import sys

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.brain import Budget, parse_intent, request_body, think, think_result
from bot.settings import Settings
from bot.types import Snapshot, TradeIntent


def test_parse_strips_fences_and_extra_keys():
    raw = """```json
    {"action":"BUY","confidence":0.7,"reason":"breakout","regime":"trend","qty":99}
    ```"""
    intent = parse_intent(raw)
    assert intent == TradeIntent("BUY", 0.7, "breakout", "trend")


def test_parse_rejects_bad_action():
    assert parse_intent('{"action":"YEET","confidence":1,"reason":"x","regime":"trend"}') is None


def test_parse_none_or_empty_returns_none():
    assert parse_intent(None) is None  # type: ignore[arg-type]
    assert parse_intent("") is None
    assert parse_intent("   ") is None


def test_parse_truncates_reason():
    reason = "n" * 400
    intent = parse_intent(
        '{"action":"HOLD","confidence":0.1,"reason":"%s","regime":"unknown"}' % reason
    )
    assert intent is not None
    assert len(intent.reason) == 240


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http")


def _snap():
    return Snapshot(
        ts_ms=1, last=1, bid=1, ask=1, spread=0, bars_15m=[], atr=1,
        free_usdt=1, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )


def _settings(**kw) -> Settings:
    d = Settings.from_env().__dict__.copy()
    d["openrouter_api_key"] = "k"
    d["llm_model"] = "x"
    d.update(kw)
    return Settings(**d)


def _ok_payload(content='{"action":"HOLD","confidence":0.1,"reason":"wait","regime":"range"}', usage=None):
    payload = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return payload


def test_think_charges_budget_on_success():
    budget = Budget(spent_usd=0.0, cap_usd=2.0, day="2026-09-04")

    def post(*args, **kwargs):
        return FakeResp(_ok_payload())

    out = think(_snap(), _settings(), budget, http_post=post)
    assert out is not None
    assert out.action == "HOLD"
    assert budget.spent_usd > 0
    assert budget.calls == 1


def test_think_uses_reported_cost_when_present():
    budget = Budget(spent_usd=0.0, cap_usd=2.0, day="2026-09-04")

    def post(*args, **kwargs):
        return FakeResp(_ok_payload(usage={"cost": 0.00037, "prompt_tokens": 500}))

    res = think_result(_snap(), _settings(), budget, http_post=post)
    assert res.reason == "ok"
    assert res.cost_source == "usage"
    assert abs(budget.spent_usd - 0.00037) < 1e-9


def test_think_falls_back_to_flat_cost_without_usage():
    budget = Budget(spent_usd=0.0, cap_usd=2.0, day="2026-09-04")
    res = think_result(_snap(), _settings(llm_fallback_cost_usd=0.02), budget, http_post=lambda *a, **k: FakeResp(_ok_payload()))
    assert res.cost_source == "fallback"
    assert abs(budget.spent_usd - 0.02) < 1e-9


def test_think_empty_content_returns_none():
    budget = Budget(spent_usd=0.0, cap_usd=2.0, day="2026-09-04")
    res = think_result(_snap(), _settings(), budget, http_post=lambda *a, **k: FakeResp(_ok_payload(content=None)))
    assert res.intent is None
    assert res.reason == "llm_empty"


def test_think_returns_none_over_budget():
    budget = Budget(spent_usd=2.0, cap_usd=2.0, day="2026-09-04")
    called = []

    def post(*args, **kwargs):
        called.append(1)
        return FakeResp({})

    res = think_result(_snap(), _settings(), budget, http_post=post)
    assert res.intent is None
    assert res.reason == "llm_budget"
    assert called == []


def test_think_names_config_http_timeout_network_and_parse_failures():
    s = _settings()
    assert think_result(_snap(), _settings(openrouter_api_key=""), Budget(0, 2, "d")).reason == "llm_config"

    res = think_result(_snap(), s, Budget(0, 2, "d"), http_post=lambda *a, **k: FakeResp({"error": "x"}, status=429))
    assert res.reason == "llm_http_429" and res.http_status == 429

    def timeout(*a, **k):
        raise requests.Timeout("slow")

    assert think_result(_snap(), s, Budget(0, 2, "d"), http_post=timeout).reason == "llm_timeout"

    def network(*a, **k):
        raise requests.ConnectionError("dns")

    assert think_result(_snap(), s, Budget(0, 2, "d"), http_post=network).reason == "llm_network"

    budget = Budget(0, 2, "d")
    res = think_result(_snap(), s, budget, http_post=lambda *a, **k: FakeResp(_ok_payload(content="I think we should wait")))
    assert res.reason == "llm_parse"
    assert budget.calls == 1  # a bad answer still cost money

    res = think_result(_snap(), s, Budget(0, 2, "d"), http_post=lambda *a, **k: FakeResp({"choices": []}))
    assert res.reason == "llm_bad_response"


def test_request_body_has_token_cap_usage_and_optional_json_mode():
    body = request_body(_snap(), _settings(llm_max_tokens=150))
    assert body["max_tokens"] == 150
    assert body["usage"] == {"include": True}
    assert "response_format" not in body
    body = request_body(_snap(), _settings(llm_json_mode=True))
    assert body["response_format"] == {"type": "json_object"}


def test_budget_rolls_over_at_new_day():
    b = Budget(1.5, 2.0, "2026-09-04", calls=7)
    b.roll_day("2026-09-04")
    assert b.calls == 7
    b.roll_day("2026-09-05")
    assert b.spent_usd == 0.0 and b.calls == 0


def test_truncated_response_is_named_not_silently_a_hold(caplog):
    """Finding 10: LLM_MAX_TOKENS caps the *whole* completion, and on OpenRouter a
    reasoning model spends that budget before emitting content. The result was an
    empty string -> llm_empty -> forced HOLD, indistinguishable from a real HOLD
    in the audit, while budget.spend() still charged for every call."""
    import logging

    caplog.set_level(logging.ERROR, logger="bot")
    budget = Budget(spent_usd=0.0, cap_usd=2.0, day="2026-09-04")
    payload = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}

    res = think_result(_snap(), _settings(), budget, http_post=lambda *a, **k: FakeResp(payload))

    assert res.intent is None
    assert res.reason == "llm_truncated"
    assert "max_tokens" in caplog.text


def test_empty_content_without_truncation_is_still_llm_empty():
    """The control: an empty body that was not truncated keeps its own reason."""
    budget = Budget(spent_usd=0.0, cap_usd=2.0, day="2026-09-04")
    res = think_result(_snap(), _settings(), budget, http_post=lambda *a, **k: FakeResp(_ok_payload(content=None)))
    assert res.reason == "llm_empty"


def test_parse_intent_survives_trailing_garbage_after_the_object():
    """Observed live on 2026-09-05 with deepseek-v4-flash: the model emitted a
    valid object followed by a stray '"}'. The fallback used rfind('}'), which
    grabbed the *trailing* brace, so the slice was still invalid and a perfectly
    good decision was thrown away as a forced HOLD."""
    raw = (
        ' {"action":"HOLD","confidence":0.6,'
        '"reason":"price is ranging, ATR is 58, no clear direction","regime":"range"}"}'
    )
    intent = parse_intent(raw)
    assert intent is not None
    assert intent.action == "HOLD"
    assert intent.confidence == 0.6
    assert intent.regime == "range"


def test_parse_intent_ignores_braces_inside_strings():
    raw = '{"action":"BUY","confidence":0.8,"reason":"a } inside text","regime":"trend"} trailing'
    intent = parse_intent(raw)
    assert intent is not None and intent.action == "BUY"
    assert intent.reason == "a } inside text"
