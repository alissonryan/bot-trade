from types import SimpleNamespace

import pytest

from fut.jev import JevClient, MockJev, make_jev
from fut.questions import build_questions, jev_side, jev_state, should_wake
from fut.settings import FutSettings
from fut.types import FutPosition, JevVerdict
from tests.fut.helpers import make_snap


LONG = FutPosition(side="long", contracts=2, entry=76000.0, stop=75900.0, liq=380.0, margin=15.2, opened_ms=0)


def verdict(direction="up", conf=0.8, beats=0.8, exit_now=None, error=None):
    return JevVerdict(direction, conf, beats, 0.5, "trend", exit_now, 100, 1000, "jev-1", error=error)


def test_questions_without_and_with_position():
    flat = build_questions(has_position=False, move_cost_bps=3.0)
    assert set(flat) == {"direction_60s", "move_beats_cost", "flow_aligned", "regime"}
    assert set(flat["direction_60s"].criteria) == {"up", "down", "flat"}
    assert set(flat["regime"].criteria) == {"trend", "range", "volatile"}
    assert "3 bps" in str(flat["move_beats_cost"].instructions)
    assert "exit_now" in build_questions(has_position=True, move_cost_bps=3.0)


def test_jev_state_flat_and_open():
    snap = make_snap()
    assert jev_state(snap, FutPosition(), now_ms=0)["position"] == "flat"
    state = jev_state(snap, LONG, now_ms=30_000)
    pos = state["position"]
    assert pos["side"] == "long" and pos["seconds_open"] == 30
    assert pos["stop_distance_bps"] == pytest.approx((snap.mid - 75900.0) / snap.mid * 10_000, abs=0.01)
    assert {"mid", "spread_bps", "imbalance", "depth_bps", "returns_bps", "flow", "funding_rate"} <= set(state)


def test_should_wake_entry_needs_direction_and_cost():
    flat = FutPosition()
    assert should_wake(verdict(), flat, threshold=0.6) == "entry_signal"
    assert should_wake(verdict(conf=0.5), flat, threshold=0.6) is None
    assert should_wake(verdict(beats=0.5), flat, threshold=0.6) is None
    assert should_wake(verdict(direction="flat"), flat, threshold=0.6) is None
    assert should_wake(verdict(error="timeout"), flat, threshold=0.6) is None


def test_should_wake_with_position():
    assert should_wake(verdict(exit_now=0.7), LONG, threshold=0.6) == "exit_signal"
    assert should_wake(verdict(direction="down", exit_now=0.1), LONG, threshold=0.6) == "reversal_signal"
    assert should_wake(verdict(direction="up", exit_now=0.1), LONG, threshold=0.6) is None
    assert jev_side(verdict(direction="down")) == "short"


class FakeClient:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    def system_one(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        if self.exc:
            raise self.exc
        return self.response


def response():
    return SimpleNamespace(
        choices={"direction_60s": SimpleNamespace(choice="down", confidence=0.72),
                 "regime": SimpleNamespace(choice="trend", confidence=0.9)},
        nouls={"move_beats_cost": SimpleNamespace(noul=0.66), "flow_aligned": SimpleNamespace(noul=0.8),
               "exit_now": SimpleNamespace(noul=0.1)},
        usage=SimpleNamespace(input_tokens=1500), model="jev-1.13.0")


def test_jev_client_maps_answers():
    fake = FakeClient(response())
    v = JevClient(FutSettings(typesafe_api_key="k"), client=fake).evaluate(make_snap(), LONG, now_ms=1000)
    assert (v.direction, v.direction_conf, v.beats_cost, v.flow_aligned) == ("down", 0.72, 0.66, 0.8)
    assert (v.regime, v.exit_now, v.input_tokens, v.model, v.error) == ("trend", 0.1, 1500, "jev-1.13.0", None)
    assert "exit_now" in fake.calls[0][1]
    assert v.state["position"]["side"] == "long"


def test_jev_client_failure_is_a_named_verdict():
    v = JevClient(FutSettings(typesafe_api_key="k"), client=FakeClient(exc=TimeoutError("slow"))).evaluate(
        make_snap(), FutPosition(), now_ms=1000)
    assert v.error.startswith("TimeoutError") and v.direction == "flat"
    assert should_wake(v, FutPosition(), threshold=0.6) is None


def test_mock_jev_follows_momentum_and_flow():
    snap = make_snap(returns_bps={"10s": 20.0, "60s": 6.0}, imbalance=0.5,
                     flow={"30s": {"buy": 10, "sell": 0, "cvd": 10, "vwap": 76000.0}})
    v = MockJev(FutSettings()).evaluate(snap, FutPosition(), now_ms=0)
    assert v.direction == "up" and v.direction_conf > 0.9 and v.beats_cost == 1.0
    assert v.model == "mock" and v.exit_now is None


def test_make_jev_uses_mock_without_key():
    assert isinstance(make_jev(FutSettings()), MockJev)
    assert isinstance(make_jev(FutSettings(typesafe_api_key="k", jev_model="mock")), MockJev)
