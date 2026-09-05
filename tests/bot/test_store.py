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
    store.add_fill("2026-09-04", 1.0, side="SELL", qty=0.00025, price=81000.0)
    assert store.fills(1)[0]["side"] == "SELL"
