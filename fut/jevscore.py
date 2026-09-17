"""Read-only scorer for the raw and semantic Jev A/B observations."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Iterable

VARIANTS = ("jev", "jev_ab")
HORIZON_MS = 60_000
MIN_FUTURE_MS = 55_000
MAX_FUTURE_MS = 75_000


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _mid(payload: Any) -> float | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
        return None
    snap = payload["snapshot"]
    bid, ask, last = (_number(snap.get(key)) for key in ("bid", "ask", "last"))
    if bid is None or ask is None or last is None:
        return None
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return last if last > 0 else None


def _answer(payload: Any) -> tuple[str, float, dict[str, Any] | None] | None:
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    answers = payload.get("answers")
    if not isinstance(answers, dict) or answers.get("direction") not in {"up", "down", "flat"}:
        return None
    confidence = _number(answers.get("direction_conf"))
    if confidence is None:
        return None
    probabilities = payload.get("probabilities")
    return str(answers["direction"]), confidence, probabilities if isinstance(probabilities, dict) else None


def _probability_up(direction: str, confidence: float, probabilities: dict[str, Any] | None) -> tuple[float, str]:
    if probabilities is not None:
        p_up = _number(probabilities.get("up"))
        if p_up is not None:
            return p_up, "probabilities"
    if direction == "up":
        return confidence, "confidence fallback (not a probability)"
    if direction == "down":
        return 1.0 - confidence, "confidence fallback (not a probability)"
    return 0.5, "confidence fallback (not a probability)"


def _future_mid(rows: list[dict[str, Any]], ts_ms: int) -> float | None:
    candidates = []
    for row in rows:
        later = int(row["ts_ms"]) - ts_ms
        if MIN_FUTURE_MS <= later <= MAX_FUTURE_MS:
            mid = _mid(row.get("payload"))
            if mid is not None:
                candidates.append((abs(later - HORIZON_MS), later, int(row.get("id", 0)), mid))
    return min(candidates)[-1] if candidates else None


def _empty() -> dict[str, Any]:
    return {"rows_scored": 0, "rows_skipped": 0, "share": {key: 0.0 for key in ("up", "down", "flat")},
            "realized_share": {key: 0.0 for key in ("up", "down", "flat")},
            "directional_hit_rate": None, "mean_realized_bps_in_answered_direction": None,
            "brier_score": None, "brier_source": "unavailable"}


def score_rows(rows: Iterable[dict[str, Any]], *, since_ms: int | None = None) -> dict[str, dict[str, Any]]:
    rows = sorted(rows, key=lambda row: (int(row["ts_ms"]), int(row.get("id", 0))))
    results = {variant: _empty() for variant in VARIANTS}
    source_rows = {variant: [row for row in rows if row.get("kind") == variant and
                             (since_ms is None or int(row["ts_ms"]) >= since_ms)]
                   for variant in VARIANTS}
    for variant, variant_rows in source_rows.items():
        scored = []
        for row in variant_rows:
            answer = _answer(row.get("payload"))
            future = _future_mid(rows, int(row["ts_ms"]))
            if answer is None or future is None:
                results[variant]["rows_skipped"] += 1
                continue
            direction, confidence, probabilities = answer
            current = _mid(row.get("payload"))
            if current is None or current <= 0:
                results[variant]["rows_skipped"] += 1
                continue
            realized_bps = (future - current) / current * 10_000
            realized_direction = "up" if realized_bps > 0 else "down" if realized_bps < 0 else "flat"
            scored.append((direction, confidence, probabilities, realized_bps, realized_direction))

        result = results[variant]
        result["rows_scored"] = len(scored)
        for direction, _, _, _, _ in scored:
            result["share"][direction] += 1 / len(scored) if scored else 0
        for _, _, _, _, direction in scored:
            result["realized_share"][direction] += 1 / len(scored) if scored else 0
        directional = [item for item in scored if item[0] in ("up", "down") and item[4] in ("up", "down")]
        if directional:
            result["directional_hit_rate"] = sum(item[0] == item[4] for item in directional) / len(directional)
        answered_moves = [item[3] if item[0] == "up" else -item[3] for item in scored if item[0] in ("up", "down")]
        if answered_moves:
            result["mean_realized_bps_in_answered_direction"] = sum(answered_moves) / len(answered_moves)
        brier = []
        sources = []
        for direction, confidence, probabilities, _, realized_direction in scored:
            p_up, source = _probability_up(direction, confidence, probabilities)
            brier.append((p_up - (1.0 if realized_direction == "up" else 0.0)) ** 2)
            sources.append(source)
        if brier:
            result["brier_score"] = sum(brier) / len(brier)
            result["brier_source"] = ("probabilities" if all(source == "probabilities" for source in sources)
                                       else "confidence fallback (not a probability)" if
                                       all(source != "probabilities" for source in sources)
                                       else "probabilities + confidence fallback (not a probability)")
    return results


def score_database(path: Path, *, since_ms: int | None = None) -> dict[str, dict[str, Any]]:
    uri = f"file:{Path(path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = [{"id": row[0], "ts_ms": row[1], "kind": row[2], "payload": json.loads(row[3])}
                for row in conn.execute("SELECT id, ts_ms, kind, payload FROM fut_decisions ORDER BY ts_ms, id")]
    return score_rows(rows, since_ms=since_ms)


def render(results: dict[str, dict[str, Any]]) -> str:
    lines = ["Jev score (realized mid around 60s later)",
             "variant | scored | skipped | answer up/down/flat | realized up/down/flat | hit excl flat | mean bps in answered direction | Brier"]
    for variant in VARIANTS:
        result = results[variant]
        share = result["share"]
        realized = result["realized_share"]
        lines.append(f"{variant:7} | {result['rows_scored']:7} | {result['rows_skipped']:7} | "
                     f"{share['up']:.1%}/{share['down']:.1%}/{share['flat']:.1%} | "
                     f"{realized['up']:.1%}/{realized['down']:.1%}/{realized['flat']:.1%} | "
                     f"{_fmt(result['directional_hit_rate'])} | "
                     f"{_fmt(result['mean_realized_bps_in_answered_direction'])} | "
                     f"{_fmt(result['brier_score'])} ({result['brier_source']})")
    lines.append("WARNING: rows from different configurations are not comparable")
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"
