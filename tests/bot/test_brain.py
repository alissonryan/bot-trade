from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.brain import Budget, parse_intent, think
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


def test_think_charges_budget_on_success():
    budget = Budget(spent_usd=0.0, cap_usd=2.0, day="2026-09-04")

    def post(*args, **kwargs):
        class R:
            status_code = 200

            def json(self):
                return {
                    "choices": [
                        {"message": {"content": '{"action":"HOLD","confidence":0.1,"reason":"wait","regime":"range"}'}}
                    ]
                }

            def raise_for_status(self):
                return None

        return R()

    snap = Snapshot(
        ts_ms=1, last=1, bid=1, ask=1, spread=0, bars_15m=[], atr=1,
        free_usdt=1, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )
    d = Settings.from_env().__dict__.copy()
    d["openrouter_api_key"] = "k"
    d["llm_model"] = "x"
    s = Settings(**d)
    out = think(snap, s, budget, http_post=post)
    assert out is not None
    assert out.action == "HOLD"
    assert budget.spent_usd > 0


def test_think_empty_content_returns_none():
    budget = Budget(spent_usd=0.0, cap_usd=2.0, day="2026-09-04")

    def post(*args, **kwargs):
        class R:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": None}}]}

            def raise_for_status(self):
                return None

        return R()

    snap = Snapshot(
        ts_ms=1, last=1, bid=1, ask=1, spread=0, bars_15m=[], atr=1,
        free_usdt=1, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )
    d = Settings.from_env().__dict__.copy()
    d["openrouter_api_key"] = "k"
    d["llm_model"] = "x"
    s = Settings(**d)
    assert think(snap, s, budget, http_post=post) is None


def test_think_returns_none_over_budget():
    budget = Budget(spent_usd=2.0, cap_usd=2.0, day="2026-09-04")
    called = []

    def post(*args, **kwargs):
        called.append(1)
        return FakeResp({})

    snap = Snapshot(
        ts_ms=1, last=1, bid=1, ask=1, spread=0, bars_15m=[], atr=1,
        free_usdt=1, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )
    s = Settings.from_env()
    out = think(snap, s, budget, http_post=post)
    assert out is None
    assert called == []
