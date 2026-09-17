"""Jev (TypeSafe System One) as the fast trigger, plus a deterministic stand-in.

Sessions run with MockJev are marked ``model="mock"`` and never count toward the edge criterion.
"""

from __future__ import annotations

import math
import time

from fut.questions import build_questions, jev_state
from fut.settings import FutSettings
from fut.types import FutPosition, FutSnapshot, JevVerdict


class JevClient:
    def __init__(self, settings: FutSettings, *, client=None):
        self.settings = settings
        self.name = settings.jev_model
        if client is None:
            from typesafe_sdk import RetryPolicy, TypeSafeClient

            client = TypeSafeClient(api_key=settings.typesafe_api_key, model=settings.jev_model,
                                    retry=RetryPolicy(max_retries=0), timeout=settings.jev_timeout_s)
        self.client = client

    def evaluate(self, snap: FutSnapshot, position: FutPosition, *, now_ms: int) -> JevVerdict:
        state = jev_state(snap, position, now_ms=now_ms)
        questions = build_questions(has_position=position.is_open(), move_cost_bps=self.settings.move_cost_bps)
        started = time.monotonic()
        try:
            r = self.client.system_one(state, questions)
            direction, regime = r.choices["direction_60s"], r.choices["regime"]
            exit_now = float(r.nouls["exit_now"].noul) if "exit_now" in questions else None
            return JevVerdict(
                direction=str(direction.choice), direction_conf=float(direction.confidence),
                beats_cost=float(r.nouls["move_beats_cost"].noul), flow_aligned=float(r.nouls["flow_aligned"].noul),
                regime=str(regime.choice), exit_now=exit_now,
                latency_ms=int((time.monotonic() - started) * 1000),
                input_tokens=int(getattr(r.usage, "input_tokens", 0) or 0), model=str(r.model), state=state)
        except Exception as exc:  # noqa: BLE001 - any failure means "do not wake the LLM", named in the audit
            return JevVerdict.failed(f"{type(exc).__name__}: {exc}"[:200],
                                     latency_ms=int((time.monotonic() - started) * 1000),
                                     model=self.name, state=state)


class MockJev:
    name = "mock"

    def __init__(self, settings: FutSettings):
        self.settings = settings

    def evaluate(self, snap: FutSnapshot, position: FutPosition, *, now_ms: int) -> JevVerdict:
        state = jev_state(snap, position, now_ms=now_ms)
        f30 = snap.flow.get("30s", {})
        volume = (f30.get("buy") or 0) + (f30.get("sell") or 0)
        flow = (f30.get("cvd") or 0) / volume if volume else 0.0
        r10 = snap.returns_bps.get("10s") or 0.0
        signal = max(-50.0, min(50.0, r10 / 2 + snap.imbalance * 1.5 + flow * 2))
        p_up = 1 / (1 + math.exp(-signal))
        direction = "up" if p_up >= 0.6 else "down" if p_up <= 0.4 else "flat"
        beats = min(1.0, abs(snap.returns_bps.get("60s") or 0.0) / max(self.settings.move_cost_bps, 1e-9))
        aligned = 1.0 if (flow > 0) == (r10 > 0) else 0.0
        regime = "trend" if abs(signal) >= 8 else "volatile" if abs(signal) >= 3 else "range"
        exit_now = None
        if position.is_open():
            exit_now = 1 - p_up if position.side == "long" else p_up
        return JevVerdict(direction, min(1.0, abs(p_up - 0.5) * 2), beats, aligned, regime, exit_now,
                          0, 0, "mock", state=state)


def make_jev(settings: FutSettings):
    return MockJev(settings) if settings.uses_mock_jev else JevClient(settings)
