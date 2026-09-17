"""SQLite state for futures paper: per-book positions, fills and balances, plus a decision log.

Subclasses bot.store.Store to inherit its mode stamp (StoreIdentityMismatch), kv table and
durable LLM budget. The spot tables it also creates stay unused here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.store import Store
from fut.types import FutPosition

MODE = "futures-paper"
_BALANCE_KEY = "fut_balance:{book}"


def day_of(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def day_bounds_ms(day: str) -> tuple[int, int]:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    return start_ms, start_ms + 86_400_000


class FutStore(Store):
    def __init__(self, path: Path, *, mode: str = MODE):
        super().__init__(path, mode=mode)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS fut_position (
                book TEXT PRIMARY KEY, side TEXT NOT NULL, contracts INTEGER NOT NULL,
                entry REAL NOT NULL, stop REAL, liq REAL, margin REAL NOT NULL,
                leverage INTEGER NOT NULL, opened_ms INTEGER NOT NULL,
                funding_through_ms INTEGER NOT NULL)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS fut_fills (
                id INTEGER PRIMARY KEY, book TEXT NOT NULL, ts_ms INTEGER NOT NULL, day TEXT NOT NULL,
                kind TEXT NOT NULL, side TEXT, contracts INTEGER, price REAL,
                fee REAL NOT NULL DEFAULT 0, funding REAL NOT NULL DEFAULT 0,
                pnl REAL NOT NULL DEFAULT 0, reason TEXT)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS fut_decisions (
                id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)"""
        )
        self.commit()

    # -- positions ---------------------------------------------------------------

    def load_fut_position(self, book: str) -> FutPosition:
        row = self._conn.execute(
            "SELECT side, contracts, entry, stop, liq, margin, leverage, opened_ms, funding_through_ms "
            "FROM fut_position WHERE book=?", (book,)).fetchone()
        if row is None:
            return FutPosition()
        return FutPosition(side=row[0], contracts=int(row[1]), entry=float(row[2]), stop=row[3], liq=row[4],
                           margin=float(row[5]), leverage=int(row[6]), opened_ms=int(row[7]),
                           funding_through_ms=int(row[8]))

    def save_fut_position(self, book: str, position: FutPosition, *, commit: bool = True) -> None:
        if not position.is_open():
            self._conn.execute("DELETE FROM fut_position WHERE book=?", (book,))
        else:
            self._conn.execute(
                """INSERT INTO fut_position(book, side, contracts, entry, stop, liq, margin, leverage,
                       opened_ms, funding_through_ms) VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(book) DO UPDATE SET side=excluded.side, contracts=excluded.contracts,
                       entry=excluded.entry, stop=excluded.stop, liq=excluded.liq, margin=excluded.margin,
                       leverage=excluded.leverage, opened_ms=excluded.opened_ms,
                       funding_through_ms=excluded.funding_through_ms""",
                (book, position.side, position.contracts, position.entry, position.stop, position.liq,
                 position.margin, position.leverage, position.opened_ms, position.funding_through_ms))
        if commit:
            self.commit()

    # -- balances ----------------------------------------------------------------

    def balance(self, book: str, starting: float) -> float:
        raw = self.kv_get(_BALANCE_KEY.format(book=book))
        return float(raw) if raw is not None else float(starting)

    def set_balance(self, book: str, value: float, *, commit: bool = True) -> None:
        self.kv_set(_BALANCE_KEY.format(book=book), repr(float(value)), commit=commit)

    # -- fills -------------------------------------------------------------------

    def add_fut_fill(self, book: str, *, ts_ms: int, kind: str, side: str | None, contracts: int,
                     price: float, fee: float, funding: float, pnl: float, reason: str,
                     commit: bool = True) -> None:
        self._conn.execute(
            "INSERT INTO fut_fills(book, ts_ms, day, kind, side, contracts, price, fee, funding, pnl, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (book, ts_ms, day_of(ts_ms), kind, side, contracts, price, fee, funding, pnl, reason))
        if commit:
            self.commit()

    def fut_fills(self, book: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, ts_ms, day, kind, side, contracts, price, fee, funding, pnl, reason "
            "FROM fut_fills WHERE book=? ORDER BY id", (book,)).fetchall()
        keys = ("id", "ts_ms", "day", "kind", "side", "contracts", "price", "fee", "funding", "pnl", "reason")
        return [dict(zip(keys, row)) for row in rows]

    def count_opens(self, book: str, since_ms: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM fut_fills WHERE book=? AND kind='open' AND ts_ms >= ?",
            (book, since_ms),
        ).fetchone()
        return int(row[0])

    def count_real_jev(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM fut_decisions "
            "WHERE kind='jev' AND json_extract(payload, '$.model') != 'mock'"
        ).fetchone()
        return int(row[0])

    def day_net(self, book: str, day: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl),0) - COALESCE(SUM(fee),0) - COALESCE(SUM(funding),0) "
            "FROM fut_fills WHERE book=? AND day=?", (book, day)).fetchone()
        return float(row[0])

    # -- decisions ---------------------------------------------------------------

    def log_decision(self, kind: str, payload: dict[str, Any], *, ts_ms: int) -> None:
        self._conn.execute("INSERT INTO fut_decisions(ts_ms, kind, payload) VALUES (?,?,?)",
                           (ts_ms, kind, json.dumps(payload, default=str)))
        self.commit()

    def decisions(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is None:
            rows = self._conn.execute("SELECT id, ts_ms, kind, payload FROM fut_decisions ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, ts_ms, kind, payload FROM fut_decisions WHERE kind=? ORDER BY id", (kind,)).fetchall()
        return [{"id": r[0], "ts_ms": r[1], "kind": r[2], "payload": json.loads(r[3])} for r in rows]

    def model_cost_between(self, start_ms: int, end_ms: int) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(json_extract(payload, '$.cost_usd')), 0) FROM fut_decisions "
            "WHERE kind IN ('jev', 'jev_ab', 'llm') AND ts_ms >= ? AND ts_ms < ?", (start_ms, end_ms)).fetchone()
        return float(row[0] or 0.0)
