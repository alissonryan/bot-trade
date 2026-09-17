import json
import sqlite3
from types import SimpleNamespace

import pytest

from fut.jev import JevClient, MockJev
from fut.questions import build_questions, build_questions_labels, jev_state_labels
from fut.settings import FutSettings
from fut.types import FutPosition
from tests.fut.helpers import make_snap


LONG = FutPosition(side="long", contracts=1, entry=76000.0, stop=75900.0, opened_ms=0)


def test_labeled_questions_are_semantic_and_drop_cost_question():
    questions = build_questions_labels(has_position=True)
    assert set(questions) == {"direction_60s", "flow_aligned", "regime", "exit_now"}
    text = str(questions["direction_60s"].criteria)
    assert "up" in text and "more likely" in text
    assert "bps" not in text
    assert "move_beats_cost" not in questions
    assert set(build_questions(has_position=False, move_cost_bps=3.0)) == {
        "direction_60s", "move_beats_cost", "flow_aligned", "regime"
    }


def test_labeled_state_is_bucketed():
    state = jev_state_labels(make_snap(returns_bps={"10s": 10.0, "60s": 10.0}), LONG, now_ms=1000)
    assert state["labels"]["price_10s"] == "rising_fast"
    assert state["position"]["side"] == "long" if isinstance(state["position"], dict) else False
    assert "mid" not in state and "returns_bps" not in state


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def system_one(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        return self.response


def response(with_probabilities=True):
    direction = SimpleNamespace(choice="up", confidence=0.72)
    if with_probabilities:
        direction.probabilities = {"up": 0.72, "down": 0.18, "flat": 0.10}
    return SimpleNamespace(
        choices={"direction_60s": direction, "regime": SimpleNamespace(choice="trend", confidence=0.9)},
        nouls={"move_beats_cost": SimpleNamespace(noul=0.66), "flow_aligned": SimpleNamespace(noul=0.8),
               "exit_now": SimpleNamespace(noul=0.1)},
        usage=SimpleNamespace(input_tokens=1500), model="jev-1.13.0")


def test_jev_probabilities_are_optional_and_kept_on_raw_path():
    fake = FakeClient(response())
    verdict = JevClient(FutSettings(typesafe_api_key="k"), client=fake).evaluate(make_snap(), LONG, now_ms=1000)
    assert verdict.probabilities == {"up": 0.72, "down": 0.18, "flat": 0.1}

    no_probs = JevClient(FutSettings(typesafe_api_key="k"), client=FakeClient(response(False))).evaluate(
        make_snap(), LONG, now_ms=1000
    )
    assert no_probs.probabilities is None


def test_jev_label_evaluation_uses_labeled_state_and_questions():
    fake = FakeClient(response())
    verdict = JevClient(FutSettings(typesafe_api_key="k"), client=fake).evaluate_labels(
        make_snap(), LONG, now_ms=1000
    )
    assert verdict.probabilities == {"up": 0.72, "down": 0.18, "flat": 0.1}
    assert "labels" in fake.calls[0][0]
    assert "move_beats_cost" not in fake.calls[0][1]
    assert verdict.beats_cost is None


def test_mock_jev_supports_labeled_variant():
    verdict = MockJev(FutSettings()).evaluate_labels(make_snap(), FutPosition(), now_ms=0)
    assert verdict.model == "mock"
    assert "labels" in verdict.state
    assert verdict.beats_cost is None


def test_model_cost_includes_observational_rows(tmp_path):
    from fut.store import FutStore

    store = FutStore(tmp_path / "fut.db")
    store.log_decision("jev_ab", {"cost_usd": 0.25}, ts_ms=1000)
    assert store.model_cost_between(0, 2000) == pytest.approx(0.25)
