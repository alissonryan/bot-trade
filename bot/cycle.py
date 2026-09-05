"""One loop iteration: a cheap quote refresh every second and, when due, the LLM cycle
(heavy REST reads, live reconcile, session check, brain, collar, hands, audit)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from bot.brain import Budget, ThinkResult, think_result
from bot.collar import decide
from bot.eye import Eye
from bot.hands import LiveHands, PaperHands
from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, TradeIntent
from kcex.client import KcexClient

log = logging.getLogger(__name__)


class SessionDead(RuntimeError):
    """Live session returned 401 / auth failure. Halt the process."""


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


def unrealized_pnl(hands: PaperHands | LiveHands, mark: float) -> float:
    pos = hands.position
    if pos.qty <= 0 or not mark:
        return 0.0
    return (mark - pos.entry) * pos.qty


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
    think: Callable[..., ThinkResult] | None = None,
) -> tuple[int, float, GateResult | None]:
    eye.poll_quotes()  # never raises; a failure just leaves the quotes stale
    snap = eye.snapshot()
    now = int(time.time() * 1000)
    if not due(now, last_llm_ms, last_px, snap.last, settings):
        if isinstance(hands, PaperHands):
            hands.mark(snap)
        return last_llm_ms, last_px, None

    eye.poll_heavy()  # may raise EyeError in live mode; the loop backs off and retries
    if settings.mode == "paper" and hasattr(hands, "cash"):
        eye.free_usdt = float(hands.cash)  # the paper ledger is the cash, not the KCEX balance

    session_ok = True
    if settings.mode == "live":
        try:
            client.user_info()
        except Exception as exc:
            raise SessionDead(str(exc)) from exc
        verdict = hands.reconcile()  # may raise UnprotectedPosition
        if verdict not in ("ok", "flat"):
            log.warning("reconcile: %s", verdict)

    day = utc_day()
    eye.bot_qty = hands.position.qty
    eye.bot_avg_entry = hands.position.entry or None
    eye.last_bot_pnl_usdt = store.day_pnl(day)
    snap = eye.snapshot()
    budget.roll_day(day)

    thinker = think or think_result
    result = thinker(snap, settings, budget)
    intent = result.intent or TradeIntent("HOLD", 0.0, result.reason, "unknown")
    gate = decide(
        intent,
        snap,
        settings,
        session_ok=session_ok,
        day_pnl_usdt=store.day_pnl(day),
        unrealized_pnl_usdt=unrealized_pnl(hands, snap.bid or snap.last),
        rules=getattr(eye, "rules", None),
    )

    exec_error: str | None = None
    try:
        if gate.ok:
            hands.execute(gate, snap)
    except Exception as exc:
        exec_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        eye.bot_qty = hands.position.qty
        eye.bot_avg_entry = hands.position.entry or None
        extra: dict[str, Any] = {
            "exec_error": exec_error,
            "position_state": hands.position.state,
            "stop_order_id": getattr(hands, "stop_order_id", None),
        }
        health = getattr(eye, "health", None)
        if callable(health):
            extra["eye"] = health()
        store.append_audit(
            intent,
            gate,
            settings.mode,
            order_id=getattr(hands, "entry_order_id", None) if gate.ok else None,
            snapshot=snap.compact(),
            llm=result.as_audit(),
            extra=extra,
        )
        eye.last_intent_action = intent.action
        eye.last_bot_pnl_usdt = store.day_pnl(day)
    return now, snap.last, gate
