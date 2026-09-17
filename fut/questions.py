"""Every question Jev is asked and every threshold that turns its answers into an LLM call.

Review this file, not the loop, when the trigger behaves unexpectedly.
"""

from __future__ import annotations

from typesafe_sdk import Choice, Noul, NoulCriteria

from fut.types import FutPosition, FutSnapshot, JevVerdict


def _r(value, digits=2):
    return round(float(value), digits)


def build_questions(*, has_position: bool, move_cost_bps: float) -> dict:
    cost = f"{move_cost_bps:g} bps"
    questions = {
        "direction_60s": Choice(
            instructions={
                "question": "Where will the BTC_USDT perpetual mid be 60 seconds after `ts_ms`, relative to `mid`?",
                "goal": f"Seconds-horizon futures trading. A trade only pays if the move beats about {cost} of cost.",
                "inputs": "`flow` (aggressor contracts and cvd over 30s/120s) and `depth_bps`/`imbalance` are the "
                          "fastest signals; `returns_bps` is the recent path; `funding_rate` and "
                          "`fair_minus_last_bps` show positioning pressure.",
            },
            criteria={
                "up": f"Mid more than {cost} above the current mid after 60 seconds",
                "down": f"Mid more than {cost} below the current mid after 60 seconds",
                "flat": f"Mid within {cost} of the current mid after 60 seconds",
            },
        ),
        "move_beats_cost": Noul(
            instructions=f"Will the mid move more than {cost} in either direction within the next 60 seconds?",
            criteria=NoulCriteria(true=f"A move larger than {cost} is likely within 60 seconds",
                                  false=f"The mid is likely to stay within {cost} for 60 seconds"),
        ),
        "flow_aligned": Noul(
            instructions="Does the recent aggressor flow in `flow` push in the same direction as the recent "
                         "price path in `returns_bps`?",
        ),
        "regime": Choice(
            instructions="Which regime describes the last few minutes of this market?",
            criteria={
                "trend": "Persistent one-directional movement with aggressor flow on the same side",
                "range": "Price oscillating around a level without follow-through",
                "volatile": "Large fast swings in both directions",
            },
        ),
    }
    if has_position:
        questions["exit_now"] = Noul(
            instructions="Given `position`, has the case for keeping this position weakened enough that closing "
                         "now is better than waiting?",
            criteria=NoulCriteria(true="Flow, depth or the price path now point against the position side",
                                  false="The market still supports the position side, or nothing changed"),
        )
    return questions


def jev_state(snap: FutSnapshot, position: FutPosition, *, now_ms: int) -> dict:
    mid = snap.mid
    state = {
        "market": "BTC_USDT perpetual (KCEX)",
        "ts_ms": snap.ts_ms,
        "mid": _r(mid, 1),
        "spread_bps": _r(snap.spread_bps, 3),
        "imbalance": _r(snap.imbalance, 3),
        "depth_bps": snap.depth_bps,
        "returns_bps": {k: _r(v) for k, v in snap.returns_bps.items()},
        "flow": {k: {kk: (_r(vv, 1) if vv is not None else None) for kk, vv in v.items()} for k, v in snap.flow.items()},
        "funding_rate": snap.funding_rate,
        "fair_minus_last_bps": _r((snap.fair - snap.last) / snap.last * 10_000) if snap.fair > 0 and snap.last > 0 else 0.0,
    }
    if position.is_open() and mid > 0:
        sign = 1 if position.side == "long" else -1
        state["position"] = {
            "side": position.side,
            "seconds_open": max(0, (now_ms - position.opened_ms) // 1000),
            "unrealized_bps": _r(sign * (mid - position.entry) / position.entry * 10_000),
            "stop_distance_bps": _r(abs(mid - position.stop) / mid * 10_000) if position.stop else None,
            "liq_distance_bps": _r(abs(mid - position.liq) / mid * 10_000) if position.liq else None,
        }
    else:
        state["position"] = "flat"
    return state


def jev_side(verdict: JevVerdict) -> str | None:
    return {"up": "long", "down": "short"}.get(verdict.direction)


def entry_qualifies(verdict: JevVerdict, *, threshold: float, regimes: tuple[str, ...] = ()) -> str | None:
    if verdict.error:
        return None
    side = jev_side(verdict)
    if not side or verdict.direction_conf < threshold or verdict.beats_cost < threshold:
        return None
    if regimes and verdict.regime not in regimes:
        return None
    return side


def should_wake(verdict: JevVerdict, position: FutPosition, *, threshold: float,
                now_ms: int = 0, min_hold_s: float = 0.0, streak: int = 1,
                wake_streak: int = 1, regimes: tuple[str, ...] = ()) -> str | None:
    if verdict.error:
        return None
    if not position.is_open():
        side = entry_qualifies(verdict, threshold=threshold, regimes=regimes)
        if side and streak >= wake_streak:
            return "entry_signal"
        return None
    side = jev_side(verdict)
    # Exit/reversal wakes wait out the minimum hold; stop, liquidation and max hold
    # are enforced by the ledger every step and are not affected.
    if now_ms - position.opened_ms < min_hold_s * 1000:
        return None
    if verdict.exit_now is not None and verdict.exit_now >= threshold:
        return "exit_signal"
    if side and side != position.side and verdict.direction_conf >= threshold:
        return "reversal_signal"
    return None
