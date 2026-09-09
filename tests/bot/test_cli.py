import logging
from pathlib import Path
import subprocess
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
    # DATA_DIR/DB_PATH/LOCK_PATH/LOG_PATH are stable to the checkout now
    # (Finding 3), not the cwd -- chdir alone no longer isolates a test from
    # this worktree's real data/ directory; patch them explicitly.
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "bot.db")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "bot.lock")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "bot.log")

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
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "bot.db")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "bot.lock")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "bot.log")
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


def test_live_token_preflight_is_acquired_after_the_lock_too(tmp_path, monkeypatch):
    """require_live_token() validates the session REMOTELY and, on a missing/
    expired token, can open a real browser (login_interactive()) and write
    .env -- a side effect at least as significant as Store/Eye/KcexClient.
    With another instance already holding the lock, MODE=live must fail
    closed via AlreadyRunning before require_live_token() is ever called;
    no network/browser call may happen on the losing side of the race."""
    import bot.cli as cli

    monkeypatch.setenv("MODE", "live")
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "bot.db")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "bot.lock")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "bot.log")

    def boom(*a, **kw):
        raise AssertionError("require_live_token() ran despite the lock already being held")

    monkeypatch.setattr(cli, "require_live_token", boom)
    monkeypatch.setattr(cli, "Store", boom)
    monkeypatch.setattr(cli, "Eye", boom)
    monkeypatch.setattr(cli, "KcexClient", boom)

    with InstanceLock(cli.LOCK_PATH):
        assert cli.main(["run", "--once"]) == cli.EXIT_ALREADY_RUNNING



def test_paths_are_stable_across_cwd_not_relative_to_it(monkeypatch, tmp_path):
    """Finding 3: DATA_DIR/DB_PATH/LOCK_PATH used to be `Path("data")`,
    resolved against the process cwd. Invoking the SAME installed module
    from a different cwd must resolve the exact same absolute paths -- never
    silently select a second ledger/lock next to wherever the process
    happened to be launched."""
    import bot.cli as cli

    before = (cli.DATA_DIR, cli.DB_PATH, cli.LOCK_PATH, cli.LOG_PATH)
    monkeypatch.chdir(tmp_path)
    import importlib
    reloaded = importlib.reload(cli)
    try:
        after = (reloaded.DATA_DIR, reloaded.DB_PATH, reloaded.LOCK_PATH, reloaded.LOG_PATH)
        assert after == before
        assert all(p.is_absolute() for p in after)
        assert reloaded.db_path_for_mode("paper") == before[1]
    finally:
        importlib.reload(cli)  # restore the real module state for later tests


def test_second_instance_from_a_different_cwd_never_touches_the_data_dir(tmp_path, monkeypatch):
    """Prove the concrete consequence of path stability: a second instance
    launched from an unrelated cwd, with the first instance already holding
    the lock, must fail via AlreadyRunning before creating/touching ANY file
    under the (stable) data directory -- no second ledger, no second lock,
    no log file."""
    import bot.cli as cli

    data_dir = tmp_path / "checkout" / "data"
    monkeypatch.setattr(cli, "DATA_DIR", data_dir)
    monkeypatch.setattr(cli, "DB_PATH", data_dir / "bot.db")
    monkeypatch.setattr(cli, "LOCK_PATH", data_dir / "bot.lock")
    monkeypatch.setattr(cli, "LOG_PATH", data_dir / "bot.log")
    monkeypatch.setenv("MODE", "paper")

    def boom(*a, **kw):
        raise AssertionError("no initializer may run once AlreadyRunning is certain")

    monkeypatch.setattr(cli, "Store", boom)
    monkeypatch.setattr(cli, "Eye", boom)
    monkeypatch.setattr(cli, "KcexClient", boom)

    elsewhere = tmp_path / "somewhere-else-entirely"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with InstanceLock(cli.LOCK_PATH):
        assert cli.main(["run", "--once"]) == cli.EXIT_ALREADY_RUNNING

    assert not data_dir.exists() or list(data_dir.iterdir()) == [data_dir / "bot.lock"]
    assert not (data_dir / "bot.db").exists()
    assert not (data_dir / "bot.log").exists()


def test_file_logging_starts_only_after_the_lock_is_held(tmp_path, monkeypatch):
    """Finding 3: setup_logging() used to add the file handler (creating
    bot.log) before the instance lock was ever acquired. A process that
    loses the race to AlreadyRunning must never create/touch bot.log."""
    import logging
    import bot.cli as cli

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    for h in saved_handlers:
        root.removeHandler(h)
    try:
        data_dir = tmp_path / "data"
        monkeypatch.setattr(cli, "DATA_DIR", data_dir)
        monkeypatch.setattr(cli, "DB_PATH", data_dir / "bot.db")
        monkeypatch.setattr(cli, "LOCK_PATH", data_dir / "bot.lock")
        monkeypatch.setattr(cli, "LOG_PATH", data_dir / "bot.log")
        monkeypatch.setenv("MODE", "paper")

        with InstanceLock(cli.LOCK_PATH):
            assert cli.main(["run", "--once"]) == cli.EXIT_ALREADY_RUNNING
        # AlreadyRunning: setup_logging() ran (stderr only), but the lock was
        # never held, so add_file_logging() never ran either.
        assert not (data_dir / "bot.log").exists()
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)


def test_loop_restart_resumes_spend_and_refuses_when_it_no_longer_fits(tmp_path, monkeypatch):
    """Finding 3: `_loop` used to construct `Budget(spent_usd=0.0, ...)` on
    every process start, so a same-day restart silently re-authorized the
    full daily cap regardless of what had already been spent. Seed a prior
    process's reservation directly through the durable API, then prove the
    RESUMED budget actually refuses a request that would otherwise fit in a
    fresh zero budget -- an observed admission decision from the real
    think_result()/store.reserve_budget() path, not just a copied number."""
    import bot.cli as cli
    from bot.brain import think_result
    from bot.cycle import utc_day
    from bot.settings import Settings
    from bot.store import Store
    from bot.types import GateResult, Snapshot

    store = Store(tmp_path / "c.db", mode="paper")
    store.reserve_budget(today=utc_day(), cap_usd=2.0, reserve_usd=1.5)  # a prior process already spent 1.5/2.0

    d = Settings.from_env().__dict__.copy()
    d.update(mode="paper", llm_daily_budget_usd=2.0, llm_fallback_cost_usd=0.6,
             openrouter_api_key="k", llm_model="m")
    settings = Settings(**d)
    snap = Snapshot(ts_ms=1, last=100, bid=99, ask=101, spread=2, bars_15m=[], atr=1,
                    free_usdt=450, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False)
    reasons = []

    def fake_run_once(*, budget, store, **kw):
        def boom(*a, **k):
            raise AssertionError("must not dispatch once the cap refuses admission")
        result = think_result(snap, settings, budget, store=store, http_post=boom)
        reasons.append(result.reason)
        return 0, 0.0, GateResult(False, "hold", "HOLD")

    class FakeEye:
        rules = None
        def connect_ws(self): pass
        def snapshot_rest(self): pass

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    code = cli._loop(True, settings, object(), store, FakeEye(), object())

    assert code == cli.EXIT_OK
    # 1.5 resumed + 0.6 reserve > 2.0 cap: correctly refused, not silently
    # approved by a fresh-zero budget that forgot the prior spend.
    assert reasons == ["llm_budget"]
    assert store.budget_load()["spent_usd"] == pytest.approx(1.5)  # refusal writes nothing


def test_loop_restart_resumes_spend_and_admits_when_it_still_fits(tmp_path, monkeypatch):
    """The control: resuming a SMALL prior spend still leaves room, and the
    resumed budget correctly admits (and durably reserves) a new request."""
    import bot.cli as cli
    from bot.brain import think_result
    from bot.cycle import utc_day
    from bot.settings import Settings
    from bot.store import Store
    from bot.types import GateResult, Snapshot

    store = Store(tmp_path / "c.db", mode="paper")
    store.reserve_budget(today=utc_day(), cap_usd=2.0, reserve_usd=0.1)

    d = Settings.from_env().__dict__.copy()
    d.update(mode="paper", llm_daily_budget_usd=2.0, llm_fallback_cost_usd=0.6,
             openrouter_api_key="k", llm_model="m")
    settings = Settings(**d)
    snap = Snapshot(ts_ms=1, last=100, bid=99, ask=101, spread=2, bars_15m=[], atr=1,
                    free_usdt=450, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False)
    reasons = []

    def fake_run_once(*, budget, store, **kw):
        def timeout(*a, **k):
            raise __import__("requests").Timeout("slow")
        result = think_result(snap, settings, budget, store=store, http_post=timeout)
        reasons.append(result.reason)
        return 0, 0.0, GateResult(False, "hold", "HOLD")

    class FakeEye:
        rules = None
        def connect_ws(self): pass
        def snapshot_rest(self): pass

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    code = cli._loop(True, settings, object(), store, FakeEye(), object())

    assert code == cli.EXIT_OK
    assert reasons == ["llm_timeout"]  # admitted -- the dispatch was attempted
    assert store.budget_load()["spent_usd"] == pytest.approx(0.7)  # 0.1 resumed + 0.6 reserved


def test_loop_restart_on_a_new_utc_day_starts_at_zero(tmp_path, monkeypatch):
    """The control: a persisted budget from a PRIOR UTC day must not carry
    over -- that is a fresh day's authorization, not a bug to work around."""
    import bot.cli as cli
    from bot.settings import Settings
    from bot.store import Store
    from bot.types import GateResult

    store = Store(tmp_path / "c.db", mode="paper")
    store.reserve_budget(today="2000-01-01", cap_usd=2.0, reserve_usd=1.9)

    d = Settings.from_env().__dict__.copy()
    d["mode"] = "paper"
    settings = Settings(**d)

    captured = {}

    def fake_run_once(*, budget, **kw):
        captured["spent_usd"] = budget.spent_usd
        return 0, 0.0, GateResult(False, "hold", "HOLD")

    class FakeEye:
        rules = None
        def connect_ws(self): pass
        def snapshot_rest(self): pass

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    cli._loop(True, settings, object(), store, FakeEye(), object())

    assert captured["spent_usd"] == 0.0


def test_reservation_survives_a_fatal_hands_error_in_the_same_cycle(tmp_path):
    """A reservation committed by think_result() happens BEFORE the collar
    decision and hands.execute() even run; it must stay durably persisted
    even when hands.execute() raises immediately afterward in the SAME
    cycle -- there is no post-hoc save left to skip, because there is
    nothing left to skip: the commit already happened at the request
    boundary."""
    from unittest.mock import Mock
    from bot.brain import Budget, think_result
    from bot.cycle import run_once, utc_day
    from bot.hands import PaperHands, UnprotectedPosition
    from bot.settings import Settings
    from bot.store import Store
    from bot.types import Bar, Snapshot

    d = Settings.from_env().__dict__.copy()
    d.update(mode="paper", openrouter_api_key="k", llm_model="m", llm_fallback_cost_usd=0.02)
    settings = Settings(**d)
    store = Store(tmp_path / "c.db", mode="paper")

    class FakeEye:
        bot_qty = 0.0
        bot_avg_entry = None
        last_intent_action = None
        last_bot_pnl_usdt = 0.0
        rules = None
        def poll_quotes(self): return True
        def poll_heavy(self): pass
        def snapshot(self):
            return Snapshot(ts_ms=1, last=80_000.0, bid=79_999.0, ask=80_001.0, spread=2,
                            bars_15m=[Bar(t=i, o=100, h=101, l=99, c=100) for i in range(20)],
                            atr=1.0, free_usdt=450.0, bot_qty=0.0, bot_avg_entry=None,
                            ws_ok=True, stale=False)

    class BoomHands(PaperHands):
        def execute(self, gate, snap):
            raise UnprotectedPosition("no stop")

    post = Mock(return_value=Mock(status_code=200, json=lambda: {
        "choices": [{"message": {"content": '{"action":"BUY","confidence":1,"reason":"go","regime":"trend"}'}}],
        "usage": {"cost": 0.001},
    }))

    def think(snap, settings, budget, **kwargs):
        return think_result(snap, settings, budget, http_post=post, store=kwargs.get("store"))

    budget = Budget(0, 2, utc_day())
    with pytest.raises(UnprotectedPosition):
        run_once(settings=settings, eye=FakeEye(), store=store, client=object(),
                hands=BoomHands(settings, store), budget=budget, last_llm_ms=0, last_px=0.0, think=think)

    persisted = store.budget_load()
    assert persisted is not None
    assert persisted["day"] == utc_day()
    assert persisted["spent_usd"] == pytest.approx(0.001)


def test_reservation_survives_a_subprocess_crash_inside_the_real_http_dispatch(tmp_path):
    """The strongest form of the crash regression: drives the REAL,
    unmodified `bot.cli._loop` -> `bot.cycle.run_once` -> `bot.brain.
    think_result` path (no fake run_once bypassing the production wiring),
    with `requests.post` itself crashing the process mid-dispatch. The
    reservation committed by think_result() before calling post() must
    already be durable on disk when a fresh process reopens the database."""
    from bot.store import Store
    db_path = tmp_path / "crash.db"
    child = f'''
import os, sys
sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from dataclasses import replace
from bot import cli
from bot.settings import Settings
from bot.types import Bar, Snapshot

settings = replace(Settings.from_env(), mode="paper", openrouter_api_key="synthetic",
                   llm_model="synthetic", llm_fallback_cost_usd=0.02)
store = cli.Store(Path({str(db_path)!r}), mode="paper")

class FakeEye:
    bot_qty = 0.0
    bot_avg_entry = None
    last_intent_action = None
    last_bot_pnl_usdt = 0.0
    rules = None
    def connect_ws(self): pass
    def snapshot_rest(self): pass
    def poll_quotes(self): return True
    def poll_heavy(self): pass
    def snapshot(self):
        return Snapshot(ts_ms=1, last=100.0, bid=99.0, ask=101.0, spread=2,
                        bars_15m=[Bar(t=i, o=100, h=101, l=99, c=100) for i in range(20)],
                        atr=1.0, free_usdt=450.0, bot_qty=0.0, bot_avg_entry=None,
                        ws_ok=True, stale=False)

def crash(*a, **kw):
    os._exit(0)

import requests
requests.post = crash

hands = cli.PaperHands(settings, store)
cli._loop(True, settings, None, store, FakeEye(), hands)
'''
    subprocess.run([sys.executable, "-c", child], check=True, cwd=ROOT)
    reopened = Store(db_path, mode="paper")
    persisted = reopened.budget_load()
    assert persisted is not None, "the crash lost the reservation -- not durable at the dispatch boundary"
    assert persisted["spent_usd"] == pytest.approx(0.02)


def test_cli_main_never_loads_an_implicit_dotenv_file_even_when_one_is_discoverable(tmp_path, monkeypatch):
    """Regression for the post-merge test-isolation bug: `cli.main()` used
    to call the real, unmocked `load_dotenv()`. python-dotenv's default
    search is stack-based, not cwd-based -- `find_dotenv()` walks up from
    bot/cli.py's OWN file location, so a `monkeypatch.chdir()` trick cannot
    exercise (or fake) that path directly. Instead, simulate the exact
    failure mode -- "the stack-based search successfully finds a real .env"
    -- by making `find_dotenv()` itself report a sentinel file, and drive
    the real `cli.main()` on top of the project's normal test isolation (no
    per-test dotenv mocking here, matching the four tests that regressed:
    `test_paper_mode_never_authenticates_even_with_a_leaked_token`,
    `test_lock_is_acquired_before_any_initializer_with_side_effects`,
    `test_live_token_preflight_is_acquired_after_the_lock_too`,
    `test_file_logging_starts_only_after_the_lock_is_held`). If the
    project's dotenv-blocking fixture is ever removed or broken, this test
    fails: `cli.main()` would call the real `load_dotenv()`, which finds the
    faked path and loads it for real. Proves two things: the sentinel
    values never land in `os.environ` (not just that nothing happened to
    read a real file this time), and `Settings.from_env()` right afterward
    still reports the documented defaults -- the environment a later test
    (e.g. a backtest replay) would observe is clean."""
    import os
    import dotenv
    import bot.cli as cli
    from bot.settings import Settings

    sentinel = tmp_path / "sentinel.env"
    sentinel.write_text(
        "JOURNAL_ENABLED=1\nTP_ATR_MULT=3\nTIME_LIMIT_MINUTES=60\nCOOLDOWN_MINUTES=30\n"
    )
    monkeypatch.setattr(dotenv.main, "find_dotenv", lambda *a, **kw: str(sentinel))

    monkeypatch.setenv("MODE", "paper")
    monkeypatch.setenv("WS_ENABLED", "false")
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "bot.db")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "bot.lock")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "bot.log")

    class FakeEye:
        rules = None

        def __init__(self, client, settings):
            pass

        def start_ws_thread(self):
            pass

        def load_rules(self):
            return None

    monkeypatch.setattr(cli, "Eye", FakeEye)
    monkeypatch.setattr(cli, "_loop", lambda *a, **kw: 0)

    assert cli.main(["run", "--once"]) == 0

    for key in ("JOURNAL_ENABLED", "TP_ATR_MULT", "TIME_LIMIT_MINUTES", "COOLDOWN_MINUTES"):
        assert key not in os.environ, f"{key} leaked from a dotenv file -- load_dotenv() was not neutralized"

    settings = Settings.from_env()
    assert settings.journal_enabled is False
    assert settings.tp_atr_mult == 0.0
    assert settings.time_limit_minutes == 0.0
    assert settings.cooldown_minutes == 0.0


def test_synthetic_dotenv_fixture_loads_only_its_own_file(synthetic_dotenv, monkeypatch):
    """The opt-in escape hatch: a test that genuinely wants dotenv-loading
    behavior under test gets a real load through a file it fully controls
    (`synthetic_dotenv`), not a permanent no-op -- proving the isolation
    fixture blocks the *implicit, ambient* search, not dotenv loading
    altogether. Only the keys the fixture's file names are affected."""
    import os
    import bot.cli as cli

    synthetic_dotenv(JOURNAL_ENABLED="1", TP_ATR_MULT="3")
    monkeypatch.delenv("JOURNAL_ENABLED", raising=False)
    monkeypatch.delenv("TP_ATR_MULT", raising=False)

    assert cli.load_dotenv() is True
    assert os.environ["JOURNAL_ENABLED"] == "1"
    assert os.environ["TP_ATR_MULT"] == "3"
    assert "TIME_LIMIT_MINUTES" not in os.environ
