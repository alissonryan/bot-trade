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


def test_lock_fails_closed_when_fcntl_is_unavailable(tmp_path, monkeypatch):
    """Finding 5: `except ImportError: pass` let a platform with no fcntl
    proceed as if it held an exclusive lock. An unenforceable lock must refuse
    to start, not silently allow two instances to trade unlocked."""
    import sys

    monkeypatch.setitem(sys.modules, "fcntl", None)  # forces `import fcntl` to raise ImportError
    with pytest.raises(RuntimeError, match="fcntl"):
        with InstanceLock(tmp_path / "bot.lock"):
            pass


def test_lock_is_acquired_before_any_initializer_with_side_effects(tmp_path, monkeypatch):
    """Finding 5: the lock used to be acquired after Store/Eye/WS/load_rules/
    Hands construction, so a second racing instance could create/migrate the
    database, open a WS connection and construct Hands before discovering, at
    the very end, that another instance already held the lock. With another
    instance already holding the lock, main() must fail before touching any
    of them."""
    import bot.cli as cli

    monkeypatch.setenv("MODE", "paper")
    monkeypatch.chdir(tmp_path)

    def boom(*a, **kw):
        raise AssertionError("initializer ran despite the lock already being held")

    monkeypatch.setattr(cli, "Store", boom)
    monkeypatch.setattr(cli, "Eye", boom)
    monkeypatch.setattr(cli, "KcexClient", boom)

    with InstanceLock(cli.LOCK_PATH):
        assert cli.main(["run", "--once"]) == cli.EXIT_ALREADY_RUNNING

    # released: the same initializers now run normally
    monkeypatch.setattr(cli, "Store", lambda *a, **kw: object())
    called = {"eye": False}

    class FakeEye:
        rules = None

        def __init__(self, client, settings):
            called["eye"] = True

        def start_ws_thread(self):
            pass

        def load_rules(self):
            return None

    monkeypatch.setattr(cli, "Eye", FakeEye)
    monkeypatch.setattr(cli, "KcexClient", lambda *a, **kw: object())
    monkeypatch.setattr(cli, "PaperHands", lambda *a, **kw: object())
    monkeypatch.setattr(cli, "_loop", lambda *a, **kw: 0)
    assert cli.main(["run", "--once"]) == 0
    assert called["eye"] is True
