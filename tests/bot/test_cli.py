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
