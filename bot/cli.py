"""``python -m bot run [--once]``

Exit codes: 0 ok, 1 session dead (re-run ``python -m kcex.cli login``), 2 unprotected
position on the exchange (fix it by hand, then restart), 3 another instance holds the
lock, 4 a --once cycle failed, 5 stuck position (protected, but the bot cannot exit it
-- the resident stop is not a bot order; square it by hand), 6 terminal evidence
unavailable (a live discretionary exit -- LLM SELL, local take-profit, time limit --
was refused before any write; this attempt changed no orders, but that alone does not
prove a resident stop is still protecting the position -- capture the missing
order-history/deals evidence in docs/kcex-spot-api.md, or inspect the exchange and exit
by hand), 7 exit latch blocked (a durable EXIT-latch record, or a legacy CLOSING row
with no latch, fences every write path; this is not auto-resumable and no timeout
resolves it -- a human must inspect the exchange directly).

HALT CONTRACT for exit code 6 specifically (2026-09 third-round re-review corrected
this): the barrier tick that can raise ``TerminalEvidenceUnavailable`` runs BEFORE
``poll_heavy()``/the LLM section of the SAME cycle, and a raise here ends the process
-- there is no bounded "the next LLM cycle repairs it" recovery, because there is no
next cycle at all until a human restarts the process. If the resident stop is in fact
gone (executed or cancelled by something else), the position can sit on the exchange
with NO protection at all for as long as it takes a human to notice and act. Exit 6
is not "the stop is fine, just wait" -- it is "we do not know, and nothing will look
again until you do."
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
from bot.hands import (
    ExitLatchBlocked,
    LiveHands,
    PaperHands,
    PositionStuck,
    TerminalEvidenceUnavailable,
    UnprotectedPosition,
)
from bot.settings import Settings
from bot.store import Store
from kcex.client import KcexClient
from kcex.login import require_live_token
from kcex.session import SESSION_DAYS, token_age_days

log = logging.getLogger("bot")

# Relative to this checkout's own location, not the process's cwd: invoking
# the same installed module (e.g. python -m bot run) from a different
# working directory must resolve the exact same ledger/lock, never a second
# one silently created next to wherever the process happened to be launched.
# A genuinely separate worktree/checkout has its own bot/cli.py at a
# different path, so its data/ stays genuinely separate too.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "bot.db"


def db_path_for_mode(mode: str) -> Path:
    """One database per mode, so live can never reach a simulated position.

    Paper keeps `bot.db` and its existing history; live gets its own file. The
    separation is structural -- the stamp in `Store` and the provenance check in
    `LiveHands` are the second and third layers, for a human who points a flag
    somewhere unexpected.
    """
    return DB_PATH if mode == "paper" else DATA_DIR / f"bot-{mode}.db"
LOCK_PATH = DATA_DIR / "bot.lock"
LOG_PATH = DATA_DIR / "bot.log"

EXIT_OK = 0
EXIT_SESSION_DEAD = 1
EXIT_UNPROTECTED = 2
EXIT_ALREADY_RUNNING = 3
EXIT_CYCLE_FAILED = 4
# The position is protected but the bot cannot exit it on its own; retrying would
# repeat the same impossible exit every cycle.
EXIT_STUCK = 5
# A live discretionary exit (LLM SELL, local take-profit, time limit) was refused
# before any write because terminal evidence to prove a cancel is safe is not
# captured (bot.hands.TerminalEvidenceUnavailable). The condition that triggered it
# (e.g. a crossed take-profit) does not resolve itself, so retrying is a livelock,
# not resilience: halt instead. Distinct from EXIT_STUCK (5) on purpose -- the
# operator's remedy differs. EXIT_STUCK means a *foreign* resident stop can never be
# cancelled by this bot (square it by hand). This code means the bot cannot yet prove
# a cancel of *any* stop -- foreign or its own -- succeeded rather than executed a
# moment before or after the read; the fix is either to capture the missing
# order-history/deals evidence (docs/kcex-spot-api.md) or to exit the position by
# hand today. One exit code mapping to one operator action beats one code mapping to
# two different ones. IMPORTANT (2026-09 third-round re-review): the barrier that
# raises this runs BEFORE poll_heavy()/the LLM section in the SAME cycle, so this
# code means the process HALTS NOW with no next cycle to repair anything -- not "the
# next LLM cycle will fix it". If the resident stop is genuinely gone the position may
# be completely unprotected until a human notices; there is no bound on that window.
EXIT_TERMINAL_EVIDENCE_UNAVAILABLE = 6
# A durable EXIT latch is present (a foreign/future writer, or a resumed one of
# ours), the position is a legacy CLOSING row with no latch, or the latch is
# corrupt/mismatched/unreadable (bot.hands.ExitLatchBlocked). This is its own
# code, not a share of EXIT_STUCK (5) or EXIT_TERMINAL_EVIDENCE_UNAVAILABLE (6):
# 5 is a *foreign* stop this bot can never cancel; 6 is "we have not started an
# exit and refuse to, the stop is untouched going in"; 7 is "an exit attempt (or
# a state that looks like one) is already mid-flight or ambiguous, and no
# transition out of it is safe without a human" -- a materially different
# remedy (inspect the in-flight state itself, not just the resident stop), so
# it gets its own code rather than overloading either existing one.
EXIT_EXIT_LATCH_BLOCKED = 7


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
        except ImportError as exc:
            # A platform with no fcntl cannot prove exclusion at all -- silently
            # proceeding "best effort" is exactly the silent paper/live
            # concurrency this lock exists to prevent. Fail closed instead of
            # pretending an unenforced lock was taken.
            fh.close()
            raise RuntimeError(
                f"cannot lock {self.path}: the fcntl module is unavailable on "
                "this platform, so exclusive access cannot be proven. Refusing "
                "to start rather than risk two instances trading unlocked."
            ) from exc
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


def setup_logging(level: str, log_path: Path | None = None) -> None:
    """Console (stderr) only. Failures before the instance lock is held
    (bad MODE, a stuck lock) must still be visible without ever touching
    disk state pre-lock -- see add_file_logging() for the on-disk handler,
    added only once the lock is actually acquired."""
    root = logging.getLogger()
    if not root.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if log_path is not None:
        add_file_logging(log_path)


def add_file_logging(log_path: Path) -> None:
    """Adds the on-disk log handler; idempotent. Called from main() only
    after the instance lock is held, so a process that never gets the lock
    (or fails validation before trying) never creates/touches bot.log."""
    root = logging.getLogger()
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def warn_token_age(token_at: str | None) -> float | None:
    age = token_age_days(token_at)
    if age is None:
        log.warning("KCEX_TOKEN_AT is missing: cannot tell how old the session is. Re-run: python -m kcex.cli login")
    elif age >= SESSION_DAYS - 1:
        log.warning("session token is %.1f days old and dies at ~%d days. Re-run: python -m kcex.cli login", age, int(SESSION_DAYS))
    return age


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KCEX spot bot")
    parser.add_argument("cmd", choices=["run"])
    parser.add_argument("--once", action="store_true", help="one loop iteration (one LLM cycle) then exit")
    parser.add_argument(
        "--chart",
        action="store_true",
        default=False,
        help="serve the read-only local candlestick chart on loopback",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    if settings.mode not in {"paper", "live"}:
        log.error("MODE must be paper or live, got %r", settings.mode)
        return EXIT_CYCLE_FAILED

    token = ""
    if settings.mode == "live":
        token = require_live_token()
        warn_token_age(os.getenv("KCEX_TOKEN_AT"))

    try:
        with InstanceLock(LOCK_PATH):
            # File logging only starts once the lock is held: a process that
            # loses the race to AlreadyRunning (or fails MODE validation
            # above) must never create/touch bot.log at all. Console logging
            # from setup_logging() above already covers pre-lock failures.
            add_file_logging(LOG_PATH)
            # Every initializer below has a side effect -- opening/migrating
            # the database, starting the KCEX WS thread, a REST call for
            # symbol rules, binding the chart's HTTP/WS server, LiveHands'
            # own construction -- and now runs only once the exclusive lock is
            # held. Running them first (as before) let two racing processes
            # both create/migrate bot.db, both open a KCEX WS connection, and
            # both attempt to bind the chart port before only one of them
            # discovered, at the very end, that it should never have started.
            #
            # `token` is already the empty string in paper mode -- pass it
            # explicitly rather than `token or None`, which collapses "" to
            # None and makes KcexClient() fall back to reading KCEX_TOKEN from
            # the environment. Paper must never authenticate, even when a
            # stale KCEX_TOKEN from a prior live login is still sitting in
            # .env/the shell.
            client = KcexClient(token=token)
            store = Store(db_path_for_mode(settings.mode), mode=settings.mode)
            eye = Eye(client, settings)
            eye.start_ws_thread()
            eye.load_rules()
            chart = None
            if args.chart:
                from bot.chart_server import ChartServer

                try:
                    chart = ChartServer(
                        hub=eye.hub,
                        client=client,
                        host=settings.chart_host,
                        port=settings.chart_port,
                        symbol=settings.symbol,
                    )
                    chart.start()
                except (ValueError, OSError) as exc:
                    print(f"chart server failed to start: {exc}")
                    return 1
                print(f"chart http://{settings.chart_host}:{settings.chart_port}/")
            if settings.mode == "live":
                hands: PaperHands | LiveHands = LiveHands(settings, store, client, rules=eye.rules)
            else:
                hands = PaperHands(settings, store)
            return _loop(args.once, settings, client, store, eye, hands)
    except AlreadyRunning as exc:
        log.error("%s", exc)
        return EXIT_ALREADY_RUNNING


def _loop(once: bool, settings: Settings, client: KcexClient, store: Store, eye: Eye, hands: PaperHands | LiveHands) -> int:
    # The durable admission gate lives at the real request boundary now
    # (bot.brain.think_result/reflect_result, via store.reserve_budget/
    # settle_budget) -- this seed is only the in-memory fast-path/logging
    # view, seeded from what is durably known so a same-day restart does not
    # visually look like a full re-authorized cap. An unreadable/corrupt kv
    # row must not crash boot (the fatal-halt precedence -- stop monitoring,
    # session checks -- is not a budget concern), but it must also not be
    # silently treated as "resume at zero": reserve_budget() will keep
    # raising the same BudgetStateCorrupt on every real attempt to spend,
    # so every LLM cycle correctly refuses with REASON_BUDGET_STATE while
    # the rest of the loop (barriers, stop monitoring) keeps running.
    today = utc_day()
    try:
        persisted = store.budget_load()
    except Exception as exc:  # noqa: BLE001 - any read failure degrades boot, never crashes it
        log.error("budget state unreadable at boot; new LLM spend will refuse until fixed: %s", exc)
        persisted = None
    if persisted and persisted["day"] == today:
        budget = Budget(spent_usd=persisted["spent_usd"], cap_usd=settings.llm_daily_budget_usd,
                        day=today, calls=persisted["calls"])
    else:
        budget = Budget(spent_usd=0.0, cap_usd=settings.llm_daily_budget_usd, day=today)
    log.info("mode=%s symbol=%s cycle=%dmin ws=%s budget=%.4f/%.2f", settings.mode, settings.symbol,
             settings.cycle_minutes, settings.ws_enabled, budget.spent_usd, budget.cap_usd)
    try:
        if settings.mode == "live":
            log.info("boot reconcile: %s", hands.reconcile())
    except UnprotectedPosition as exc:
        log.critical("UNPROTECTED POSITION at boot: %s. Fix it on the exchange, then restart.", exc)
        return EXIT_UNPROTECTED
    except ExitLatchBlocked as exc:
        log.critical(
            "EXIT LATCH BLOCKED at boot: %s. A durable exit record (or a legacy CLOSING "
            "row, or a guard read failure) is fencing every write path; this is not "
            "auto-resumable and no timeout resolves it. Inspect the exchange by hand.",
            exc,
        )
        return EXIT_EXIT_LATCH_BLOCKED
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
        except PositionStuck as exc:
            log.critical("STUCK POSITION: %s", exc)
            return EXIT_STUCK
        except TerminalEvidenceUnavailable as exc:
            log.critical(
                "TERMINAL EVIDENCE UNAVAILABLE: %s. HALT CONTRACT: this call placed "
                "nothing, but that is NOT proof the resident stop is still there -- if "
                "it is in fact gone, the position may be sitting on the exchange "
                "completely UNPROTECTED. This process exits now and will NOT retry, "
                "repair, or resume by itself: there is no next cycle -- not the next "
                "LLM cycle, not a reconcile() -- to fix this later. A human must "
                "inspect the exchange directly and either restore protection or close "
                "the position by hand before the bot runs again. Capture the missing "
                "order-history/deals evidence (docs/kcex-spot-api.md) to close this gap "
                "for good.",
                exc,
            )
            return EXIT_TERMINAL_EVIDENCE_UNAVAILABLE
        except ExitLatchBlocked as exc:
            log.critical(
                "EXIT LATCH BLOCKED: %s. A durable exit record (or a legacy CLOSING row, "
                "or a guard read failure) is fencing every write path; this condition does "
                "not resolve itself and retrying is a livelock, not resilience. Inspect "
                "the exchange by hand.",
                exc,
            )
            return EXIT_EXIT_LATCH_BLOCKED
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
