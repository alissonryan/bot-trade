# Order-write rate limit and write-storm kill-switch — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cap how often this bot may submit writes to the venue, refusing only entries and halting only at a safe barrier, so a control-flow loop or a crash-restart loop cannot machine-gun orders at a browser-session account.

**Architecture:** A `WriteMeter` records every venue write to a new SQLite table *before* the POST is issued, and exposes rolling counts. The pure `collar.decide()` receives those counts as a parameter and refuses BUY with a new `rate_limit` reason. A separate hard ceiling raises `WriteStormHalt` at the `cycle.run_once` barrier — before any write, before the LLM — which `bot/cli.py` maps to new exit code 8. Nothing on an exit or protection path is ever refused.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`, pytest. No new dependencies. `requirements.txt` must not grow.

**Spec:** `docs/superpowers/specs/2026-09-08-order-write-rate-limit-design.md` — read it first; this plan argues from it.

## Global Constraints

- **Never block an exit.** Stop placement, stop replacement, cancel, flatten and SELL are counted but never refused, under any counter state. This is live invariant 3 (`AGENTS.md` § Live invariants).
- **Record before the POST, never after.** `kcex/client.py` does not retry POST/DELETE; a write that times out may still have executed.
- **`kcex/` must not import from `bot/`.** The meter lives in `bot/`, wraps the client from outside.
- **No live orders in tests or CI.** `KcexClient` is always mocked (`AGENTS.md` § Locked decisions).
- **TDD is mandatory** — this touches `bot/collar.py` and `bot/hands.py` (`CLAUDE.md` § P1 barriers).
- **Both windows are rolling**, not calendar: `writes_1h` = last 3_600_000 ms, `entries_24h` = last 86_400_000 ms.
- Defaults, verbatim: `MAX_WRITES_PER_HOUR=30`, `MAX_ENTRIES_PER_DAY=20`, `KILL_WRITES_PER_HOUR=90`. `0` disables that knob individually.
- New collar rejection reason string is exactly `rate_limit`. New exit code is exactly `8`.
- Run tests with `PYTHONPATH=. python -m pytest tests -q`. The suite is at 517 passing tests before this work; it must be green after every task.
- Commit after every task. Never commit `.env`, `data/`, or `.kcex-profile/`.

---

### Task 1: Settings knobs

**Files:**
- Modify: `bot/settings.py` (dataclass fields near `cooldown_minutes:61-65`, `__post_init__:68`, `from_env:76`)
- Test: `tests/bot/test_settings.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings.max_writes_per_hour: int`, `Settings.max_entries_per_day: int`, `Settings.kill_writes_per_hour: int`. All later tasks read these three names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/bot/test_settings.py`:

```python
def test_rate_limit_defaults_are_on(monkeypatch):
    for name in ("MAX_WRITES_PER_HOUR", "MAX_ENTRIES_PER_DAY", "KILL_WRITES_PER_HOUR"):
        monkeypatch.delenv(name, raising=False)
    s = Settings.from_env()
    assert (s.max_writes_per_hour, s.max_entries_per_day, s.kill_writes_per_hour) == (30, 20, 90)


def test_rate_limit_knobs_read_env(monkeypatch):
    monkeypatch.setenv("MAX_WRITES_PER_HOUR", "5")
    monkeypatch.setenv("MAX_ENTRIES_PER_DAY", "3")
    monkeypatch.setenv("KILL_WRITES_PER_HOUR", "9")
    s = Settings.from_env()
    assert (s.max_writes_per_hour, s.max_entries_per_day, s.kill_writes_per_hour) == (5, 3, 9)


def test_rate_limit_knobs_reject_negative(monkeypatch):
    monkeypatch.setenv("MAX_WRITES_PER_HOUR", "-1")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_kill_ceiling_below_soft_limit_is_a_config_error(monkeypatch):
    # A kill ceiling under the soft limit halts the process before the soft
    # gate could ever refuse anything -- the soft gate would be dead code.
    monkeypatch.setenv("MAX_WRITES_PER_HOUR", "30")
    monkeypatch.setenv("KILL_WRITES_PER_HOUR", "10")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_kill_ceiling_zero_is_allowed_with_soft_limit_on(monkeypatch):
    monkeypatch.setenv("MAX_WRITES_PER_HOUR", "30")
    monkeypatch.setenv("KILL_WRITES_PER_HOUR", "0")
    assert Settings.from_env().kill_writes_per_hour == 0
```

`pytest` is already imported at the top of that file; if it is not, add `import pytest`.

- [ ] **Step 2: Run them and watch them fail**

Run: `PYTHONPATH=. python -m pytest tests/bot/test_settings.py -q`
Expected: FAIL — `TypeError: Settings.__init__() got an unexpected keyword argument` or `AttributeError: 'Settings' object has no attribute 'max_writes_per_hour'`.

- [ ] **Step 3: Add the fields**

In `bot/settings.py`, next to `cooldown_minutes`:

```python
    max_writes_per_hour: int = 30
    max_entries_per_day: int = 20
    kill_writes_per_hour: int = 90
```

In `__post_init__`, after the existing cooldown guard:

```python
        for name in ("max_writes_per_hour", "max_entries_per_day", "kill_writes_per_hour"):
            if getattr(self, name) < 0:
                raise ValueError(f"invalid {name}: nonnegative integer required (0 disables)")
        if 0 < self.kill_writes_per_hour < self.max_writes_per_hour:
            raise ValueError(
                "invalid KILL_WRITES_PER_HOUR: a nonzero ceiling below MAX_WRITES_PER_HOUR "
                "halts the process before the soft gate can refuse anything"
            )
```

In `from_env`, alongside `cooldown_minutes=_f("COOLDOWN_MINUTES", 0.0)`:

```python
            max_writes_per_hour=_i("MAX_WRITES_PER_HOUR", 30),
            max_entries_per_day=_i("MAX_ENTRIES_PER_DAY", 20),
            kill_writes_per_hour=_i("KILL_WRITES_PER_HOUR", 90),
```

- [ ] **Step 4: Run the whole suite**

Run: `PYTHONPATH=. python -m pytest tests -q`
Expected: PASS. Other tests build `Settings` via `Settings.from_env().__dict__.copy()`, so the new fields flow through untouched.

- [ ] **Step 5: Commit**

```bash
git add bot/settings.py tests/bot/test_settings.py
git commit -m "feat(settings): rate-limit knobs, on by default"
```

---

### Task 2: `order_writes` table in the Store

**Files:**
- Modify: `bot/store.py` (table creation near the `kv` table at :113, `_migrate:178`, new methods near `kv_get:533`)
- Test: `tests/bot/test_store.py`

**Interfaces:**
- Consumes: `Settings` from Task 1 (not directly — the Store stays settings-free).
- Produces:
  - `Store.record_write(kind: str, ts_ms: int, *, commit: bool = True) -> None`
  - `Store.count_writes(since_ms: int, kind: str | None = None) -> int`
  - `Store.prune_writes(before_ms: int, *, commit: bool = True) -> int` (returns rows deleted)

- [ ] **Step 1: Write the failing tests**

Append to `tests/bot/test_store.py` (match the file's existing fixture style for building a `Store` in `tmp_path`):

```python
def test_write_counts_survive_reopen(tmp_path):
    path = tmp_path / "bot.db"
    store = Store(path, mode="paper")
    store.record_write("ENTRY", 1_000)
    store.record_write("PROTECTIVE", 2_000)
    store.close() if hasattr(store, "close") else None

    reopened = Store(path, mode="paper")
    assert reopened.count_writes(since_ms=0) == 2
    assert reopened.count_writes(since_ms=0, kind="ENTRY") == 1


def test_count_writes_is_a_half_open_window(tmp_path):
    store = Store(tmp_path / "bot.db", mode="paper")
    store.record_write("ENTRY", 1_000)
    store.record_write("ENTRY", 2_000)
    assert store.count_writes(since_ms=1_000) == 1  # strictly greater than
    assert store.count_writes(since_ms=999) == 2


def test_prune_spares_rows_inside_the_window(tmp_path):
    store = Store(tmp_path / "bot.db", mode="paper")
    store.record_write("ENTRY", 1_000)
    store.record_write("ENTRY", 5_000)
    assert store.prune_writes(before_ms=2_000) == 1
    assert store.count_writes(since_ms=0) == 1


def test_legacy_database_gains_the_table(tmp_path):
    path = tmp_path / "bot.db"
    Store(path, mode="paper")           # creates today's schema
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE order_writes")
    conn.commit()
    conn.close()
    reopened = Store(path, mode="paper")   # forward migration must recreate it
    assert reopened.count_writes(since_ms=0) == 0
```

- [ ] **Step 2: Run them and watch them fail**

Run: `PYTHONPATH=. python -m pytest tests/bot/test_store.py -q`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'record_write'`.

- [ ] **Step 3: Implement**

In `Store.__init__`, right after the `kv` table statement:

```python
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS order_writes (
                id INTEGER PRIMARY KEY,
                ts_ms INTEGER NOT NULL,
                kind TEXT NOT NULL
            )"""
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_order_writes_ts ON order_writes(ts_ms)"
        )
```

`CREATE TABLE IF NOT EXISTS` in `__init__` is itself the forward migration for this table — it runs on every open, including a legacy database, which is what the last test proves. Nothing goes in `_migrate()`; that helper only adds *columns* to existing tables.

Near `kv_get`, add:

```python
    # -- order write ledger -----------------------------------------------------

    def record_write(self, kind: str, ts_ms: int, *, commit: bool = True) -> None:
        """One row per venue write. Written BEFORE the POST is issued."""
        self._conn.execute(
            "INSERT INTO order_writes(ts_ms, kind) VALUES (?,?)", (int(ts_ms), str(kind))
        )
        if commit:
            self._conn.commit()

    def count_writes(self, since_ms: int, kind: str | None = None) -> int:
        if kind is None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM order_writes WHERE ts_ms > ?", (int(since_ms),)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM order_writes WHERE ts_ms > ? AND kind = ?",
                (int(since_ms), str(kind)),
            ).fetchone()
        return int(row[0]) if row else 0

    def prune_writes(self, before_ms: int, *, commit: bool = True) -> int:
        cur = self._conn.execute(
            "DELETE FROM order_writes WHERE ts_ms < ?", (int(before_ms),)
        )
        if commit:
            self._conn.commit()
        return int(cur.rowcount or 0)
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=. python -m pytest tests/bot/test_store.py tests/bot/test_mode_isolation.py -q`
Expected: PASS. If `Store` has no `close()`, drop that line from the first test rather than adding one.

- [ ] **Step 5: Commit**

```bash
git add bot/store.py tests/bot/test_store.py
git commit -m "feat(store): order_writes ledger with rolling counts and pruning"
```

---

### Task 3: `bot/ratelimit.py` — meter, counts, halt

**Files:**
- Create: `bot/ratelimit.py`
- Test: `tests/bot/test_ratelimit.py` (new)

**Interfaces:**
- Consumes: `Store.record_write`, `Store.count_writes`, `Store.prune_writes` (Task 2); `Settings.max_writes_per_hour`, `Settings.max_entries_per_day`, `Settings.kill_writes_per_hour` (Task 1).
- Produces, and every later task depends on these exact names:
  - `ENTRY: str = "ENTRY"`, `PROTECTIVE: str = "PROTECTIVE"`
  - `class WriteStormHalt(RuntimeError)`
  - `@dataclass(frozen=True) class WriteCounts: writes_1h: int; entries_24h: int`
  - `class WriteMeter:`
    - `__init__(self, store, settings)`
    - `record(self, kind: str, now_ms: int) -> None`
    - `counts(self, now_ms: int) -> WriteCounts`
    - `check_storm(self, now_ms: int, *, stop_observation: str | None = None) -> None`
    - `wrap(self, client)` → proxy (added in Task 5, not here)
  - `def rate_limited(counts: WriteCounts, settings) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/bot/test_ratelimit.py`:

```python
import pytest

from bot.ratelimit import ENTRY, PROTECTIVE, WriteCounts, WriteMeter, WriteStormHalt, rate_limited
from bot.settings import Settings
from bot.store import Store

HOUR = 3_600_000
DAY = 86_400_000


def _settings(**kwargs) -> Settings:
    data = Settings.from_env().__dict__.copy()
    data.update(kwargs)
    return Settings(**data)


def _meter(tmp_path, **kwargs) -> WriteMeter:
    return WriteMeter(Store(tmp_path / "bot.db", mode="paper"), _settings(**kwargs))


def test_counts_are_rolling_windows(tmp_path):
    meter = _meter(tmp_path)
    now = 10 * DAY
    meter.record(ENTRY, now - HOUR - 1)        # outside both? no: inside 24h
    meter.record(ENTRY, now - 1)               # inside both
    meter.record(PROTECTIVE, now - 1)
    counts = meter.counts(now)
    assert counts.writes_1h == 2               # the two at now-1
    assert counts.entries_24h == 2             # both ENTRY rows are inside 24h


def test_record_prunes_only_beyond_48h(tmp_path):
    meter = _meter(tmp_path)
    now = 10 * DAY
    meter.record(ENTRY, now - 47 * HOUR)
    meter.record(ENTRY, now - 49 * HOUR)
    meter.record(ENTRY, now)
    assert meter.store.count_writes(since_ms=0) == 2   # the 49h row is gone


def test_rate_limited_on_each_knob():
    on = _settings(max_writes_per_hour=30, max_entries_per_day=20)
    assert rate_limited(WriteCounts(writes_1h=30, entries_24h=0), on) is True
    assert rate_limited(WriteCounts(writes_1h=29, entries_24h=20), on) is True
    assert rate_limited(WriteCounts(writes_1h=29, entries_24h=19), on) is False


def test_zero_disables_each_knob_independently():
    only_entries = _settings(max_writes_per_hour=0, max_entries_per_day=20)
    assert rate_limited(WriteCounts(writes_1h=10_000, entries_24h=0), only_entries) is False
    assert rate_limited(WriteCounts(writes_1h=10_000, entries_24h=20), only_entries) is True
    off = _settings(max_writes_per_hour=0, max_entries_per_day=0)
    assert rate_limited(WriteCounts(writes_1h=10_000, entries_24h=10_000), off) is False


def test_check_storm_raises_at_the_ceiling(tmp_path):
    meter = _meter(tmp_path, max_writes_per_hour=1, kill_writes_per_hour=2)
    now = 10 * DAY
    meter.record(PROTECTIVE, now)
    meter.check_storm(now)                       # one write: below the ceiling
    meter.record(PROTECTIVE, now)
    with pytest.raises(WriteStormHalt) as exc:
        meter.check_storm(now, stop_observation="stop_present")
    assert "stop_present" in str(exc.value)


def test_check_storm_reports_unknown_when_nothing_was_observed(tmp_path):
    meter = _meter(tmp_path, max_writes_per_hour=1, kill_writes_per_hour=1)
    now = 10 * DAY
    meter.record(PROTECTIVE, now)
    with pytest.raises(WriteStormHalt) as exc:
        meter.check_storm(now)
    assert "unknown" in str(exc.value)


def test_kill_switch_zero_never_raises(tmp_path):
    meter = _meter(tmp_path, max_writes_per_hour=0, kill_writes_per_hour=0)
    now = 10 * DAY
    for _ in range(100):
        meter.record(ENTRY, now)
    meter.check_storm(now)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `PYTHONPATH=. python -m pytest tests/bot/test_ratelimit.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.ratelimit'`.

- [ ] **Step 3: Implement `bot/ratelimit.py`**

```python
"""Submission-rate collar: how OFTEN this bot may write to the venue.

`bot/collar.py` gates position risk. Nothing gated submission rate, and the
exposure here is specific: the venue session is a browser token, POST/DELETE are
never retried (so a duplicate order can only come from our own control flow), and
`bot/cli.py` restarts with backoff -- a crash after a successful write, repeated,
is a write loop no in-process counter would ever see. Hence SQLite, not a field.

The meter OBSERVES every write and REFUSES only where refusing is safe: the
collar's BUY branch, and the barrier before a cycle begins. It must never refuse
a stop, a cancel, a flatten or a SELL -- a safety feature that leaves a position
unprotected has broken live invariant 3, which is worse than the storm it stopped.
"""

from __future__ import annotations

from dataclasses import dataclass

ENTRY = "ENTRY"
PROTECTIVE = "PROTECTIVE"

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
RETENTION_MS = 48 * HOUR_MS


class WriteStormHalt(RuntimeError):
    """Writes are being issued in a pattern nobody designed. Exit code 8."""


@dataclass(frozen=True)
class WriteCounts:
    """The only thing the pure collar sees. No I/O behind it."""

    writes_1h: int
    entries_24h: int


def rate_limited(counts: WriteCounts, settings) -> bool:
    """Soft trip: refuse an ENTRY. Each knob is disabled independently by 0."""
    if settings.max_writes_per_hour and counts.writes_1h >= settings.max_writes_per_hour:
        return True
    if settings.max_entries_per_day and counts.entries_24h >= settings.max_entries_per_day:
        return True
    return False


class WriteMeter:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings

    def record(self, kind: str, now_ms: int) -> None:
        """Called BEFORE the POST. A write that times out may still have executed."""
        self.store.record_write(kind, now_ms)
        self.store.prune_writes(now_ms - RETENTION_MS)

    def counts(self, now_ms: int) -> WriteCounts:
        return WriteCounts(
            writes_1h=self.store.count_writes(since_ms=now_ms - HOUR_MS),
            entries_24h=self.store.count_writes(since_ms=now_ms - DAY_MS, kind=ENTRY),
        )

    def check_storm(self, now_ms: int, *, stop_observation: str | None = None) -> None:
        """Hard trip. Called at the cycle barrier, never in the middle of a write."""
        ceiling = self.settings.kill_writes_per_hour
        if not ceiling:
            return
        writes = self.store.count_writes(since_ms=now_ms - HOUR_MS)
        if writes < ceiling:
            return
        raise WriteStormHalt(
            f"{writes} venue writes in the last hour (ceiling {ceiling}); "
            f"resident stop last observed: {stop_observation or 'unknown'}"
        )
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=. python -m pytest tests/bot/test_ratelimit.py -q`
Expected: PASS, all 7.

- [ ] **Step 5: Commit**

```bash
git add bot/ratelimit.py tests/bot/test_ratelimit.py
git commit -m "feat(ratelimit): write meter, rolling counts and storm halt"
```

---

### Task 4: Collar refuses BUY with `rate_limit`

**Files:**
- Modify: `bot/collar.py` (`decide` signature :50-60, BUY branch after the `day_loss` check at :105)
- Test: `tests/bot/test_collar.py`

**Interfaces:**
- Consumes: `WriteCounts`, `rate_limited` (Task 3).
- Produces: `decide(..., write_counts: WriteCounts | None = None)`. `None` means "no limiter wired" and changes nothing — every existing caller and test keeps its behaviour.

- [ ] **Step 1: Write the failing tests**

Append to `tests/bot/test_collar.py`, reusing that file's existing snapshot/settings helpers (if its flat-snapshot helper has a different name, use it — do not add a second one):

```python
from bot.ratelimit import WriteCounts


def test_buy_refused_when_writes_exhausted():
    gate = decide(
        TradeIntent("BUY", 1.0, "", "trend"), _flat_snap(),
        _settings(max_writes_per_hour=2),
        session_ok=True, day_pnl_usdt=0.0,
        write_counts=WriteCounts(writes_1h=2, entries_24h=0),
    )
    assert (gate.ok, gate.rule) == (False, "rate_limit")


def test_sell_is_never_rate_limited():
    snap = replace(_flat_snap(), bot_qty=0.001, bot_avg_entry=100_000.0)
    gate = decide(
        TradeIntent("SELL", 1.0, "", "trend"), snap,
        _settings(max_writes_per_hour=1),
        session_ok=True, day_pnl_usdt=0.0,
        write_counts=WriteCounts(writes_1h=10_000, entries_24h=10_000),
    )
    assert gate.ok and gate.action == "SELL"


def test_no_write_counts_means_no_limiter():
    gate = decide(
        TradeIntent("BUY", 1.0, "", "trend"), _flat_snap(),
        _settings(max_writes_per_hour=1),
        session_ok=True, day_pnl_usdt=0.0,
    )
    assert gate.ok and gate.rule == "ok_buy"


def test_a_backward_clock_jump_fails_closed_for_buy_only():
    """A clock that jumps back shrinks the window, so the count rises. That must
    refuse an entry and still let an exit out."""
    spent = WriteCounts(writes_1h=10_000, entries_24h=10_000)
    settings = _settings(max_writes_per_hour=30)
    buy = decide(TradeIntent("BUY", 1.0, "", "trend"), _flat_snap(), settings,
                 session_ok=True, day_pnl_usdt=0.0, write_counts=spent)
    held = replace(_flat_snap(), bot_qty=0.001, bot_avg_entry=100_000.0)
    sell = decide(TradeIntent("SELL", 1.0, "", "trend"), held, settings,
                  session_ok=True, day_pnl_usdt=0.0, write_counts=spent)
    assert (buy.ok, buy.rule) == (False, "rate_limit")
    assert sell.ok and sell.action == "SELL"


def test_day_loss_outranks_rate_limit():
    # Ordering is observable in the audit; day_loss is the more serious fact.
    gate = decide(
        TradeIntent("BUY", 1.0, "", "trend"), _flat_snap(),
        _settings(max_writes_per_hour=1, max_day_loss_usdt=10.0),
        session_ok=True, day_pnl_usdt=-50.0,
        write_counts=WriteCounts(writes_1h=10_000, entries_24h=0),
    )
    assert gate.rule == "day_loss"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `PYTHONPATH=. python -m pytest tests/bot/test_collar.py -q`
Expected: FAIL — `TypeError: decide() got an unexpected keyword argument 'write_counts'`.

- [ ] **Step 3: Implement**

Add the import at the top of `bot/collar.py`:

```python
from bot.ratelimit import WriteCounts, rate_limited
```

Add the parameter to `decide`, in the keyword-only block next to `last_loss_exit_ms`:

```python
    write_counts: WriteCounts | None = None,
```

In the BUY branch, immediately after the `day_loss` check and before the confidence check:

```python
    # How OFTEN we may write, not how much we may risk. Placed here so a
    # day-loss halt still reports itself as day_loss -- the more serious fact.
    # SELL returned above and never reads this: refusing an exit is how a
    # rate limiter would create the unprotected position it was meant to prevent.
    if write_counts is not None and rate_limited(write_counts, settings):
        return GateResult(False, "rate_limit", "BUY")
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=. python -m pytest tests/bot/test_collar.py tests/bot/test_cooldown.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/collar.py tests/bot/test_collar.py
git commit -m "feat(collar): refuse BUY when the write budget is spent"
```

---

### Task 5: Wrap the client, count every write

**Files:**
- Modify: `bot/ratelimit.py` (add `WriteMeter.wrap` and `_MeteredClient`)
- Modify: `bot/hands.py` (`PaperHands.__init__:326`, its buy/`_close` paths at :400-432; `LiveHands.__init__:462`)
- Test: `tests/bot/test_ratelimit.py`, `tests/bot/test_hands_live.py`

**Interfaces:**
- Consumes: `WriteMeter.record`, `ENTRY`, `PROTECTIVE` (Task 3).
- Produces:
  - `WriteMeter.wrap(client) -> _MeteredClient` — proxies the whole `KcexClient` surface; `place_market`, `place_trigger` and `cancel_order` record first, then delegate.
  - `METERED_WRITE_METHODS: frozenset[str] = frozenset({"place_market", "place_trigger", "cancel_order"})`
  - `PaperHands(settings, store, *, meter=None)` and `LiveHands(settings, store, client, *, rules=None, sleep=time.sleep, meter=None)`.

The proxy is the choke point on purpose. Recording at each call site in `bot/hands.py` works today and silently misses the write someone adds next month.

- [ ] **Step 1: Write the failing tests**

Append to `tests/bot/test_ratelimit.py`:

```python
class _FakeClient:
    def __init__(self):
        self.calls = []

    def place_market(self, **kw):
        self.calls.append("place_market")
        return {"data": {"orderId": "1"}}

    def place_trigger(self, **kw):
        self.calls.append("place_trigger")
        return {"data": {"orderId": "2"}}

    def cancel_order(self, order_id):
        self.calls.append("cancel_order")
        return {"code": 0}

    def balances(self, **kw):
        self.calls.append("balances")
        return {}


def test_wrap_records_entry_and_protective(tmp_path):
    meter = _meter(tmp_path)
    client = meter.wrap(_FakeClient())
    client.place_market(side=1)
    client.place_trigger(price="1")
    client.cancel_order("abc")
    counts = meter.counts(now_ms=int(__import__("time").time() * 1000))
    assert counts.writes_1h == 3
    assert counts.entries_24h == 1          # only the BUY market order is an ENTRY


def test_a_sell_market_order_is_protective(tmp_path):
    meter = _meter(tmp_path)
    client = meter.wrap(_FakeClient())
    client.place_market(side=2)
    assert meter.counts(now_ms=int(__import__("time").time() * 1000)).entries_24h == 0


def test_reads_are_passed_through_uncounted(tmp_path):
    meter = _meter(tmp_path)
    client = meter.wrap(_FakeClient())
    client.balances(currencies="BTC,USDT")
    assert meter.store.count_writes(since_ms=0) == 0


def test_the_row_is_written_even_when_the_post_raises(tmp_path):
    class Boom(_FakeClient):
        def place_market(self, **kw):
            raise RuntimeError("network")

    meter = _meter(tmp_path)
    client = meter.wrap(Boom())
    with pytest.raises(RuntimeError):
        client.place_market(side=1)
    assert meter.store.count_writes(since_ms=0) == 1


def test_every_write_method_on_the_real_client_is_metered():
    """Guard: a new POST/DELETE on KcexClient must not slip through uncounted."""
    import inspect

    from bot.ratelimit import METERED_WRITE_METHODS
    from kcex.client import KcexClient

    writes = set()
    for name, fn in inspect.getmembers(KcexClient, inspect.isfunction):
        if name.startswith("_") or name in {"request", "get", "post", "delete"}:
            continue
        source = inspect.getsource(fn)
        if "self.post(" in source or "self.delete(" in source:
            writes.add(name)
    assert writes == set(METERED_WRITE_METHODS), (
        f"unmetered venue writes: {sorted(writes - set(METERED_WRITE_METHODS))}; "
        f"stale entries: {sorted(set(METERED_WRITE_METHODS) - writes)}"
    )
```

If that last test reports extra names such as `place_limit`, do **not** loosen the assertion — add the name to `METERED_WRITE_METHODS`. Every venue write gets counted; the guard is the point of the task.

- [ ] **Step 2: Run them and watch them fail**

Run: `PYTHONPATH=. python -m pytest tests/bot/test_ratelimit.py -q`
Expected: FAIL — `AttributeError: 'WriteMeter' object has no attribute 'wrap'`.

- [ ] **Step 3: Implement the proxy**

Add to `bot/ratelimit.py`:

```python
import time

METERED_WRITE_METHODS = frozenset({"place_market", "place_trigger", "cancel_order"})


class _MeteredClient:
    """Thin proxy: record, then delegate. Reads pass straight through.

    It cannot refuse a call. A transport that can refuse is a transport that can
    refuse the place_trigger protecting a fresh position -- see the module docstring.
    """

    def __init__(self, client, meter: "WriteMeter"):
        self._client = client
        self._meter = meter

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if name not in METERED_WRITE_METHODS:
            return attr

        def metered(*args, **kwargs):
            kind = ENTRY if _is_entry(name, args, kwargs) else PROTECTIVE
            self._meter.record(kind, int(time.time() * 1000))
            return attr(*args, **kwargs)

        return metered


def _is_entry(name: str, args, kwargs) -> bool:
    """Only a BUY market order is an entry. KCEX `side`: 1 buy, 2 sell."""
    if name != "place_market":
        return False
    side = kwargs.get("side", args[0] if args else None)
    return str(side) == "1"
```

And on `WriteMeter`:

```python
    def wrap(self, client) -> _MeteredClient:
        return _MeteredClient(client, self)
```

Confirm the buy/sell `side` encoding against `kcex/client.py::place_market` before trusting `"1"`; if the client takes a string like `"BUY"`, widen `_is_entry` to accept both and say so in a comment. **Do not guess** — read the function.

- [ ] **Step 4: Wire the hands**

`LiveHands.__init__` gains `meter=None` in its keyword-only block, and the assignment becomes:

```python
        self.meter = meter
        self.client = meter.wrap(client) if meter is not None else client
```

`PaperHands.__init__` gains `*, meter=None` and stores `self.meter = meter`. Paper writes to no venue, so it records the same four logical writes a live trade costs, keeping the P0 replay faithful and the `rate_limit` reason reachable in paper. In the BUY branch, before the ledger transaction begins:

```python
        if self.meter is not None:
            now = int(time.time() * 1000)
            self.meter.record(ENTRY, now)          # the entry market order
            if gate.stop_price:
                self.meter.record(PROTECTIVE, now)  # the stop that follows it
```

and at the top of `_close`:

```python
        if self.meter is not None:
            now = int(time.time() * 1000)
            self.meter.record(PROTECTIVE, now)      # cancel the resident stop
            self.meter.record(PROTECTIVE, now)      # then the exit market order
```

- [ ] **Step 5: Prove the protection paths still run with every counter blown**

This is spec test item 3, and it is the one that matters most: the failure mode
being designed against is a limiter that quietly stops a stop from being placed.
Append to `tests/bot/test_hands_live.py`, reusing that file's mocked-client fixture:

```python
def test_protective_writes_execute_with_the_budget_long_gone(tmp_path):
    store = Store(tmp_path / "bot-live.db", mode="live")
    meter = WriteMeter(store, _settings(max_writes_per_hour=1, max_entries_per_day=1,
                                        kill_writes_per_hour=0))
    for _ in range(500):
        meter.record(PROTECTIVE, int(time.time() * 1000))
    hands = LiveHands(_settings(), store, FakeClient(), rules=_rules(), meter=meter)
    # ... open a position through the file's existing helper, then:
    assert hands._place_stop("0.001", 90_000.0) is not None
    assert hands._flatten("0.001", _snap()) is True
    assert hands.cancel_if_ours(hands.stop_order_id) is True
```

Adapt the position setup to the helpers `tests/bot/test_hands_live.py` already
uses. The assertion that matters is that none of the three raises or returns a
refusal because of the counter.

Run: `PYTHONPATH=. python -m pytest tests/bot/test_hands_live.py -q`
Expected: PASS.

- [ ] **Step 6: Run the suite**

Run: `PYTHONPATH=. python -m pytest tests -q`
Expected: PASS. `LiveHands` tests construct without `meter`, get `None`, and behave exactly as before.

- [ ] **Step 7: Commit**

```bash
git add bot/ratelimit.py bot/hands.py tests/bot/test_ratelimit.py tests/bot/test_hands_live.py
git commit -m "feat(ratelimit,hands): meter every venue write at one choke point"
```

---

### Task 6: Barrier check and exit code 8

**Files:**
- Modify: `bot/cycle.py` (`run_once` signature :50-61, barrier `try` block at :70-83, the `decide(...)` call)
- Modify: `bot/cli.py` (exit constants near :72-107, `main` hands construction at :225-228, `_loop` signature and its `except` chain at :278-310)
- Test: `tests/bot/test_cycle.py`, `tests/bot/test_cli.py`

**Interfaces:**
- Consumes: `WriteMeter.check_storm`, `WriteMeter.counts`, `WriteStormHalt` (Task 3); `decide(..., write_counts=)` (Task 4).
- Produces: `run_once(..., meter: WriteMeter | None = None)`; `bot.cli.EXIT_WRITE_STORM = 8`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/bot/test_cycle.py` (reuse that file's existing `run_once` harness and fakes):

```python
def test_storm_halts_before_the_llm_and_before_any_write(tmp_path):
    store = Store(tmp_path / "bot.db", mode="paper")
    meter = WriteMeter(store, _settings(max_writes_per_hour=1, kill_writes_per_hour=1))
    meter.record(PROTECTIVE, int(time.time() * 1000))
    called = []
    with pytest.raises(WriteStormHalt):
        run_once(
            settings=_settings(max_writes_per_hour=1, kill_writes_per_hour=1),
            eye=eye, store=store, client=client, hands=hands, budget=budget,
            last_llm_ms=0, last_px=0.0,
            think=lambda *a, **k: called.append("llm"),
            meter=meter,
        )
    assert called == []          # the LLM was never reached


def test_counts_reach_the_collar(tmp_path):
    """A spent write budget must show up as a rate_limit gate in the audit."""
    store = Store(tmp_path / "bot.db", mode="paper")
    settings = _settings(max_writes_per_hour=1, kill_writes_per_hour=0)
    meter = WriteMeter(store, settings)
    meter.record(PROTECTIVE, int(time.time() * 1000))
    _, _, gate = run_once(
        settings=settings, eye=eye, store=store, client=client, hands=hands,
        budget=budget, last_llm_ms=0, last_px=0.0,
        think=lambda *a, **k: ThinkResult(TradeIntent("BUY", 1.0, "", "trend"), "ok"),
        meter=meter,
    )
    assert gate is not None and gate.rule == "rate_limit"
```

Append to `tests/bot/test_cli.py`:

```python
def test_write_storm_returns_exit_8(monkeypatch):
    from bot import cli
    from bot.ratelimit import WriteStormHalt

    def boom(**kwargs):
        raise WriteStormHalt("120 venue writes in the last hour (ceiling 90); "
                             "resident stop last observed: stop_present")

    monkeypatch.setattr(cli, "run_once", boom)
    assert cli._loop(True, settings, client, store, eye, hands) == cli.EXIT_WRITE_STORM
    assert cli.EXIT_WRITE_STORM == 8
```

Adapt both to the fixtures those files already use; do not invent a second harness.

- [ ] **Step 2: Run them and watch them fail**

Run: `PYTHONPATH=. python -m pytest tests/bot/test_cycle.py tests/bot/test_cli.py -q`
Expected: FAIL — `run_once() got an unexpected keyword argument 'meter'` and `AttributeError: module 'bot.cli' has no attribute 'EXIT_WRITE_STORM'`.

- [ ] **Step 3: Implement in `bot/cycle.py`**

Add `meter: WriteMeter | None = None` to the `run_once` signature. As the **first statement inside the barrier `try` block**, before `local_exit_reason` and long before `poll_heavy()`:

```python
        if meter is not None:
            # Before any write and before the LLM. Never mid-write: aborting a
            # half-finished exit is how a position ends up unprotected.
            meter.check_storm(now, stop_observation=getattr(hands, "last_stop_observation", None))
```

At the `decide(...)` call further down, add:

```python
            write_counts=meter.counts(now) if meter is not None else None,
```

- [ ] **Step 4: Implement in `bot/cli.py`**

Add next to the other exit constants, with a comment in the style of the ones already there:

```python
# The bot is issuing venue writes in a pattern nobody designed -- a control-flow
# loop, or a crash/restart loop that survives an in-process counter. Distinct from
# 2/5/6/7: those name a position that needs squaring by hand; 8 names the bot's own
# behaviour. Read data/bot.log and the order_writes table before restarting it.
EXIT_WRITE_STORM = 8
```

Build the meter in `main` before the hands and pass it to both:

```python
    meter = WriteMeter(store, settings)
    if settings.mode == "live":
        hands: PaperHands | LiveHands = LiveHands(settings, store, client, rules=eye.rules, meter=meter)
    else:
        hands = PaperHands(settings, store, meter=meter)
```

`_loop` takes `meter` and forwards it to `run_once`. Add the handler to the `except` chain, after `SessionDead`:

```python
        except WriteStormHalt as exc:
            log.critical(
                "WRITE STORM: %s. This process placed nothing on this cycle and exits "
                "now; it will NOT resume by itself. The counts come from the "
                "order_writes table in this mode's database -- read them, and "
                "data/bot.log, before restarting. If a position is open, the stop "
                "observation above is the last thing reconcile() actually saw; confirm "
                "protection on the exchange by hand.",
                exc,
            )
            return EXIT_WRITE_STORM
```

- [ ] **Step 5: Run the suite**

Run: `PYTHONPATH=. python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add bot/cycle.py bot/cli.py tests/bot/test_cycle.py tests/bot/test_cli.py
git commit -m "feat(cycle,cli): storm halt at the barrier, exit code 8"
```

---

### Task 7: P0 replay and the opt-out equivalence artifact

**Files:**
- Modify: `bot/backtest.py` (`replay` at :215-260 and its `decide(...)` call at :301)
- Create: `data/backtest/p5-verify.py`
- Test: `tests/test_backtest.py`

**Interfaces:**
- Consumes: `WriteMeter`, `ENTRY`, `PROTECTIVE` (Tasks 3 and 5); `decide(..., write_counts=)` (Task 4).
- Produces: `data/backtest/p5-optout-equivalence.json`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backtest.py`:

```python
def test_replay_counts_writes_and_defaults_never_trip():
    history = _history(400)           # reuse this file's history helper
    settings = _settings()            # shipped defaults: 30 / 20 / 90
    result = replay(history, settings, fixed_policy([TradeIntent("BUY", 1.0, "", "trend")] * 400))
    assert not any(d["gate"]["rule"] == "rate_limit" for d in result["decisions"])


def test_replay_can_trip_the_limiter_when_told_to():
    history = _history(400)
    settings = _settings(max_writes_per_hour=1)
    result = replay(history, settings, fixed_policy([TradeIntent("BUY", 1.0, "", "trend")] * 400))
    assert any(d["gate"]["rule"] == "rate_limit" for d in result["decisions"])
```

Use the helper names that file already defines; if the replay result is a dataclass rather than a dict, read `.decisions`.

- [ ] **Step 2: Run and watch fail**

Run: `PYTHONPATH=. python -m pytest tests/test_backtest.py -q`
Expected: FAIL on the second test — nothing trips, because the replay passes no counts.

- [ ] **Step 3: Implement in `bot/backtest.py`**

In `replay`, next to the existing in-memory journal Store:

```python
    from bot.ratelimit import ENTRY, PROTECTIVE, WriteMeter
    meter = WriteMeter(Store(Path(":memory:")), settings)
```

Record the same four writes a live trade costs, using replay time, not wall clock. In `close(...)`, before the ledger math:

```python
        meter.record(PROTECTIVE, exit_ms)   # cancel the resident stop
        meter.record(PROTECTIVE, exit_ms)   # then the exit market order
```

In the BUY branch, after the fill is accepted:

```python
            meter.record(ENTRY, bar.t * 1000)
            meter.record(PROTECTIVE, bar.t * 1000)
```

And in the `decide(...)` call:

```python
                      write_counts=meter.counts(bar.t * 1000),
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=. python -m pytest tests/test_backtest.py -q`
Expected: PASS.

- [ ] **Step 5: Produce the equivalence artifact**

Model `data/backtest/p5-verify.py` on the existing `data/backtest/p2-verify.py`: run the frozen sample in **isolated processes**, once with the shipped defaults and once with `MAX_WRITES_PER_HOUR=0 MAX_ENTRIES_PER_DAY=0 KILL_WRITES_PER_HOUR=0`, hash the inputs and the `bot/` + `kcex/` sources into the artifact, and assert the episodes are identical:

```python
assert before['input_sha256'] == after['input_sha256']
assert before['episodes'] == after['episodes'], 'defaults changed the replay'
```

Write `data/backtest/p5-optout-equivalence.json` with `equivalent`, `isolated_processes`, `real_decisions`, `input_sha256`, both source hashes, and a summary carrying `rate_limit_rejections` per arm.

If the two arms differ, **stop and report it**. That result means the shipped defaults touch a normal sample and the defaults are wrong — it is not a test to bend until it passes.

- [ ] **Step 6: Commit**

```bash
git add bot/backtest.py tests/test_backtest.py data/backtest/p5-verify.py
git commit -m "feat(backtest): count simulated writes; P0 opt-out equivalence"
```

`data/` is gitignored: confirm with `git status --short data/backtest/` whether the verify script and artifact are actually tracked. If they are ignored, leave them local like the P2/P4 artifacts and say so in the report — do not force-add them.

---

### Task 8: Documentation

**Files:**
- Modify: `AGENTS.md` (new `## P5 — order-write rate limit` section after § P4; exit-code list in § Live invariants)
- Modify: `CLAUDE.md` (Layout table, a `## P5 rate limit` paragraph after § P2 cooldown)
- Modify: `.env.example` if the repo has one (`ls .env.example`)

**Interfaces:**
- Consumes: everything above. Produces: no code.

- [ ] **Step 1: Write the AGENTS.md section**

After § P4, in the voice of the surrounding sections — what is true, what is measured, what is not:

```markdown
## P5 — order-write rate limit and storm halt

- `MAX_WRITES_PER_HOUR=0`, `MAX_ENTRIES_PER_DAY=0`, `KILL_WRITES_PER_HOUR=0` disable each knob independently; all three at `0` reproduce pre-P5 behaviour exactly (see `data/backtest/p5-optout-equivalence.json`). Unlike P1/P2/P4 this ships **on**: a guard-rail only counts if it is mounted before the accident.
- Every venue write (`place_market`, `place_trigger`, `cancel_order`) is recorded in `order_writes` **before** the POST, in the mode's own database. POST/DELETE are never retried, so a write that timed out may still have executed; counting after the fact would undercount in exactly the situation that matters.
- Both windows are rolling, not calendar. A UTC-day counter would hand a loop a fresh allowance at midnight.
- **Only entries are refused.** Stop, stop replacement, cancel, flatten and SELL are counted and always allowed. A limiter that can refuse an exit is a limiter that creates the unprotected position of invariant 3. The collar's `rate_limit` sits after `day_loss` and is unreachable from the SELL branch.
- The hard ceiling raises `WriteStormHalt` at the `cycle.run_once` barrier — before any write, before the LLM — and `bot/cli.py` returns **exit 8**. Never mid-write. The halt carries `LiveHands.last_stop_observation` verbatim (`stop_present` / `stop_absent` / `unknown`) and never infers one.
- **Scope, honestly:** this bounds repetition, not correctness. One wrong order is still one wrong order, and 30 writes an hour is ample rope. It also cannot see writes made by anything other than this process — the owner's own manual orders are invisible to it by design. The defaults are argued from the cycle arithmetic (~4 writes per trade, one position at a time, ~12 decisions/h at `CYCLE_MINUTES=5`), **not** calibrated against a measured distribution of real write bursts: no such sample exists, because no live order has ever been sent through this client.
```

Add exit 8 to the exit-code paragraph in § Live invariants, matching the existing prose.

- [ ] **Step 2: Update CLAUDE.md**

Add to the Layout table:

```markdown
| `bot/ratelimit.py` | Write meter, rolling counts, storm halt (exit 8) |
```

and a short § P5 paragraph after § P2, pointing at AGENTS.md as canonical.

- [ ] **Step 3: Verify the docs match the code**

Run: `PYTHONPATH=. python -m pytest tests -q` one final time, then re-read both files against the shipped defaults, the reason string `rate_limit` and exit code 8. A doc that disagrees with the code is worse than no doc.

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md CLAUDE.md
git commit -m "docs: P5 order-write rate limit and exit code 8"
```

---

## Done when

- `PYTHONPATH=. python -m pytest tests -q` is green, with at least 25 new tests over the 517 baseline.
- Every spec test item (1-11) has a named test.
- `git grep -n "rate_limit" bot/` shows the reason produced in exactly one place.
- The equivalence artifact exists and reports `equivalent: true`, or the run is stopped and reported.
- Nothing in `requirements.txt` changed.
