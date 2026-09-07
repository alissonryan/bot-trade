"""One loop iteration: a cheap quote refresh every second and, when due, the LLM cycle
(heavy REST reads, live reconcile, session check, brain, collar, hands, audit)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from bot.brain import Budget, ThinkResult, think_result, reflect_result
from bot.collar import decide
from bot.eye import Eye
from bot.hands import LiveHands, PaperHands, local_exit_reason
from bot.journal import record_decision, deferred_reflection
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
    reflect: Callable | None = None,
) -> tuple[int, float, GateResult | None]:
    eye.poll_quotes()  # never raises; a failure just leaves the quotes stale
    eye.bot_qty = hands.position.qty
    eye.bot_avg_entry = hands.position.entry or None
    snap = eye.snapshot()
    now = int(time.time() * 1000)
    # Barriers precede the LLM timer in BOTH modes. No private polling on idle
    # ticks: only a prospective live exit warrants a session/balance check.
    barrier_error = None
    barrier_gate = None
    tick_enabled = settings.tp_atr_mult > 0 or settings.time_limit_minutes > 0
    hands.last_mark_reason = None
    try:
        reason = local_exit_reason(hands.position, snap, settings, now)
        if settings.mode == "live" and reason:
            hands.last_mark_reason = reason
            try:
                client.user_info()
            except Exception as exc:
                raise SessionDead(str(exc)) from exc
        if tick_enabled:
            hands.mark(snap, now_ms=now)
    except Exception as exc:
        barrier_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if hands.last_mark_reason:
            barrier_gate = GateResult(True, hands.last_mark_reason, "SELL")
            try:
                eye.bot_qty = hands.position.qty
                eye.bot_avg_entry = hands.position.entry or None
                eye.last_bot_pnl_usdt = store.day_pnl(utc_day())
                store.append_audit(
                    TradeIntent("SELL", 1, hands.last_mark_reason, "risk"), barrier_gate, settings.mode,
                    snapshot=snap.compact(), llm=ThinkResult(None, "not_called_barrier").as_audit(),
                    extra={"exec_error": barrier_error, "position_state": hands.position.state,
                           "stop_order_id": hands.stop_order_id},
                )
            except Exception as audit_exc:
                log.error("barrier audit bookkeeping failed: %s", audit_exc)
                if barrier_error is None:
                    raise
    if barrier_gate:
        return last_llm_ms, last_px, barrier_gate  # do not re-enter on the exit tick
    if not due(now, last_llm_ms, last_px, snap.last, settings):
        if not tick_enabled and isinstance(hands, PaperHands):
            hands.mark(snap)  # exact pre-P1 scheduling/return/audit when opted out
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
    journal_id = None
    journal_error = None
    context = {}
    decision_ms = now
    if settings.journal_enabled:
        decision_ms = int(time.time() * 1000)
        try:
            context = {"lessons": store.journal_lessons(as_of_ms=decision_ms), "as_of_ms": decision_ms}
        except Exception:
            journal_error = "journal_read_error"
            log.exception("journal read failed; decision proceeds without lessons")
    result = thinker(snap, settings, budget, **context)
    intent = result.intent or TradeIntent("HOLD", 0.0, result.reason, "unknown")
    gate = decide(
        intent,
        snap,
        settings,
        session_ok=session_ok,
        day_pnl_usdt=store.day_pnl(day),
        unrealized_pnl_usdt=unrealized_pnl(hands, snap.bid or snap.last),
        rules=getattr(eye, "rules", None),
        last_loss_exit_ms=(store.last_loss_exit_ms()
                           if settings.cooldown_minutes > 0 and intent.action == "BUY" else None),
        now_ms=int(time.time() * 1000) if settings.cooldown_minutes > 0 else None,
    )

    if settings.journal_enabled:
        try:
            journal_id = record_decision(store, intent, snap, gate, decision_ms=decision_ms,
                                         known_ms=int(time.time() * 1000))
        except Exception:
            journal_error = "journal_write_error"
            log.exception("journal record failed; execution still has priority")
            if gate.ok and gate.action == "BUY":
                store.journal_detach()
    elif gate.ok and gate.action == "BUY":
        # Disabling the feature must not attach a new trade to an older pending
        # thesis from a prior enabled run. Does not create any journal/kv rows.
        store.journal_detach()

    exec_error: str | None = None
    try:
        if gate.ok:
            hands.execute(gate, snap)
    except Exception as exc:
        exec_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        # Bookkeeping must never replace the exception on its way out. A raise in
        # here while UnprotectedPosition is in flight would downgrade the halt to
        # the loop's generic "back off and keep trading" branch -- exactly the
        # failure invariant 3 exists to catch.
        try:
            eye.bot_qty = hands.position.qty
            eye.bot_avg_entry = hands.position.entry or None
            extra: dict[str, Any] = {
                "exec_error": exec_error,
                "position_state": hands.position.state,
                "stop_order_id": getattr(hands, "stop_order_id", None),
            }
            if settings.journal_enabled:
                extra.update(journal_id=journal_id, journal_error=journal_error)
                if exec_error is None:
                    try:
                        if journal_id is not None and gate.ok and gate.action == "BUY" and hands.position.qty <= 0:
                            store.journal_unfilled(journal_id, known_ms=int(time.time() * 1000))
                        extra["reflection"] = deferred_reflection(
                            store, settings, budget, as_of_ms=int(time.time() * 1000),
                            completed_ms=lambda: int(time.time() * 1000), reflect=reflect or reflect_result)
                    except Exception:
                        extra["reflection"] = {"reason": "reflection_store_error", "cost_usd": 0.0}
                        log.exception("deferred reflection bookkeeping failed")
                else:
                    extra["reflection"] = {"reason": "reflection_execution_error", "cost_usd": 0.0}
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
        except Exception as audit_exc:  # noqa: BLE001
            log.error("audit bookkeeping failed: %s", audit_exc)
            if exec_error is None:
                raise
    return now, snap.last, gate
