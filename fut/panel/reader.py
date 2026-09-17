"""Read-only window onto the futures paper database for the local panel.

Every call opens its own ``mode=ro`` connection and closes it. The panel's state path uses
bounded incremental reads by decision id, with one refresher shared by all browser tabs;
those short reads can still contend briefly with a writer and are reported explicitly.
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


class DecisionFacts(list):
    """A bounded fact page with the table high-water mark from the same read."""

    def __init__(self, rows: list[dict[str, Any]], max_id: int):
        super().__init__(rows)
        self.max_id = max_id


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
            rows = self._query(f"SELECT id, ts_ms, kind, payload FROM fut_decisions "
                               f"WHERE id > ? AND id <= ? AND {_EVENT_FILTER} "
                               "ORDER BY id DESC LIMIT ?", (max(0, upto_id - 20_000), upto_id, limit))[::-1]
        else:
            rows = self._query(f"SELECT id, ts_ms, kind, payload FROM fut_decisions WHERE id > ? AND id <= ? "
                               f"AND {_EVENT_FILTER} ORDER BY id LIMIT ?", (after_id, upto_id, limit))
        return [{"id": int(r[0]), "ts_ms": int(r[1]), "kind": r[2], "payload": _loads(r[3])} for r in rows]

    def decision_facts(self, after_id: int, limit: int = 2000) -> DecisionFacts:
        """Read the next bounded decision page and its max id in one short query."""
        sql = """
            WITH facts AS (
                SELECT id, ts_ms, kind,
                       CASE WHEN json_valid(payload)
                            THEN CAST(COALESCE(json_extract(payload, '$.cost_usd'), 0) AS REAL)
                            ELSE 0.0 END AS cost_usd,
                       CASE WHEN json_valid(payload) THEN json_extract(payload, '$.snapshot.bid') END AS bid,
                       CASE WHEN json_valid(payload) THEN json_extract(payload, '$.snapshot.ask') END AS ask,
                       CASE WHEN json_valid(payload) THEN json_extract(payload, '$.snapshot.last') END AS last
                FROM fut_decisions
                WHERE id > ?
                ORDER BY id
                LIMIT ?
            ), max_row AS (
                SELECT COALESCE(MAX(id), 0) AS max_id FROM fut_decisions
            )
            SELECT f.id, f.ts_ms, f.kind, f.cost_usd, f.bid, f.ask, f.last, m.max_id, 0 AS sentinel
            FROM facts AS f CROSS JOIN max_row AS m
            UNION ALL
            SELECT NULL, NULL, NULL, NULL, NULL, NULL, NULL, m.max_id, 1 AS sentinel
            FROM max_row AS m
            WHERE NOT EXISTS (SELECT 1 FROM facts)
        """
        rows = self._query(sql, (after_id, limit))
        max_id = int(rows[0][7] or 0) if rows else 0
        facts = [
            {"id": int(row[0]), "ts_ms": int(row[1]), "kind": row[2], "cost_usd": row[3],
             "bid": row[4], "ask": row[5], "last": row[6]}
            for row in rows if not row[8]
        ]
        return DecisionFacts(facts, max_id)

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
