import importlib
import sys
from pathlib import Path

import pytest

from tests._env_guard import missing_distributions, required_distributions

_ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config: pytest.Config) -> None:
    requirements = _ROOT / "requirements.txt"
    if not requirements.exists():  # nothing to check against
        return
    missing = missing_distributions(required_distributions(requirements))
    if not missing:
        return
    # Exit rather than fail: a wrong interpreter invalidates the whole run, and
    # a run that reports "2 failed" invites someone to explain the failures
    # instead of fixing the environment.
    pytest.exit(
        "wrong interpreter: "
        f"{sys.executable}\n"
        f"missing from requirements.txt: {', '.join(missing)}\n\n"
        "Run the suite with ./scripts/test, which finds this project's "
        "virtualenv (including from a git worktree, which has no .venv of "
        "its own).\n"
        "Any result produced under this interpreter is not evidence about "
        "the project.",
        returncode=pytest.ExitCode.USAGE_ERROR,
    )


# --- Environment isolation --------------------------------------------------
#
# `Settings.from_env()` reads its defaults from `os.environ`. A pytest
# process launched from a shell (or CI step) that happens to export any of
# these names -- or a prior test that mutated `os.environ` directly, not
# through `monkeypatch` -- must never leak into a test that never opted in
# to that value. Clearing them before every test is what makes "no env set"
# actually mean "the documented defaults", not "whatever this process
# inherited".
_SETTINGS_ENV_VARS = (
    "MODE", "SYMBOL", "CYCLE_MINUTES", "WAKE_MOVE_PCT", "MAX_ORDER_USDT",
    "MAX_PORTFOLIO_PCT", "MAX_DAY_LOSS_USDT", "ATR_PERIOD", "ATR_MULT",
    "MIN_STOP_PCT", "MAX_STOP_PCT", "MIN_CONFIDENCE", "LLM_DAILY_BUDGET_USD",
    "LLM_MODEL", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "LLM_MAX_TOKENS",
    "LLM_JSON_MODE", "LLM_FALLBACK_COST_USD", "QTY_SCALE", "PAPER_SLIPPAGE_BPS",
    "PAPER_STARTING_USDT", "WS_ENABLED", "KCEX_WS_URL", "POLL_SECONDS",
    "STALE_MS", "CHART_PORT", "CHART_HOST", "FILL_CONFIRM_TRIES",
    "FILL_CONFIRM_WAIT_S", "LOG_LEVEL", "TP_ATR_MULT", "MIN_TP_PCT",
    "MAX_TP_PCT", "TIME_LIMIT_MINUTES", "COOLDOWN_MINUTES", "JOURNAL_ENABLED",
    "KCEX_TOKEN", "KCEX_TOKEN_AT", "KCEX_EMAIL", "KCEX_PASSWORD",
    "KCEX_BASE_URL", "KCEX_USER_DEVICE", "KCEX_LANGUAGE", "KCEX_USER_AGENT",
    "KCEX_PLATFORM",
    "FUT_SYMBOL", "FUT_LEVERAGE", "FUT_MARGIN_USDT", "FUT_MAX_BALANCE_PCT",
    "FUT_MAX_DAY_LOSS_USDT", "FUT_PAPER_STARTING_USDT", "FUT_SLIPPAGE_BPS", "FUT_ATR_PERIOD",
    "FUT_ATR_MULT", "FUT_MIN_STOP_PCT", "FUT_MAX_STOP_PCT", "FUT_LIQ_STOP_RATIO",
    "FUT_MAX_HOLD_SECONDS", "FUT_MIN_HOLD_SECONDS", "FUT_MIN_CONFIDENCE", "FUT_JEV_EVERY_SECONDS", "FUT_JEV_MODEL",
    "FUT_JEV_USD_PER_MTOK", "FUT_JEV_TIMEOUT_SECONDS", "FUT_WAKE_THRESHOLD", "FUT_MOVE_COST_BPS",
    "FUT_LLM_COOLDOWN_SECONDS", "FUT_LLM_TIMEOUT_SECONDS", "FUT_STALE_PRICE_BPS",
    "FUT_STALE_MARKET_SECONDS", "FUT_UNMONITORED_SECONDS", "FUT_LLM_REASONING", "FUT_WS_URL",
    "FUT_SHADOW_SEED", "TYPESAFE_API_KEY", "TYPESAFE_DEFAULT_MODEL", "TYPESAFE_BASE_URL",
)


@pytest.fixture(autouse=True)
def _isolated_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from `Settings`' documented defaults. A test that
    wants a specific value still sets it explicitly with its own
    `monkeypatch.setenv(...)` (that continues to work -- it runs during the
    test body, after this fixture's setup, so it wins); this only removes
    what nobody asked for. A test that constructs a real subprocess inherits
    this same, already-cleared `os.environ` (subprocess.run with no explicit
    `env=` copies the parent process's environment at call time), so the
    synthetic environment propagates there too without extra plumbing.
    """
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# --- No implicit dotenv loading in tests -------------------------------
#
# `bot/cli.py::main()`, `kcex/login.py::require_live_token()` and
# `bot/backtest.py`'s `--load-env` path all call python-dotenv's
# `load_dotenv()` with no explicit path. python-dotenv's default search is
# NOT cwd-based: `find_dotenv()` walks up from the *calling source file's own
# location* (inspecting the call stack), independent of any
# `monkeypatch.chdir()` a test does. In a checkout that has a real `.env`
# next to `bot/cli.py` (any real dev/CI checkout, as opposed to this
# worktree), an unmocked `cli.main()` call in a test loads real secrets and
# real settings (JOURNAL_ENABLED, TP_ATR_MULT, ...) directly into
# `os.environ` -- a mutation `monkeypatch` never tracked and therefore never
# reverts -- silently changing every test that runs afterward in the same
# process. That is what turned a clean 573-pass run in this worktree into
# 4 failures once the very same tests ran in a checkout with a real `.env`.
#
# Every test therefore gets every known `load_dotenv` entry point replaced
# with a no-op by default. `bot.cli` binds its own module-level alias via
# `from dotenv import load_dotenv`, so that alias needs a direct patch;
# `kcex.login` and `bot.backtest` re-import `dotenv.load_dotenv` locally
# inside the function body on every call, so patching the `dotenv` package
# attribute they read from is enough for those two. A test whose actual
# purpose is dotenv-loading behavior opts in with the `synthetic_dotenv`
# fixture below -- an explicit temp file, never the ambient stack/cwd
# search that caused the leak, and never the repo's real `.env`.
_DOTENV_ALIAS_MODULES = ("bot.cli", "fut.cli")


def _blocked_load_dotenv(*_args: object, **_kwargs: object) -> bool:
    return False


@pytest.fixture(autouse=True)
def _no_implicit_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", _blocked_load_dotenv)
    for modname in _DOTENV_ALIAS_MODULES:
        module = importlib.import_module(modname)
        monkeypatch.setattr(module, "load_dotenv", _blocked_load_dotenv)


@pytest.fixture
def synthetic_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Opt-in for the rare test whose actual point is dotenv-loading
    behavior. Writes a throwaway `.env` under `tmp_path` -- never the repo's
    real `.env` -- and points `bot.cli.load_dotenv` at exactly that file
    through python-dotenv's real loader (an explicit `dotenv_path`, never
    the ambient stack/cwd search that caused the original leak). Returns a
    writer: call it with the key/value pairs the file should contain.
    """
    import bot.cli as cli
    # `dotenv.main` -- not the `dotenv` package's re-exported top-level
    # name -- because `_no_implicit_dotenv` patches the LATTER; importing
    # the real implementation straight from its defining submodule keeps
    # this fixture's loader genuine regardless of fixture ordering.
    from dotenv.main import load_dotenv as real_load_dotenv

    env_path = tmp_path / "sentinel.env"
    env_path.write_text("")

    def _write(**values: object) -> None:
        env_path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")

    def _load() -> bool:
        return real_load_dotenv(dotenv_path=env_path, override=True)

    monkeypatch.setattr(cli, "load_dotenv", _load)
    return _write
