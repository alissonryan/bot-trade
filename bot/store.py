"""SQLite store: audit rows, fills, bot order ids, the single bot position, and a small
key/value table.

The schema is created with CREATE TABLE IF NOT EXISTS and then migrated forward with
ALTER TABLE ADD COLUMN, so a data/bot.db written by an earlier version keeps its rows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.types import GateResult, TradeIntent

POSITION_STATES = ("PENDING", "OPEN", "UNPROTECTED", "CLOSING")
MODE_KEY = "store_mode"


class StoreIdentityMismatch(RuntimeError):
    """The database was not written by the mode now trying to open it.

    Adopting paper state in live sizes a real order from a position the bot
    never bought. Refusing costs one manual inspection, so this always fails
    closed and never migrates a database from one mode to the other.
    """
JOURNAL_ENTRY_KEY = "journal_active_entry"
log = logging.getLogger(__name__)
_JOURNAL_COLUMNS = (
    ("decision_ms", "INTEGER"), ("action", "TEXT"), ("confidence", "REAL"),
    ("regime", "TEXT"), ("reason", "TEXT"), ("snapshot", "TEXT"),
    ("status", "TEXT"), ("outcome", "TEXT"), ("outcome_known_ms", "INTEGER"),
    ("reflection", "TEXT"), ("reflection_known_ms", "INTEGER"),
    ("reflection_audit", "TEXT"),
)

_FILL_COLUMNS = (
    ("ts", "TEXT"),
    ("side", "TEXT"),
    ("qty", "REAL"),
    ("price", "REAL"),
    ("fee", "REAL"),
    ("order_id", "TEXT"),
    ("source", "TEXT"),
    ("journal_id", "INTEGER"),
)
_POSITION_COLUMNS = (
    ("state", "TEXT"),
    ("entry_source", "TEXT"),
    ("btc_before", "REAL"),
    ("opened_ts", "TEXT"),
    ("take_profit_price", "REAL"),
    ("exit_reason", "TEXT"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_ms(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp() * 1000)


class Store:
    def __init__(self, path: Path, *, mode: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        # Identity is settled BEFORE any schema work, reading only. A refused
        # open must leave the file exactly as it was found -- even creating the
        # kv table to look for the stamp is a write on a legacy database that
        # never had one.
        self.mode = mode
        if mode is not None:
            self._preflight_mode(mode)
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
        self._conn.execute("CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY)")
        self._migrate()
        self._conn.commit()
        if mode is None:
            self.mode = self.kv_get(MODE_KEY)
        else:
            # The schema exists now, so the stamp can finally be written.
            self.kv_set(MODE_KEY, mode)

    def _preflight_mode(self, mode: str) -> None:
        """Refuse another mode's database without writing a single byte.

        An unstamped file predates this check, so nothing in it proves who
        wrote it. Paper may adopt one -- being wrong there costs a simulated
        number. Live may not: being wrong there sends a real order sized from
        a position that may never have existed.
        """
        tables = {
            row[0] for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        stored = None
        if "kv" in tables:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key=?", (MODE_KEY,)).fetchone()
            stored = row[0] if row else None
        if stored == mode:
            return
        if stored is not None:
            raise StoreIdentityMismatch(
                f"{self.path} was written in {stored!r} mode and cannot be opened as "
                f"{mode!r}. Point this mode at its own database instead of migrating "
                "state between them."
            )
        if mode != "paper" and self._has_history(tables):
            raise StoreIdentityMismatch(
                f"{self.path} is unstamped and already carries rows, so nothing proves "
                f"it is not paper state; refusing to adopt it as {mode!r}. Start "
                f"{mode!r} on an empty database, or inspect and stamp this one by hand."
            )

    def _has_history(self, tables: set[str]) -> bool:
        """Any trace at all, including kv and the journal.

        A paper database can carry nothing but `paper_cash` and journal rows;
        skipping those tables let exactly that file be adopted as live. Only
        tables that already exist are read, so looking costs no write.
        """
        for table in ("position", "fills", "bot_orders", "audit", "journal", "kv"):
            if table not in tables:
                continue
            if self._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return True
        return False

    def close(self) -> None:
        self._conn.close()

    # -- schema -----------------------------------------------------------------

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        have = self._columns("journal")
        for name, ddl in _JOURNAL_COLUMNS:
            if name not in have:
                self._conn.execute(f"ALTER TABLE journal ADD COLUMN {name} {ddl}")
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
        known_ms: int | None = None,
    ) -> None:
        journal_id = self.kv_get(JOURNAL_ENTRY_KEY)
        self._conn.execute(
            "INSERT INTO fills(day, pnl, ts, side, qty, price, fee, order_id, source, journal_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (day, float(pnl), ts or _now_iso(), side, qty, price, fee, order_id, source, journal_id),
        )
        if journal_id and side == "SELL":
            self._conn.execute("SAVEPOINT journal_settle")
            try:
                self._journal_close(int(journal_id), known_ms if known_ms is not None else int(time.time() * 1000), source)
            except Exception:
                self._conn.execute("ROLLBACK TO journal_settle")
                log.exception("journal_resolution_error; preserving the real fill")
            finally:
                self._conn.execute("RELEASE journal_settle")
        self._conn.commit()

    # -- decision journal -------------------------------------------------------

    def journal_begin(self, intent: TradeIntent, snapshot: dict, *, decision_ms: int,
                      track_entry: bool = False) -> int:
        """Persist before execution; only an approved flat BUY owns future fills.

        Superseded ambiguous entries remain pending, never inherit another trade.
        """
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO journal(decision_ms,action,confidence,regime,reason,snapshot,status) VALUES (?,?,?,?,?,?,?)",
                (decision_ms, intent.action, intent.confidence, intent.regime,
                 intent.reason, json.dumps(snapshot), "pending"),
            )
            assert cursor.lastrowid is not None
            jid = cursor.lastrowid
            if track_entry:
                if intent.action != "BUY":
                    raise ValueError("only BUY can own position fills")
                self._conn.execute(
                    "INSERT INTO kv(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (JOURNAL_ENTRY_KEY, str(jid)),
                )
        return jid

    def journal_resolve(self, jid: int, outcome: dict, *, known_ms: int) -> None:
        self._conn.execute(
            "UPDATE journal SET status='resolved',outcome=?,outcome_known_ms=? WHERE id=? AND status='pending' AND decision_ms<=?",
            (json.dumps(outcome), known_ms, jid, known_ms),
        )
        self._conn.commit()

    def journal_detach(self) -> None:
        """A new entry cannot inherit a previous ambiguous decision's fills."""
        self._conn.execute("DELETE FROM kv WHERE key=?", (JOURNAL_ENTRY_KEY,))
        self._conn.commit()

    def journal_unfilled(self, jid: int, *, known_ms: int) -> None:
        """Caller has a successful execution return and is flat, not an exception."""
        if self._conn.execute("SELECT 1 FROM fills WHERE journal_id=? LIMIT 1", (jid,)).fetchone():
            return
        self.journal_resolve(jid, {"kind": "not_executed", "realized_pnl_usdt": None,
                                  "reason": "execution_returned_flat_without_fill"}, known_ms=known_ms)
        self._conn.execute("DELETE FROM kv WHERE key=? AND value=?", (JOURNAL_ENTRY_KEY, str(jid)))
        self._conn.commit()

    def _journal_close(self, jid: int, known_ms: int, source: str | None) -> None:
        # No synthetic BUY for a PENDING entry whose fill was never recorded.
        buys = self._conn.execute(
            "SELECT qty,price,fee,ts FROM fills WHERE journal_id=? AND side='BUY'", (jid,)
        ).fetchall()
        sells = self._conn.execute(
            "SELECT qty,pnl,ts FROM fills WHERE journal_id=? AND side='SELL'", (jid,)
        ).fetchall()
        bought = sum(row[0] or 0 for row in buys)
        sold = sum(row[0] or 0 for row in sells)
        if bought <= 0 or sold < bought - 1e-12:
            return
        pnl = sum(row[1] for row in sells)
        capital = sum((row[0] or 0) * (row[1] or 0) + (row[2] or 0) for row in buys)
        duration = (_iso_ms(sells[-1][2]) - _iso_ms(buys[0][3])) / 1000
        outcome = {"kind": "closed", "realized_pnl_usdt": pnl,
                   "return_pct": 100 * pnl / capital if capital > 0 else None,
                   "duration_seconds": max(0, duration), "exit_type": source or "unknown",
                   "provenance": "bot_fill_ledger; live prices may be estimated"}
        self._conn.execute(
            "UPDATE journal SET status='resolved',outcome=?,outcome_known_ms=? WHERE id=? AND status='pending' AND decision_ms<=?",
            (json.dumps(outcome), known_ms, jid, known_ms),
        )
        self._conn.execute("DELETE FROM kv WHERE key=? AND value=?", (JOURNAL_ENTRY_KEY, str(jid)))

    def journal_lessons(self, *, as_of_ms: int, limit: int = 5) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id,decision_ms,action,confidence,regime,reason,outcome,outcome_known_ms,reflection,reflection_known_ms FROM journal "
            "WHERE status='resolved' AND json_extract(outcome,'$.kind')='closed' AND decision_ms<=? AND outcome_known_ms<=? "
            "ORDER BY outcome_known_ms DESC,id DESC LIMIT ?",
            (as_of_ms, as_of_ms, max(0, min(5, limit))),
        ).fetchall()
        keys = ("id", "decision_ms", "action", "confidence", "regime", "reason", "outcome", "outcome_known_ms")
        lessons = []
        for row in rows:
            lesson = dict(zip(keys, row), outcome=json.loads(row[6]))
            if row[8] and row[9] is not None and row[9] <= as_of_ms:
                lesson.update(reflection=row[8][:400], reflection_known_ms=row[9])
            lessons.append(lesson)
        return lessons

    def journal_get(self, jid: int) -> dict | None:
        cursor = self._conn.execute("SELECT * FROM journal WHERE id=?", (jid,))
        row = cursor.fetchone()
        if row is None:
            return None
        result = dict(zip((column[0] for column in cursor.description), row))
        for key in ("snapshot", "outcome", "reflection_audit"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    def journal_candidate(self, *, as_of_ms: int) -> dict | None:
        row = self._conn.execute(
            "SELECT id FROM journal WHERE status='resolved' AND action='BUY' "
            "AND json_extract(outcome,'$.kind')='closed' AND reflection_audit IS NULL "
            "AND decision_ms<=? AND outcome_known_ms<=? ORDER BY outcome_known_ms DESC,id DESC LIMIT 1",
            (as_of_ms, as_of_ms),
        ).fetchone()
        return self.journal_get(row[0]) if row else None

    def journal_reflect(self, jid: int, text: str | None, audit: dict, *, known_ms: int) -> None:
        # Immutable first attempt: generated-later text cannot rewrite history.
        self._conn.execute(
            "UPDATE journal SET reflection=?,reflection_known_ms=?,reflection_audit=? "
            "WHERE id=? AND status='resolved' AND outcome_known_ms<=? AND reflection_audit IS NULL",
            (text[:400] if text else None, known_ms, json.dumps(audit), jid, known_ms),
        )
        self._conn.commit()

    def last_exit_ms(self) -> int | None:
        """Last recorded bot SELL, across days/restarts (never account activity).

        Reconciled exits are dated at observation, conservatively delaying entry.
        Legacy rows without a SELL timestamp cannot establish a cooldown age.
        """
        return self._last_exit("SELECT ts FROM fills WHERE side='SELL' ORDER BY id DESC LIMIT 1")

    def last_loss_exit_ms(self) -> int | None:
        """Last *losing* bot SELL — the only exit that arms the cooldown.

        A profitable exit does not start the clock: re-entering the same
        direction while the move continues is riding it, not revenge, and every
        entry still needs a fresh signal. Breakeven (``pnl >= 0``) is not a loss.
        A later win never resets an armed clock, because the loss still happened.
        """
        return self._last_exit(
            "SELECT ts FROM fills WHERE side='SELL' AND pnl < 0 ORDER BY id DESC LIMIT 1"
        )

    def _last_exit(self, sql: str) -> int | None:
        row = self._conn.execute(sql).fetchone()
        if not row or not row[0]:
            return None
        dt = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

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
        take_profit_price: float | None = None,
        exit_reason: str | None = None,
    ) -> None:
        if state not in POSITION_STATES:
            raise ValueError(f"unknown position state {state!r}")
        old = self.load_position()
        opened_ts = (old["opened_ts"] if old else None) or opened_ts or _now_iso()
        self._conn.execute("DELETE FROM position")
        if qty > 0:
            self._conn.execute(
                """INSERT INTO position(id, qty, entry, stop_price, entry_order_id, stop_order_id,
                                        state, entry_source, btc_before, opened_ts, take_profit_price, exit_reason)
                   VALUES (1,?,?,?,?,?,?,?,?,?,?,?)""",
                (qty, entry, stop_price, entry_order_id, stop_order_id, state, entry_source, btc_before,
                 opened_ts, take_profit_price, exit_reason),
            )
        self._conn.commit()

    def load_position(self) -> dict | None:
        row = self._conn.execute(
            """SELECT qty, entry, stop_price, entry_order_id, stop_order_id, state, entry_source, btc_before, opened_ts, take_profit_price, exit_reason
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
            "take_profit_price": float(row[9]) if row[9] is not None else None,
            "exit_reason": row[10],
        }

    def set_position_state(self, state: str) -> None:
        if state not in POSITION_STATES:
            raise ValueError(f"unknown position state {state!r}")
        self._conn.execute("UPDATE position SET state=? WHERE id=1", (state,))
        self._conn.commit()

    def clear_position(self) -> None:
        self._conn.execute("DELETE FROM position")
        active = self.kv_get(JOURNAL_ENTRY_KEY)
        if active:
            self._conn.execute("SAVEPOINT journal_clear")
            try:
                have_fill = self._conn.execute("SELECT 1 FROM fills WHERE journal_id=? LIMIT 1", (active,)).fetchone()
                if not have_fill:
                    self._conn.execute(
                        "UPDATE journal SET status='resolved',outcome=?,outcome_known_ms=? WHERE id=? AND status='pending'",
                        (json.dumps({"kind": "not_executed", "realized_pnl_usdt": None,
                                     "reason": "entry_cleared_without_fill"}), int(time.time() * 1000), active),
                    )
                # Missing entry evidence stays pending rather than inventing returns.
                self._conn.execute("DELETE FROM kv WHERE key=?", (JOURNAL_ENTRY_KEY,))
            except Exception:
                self._conn.execute("ROLLBACK TO journal_clear")
                log.exception("journal_clear_error; preserving the position clear")
            finally:
                self._conn.execute("RELEASE journal_clear")
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
