import json
from dataclasses import replace

import pytest
import requests

from bot.brain import Budget
from bot.settings import Settings
from fut.llm import REASON_INVALID_ACTION, decide, parse_action, request_body
from fut.settings import FutSettings
from fut.store import FutStore
from fut.types import FutIntent

STATE = {"mid": 76000.0, "position": "flat"}


def settings(**kwargs):
    llm = replace(Settings.from_env(), openrouter_api_key="key", llm_model="deepseek/x",
                  llm_fallback_cost_usd=0.02)
    return FutSettings(llm=llm, **kwargs)


def budget(cap=1.0, spent=0.0):
    return Budget(spent_usd=spent, cap_usd=cap, day="2026-09-17")


class Resp:
    def __init__(self, payload=None, status=200, bad_json=False):
        self.status_code, self._payload, self._bad = status, payload, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._payload


def completion(content, cost=0.001, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}], "usage": {"cost": cost}}


class Post:
    def __init__(self, resp=None, exc=None, on_call=None):
        self.resp, self.exc, self.on_call, self.calls = resp, exc, on_call, []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.on_call:
            self.on_call()
        if self.exc:
            raise self.exc
        return self.resp


def test_parse_action_variants():
    assert parse_action('{"action":"long","confidence":0.7,"reason":"flow"}') == FutIntent("LONG", 0.7, "flow")
    assert parse_action('```json\n{"action":"CLOSE","confidence":2,"reason":"x"}\n```') == FutIntent("CLOSE", 1.0, "x")
    assert parse_action('{"action":"HOLD","confidence":0.1,"reason":"x"} trailing "}') == FutIntent("HOLD", 0.1, "x")
    assert parse_action('{"action":"BUY","confidence":0.7}') is None
    assert parse_action('{"action":"LONG","confidence":"NaN"}') is None
    assert parse_action("garbage") is None
    assert parse_action(None) is None


def test_request_body_disables_reasoning_by_default():
    body = request_body(STATE, settings().llm, reasoning=False)
    assert body["reasoning"] == {"enabled": False}
    assert body["usage"] == {"include": True}
    assert json.loads(body["messages"][1]["content"]) == STATE
    assert "reasoning" not in request_body(STATE, settings().llm, reasoning=True)


def test_ok_long_when_flat_settles_real_cost():
    post = Post(Resp(completion('{"action":"LONG","confidence":0.7,"reason":"flow"}')))
    b = budget()
    d = decide(STATE, has_position=False, settings=settings(), budget=b, store=None, http_post=post)
    assert (d.reason, d.intent, d.cost_usd, d.cost_source) == ("ok", FutIntent("LONG", 0.7, "flow"), 0.001, "usage")
    assert b.spent_usd == pytest.approx(0.001)
    assert post.calls[0]["timeout"] == 8.0
    assert post.calls[0]["url"].endswith("/chat/completions")
    assert d.request == post.calls[0]["json"]
    assert "Authorization" not in json.dumps(d.as_audit())


@pytest.mark.parametrize("content, has_position", [
    ('{"action":"LONG","confidence":0.7,"reason":"x"}', True),
    ('{"action":"SHORT","confidence":0.7,"reason":"x"}', True),
    ('{"action":"CLOSE","confidence":0.7,"reason":"x"}', False),
])
def test_action_invalid_for_position_is_refused_but_still_charged(content, has_position):
    b = budget()
    d = decide(STATE, has_position=has_position, settings=settings(), budget=b, store=None,
               http_post=Post(Resp(completion(content))))
    assert (d.reason, d.intent) == (REASON_INVALID_ACTION, None)
    assert b.spent_usd == pytest.approx(0.001)


def test_timeout_keeps_the_reservation_as_charge():
    b = budget()
    d = decide(STATE, has_position=False, settings=settings(), budget=b, store=None,
               http_post=Post(exc=requests.Timeout("slow")))
    assert (d.reason, d.cost_usd, d.cost_source) == ("llm_timeout", 0.02, "fallback_uncertain")
    assert b.spent_usd == pytest.approx(0.02)


def test_http_error_without_usage_keeps_reservation():
    d = decide(STATE, has_position=False, settings=settings(), budget=budget(), store=None,
               http_post=Post(Resp({"error": "x"}, status=500)))
    assert (d.reason, d.http_status, d.cost_source) == ("llm_http_500", 500, "fallback_uncertain")


def test_truncated_completion_is_named():
    d = decide(STATE, has_position=False, settings=settings(), budget=budget(), store=None,
               http_post=Post(Resp(completion("", finish="length"))))
    assert d.reason == "llm_truncated"


def test_missing_key_and_exhausted_budget_never_post():
    post = Post(Resp(completion("{}")))
    no_key = FutSettings(llm=replace(Settings.from_env(), openrouter_api_key="", llm_model="x"))
    assert decide(STATE, has_position=False, settings=no_key, budget=budget(), store=None, http_post=post).reason == "llm_config"
    assert decide(STATE, has_position=False, settings=settings(), budget=budget(cap=0.01, spent=0.01),
                  store=None, http_post=post).reason == "llm_budget"
    assert post.calls == []


def test_reservation_is_durable_before_the_request(tmp_path):
    store = FutStore(tmp_path / "fut.db")
    seen = {}
    post = Post(Resp(completion('{"action":"HOLD","confidence":0.2,"reason":"x"}')),
                on_call=lambda: seen.update(store.budget_load()))
    d = decide(STATE, has_position=False, settings=settings(), budget=budget(), store=store, http_post=post)
    assert d.reason == "ok"
    assert seen["spent_usd"] == pytest.approx(0.02)
    assert store.budget_load()["spent_usd"] == pytest.approx(0.001)
