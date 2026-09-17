from bot.cli import InstanceLock
from bot.store import Store
import fut.cli as cli
from fut.store import FutStore


def test_report_without_database_creates_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "none.db")
    assert cli.main(["report"]) == cli.EXIT_OK
    assert "no futures paper database yet" in capsys.readouterr().out
    assert not (tmp_path / "none.db").exists()


def test_report_prints_the_criterion(tmp_path, monkeypatch, capsys):
    db = tmp_path / "fut.db"
    FutStore(db).close()
    before = db.stat().st_mtime_ns
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "FutStore", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("report opened FutStore")))
    assert cli.main(["report"]) == cli.EXIT_OK
    assert "Edge criterion: FAILED" in capsys.readouterr().out
    assert db.stat().st_mtime_ns == before


def test_report_prints_the_requested_window(tmp_path, monkeypatch, capsys):
    db = tmp_path / "fut.db"
    FutStore(db).close()
    monkeypatch.setattr(cli, "DB_PATH", db)

    assert cli.main(["report", "--since-ms", "12345"]) == cli.EXIT_OK

    assert "window: since_ms >= 12345" in capsys.readouterr().out


def test_report_refuses_a_database_from_another_mode(tmp_path, monkeypatch):
    db = tmp_path / "bot.db"
    Store(db, mode="paper").close()
    monkeypatch.setattr(cli, "DB_PATH", db)
    assert cli.main(["report"]) == cli.EXIT_STORE_MISMATCH


def patch_run(tmp_path, monkeypatch):
    # add_file_logging attaches a FileHandler to the root logger for the rest of the
    # process; record the call instead so later tests (bot CLI log assertions) are unaffected.
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "futures.lock")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "futures.log")
    calls = {"run": [], "log": []}
    monkeypatch.setattr(cli, "run_loop", lambda max_seconds: calls["run"].append(max_seconds) or 0)
    monkeypatch.setattr(cli, "add_file_logging", lambda path: calls["log"].append(path))
    return calls


def test_run_exits_3_without_logging_when_lock_is_held(tmp_path, monkeypatch):
    calls = patch_run(tmp_path, monkeypatch)
    with InstanceLock(tmp_path / "futures.lock"):
        assert cli.main(["run", "--max-seconds", "1"]) == cli.EXIT_ALREADY_RUNNING
    assert calls == {"run": [], "log": []}


def test_run_takes_the_lock_then_logs_then_runs(tmp_path, monkeypatch):
    calls = patch_run(tmp_path, monkeypatch)
    assert cli.main(["run", "--max-seconds", "1"]) == cli.EXIT_OK
    assert calls == {"run": [1.0], "log": [tmp_path / "futures.log"]}


def test_wakegrid_reads_without_futstore_mode_stamp_or_write(tmp_path, monkeypatch, capsys):
    db = tmp_path / "fut.db"
    import json
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE fut_decisions (id INTEGER PRIMARY KEY, ts_ms INTEGER, kind TEXT, payload TEXT)")
    conn.execute("INSERT INTO fut_decisions(ts_ms, kind, payload) VALUES (?,?,?)", (1, "jev", json.dumps({
        "error": "timeout", "answers": None, "snapshot": {},
    })))
    conn.commit()
    conn.close()
    before = db.stat().st_mtime_ns
    monkeypatch.setattr(cli, "DB_PATH", db)
    assert cli.main(["wakegrid"]) == cli.EXIT_OK
    assert "WARNING: in-sample" in capsys.readouterr().out
    assert db.stat().st_mtime_ns == before


def test_jevscore_reads_without_futstore_mode_stamp_or_write(tmp_path, monkeypatch, capsys):
    db = tmp_path / "fut.db"
    import json
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE fut_decisions (id INTEGER PRIMARY KEY, ts_ms INTEGER, kind TEXT, payload TEXT)")
    conn.execute("INSERT INTO fut_decisions VALUES (1, 0, 'jev', ?)", (json.dumps({
        "error": None, "answers": {"direction": "up", "direction_conf": 0.8},
        "snapshot": {"bid": 100.0, "ask": 100.0, "last": 100.0},
    }),))
    conn.execute("INSERT INTO fut_decisions VALUES (2, 60000, 'llm', ?)", (json.dumps({
        "snapshot": {"bid": 101.0, "ask": 101.0, "last": 101.0},
    }),))
    conn.commit()
    conn.close()
    before = db.stat().st_mtime_ns
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "FutStore", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("opened FutStore")))
    assert cli.main(["jevscore", "--since-ms", "0"]) == cli.EXIT_OK
    output = capsys.readouterr().out
    assert "jev_ab" in output and "WARNING: rows from different configurations" in output
    assert db.stat().st_mtime_ns == before


def test_panel_serves_read_only_without_lock_or_database(tmp_path, monkeypatch):
    seen = {}

    class FakeServer:
        def __init__(self, *, reader, index_path, host, port, max_hold_s, jev_every_s):
            seen.update(path=reader.path, index=index_path, host=host, port=port, max_hold_s=max_hold_s,
                        jev_every_s=jev_every_s)

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            seen["shutdown"] = True

    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "none.db")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "futures.lock")
    monkeypatch.setattr(cli, "PanelServer", FakeServer)
    monkeypatch.setenv("FUT_MAX_HOLD_SECONDS", "120")
    monkeypatch.setenv("FUT_JEV_EVERY_SECONDS", "3")
    assert cli.main(["panel", "--port", "9999"]) == cli.EXIT_OK
    assert seen["path"] == tmp_path / "none.db" and seen["port"] == 9999 and seen["host"] == "127.0.0.1"
    assert seen["index"] == cli.PANEL_INDEX and seen["max_hold_s"] == 120.0 and seen["jev_every_s"] == 3.0
    assert seen["shutdown"] is True
    assert not (tmp_path / "none.db").exists() and not (tmp_path / "futures.lock").exists()


def test_panel_refuses_a_non_loopback_host(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "none.db")
    assert cli.main(["panel", "--host", "0.0.0.0"]) == 1
    assert "loopback" in capsys.readouterr().out
