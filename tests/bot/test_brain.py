from pathlib import Path
import json
import sys

import pytest
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


@pytest.mark.parametrize("confidence", ["NaN", "Infinity", "-Infinity"])
def test_parse_rejects_non_finite_confidence_json_literal(confidence):
    # json.loads() accepts these non-standard tokens by default, so a model
    # that emits a bare NaN/Infinity token (not a quoted string) must still
    # be rejected -- comparisons against NaN are always False and would
    # silently bypass the confidence gate downstream.
    raw = '{"action":"BUY","confidence":%s,"reason":"x","regime":"trend"}' % confidence
    assert parse_intent(raw) is None


@pytest.mark.parametrize("confidence", ["NaN", "Infinity", "-Infinity", "nan", "inf", "-inf"])
def test_parse_rejects_non_finite_confidence_string_form(confidence):
    raw = '{"action":"BUY","confidence":"%s","reason":"x","regime":"trend"}' % confidence
    assert parse_intent(raw) is None


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


def test_timeout_and_network_reserve_a_conservative_fallback_charge():
    """Finding 3: a timeout or network error leaves the actual provider outcome
    genuinely unknown -- the request may have already reached and been billed
    by OpenRouter even though this process never saw the response. Charging
    exactly $0 there is an optimistic assumption, not a fact; it must reserve
    the same conservative fallback cost an unparseable-but-received response
    already gets (see llm_parse above), so a run of timeouts cannot silently
    look free while draining a real daily budget on the provider's side."""
    s = _settings(llm_fallback_cost_usd=0.02)

    def timeout(*a, **k):
        raise requests.Timeout("slow")

    budget = Budget(0, 2, "d")
    res = think_result(_snap(), s, budget, http_post=timeout)
    assert res.reason == "llm_timeout"
    assert res.cost_usd == pytest.approx(0.02)
    assert budget.spent_usd == pytest.approx(0.02)
    assert budget.calls == 1

    def network(*a, **k):
        raise requests.ConnectionError("dns")

    budget = Budget(0, 2, "d")
    res = think_result(_snap(), s, budget, http_post=network)
    assert res.reason == "llm_network"
    assert res.cost_usd == pytest.approx(0.02)
    assert budget.spent_usd == pytest.approx(0.02)


def test_request_body_has_token_cap_usage_and_optional_json_mode():
    body = request_body(_snap(), _settings(llm_max_tokens=150))
    assert body["max_tokens"] == 150
    assert body["usage"] == {"include": True}
    assert "response_format" not in body
    body = request_body(_snap(), _settings(llm_json_mode=True))
    assert body["response_format"] == {"type": "json_object"}


def test_think_result_audit_matches_the_exact_request_actually_dispatched():
    """Finding 4 (re-review): checking a few cherry-picked fields (model/
    role/count) cannot catch a refactor that persists a SEPARATELY built
    audit dict instead of the literal body handed to post() -- they could
    silently diverge. Assert full equality against the captured post()
    kwarg instead, with lessons/as_of/json_mode all varying the body so a
    stub audit that only copies the static parts would fail this too."""
    from bot.types import Bar

    budget = Budget(spent_usd=0.0, cap_usd=2.0, day="2026-09-04")
    snap = Snapshot(
        ts_ms=1000, last=100, bid=99, ask=101, spread=2,
        bars_15m=[Bar(t=i, o=100, h=101, l=99, c=100 + i, v=1) for i in range(25)],
        atr=1.5, free_usdt=450, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )
    lessons = [{"id": 7, "decision_ms": 0, "outcome_known_ms": 0, "action": "BUY",
                "confidence": 0.5, "regime": "trend", "reason": "x", "outcome": "win"}]
    settings = _settings(llm_json_mode=True)
    captured: dict = {}

    def post(*a, **kw):
        captured["json"] = kw["json"]
        return FakeResp(_ok_payload())

    res = think_result(snap, settings, budget, http_post=post, lessons=lessons, as_of_ms=snap.ts_ms)
    audit = res.as_audit()
    assert audit["request"] == captured["json"], "persisted request diverges from what was actually sent"
    assert audit["request"]["response_format"] == {"type": "json_object"}
    user_payload = json.loads(audit["request"]["messages"][1]["content"])
    assert len(user_payload["bars_15m"]) == 20, "candles must be the full sliced window, not a count"
    assert user_payload["lessons"][0]["id"] == 7
    # No secret ever rides in the body -- the API key only ever goes in headers.
    assert "openrouter_api_key" not in json.dumps(audit)
    assert "Bearer" not in json.dumps(audit)


def test_no_request_captured_when_nothing_was_actually_sent():
    """A budget/config short-circuit sends no HTTP request at all -- the audit
    must not fabricate a request payload for a call that never happened."""
    res = think_result(_snap(), _settings(openrouter_api_key=""), Budget(0, 2, "d"))
    assert res.reason == "llm_config"
    assert "request" not in res.as_audit()

    over_budget = think_result(_snap(), _settings(), Budget(2.0, 2.0, "d"))
    assert over_budget.reason == "llm_budget"
    assert "request" not in over_budget.as_audit()


def test_timeout_and_http_error_still_capture_the_exact_dispatched_request():
    """A request that was sent but failed (timeout, 4xx) is exactly the case
    an operator most needs to reconstruct -- the persisted request must be
    the literal body handed to post(), not a reconstruction, even though
    the call itself never returned normally."""
    s = _settings()
    captured: dict = {}

    def timeout(*a, **kw):
        captured["json"] = kw["json"]
        raise requests.Timeout("slow")

    res = think_result(_snap(), s, Budget(0, 2, "d"), http_post=timeout)
    assert res.reason == "llm_timeout"
    assert res.as_audit()["request"] == captured["json"]

    captured.clear()

    def http_429(*a, **kw):
        captured["json"] = kw["json"]
        return FakeResp({"error": "x"}, status=429)

    res = think_result(_snap(), s, Budget(0, 2, "d"), http_post=http_429)
    assert res.reason == "llm_http_429"
    assert res.as_audit()["request"] == captured["json"]



def test_invalid_fallback_cost_refuses_before_any_http_call():
    """Main's boundary proof: LLM_FALLBACK_COST_USD=0 was coerced to 0.0 and
    the call proceeded with NO real reservation at all -- an HTTP call still
    fired. An invalid (missing/zero/negative/non-finite) fallback cost must
    refuse outright, in both think_result and reflect_result."""
    calls = []

    def post(*a, **kw):
        calls.append(1)
        return FakeResp(_ok_payload())

    for bad in (0.0, -0.01, float("nan"), float("inf")):
        s = _settings(llm_fallback_cost_usd=bad)
        res = think_result(_snap(), s, Budget(0, 2, "d"), http_post=post)
        assert res.reason == "llm_invalid_reserve"
        assert res.intent is None
    assert calls == []

    from bot.brain import reflect_result
    for bad in (0.0, -0.01, float("nan")):
        s = _settings(llm_fallback_cost_usd=bad)
        res = reflect_result({"outcome": {"kind": "closed"}}, s, Budget(0, 2, "d"), http_post=post)
        assert res.reason == "reflection_invalid_reserve"
    assert calls == []


def test_insufficient_in_memory_reserve_refuses_before_http_even_when_remaining_is_positive():
    """Main's boundary proof: budget.remaining()<=0 is not the same check as
    'enough room for the planned reserve' -- a cap of 0.001 with a reserve of
    0.02 has SOME room (passes remaining()<=0) but not ENOUGH, and the old
    code dispatched anyway."""
    calls = []

    def post(*a, **kw):
        calls.append(1)
        return FakeResp(_ok_payload())

    s = _settings(llm_fallback_cost_usd=0.02)
    res = think_result(_snap(), s, Budget(0, 0.001, "d"), http_post=post)
    assert res.reason == "llm_budget"
    assert calls == []


def test_http_error_reads_real_usage_cost_when_the_body_provides_one():
    """'Not typically billed' is an assumption, not proof for THIS response
    -- when an error body still carries a real usage.cost, settle to that
    real figure instead of guessing $0."""
    budget = Budget(0.0, 2.0, "d")
    res = think_result(_snap(), _settings(), budget,
                       http_post=lambda *a, **k: FakeResp({"usage": {"cost": 0.0009}, "error": "rejected"}, status=402))
    assert res.reason == "llm_http_402"
    assert res.cost_source == "usage"
    assert res.cost_usd == pytest.approx(0.0009)
    assert budget.spent_usd == pytest.approx(0.0009)


def test_http_5xx_preserves_the_reservation_instead_of_fabricating_zero():
    """A 5xx with no usage figure is genuinely uncertain -- settling to $0
    would assume free, but the request may already have been billed on the
    provider's side. Keep the reservation as the charge, like a timeout."""
    s = _settings(llm_fallback_cost_usd=0.02)
    budget = Budget(0.0, 2.0, "d")
    res = think_result(_snap(), s, budget,
                       http_post=lambda *a, **k: FakeResp({"error": "boom"}, status=503))
    assert res.reason == "llm_http_503"
    assert res.cost_source == "fallback_uncertain"
    assert res.cost_usd == pytest.approx(0.02)
    assert budget.spent_usd == pytest.approx(0.02)


def test_settlement_failure_blocks_every_subsequent_call_in_this_process():
    """A durable settlement failure after the real cost is already known
    means the persisted ledger may now understate true spend -- letting the
    NEXT call proceed as if nothing happened could authorize spend beyond
    the real cap. Block every further think_result()/reflect_result() call
    on this Budget, with no timeout that clears it, without touching the
    CURRENT call's already-earned result."""
    from bot.brain import reflect_result

    class ExplodingStore:
        def reserve_budget(self, **kw):
            return True

        def settle_budget(self, **kw):
            raise RuntimeError("disk full")

    s = _settings(llm_fallback_cost_usd=0.02)
    budget = Budget(0.0, 2.0, "2026-09-09")
    store = ExplodingStore()
    res = think_result(_snap(), s, budget, store=store,
                       http_post=lambda *a, **k: FakeResp(_ok_payload()))
    # The current call still returns its real, already-earned outcome.
    assert res.reason == "ok"
    assert res.intent is not None
    assert budget.blocked_reason == "llm_budget_settlement_failed"

    calls = []

    def post(*a, **kw):
        calls.append(1)
        return FakeResp(_ok_payload())

    next_decision = think_result(_snap(), s, budget, store=store, http_post=post)
    assert next_decision.reason == "llm_budget_settlement_failed"
    assert next_decision.intent is None
    assert calls == []

    reflection = reflect_result({"outcome": {"kind": "closed"}}, s, budget, store=store, http_post=post)
    assert reflection.reason == "llm_budget_settlement_failed"
    assert calls == []

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
