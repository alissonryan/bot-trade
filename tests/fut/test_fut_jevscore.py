import json
import sqlite3

from fut.jevscore import render, score_database, score_rows


def payload(mid, direction, confidence=0.8, probabilities=None):
    answers = {"direction": direction, "direction_conf": confidence}
    return {"model": "jev-1", "error": None, "answers": answers,
            "snapshot": {"bid": mid, "ask": mid, "last": mid, "spread_bps": 0.0, "stale": False},
            **({"probabilities": probabilities} if probabilities is not None else {})}


def test_score_uses_nearest_future_snapshot_in_55_to_75_second_window():
    rows = [
        {"id": 1, "ts_ms": 0, "kind": "jev", "payload": payload(100.0, "up", probabilities={"up": 0.8})},
        {"id": 2, "ts_ms": 0, "kind": "jev_ab", "payload": payload(100.0, "down")},
        {"id": 3, "ts_ms": 55_000, "kind": "jev", "payload": payload(101.0, "up")},
        {"id": 4, "ts_ms": 60_000, "kind": "llm", "payload": {"snapshot": {"bid": 103.0, "ask": 103.0, "last": 103.0}}},
    ]
    result = score_rows(rows)
    assert result["jev"]["rows_scored"] == 1
    assert result["jev"]["directional_hit_rate"] == 1.0
    assert result["jev"]["mean_realized_bps_in_answered_direction"] == 300.0
    assert result["jev_ab"]["rows_scored"] == 1
    assert result["jev_ab"]["directional_hit_rate"] == 0.0
    assert result["jev"]["brier_source"] == "probabilities"
    assert result["jev"]["brier_score"] is not None


def test_score_uses_confidence_label_when_probabilities_are_missing():
    rows = [
        {"id": 1, "ts_ms": 0, "kind": "jev", "payload": payload(100.0, "up")},
        {"id": 2, "ts_ms": 10_000, "kind": "jev_ab", "payload": payload(100.0, "flat")},
        {"id": 3, "ts_ms": 65_000, "kind": "llm", "payload": {"snapshot": {"bid": 101.0, "ask": 101.0, "last": 101.0}}},
    ]
    result = score_rows(rows)
    assert result["jev"]["rows_scored"] == 1 and result["jev"]["rows_skipped"] == 0
    assert result["jev_ab"]["rows_scored"] == 1 and result["jev_ab"]["rows_skipped"] == 0
    assert result["jev"]["brier_score"] is not None
    assert "confidence" in result["jev"]["brier_source"]
    assert result["jev_ab"]["brier_score"] is not None
    assert "confidence" in result["jev_ab"]["brier_source"]


def test_score_database_is_read_only(tmp_path):
    db = tmp_path / "fut.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE fut_decisions (id INTEGER PRIMARY KEY, ts_ms INTEGER, kind TEXT, payload TEXT)")
    conn.execute("INSERT INTO fut_decisions VALUES (1, 0, 'jev', ?)", (json.dumps(payload(100.0, "up")),))
    conn.execute("INSERT INTO fut_decisions VALUES (2, 60000, 'llm', ?)",
                 (json.dumps({"snapshot": {"bid": 101.0, "ask": 101.0, "last": 101.0}}),))
    conn.commit()
    conn.close()
    before = db.stat().st_mtime_ns
    result = score_database(db)
    assert result["jev"]["rows_scored"] == 1
    assert db.stat().st_mtime_ns == before
    assert "jev_ab" in render(result)


def test_score_counts_row_without_a_55_to_75_second_future_as_skipped():
    result = score_rows([{
        "id": 1, "ts_ms": 0, "kind": "jev", "payload": payload(100.0, "up")
    }])
    assert result["jev"]["rows_scored"] == 0
    assert result["jev"]["rows_skipped"] == 1
