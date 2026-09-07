"""Fix 2 (2026-09 re-review, blocker 2): `ExitLatchBlocked` is another
RuntimeError with no dedicated handler in `bot/cli.py`, so it falls into the
same generic "keep the loop alive, loudly" branch the coordinator had just
fixed for `TerminalEvidenceUnavailable` one commit earlier -- the exact same
livelock, for its sibling exception. The review's own probe drove
`_loop -> run_once -> mark` with a latch present and hit `time.sleep(1)`; at
boot the same exception escaped with no mapped exit code at all.

These tests drive the REAL guard (`LiveHands._guard_exit`) through the REAL
`bot.cli._loop`, both at boot (before the while-loop even starts) and at
runtime (a latch appearing between quote polls, simulating a foreign/future
writer), plus a legacy CLOSING row and a storage read failure while the
guard itself is reading. `time.sleep` is monkeypatched to raise so any retry
attempt fails the test immediately instead of silently looping.
"""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.hands import EXIT_LATCH_KEY, ExitLatchBlocked, LiveHands
from bot.settings import Settings
from bot.store import Store
from bot.types import Bar, Snapshot

FOREIGN_BTC = 0.00064


def _live_settings(**kw) -> Settings:
    d = Settings.from_env().__dict__.copy()
    d["mode"] = "live"
    d.update(kw)
    return Settings(**d)


def _latch(phase="cancel_pending", entry_id="oid-m1", **kw):
    base = {
        "schema": 1, "operation_id": "op-1", "entry_id": entry_id, "stop_id": "oid-t",
        "qty_original": 0.00025, "qty_booked": 0.0, "qty_residual": 0.00025,
        "baselines": [{"total": FOREIGN_BTC, "ts_ms": 1000}], "exit_reason": "ok_close",
        "phase": phase, "sell_order_id": None,
    }
    base.update(kw)
    return json.dumps(base)


def _open_position(store, *, state="OPEN"):
    store.remember_order("oid-m1")
    store.remember_order("oid-t")
    store.save_position(
        qty=0.00025, entry=80000.0, stop_price=79200.0, entry_order_id="oid-m1",
        stop_order_id="oid-t", state=state, entry_source="estimated", btc_before=FOREIGN_BTC,
    )


class FakeClient:
    """Balances/open orders never change -- a livelocked loop would see the
    exact same condition forever, and any write proves the fence was bypassed."""

    def __init__(self, *, btc, open_ids):
        self.btc = btc
        self.open_ids = open_ids
        self.calls = []

    def user_info(self):
        self.calls.append(("user_info",))
        return {"code": 0}

    def balances(self, currencies="BTC,USDT"):
        self.calls.append(("balances",))
        return {"data": [{"currency": "BTC", "available": str(self.btc), "frozen": "0", "total": str(self.btc)},
                         {"currency": "USDT", "available": "450", "frozen": "0", "total": "450"}]}

    def open_orders(self, **kw):
        self.calls.append(("open_orders",))
        rows = [{"id": i} for i in self.open_ids]
        return {"data": rows, "total": len(rows)}

    def place_market(self, **kwargs):
        self.calls.append(("market", kwargs))
        raise AssertionError("must never POST a market order: ExitLatchBlocked refuses before any write")

    def place_trigger(self, **kwargs):
        self.calls.append(("trigger", kwargs))
        raise AssertionError("must never place a trigger here")

    def cancel_order(self, order_id: str):
        self.calls.append(("cancel", order_id))
        raise AssertionError("must never DELETE the resident stop")

    def my_deals(self, *a, **k):
        self.calls.append(("my_deals",))
        return {"data": []}


class FakeLiveEye:
    """Minimal Eye double matching the real Eye/cycle wiring used by _loop."""

    def __init__(self, *, last=80000.0, bid=79999.0, ask=80001.0, atr=400.0, on_poll_quotes=None):
        self.last, self.bid, self.ask, self.atr = last, bid, ask, atr
        self.free_usdt = 450.0
        self.bot_qty = 0.0
        self.bot_avg_entry = None
        self.rules = None
        self.last_bot_pnl_usdt = 0.0
        self.last_intent_action = None
        self._on_poll_quotes = on_poll_quotes

    def connect_ws(self):
        pass

    def snapshot_rest(self):
        pass

    def poll_quotes(self):
        if self._on_poll_quotes:
            self._on_poll_quotes()
        return True

    def poll_heavy(self):
        pass

    def snapshot(self):
        return Snapshot(
            ts_ms=1, last=self.last, bid=self.bid, ask=self.ask, spread=self.ask - self.bid,
            bars_15m=[Bar(1, self.last, self.last, self.last, self.last)], atr=self.atr,
            free_usdt=self.free_usdt, bot_qty=self.bot_qty, bot_avg_entry=self.bot_avg_entry,
            ws_ok=True, stale=False,
        )


def _no_retry(seconds):
    raise AssertionError(f"loop must not sleep/retry (tried to sleep {seconds}s) -- must halt instead")


# -- boot: a latch already present must halt before the while-loop starts ---

def test_boot_halts_on_exit_latch_present_instead_of_retrying(monkeypatch, tmp_path):
    import bot.cli as cli

    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, _latch())
    client = FakeClient(btc=FOREIGN_BTC + 0.00025, open_ids={"oid-t"})
    settings = _live_settings()
    hands = LiveHands(settings, store, client, sleep=lambda s: None)
    eye = FakeLiveEye()

    monkeypatch.setattr(cli.time, "sleep", _no_retry)

    code = cli._loop(False, settings, client, store, eye, hands)

    assert code == cli.EXIT_EXIT_LATCH_BLOCKED
    assert client.calls == [], "the guard must fence before any exchange read/write"
    assert store.load_position()["qty"] == pytest.approx(0.00025)


def test_boot_halts_on_legacy_closing_row_instead_of_retrying(monkeypatch, tmp_path):
    import bot.cli as cli

    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store, state="CLOSING")  # legacy row, no latch
    client = FakeClient(btc=FOREIGN_BTC + 0.00025, open_ids={"oid-t"})
    settings = _live_settings()
    hands = LiveHands(settings, store, client, sleep=lambda s: None)
    eye = FakeLiveEye()

    monkeypatch.setattr(cli.time, "sleep", _no_retry)

    code = cli._loop(False, settings, client, store, eye, hands)

    assert code == cli.EXIT_EXIT_LATCH_BLOCKED
    assert client.calls == []


def test_boot_halts_on_corrupt_latch_instead_of_retrying(monkeypatch, tmp_path):
    import bot.cli as cli

    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, "{not json")
    client = FakeClient(btc=FOREIGN_BTC + 0.00025, open_ids={"oid-t"})
    settings = _live_settings()
    hands = LiveHands(settings, store, client, sleep=lambda s: None)
    eye = FakeLiveEye()

    monkeypatch.setattr(cli.time, "sleep", _no_retry)

    code = cli._loop(False, settings, client, store, eye, hands)

    assert code == cli.EXIT_EXIT_LATCH_BLOCKED
    assert client.calls == []


def test_boot_halts_on_guard_storage_read_failure_instead_of_retrying(monkeypatch, tmp_path):
    """The review's second finding: a guard read failure (the store raising
    while `_guard_exit` reads the latch) must halt the same way, not escape
    as an untyped RuntimeError that the generic branch would swallow."""
    import bot.cli as cli

    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=FOREIGN_BTC + 0.00025, open_ids={"oid-t"})
    settings = _live_settings()
    hands = LiveHands(settings, store, client, sleep=lambda s: None)
    eye = FakeLiveEye()

    def boom(key, default=None):
        raise RuntimeError("kv read failed")

    store.kv_get = boom

    monkeypatch.setattr(cli.time, "sleep", _no_retry)

    code = cli._loop(False, settings, client, store, eye, hands)

    assert code == cli.EXIT_EXIT_LATCH_BLOCKED
    assert client.calls == []


# -- runtime: a latch appearing mid-loop (foreign/future writer) must halt --

@pytest.mark.parametrize("once", [True, False])
def test_runtime_halts_on_exit_latch_inserted_before_mark_instead_of_retrying(monkeypatch, tmp_path, once):
    """Drives the REAL `_loop -> run_once -> hands.mark()` chain (no mocked
    run_once): boot reconcile succeeds cleanly (no latch yet, stop resident),
    then the very first `poll_quotes()` call inserts a latch -- simulating a
    foreign/future process writing one -- immediately before `mark()` runs."""
    import bot.cli as cli

    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=FOREIGN_BTC + 0.00025, open_ids={"oid-t"})
    settings = _live_settings(tp_atr_mult=3)  # tick_enabled so mark() runs every tick
    hands = LiveHands(settings, store, client, sleep=lambda s: None)

    def inject_latch():
        store.kv_set(EXIT_LATCH_KEY, _latch())

    eye = FakeLiveEye(last=80000.0, bid=79999.0, ask=80001.0, on_poll_quotes=inject_latch)

    monkeypatch.setattr(cli.time, "sleep", _no_retry)

    code = cli._loop(once, settings, client, store, eye, hands)

    assert code == cli.EXIT_EXIT_LATCH_BLOCKED
    assert not [c for c in client.calls if c[0] in ("cancel", "market", "trigger")]
    assert store.load_position()["qty"] == pytest.approx(0.00025)
