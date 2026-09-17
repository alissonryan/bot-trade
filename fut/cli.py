"""``python -m fut run [--max-seconds N]``, ``python -m fut report`` and ``python -m fut panel``.

Paper only: no private route, no KCEX_TOKEN, no order is ever sent.

Exit codes: 0 ok, 3 another futures instance holds data/futures.lock, 8 an open paper position
had no price for FUT_UNMONITORED_SECONDS, 9 data/futures-paper.db was written by another mode.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

import bot.cli as bot_cli
from bot.cli import AlreadyRunning, InstanceLock, add_file_logging, setup_logging
from bot.store import StoreIdentityMismatch
from fut.jev import make_jev
from fut.loop import FutLoop, Unmonitored, now_ms, seed_budget, start_ws_thread
from fut.panel.reader import PanelReader
from fut.panel.server import PanelServer
from fut.report import ReadOnlyReportStore, evaluate, render, summarize
from fut.settings import FutSettings
from fut.store import FutStore, day_of
from fut.wakegrid import grid_results, load_rows, render_grid
from kcex.fapi import FuturesPublic

log = logging.getLogger("fut")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "futures-paper.db"
LOCK_PATH = DATA_DIR / "futures.lock"
LOG_PATH = DATA_DIR / "futures.log"
ENV_PATH = ROOT / ".env"
PANEL_INDEX = ROOT / "panel" / "index.html"
PANEL_PORT = 8766

EXIT_OK = 0
EXIT_ALREADY_RUNNING = 3
EXIT_UNMONITORED = 8
EXIT_STORE_MISMATCH = 9
STEP_SLEEP_S = 0.2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fut")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the futures paper loop")
    run.add_argument("--max-seconds", type=float, default=None)
    report_parser = sub.add_parser("report", help="print the edge report")
    report_parser.add_argument("--since-ms", type=int, default=None)
    wakegrid = sub.add_parser("wakegrid", help="replay stored Jev wakes in read-only mode")
    wakegrid.add_argument("--since-ms", type=int, default=None)
    panel_cmd = sub.add_parser("panel", help="serve the read-only local panel (loopback only)")
    panel_cmd.add_argument("--host", default="127.0.0.1")
    panel_cmd.add_argument("--port", type=int, default=PANEL_PORT)
    args = parser.parse_args(argv)
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    if args.cmd == "report":
        return report(args.since_ms)
    if args.cmd == "wakegrid":
        return wakegrid_report(args.since_ms)
    if args.cmd == "panel":
        return panel(args.host, args.port)
    try:
        with InstanceLock(LOCK_PATH):
            add_file_logging(LOG_PATH)
            return run_loop(args.max_seconds)
    # Tests reload bot.cli, creating a new AlreadyRunning class; catch both identities.
    except (AlreadyRunning, bot_cli.AlreadyRunning) as exc:
        log.error("%s", exc)
        return EXIT_ALREADY_RUNNING


def report(since_ms: int | None = None) -> int:
    if not DB_PATH.exists():
        print(f"no futures paper database yet at {DB_PATH}")
        return EXIT_OK
    try:
        store = ReadOnlyReportStore(DB_PATH, since_ms=since_ms)
    except StoreIdentityMismatch as exc:
        log.error("%s", exc)
        return EXIT_STORE_MISMATCH
    load_dotenv(ENV_PATH)
    settings = FutSettings.from_env()
    try:
        summary = summarize(store, settings)
        print(render(summary, evaluate(summary, settings)))
    finally:
        store.close()
    return EXIT_OK


def wakegrid_report(since_ms: int | None) -> int:
    if not DB_PATH.exists():
        print(f"no futures paper database yet at {DB_PATH}")
        return EXIT_OK
    print(render_grid(grid_results(load_rows(DB_PATH, since_ms=since_ms))))
    return EXIT_OK


def panel(host: str, port: int) -> int:
    # Read-only and lock-free on purpose: it runs beside `fut run`. It reads no .env; the only
    # setting it needs is the max hold for the "closes in" estimate, taken from its own environment.
    try:
        max_hold_s = float(os.getenv("FUT_MAX_HOLD_SECONDS", "300"))
    except ValueError:
        max_hold_s = 300.0
    try:
        jev_every_s = float(os.getenv("FUT_JEV_EVERY_SECONDS", "2.0"))
    except ValueError:
        jev_every_s = 2.0
    try:
        server = PanelServer(reader=PanelReader(DB_PATH), index_path=PANEL_INDEX, host=host, port=port,
                             max_hold_s=max_hold_s, jev_every_s=jev_every_s)
    except ValueError as exc:
        print(f"panel is loopback only: {exc}")
        return 1
    print(f"futures panel (read-only): http://{host}:{port}/  — Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"cannot serve on {host}:{port}: {exc}")
        return 1
    finally:
        server.shutdown()
    return EXIT_OK


def run_loop(max_seconds: float | None) -> int:
    load_dotenv(ENV_PATH)
    settings = FutSettings.from_env()
    try:
        store = FutStore(DB_PATH)
    except StoreIdentityMismatch as exc:
        log.error("%s", exc)
        return EXIT_STORE_MISMATCH
    rest = FuturesPublic()
    spec = rest.contract_detail(settings.symbol)
    events: queue.SimpleQueue = queue.SimpleQueue()
    stop = threading.Event()
    if settings.ws_url:
        start_ws_thread(settings, events, stop)
    else:
        log.warning("FUT_WS_URL disabled: REST-only prices, entries stay blocked as stale")
    if settings.uses_mock_jev:
        log.warning("Jev is the mock stand-in: this session never counts toward the edge criterion")
    loop = FutLoop(settings=settings, store=store, spec=spec, rest=rest, jev=make_jev(settings),
                   budget=seed_budget(store, settings.llm, today=day_of(now_ms())), events=events,
                   store_factory=lambda: FutStore(DB_PATH))
    log.info("futures paper: leverage %sx, margin %s USDT, jev %s, llm %s, taker fee %s",
             settings.leverage, settings.margin_usdt, getattr(loop.jev, "name", "?"),
             settings.llm.llm_model or "(unset)", spec.taker_fee)
    started = time.monotonic()
    try:
        while max_seconds is None or time.monotonic() - started < max_seconds:
            loop.step()
            time.sleep(STEP_SLEEP_S)
    except Unmonitored as exc:
        log.error("%s", exc)
        return EXIT_UNMONITORED
    except KeyboardInterrupt:
        log.info("stopped by operator")
    finally:
        stop.set()
    return EXIT_OK
