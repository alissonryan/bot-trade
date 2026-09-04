from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import time

from bot.cycle import due
from bot.settings import Settings


def test_due_on_timer():
    s = Settings.from_env()
    assert due(now_ms=1_000_000, last_llm_ms=1_000_000 - 15 * 60_000, last_px=100, px=100, settings=s) is True


def test_not_due_early():
    s = Settings.from_env()
    assert due(now_ms=1_000_000, last_llm_ms=1_000_000 - 60_000, last_px=100, px=100, settings=s) is False


def test_due_on_wake_move():
    s = Settings.from_env()
    assert due(now_ms=1_000_000, last_llm_ms=1_000_000 - 1000, last_px=100, px=100.5, settings=s) is True


class _QuoteEye:
    def __init__(self):
        self.quotes = 0
        self.last = 100.0
        self.bot_qty = 0.0
        self.bot_avg_entry = None
        self.last_intent_action = None
        self.last_bot_pnl_usdt = 0.0

    def poll_quotes(self):
        self.quotes += 1

    def poll_heavy(self):
        return None

    def snapshot(self):
        from bot.types import Snapshot

        return Snapshot(
            ts_ms=1,
            last=self.last,
            bid=self.last - 1,
            ask=self.last + 1,
            spread=2,
            bars_15m=[],
            atr=1,
            free_usdt=450,
            bot_qty=self.bot_qty,
            bot_avg_entry=self.bot_avg_entry,
            ws_ok=False,
            stale=False,
        )


def test_run_once_polls_quotes_when_not_due(tmp_path):
    from bot.cycle import run_once
    from bot.brain import Budget
    from bot.hands import PaperHands
    from bot.store import Store

    s = Settings.from_env()
    eye = _QuoteEye()
    store = Store(tmp_path / "c.db")
    hands = PaperHands(s, store)

    class C:
        pass

    now = int(time.time() * 1000)
    last_llm, last_px, gate = run_once(
        settings=s,
        eye=eye,
        store=store,
        client=C(),
        hands=hands,
        budget=Budget(0, 2, "2026-09-04"),
        last_llm_ms=now,
        last_px=100.0,
    )
    assert eye.quotes == 1
    assert gate is None
    assert last_px == 100.0
    assert last_llm == now
