"""Synthetic futures-paper database for the panel tests. Plain sqlite3: no FutStore, no mode stamp."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def make_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE fut_decisions (id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, "
                 "kind TEXT NOT NULL, payload TEXT NOT NULL)")
    conn.execute("CREATE TABLE fut_fills (id INTEGER PRIMARY KEY, book TEXT NOT NULL, ts_ms INTEGER NOT NULL, "
                 "day TEXT NOT NULL, kind TEXT NOT NULL, side TEXT, contracts INTEGER, price REAL, "
                 "fee REAL NOT NULL DEFAULT 0, funding REAL NOT NULL DEFAULT 0, pnl REAL NOT NULL DEFAULT 0, reason TEXT)")
    conn.execute("CREATE TABLE fut_position (book TEXT PRIMARY KEY, side TEXT NOT NULL, contracts INTEGER NOT NULL, "
                 "entry REAL NOT NULL, stop REAL, liq REAL, margin REAL NOT NULL, leverage INTEGER NOT NULL, "
                 "opened_ms INTEGER NOT NULL, funding_through_ms INTEGER NOT NULL)")
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def SNAP(**overrides) -> dict:
    base = {"ts_ms": 0, "last": 76000.0, "bid": 76000.0, "ask": 76000.2, "spread_bps": 0.026, "stale": False}
    base.update(overrides)
    return base


def add_decision(conn, ts_ms: int, kind: str, payload: dict) -> int:
    cur = conn.execute("INSERT INTO fut_decisions(ts_ms, kind, payload) VALUES (?,?,?)",
                       (ts_ms, kind, json.dumps(payload)))
    conn.commit()
    return int(cur.lastrowid)


def add_fill(conn, book: str, ts_ms: int, kind: str, *, side="long", contracts=2, price=76000.0,
             fee=0.0, funding=0.0, pnl=0.0, reason="entry") -> None:
    day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()
    conn.execute("INSERT INTO fut_fills(book, ts_ms, day, kind, side, contracts, price, fee, funding, pnl, reason) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (book, ts_ms, day, kind, side, contracts, price, fee, funding, pnl, reason))
    conn.commit()


def set_position(conn, book: str, *, side="long", contracts=2, entry=76000.0, stop=75900.0, liq=380.0,
                 margin=15.2, leverage=1, opened_ms=0) -> None:
    conn.execute("INSERT OR REPLACE INTO fut_position VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (book, side, contracts, entry, stop, liq, margin, leverage, opened_ms, opened_ms))
    conn.commit()


def set_balance(conn, book: str, value: float) -> None:
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (f"fut_balance:{book}", repr(float(value))))
    conn.commit()
