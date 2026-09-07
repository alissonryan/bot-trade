"""L6: every settlement site that writes a fill and then updates or clears the
position must do so in ONE local transaction, not two-or-more auto-committed
Store calls. ``LiveHands._settle_closed_on_exchange`` (see test_exit_latch.py,
"item 6") is the reference implementation this mirrors: ``commit=False`` on
``add_fill``/``save_position``/``kv_set``/``clear_position``, one trailing
``store.commit()``, ``store.rollback()`` on any exception, and in-memory state
synced from the committed row BEFORE any further fallible work.

These tests reuse the SAME Store/connection across the failure and the
assertion or retry that follows -- closing and reopening a fresh connection
would only prove SQLite's own crash recovery, not that this code rolls back
an in-process failure on a connection the CLI keeps reusing after backoff
(see AGENTS.md and test_exit_latch.py's own note on this exact mistake).
"""
import pytest

from bot.hands import LiveHands, STOP_SUBMISSION_KEY, UnprotectedPosition
from bot.store import Store
from bot.types import GateResult
from test_hands_live import FOREIGN_BTC, FakeClient, _buy_gate, _hands, _open_position, _snap


# --- site: LiveHands._buy, confirmed-fill booking (qty/entry/stop update + add_fill) --


def test_live_buy_confirmed_fill_and_position_update_is_one_transaction(tmp_path):
    """After the fill is confirmed by balance, the position is updated to the
    real qty/price/stop AND the BUY fill is recorded. If the FILL write fails,
    the position must not keep the confirmed (deals) price/qty either --
    otherwise the row shows a confirmed entry the ledger never recorded."""
    store = Store(tmp_path / "l.db", mode="live")
    deals = {"data": [{"orderId": "oid-m1", "price": "80020.5", "quantity": "0.00025"}]}
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.00025], deals=deals)
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-fill-booking")

    store.add_fill = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands.execute(_buy_gate(), _snap())

    assert store.fills() == [], "no fill was ever durably recorded"
    row = store.load_position()
    assert row is not None and row["state"] == "PENDING"
    assert row["entry_source"] == "estimated", "must not show the confirmed (deals) price without the matching committed fill"
    assert row["qty"] == pytest.approx(0.00025)  # the invariant-1 estimate, not silently changed


def test_live_buy_confirmed_fill_failure_leaves_no_open_transaction(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    deals = {"data": [{"orderId": "oid-m1", "price": "80020.5", "quantity": "0.00025"}]}
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.00025], deals=deals)
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-fill-booking")

    store.add_fill = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands.execute(_buy_gate(), _snap())

    assert store._conn.in_transaction is False
    store.kv_set("unrelated", "x")  # the CLI's next incidental write on the same connection
    assert store.fills() == [], "the orphan situation must not have been swept in by an unrelated later commit"
    assert store.load_position()["entry_source"] == "estimated"


def test_live_buy_confirmed_fill_retry_is_a_safe_no_op_never_double_booking(tmp_path):
    """_buy() refuses to re-enter once a position is open (``is_open()`` guard),
    so a same-object retry after this failure must send no second order and
    must never book the fill twice -- it stays a safe no-op, not a duplicate."""
    store = Store(tmp_path / "l.db", mode="live")
    deals = {"data": [{"orderId": "oid-m1", "price": "80020.5", "quantity": "0.00025"}]}
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.00025], deals=deals)
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-fill-booking")

    store.add_fill = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands.execute(_buy_gate(), _snap())
    n_market_calls = len([c for c in client.calls if c[0] == "market"])

    pos = hands.execute(_buy_gate(), _snap())  # retry, same Hands object
    assert pos.qty == pytest.approx(0.00025)
    assert len([c for c in client.calls if c[0] == "market"]) == n_market_calls, "must not place a second entry order"
    assert store.fills() == [], "must never book the BUY fill twice -- staying un-booked beats double-booking"


# --- site: LiveHands._flatten (stop failed after entry fill; sell to flatten) --


def test_live_flatten_fill_and_clear_roll_back_together(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.00025, FOREIGN_BTC + 0.00025, FOREIGN_BTC], trigger_fail=1)
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-flatten")

    store.clear_position = boom
    # _flatten() preserves its own "always returns bool, never raises" contract
    # (see report): a transactional failure here is caught, logged, and
    # returns False, which drives _buy()'s existing "stop and flatten both
    # failed" halt -- the same loud UnprotectedPosition raise as before, not a
    # raw DB exception escaping in its place.
    with pytest.raises(UnprotectedPosition):
        hands.execute(_buy_gate(), _snap())
    fills = store.fills()
    assert len(fills) == 1 and fills[0]["side"] == "BUY", "orphan flatten SELL fill with the position row never cleared"
    row = store.load_position()
    assert row is not None and row["state"] == "UNPROTECTED" and row["qty"] == pytest.approx(0.00025)


def test_live_flatten_failure_leaves_no_open_transaction(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.00025, FOREIGN_BTC + 0.00025, FOREIGN_BTC], trigger_fail=1)
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-flatten")

    store.clear_position = boom
    with pytest.raises(UnprotectedPosition):
        hands.execute(_buy_gate(), _snap())

    assert store._conn.in_transaction is False
    store.kv_set("unrelated", "x")
    fills = store.fills()
    assert len(fills) == 1 and fills[0]["side"] == "BUY", "orphan fill must not be swept in by an unrelated later commit"
    assert store.load_position()["qty"] == pytest.approx(0.00025)


def test_live_flatten_retry_stays_fenced_until_a_human_clears_the_stop_latch(tmp_path):
    """A rejected stop submission leaves STOP_SUBMISSION_KEY set on purpose
    (``_place_stop``'s "a failed fallback stays halted" comment); _buy() only
    clears it once ``_flatten`` reports success. Here flatten's own local
    transaction fails, so the latch is NEVER cleared -- reconcile() must keep
    refusing (fail closed) even once storage "recovers", not silently
    self-heal past a submission a human has not reviewed. Only after that
    latch is cleared by hand does the exit get booked, and then exactly once.
    """
    store = Store(tmp_path / "l.db", mode="live")
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + 0.00025, FOREIGN_BTC + 0.00025, FOREIGN_BTC], trigger_fail=1)
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-flatten")

    store.clear_position = boom
    with pytest.raises(UnprotectedPosition):
        hands.execute(_buy_gate(), _snap())
    assert len(store.fills()) == 1  # only the BUY; no orphan SELL
    assert store.kv_get(STOP_SUBMISSION_KEY), "the submission latch must still be set"

    store.clear_position = Store.clear_position.__get__(store)  # storage "recovers"
    fenced = LiveHands(hands.settings, store, FakeClient(btc=[FOREIGN_BTC]), sleep=lambda s: None)
    with pytest.raises(UnprotectedPosition, match="unfinished stop submission"):
        fenced.reconcile()  # still fenced: a human has not reviewed the latch
    assert len(store.fills()) == 1

    store.kv_set(STOP_SUBMISSION_KEY, "")  # the human resolves it by hand
    fresh = LiveHands(hands.settings, store, FakeClient(btc=[FOREIGN_BTC]), sleep=lambda s: None)
    verdict = fresh.reconcile()
    assert verdict == "closed_on_exchange"
    fills = store.fills()
    assert len(fills) == 2 and fills[0]["side"] == "SELL", "must book the flatten exit exactly once, not zero or two"
    assert store.load_position() is None


# --- site: LiveHands._sell, the "sell had in fact executed" recovery branch --


def test_live_sell_recovered_fill_and_clear_roll_back_together(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    full = FOREIGN_BTC + 0.00025
    client = FakeClient(btc=[full, FOREIGN_BTC], open_ids=[set()], sell_fail=True)
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-recovered-sell")

    store.clear_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands._sell(_snap(), exit_reason=None)

    assert store.fills() == [], "orphan recovered-sell fill with the position row never cleared"
    assert store.load_position() is not None
    assert hands.position.is_open(), "in-memory must not diverge from the rolled-back store"


def test_live_sell_recovered_fill_failure_leaves_no_open_transaction(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    full = FOREIGN_BTC + 0.00025
    client = FakeClient(btc=[full, FOREIGN_BTC], open_ids=[set()], sell_fail=True)
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-recovered-sell")

    store.clear_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands._sell(_snap(), exit_reason=None)

    assert store._conn.in_transaction is False
    store.kv_set("unrelated", "x")
    assert store.fills() == []


def test_live_sell_recovered_fill_retry_via_fresh_reconcile_books_exactly_once(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    full = FOREIGN_BTC + 0.00025
    client = FakeClient(btc=[full, FOREIGN_BTC], open_ids=[set()], sell_fail=True)
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-recovered-sell")

    store.clear_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands._sell(_snap(), exit_reason=None)
    assert store.fills() == []

    store.clear_position = Store.clear_position.__get__(store)
    fresh = LiveHands(hands.settings, store, FakeClient(btc=[FOREIGN_BTC], open_ids=[set()]), sleep=lambda s: None)
    assert fresh.reconcile() == "closed_on_exchange"
    assert len(store.fills()) == 1
    assert store.load_position() is None


# --- site: LiveHands._sell, partial-fill booking after a failed sell POST ---
#
# The sell POST is never retried; a lost response can hide a real PARTIAL
# fill. Booking that partial and shrinking the tracked qty to the remainder
# must be one transaction. In the unfixed code, ``add_fill`` for the partial
# amount commits immediately, and the qty reduction is only durably written
# later, inside ``_place_stop``'s own ``self._persist()`` call -- so the
# reproduction has to let the two earlier (unrelated) position writes in
# ``_sell`` through (the pre-sell "OPEN" persist and the post-cancel
# "UNPROTECTED" persist) and fail exactly the THIRD ``save_position`` call,
# which is the one that would durably record the reduced qty.


def _boom_on_third_save_position(store):
    calls = {"n": 0}
    real_save_position = store.save_position

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk full mid-partial-sell")
        return real_save_position(*a, **kw)

    store.save_position = flaky
    return calls


def test_live_sell_partial_fill_and_qty_reduction_roll_back_together(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    start = FOREIGN_BTC + 0.00025
    after = FOREIGN_BTC + 0.00015  # 0.0001 actually sold before the response was lost
    client = FakeClient(btc=[start, after], open_ids=[set()], sell_fail=True)
    hands = _hands(store, client)

    _boom_on_third_save_position(store)
    with pytest.raises(RuntimeError, match="disk full"):
        hands._sell(_snap(), exit_reason=None)

    assert store.fills() == [], "orphan partial fill with the qty reduction never committed"
    row = store.load_position()
    assert row is not None and row["qty"] == pytest.approx(0.00025), "qty must not shrink without the matching booked fill"
    assert hands.position.qty == pytest.approx(0.00025), "in-memory must not diverge from the rolled-back store"


def test_live_sell_partial_fill_failure_leaves_no_open_transaction(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    start = FOREIGN_BTC + 0.00025
    after = FOREIGN_BTC + 0.00015
    client = FakeClient(btc=[start, after], open_ids=[set()], sell_fail=True)
    hands = _hands(store, client)

    _boom_on_third_save_position(store)
    with pytest.raises(RuntimeError, match="disk full"):
        hands._sell(_snap(), exit_reason=None)

    assert store._conn.in_transaction is False
    store.kv_set("unrelated", "x")
    assert store.fills() == []
    assert store.load_position()["qty"] == pytest.approx(0.00025)


def test_live_sell_partial_fill_retry_books_exactly_once(tmp_path):
    """A retried `_sell()` call re-reads the balance from scratch (`start`),
    so it must book the SAME partial amount exactly once, not stack a second
    partial fill on top of the first rolled-back attempt."""
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    start = FOREIGN_BTC + 0.00025
    after = FOREIGN_BTC + 0.00015
    client = FakeClient(btc=[start, after], open_ids=[set()], sell_fail=True)
    hands = _hands(store, client)

    _boom_on_third_save_position(store)
    with pytest.raises(RuntimeError, match="disk full"):
        hands._sell(_snap(), exit_reason=None)
    assert store.fills() == []

    # Retry: a fresh Hands object reloading the still-full-qty row, exactly
    # what the CLI's next cycle would construct; the exchange numbers repeat
    # (same partial-sell script) because nothing was actually retried on the
    # exchange side -- only the local commit is being retried here.
    client2 = FakeClient(btc=[start, after], open_ids=[set()], sell_fail=True)
    retry = LiveHands(hands.settings, store, client2, sleep=lambda s: None)
    retry._sell(_snap(), exit_reason=None)

    fills = store.fills()
    assert len(fills) == 1, "retry must book exactly one partial fill, not zero or two"
    assert fills[0]["qty"] == pytest.approx(0.0001)
    assert store.load_position()["qty"] == pytest.approx(0.00015)


# --- site: LiveHands._sell, the ordinary confirmed-sell settlement ---


def test_live_sell_settlement_fill_and_clear_roll_back_together(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025, FOREIGN_BTC], open_ids=[set()])
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-sell-settlement")

    store.clear_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands._sell(_snap(bid=81000, last=81001, ask=81002), exit_reason=None)

    assert store.fills() == [], "orphan SELL fill with the position row never cleared"
    assert store.load_position() is not None
    assert hands.position.is_open()


def test_live_sell_settlement_failure_leaves_no_open_transaction(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025, FOREIGN_BTC], open_ids=[set()])
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-sell-settlement")

    store.clear_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands._sell(_snap(bid=81000, last=81001, ask=81002), exit_reason=None)

    assert store._conn.in_transaction is False
    store.kv_set("unrelated", "x")
    assert store.fills() == []


def test_live_sell_settlement_retry_via_fresh_reconcile_books_exactly_once(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025, FOREIGN_BTC], open_ids=[set()])
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-sell-settlement")

    store.clear_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands._sell(_snap(bid=81000, last=81001, ask=81002), exit_reason=None)
    assert store.fills() == []

    store.clear_position = Store.clear_position.__get__(store)
    fresh = LiveHands(hands.settings, store, FakeClient(btc=[FOREIGN_BTC], open_ids=[set()]), sleep=lambda s: None)
    assert fresh.reconcile() == "closed_on_exchange"
    assert len(store.fills()) == 1
    assert store.load_position() is None
