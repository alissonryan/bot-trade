"""Incremental decision cache shared by every local-panel request."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import statistics
import time
import threading
from typing import Any

from fut.panel.reader import PanelReader
from fut.panel.narrate import short_jev_error

SERIES_MS = 2 * 3_600_000
MAX_SERIES_POINTS = 600


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


class PanelCache:
    def __init__(self, reader: PanelReader, *, limit: int = 2000):
        self.reader = reader
        self.limit = limit
        self._lock = threading.Lock()
        self.last_id = 0
        self.last_ts_ms = 0
        self.loading = True
        self.snapshot: dict[str, Any] | None = None
        self.lifetime_costs = {"jev": 0.0, "llm": 0.0}
        self._day_costs: dict[str, dict[str, float]] = {}
        self._prices: deque[tuple[int, float]] = deque()
        self.jev_failures = 0
        self.jev_failure_since_ms: int | None = None
        self.jev_failure_reason: str | None = None
        self._jev_gaps: deque[int] = deque(maxlen=20)
        self._last_jev_ts_ms: int | None = None

    def _reset(self) -> None:
        self.last_id = 0
        self.last_ts_ms = 0
        self.loading = True
        self.snapshot = None
        self.lifetime_costs = {"jev": 0.0, "llm": 0.0}
        self._day_costs.clear()
        self._prices.clear()
        self.jev_failures = 0
        self.jev_failure_since_ms = None
        self.jev_failure_reason = None
        self._jev_gaps.clear()
        self._last_jev_ts_ms = None

    def _fold(self, fact: dict[str, Any]) -> None:
        self.last_id = int(fact["id"])
        self.last_ts_ms = int(fact["ts_ms"])
        kind = str(fact.get("kind") or "")
        cost = _number(fact.get("cost_usd")) or 0.0
        cost_kind = "jev" if kind == "jev_ab" else kind
        if cost_kind in self.lifetime_costs:
            self.lifetime_costs[cost_kind] += cost
            day = datetime.fromtimestamp(self.last_ts_ms / 1000, tz=timezone.utc).date().isoformat()
            totals = self._day_costs.setdefault(day, {"jev": 0.0, "llm": 0.0})
            totals[cost_kind] += cost

        if kind != "jev":
            return
        if self._last_jev_ts_ms is not None:
            gap = self.last_ts_ms - self._last_jev_ts_ms
            if gap > 0:
                self._jev_gaps.append(gap)
        self._last_jev_ts_ms = self.last_ts_ms
        error = fact.get("error")
        if error:
            if self.jev_failures == 0:
                self.jev_failure_since_ms = self.last_ts_ms
            self.jev_failures += 1
            self.jev_failure_reason = short_jev_error(error)
        else:
            self.jev_failures = 0
            self.jev_failure_since_ms = None
            self.jev_failure_reason = None
        bid = _number(fact.get("bid"))
        ask = _number(fact.get("ask"))
        last = _number(fact.get("last"))
        if bid is None and ask is None and last is None:
            return
        bid_value, ask_value, last_value = bid or 0.0, ask or 0.0, last or 0.0
        mid = (bid_value + ask_value) / 2 if bid_value > 0 and ask_value > 0 else last_value
        if mid > 0:
            self._prices.append((self.last_ts_ms, mid))
        spread_bps = _number(fact.get("spread_bps"))
        if spread_bps is None:
            spread_bps = ((ask_value - bid_value) / mid * 10_000) if mid > 0 and ask_value > bid_value else 0.0
        stale = fact.get("stale")
        self.snapshot = {"ts_ms": self.last_ts_ms, "last": last_value, "bid": bid_value, "ask": ask_value,
                         "spread_bps": spread_bps, "stale": bool(stale) if stale is not None else False}

    def _trim(self, now_ms: int) -> None:
        cutoff = now_ms - SERIES_MS
        while self._prices and self._prices[0][0] < cutoff:
            self._prices.popleft()

    def refresh(self, *, now_ms: int | None = None) -> None:
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        with self._lock:
            facts = self.reader.decision_facts(self.last_id, self.limit)
            max_id = getattr(facts, "max_id", None)
            if max_id is not None and max_id < self.last_id:
                self._reset()
                return
            for fact in facts:
                self._fold(fact)
            self.loading = len(facts) >= self.limit
            self._trim(now_ms)

    def costs_for_day(self, day: str) -> dict[str, float]:
        with self._lock:
            return dict(self._day_costs.get(day, {"jev": 0.0, "llm": 0.0}))

    def _price_series(self, since_ms: int, max_points: int) -> list[list[float | int]]:
        points = [[ts_ms, mid] for ts_ms, mid in self._prices if ts_ms >= since_ms]
        if len(points) <= max_points:
            return points
        stride = max(1, -(-len(points) // max_points))
        return points[::stride]

    def price_series(self, since_ms: int, max_points: int = MAX_SERIES_POINTS) -> list[list[float | int]]:
        with self._lock:
            return self._price_series(since_ms, max_points)

    def view(self, *, day: str, since_ms: int, max_points: int = MAX_SERIES_POINTS) -> dict[str, Any]:
        with self._lock:
            return {
                "snapshot": dict(self.snapshot) if self.snapshot is not None else None,
                "last_id": self.last_id,
                "last_ts_ms": self.last_ts_ms,
                "loading": self.loading,
                "lifetime_costs": dict(self.lifetime_costs),
                "day_costs": dict(self._day_costs.get(day, {"jev": 0.0, "llm": 0.0})),
                "price_series": self._price_series(since_ms, max_points),
                "jev": {"ok": self.jev_failures == 0, "falhas_seguidas": self.jev_failures,
                        "desde_ms": self.jev_failure_since_ms, "motivo": self.jev_failure_reason},
                "jev_gap_ms": (statistics.median(self._jev_gaps) if len(self._jev_gaps) >= 5 else None),
            }
