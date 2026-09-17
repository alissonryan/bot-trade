import hashlib
import sqlite3

import pytest

from fut.panel.reader import PanelDbBusy, PanelDbMissing, PanelReader
from tests.fut.panel_db import SNAP, add_decision, add_fill, make_db, set_balance, set_position

QUIET = {"wake": None, "error": None, "gate": None, "snapshot": SNAP(bid=100.0, ask=102.0)}


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_missing_database_is_a_named_error(tmp_path):
    with pytest.raises(PanelDbMissing):
        PanelReader(tmp_path / "none.db").last_decision()
    assert not (tmp_path / "none.db").exists()


def test_every_read_leaves_the_file_byte_identical_and_needs_no_mode_stamp(tmp_path):
    db = tmp_path / "fut.db"
    conn = make_db(db)
    add_decision(conn, 1000, "jev", QUIET)
    add_fill(conn, "main", 1000, "open")
    set_position(conn, "main", opened_ms=1000)
    set_balance(conn, "main", 449.5)
    conn.close()
    before = digest(db)
    r = PanelReader(db)
    r.last_decision(); r.event_rows(None, 10); r.decision_facts(0)
    r.position("main"); r.balances(); r.fills("main")
    assert digest(db) == before
    assert not list(tmp_path.glob("fut.db-*"))  # no journal/wal side files


def test_event_rows_skip_quiet_jev_rows_and_page_by_id(tmp_path):
    db = tmp_path / "fut.db"
    conn = make_db(db)
    quiet = add_decision(conn, 1000, "jev", QUIET)
    wake = add_decision(conn, 2000, "jev", dict(QUIET, wake="entry_signal"))
    err = add_decision(conn, 3000, "jev", dict(QUIET, error="TimeoutError: x"))
    gate = add_decision(conn, 4000, "jev", dict(QUIET, gate="spread_too_wide"))
    llm = add_decision(conn, 5000, "llm", {"outcome": "ok"})
    late = add_decision(conn, 6000, "exit", {"reason": "stop"})
    r = PanelReader(db)
    assert r.last_decision() == (late, 6000)
    assert [x["id"] for x in r.event_rows(0, llm)] == [wake, err, gate, llm]  # quiet skipped, upto respected
    assert [x["id"] for x in r.event_rows(gate, late)] == [llm, late]
    assert [x["id"] for x in r.event_rows(None, late, limit=2)] == [llm, late]  # most recent, ascending
    assert r.event_rows(0, late)[0]["payload"]["wake"] == "entry_signal"
    assert quiet not in [x["id"] for x in r.event_rows(0, late)]


def test_bad_json_payload_becomes_an_empty_dict(tmp_path):
    db = tmp_path / "fut.db"
    conn = make_db(db)
    conn.execute("INSERT INTO fut_decisions(ts_ms, kind, payload) VALUES (1, 'exit', 'not json')")
    conn.commit()
    assert PanelReader(db).event_rows(0, 10)[0]["payload"] == {}


def test_snapshot_series_position_balances_fills_and_costs(tmp_path):
    db = tmp_path / "fut.db"
    conn = make_db(db)
    for i in range(10):
        add_decision(conn, 1000 + i, "jev", dict(QUIET, cost_usd=0.001, snapshot=SNAP(bid=100.0 + i, ask=102.0 + i)))
    add_decision(conn, 2000, "llm", {"cost_usd": 0.5})
    add_fill(conn, "main", 1500, "open", fee=0.01)
    add_fill(conn, "shadow:random", 1500, "open")
    set_position(conn, "main", side="short", opened_ms=1500)
    set_balance(conn, "main", 449.5)
    set_balance(conn, "shadow:random", 450.25)
    r = PanelReader(db)
    facts = r.decision_facts(0)
    assert [fact for fact in facts if fact["kind"] == "jev"][-1]["bid"] == 109.0
    assert r.position("main")["side"] == "short" and r.position("shadow:jev_only") is None
    assert r.balances() == {"main": 449.5, "shadow:random": 450.25}
    assert [f["kind"] for f in r.fills("main")] == ["open"] and r.fills("main", since_ms=1501) == []
    assert r.fills("main", day="1970-01-01")[0]["fee"] == 0.01


def test_database_without_tables_reads_as_empty(tmp_path):
    db = tmp_path / "fut.db"
    sqlite3.connect(db).close()
    db.write_bytes(b"")  # a zero-byte file is a valid empty sqlite database
    r = PanelReader(db)
    assert r.last_decision() == (0, 0) and r.event_rows(None, 0) == [] and r.balances() == {}


def test_locked_database_is_a_named_busy_error(tmp_path):
    db = tmp_path / "fut.db"
    conn = make_db(db)
    add_decision(conn, 1, "exit", {"reason": "stop"})
    conn.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(PanelDbBusy):
            PanelReader(db, timeout_s=0.05).last_decision()
    finally:
        conn.rollback()
