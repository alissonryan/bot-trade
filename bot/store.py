"""SQLite store: audit rows, fills, bot order ids, the single bot position, and a small
key/value table.

The schema is created with CREATE TABLE IF NOT EXISTS and then migrated forward with
ALTER TABLE ADD COLUMN, so a data/bot.db written by an earlier version keeps its rows.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from bot.types import GateResult, TradeIntent

POSITION_STATES = ("PENDING", "OPEN", "UNPROTECTED", "CLOSING")
MODE_KEY = "store_mode"

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_day(value: Any) -> bool:
    """True only for an exact ``YYYY-MM-DD`` string that is a real calendar
    date. A bare string/lexicographic compare (``"0" < "2026-09-09"``)
    would otherwise treat any garbage value as "an earlier day", silently
    resetting a budget that was never actually rolled over."""
    if not isinstance(value, str) or not _DAY_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


class StoreIdentityMismatch(RuntimeError):
    """The database was not written by the mode now trying to open it.

    Adopting paper state in live sizes a real order from a position the bot
    never bought. Refusing costs one manual inspection, so this always fails
    closed and never migrates a database from one mode to the other.
    """
JOURNAL_ENTRY_KEY = "journal_active_entry"
BUDGET_KEY = "llm_budget"


class BudgetStateCorrupt(RuntimeError):
    """The persisted llm_budget kv row exists but cannot be trusted (bad
    JSON, missing/invalid fields, wrong types, non-finite/negative spend, a
    non-canonical day, or a stored day after "today" -- a backward clock or
    a future stored day). Callers MUST treat this as "block new paid calls",
    never as license to mint a fresh zero budget: silently resetting on
    corruption is exactly how a spend cap gets bypassed.
    """


class BudgetTransactionConflict(RuntimeError):
    """reserve_budget()/settle_budget() were called on a connection that
    already has an uncommitted transaction open. Proceeding would either
    write into the caller's pending transaction (invisible to any other
    reader/connection until the caller commits -- not atomic despite
    returning as if it were) or touch/roll back writes this call does not
    own. Refuses before reading or writing anything; the caller must commit
    or roll back its own pending writes first.
    """
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
        commit: bool = True,
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
        if commit:
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
        commit: bool = True,
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
        if commit:
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

    def clear_position(self, *, commit: bool = True) -> None:
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
        if commit:
            self._conn.commit()

    # -- key/value --------------------------------------------------------------

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str, *, commit: bool = True) -> None:
        self._conn.execute(
            "INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        if commit:
            self._conn.commit()


    # -- LLM daily budget ---------------------------------------------------
    #
    # A durable reservation, committed BEFORE every paid HTTP dispatch (see
    # bot.brain.think_result/reflect_result), not a snapshot written after
    # the fact -- a crash mid-request still leaves the reservation on disk.
    # Reuses the existing kv table (same mechanism PAPER_CASH_KEY/MODE_KEY
    # already use), not a second persistence convention. Paper and live
    # already get separate Store files (db_path_for_mode), so budgets are
    # separate per mode/database for free -- this is not an account-identity
    # or provider-wide cap, only a local per-database ledger.

    def _parse_budget_row(self, raw: str | None) -> dict[str, Any] | None:
        """None only when the kv row was never written (a genuinely fresh
        Store/new day) -- never for a row that exists but cannot be trusted.
        Raises BudgetStateCorrupt for anything present but unreadable:
        invalid JSON, a non-canonical ``day`` (must be exactly YYYY-MM-DD and
        a real calendar date -- a bare string compare like ``"0" < today``
        would otherwise treat garbage as "an earlier day, roll to zero"),
        a non-numeric/negative/non-finite ``spent_usd``, or a non-integer/
        negative ``calls``. Booleans are rejected even where Python would
        accept them as ints/floats (``True == 1``)."""
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BudgetStateCorrupt(f"llm_budget row is not valid JSON: {raw[:120]!r}") from exc
        if not isinstance(data, dict) or "day" not in data or "spent_usd" not in data:
            raise BudgetStateCorrupt(f"llm_budget row missing required fields: {data!r}")
        day = data["day"]
        if not _valid_day(day):
            raise BudgetStateCorrupt(f"llm_budget row has a non-canonical day: {day!r}")
        spent = data["spent_usd"]
        if isinstance(spent, bool) or not isinstance(spent, (int, float)):
            raise BudgetStateCorrupt(f"llm_budget row has a non-numeric spent_usd: {spent!r}")
        spent = float(spent)
        if not math.isfinite(spent) or spent < 0:
            raise BudgetStateCorrupt(f"llm_budget row has a non-finite/negative spent_usd: {spent!r}")
        calls = data.get("calls", 0)
        if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
            raise BudgetStateCorrupt(f"llm_budget row has an invalid calls count: {calls!r}")
        return {"day": day, "spent_usd": spent, "calls": calls}

    def budget_load(self) -> dict[str, Any] | None:
        """Read-only snapshot for startup logging/seeding the in-memory
        Budget fast-path -- NOT the admission gate. Raises BudgetStateCorrupt
        on an untrustworthy row; the caller must not treat that as license to
        start a fresh zero budget (see reserve_budget, the real gate)."""
        return self._parse_budget_row(self.kv_get(BUDGET_KEY))

    def reserve_budget(self, *, today: str, cap_usd: float, reserve_usd: float,
                       required_headroom: float = 0.0) -> bool:
        """Atomic admission check + durable commit for one paid HTTP dispatch.

        Reads the persisted row and writes the reservation back inside a
        single ``BEGIN IMMEDIATE`` transaction, so two Store connections
        against the same database file cannot both read the same "remaining"
        figure and both admit a request that together exceed the cap -- the
        second writer blocks/serializes behind the first instead of racing
        it. Returns True (and commits the reservation) when
        ``already_spent_today + reserve_usd + required_headroom <= cap_usd``;
        returns False (no write at all) when it does not fit. Only
        ``reserve_usd`` is actually committed to the persisted spend --
        ``required_headroom`` is checked atomically alongside it (for a
        caller that must also prove room for a SEPARATE follow-up spend,
        e.g. reflection reserving room for the next decision too) without
        being charged itself.

        Raises ``BudgetTransactionConflict`` immediately, before reading or
        writing anything, if this connection already has an uncommitted
        transaction open: writing into a caller's pending transaction would
        be invisible to any other reader until the caller commits, so
        returning True there would claim an atomicity this call cannot
        actually provide. Raises ``ValueError`` for a non-canonical
        ``today`` or a non-finite/negative ``cap_usd``/``reserve_usd``/
        ``required_headroom`` (a reservation of exactly 0 reserves nothing
        and must be refused explicitly, not silently admitted).

        A genuinely absent row (new database) or a persisted day strictly
        BEFORE ``today`` both mean zero already spent today -- ordinary UTC
        rollover, not a bug. A persisted day AFTER ``today`` (a backward
        system clock, or a corrupted/future stored day) is refused via
        BudgetStateCorrupt rather than silently minting a fresh budget --
        that would let a clock rollback bypass the cap entirely. The same
        exception propagates for any other unreadable/corrupt row (see
        _parse_budget_row); callers must block new paid calls on it, not
        continue as if nothing were wrong.
        """
        if self._conn.in_transaction:
            raise BudgetTransactionConflict(
                "reserve_budget() cannot prove atomicity inside an already-open "
                "transaction on this connection; commit or roll back first"
            )
        if not _valid_day(today):
            raise ValueError(f"invalid today: {today!r}")
        if not math.isfinite(cap_usd) or cap_usd < 0:
            raise ValueError(f"invalid cap_usd: {cap_usd!r}")
        if not math.isfinite(reserve_usd) or reserve_usd <= 0:
            raise ValueError(f"invalid reserve_usd: {reserve_usd!r}")
        if not math.isfinite(required_headroom) or required_headroom < 0:
            raise ValueError(f"invalid required_headroom: {required_headroom!r}")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            persisted = self._parse_budget_row(self.kv_get(BUDGET_KEY))
            if persisted is None or persisted["day"] < today:
                base_spent, base_calls = 0.0, 0
            elif persisted["day"] == today:
                base_spent, base_calls = persisted["spent_usd"], persisted["calls"]
            else:
                raise BudgetStateCorrupt(
                    f"persisted budget day {persisted['day']!r} is after today {today!r}: "
                    "backward clock or a future stored day -- refusing to mint a fresh budget"
                )
            if base_spent + reserve_usd + required_headroom > cap_usd + 1e-9:
                self.rollback()
                return False
            self.kv_set(
                BUDGET_KEY,
                json.dumps({"day": today, "spent_usd": base_spent + reserve_usd, "calls": base_calls + 1}),
                commit=False,
            )
            self.commit()
            return True
        except Exception:
            self.rollback()
            raise

    def settle_budget(self, *, day: str, delta_usd: float) -> None:
        """True up a reservation to the real provider cost: ``delta_usd`` =
        actual - reserved (usually negative). Same atomicity/refusal rules
        as reserve_budget() (an open caller transaction raises
        BudgetTransactionConflict before touching anything; a non-canonical
        ``day`` or non-finite ``delta_usd`` raises ValueError).

        A prior reserve_budget() for ``day`` must already have committed a
        row -- settle_budget() is only ever called after a reservation it
        is truing up. So an ABSENT row, or a persisted day strictly BEFORE
        ``day``, is not a legitimate "nothing to settle": it means the
        reservation this call is correcting is itself missing or was
        somehow undone, which is exactly the kind of silent ledger
        understatement that must block further spend, not be swallowed as
        a no-op. Both now raise BudgetStateCorrupt. The ONLY legitimate
        no-op is a persisted day strictly AFTER ``day``: a genuine UTC
        rollover already superseded this row with a newer reservation, and
        there is nothing left of the old one to correct. A row that IS
        present for ``day`` but unreadable (BudgetStateCorrupt from
        _parse_budget_row) is likewise NOT swallowed: settlement genuinely
        failed and the caller (bot.brain.think_result/reflect_result) must
        block further spend in this process rather than silently losing a
        real cost correction."""
        if self._conn.in_transaction:
            raise BudgetTransactionConflict(
                "settle_budget() cannot prove atomicity inside an already-open "
                "transaction on this connection; commit or roll back first"
            )
        if not _valid_day(day):
            raise ValueError(f"invalid day: {day!r}")
        if not math.isfinite(delta_usd):
            raise ValueError(f"invalid delta_usd: {delta_usd!r}")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            persisted = self._parse_budget_row(self.kv_get(BUDGET_KEY))
            if persisted is None:
                raise BudgetStateCorrupt(
                    f"settle_budget() found no persisted row for day {day!r}: a prior "
                    "reserve_budget() must already have written one -- a missing row "
                    "here is not a legitimate 'nothing to settle'"
                )
            if persisted["day"] == day:
                new_spent = max(0.0, persisted["spent_usd"] + delta_usd)
                self.kv_set(
                    BUDGET_KEY,
                    json.dumps({"day": day, "spent_usd": new_spent, "calls": persisted["calls"]}),
                    commit=False,
                )
            elif persisted["day"] < day:
                raise BudgetStateCorrupt(
                    f"persisted budget day {persisted['day']!r} is BEFORE the day being "
                    f"settled {day!r}: settle_budget() must never run before the "
                    "reservation it corrects was durably written"
                )
            # else: persisted["day"] > day -- a genuine rollover already
            # superseded this row; nothing left of the old reservation.
            self.commit()
        except Exception:
            self.rollback()
            raise

    def commit(self) -> None:
        """Public commit for callers that pass ``commit=False`` to add_fill /
        kv_set / clear_position / save_position to combine several writes into
        one local transaction (see LiveHands._settle_closed_on_exchange)."""
        self._conn.commit()

    def rollback(self) -> None:
        """Discard writes made under commit=False since the last commit.

        Pairs with ``commit()`` for a caller building one local transaction
        across several store calls: if any of them raises, the caller must
        roll back rather than leave the connection ``in_transaction`` -- a
        later, unrelated commit on the SAME connection (e.g. the CLI reusing
        one Store after its backoff) would otherwise durably apply half of a
        settlement that was never meant to be observed."""
        self._conn.rollback()
