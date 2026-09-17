"""When Jev fires, call the LLM now; never two calls at once; never discard an exit."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from fut.llm import LlmDecision
from fut.settings import FutSettings

ENTRY_ACTIONS = ("LONG", "SHORT")


@dataclass(frozen=True)
class Trigger:
    kind: str  # entry_signal | exit_signal | reversal_signal
    side: str | None
    ts_ms: int
    mid: float
    state: dict[str, Any]


@dataclass(frozen=True)
class Resolution:
    trigger: Trigger
    decision: LlmDecision
    verdict: str  # ok | stale_timeout | stale_price
    elapsed_ms: int


class Dispatcher:
    def __init__(self, settings: FutSettings, *, run_llm: Callable[[Trigger], LlmDecision],
                 clock_ms: Callable[[], int], submit: Callable[..., Any] | None = None):
        self.settings = settings
        self._run = run_llm
        self._clock = clock_ms
        if submit is None:
            submit = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fut-llm").submit
        self._submit = submit
        self._future = None
        self._trigger: Trigger | None = None
        self._started_ms = 0
        self._last_hold: tuple[Trigger, int] | None = None

    @property
    def busy(self) -> bool:
        return self._future is not None

    def offer(self, trigger: Trigger, *, budget_ok: bool) -> str:
        now = self._clock()
        if self._future is not None:
            return "suppressed_inflight"
        if not budget_ok:
            return "suppressed_budget"
        if self._last_hold is not None and trigger.kind != "exit_signal":
            held, at = self._last_hold
            same = trigger.kind == held.kind and trigger.side == held.side
            if same and now - at < self.settings.llm_cooldown_s * 1000:
                return "suppressed_cooldown"
        self._future = self._submit(self._run, trigger)
        self._trigger = trigger
        self._started_ms = now
        return "dispatched"

    def poll(self, *, mid_now: float) -> Resolution | None:
        if self._future is None or not self._future.done():
            return None
        future, trigger = self._future, self._trigger
        self._future, self._trigger = None, None
        now = self._clock()
        elapsed = now - self._started_ms
        try:
            decision = future.result()
        except Exception as exc:  # noqa: BLE001 - a crashing worker is a non-decision, never a trade
            decision = LlmDecision(intent=None, reason=f"llm_crash:{type(exc).__name__}")

        verdict = "ok"
        if decision.intent is not None and decision.intent.action in ENTRY_ACTIONS:
            if elapsed > self.settings.llm_timeout_s * 1000:
                verdict = "stale_timeout"
            elif trigger.mid > 0 and mid_now > 0 and \
                    abs(mid_now - trigger.mid) / trigger.mid * 10_000 > self.settings.stale_price_bps:
                verdict = "stale_price"

        if decision.intent is None or decision.intent.action == "HOLD":
            self._last_hold = (trigger, now)
        else:
            self._last_hold = None
        return Resolution(trigger, decision, verdict, elapsed)
