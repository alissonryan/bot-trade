import json
import sqlite3

import pytest

from fut.wakegrid import load_rows, replay, render_grid


def payload(*, direction="up", conf=0.8, beats=0.8, regime="trend", bid=99.9, ask=100.0,
            atr=1.0, error=None, answers=True, position=None):
    result = {
        "error": error,
        "answers": ({"direction": direction, "direction_conf": conf, "beats_cost": beats,
                     "regime": regime} if answers else None),
        "snapshot": {"bid": bid, "ask": ask, "last": bid, "spread_bps": (ask - bid) / ((ask + bid) / 2) * 10_000,
                      "atr_1m": atr},
    }
    if position is not None:
        result["state"] = {"position": position}
    return result


def row(ts_ms, **kwargs):
    return {"ts_ms": ts_ms, "payload": payload(**kwargs)}


def test_replay_enters_at_side_price_exits_on_first_eligible_row_and_applies_cost():
    rows = [
        row(0),
        row(30_000, bid=100.2, ask=100.3),
        row(60_000, bid=101.0, ask=101.1),
    ]
    result = replay(rows, streak=1, threshold=0.5, regimes=(), gates=False)
    assert (result.wakes, result.trades) == (1, 1)
    assert result.sum_net_bps == pytest.approx(94.0)
    assert result.mean_net_bps == pytest.approx(94.0)
    assert result.win_rate == pytest.approx(1.0)


def test_replay_supports_short_entries_streak_and_regime_filter():
    rows = [row(0, direction="down"), row(1_000, direction="down"),
            row(61_000, direction="down", bid=98.9, ask=99.0)]
    result = replay(rows, streak=2, threshold=0.5, regimes=("trend", "volatile"), gates=False)
    assert result.trades == 1
    assert result.sum_net_bps == pytest.approx(84.0, abs=0.1)
    assert replay(rows, streak=1, threshold=0.5, regimes=("range",), gates=False).trades == 0


def test_flat_row_resets_entry_streak():
    rows = [row(0, direction="up"), row(1_000, direction="flat"), row(2_000, direction="up")]

    result = replay(rows, streak=2, threshold=0.5, regimes=(), gates=False)

    assert result.wakes == 0 and result.trades == 0


def test_flat_row_can_close_replay_position_at_first_hold_boundary():
    rows = [row(0), row(60_000, direction="flat", bid=101.0, ask=101.1),
            row(120_000, bid=99.0, ask=99.1)]

    result = replay(rows, streak=1, threshold=0.5, regimes=(), gates=False)

    assert result.trades == 1
    assert result.sum_net_bps == pytest.approx(94.0)


def test_replay_skips_entries_recorded_while_main_was_open(tmp_path):
    rows = [row(0, position={"side": "long"}), row(60_000, bid=101.0, ask=101.1,
                                                   position={"side": "long"})]

    result = replay(rows, streak=1, threshold=0.5, regimes=(), gates=False)

    assert result.wakes == 0 and result.trades == 0


def test_replay_cost_gates_block_wide_spread_and_quiet_atr():
    wide = row(0, atr=100.0, bid=99.9, ask=100.0)
    wide["payload"]["snapshot"]["spread_bps"] = 3.0
    quiet = row(0, atr=0.01)
    exit_row = row(60_000, bid=101.0, ask=101.1, atr=1.0)
    assert replay([wide, exit_row], streak=1, threshold=0.5, regimes=(), gates=True).trades == 0
    assert replay([quiet, exit_row], streak=1, threshold=0.5, regimes=(), gates=True).trades == 0
    assert replay([wide, exit_row], streak=1, threshold=0.5, regimes=(), gates=False).trades == 1


def test_load_rows_is_since_filtered_and_ignores_null_or_error_during_replay(tmp_path):
    db = tmp_path / "fut.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE fut_decisions (id INTEGER PRIMARY KEY, ts_ms INTEGER, kind TEXT, payload TEXT)")
    conn.executemany("INSERT INTO fut_decisions(ts_ms, kind, payload) VALUES (?,?,?)", [
        (1, "jev", json.dumps(payload(answers=False))),
        (2, "jev", json.dumps(payload(error="timeout"))),
        (3, "jev", json.dumps(payload())),
        (4, "llm", json.dumps(payload())),
    ])
    conn.commit()
    conn.close()
    rows = load_rows(db, since_ms=2)
    assert [r["ts_ms"] for r in rows] == [2, 3]
    assert replay(rows, streak=1, threshold=0.5, regimes=(), gates=False).wakes == 1


def test_render_grid_sorts_by_sum_net_bps_and_warns_in_sample():
    results = [
        replay([row(0), row(60_000, bid=101.0, ask=101.1)], streak=1, threshold=0.5, regimes=(), gates=False),
        replay([row(0), row(60_000, bid=100.1, ask=100.2)], streak=1, threshold=0.5, regimes=(), gates=False),
    ]
    rendered = render_grid(results)
    assert "WARNING: in-sample" in rendered
    assert rendered.index("94.00") < rendered.index("4.00")
    assert "wakes" in rendered and "win_rate" in rendered
    assert "fixed 60s hold" in rendered
    assert "wakes = entries" in rendered
