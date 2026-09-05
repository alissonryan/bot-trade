from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.brain import Budget, ThinkResult
from bot.cycle import SessionDead, due, run_once, unrealized_pnl
from bot.hands import PaperHands, Position, UnprotectedPosition
from bot.settings import Settings
from bot.store import Store
from bot.types import Bar, GateResult, Snapshot, TradeIntent


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
        self.rules = None

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
    store = Store(tmp_path / "c.db")

    class DeadClient:
        def user_info(self):
            raise RuntimeError("401")

    class Hands:
        position = Position()

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
    store = Store(tmp_path / "c.db")

    class OkClient:
        def user_info(self):
            return {"code": 0}

    class Hands:
        position = Position()
        entry_order_id = None
        stop_order_id = None
        reconciled = 0

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
