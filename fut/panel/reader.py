"""Read-only window onto the futures paper database for the local panel.

Every call opens its own ``mode=ro`` connection and closes it: the panel must never hold a
read lock while the bot wants to commit, and must never run FutStore's schema/stamp writes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

# Jev rows that woke nothing are ~1,500/hour and only feed the price; they are not events.
_EVENT_FILTER = ("(kind != 'jev' OR json_extract(payload, '$.wake') IS NOT NULL "
                 "OR json_extract(payload, '$.error') IS NOT NULL "
                 "OR json_extract(payload, '$.gate') IS NOT NULL)")
_FILL_KEYS = ("id", "ts_ms", "day", "kind", "side", "contracts", "price", "fee", "funding", "pnl", "reason")
_POSITION_KEYS = ("side", "contracts", "entry", "stop", "liq", "margin", "leverage", "opened_ms")
_BALANCE_PREFIX = "fut_balance:"


class PanelDbMissing(RuntimeError):
    """The futures paper database does not exist yet."""


class PanelDbBusy(RuntimeError):
    """The database is locked by a writer right now; try again."""


def _loads(text: Any) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class PanelReader:
    def __init__(self, path: Path, *, timeout_s: float = 0.5):
        self.path = Path(path)
        self.timeout_s = timeout_s

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        if not self.path.exists():
            raise PanelDbMissing(str(self.path))
        uri = f"file:{quote(str(self.path.resolve()))}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=self.timeout_s)
            try:
                return conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise PanelDbBusy(str(exc)) from exc

    def last_decision(self) -> tuple[int, int]:
        rows = self._query("SELECT id, ts_ms FROM fut_decisions ORDER BY id DESC LIMIT 1")
        return (int(rows[0][0]), int(rows[0][1])) if rows else (0, 0)

    def event_rows(self, after_id: int | None, upto_id: int, limit: int = 500) -> list[dict[str, Any]]:
        if after_id is None:
            rows = self._query(f"SELECT id, ts_ms, kind, payload FROM fut_decisions WHERE id <= ? AND {_EVENT_FILTER} "
                               "ORDER BY id DESC LIMIT ?", (upto_id, limit))[::-1]
        else:
            rows = self._query(f"SELECT id, ts_ms, kind, payload FROM fut_decisions WHERE id > ? AND id <= ? "
                               f"AND {_EVENT_FILTER} ORDER BY id LIMIT ?", (after_id, upto_id, limit))
        return [{"id": int(r[0]), "ts_ms": int(r[1]), "kind": r[2], "payload": _loads(r[3])} for r in rows]

    def latest_snapshot(self) -> dict[str, Any] | None:
        rows = self._query("SELECT json_extract(payload, '$.snapshot') FROM fut_decisions "
                           "WHERE kind='jev' ORDER BY id DESC LIMIT 1")
        snap = _loads(rows[0][0]) if rows else {}
        return snap or None

    def price_series(self, since_ms: int, max_points: int = 600) -> list[list]:
        rows = self._query("SELECT ts_ms, json_extract(payload, '$.snapshot.bid'), "
                           "json_extract(payload, '$.snapshot.ask'), json_extract(payload, '$.snapshot.last') "
                           "FROM fut_decisions WHERE kind='jev' AND ts_ms >= ? ORDER BY id", (since_ms,))
        points = []
        for ts, bid, ask, last in rows:
            bid, ask, last = float(bid or 0), float(ask or 0), float(last or 0)
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
            if mid > 0:
                points.append([int(ts), mid])
        stride = max(1, -(-len(points) // max_points))
        return points[::stride]

    def position(self, book: str) -> dict[str, Any] | None:
        rows = self._query(f"SELECT {', '.join(_POSITION_KEYS)} FROM fut_position WHERE book=?", (book,))
        return dict(zip(_POSITION_KEYS, rows[0])) if rows else None

    def balances(self) -> dict[str, float]:
        rows = self._query("SELECT key, value FROM kv WHERE key LIKE ?", (_BALANCE_PREFIX + "%",))
        out = {}
        for key, value in rows:
            try:
                out[key[len(_BALANCE_PREFIX):]] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    def fills(self, book: str, *, since_ms: int | None = None, day: str | None = None) -> list[dict[str, Any]]:
        sql, params = f"SELECT {', '.join(_FILL_KEYS)} FROM fut_fills WHERE book=?", [book]
        if since_ms is not None:
            sql, params = sql + " AND ts_ms >= ?", params + [since_ms]
        if day is not None:
            sql, params = sql + " AND day = ?", params + [day]
        return [dict(zip(_FILL_KEYS, row)) for row in self._query(sql + " ORDER BY id", tuple(params))]

    def model_costs(self, start_ms: int, end_ms: int) -> dict[str, float]:
        rows = self._query("SELECT kind, COALESCE(SUM(json_extract(payload, '$.cost_usd')), 0) FROM fut_decisions "
                           "WHERE kind IN ('jev', 'llm') AND ts_ms >= ? AND ts_ms < ? GROUP BY kind", (start_ms, end_ms))
        out = {"jev": 0.0, "llm": 0.0}
        out.update({kind: float(total or 0.0) for kind, total in rows})
        return out
