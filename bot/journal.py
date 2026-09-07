"""Shared point-in-time journal orchestration; no order authority."""
from __future__ import annotations

from typing import Callable

from bot.brain import Budget, ReflectionResult, REASON_REFLECTION_OFFLINE
from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, Snapshot, TradeIntent


def record_decision(store: Store, intent: TradeIntent, snap: Snapshot, gate: GateResult,
                    *, decision_ms: int, known_ms: int) -> int:
    tracking = gate.ok and gate.action == "BUY"
    jid = store.journal_begin(intent, snap.compact(), decision_ms=decision_ms, track_entry=tracking)
    if not tracking:
        store.journal_resolve(jid, {
            "kind": "exit_intent" if gate.ok and gate.action == "SELL" else "not_executed",
            "realized_pnl_usdt": None, "reason": gate.rule,
            "note": "HOLD is an observation, not a counterfactual trade; exits resolve the owning BUY",
        }, known_ms=known_ms)
    return jid


def deferred_reflection(store: Store, settings: Settings, budget: Budget, *, as_of_ms: int,
                        completed_ms: Callable[[], int], reflect=None) -> dict:
    """At most one, after execution. Offline is explicit, never invented prose."""
    lesson = store.journal_candidate(as_of_ms=as_of_ms)
    if lesson is None:
        return {"reason": "reflection_not_due", "cost_usd": 0.0}
    # Snapshot and original thesis are part of the reflection, not only PnL.
    evidence = {key: lesson[key] for key in ("decision_ms", "action", "confidence", "regime", "reason",
                                            "snapshot", "outcome", "outcome_known_ms")}
    try:
        result = (reflect(evidence, settings, budget) if reflect else
                  ReflectionResult(None, REASON_REFLECTION_OFFLINE))
    except Exception:
        result = ReflectionResult(None, "reflection_internal")
    audit = dict(result.as_audit(), journal_id=lesson["id"])
    try:
        store.journal_reflect(lesson["id"], result.text, audit, known_ms=completed_ms())
    except Exception:
        audit.update(result_reason=audit["reason"], reason="reflection_store_error")
    return audit
