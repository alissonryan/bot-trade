from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.hands import LiveHands, UnprotectedPosition, avg_fill_from_deals
from bot.settings import Settings
from bot.store import Store
from bot.types import Bar, GateResult, Snapshot

FOREIGN_BTC = 0.00064  # the account already holds BTC the bot does not own


def _live_settings(**kw) -> Settings:
    d = Settings.from_env().__dict__.copy()
    d["mode"] = "live"
    d["fill_confirm_tries"] = 3
    d.update(kw)
    return Settings(**d)


def _snap(last=80000.0, bid=79999.0, ask=80001.0):
    return Snapshot(
        ts_ms=1, last=last, bid=bid, ask=ask, spread=ask - bid,
        bars_15m=[Bar(1, last, last, last, last)], atr=400,
        free_usdt=450, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )


def _buy_gate(qty="0.00025"):
    return GateResult(True, "ok_buy", "BUY", qty=qty, notional=20, stop_price="79200.00")


class FakeClient:
    """Scripted exchange. ``btc`` is the sequence of BTC totals returned by balances
    (last value repeats); ``open_ids`` the sequence of open-order id sets."""

    def __init__(self, *, btc, open_ids=None, trigger_fail=0, sell_fail=False, deals=None, on_trigger=None):
        self.btc = list(btc)
        self.open_ids = list(open_ids or [set()])
        self.trigger_fail = trigger_fail
        self.sell_fail = sell_fail
        self.deals = deals
        self.on_trigger = on_trigger
        self.calls = []
        self.n_market = 0

    def _next(self, seq):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def balances(self, currencies="BTC,USDT"):
        self.calls.append(("balances",))
        v = self._next(self.btc)
        return {"data": [{"currency": "BTC", "available": str(v), "frozen": "0", "total": str(v)},
                         {"currency": "USDT", "available": "450", "frozen": "0", "total": "450"}]}

    def open_orders(self, **kw):
        self.calls.append(("open_orders",))
        return {"data": [{"id": i} for i in self._next(self.open_ids)]}

    def place_market(self, **kwargs):
        self.calls.append(("market", kwargs))
        if kwargs["side"] == "SELL" and self.sell_fail:
            raise RuntimeError("sell rejected")
        self.n_market += 1
        return {"code": 0, "data": f"oid-m{self.n_market}"}

    def place_trigger(self, **kwargs):
        self.calls.append(("trigger", kwargs))
        if self.on_trigger:
            self.on_trigger()
        if self.trigger_fail > 0:
            self.trigger_fail -= 1
            raise RuntimeError("trigger down")
        return {"code": 0, "data": "oid-t"}

    def cancel_order(self, order_id: str):
        self.calls.append(("cancel", order_id))
        return {"code": 200}

    def my_deals(self, *a, **k):
        self.calls.append(("my_deals",))
        return self.deals or {"data": []}


def _hands(store, client, **kw):
    return LiveHands(_live_settings(**kw), store, client, sleep=lambda s: None)


def _open_position(store, qty=0.00025, state="OPEN", stop_id="oid-t"):
    store.remember_order("oid-m1")
    if stop_id:
        store.remember_order(stop_id)
    store.save_position(
        qty=qty, entry=80000.0, stop_price=79200.0, entry_order_id="oid-m1", stop_order_id=stop_id,
        state=state, entry_source="estimated", btc_before=FOREIGN_BTC,
    )


def test_live_buy_confirms_fill_by_balance_then_places_trigger(tmp_path):
    store = Store(tmp_path / "l.db")
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.00025])
    hands = _hands(store, client)
    pos = hands.execute(_buy_gate(), _snap())
    kinds = [c[0] for c in client.calls]
    assert kinds[:2] == ["balances", "market"]
    assert "trigger" in kinds
    trig = next(c[1] for c in client.calls if c[0] == "trigger")
    assert trig["market_order"] is True and trig["quantity"] == "0.00025" and trig["trigger_type"] == "LE"
    assert pos.qty == 0.00025 and pos.state == "OPEN"
    assert store.is_bot_order("oid-m1") and store.is_bot_order("oid-t")
    row = store.load_position()
    assert row["state"] == "OPEN" and row["btc_before"] == FOREIGN_BTC and row["stop_order_id"] == "oid-t"
    fills = store.fills(5)
    assert fills[0]["side"] == "BUY" and fills[0]["qty"] == 0.00025 and fills[0]["source"] == "estimated"


def test_live_buy_persists_pending_row_before_stop_attempt(tmp_path):
    store = Store(tmp_path / "l.db")
    seen = []
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.00025], on_trigger=lambda: seen.append(store.load_position()))
    _hands(store, client).execute(_buy_gate(), _snap())
    assert seen and seen[0]["qty"] == 0.00025 and seen[0]["state"] == "PENDING"
    assert store.load_position()["state"] == "OPEN"


def test_live_buy_uses_deals_price_when_available(tmp_path):
    store = Store(tmp_path / "l.db")
    deals = {"data": [{"orderId": "oid-m1", "price": "80020.5", "quantity": "0.00025"}]}
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.00025], deals=deals)
    pos = _hands(store, client).execute(_buy_gate(), _snap())
    assert pos.entry == 80020.5 and pos.entry_source == "deals"
    # stop recomputed on the real fill: 2 x ATR(400) below entry
    assert pos.stop_price == pytest.approx(80020.5 - 800.0)


def test_live_buy_stop_fails_then_flattens(tmp_path):
    store = Store(tmp_path / "l.db")
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.00025, FOREIGN_BTC + 0.00025, FOREIGN_BTC], trigger_fail=2)
    pos = _hands(store, client).execute(_buy_gate(), _snap())
    kinds = [c[0] for c in client.calls]
    assert kinds.count("trigger") == 2
    assert kinds[-1] in ("balances", "my_deals")
    sells = [c for c in client.calls if c[0] == "market" and c[1]["side"] == "SELL"]
    assert len(sells) == 1
    assert pos.qty == 0.0
    assert store.load_position() is None
    assert store.day_pnl(_hands(store, client).today()) <= 0


def test_live_buy_unprotected_when_stop_and_flatten_fail(tmp_path):
    store = Store(tmp_path / "l.db")
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.00025], trigger_fail=2, sell_fail=True)
    with pytest.raises(UnprotectedPosition):
        _hands(store, client).execute(_buy_gate(), _snap())
    row = store.load_position()
    assert row is not None and row["state"] == "UNPROTECTED" and row["qty"] == 0.00025


def test_live_buy_not_filled_cancels_and_stays_flat(tmp_path):
    store = Store(tmp_path / "l.db")
    client = FakeClient(btc=[FOREIGN_BTC], open_ids=[{"oid-m1"}, set()])
    pos = _hands(store, client).execute(_buy_gate(), _snap())
    assert pos.qty == 0.0
    assert ("cancel", "oid-m1") in client.calls
    assert not any(c[0] == "trigger" for c in client.calls)
    assert store.load_position() is None


def test_live_partial_fill_protects_only_what_was_bought(tmp_path):
    store = Store(tmp_path / "l.db")
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.0001])
    pos = _hands(store, client).execute(_buy_gate(), _snap())
    assert pos.qty == pytest.approx(0.0001)
    trig = next(c[1] for c in client.calls if c[0] == "trigger")
    assert trig["quantity"] == "0.00010"


def test_live_sell_cancels_stop_confirms_then_sells(tmp_path):
    store = Store(tmp_path / "l.db")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025, FOREIGN_BTC], open_ids=[set()])
    hands = _hands(store, client)
    gate = GateResult(True, "ok_close", "SELL", qty="0.00025")
    pos = hands.execute(gate, _snap(bid=81000, last=81001, ask=81002))
    kinds = [c[0] for c in client.calls]
    assert kinds.index("cancel") < kinds.index("market")
    assert pos.qty == 0.0
    assert store.load_position() is None
    fill = store.fills(1)[0]
    assert fill["side"] == "SELL" and fill["pnl"] == pytest.approx((81000 - 80000) * 0.00025)


def test_live_sell_failure_restores_stop(tmp_path):
    store = Store(tmp_path / "l.db")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[set()], sell_fail=True)
    hands = _hands(store, client)
    pos = hands.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap())
    assert pos.qty == 0.00025 and pos.state == "OPEN"
    row = store.load_position()
    assert row["stop_order_id"] == "oid-t" and row["state"] == "OPEN"
    assert [c[0] for c in client.calls].count("trigger") == 1


def test_live_sell_aborts_when_stop_cancel_unconfirmed(tmp_path):
    store = Store(tmp_path / "l.db")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    pos = hands.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap())
    assert pos.qty == 0.00025
    assert not any(c[0] == "market" for c in client.calls)
    assert store.load_position()["stop_order_id"] == "oid-t"


def test_live_persists_position_across_restart(tmp_path):
    db = tmp_path / "l.db"
    store = Store(db)
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.00025])
    _hands(store, client).execute(_buy_gate(), _snap())
    again = LiveHands(_live_settings(), Store(db), FakeClient(btc=[FOREIGN_BTC + 0.00025]), sleep=lambda s: None)
    assert again.position.qty == 0.00025
    assert again.stop_order_id == "oid-t"
    assert again.position.btc_before == FOREIGN_BTC


def test_live_never_cancels_foreign_id(tmp_path):
    store = Store(tmp_path / "l.db")
    client = FakeClient(btc=[0.0])
    hands = _hands(store, client)
    foreign = "C02__723550870020620296064"
    assert store.is_bot_order(foreign) is False
    assert hands.cancel_if_ours(foreign) is False
    assert client.calls == []


def test_reconcile_detects_position_closed_on_exchange(tmp_path):
    store = Store(tmp_path / "l.db")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC], open_ids=[set()])  # bot BTC is gone: stop hit
    hands = _hands(store, client)
    assert hands.reconcile() == "closed_on_exchange"
    assert store.load_position() is None
    fill = store.fills(1)[0]
    assert fill["source"] == "reconcile" and fill["price"] == 79200.0
    assert fill["pnl"] == pytest.approx((79200.0 - 80000.0) * 0.00025)


def test_reconcile_restores_missing_stop(tmp_path):
    store = Store(tmp_path / "l.db")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[set()])  # holding, no stop on the book
    hands = _hands(store, client)
    assert hands.reconcile() == "stop_restored"
    assert [c[0] for c in client.calls].count("trigger") == 1
    assert store.load_position()["state"] == "OPEN"


def test_reconcile_ok_when_stop_alive(tmp_path):
    store = Store(tmp_path / "l.db")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    assert _hands(store, client).reconcile() == "ok"


def test_reconcile_raises_when_stop_cannot_be_restored(tmp_path):
    store = Store(tmp_path / "l.db")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[set()], trigger_fail=2)
    with pytest.raises(UnprotectedPosition):
        _hands(store, client).reconcile()
    assert store.load_position()["state"] == "UNPROTECTED"


def test_reconcile_legacy_row_without_btc_before(tmp_path):
    store = Store(tmp_path / "l.db")
    store.remember_order("oid-t")
    store.save_position(qty=0.00025, entry=80000.0, stop_price=79200.0, entry_order_id="oid-m1", stop_order_id="oid-t")
    client = FakeClient(btc=[0.00025], open_ids=[{"oid-t"}])
    assert _hands(store, client).reconcile() == "ok"


def test_avg_fill_from_deals_tolerates_unknown_shape():
    assert avg_fill_from_deals({"data": {"resultList": []}}, "x") is None
    assert avg_fill_from_deals(None, "x") is None
    payload = {"data": {"resultList": [
        {"orderId": "x", "price": "100", "quantity": "1"},
        {"orderId": "x", "price": "110", "quantity": "1"},
        {"orderId": "y", "price": "999", "quantity": "5"},
    ]}}
    assert avg_fill_from_deals(payload, "x") == (105.0, 2.0)


# --- review findings: reconcile must not lie about what the exchange holds ---


def _open_hands(tmp_path, client, *, qty=0.00025, entry=80000.0, stop=79200.0,
                btc_before=FOREIGN_BTC, stop_id="oid-t", state="OPEN"):
    """A LiveHands whose local row already describes an open, stopped position."""
    store = Store(tmp_path / "l.db")
    store.remember_order(stop_id)
    store.save_position(
        qty=qty, entry=entry, stop_price=stop, entry_order_id="oid-m1",
        stop_order_id=stop_id, state=state, entry_source="deals", btc_before=btc_before,
    )
    return LiveHands(_live_settings(), store, client, sleep=lambda s: None)


def test_reconcile_flat_still_queries_the_exchange(tmp_path):
    """Finding 1: the entry POST is never retried and is not wrapped, so a timeout
    (order accepted, response lost) raises before the row is persisted. reconcile()
    returned "flat" from the empty local row without ever asking the exchange, so
    the orphan BTC stayed invisible and the next BUY stacked a second long."""
    store = Store(tmp_path / "l.db")
    client = FakeClient(btc=[FOREIGN_BTC])
    hands = LiveHands(_live_settings(), store, client, sleep=lambda s: None)

    assert hands.reconcile() == "flat"  # seeds the foreign baseline
    assert ("balances",) in client.calls  # it actually asked

    client.btc = [FOREIGN_BTC + 0.00025]  # an entry filled that we never recorded
    with pytest.raises(UnprotectedPosition):
        hands.reconcile()


def test_reconcile_ignores_foreign_btc_leaving(tmp_path):
    """Finding 4: `holding` compared the whole-account total against btc_before,
    so the user's own 0.00064 stop firing read as the bot's position closing --
    booking a phantom exit and orphaning the bot's real long and its live stop."""
    client = FakeClient(btc=[0.00025], open_ids=[{"oid-t"}])  # our stop is STILL open
    hands = _open_hands(tmp_path, client)

    verdict = hands.reconcile()

    assert verdict == "ok"
    assert hands.position.qty == 0.00025  # row intact
    assert hands.stop_order_id == "oid-t"
    assert hands.store.day_pnl(hands.today()) == 0.0  # no phantom SELL booked


def test_reconcile_books_the_exit_when_our_own_qty_is_what_left(tmp_path):
    """The legitimate case must still work: our stop filled, our qty is gone,
    the foreign BTC is untouched."""
    client = FakeClient(btc=[FOREIGN_BTC], open_ids=[set()])  # stop gone, our qty gone
    hands = _open_hands(tmp_path, client)

    assert hands.reconcile() == "closed_on_exchange"
    assert hands.position.qty == 0.0


# --- review findings: never delete the row on ambiguous evidence ---


class BalanceBlindClient(FakeClient):
    """Answers the pre-trade balance read, then the balances endpoint goes down --
    the 429/5xx case that must not be read as 'nothing filled'."""

    def __init__(self, *, ok_reads=1, **kw):
        super().__init__(**kw)
        self.reads = 0
        self.ok_reads = ok_reads

    def balances(self, currencies="BTC,USDT"):
        self.reads += 1
        if self.reads > self.ok_reads:
            raise ConnectionError("429 slow down")
        return super().balances(currencies)


def test_buy_keeps_the_row_when_the_balance_endpoint_is_unreadable(tmp_path):
    """Finding 3: _watch_balance swallowed every exception and left `seen` at 0.0,
    so a balances outage was indistinguishable from 'nothing filled' and _buy
    deleted the row for an order that may well have filled."""
    store = Store(tmp_path / "l.db")
    client = BalanceBlindClient(btc=[FOREIGN_BTC], open_ids=[set()])
    hands = LiveHands(_live_settings(), store, client, sleep=lambda s: None)

    hands.execute(_buy_gate(), _snap())

    assert store.load_position() is not None, "row deleted while the fill was unknown"
    assert hands.position.state == "PENDING"


def test_buy_keeps_the_row_when_the_fill_is_merely_unconfirmed(tmp_path):
    """Finding 2: an instant fill with the balance endpoint lagging past
    tries*wait reads as filled == 0, and the entry is no longer in open_orders
    *because it filled*. The old code fell through to _clear() and the bot went
    'flat' while holding real BTC with no stop."""
    store = Store(tmp_path / "l.db")
    client = FakeClient(btc=[FOREIGN_BTC], open_ids=[set()])  # never appears as open
    hands = LiveHands(_live_settings(), store, client, sleep=lambda s: None)

    hands.execute(_buy_gate(), _snap())

    assert store.load_position() is not None, "row deleted on ambiguous evidence"
    assert hands.position.state == "PENDING"


def test_reconcile_clears_a_pending_entry_without_booking_a_fill(tmp_path):
    """The other half of findings 2/3: a PENDING row whose entry never filled must
    be dropped by reconcile *without* inventing a SELL fill and a PnL for it."""
    client = FakeClient(btc=[FOREIGN_BTC], open_ids=[set()])
    hands = _open_hands(tmp_path, client, state="PENDING", stop_id="none")
    hands.stop_order_id = None

    assert hands.reconcile() == "entry_never_filled"
    assert hands.position.qty == 0.0
    assert hands.store.day_pnl(hands.today()) == 0.0


# --- review findings: the exit path ---


def test_sell_escalates_when_the_stop_is_not_ours_to_cancel(tmp_path):
    """Finding 6: cancel_if_ours() returns False when the id is not in bot_orders,
    so no cancel is sent, the confirm loop always sees the stop still open, and
    _sell returned unchanged. Every later SELL repeated it -- the position could
    never be exited through the normal path, marked by one log line."""
    from bot.hands import PositionStuck

    store = Store(tmp_path / "l.db")
    store.save_position(  # note: the stop id is NOT remembered as a bot order
        qty=0.00025, entry=80000.0, stop_price=79200.0, entry_order_id="oid-m1",
        stop_order_id="oid-t", state="OPEN", entry_source="deals", btc_before=FOREIGN_BTC,
    )
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = LiveHands(_live_settings(), store, client, sleep=lambda s: None)

    with pytest.raises(PositionStuck):
        hands.execute(GateResult(True, "ok_sell", "SELL", qty="0.00025", notional=20), _snap())


def test_sell_failure_rechecks_the_balance_before_restoring_the_stop(tmp_path):
    """Finding 5: POSTs are never retried, so a Timeout surfaces as an error while
    the sell may actually have executed. Re-placing a stop for BTC we no longer
    own parks a bot trigger on top of the owner's own coins."""
    store = Store(tmp_path / "l.db")
    store.remember_order("oid-t")
    store.save_position(
        qty=0.00025, entry=80000.0, stop_price=79200.0, entry_order_id="oid-m1",
        stop_order_id="oid-t", state="OPEN", entry_source="deals", btc_before=FOREIGN_BTC,
    )
    # start read shows our BTC present; every read after the failed POST shows it gone
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025, FOREIGN_BTC], open_ids=[set()], sell_fail=True)
    hands = LiveHands(_live_settings(), store, client, sleep=lambda s: None)

    hands.execute(GateResult(True, "ok_sell", "SELL", qty="0.00025", notional=20), _snap())

    assert not any(c[0] == "trigger" for c in client.calls), "stop restored over the owner's BTC"
    assert hands.position.qty == 0.0


def test_sell_survives_a_blind_balance_endpoint(tmp_path):
    """_watch_balance now reports 'no reading' as None; the exit path must treat
    that as unconfirmed (reconcile settles it), not crash the live loop."""
    store = Store(tmp_path / "l.db")
    store.remember_order("oid-t")
    store.save_position(
        qty=0.00025, entry=80000.0, stop_price=79200.0, entry_order_id="oid-m1",
        stop_order_id="oid-t", state="OPEN", entry_source="deals", btc_before=FOREIGN_BTC,
    )
    client = BalanceBlindClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[set()])
    hands = LiveHands(_live_settings(), store, client, sleep=lambda s: None)

    hands.execute(GateResult(True, "ok_sell", "SELL", qty="0.00025", notional=20), _snap())

    assert hands.position.state == "CLOSING"
