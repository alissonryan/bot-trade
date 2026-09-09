import sqlite3
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.store import Store
from bot.types import GateResult, TradeIntent


def test_audit_and_bot_order_ids(tmp_path):
    db = tmp_path / "bot.db"
    store = Store(db)
    store.append_audit(
        intent=TradeIntent("BUY", 0.5, "x", "trend"),
        gate=GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79000.00"),
        mode="paper",
        order_id="bot-1",
    )
    store.remember_order("bot-1")
    store.remember_order("bot-stop-1")
    assert store.is_bot_order("bot-1") is True
    assert store.is_bot_order("C02__723550870020620296064") is False
    assert store.day_pnl("2026-09-04") == 0.0
    store.add_fill("2026-09-04", 1.5)
    store.add_fill("2026-09-04", -0.5)
    assert store.day_pnl("2026-09-04") == 1.0


def test_audit_keeps_snapshot_and_llm_context(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.append_audit(
        intent=TradeIntent("HOLD", 0.0, "llm_timeout", "unknown"),
        gate=GateResult(False, "hold", "HOLD"),
        mode="paper",
        snapshot={"last": 80000.0, "bid": 79999.0, "ask": 80001.0, "atr": 400.0, "ts_ms": 1},
        llm={"reason": "llm_timeout", "cost_usd": 0.0},
    )
    row = store.recent_audit(1)[0]
    assert row["rule"] == "hold"
    assert row["payload"]["snapshot"]["last"] == 80000.0
    assert row["payload"]["llm"]["reason"] == "llm_timeout"


def test_fill_details_are_stored(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.add_fill("2026-09-04", -0.8, side="SELL", qty=0.00025, price=79200.0, fee=0.0, order_id="oid-t", source="stop")
    fill = store.fills(1)[0]
    assert fill["side"] == "SELL"
    assert fill["qty"] == 0.00025
    assert fill["price"] == 79200.0
    assert fill["order_id"] == "oid-t"
    assert fill["source"] == "stop"
    assert fill["ts"]


def test_position_roundtrip(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.save_position(qty=0.00025, entry=80010.0, stop_price=79200.0, entry_order_id="oid-m", stop_order_id="oid-t")
    row = store.load_position()
    assert row is not None
    assert row["qty"] == 0.00025
    assert row["entry"] == 80010.0
    assert row["stop_price"] == 79200.0
    assert row["entry_order_id"] == "oid-m"
    assert row["stop_order_id"] == "oid-t"
    assert row["state"] == "OPEN"
    assert row["opened_ts"]
    store.clear_position()
    assert store.load_position() is None


def test_barrier_metadata_and_entry_age_survive_updates_and_restart(tmp_path, monkeypatch):
    db = tmp_path / "barriers.db"
    store = Store(db)
    kwargs = dict(qty=.00025, entry=80000, stop_price=79200, entry_order_id="entry", stop_order_id="stop")
    store.save_position(**kwargs, opened_ts="2026-01-01T00:00:00+00:00", take_profit_price=81200)
    monkeypatch.setattr("bot.store._now_iso", lambda: "2026-01-02T00:00:00+00:00")
    store.save_position(**kwargs, state="CLOSING", take_profit_price=81200, exit_reason="time_limit")
    row = Store(db).load_position()
    assert row["opened_ts"] == "2026-01-01T00:00:00+00:00"
    assert row["take_profit_price"] == 81200
    assert row["exit_reason"] == "time_limit"


def test_position_state_and_provenance(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.save_position(
        qty=0.00025, entry=80010.0, stop_price=None, entry_order_id="oid-m", stop_order_id=None,
        state="PENDING", entry_source="estimated", btc_before=0.00064,
    )
    row = store.load_position()
    assert row["state"] == "PENDING"
    assert row["entry_source"] == "estimated"
    assert row["btc_before"] == 0.00064
    store.set_position_state("UNPROTECTED")
    assert store.load_position()["state"] == "UNPROTECTED"
    with pytest.raises(ValueError):
        store.set_position_state("WEIRD")


def test_kv(tmp_path):
    store = Store(tmp_path / "bot.db")
    assert store.kv_get("paper_cash") is None
    store.kv_set("paper_cash", "450.0")
    store.kv_set("paper_cash", "430.5")
    assert store.kv_get("paper_cash") == "430.5"


def test_migrates_database_from_previous_schema(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY, ts TEXT, action TEXT, ok INTEGER, rule TEXT, payload TEXT)")
    conn.execute("CREATE TABLE bot_orders (order_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE fills (id INTEGER PRIMARY KEY, day TEXT, pnl REAL)")
    conn.execute(
        "CREATE TABLE position (id INTEGER PRIMARY KEY CHECK (id = 1), qty REAL, entry REAL, stop_price REAL, entry_order_id TEXT, stop_order_id TEXT)"
    )
    conn.execute("INSERT INTO fills(day, pnl) VALUES ('2026-09-03', 2.5)")
    conn.execute("INSERT INTO position VALUES (1, 0.00025, 80000.0, 79200.0, 'oid-m', 'oid-t')")
    conn.commit()
    conn.close()

    store = Store(db)
    assert store.day_pnl("2026-09-03") == 2.5
    row = store.load_position()
    assert row["qty"] == 0.00025
    assert row["state"] == "OPEN"  # legacy rows were always a protected long
    assert row["btc_before"] is None
    assert row["take_profit_price"] is None
    assert row["exit_reason"] is None
    store.add_fill("2026-09-04", 1.0, side="SELL", qty=0.00025, price=81000.0)
    assert store.fills(1)[0]["side"] == "SELL"


def test_budget_load_returns_none_only_when_never_written(tmp_path):
    store = Store(tmp_path / "budget.db")
    assert store.budget_load() is None
    store.reserve_budget(today="2026-09-08", cap_usd=10, reserve_usd=1.0)
    assert store.budget_load() == {"day": "2026-09-08", "spent_usd": 1.0, "calls": 1}


def test_budget_load_raises_on_corrupt_or_invalid_row(tmp_path):
    """Finding 1 (re-review): a corrupt/unreadable row must never be
    silently read as a fresh zero budget -- that is exactly how a spend cap
    gets bypassed. Every unreadable shape raises BudgetStateCorrupt."""
    from bot.store import BudgetStateCorrupt
    store = Store(tmp_path / "budget.db")
    for bad in (
        "not json",
        '{"day": "2026-09-08"}',                                    # missing spent_usd
        '{"day": "2026-09-08", "spent_usd": "nope"}',               # wrong type
        '{"day": "2026-09-08", "spent_usd": -1}',                   # negative
        '{"day": "2026-09-08", "spent_usd": NaN}',                  # non-finite
        "[]",                                                        # not an object
    ):
        store.kv_set("llm_budget", bad)
        with pytest.raises(BudgetStateCorrupt):
            store.budget_load()


def test_reserve_budget_commits_before_dispatch_and_respects_cap(tmp_path):
    store = Store(tmp_path / "budget.db")
    assert store.reserve_budget(today="2026-09-08", cap_usd=0.05, reserve_usd=0.02) is True
    assert store.budget_load() == {"day": "2026-09-08", "spent_usd": 0.02, "calls": 1}
    assert store.reserve_budget(today="2026-09-08", cap_usd=0.05, reserve_usd=0.02) is True
    assert store.budget_load()["spent_usd"] == pytest.approx(0.04)
    # A third reservation would exceed the cap (0.04 + 0.02 > 0.05): refused,
    # and refusing must not write anything -- the persisted state is unchanged.
    assert store.reserve_budget(today="2026-09-08", cap_usd=0.05, reserve_usd=0.02) is False
    assert store.budget_load()["spent_usd"] == pytest.approx(0.04)
    assert store.budget_load()["calls"] == 2


def test_reserve_budget_resumes_same_day_and_resets_new_day(tmp_path):
    store = Store(tmp_path / "budget.db")
    store.reserve_budget(today="2026-09-08", cap_usd=10, reserve_usd=1.0)
    # A restart on the SAME day resumes from what was already spent.
    reopened = Store(tmp_path / "budget.db")
    assert reopened.reserve_budget(today="2026-09-08", cap_usd=1.5, reserve_usd=0.4) is True
    assert reopened.budget_load()["spent_usd"] == pytest.approx(1.4)
    # A genuinely later UTC day starts at zero -- ordinary rollover, not a bug.
    assert reopened.reserve_budget(today="2026-09-09", cap_usd=1.0, reserve_usd=0.9) is True
    assert reopened.budget_load() == {"day": "2026-09-09", "spent_usd": 0.9, "calls": 1}


def test_reserve_budget_refuses_backward_clock_instead_of_minting_fresh_budget(tmp_path):
    """A persisted day AFTER 'today' (system clock moved backward, or a
    corrupted/future stored day) must never be read as 'a new day, start at
    zero' -- that would let a clock rollback bypass the cap entirely."""
    from bot.store import BudgetStateCorrupt
    store = Store(tmp_path / "budget.db")
    store.reserve_budget(today="2026-09-10", cap_usd=10, reserve_usd=1.0)
    with pytest.raises(BudgetStateCorrupt):
        store.reserve_budget(today="2026-09-08", cap_usd=10, reserve_usd=1.0)
    # Refusing must not have written anything for the earlier "today".
    assert store.budget_load() == {"day": "2026-09-10", "spent_usd": 1.0, "calls": 1}


def test_settle_budget_trues_up_reservation_to_real_cost(tmp_path):
    store = Store(tmp_path / "budget.db")
    store.reserve_budget(today="2026-09-08", cap_usd=10, reserve_usd=0.02)
    store.settle_budget(day="2026-09-08", delta_usd=0.001 - 0.02)  # real cost 0.001
    assert store.budget_load()["spent_usd"] == pytest.approx(0.001)


def test_settle_budget_ignores_a_day_that_already_rolled_over(tmp_path):
    store = Store(tmp_path / "budget.db")
    store.reserve_budget(today="2026-09-08", cap_usd=10, reserve_usd=0.02)
    store.reserve_budget(today="2026-09-09", cap_usd=10, reserve_usd=0.5)  # new day supersedes it
    store.settle_budget(day="2026-09-08", delta_usd=-0.019)  # stale settlement for the old day
    assert store.budget_load() == {"day": "2026-09-09", "spent_usd": 0.5, "calls": 1}


def test_budget_load_raises_on_a_non_canonical_day(tmp_path):
    """Main's boundary proof: a bare string compare ('0' < '2026-09-09')
    silently treated a corrupt day as 'earlier day, start fresh'. A
    non-canonical day must raise, never be read as a rollover."""
    from bot.store import BudgetStateCorrupt
    store = Store(tmp_path / "budget.db")
    for bad_day in ("0", "2026-13-40", "20260908", "", "2026/09/08", "2026-09-08T00:00:00"):
        store.kv_set("llm_budget", '{"day": %r, "spent_usd": 0.02, "calls": 1}' % bad_day)
        with pytest.raises(BudgetStateCorrupt):
            store.budget_load()


def test_budget_load_rejects_bool_calls_and_string_calls(tmp_path):
    from bot.store import BudgetStateCorrupt
    store = Store(tmp_path / "budget.db")
    for bad in (
        '{"day": "2026-09-08", "spent_usd": 0.02, "calls": true}',
        '{"day": "2026-09-08", "spent_usd": 0.02, "calls": "3"}',
        '{"day": "2026-09-08", "spent_usd": 0.02, "calls": 2.5}',
        '{"day": "2026-09-08", "spent_usd": 0.02, "calls": -1}',
    ):
        store.kv_set("llm_budget", bad)
        with pytest.raises(BudgetStateCorrupt):
            store.budget_load()


def test_reserve_budget_rejects_invalid_today_cap_and_reserve(tmp_path):
    store = Store(tmp_path / "budget.db")
    with pytest.raises(ValueError):
        store.reserve_budget(today="0", cap_usd=10, reserve_usd=1.0)
    with pytest.raises(ValueError):
        store.reserve_budget(today="2026-09-08", cap_usd=float("nan"), reserve_usd=1.0)
    with pytest.raises(ValueError):
        store.reserve_budget(today="2026-09-08", cap_usd=-1, reserve_usd=1.0)
    with pytest.raises(ValueError):
        store.reserve_budget(today="2026-09-08", cap_usd=10, reserve_usd=0.0)
    with pytest.raises(ValueError):
        store.reserve_budget(today="2026-09-08", cap_usd=10, reserve_usd=float("inf"))
    with pytest.raises(ValueError):
        store.reserve_budget(today="2026-09-08", cap_usd=10, reserve_usd=-1.0)
    # None of the rejected calls wrote anything.
    assert store.budget_load() is None


def test_reserve_budget_required_headroom_is_checked_but_not_charged(tmp_path):
    """Reflection reserving its own call plus the next decision's headroom:
    the headroom must block admission when it would blow the cap, but only
    reserve_usd is ever actually committed to spent_usd."""
    store = Store(tmp_path / "budget.db")
    # cap 0.05: 0.02 reserve + 0.04 headroom > cap -- refused, nothing written.
    assert store.reserve_budget(today="2026-09-08", cap_usd=0.05, reserve_usd=0.02,
                                 required_headroom=0.04) is False
    assert store.budget_load() is None
    # cap 0.10: 0.02 + 0.04 fits -- admitted, but only 0.02 is charged.
    assert store.reserve_budget(today="2026-09-08", cap_usd=0.10, reserve_usd=0.02,
                                 required_headroom=0.04) is True
    assert store.budget_load()["spent_usd"] == pytest.approx(0.02)


def test_reserve_budget_refuses_inside_an_already_open_caller_transaction(tmp_path):
    """Main's boundary proof: reserve_budget() returned True while writing
    into the caller's own uncommitted transaction -- invisible to any other
    connection until the caller committed, so the return value lied about
    atomicity. It must now refuse up front, before touching kv at all, and
    must not disturb the caller's pending transaction."""
    from bot.store import BudgetTransactionConflict
    db_path = tmp_path / "budget.db"
    store = Store(db_path)
    store.kv_set("unrelated", "pending-value", commit=False)
    assert store._conn.in_transaction is True
    with pytest.raises(BudgetTransactionConflict):
        store.reserve_budget(today="2026-09-08", cap_usd=10, reserve_usd=1.0)
    # The caller's own pending write is untouched -- neither committed nor rolled back.
    assert store._conn.in_transaction is True
    assert store.kv_get("unrelated") == "pending-value"
    # A separate connection sees none of it: truly not committed.
    observer = sqlite3.connect(db_path)
    row = observer.execute("SELECT value FROM kv WHERE key='llm_budget'").fetchone()
    assert row is None
    observer.close()
    store.rollback()


def test_settle_budget_refuses_inside_an_already_open_caller_transaction(tmp_path):
    from bot.store import BudgetTransactionConflict
    store = Store(tmp_path / "budget.db")
    store.reserve_budget(today="2026-09-08", cap_usd=10, reserve_usd=1.0)
    store.kv_set("unrelated", "pending-value", commit=False)
    with pytest.raises(BudgetTransactionConflict):
        store.settle_budget(day="2026-09-08", delta_usd=-0.5)
    assert store.kv_get("unrelated") == "pending-value"
    store.rollback()


def test_settle_budget_raises_instead_of_swallowing_a_corrupt_same_day_row(tmp_path):
    """A row that is corrupt AT SETTLE TIME (something else wrote garbage
    between reserve and settle) must raise, not be silently treated as
    'nothing to settle' -- that would lose a known real-cost correction and
    let the process keep authorizing spend against an understated ledger."""
    from bot.store import BudgetStateCorrupt
    store = Store(tmp_path / "budget.db")
    store.kv_set("llm_budget", '{"day": "2026-09-08", "spent_usd": "not-a-number"}')
    with pytest.raises(BudgetStateCorrupt):
        store.settle_budget(day="2026-09-08", delta_usd=-0.01)
