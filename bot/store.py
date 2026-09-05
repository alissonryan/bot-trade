"""SQLite store: audit rows, fills, bot order ids, the single bot position, and a small
key/value table.

The schema is created with CREATE TABLE IF NOT EXISTS and then migrated forward with
ALTER TABLE ADD COLUMN, so a data/bot.db written by an earlier version keeps its rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.types import GateResult, TradeIntent

POSITION_STATES = ("PENDING", "OPEN", "UNPROTECTED", "CLOSING")

_FILL_COLUMNS = (
    ("ts", "TEXT"),
    ("side", "TEXT"),
    ("qty", "REAL"),
    ("price", "REAL"),
    ("fee", "REAL"),
    ("order_id", "TEXT"),
    ("source", "TEXT"),
)
_POSITION_COLUMNS = (
    ("state", "TEXT"),
    ("entry_source", "TEXT"),
    ("btc_before", "REAL"),
    ("opened_ts", "TEXT"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY,
                ts TEXT,
                action TEXT,
                ok INTEGER,
                rule TEXT,
                payload TEXT
            )"""
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS bot_orders (order_id TEXT PRIMARY KEY)"
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY,
                day TEXT,
                pnl REAL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS position (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                qty REAL,
                entry REAL,
                stop_price REAL,
                entry_order_id TEXT,
                stop_order_id TEXT
            )"""
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)"
        )
        self._migrate()
        self._conn.commit()

    # -- schema -----------------------------------------------------------------

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        have = self._columns("fills")
        for name, ddl in _FILL_COLUMNS:
            if name not in have:
                self._conn.execute(f"ALTER TABLE fills ADD COLUMN {name} {ddl}")
        have = self._columns("position")
        for name, ddl in _POSITION_COLUMNS:
            if name not in have:
                self._conn.execute(f"ALTER TABLE position ADD COLUMN {name} {ddl}")

    # -- audit ------------------------------------------------------------------

    def append_audit(
        self,
        intent: TradeIntent,
        gate: GateResult,
        mode: str,
        order_id: str | None = None,
        *,
        snapshot: dict[str, Any] | None = None,
        llm: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """One row per decision. ``snapshot`` (last/bid/ask/atr/ts) is what makes a decision
        re-readable later; without it the audit cannot say whether the model was right."""
        ts = _now_iso()
        payload: dict[str, Any] = {
            "mode": mode,
            "intent": intent.__dict__,
            "gate": gate.__dict__,
            "order_id": order_id,
        }
        if snapshot is not None:
            payload["snapshot"] = snapshot
        if llm is not None:
            payload["llm"] = llm
        if extra:
            payload.update(extra)
        self._conn.execute(
            "INSERT INTO audit(ts, action, ok, rule, payload) VALUES (?,?,?,?,?)",
            (ts, intent.action, int(gate.ok), gate.rule, json.dumps(payload)),
        )
        self._conn.commit()

    def recent_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, ts, action, ok, rule, payload FROM audit ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row[5]) if row[5] else {}
            except json.JSONDecodeError:
                payload = {}
            out.append({"id": row[0], "ts": row[1], "action": row[2], "ok": bool(row[3]), "rule": row[4], "payload": payload})
        return out

    # -- bot order ids ----------------------------------------------------------

    def remember_order(self, order_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO bot_orders(order_id) VALUES (?)", (order_id,)
        )
        self._conn.commit()

    def is_bot_order(self, order_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM bot_orders WHERE order_id=?", (order_id,)
        ).fetchone()
        return row is not None

    # -- fills ------------------------------------------------------------------

    def add_fill(
        self,
        day: str,
        pnl: float,
        *,
        ts: str | None = None,
        side: str | None = None,
        qty: float | None = None,
        price: float | None = None,
        fee: float | None = None,
        order_id: str | None = None,
        source: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO fills(day, pnl, ts, side, qty, price, fee, order_id, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (day, float(pnl), ts or _now_iso(), side, qty, price, fee, order_id, source),
        )
        self._conn.commit()

    def day_pnl(self, day: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM fills WHERE day=?", (day,)
        ).fetchone()
        return float(row[0])

    def fills(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, day, pnl, ts, side, qty, price, fee, order_id, source FROM fills ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        keys = ("id", "day", "pnl", "ts", "side", "qty", "price", "fee", "order_id", "source")
        return [dict(zip(keys, row)) for row in rows]

    # -- position ---------------------------------------------------------------

    def save_position(
        self,
        *,
        qty: float,
        entry: float,
        stop_price: float | None,
        entry_order_id: str | None,
        stop_order_id: str | None,
        state: str = "OPEN",
        entry_source: str | None = None,
        btc_before: float | None = None,
        opened_ts: str | None = None,
    ) -> None:
        if state not in POSITION_STATES:
            raise ValueError(f"unknown position state {state!r}")
        self._conn.execute("DELETE FROM position")
        if qty > 0:
            self._conn.execute(
                """INSERT INTO position(id, qty, entry, stop_price, entry_order_id, stop_order_id,
                                        state, entry_source, btc_before, opened_ts)
                   VALUES (1,?,?,?,?,?,?,?,?,?)""",
                (qty, entry, stop_price, entry_order_id, stop_order_id, state, entry_source, btc_before,
                 opened_ts or _now_iso()),
            )
        self._conn.commit()

    def load_position(self) -> dict | None:
        row = self._conn.execute(
            """SELECT qty, entry, stop_price, entry_order_id, stop_order_id, state, entry_source, btc_before, opened_ts
               FROM position WHERE id=1"""
        ).fetchone()
        if not row:
            return None
        return {
            "qty": float(row[0]),
            "entry": float(row[1]),
            "stop_price": float(row[2]) if row[2] is not None else None,
            "entry_order_id": row[3],
            "stop_order_id": row[4],
            # Rows written before the state column existed were always a protected long.
            "state": row[5] or "OPEN",
            "entry_source": row[6],
            "btc_before": float(row[7]) if row[7] is not None else None,
            "opened_ts": row[8],
        }

    def set_position_state(self, state: str) -> None:
        if state not in POSITION_STATES:
            raise ValueError(f"unknown position state {state!r}")
        self._conn.execute("UPDATE position SET state=? WHERE id=1", (state,))
        self._conn.commit()

    def clear_position(self) -> None:
        self._conn.execute("DELETE FROM position")
        self._conn.commit()

    # -- key/value --------------------------------------------------------------

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self._conn.commit()
