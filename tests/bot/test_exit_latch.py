"""M1 fail-closed hardening: durable EXIT latch, fencing, and the prerequisite
gate that refuses to start a live discretionary exit.

These tests deliberately do NOT try to prove that a balance delta can tell a
cancelled stop apart from an executed one -- the 2026-09 adversarial design
review (/private/tmp/claude-501/omp-m1-design-review.md) demonstrated
indistinguishable counterexamples for that (owner deposit/withdrawal exactly
offsetting the bot's own quantity). Every scenario here proves the OPPOSITE:
once an EXIT latch is present (or a legacy CLOSING row with no latch), the
observed balance numbers are irrelevant to the outcome -- fencing refuses
unconditionally, without even inspecting them. That is the fail-closed
answer in the absence of captured terminal evidence (order-history/deals
payload shapes), and it is the only thing this module claims.

All exchange effects are synthetic (FakeClient) and storage is isolated
SQLite in tmp_path. No live orders, no claim about real KCEX behavior.
"""
import json

import pytest

from bot.hands import (
    EXIT_LATCH_KEY,
    ExitLatchBlocked,
    LiveHands,
    PositionStuck,
    STOP_REPLACE_KEY,
    STOP_SUBMISSION_KEY,
    TerminalEvidenceUnavailable,
    UnprotectedPosition,
)
from bot.store import Store
from bot.types import GateResult, Snapshot
from test_hands_live import FOREIGN_BTC, FakeClient, _barrier_position, _buy_gate, _hands, _live_settings, _open_position, _snap


def _latch(phase="cancel_pending", **kw):
    base = {
        "schema": 1,
        "operation_id": "op-1",
        "entry_id": "oid-m1",
        "stop_id": "oid-t",
        "qty_original": 0.00025,
        "qty_booked": 0.0,
        "qty_residual": 0.00025,
        "baselines": [{"total": FOREIGN_BTC, "ts_ms": 1000}],
        "exit_reason": "ok_close",
        "phase": phase,
        "sell_order_id": None,
    }
    base.update(kw)
    return json.dumps(base)


# --- item 2: the prerequisite gate refuses to start, unconditionally ---------


def test_llm_sell_refuses_before_any_write(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(TerminalEvidenceUnavailable):
        hands.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap())
    assert client.calls == []
    assert store.load_position()["qty"] == pytest.approx(0.00025)
    assert store.load_position()["state"] == "OPEN"
    assert store.fills() == []


def test_local_take_profit_refuses_before_any_write(tmp_path):
    store = Store(tmp_path / "tp.db", mode="live")
    _barrier_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client, tp_atr_mult=3)
    with pytest.raises(TerminalEvidenceUnavailable):
        hands.mark(_snap(bid=81200, last=81201))
    assert not [c for c in client.calls if c[0] in ("cancel", "market", "trigger")]
    assert hands.position.is_open()
    assert store.fills() == []


def test_time_limit_refuses_before_any_write(tmp_path, monkeypatch):
    store = Store(tmp_path / "ttl.db", mode="live")
    _barrier_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client, time_limit_minutes=1)
    monkeypatch.setattr("bot.hands.time.time", lambda: 1_900_000_000)
    with pytest.raises(TerminalEvidenceUnavailable):
        hands.mark(_snap())
    assert not [c for c in client.calls if c[0] in ("cancel", "market", "trigger")]
    assert hands.position.is_open()


# --- item 3 (re-review blocker): the barrier itself must be zero-write, even
# when the resident stop is ABSENT from the book. `test_local_take_profit_
# refuses_before_any_write` above keeps the stop present in `open_ids`, which
# is exactly the setup that never reaches `reconcile()`'s stop-restoration
# write (`_place_stop` -> a real `place_trigger` POST). A stop can be absent
# from the book because it executed OR because it was cancelled by someone
# else -- indistinguishable -- and the balance can still read as fully
# holding (stale, or an owner deposit that happens to offset). `mark()` must
# not restore a stop as a side effect of evaluating a barrier it is about to
# refuse anyway.


@pytest.mark.parametrize("total", [
    FOREIGN_BTC + 0.00025,             # stable: reads as still fully holding
    FOREIGN_BTC + 0.00025 + 0.00003,   # owner deposit on top: still reads as holding
    FOREIGN_BTC + 0.00010,             # some OTHER amount moved (not our exact size):
                                       # reconcile's "else" branch -- still treated as
                                       # holding and re-baselined, never settled as closed
], ids=["stable_balance", "owner_deposit_on_top", "owner_partial_offset_other_amount_moved"])
def test_local_take_profit_refuses_before_any_write_with_stop_absent_from_book(tmp_path, total):
    store = Store(tmp_path / "tp.db", mode="live")
    _barrier_position(store)  # local row still has stop_order_id="oid-t"
    client = FakeClient(btc=[total], open_ids=[set()])  # but it is gone from the exchange book
    hands = _hands(store, client, tp_atr_mult=3)
    with pytest.raises(TerminalEvidenceUnavailable):
        hands.mark(_snap(bid=81200, last=81201))
    assert not [c for c in client.calls if c[0] in ("cancel", "market", "trigger")], (
        "reconcile's stop-restoration write must not fire while evaluating a "
        "barrier that is about to be refused"
    )
    assert hands.position.is_open()
    assert store.fills() == []


def test_time_limit_refuses_before_any_write_with_stop_absent_from_book(tmp_path, monkeypatch):
    store = Store(tmp_path / "ttl.db", mode="live")
    _barrier_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[set()])
    hands = _hands(store, client, time_limit_minutes=1)
    monkeypatch.setattr("bot.hands.time.time", lambda: 1_900_000_000)
    with pytest.raises(TerminalEvidenceUnavailable):
        hands.mark(_snap())
    assert not [c for c in client.calls if c[0] in ("cancel", "market", "trigger")]
    assert hands.position.is_open()
    assert store.fills() == []


def test_local_take_profit_settles_quietly_when_stop_already_executed_without_restoring(tmp_path):
    """The other side of the same fix: when the position really has closed on
    the exchange (our whole quantity is missing, not just the stop id from the
    open-orders list), the barrier's read-only observation must still detect
    and book that exit -- it must not manufacture a second local exit, and it
    must not need any order-side write to notice it."""
    store = Store(tmp_path / "tp.db", mode="live")
    _barrier_position(store)
    client = FakeClient(btc=[FOREIGN_BTC], open_ids=[set()])  # our qty is gone: stop truly fired
    hands = _hands(store, client, tp_atr_mult=3)
    result = hands.mark(_snap(bid=81200, last=81201))
    assert not result.is_open()
    assert not [c for c in client.calls if c[0] in ("cancel", "market", "trigger")]
    assert len(store.fills()) == 1
    assert hands.last_mark_reason == "reconcile"


def test_prerequisite_gate_has_no_bypass_setting(tmp_path):
    """Settings is a frozen dataclass with a fixed field set (bot/settings.py)
    -- there is no flag on it (env or otherwise) that could disable this
    guard. Constructing hands with the plain default settings and calling
    SELL must still refuse."""
    import dataclasses

    from bot.settings import Settings

    assert not any("unsafe" in f.name or "bypass" in f.name or "skip_evidence" in f.name
                  for f in dataclasses.fields(Settings)), "no bypass field may ever exist"
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(TerminalEvidenceUnavailable):
        hands.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap())
    assert client.calls == []


def test_paper_hands_unaffected_by_prerequisite_gate(tmp_path):
    """PaperHands places no real orders; it must sell exactly as before."""
    from bot.hands import PaperHands
    from bot.settings import Settings

    store = Store(tmp_path / "paper.db", mode="paper")
    hands = PaperHands(Settings.from_env(), store)
    snap = Snapshot(ts_ms=1, last=100.0, bid=100.0, ask=100.0, spread=0.0, bars_15m=[],
                    atr=1.0, free_usdt=1000.0, bot_qty=0.0, bot_avg_entry=None, ws_ok=True, stale=False)
    hands.execute(GateResult(True, "ok_buy", "BUY", qty="1", stop_price="50"), snap)
    assert hands.position.is_open()
    hands.execute(GateResult(True, "ok_close", "SELL"), snap)
    assert not hands.position.is_open()


# --- item 3: fencing -- legacy CLOSING with no latch fails closed -----------


def _closing_row(store, *, stop_id="oid-t", open_ids=None):
    _open_position(store, stop_id=stop_id)
    store.save_position(qty=0.00025, entry=80000.0, stop_price=79200.0, entry_order_id="oid-m1",
                        stop_order_id=stop_id, state="CLOSING", entry_source="estimated",
                        btc_before=FOREIGN_BTC)


@pytest.mark.parametrize("open_ids", [[set()], [{"oid-t"}]], ids=["stop_gone", "stop_still_open"])
def test_legacy_closing_without_latch_fails_closed_on_every_entry_point(tmp_path, open_ids):
    store = Store(tmp_path / "l.db", mode="live")
    _closing_row(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=open_ids)
    hands = _hands(store, client)

    with pytest.raises(ExitLatchBlocked):
        hands.reconcile()
    assert client.calls == []

    with pytest.raises(ExitLatchBlocked):
        hands.mark(_snap())
    assert client.calls == []

    with pytest.raises(ExitLatchBlocked):
        hands.execute(_buy_gate(), _snap())
    assert client.calls == []

    with pytest.raises(ExitLatchBlocked):
        hands.replace_stop(79300, _snap())
    assert client.calls == []


# --- item 1/3: an EXIT latch fences every write path ------------------------


@pytest.mark.parametrize("phase", [
    "cancel_submitting", "cancel_pending", "ready_to_sell", "sell_submitting",
    "sell_pending", "settlement_pending", "settled", "manual_review",
])
def test_exit_latch_at_every_phase_blocks_every_entry_point(tmp_path, phase):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, _latch(phase=phase))
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)

    with pytest.raises(ExitLatchBlocked):
        hands.execute(_buy_gate(), _snap())
    with pytest.raises(ExitLatchBlocked):
        hands.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap())
    with pytest.raises(ExitLatchBlocked):
        hands.mark(_snap())
    with pytest.raises(ExitLatchBlocked):
        hands.reconcile()
    with pytest.raises(ExitLatchBlocked):
        hands.replace_stop(79300, _snap())
    with pytest.raises(ExitLatchBlocked):
        hands._flatten("0.00025", _snap())
    with pytest.raises(ExitLatchBlocked):
        hands._place_stop("0.00025", 79200)

    assert client.calls == [], "a fenced latch must never let a client call through"
    row = store.load_position()
    assert row["qty"] == pytest.approx(0.00025)
    assert store.fills() == []


def test_exit_latch_coexisting_with_stop_submission_halts_never_picks_one(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, _latch())
    store.kv_set(STOP_SUBMISSION_KEY, json.dumps({"phase": "submitting"}))
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(ExitLatchBlocked):
        hands.reconcile()
    assert client.calls == []
    assert store.kv_get(EXIT_LATCH_KEY)
    assert store.kv_get(STOP_SUBMISSION_KEY)


def test_exit_latch_coexisting_with_stop_replace_halts_never_picks_one(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, _latch())
    store.kv_set(STOP_REPLACE_KEY, json.dumps({"phase": "cancel"}))
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(ExitLatchBlocked):
        hands.execute(_buy_gate(), _snap())
    assert client.calls == []
    assert store.kv_get(EXIT_LATCH_KEY)
    assert store.kv_get(STOP_REPLACE_KEY)


# --- item 5: corrupt / mismatched latch -> halt, never clear and continue --


def test_corrupt_latch_json_halts(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, "{not json")
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(ExitLatchBlocked):
        hands.reconcile()
    assert client.calls == []
    assert store.kv_get(EXIT_LATCH_KEY) == "{not json"  # never cleared


def test_latch_as_a_json_list_not_a_dict_halts(tmp_path):
    """'Multiple latches' shaped as a list instead of the one-record schema."""
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, json.dumps([json.loads(_latch()), json.loads(_latch(phase="settled"))]))
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(ExitLatchBlocked):
        hands.reconcile()
    assert client.calls == []


def test_latch_unknown_schema_halts(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, _latch(schema=99))
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(ExitLatchBlocked):
        hands.reconcile()
    assert client.calls == []


def test_latch_missing_field_halts(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    data = json.loads(_latch())
    del data["operation_id"]
    store.kv_set(EXIT_LATCH_KEY, json.dumps(data))
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(ExitLatchBlocked):
        hands.reconcile()
    assert client.calls == []


def test_latch_disagreeing_with_position_row_halts(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)  # entry_order_id == "oid-m1"
    store.kv_set(EXIT_LATCH_KEY, _latch(entry_id="some-other-entry"))
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(ExitLatchBlocked):
        hands.reconcile()
    assert client.calls == []


# --- required scenarios 1-5: balance ambiguity is irrelevant once latched ---


@pytest.mark.parametrize("baselines", [
    # stop executed + owner deposit of the same qty -> delta 0
    [{"total": FOREIGN_BTC + 0.00025, "ts_ms": 1}, {"total": FOREIGN_BTC + 0.00025, "ts_ms": 2}],
    # stop cancelled + owner withdrawal of the same qty -> delta == qty
    [{"total": FOREIGN_BTC + 0.00025, "ts_ms": 1}, {"total": FOREIGN_BTC, "ts_ms": 2}],
    # stop executed before the very first (baseline) read: both readings post-execution
    [{"total": FOREIGN_BTC, "ts_ms": 1}, {"total": FOREIGN_BTC, "ts_ms": 2}],
], ids=["delta_zero_compensated", "delta_qty_compensated", "executed_before_baseline"])
def test_balance_ambiguity_never_authorizes_a_sell_or_a_fill(tmp_path, baselines):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, _latch(baselines=baselines))
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(ExitLatchBlocked):
        hands.reconcile()
    assert client.calls == []
    assert store.fills() == []
    assert store.load_position()["qty"] == pytest.approx(0.00025)


@pytest.mark.parametrize("n", [1, 2, 5, 50])
def test_increasing_stale_read_count_never_changes_the_safe_outcome(tmp_path, n):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    baselines = [{"total": FOREIGN_BTC, "ts_ms": i} for i in range(n)]
    store.kv_set(EXIT_LATCH_KEY, _latch(baselines=baselines))
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(ExitLatchBlocked):
        hands.reconcile()
    assert client.calls == []


@pytest.mark.parametrize("qty_residual", [0.00025, 0.00001, 0.0])
def test_one_lot_whole_or_partial_invents_neither_cancel_nor_full_fill(tmp_path, qty_residual):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, _latch(qty_residual=qty_residual, qty_booked=0.00025 - qty_residual))
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(ExitLatchBlocked):
        hands.reconcile()
    assert client.calls == []
    assert store.fills() == []


# --- required scenario 6: crash at every phase boundary blocks on restart --


@pytest.mark.parametrize("phase", [
    "cancel_submitting", "cancel_pending", "ready_to_sell", "sell_submitting",
    "sell_pending", "settlement_pending", "settled", "manual_review",
])
def test_restart_after_crash_at_any_phase_never_repeats_a_write(tmp_path, phase):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, _latch(phase=phase))
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])

    for _restart in range(3):  # simulate several process restarts in a row
        store._conn.close()
        store = Store(store.path, mode="live")
        hands = LiveHands(_live_settings(), store, client, sleep=lambda s: None)
        with pytest.raises(ExitLatchBlocked):
            hands.reconcile()

    assert client.calls == [], "at most zero DELETE/POST after restart -- never a repeat, never a second SELL"
    assert store.load_position()["qty"] == pytest.approx(0.00025), "qty preserved across every restart"
    assert store.fills() == [], "pnl/journal preserved (no invented settlement) across every restart"


# --- required scenario 9: storage read failure while guarding is fatal -----


def test_storage_failure_reading_the_latch_is_fatal_not_swallowed(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)

    def boom(key, default=None):
        raise RuntimeError("kv read failed")

    hands.store.kv_get = boom
    with pytest.raises(RuntimeError, match="kv read failed"):
        hands.reconcile()
    assert client.calls == []


def test_incomplete_pagination_never_reached_once_latched(tmp_path):
    """A latch must block reconcile before it ever queries open orders, so an
    incomplete/paginated/erroring order list cannot even become relevant."""
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, _latch())

    def boom(**kwargs):
        raise RuntimeError("open-orders endpoint down")

    client = FakeClient(btc=[FOREIGN_BTC + 0.00025])
    client.open_orders = boom
    hands = _hands(store, client)
    with pytest.raises(ExitLatchBlocked):
        hands.reconcile()


def test_foreign_stop_id_still_escalates_via_direct_sell(tmp_path):
    """_sell() itself (now unreachable from execute()/mark() in live mode,
    see test_llm_sell_refuses_before_any_write) still escalates correctly
    when called directly -- this preserves the PositionStuck diagnostic for
    whichever future caller resumes it once terminal evidence exists."""
    store = Store(tmp_path / "l.db", mode="live")
    store.save_position(
        qty=0.00025, entry=80000.0, stop_price=79200.0, entry_order_id="oid-m1",
        stop_order_id="oid-t", state="OPEN", entry_source="deals", btc_before=FOREIGN_BTC,
    )
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(PositionStuck):
        hands._sell(_snap(), exit_reason=None)


def test_direct_sell_is_itself_fenced_by_an_exit_latch(tmp_path):
    """The re-review's concrete refutation of 'ready, fenced infrastructure':
    inserting an EXIT latch (e.g. `sell_submitting`, simulating a foreign or
    future writer) and then calling `_sell()` directly used to send a real
    DELETE (cancel) and market SELL -- `_sell()` had no guard of its own and
    relied entirely on never being called while a latch exists. `_sell()` is
    kept as tested infrastructure for invariant 4 (see AGENTS.md/CLAUDE.md),
    so it must be fenced the same way every other entry point is, not merely
    unreachable from today's public route."""
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    store.kv_set(EXIT_LATCH_KEY, _latch(phase="sell_submitting"))
    client = FakeClient(btc=[FOREIGN_BTC + 0.00025], open_ids=[{"oid-t"}])
    hands = _hands(store, client)
    with pytest.raises(ExitLatchBlocked):
        hands._sell(_snap(), exit_reason=None)
    assert client.calls == [], "no DELETE/POST may reach the client once a latch is present"


# --- item 6: settlement in one local transaction (reconcile closed_on_exchange) --


def test_reconcile_settlement_is_one_local_transaction_not_two_commits(tmp_path):
    """If the position-clear half of settlement fails, the fill half must not
    have been durably committed either -- otherwise a crash there leaves a
    booked SELL fill with the position row still open, and the next
    reconcile() would book the very same exit a second time."""
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC], open_ids=[set()])  # our qty gone: stop hit
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-settlement")

    store.clear_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands.reconcile()
    # The failure happened mid-transaction on this connection; simulate a crash
    # (close without committing) and reopen fresh to see only what was durably
    # committed -- a same-connection read would see the still-uncommitted insert.
    store._conn.close()
    reopened = Store(store.path, mode="live")
    assert reopened.fills() == [], "fill must not survive if the paired clear did not commit"
    row = reopened.load_position()
    assert row is not None and row["qty"] == pytest.approx(0.00025), "position must not be half-closed"


def test_reconcile_settlement_still_commits_once_on_success(tmp_path):
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC], open_ids=[set()])
    hands = _hands(store, client)
    assert hands.reconcile() == "closed_on_exchange"
    assert store.load_position() is None
    assert len(store.fills()) == 1


def test_reconcile_settlement_failure_leaves_no_open_transaction_on_the_same_connection(tmp_path):
    """The pre-fix bug: `test_reconcile_settlement_is_one_local_transaction_not_two_commits`
    above proves nothing about the real runtime, because it closes the connection
    after the failure -- which discards any open transaction whether or not the
    code rolls back. The CLI's real retry reuses the SAME Store/connection after
    its backoff. Reproduce exactly that: no close, no reopen, a plain later
    write on the identical connection is the retry."""
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC], open_ids=[set()])  # our qty gone: stop hit
    hands = _hands(store, client)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-settlement")

    store.clear_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands.reconcile()

    assert store._conn.in_transaction is False, (
        "a failed settlement must roll back, not leave an open transaction for "
        "the next unrelated commit on this connection to sweep up"
    )
    # The CLI's retry: same process, same Store, same connection, some later
    # write happens to commit (e.g. the next audit row or kv write).
    store.kv_set("anything", "x")
    assert store.fills() == [], "the orphan fill must not have been committed by an unrelated later write"
    assert store.day_pnl(hands.today()) == 0.0
    row = store.load_position()
    assert row is not None and row["qty"] == pytest.approx(0.00025), "position must not be half-closed"


def test_reconcile_settlement_syncs_memory_before_fallible_logging_so_retry_never_duplicates(tmp_path, monkeypatch):
    """Second failure boundary from the same bug: the settlement's commit can
    succeed and then an auxiliary step (the operator log line) can still raise
    before in-memory state is zeroed. If memory is not synced BEFORE that
    fallible step, a retry on the same Hands object re-reads a stale `qty>0`
    in-memory position and books the exit a second time."""
    store = Store(tmp_path / "l.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC], open_ids=[set()])
    hands = _hands(store, client)

    def boom_log(*a, **kw):
        raise RuntimeError("logging exploded mid-settlement")

    monkeypatch.setattr("bot.hands.log.warning", boom_log)
    with pytest.raises(RuntimeError, match="logging exploded"):
        hands.reconcile()

    # The commit already happened; in-memory state must already reflect it so a
    # retry cannot re-settle the same exit.
    assert not hands.position.is_open(), "in-memory position must already be flat once the commit succeeded"
    assert len(store.fills()) == 1, "the real fill from the successful commit must be there exactly once"
    assert store.load_position() is None
