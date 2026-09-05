"""``python -m bot run [--once]``

Exit codes: 0 ok, 1 session dead (re-run ``python -m kcex.cli login``), 2 unprotected
position on the exchange (fix it by hand, then restart), 3 another instance holds the
lock, 4 a --once cycle failed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from bot.brain import Budget
from bot.cycle import SessionDead, run_once, utc_day
from bot.eye import Eye
from bot.hands import LiveHands, PaperHands, UnprotectedPosition
from bot.settings import Settings
from bot.store import Store
from kcex.client import KcexClient
from kcex.login import require_live_token
from kcex.session import SESSION_DAYS, token_age_days

log = logging.getLogger("bot")

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "bot.db"
LOCK_PATH = DATA_DIR / "bot.lock"
LOG_PATH = DATA_DIR / "bot.log"

EXIT_OK = 0
EXIT_SESSION_DEAD = 1
EXIT_UNPROTECTED = 2
EXIT_ALREADY_RUNNING = 3
EXIT_CYCLE_FAILED = 4


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """One bot per data directory. Two loops on one bot.db would double-trade."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def __enter__(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # non-POSIX: best effort, no lock
            pass
        except OSError as exc:
            fh.close()
            raise AlreadyRunning(f"another bot holds {self.path}") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh
        return self

    def __exit__(self, *exc) -> None:
        if self._fh:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            self._fh.close()
            self._fh = None


def setup_logging(level: str, log_path: Path | None = LOG_PATH) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def warn_token_age(token_at: str | None) -> float | None:
    age = token_age_days(token_at)
    if age is None:
        log.warning("KCEX_TOKEN_AT is missing: cannot tell how old the session is. Re-run: python -m kcex.cli login")
    elif age >= SESSION_DAYS - 1:
        log.warning("session token is %.1f days old and dies at ~%d days. Re-run: python -m kcex.cli login", age, int(SESSION_DAYS))
    return age


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="KCEX spot bot")
    parser.add_argument("cmd", choices=["run"])
    parser.add_argument("--once", action="store_true", help="one loop iteration (one LLM cycle) then exit")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    if settings.mode not in {"paper", "live"}:
        log.error("MODE must be paper or live, got %r", settings.mode)
        return EXIT_CYCLE_FAILED

    token = ""
    if settings.mode == "live":
        token = require_live_token()
        warn_token_age(os.getenv("KCEX_TOKEN_AT"))
    client = KcexClient(token=token or None)
    store = Store(DB_PATH)
    eye = Eye(client, settings)
    eye.load_rules()
    if settings.mode == "live":
        hands: PaperHands | LiveHands = LiveHands(settings, store, client, rules=eye.rules)
    else:
        hands = PaperHands(settings, store)

    try:
        with InstanceLock(LOCK_PATH):
            return _loop(args.once, settings, client, store, eye, hands)
    except AlreadyRunning as exc:
        log.error("%s", exc)
        return EXIT_ALREADY_RUNNING


def _loop(once: bool, settings: Settings, client: KcexClient, store: Store, eye: Eye, hands: PaperHands | LiveHands) -> int:
    budget = Budget(spent_usd=0.0, cap_usd=settings.llm_daily_budget_usd, day=utc_day())
    log.info("mode=%s symbol=%s cycle=%dmin ws=%s", settings.mode, settings.symbol, settings.cycle_minutes, settings.ws_enabled)
    try:
        if settings.mode == "live":
            log.info("boot reconcile: %s", hands.reconcile())
    except UnprotectedPosition as exc:
        log.critical("UNPROTECTED POSITION at boot: %s. Fix it on the exchange, then restart.", exc)
        return EXIT_UNPROTECTED
    eye.connect_ws()
    try:
        eye.snapshot_rest()
    except Exception as exc:  # noqa: BLE001
        log.warning("rest snapshot failed: %s", exc)

    last_llm_ms = 0
    last_px = 0.0
    backoff = 1.0
    while True:
        budget.roll_day(utc_day())
        try:
            last_llm_ms, last_px, gate = run_once(
                settings=settings,
                eye=eye,
                store=store,
                client=client,
                hands=hands,
                budget=budget,
                last_llm_ms=last_llm_ms,
                last_px=last_px,
            )
            backoff = 1.0
        except SessionDead as exc:
            log.critical("session dead: %s. Run: python -m kcex.cli login", exc)
            return EXIT_SESSION_DEAD
        except UnprotectedPosition as exc:
            log.critical("UNPROTECTED POSITION: %s. Fix it on the exchange, then restart.", exc)
            return EXIT_UNPROTECTED
        except KeyboardInterrupt:
            log.info("stopped by user")
            return EXIT_OK
        except Exception as exc:  # noqa: BLE001 - keep the loop alive, loudly
            log.error("cycle failed: %s: %s (retry in %.0fs)", type(exc).__name__, exc, backoff, exc_info=True)
            if once:
                return EXIT_CYCLE_FAILED
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
            continue
        if gate is not None:
            log.info("decision %s rule=%s qty=%s stop=%s budget=%.4f/%.2f", gate.action, gate.rule, gate.qty, gate.stop_price, budget.spent_usd, budget.cap_usd)
        if once and gate is not None:
            return EXIT_OK
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
