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
    monkeypatch.setattr(cli, "DB_PATH", db)
    assert cli.main(["report"]) == cli.EXIT_OK
    assert "Edge criterion: FAILED" in capsys.readouterr().out


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
