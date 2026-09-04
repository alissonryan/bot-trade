from __future__ import annotations

import argparse
import time
from pathlib import Path

from dotenv import load_dotenv

from bot.brain import Budget
from bot.cycle import SessionDead, run_once, utc_day
from bot.eye import Eye
from bot.hands import LiveHands, PaperHands
from bot.settings import Settings
from bot.store import Store
from kcex.client import KcexClient
from kcex.login import require_live_token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["run"])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--chart", action="store_true", default=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    token = ""
    if settings.mode == "live":
        token = require_live_token()
    client = KcexClient(token=token or None)
    store = Store(Path("data/bot.db"))
    eye = Eye(client, settings)
    eye.start_ws_thread()
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
        hands: PaperHands | LiveHands = LiveHands(settings, store, client)
    else:
        hands = PaperHands(settings, store)
    budget = Budget(spent_usd=0.0, cap_usd=settings.llm_daily_budget_usd, day=utc_day())
    last_llm_ms = 0
    last_px = 0.0
    try:
        eye.snapshot_rest()
    except Exception as exc:
        print(f"rest snapshot failed: {exc}")
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
        except SessionDead as exc:
            print(f"sessao morta: {exc}. Rode: python -m kcex.cli login")
            return 1
        if gate is not None:
            print(gate)
        if args.once:
            return 0
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
