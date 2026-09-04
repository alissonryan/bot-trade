from __future__ import annotations

import time
from datetime import datetime, timezone

from bot.brain import Budget, think
from bot.collar import decide
from bot.eye import Eye
from bot.hands import LiveHands, PaperHands
from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, TradeIntent
from kcex.client import KcexClient


def due(now_ms: int, last_llm_ms: int, last_px: float, px: float, settings: Settings) -> bool:
    if last_llm_ms == 0:
        return True
    if now_ms - last_llm_ms >= settings.cycle_minutes * 60_000:
        return True
    if last_px > 0 and abs(px / last_px - 1.0) >= settings.wake_move_pct:
        return True
    return False


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def run_once(
    *,
    settings: Settings,
    eye: Eye,
    store: Store,
    client: KcexClient,
    hands: PaperHands | LiveHands,
    budget: Budget,
    last_llm_ms: int,
    last_px: float,
) -> tuple[int, float, GateResult | None]:
    snap = eye.snapshot()
    if snap.last <= 0:
        snap = eye.snapshot_rest()
    now = int(time.time() * 1000)
    if not due(now, last_llm_ms, last_px, snap.last, settings):
        if isinstance(hands, PaperHands):
            hands.mark(snap)
        return last_llm_ms, last_px, None
    session_ok = True
    if settings.mode == "live":
        try:
            client.user_info()
        except Exception:
            session_ok = False
    intent = think(snap, settings, budget)
    if intent is None:
        intent = TradeIntent("HOLD", 0.0, "no_llm", "unknown")
    eye.bot_qty = hands.position.qty
    eye.bot_avg_entry = hands.position.entry or None
    snap = eye.snapshot()
    gate = decide(
        intent,
        snap,
        settings,
        session_ok=session_ok,
        day_pnl_usdt=store.day_pnl(utc_day()),
    )
    if gate.ok:
        hands.execute(gate, snap)
        eye.bot_qty = hands.position.qty
        eye.bot_avg_entry = hands.position.entry or None
    store.append_audit(intent, gate, settings.mode, None)
    eye.last_intent_action = intent.action
    return now, snap.last, gate
