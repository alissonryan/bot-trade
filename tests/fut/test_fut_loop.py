import json
import queue
from concurrent.futures import Future
from dataclasses import replace

import pytest

from bot.brain import REASON_BUDGET_STATE, Budget
from bot.settings import Settings
from fut.llm import LlmDecision
from fut.loop import FutLoop, Unmonitored, seed_budget
from fut.questions import jev_state
from fut.settings import FutSettings
from fut.store import FutStore
from fut.types import FutGate, FutIntent, JevVerdict
from kcex.client import KcexError
from kcex.fws import FutDepth, FutTicker
from tests.fut.helpers import SPEC, make_snap

T0 = 1_789_000_000_000
UP = JevVerdict("up", 0.9, 0.9, 0.8, "trend", None, 100, 1000, "jev-1.13.0")


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class FakeRest:
    def __init__(self, ticker_error=False, ticker_bid=76000.0, ticker_ask=76000.1, ticker_fair=76000.0):
        self.calls, self.ticker_error = [], ticker_error
        self.ticker_bid, self.ticker_ask, self.ticker_fair = ticker_bid, ticker_ask, ticker_fair

    def depth(self, symbol, limit=50):
        self.calls.append("depth")
        return 1, ((76000.0, 50),), ((76000.1, 50),)

    def klines_1m(self, symbol, start_s, end_s):
        self.calls.append("klines")
        return [(i * 60, 76000.0, 76030.0, 75970.0, 76000.0, 1.0) for i in range(20)]

    def funding(self, symbol):
        self.calls.append("funding")
        return 0.0001, None

    def ticker(self, symbol):
        self.calls.append("ticker")
        if self.ticker_error:
            raise KcexError("down", {"status": None})
        return FutTicker(0, self.ticker_bid, self.ticker_bid, self.ticker_ask,
                         self.ticker_fair, self.ticker_fair, 0.0001)

    def contract_detail(self, symbol):
        return SPEC


class FakeJev:
    name = "jev-1.13.0"

    def __init__(self, verdict):
        self.verdict, self.calls = verdict, 0

    def evaluate(self, snap, position, *, now_ms):
        self.calls += 1
        return replace(self.verdict, state=jev_state(snap, position, now_ms=now_ms))


def sync_submit(fn, *args):
    future = Future()
    future.set_result(fn(*args))
    return future


def build(tmp_path, *, action="LONG", rest=None, verdict=UP, settings=None):
    store = FutStore(tmp_path / "fut.db")
    events, clock, calls = queue.SimpleQueue(), Clock(), []

    def decide(state, *, has_position, settings, budget, store):
        calls.append(has_position)
        return LlmDecision(intent=FutIntent(action, 0.8, "flow"), reason="ok", cost_usd=0.001)

    loop = FutLoop(settings=settings or FutSettings(), store=store, spec=SPEC, rest=rest or FakeRest(), jev=FakeJev(verdict),
                   budget=Budget(0.0, 1.0, "2026-09-17"), events=events, clock_ms=clock, llm_decide=decide,
                   submit=sync_submit, store_factory=lambda: store)
    return loop, store, events, clock, calls


def ws_tick(events):
    events.put(("ws", FutTicker(0, 76000.0, 76000.0, 76000.1, 76000.0, 76000.0, 0.0001)))


def open_long(loop):
    notional = 2 * 0.0001 * 76000.0
    loop.ledger.open(FutGate(True, "ok_open", "LONG", side="long", contracts=2, price=76000.0, notional=notional,
                             margin=notional, stop=75900.0, liq=380.0, leverage=1), now_ms=T0)


def test_jev_wake_calls_llm_and_opens_long(tmp_path):
    loop, store, events, _, calls = build(tmp_path)
    ws_tick(events)
    loop.step()
    loop.step()
    assert loop.ledger.position.side == "long"
    assert calls == [False]
    jev = store.decisions("jev")[0]["payload"]
    assert (jev["wake"], jev["dispatch"]) == ("entry_signal", "dispatched")
    assert jev["cost_usd"] == pytest.approx(1000 / 1e6 * 0.042)
    llm = store.decisions("llm")[0]["payload"]
    assert (llm["verdict"], llm["outcome"], llm["cost_usd"]) == ("ok", "opened", 0.001)
    assert loop.llm_entries == 1


def test_entry_wake_requires_same_side_streak_and_logs_it(tmp_path):
    loop, store, _, _, calls = build(tmp_path, settings=FutSettings(wake_streak=2))
    snap = make_snap(ts_ms=T0)
    loop._jev(snap, T0)
    assert calls == []
    assert store.decisions("jev")[0]["payload"]["streak"] == 1
    loop._jev(snap, T0 + 2_000)
    assert calls == [False]
    assert store.decisions("jev")[1]["payload"]["streak"] == 2
    assert store.decisions("jev")[1]["payload"]["wake"] == "entry_signal"


def test_entry_streak_resets_on_nonqualifying_error_side_change_open_and_stale(tmp_path):
    loop, store, _, _, _ = build(tmp_path, settings=FutSettings(wake_streak=3))
    snap = make_snap(ts_ms=T0)
    loop._jev(snap, T0)
    assert loop._entry_streak == 1
    loop.jev.verdict = replace(UP, direction="flat")
    loop._jev(snap, T0 + 2_000)
    assert loop._entry_streak == 0
    loop.jev.verdict = replace(UP, error="timeout")
    loop._jev(snap, T0 + 4_000)
    assert loop._entry_streak == 0
    loop.jev.verdict = replace(UP, direction="down")
    loop._jev(snap, T0 + 6_000)
    assert (loop._entry_side, loop._entry_streak) == ("short", 1)
    open_long(loop)
    loop._jev(snap, T0 + 8_000)
    assert loop._entry_streak == 0
    loop.ledger.close(loop.ledger.market_exit_price(snap), now_ms=T0 + 9_000, reason="test")
    loop.jev.verdict = UP
    loop._jev(snap, T0 + 10_000)
    assert loop._entry_streak == 1
    loop._jev(replace(snap, stale=True), T0 + 12_000)
    assert loop._entry_streak == 0
    assert len(store.decisions("jev")) == 6


def test_jev_audit_keeps_main_position_state_for_replay(tmp_path):
    loop, store, _, _, _ = build(tmp_path)
    open_long(loop)

    loop._jev(make_snap(ts_ms=T0), T0)

    assert store.decisions("jev")[0]["payload"]["state"]["position"]["side"] == "long"


def test_random_entry_rate_uses_persisted_main_opens_and_real_jev_rows(tmp_path):
    loop, store, _, _, _ = build(tmp_path)
    for index in range(4):
        store.log_decision("jev", {"model": "jev-real", "error": None}, ts_ms=T0 + index)
    for index in range(2):
        store.add_fut_fill("main", ts_ms=T0 + index, kind="open", side="long", contracts=1, price=1.0,
                           fee=0.0, funding=0.0, pnl=0.0, reason="entry")

    assert loop._random_entry_rate() == 0.5


def test_jev_audit_records_effective_random_seed(tmp_path):
    loop, store, _, _, _ = build(tmp_path)

    loop._jev(make_snap(ts_ms=T0), T0)

    assert store.decisions("jev")[0]["payload"]["random_seed"] == loop.shadow.effective_seed


def test_cost_gate_blocks_flat_entry_wake_before_llm_and_is_logged(tmp_path):
    settings = FutSettings(max_spread_bps=0.01)
    loop, store, _, _, calls = build(tmp_path, settings=settings)
    loop._jev(make_snap(ts_ms=T0), T0)
    payload = store.decisions("jev")[0]["payload"]
    assert calls == []
    assert payload["wake"] is None and payload["gate"] == "spread_too_wide"


def test_atr_gate_blocks_entry_wake_before_llm(tmp_path):
    loop, store, _, _, calls = build(tmp_path)

    loop._jev(make_snap(ts_ms=T0, atr_1m=None), T0)

    payload = store.decisions("jev")[0]["payload"]
    assert calls == []
    assert payload["wake"] is None and payload["gate"] == "atr"


def test_hold_trades_nothing(tmp_path):
    loop, store, events, _, _ = build(tmp_path, action="HOLD")
    ws_tick(events)
    loop.step()
    loop.step()
    assert not loop.ledger.position.is_open()
    assert store.decisions("llm")[0]["payload"]["outcome"] == "ok"


def test_llm_close_on_exit_signal(tmp_path):
    exit_verdict = replace(UP, direction="flat", exit_now=0.9)
    loop, store, events, _, calls = build(tmp_path, action="CLOSE", verdict=exit_verdict)
    open_long(loop)
    ws_tick(events)
    loop.step()
    loop.step()
    assert not loop.ledger.position.is_open()
    assert calls == [True]
    assert store.decisions("llm")[0]["payload"]["outcome"] == "closed"


def test_exit_signal_inside_min_hold_does_not_wake_llm(tmp_path):
    exit_verdict = replace(UP, direction="flat", exit_now=0.9)
    loop, store, events, clock, calls = build(tmp_path, action="CLOSE", verdict=exit_verdict,
                                              settings=FutSettings(min_hold_s=60.0))
    open_long(loop)
    clock.now = T0 + 30_000
    ws_tick(events)
    loop.step()
    loop.step()
    assert loop.ledger.position.is_open()
    assert calls == []
    assert store.decisions("jev")[0]["payload"]["wake"] is None


def test_entry_after_price_moved_is_logged_not_traded(tmp_path):
    loop, store, events, _, _ = build(tmp_path)
    ws_tick(events)
    loop.step()
    events.put(("ws", FutDepth(0, 2, ((76000.0, 0), (76100.0, 50)), ((76000.1, 0), (76100.1, 50)))))
    loop.step()
    assert not loop.ledger.position.is_open()
    assert store.decisions("llm")[0]["payload"]["verdict"] == "stale_price"


def test_stale_ws_uses_rest_prices_but_skips_jev_when_flat(tmp_path):
    rest = FakeRest(ticker_bid=97.95, ticker_ask=98.05, ticker_fair=76000.0)
    loop, _, _, _, _ = build(tmp_path, rest=rest)
    loop.step()
    assert {"depth", "klines", "funding", "ticker"} <= set(rest.calls)
    snapshot = loop.market.snapshot(T0)
    assert (snapshot.bid, snapshot.ask) == (97.95, 98.05)
    assert loop.jev.calls == 0


def test_time_limit_exit_uses_fresh_rest_quotes_when_ws_is_stale(tmp_path):
    rest = FakeRest(ticker_bid=97.95, ticker_ask=98.05)
    loop, store, _, clock, _ = build(tmp_path, rest=rest)
    loop.ledger.open(FutGate(True, "ok_open", "LONG", side="long", contracts=2, price=76000.0,
                             notional=2 * 0.0001 * 76000.0, margin=15.2, stop=1.0, liq=1.0, leverage=1),
                     now_ms=T0)
    clock.now = T0 + 301_000

    loop.step()

    close = next(fill for fill in store.fut_fills("main") if fill["kind"] == "close")
    assert close["reason"] == "time_limit"
    assert close["price"] == pytest.approx(97.95 * (1 - 0.0002))


def test_unmonitored_open_position_halts(tmp_path):
    loop, store, _, _, _ = build(tmp_path, rest=FakeRest(ticker_error=True))
    open_long(loop)
    with pytest.raises(Unmonitored):
        loop.step()
    assert store.decisions("unmonitored")


def test_seed_budget_resumes_same_day_and_blocks_corrupt_state(tmp_path):
    store = FutStore(tmp_path / "fut.db")
    llm = Settings.from_env()
    store.kv_set("llm_budget", json.dumps({"day": "2026-09-17", "spent_usd": 0.3, "calls": 2}))
    resumed = seed_budget(store, llm, today="2026-09-17")
    assert (resumed.spent_usd, resumed.calls, resumed.blocked_reason) == (0.3, 2, None)
    assert seed_budget(store, llm, today="2026-09-18").spent_usd == 0.0
    store.kv_set("llm_budget", "not json")
    assert seed_budget(store, llm, today="2026-09-17").blocked_reason == REASON_BUDGET_STATE
