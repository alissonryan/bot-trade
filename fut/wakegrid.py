"""Read-only in-sample replay of Jev entry wakes."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from fut.collar import cost_gate
from fut.settings import FutSettings

STREAKS = (1, 2, 3, 4, 6)
THRESHOLDS = (0.50, 0.55, 0.60, 0.65, 0.70)
REGIME_SETS = ((), ("trend", "volatile"))
TAKER_FEE_BPS = 1.0
HOLD_MS = 60_000


@dataclass(frozen=True)
class ReplaySnapshot:
    bid: float
    ask: float
    last: float
    spread_bps: float
    atr_1m: float | None
    stale: bool = False

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last


@dataclass(frozen=True)
class ReplayResult:
    streak: int
    threshold: float
    regimes: tuple[str, ...]
    gates: bool
    wakes: int
    trades: int
    mean_net_bps: float
    sum_net_bps: float
    win_rate: float


@dataclass(frozen=True)
class _ReplaySpec:
    taker_fee: float = TAKER_FEE_BPS / 10_000


def _main_position_open(payload: dict[str, Any]) -> bool:
    candidates = [payload.get("position")]
    state = payload.get("state")
    if isinstance(state, dict):
        candidates.append(state.get("position"))
    for position in candidates:
        if isinstance(position, dict) and position.get("side") in ("long", "short"):
            return True
    return False


def load_rows(path: Path, *, since_ms: int | None = None) -> list[dict[str, Any]]:
    """Load Jev rows without opening/migrating/stamping the paper store."""
    uri = f"file:{Path(path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        if since_ms is None:
            rows = conn.execute(
                "SELECT id, ts_ms, payload FROM fut_decisions WHERE kind='jev' ORDER BY ts_ms, id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts_ms, payload FROM fut_decisions "
                "WHERE kind='jev' AND ts_ms >= ? ORDER BY ts_ms, id", (since_ms,)
            ).fetchall()
    return [{"id": row[0], "ts_ms": row[1], "payload": json.loads(row[2])} for row in rows]


def _snapshot(payload: dict[str, Any]) -> ReplaySnapshot | None:
    raw = payload.get("snapshot")
    if not isinstance(raw, dict):
        return None
    try:
        bid, ask = float(raw.get("bid", 0)), float(raw.get("ask", 0))
        last = float(raw.get("last", 0))
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
        spread = raw.get("spread_bps")
        spread = float(spread) if spread is not None else ((ask - bid) / mid * 10_000 if mid > 0 else 0.0)
        atr = raw.get("atr_1m")
        atr = None if atr is None else float(atr)
        stale = bool(raw.get("stale", False))
    except (TypeError, ValueError):
        return None
    values = (bid, ask, last, spread)
    if not all(math.isfinite(value) for value in values) or not math.isfinite(mid):
        return None
    if atr is not None and not math.isfinite(atr):
        atr = None
    return ReplaySnapshot(bid, ask, last, spread, atr, stale)


def _answer(payload: dict[str, Any]) -> tuple[str, float, float, str] | None:
    if payload.get("error"):
        return None
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        return None
    try:
        direction = str(answers["direction"])
        confidence = float(answers["direction_conf"])
        beats_cost = float(answers["beats_cost"])
        regime = str(answers["regime"]).lower()
    except (KeyError, TypeError, ValueError):
        return None
    if direction not in ("up", "down", "flat") or not all(math.isfinite(value) for value in (confidence, beats_cost)):
        return None
    return direction, confidence, beats_cost, regime


def _side(answer: tuple[str, float, float, str], threshold: float, regimes: tuple[str, ...]) -> str | None:
    direction, confidence, beats_cost, regime = answer
    if confidence < threshold or beats_cost < threshold or (regimes and regime not in regimes):
        return None
    return "long" if direction == "up" else "short"


def replay(rows: Iterable[dict[str, Any]], *, streak: int, threshold: float,
           regimes: tuple[str, ...], gates: bool, settings: FutSettings | None = None) -> ReplayResult:
    source_settings = settings or FutSettings.from_env()
    settings = FutSettings(max_hold_s=60.0, slippage_bps=source_settings.slippage_bps,
                           max_spread_bps=(source_settings.max_spread_bps or 3.0) if gates else 0.0,
                           min_move_mult=(source_settings.min_move_mult or 2.0) if gates else 0.0)
    spec = _ReplaySpec()
    cost_bps = 2 * (spec.taker_fee * 10_000 + settings.slippage_bps)
    current: tuple[str, int, float] | None = None
    current_side: str | None = None
    current_streak = 0
    wakes = 0
    net_values: list[float] = []

    for row in sorted(rows, key=lambda item: (int(item["ts_ms"]), int(item.get("id", 0)))):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        snap = _snapshot(payload)
        if snap is None:
            continue
        ts_ms = int(row["ts_ms"])
        if current is not None:
            side, opened_ms, entry = current
            if ts_ms >= opened_ms + HOLD_MS:
                exit_price = snap.bid if side == "long" else snap.ask
                if exit_price > 0:
                    gross_bps = ((exit_price - entry) / entry * 10_000 if side == "long"
                                 else (entry - exit_price) / entry * 10_000)
                    net_values.append(gross_bps - cost_bps)
                current = None
                current_side = None
                current_streak = 0
                continue
            current_side = None
            current_streak = 0
            continue

        answer = _answer(payload)
        if answer is None:
            current_side = None
            current_streak = 0
            continue
        if _main_position_open(payload):
            current_side = None
            current_streak = 0
            continue

        side = _side(answer, threshold, regimes)
        if side is None:
            current_side = None
            current_streak = 0
            continue
        if side == current_side:
            current_streak += 1
        else:
            current_side = side
            current_streak = 1
        if current_streak < streak or snap.stale:
            if snap.stale:
                current_side = None
                current_streak = 0
            continue
        if gates and cost_gate(snap, spec=spec, settings=settings) is not None:
            continue
        entry = snap.ask if side == "long" else snap.bid
        if entry <= 0:
            continue
        wakes += 1
        current = side, ts_ms, entry

    total = sum(net_values)
    return ReplayResult(streak, threshold, regimes, gates, wakes, len(net_values),
                        total / len(net_values) if net_values else 0.0, total,
                        sum(value > 0 for value in net_values) / len(net_values) if net_values else 0.0)


def grid_results(rows: Iterable[dict[str, Any]]) -> list[ReplayResult]:
    rows = list(rows)
    return [replay(rows, streak=streak, threshold=threshold, regimes=regimes, gates=gates)
            for streak in STREAKS for threshold in THRESHOLDS
            for regimes in REGIME_SETS for gates in (False, True)]


def render_grid(results: Iterable[ReplayResult]) -> str:
    lines = [
        "WARNING: in-sample over the supplied rows; check the selected settings on new data.",
        "Replay uses a fixed 60s hold and no stop; gaps from restarts or stale data lengthen holds; wakes = entries.",
        "streak threshold regime gates wakes trades mean_net_bps sum_net_bps win_rate",
    ]
    for result in sorted(results, key=lambda item: item.sum_net_bps, reverse=True):
        regime = "any" if not result.regimes else "trend+volatile"
        lines.append(f"{result.streak:>6} {result.threshold:>9.2f} {regime:>14} "
                     f"{'on' if result.gates else 'off':>5} {result.wakes:>5} {result.trades:>6} "
                     f"{result.mean_net_bps:>13.2f} {result.sum_net_bps:>12.2f} {result.win_rate:>8.2%}")
    return "\n".join(lines)
