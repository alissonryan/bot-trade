from pathlib import Path
from unittest.mock import Mock
import sys
import time
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.brain import Budget, ThinkResult
from bot.cycle import SessionDead, due, run_once, unrealized_pnl
from bot.hands import LiveHands, PaperHands, Position, UnprotectedPosition
from bot.ratelimit import PROTECTIVE, WriteMeter, WriteStormHalt
from bot.settings import Settings
from bot.store import Store
from bot.types import Bar, GateResult, Snapshot, SymbolRules, TradeIntent


def _settings(**kw) -> Settings:
    d = Settings.from_env().__dict__.copy()
    d.update(kw)
    return Settings(**d)


def test_due_on_timer():
    s = Settings.from_env()
    assert due(now_ms=1_000_000, last_llm_ms=1_000_000 - 15 * 60_000, last_px=100, px=100, settings=s) is True


def test_not_due_early():
    s = Settings.from_env()
    assert due(now_ms=1_000_000, last_llm_ms=1_000_000 - 60_000, last_px=100, px=100, settings=s) is False


def test_due_on_wake_move():
    s = Settings.from_env()
    assert due(now_ms=1_000_000, last_llm_ms=1_000_000 - 1000, last_px=100, px=100.5, settings=s) is True


class FakeEye:
    def __init__(self, last=100.0):
        self.quotes = 0
        self.heavy = 0
        self.last = last
        self.free_usdt = 450.0
        self.bot_qty = 0.0
        self.bot_avg_entry = None
        self.last_intent_action = None
        self.last_bot_pnl_usdt = 0.0
        self.rules: SymbolRules | None = None

    def poll_quotes(self):
        self.quotes += 1
        return True

    def poll_heavy(self):
        self.heavy += 1

    def health(self):
        return {"ws_ok": False}

    def snapshot(self):
        return Snapshot(
            ts_ms=1,
            last=self.last,
            bid=self.last - 1,
            ask=self.last + 1,
            spread=2,
            bars_15m=[Bar(t=i, o=100, h=101, l=99, c=100) for i in range(20)],
            atr=1.0,
            free_usdt=self.free_usdt,
            bot_qty=self.bot_qty,
            bot_avg_entry=self.bot_avg_entry,
            ws_ok=False,
            stale=False,
        )


class NoClient:
    pass


def test_journal_budget_skip_is_audited_after_decision_and_execution(tmp_path):
    from bot.brain import reflect_result
    settings = _settings(mode="paper", journal_enabled=True, llm_fallback_cost_usd=.02,
                         llm_model="test", openrouter_api_key="test")
    store = Store(tmp_path / "journal-cycle.db", mode='paper')
    jid = store.journal_begin(TradeIntent("BUY", 1, "old thesis", "range"), {}, decision_ms=0)
    store.journal_resolve(jid, {"kind": "closed", "realized_pnl_usdt": 1}, known_ms=1)
    hands = PaperHands(settings, store)
    budget = Budget(.97, 1, "")
    budget.day = __import__('bot.cycle', fromlist=['utc_day']).utc_day()
    called = []
    def think(snap, settings, budget, **context):
        called.append("decision")
        assert context['lessons'][0]['id'] == jid
        return ThinkResult(TradeIntent("BUY", 1, "new thesis", "range"), "ok")
    def reflect(lesson, settings, budget):
        assert hands.position.qty > 0  # execution is already complete
        called.append("reflection")
        return reflect_result(lesson, settings, budget,
                              http_post=Mock(side_effect=AssertionError("no budget for reflection")))
    _, _, gate = run_once(settings=settings, eye=FakeEye(), store=store, client=NoClient(), hands=hands,  # type: ignore[arg-type]
                         budget=budget, last_llm_ms=0, last_px=0, think=think, reflect=reflect)
    assert gate is not None and gate.ok
    assert called == ["decision", "reflection"]
    assert store.recent_audit(1)[0]['payload']['reflection']['reason'] == 'reflection_budget'
    assert store.journal_get(jid)['reflection_audit']['reason'] == 'reflection_budget'


def test_journal_known_unexecuted_paper_buy_has_no_return(tmp_path):
    settings = _settings(mode='paper', journal_enabled=True, paper_starting_usdt=20,
                         max_portfolio_pct=1, paper_slippage_bps=500)
    store = Store(tmp_path / 'cash.db', mode='paper')
    hands = PaperHands(settings, store)
    _, _, gate = run_once(settings=settings, eye=FakeEye(), store=store, client=NoClient(), hands=hands,  # type: ignore[arg-type]
                         budget=Budget(0,2,''), last_llm_ms=0, last_px=0,
                         think=lambda *a, **kw: ThinkResult(TradeIntent('BUY',1,'go','range'),'ok'))
    assert gate is not None and gate.ok  # executable cash check declines due to slippage
    assert hands.position.qty == 0
    row = store.journal_get(store.recent_audit(1)[0]['payload']['journal_id'])
    assert row['outcome']['kind'] == 'not_executed'
    assert row['outcome']['realized_pnl_usdt'] is None
    assert store.fills() == []


def test_journal_write_failure_does_not_attach_trade_to_old_ambiguous_buy(tmp_path, monkeypatch):
    settings = _settings(mode='paper', journal_enabled=True)
    store = Store(tmp_path / 'orphan.db', mode='paper')
    old = store.journal_begin(TradeIntent('BUY',1,'ambiguous','range'),{},decision_ms=0,track_entry=True)
    monkeypatch.setattr('bot.cycle.record_decision',Mock(side_effect=ValueError('journal unavailable')))
    hands = PaperHands(settings,store)
    eye = FakeEye()
    run_once(settings=settings,eye=eye,store=store,client=NoClient(),hands=hands,  # type: ignore[arg-type]
             budget=Budget(0,2,''),last_llm_ms=0,last_px=0,
             think=lambda *a,**kw:ThinkResult(TradeIntent('BUY',1,'new','range'),'ok'))
    hands.execute(GateResult(True,'ok_close','SELL'),eye.snapshot())
    assert store.journal_get(old)['status'] == 'pending'
    assert store.journal_lessons(as_of_ms=10**15) == []


def test_cooldown_survives_process_restart(tmp_path, monkeypatch):
    path = tmp_path / "restart.db"
    # The writer process ends; neither Store nor Hands state survives in RAM.
    subprocess.run([sys.executable, "-c",
                    "from bot.store import Store; from pathlib import Path; import sys; "
                    "s=Store(Path(sys.argv[1])); "
                    "s.add_fill('2026-01-01', -1, side='SELL', qty=.1, price=100, "
                    "ts='2026-01-01T00:00:00+00:00', source='paper_stop')",
                    str(path)], check=True, cwd=ROOT)
    from datetime import datetime, timezone
    exited = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("bot.cycle.time.time", lambda: exited + 60)
    settings = _settings(mode="paper", cooldown_minutes=30)
    store = Store(path, mode='paper')
    hands = PaperHands(settings, store)
    _, _, gate = run_once(
        settings=settings, eye=FakeEye(), store=store, client=NoClient(), hands=hands,  # type: ignore[arg-type]
        budget=Budget(0, 2, ""), last_llm_ms=0, last_px=0,
        think=lambda *args: ThinkResult(TradeIntent("BUY", 1, "go", "range"), "ok"),
    )
    assert gate is not None and gate.rule == "cooldown" and not gate.ok
    assert hands.position.qty == 0
    assert store.recent_audit(1)[0]["rule"] == "cooldown"


@pytest.mark.parametrize("action,minutes", [("SELL", 30), ("BUY", 0)])
def test_exit_and_optout_never_query_cooldown_store(tmp_path, action, minutes):
    settings = _settings(mode="paper", cooldown_minutes=minutes)
    store = Store(tmp_path / "skip.db", mode='paper')
    store.last_loss_exit_ms = Mock(side_effect=AssertionError("must not query cooldown"))
    hands = PaperHands(settings, store)
    eye = FakeEye()
    if action == "SELL":
        hands.execute(GateResult(True, "ok_buy", "BUY", qty=".1", stop_price="90"), eye.snapshot())
    _, _, gate = run_once(
        settings=settings, eye=eye, store=store, client=NoClient(), hands=hands,  # type: ignore[arg-type]
        budget=Budget(0, 2, ""), last_llm_ms=0, last_px=0,
        think=lambda *args: ThinkResult(TradeIntent(action, 1, "go", "range"), "ok"),
    )
    assert gate is not None and gate.ok
    store.last_loss_exit_ms.assert_not_called()


@pytest.mark.parametrize("exit_type", ["llm_sell", "stop", "time_limit"])
def test_real_losing_paper_exit_arms_next_cycle_cooldown(tmp_path, exit_type, monkeypatch):
    from datetime import datetime, timedelta, timezone
    settings = _settings(mode="paper", cooldown_minutes=30,
                         tp_atr_mult=2 if exit_type == "take_profit" else 0,
                         time_limit_minutes=30 if exit_type == "time_limit" else 0)
    store = Store(tmp_path / "real-exit.db", mode='paper')
    hands = PaperHands(settings, store)
    eye = FakeEye()
    if exit_type == "time_limit":
        opened = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        monkeypatch.setattr("bot.hands._opened_now", lambda: opened)
    hands.execute(GateResult(True, "ok_buy", "BUY", qty=".1", stop_price="90"), eye.snapshot())
    if exit_type == "llm_sell":
        hands.execute(GateResult(True, "ok_close", "SELL"), eye.snapshot())
    else:
        eye.last = 80 if exit_type == "stop" else 110 if exit_type == "take_profit" else 100
        hands.mark(eye.snapshot(), now_ms=int(time.time() * 1000))
    assert hands.position.qty == 0
    assert store.fills(1)[0]["side"] == "SELL"
    _, _, gate = run_once(
        settings=settings, eye=eye, store=store, client=NoClient(), hands=hands,  # type: ignore[arg-type]
        budget=Budget(0, 2, ""), last_llm_ms=0, last_px=0,
        think=lambda *args: ThinkResult(TradeIntent("BUY", 1, "go", "range"), "ok"),
    )
    assert gate is not None and gate.rule == "cooldown" and not gate.ok


def test_a_winning_take_profit_does_not_arm_the_cooldown(tmp_path):
    """The continuation case: a profitable exit must leave the next entry free.

    Blocking here is what Rafael Vargas measured as pure lost profit and retired
    (Apex Brief v17, Rule 3). `already_long` and the fresh-signal requirement
    still prevent stacking, so nothing is left unguarded by allowing this.
    """
    settings = _settings(mode="paper", cooldown_minutes=30, tp_atr_mult=2)
    store = Store(tmp_path / "win-exit.db", mode='paper')
    hands = PaperHands(settings, store)
    eye = FakeEye()
    hands.execute(GateResult(True, "ok_buy", "BUY", qty=".1", stop_price="90"), eye.snapshot())
    eye.last = 110  # target crossed: the exit books a profit
    hands.mark(eye.snapshot(), now_ms=int(time.time() * 1000))
    assert hands.position.qty == 0
    booked = store.fills(1)[0]
    assert booked["side"] == "SELL" and booked["pnl"] > 0, "this case must be a win"
    _, _, gate = run_once(
        settings=settings, eye=eye, store=store, client=NoClient(), hands=hands,  # type: ignore[arg-type]
        budget=Budget(0, 2, ""), last_llm_ms=0, last_px=0,
        think=lambda *args: ThinkResult(TradeIntent("BUY", 1, "go", "range"), "ok"),
    )
    assert gate is not None and gate.rule != "cooldown"


@pytest.mark.parametrize("cap,last,minimum,rule", [
    (20, 87000, 20, "min_notional"),
    (0.8, 100000, 1, "dust"),
])
def test_truncated_buy_below_minimum_never_reaches_exchange(tmp_path, cap, last, minimum, rule):
    settings = _settings(mode="live", max_order_usdt=cap)
    eye = FakeEye(last=last)
    eye.rules = SymbolRules(qty_scale=5, min_amount=minimum)
    store = Store(tmp_path / "c.db", mode='live')
    client = Mock()
    client.balances.return_value = {"data": [{"currency": "BTC", "available": "0", "frozen": "0"}]}
    hands = LiveHands(settings, store, client, rules=eye.rules)
    _, _, gate = run_once(
        settings=settings, eye=eye, store=store, client=client, hands=hands,  # type: ignore[arg-type]
        budget=Budget(0, 2, ""), last_llm_ms=0, last_px=0,
        think=lambda *args: ThinkResult(TradeIntent("BUY", 1, "go", "trend"), "ok"),
    )
    assert gate is not None and not gate.ok and gate.rule == rule
    client.place_market.assert_not_called()
    client.place_trigger.assert_not_called()
    assert store.load_position() is None


def test_run_once_polls_quotes_when_not_due(tmp_path):
    s = Settings.from_env()
    eye = FakeEye()
    store = Store(tmp_path / "c.db")
    hands = PaperHands(s, store)
    now = int(time.time() * 1000)
    last_llm, last_px, gate = run_once(
        settings=s, eye=eye, store=store, client=NoClient(), hands=hands,
        budget=Budget(0, 2, "2026-09-04"), last_llm_ms=now, last_px=100.0,
    )
    assert eye.quotes == 1 and eye.heavy == 0
    assert gate is None
    assert last_px == 100.0
    assert last_llm == now


@pytest.mark.parametrize("mode", ["paper", "live"])
@pytest.mark.parametrize("is_due", [False, True])
def test_barrier_tick_runs_before_llm_and_audits_without_reentry(tmp_path, mode, is_due):
    settings = _settings(mode=mode, tp_atr_mult=3)
    store = Store(tmp_path / "barrier.db", mode=mode)
    store.save_position(qty=.00025, entry=80000, stop_price=79200, entry_order_id="e", stop_order_id="s", take_profit_price=81200)
    client = Mock()
    eye = FakeEye(last=81201)
    if mode == "live":
        hands = LiveHands(settings, store, client)
        hands.reconcile = Mock(return_value="ok")
        hands.execute = Mock(side_effect=lambda *args: hands._clear())
    else:
        hands = PaperHands(settings, store)
    thinker = Mock(side_effect=AssertionError("must exit before thinking"))
    last_ms = 0 if is_due else int(time.time() * 1000)
    result = run_once(settings=settings, eye=eye, store=store, client=client, hands=hands,
                      budget=Budget(0, 2, ""), last_llm_ms=last_ms, last_px=81201, think=thinker)
    assert not hands.position.is_open()
    assert result[0] == last_ms  # no LLM invocation to charge/reset
    assert result[2].rule == "take_profit"
    assert eye.bot_qty == 0 and eye.heavy == 0
    thinker.assert_not_called()
    row = store.recent_audit(1)[0]
    assert row["rule"] == "take_profit" and row["payload"]["llm"]["reason"] == "not_called_barrier"
    assert row["payload"]["snapshot"]["bid"] == 81200
    assert row["payload"]["snapshot"]["bot_qty"] == .00025


def test_live_barrier_halt_not_masked_by_audit_outage(tmp_path):
    settings = _settings(mode="live", tp_atr_mult=3)
    store = Store(tmp_path / "barrier.db", mode='live')
    hands = LiveHands(settings, store, Mock())
    def fail(*args, **kwargs):
        hands.last_mark_reason = "take_profit"
        raise UnprotectedPosition("restore failed")
    hands.mark = fail
    store.append_audit = Mock(side_effect=RuntimeError("sqlite unavailable"))
    with pytest.raises(UnprotectedPosition, match="restore failed"):
        run_once(settings=settings, eye=FakeEye(), store=store, client=Mock(), hands=hands,
                 budget=Budget(0, 2, ""), last_llm_ms=0, last_px=0)


@pytest.mark.parametrize("is_due", [False, True])
def test_opt_out_preserves_pre_p1_paper_stop_scheduling(tmp_path, is_due):
    settings = _settings(mode="paper", tp_atr_mult=0, time_limit_minutes=0)
    store = Store(tmp_path / "opt-out.db", mode='paper')
    store.save_position(qty=.00025, entry=80000, stop_price=79200, entry_order_id="e", stop_order_id="s")
    hands = PaperHands(settings, store)
    thinker = Mock(return_value=ThinkResult(TradeIntent("HOLD", 0, "fixed", "range"), "ok"))
    result = run_once(settings=settings, eye=FakeEye(last=79000), store=store, client=Mock(), hands=hands,
                      budget=Budget(0, 2, ""), last_llm_ms=0 if is_due else int(time.time() * 1000),
                      last_px=79000, think=thinker)
    assert hands.position.is_open() == is_due  # legacy marks paper only on idle iterations
    assert thinker.call_count == int(is_due)
    assert len(store.recent_audit(10)) == int(is_due)
    assert (result[2].rule if result[2] else None) == ("hold" if is_due else None)


def test_opt_out_preserves_pre_p1_live_idle_no_mark(tmp_path):
    settings = _settings(mode="live", tp_atr_mult=0, time_limit_minutes=0)
    store = Store(tmp_path / "opt-out.db", mode='live')
    client = Mock()
    hands = LiveHands(settings, store, client)
    hands.mark = Mock(side_effect=AssertionError("opt-out live tick must not change behavior"))
    run_once(settings=settings, eye=FakeEye(), store=store, client=client, hands=hands,
             budget=Budget(0, 2, ""), last_llm_ms=int(time.time() * 1000), last_px=100)
    assert client.mock_calls == []


def test_llm_cycle_without_key_audits_the_reason(tmp_path):
    s = _settings(openrouter_api_key="", llm_model="")
    eye = FakeEye()
    store = Store(tmp_path / "c.db")
    hands = PaperHands(s, store)
    _, _, gate = run_once(
        settings=s, eye=eye, store=store, client=NoClient(), hands=hands,
        budget=Budget(0, 2, "2026-09-04"), last_llm_ms=0, last_px=0.0,
    )
    assert gate is not None and gate.rule == "hold"
    row = store.recent_audit(1)[0]
    assert row["payload"]["llm"]["reason"] == "llm_config"
    assert row["payload"]["intent"]["reason"] == "llm_config"
    assert row["payload"]["snapshot"]["last"] == 100.0
    assert row["payload"]["position_state"] == "FLAT"
    assert row["payload"]["eye"] == {"ws_ok": False}
    assert eye.free_usdt == hands.cash  # paper cash drives sizing, not the KCEX balance


def test_buy_intent_executes_and_records_order_id(tmp_path):
    s = _settings(paper_starting_usdt=450.0)
    eye = FakeEye(last=80_000.0)
    store = Store(tmp_path / "c.db")
    hands = PaperHands(s, store)

    def think(snap, settings, budget):
        return ThinkResult(TradeIntent("BUY", 0.9, "go", "trend"), "ok", cost_usd=0.001, cost_source="usage")

    _, _, gate = run_once(
        settings=s, eye=eye, store=store, client=NoClient(), hands=hands,
        budget=Budget(0, 2, "2026-09-04"), last_llm_ms=0, last_px=0.0, think=think,
    )
    assert gate is not None and gate.ok and gate.rule == "ok_buy"
    assert hands.position.qty == 0.00025
    assert eye.bot_qty == 0.00025
    row = store.recent_audit(1)[0]
    assert row["payload"]["order_id"] == "paper-entry"
    assert row["payload"]["llm"]["cost_usd"] == 0.001


def test_exec_error_is_audited_then_raised(tmp_path):
    s = _settings()
    eye = FakeEye(last=80_000.0)
    store = Store(tmp_path / "c.db")

    class BoomHands(PaperHands):
        def execute(self, gate, snap):
            raise UnprotectedPosition("no stop")

    hands = BoomHands(s, store)

    def think(snap, settings, budget):
        return ThinkResult(TradeIntent("BUY", 0.9, "go", "trend"), "ok")

    with pytest.raises(UnprotectedPosition):
        run_once(
            settings=s, eye=eye, store=store, client=NoClient(), hands=hands,
            budget=Budget(0, 2, "2026-09-04"), last_llm_ms=0, last_px=0.0, think=think,
        )
    row = store.recent_audit(1)[0]
    assert row["payload"]["exec_error"].startswith("UnprotectedPosition")


def test_live_dead_session_raises_before_thinking(tmp_path):
    s = _settings(mode="live")
    eye = FakeEye()
    store = Store(tmp_path / "c.db", mode='live')

    class DeadClient:
        def user_info(self):
            raise RuntimeError("401")

    class Hands:
        position = Position()

        def mark(self, snap, **kwargs):
            return self.position

        def reconcile(self):
            raise AssertionError("must not reconcile on a dead session")

    with pytest.raises(SessionDead):
        run_once(
            settings=s, eye=eye, store=store, client=DeadClient(), hands=Hands(),
            budget=Budget(0, 2, "2026-09-04"), last_llm_ms=0, last_px=0.0,
        )


def test_live_reconcile_runs_every_llm_cycle(tmp_path):
    s = _settings(mode="live", openrouter_api_key="", llm_model="")
    eye = FakeEye()
    store = Store(tmp_path / "c.db", mode='live')

    class OkClient:
        def user_info(self):
            return {"code": 0}

    class Hands:
        position = Position()
        entry_order_id = None
        stop_order_id = None
        reconciled = 0

        def mark(self, snap, **kwargs):
            return self.position

        def reconcile(self):
            self.reconciled += 1
            return "ok"

        def execute(self, gate, snap):
            raise AssertionError("HOLD must not execute")

    hands = Hands()
    run_once(
        settings=s, eye=eye, store=store, client=OkClient(), hands=hands,
        budget=Budget(0, 2, "2026-09-04"), last_llm_ms=0, last_px=0.0,
    )
    assert hands.reconciled == 1


def test_unrealized_pnl_feeds_the_collar():
    hands = PaperHands.__new__(PaperHands)
    hands.position = Position(qty=0.001, entry=80_000.0)
    assert unrealized_pnl(hands, 79_000.0) == pytest.approx(-1.0)
    hands.position = Position()
    assert unrealized_pnl(hands, 79_000.0) == 0.0


def test_audit_failure_does_not_swallow_the_unprotected_halt(tmp_path):
    """Finding 8: the audit write lives in a bare `finally`, so an exception there
    replaces an in-flight UnprotectedPosition. _loop then matches the generic
    `except Exception` branch, backs off and keeps trading instead of halting
    with EXIT_UNPROTECTED -- the exact failure the invariant exists to catch."""
    s = _settings(mode="paper")
    store = Store(tmp_path / "c.db", mode='paper')
    eye = FakeEye()

    class BoomHands(PaperHands):
        def execute(self, gate, snap):
            raise UnprotectedPosition("no stop")

    def boom_audit(*a, **kw):
        raise RuntimeError("audit table is locked")

    store.append_audit = boom_audit

    def think(snap, settings, budget):
        return ThinkResult(TradeIntent("BUY", 0.9, "go", "trend"), "ok")

    with pytest.raises(UnprotectedPosition):
        run_once(
            settings=s, eye=eye, store=store, client=NoClient(), hands=BoomHands(s, store),
            budget=Budget(0, 2, "2026-09-04"), last_llm_ms=0, last_px=0.0, think=think,
        )


def test_storm_halts_before_the_llm_and_before_any_write(tmp_path):
    # max_writes_per_hour=0 (soft gate off) isolates this test to the hard
    # ceiling alone: a nonzero kill ceiling at or below the soft one is a
    # config error since F5 (settings.py rejects it -- the soft gate would be
    # dead code), which is not what this test is about.
    store = Store(tmp_path / "bot.db", mode="paper")
    settings = _settings(max_writes_per_hour=0, kill_writes_per_hour=1)
    meter = WriteMeter(store, settings)
    meter.record(PROTECTIVE, int(time.time() * 1000))
    hands = PaperHands(settings, store)
    eye = FakeEye()
    called = []
    with pytest.raises(WriteStormHalt):
        run_once(
            settings=settings, eye=eye, store=store, client=NoClient(), hands=hands,
            budget=Budget(0, 2, ""), last_llm_ms=0, last_px=0.0,
            think=lambda *a, **k: called.append("llm"),
            meter=meter,
        )
    assert called == []          # the LLM was never reached


def test_counts_reach_the_collar(tmp_path):
    """A spent write budget must show up as a rate_limit gate in the audit."""
    store = Store(tmp_path / "bot.db", mode="paper")
    settings = _settings(max_writes_per_hour=1, kill_writes_per_hour=0)
    meter = WriteMeter(store, settings)
    meter.record(PROTECTIVE, int(time.time() * 1000))
    hands = PaperHands(settings, store)
    eye = FakeEye()
    _, _, gate = run_once(
        settings=settings, eye=eye, store=store, client=NoClient(), hands=hands,
        budget=Budget(0, 2, ""), last_llm_ms=0, last_px=0.0,
        think=lambda *a, **k: ThinkResult(TradeIntent("BUY", 1.0, "", "trend"), "ok"),
        meter=meter,
    )
    assert gate is not None and gate.rule == "rate_limit"
