import logging
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.cli import AlreadyRunning, InstanceLock, warn_token_age


def test_instance_lock_refuses_second_holder(tmp_path):
    path = tmp_path / "bot.lock"
    with InstanceLock(path):
        assert path.read_text().strip().isdigit()
        with pytest.raises(AlreadyRunning):
            with InstanceLock(path):
                pass
    # released: can be taken again
    with InstanceLock(path):
        pass


def test_warn_token_age_levels(caplog):
    caplog.set_level(logging.WARNING, logger="bot")
    assert warn_token_age(None) is None
    assert "KCEX_TOKEN_AT" in caplog.text
    caplog.clear()
    age = warn_token_age("2000-01-01T00:00:00+00:00")
    assert age is not None and age > 6
    assert "dies at" in caplog.text


def test_loop_halts_on_a_stuck_position(monkeypatch, tmp_path):
    """Finding 6, second half: a position the bot cannot exit must stop the loop
    with its own exit code, not fall into the generic 'back off and retry'
    branch that would repeat the impossible exit forever."""
    import bot.cli as cli
    from bot.hands import PositionStuck
    from bot.settings import Settings
    from bot.store import Store

    class FakeEye:
        rules = None

        def connect_ws(self):
            pass

        def snapshot_rest(self):
            pass

    def boom(**kw):
        raise PositionStuck("stop is not ours to cancel")

    monkeypatch.setattr(cli, "run_once", boom)
    d = Settings.from_env().__dict__.copy()
    d["mode"] = "paper"
    settings = Settings(**d)

    code = cli._loop(True, settings, None, Store(tmp_path / "c.db"), FakeEye(), object())

    assert code == cli.EXIT_STUCK


def test_paper_mode_never_authenticates_even_with_a_leaked_token(tmp_path, monkeypatch):
    """Finding 2: bot/cli.py built `KcexClient(token=token or None)`. In paper
    mode `token` starts as `""`, and `"" or None` collapses to `None` --
    KcexClient(token=None) then falls back to reading KCEX_TOKEN from the
    environment. A stale token left over from a prior live login (or any
    KCEX_TOKEN in .env) must never reach paper's client."""
    import bot.cli as cli

    monkeypatch.setenv("MODE", "paper")
    monkeypatch.setenv("KCEX_TOKEN", "leaked-live-token")
    monkeypatch.setenv("WS_ENABLED", "false")
    monkeypatch.chdir(tmp_path)

    captured: dict = {}

    class FakeEye:
        rules = None

        def __init__(self, client, settings):
            captured["client"] = client

        def start_ws_thread(self):
            pass

        def load_rules(self):
            return None

    monkeypatch.setattr(cli, "Eye", FakeEye)
    monkeypatch.setattr(cli, "_loop", lambda *a, **kw: 0)

    assert cli.main(["run", "--once"]) == 0
    assert captured["client"].token == "", "paper must never carry an authorization token"
