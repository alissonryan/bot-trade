from concurrent.futures import Future

from fut.dispatch import Dispatcher, Trigger
from fut.llm import LlmDecision
from fut.settings import FutSettings
from fut.types import FutIntent


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class ManualSubmit:
    def __init__(self):
        self.jobs = []

    def __call__(self, fn, *args):
        future = Future()
        self.jobs.append((future, fn, args))
        return future

    def finish(self):
        future, fn, args = self.jobs.pop(0)
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)


def trig(kind="entry_signal", side="long", mid=100.0):
    return Trigger(kind, side, 0, mid, {})


def make(action="LONG", run=None, **settings):
    clock, submit = Clock(), ManualSubmit()

    def default_run(trigger):
        return LlmDecision(intent=FutIntent(action, 0.7, "r"), reason="ok")

    return Dispatcher(FutSettings(**settings), run_llm=run or default_run, clock_ms=clock, submit=submit), clock, submit


def cycle(d, clock, submit, trigger, at, mid_now=None):
    clock.now = at
    assert d.offer(trigger, budget_ok=True) == "dispatched"
    submit.finish()
    return d.poll(mid_now=trigger.mid if mid_now is None else mid_now)


def test_dispatches_immediately_and_resolves():
    d, clock, submit = make()
    assert d.offer(trig(), budget_ok=True) == "dispatched"
    assert d.busy and d.poll(mid_now=100.0) is None
    submit.finish()
    clock.now = 500
    res = d.poll(mid_now=100.0)
    assert (res.verdict, res.elapsed_ms, res.decision.intent.action) == ("ok", 500, "LONG")
    assert not d.busy


def test_one_call_in_flight_and_budget_gate():
    d, _, submit = make()
    assert d.offer(trig(), budget_ok=False) == "suppressed_budget"
    assert submit.jobs == []
    d.offer(trig(), budget_ok=True)
    assert d.offer(trig(side="short"), budget_ok=True) == "suppressed_inflight"
    assert len(submit.jobs) == 1


def test_hold_arms_cooldown_for_same_kind_and_side_only():
    d, clock, submit = make("HOLD")
    cycle(d, clock, submit, trig(), at=0)
    clock.now = 5000
    assert d.offer(trig(), budget_ok=True) == "suppressed_cooldown"
    assert d.offer(trig(side="short"), budget_ok=True) == "dispatched"


def test_cooldown_expires():
    d, clock, submit = make("HOLD")
    cycle(d, clock, submit, trig(), at=0)
    clock.now = 10_001
    assert d.offer(trig(), budget_ok=True) == "dispatched"


def test_exit_signal_is_never_cooled_down():
    d, clock, submit = make("HOLD")
    cycle(d, clock, submit, trig("exit_signal", None), at=0)
    clock.now = 1000
    assert d.offer(trig("exit_signal", None), budget_ok=True) == "dispatched"


def test_late_entry_is_stale_timeout():
    d, clock, submit = make("LONG")
    d.offer(trig(), budget_ok=True)
    submit.finish()
    clock.now = 9000
    assert d.poll(mid_now=100.0).verdict == "stale_timeout"


def test_entry_after_price_moved_is_stale_price():
    d, clock, submit = make("SHORT")
    assert cycle(d, clock, submit, trig(side="short"), at=0, mid_now=100.06).verdict == "stale_price"
    assert cycle(d, clock, submit, trig(side="short"), at=1, mid_now=100.04).verdict == "ok"


def test_close_is_never_discarded_for_latency_or_price():
    d, clock, submit = make("CLOSE")
    d.offer(trig("exit_signal", None), budget_ok=True)
    submit.finish()
    clock.now = 60_000
    assert d.poll(mid_now=150.0).verdict == "ok"


def test_crashing_llm_becomes_a_named_non_decision_and_arms_cooldown():
    def boom(trigger):
        raise RuntimeError("bug")

    d, clock, submit = make(run=boom)
    res = cycle(d, clock, submit, trig(), at=0)
    assert res.decision.intent is None and res.decision.reason == "llm_crash:RuntimeError"
    clock.now = 100
    assert d.offer(trig(), budget_ok=True) == "suppressed_cooldown"
