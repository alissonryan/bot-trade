"""Fix 1: `TerminalEvidenceUnavailable` (M1's live discretionary-exit refusal --
see bot/hands.py) must HALT the loop with its own exit code, not fall into the
generic 'keep the loop alive, loudly' branch at the bottom of `bot/cli.py::_loop`.

Before this fix: the exception is a plain RuntimeError with no dedicated
handler, so it drops into `except Exception` -- which logs, backs off up to
30s, and retries forever. Retrying an exit that provably cannot succeed (the
condition that triggered it, e.g. a crossed take-profit, does not go away on
its own) is a livelock, not resilience.

Two levels of proof:
  - a direct fake-run_once test mirroring the existing PositionStuck coverage
    in test_cli.py (this is the fast, isolated regression test);
  - two full-stack integration tests that reproduce the exact scenarios from
    the bug report -- a live take-profit crossing and an LLM SELL decision --
    through the real `bot.cycle.run_once` and real `LiveHands`, proving the
    halt happens for both trigger paths and that no retry (no `time.sleep`,
    no repeated write attempt) ever occurs.
"""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.brain import ThinkResult
from bot.hands import LiveHands, TerminalEvidenceUnavailable
from bot.settings import Settings
from bot.store import Store
from bot.types import Bar, Snapshot, TradeIntent

FOREIGN_BTC = 0.00064  # the account already holds BTC the bot does not own


def _live_settings(**kw) -> Settings:
    d = Settings.from_env().__dict__.copy()
    d["mode"] = "live"
    d.update(kw)
    return Settings(**d)


class FakeClient:
    """Scripted exchange: balances/open_orders never change (the stop is still
    resident, the account is untouched) so a livelocked loop would see the
    exact same condition forever -- and any write (market/trigger/cancel)
    proves the refusal was bypassed."""

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
        raise AssertionError("must never POST a market order: TerminalEvidenceUnavailable refuses before any write")

    def place_trigger(self, **kwargs):
        self.calls.append(("trigger", kwargs))
        raise AssertionError("must never place a trigger here")

    def cancel_order(self, order_id: str):
        self.calls.append(("cancel", order_id))
        raise AssertionError("must never DELETE the resident stop: that is exactly the write TerminalEvidenceUnavailable exists to block")

    def my_deals(self, *a, **k):
        self.calls.append(("my_deals",))
        return {"data": []}


class FakeLiveEye:
    """Minimal Eye double: fixed quotes, and `bot_qty`/`bot_avg_entry` read from
    `hands.position` by the caller between calls, exactly like the real Eye/
    cycle wiring in `bot/cli.py::_loop` + `bot/cycle.py::run_once`."""

    def __init__(self, *, last, bid, ask, atr=400.0):
        self.last = last
        self.bid = bid
        self.ask = ask
        self.atr = atr
        self.free_usdt = 450.0
        self.bot_qty = 0.0
        self.bot_avg_entry = None
        self.rules = None
        self.last_bot_pnl_usdt = 0.0
        self.last_intent_action = None
        self.quotes_calls = 0
        self.heavy_calls = 0

    def connect_ws(self):
        pass

    def snapshot_rest(self):
        pass

    def poll_quotes(self):
        self.quotes_calls += 1
        return True

    def poll_heavy(self):
        self.heavy_calls += 1

    def snapshot(self):
        return Snapshot(
            ts_ms=1, last=self.last, bid=self.bid, ask=self.ask, spread=self.ask - self.bid,
            bars_15m=[Bar(1, self.last, self.last, self.last, self.last)], atr=self.atr,
            free_usdt=self.free_usdt, bot_qty=self.bot_qty, bot_avg_entry=self.bot_avg_entry,
            ws_ok=True, stale=False,
        )


def _open_position(store, *, qty=0.00025, take_profit_price=None, opened_ts=None):
    store.remember_order("oid-m1")
    store.remember_order("oid-t")
    store.save_position(
        qty=qty, entry=80000.0, stop_price=79200.0, entry_order_id="oid-m1", stop_order_id="oid-t",
        state="OPEN", entry_source="estimated", btc_before=FOREIGN_BTC,
        take_profit_price=take_profit_price, opened_ts=opened_ts,
    )


# -- direct, fast regression test (mirrors test_cli.py::test_loop_halts_on_a_stuck_position) --

@pytest.mark.parametrize("once", [True, False])
def test_loop_halts_on_terminal_evidence_unavailable_instead_of_retrying(monkeypatch, tmp_path, once):
    import bot.cli as cli

    class FakeEye:
        rules = None

        def connect_ws(self):
            pass

        def snapshot_rest(self):
            pass

    def boom(**kw):
        raise TerminalEvidenceUnavailable("cannot safely start a discretionary SELL")

    monkeypatch.setattr(cli, "run_once", boom)

    def _no_retry(seconds):
        # A correct halt never reaches this. If it does (no dedicated handler,
        # or ordering that lets the generic branch win), fail fast and loudly
        # instead of spinning in a livelocked `while True` with no delay.
        raise AssertionError(f"loop must not sleep/retry (tried to sleep {seconds}s) -- must halt instead")

    monkeypatch.setattr(cli.time, "sleep", _no_retry)

    # mode=paper here: this test is only about `_loop`'s exception dispatch (it
    # replaces `run_once` outright), not live semantics -- paper skips the boot
    # reconcile call that would otherwise need a real `hands.reconcile()`. The
    # two full-stack tests below exercise the real live path end to end.
    settings = _live_settings(mode="paper")
    code = cli._loop(once, settings, None, Store(tmp_path / "c.db", mode="paper"), FakeEye(), object())

    assert code == cli.EXIT_TERMINAL_EVIDENCE_UNAVAILABLE


# -- full-stack: a live take-profit crossing must halt, not loop --

def test_live_cycle_take_profit_crossed_halts_instead_of_looping(monkeypatch, tmp_path):
    import bot.cli as cli

    store = Store(tmp_path / "tp.db", mode="live")
    _open_position(store, take_profit_price=81200.0, opened_ts="2020-01-01T00:00:00+00:00")
    client = FakeClient(btc=FOREIGN_BTC + 0.00025, open_ids={"oid-t"})
    settings = _live_settings(tp_atr_mult=3, fill_confirm_tries=1)
    hands = LiveHands(settings, store, client, sleep=lambda s: None)
    eye = FakeLiveEye(last=81201, bid=81200, ask=81202)

    def _no_retry(seconds):
        raise AssertionError(f"loop must not sleep/retry (tried to sleep {seconds}s) -- must halt instead")

    monkeypatch.setattr(cli.time, "sleep", _no_retry)

    code = cli._loop(False, settings, client, store, eye, hands)

    assert code == cli.EXIT_TERMINAL_EVIDENCE_UNAVAILABLE
    # the resident stop was never touched -- the refusal happened before any write
    assert not [c for c in client.calls if c[0] in ("cancel", "market", "trigger")]
    assert store.load_position()["state"] == "OPEN"


# -- full-stack: an LLM SELL decision must halt, not loop --

def test_live_cycle_llm_sell_decision_halts_instead_of_looping(monkeypatch, tmp_path):
    import bot.cli as cli
    import bot.cycle as cycle

    store = Store(tmp_path / "sell.db", mode="live")
    _open_position(store)  # no TP/TTL target: tp_atr_mult=0, time_limit_minutes=0 below
    client = FakeClient(btc=FOREIGN_BTC + 0.00025, open_ids={"oid-t"})
    settings = _live_settings(tp_atr_mult=0, time_limit_minutes=0)
    hands = LiveHands(settings, store, client, sleep=lambda s: None)
    eye = FakeLiveEye(last=80500, bid=80499, ask=80501)

    def fake_think(snap, settings, budget, **context):
        return ThinkResult(TradeIntent("SELL", 0.9, "take some profit", "range"), "ok")

    monkeypatch.setattr(cycle, "think_result", fake_think)

    def _no_retry(seconds):
        raise AssertionError(f"loop must not sleep/retry (tried to sleep {seconds}s) -- must halt instead")

    monkeypatch.setattr(cli.time, "sleep", _no_retry)

    code = cli._loop(False, settings, client, store, eye, hands)

    assert code == cli.EXIT_TERMINAL_EVIDENCE_UNAVAILABLE
    assert not [c for c in client.calls if c[0] in ("cancel", "market", "trigger")]
    assert store.load_position()["state"] == "OPEN"
