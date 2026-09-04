from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bot.types import GateResult, TradeIntent


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
        self._conn.commit()

    def append_audit(
        self,
        intent: TradeIntent,
        gate: GateResult,
        mode: str,
        order_id: str | None = None,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(
            {
                "mode": mode,
                "intent": intent.__dict__,
                "gate": gate.__dict__,
                "order_id": order_id,
            }
        )
        self._conn.execute(
            "INSERT INTO audit(ts, action, ok, rule, payload) VALUES (?,?,?,?,?)",
            (ts, intent.action, int(gate.ok), gate.rule, payload),
        )
        self._conn.commit()

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

    def add_fill(self, day: str, pnl: float) -> None:
        self._conn.execute("INSERT INTO fills(day, pnl) VALUES (?,?)", (day, pnl))
        self._conn.commit()

    def day_pnl(self, day: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM fills WHERE day=?", (day,)
        ).fetchone()
        return float(row[0])

    def save_position(
        self,
        *,
        qty: float,
        entry: float,
        stop_price: float | None,
        entry_order_id: str | None,
        stop_order_id: str | None,
    ) -> None:
        self._conn.execute("DELETE FROM position")
        if qty > 0:
            self._conn.execute(
                """INSERT INTO position(id, qty, entry, stop_price, entry_order_id, stop_order_id)
                   VALUES (1,?,?,?,?,?)""",
                (qty, entry, stop_price, entry_order_id, stop_order_id),
            )
        self._conn.commit()

    def load_position(self) -> dict | None:
        row = self._conn.execute(
            "SELECT qty, entry, stop_price, entry_order_id, stop_order_id FROM position WHERE id=1"
        ).fetchone()
        if not row:
            return None
        return {
            "qty": float(row[0]),
            "entry": float(row[1]),
            "stop_price": float(row[2]) if row[2] is not None else None,
            "entry_order_id": row[3],
            "stop_order_id": row[4],
        }

    def clear_position(self) -> None:
        self._conn.execute("DELETE FROM position")
        self._conn.commit()
