from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.hands import PaperHands
from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, Snapshot, Bar


def _snap(last=80000.0, bid=79999.0, ask=80001.0):
    return Snapshot(
        ts_ms=1, last=last, bid=bid, ask=ask, spread=ask - bid,
        bars_15m=[Bar(1, last, last, last, last)], atr=400,
        free_usdt=450, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )


def _settings(**kw) -> Settings:
    d = Settings.from_env().__dict__.copy()
    d.update(kw)
    return Settings(**d)


def test_paper_buy_then_stop(tmp_path):
    store = Store(tmp_path / "x.db")
    hands = PaperHands(_settings(paper_starting_usdt=450.0), store)
    gate = GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79200.00")
    pos = hands.execute(gate, _snap(ask=80010))
    assert pos.qty == 0.00025
    assert pos.stop_price == 79200.00
    assert pos.entry > 80000
    assert hands.cash == pytest.approx(450.0 - pos.entry * 0.00025)
    stopped = hands.mark(_snap(last=79100, bid=79090, ask=79110))
    assert stopped.qty == 0.0
    assert hands.position.qty == 0.0
    assert store.day_pnl(hands.today()) < 0
    stop_fill = store.fills(1)[0]
    assert stop_fill["source"] == "paper_stop"
    assert stop_fill["price"] < 79090  # stop pays the same slippage as the entry
    assert hands.cash == pytest.approx(450.0 + store.day_pnl(hands.today()))


def test_paper_cash_and_position_survive_restart(tmp_path):
    db = tmp_path / "x.db"
    hands = PaperHands(_settings(paper_starting_usdt=450.0), Store(db))
    hands.execute(GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79200.00"), _snap())
    cash_after_buy = hands.cash
    again = PaperHands(_settings(paper_starting_usdt=999.0), Store(db))
    assert again.cash == pytest.approx(cash_after_buy)
    assert again.position.qty == 0.00025
    again.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap(bid=81000))
    assert again.position.qty == 0.0
    assert again.cash > cash_after_buy


def test_paper_refuses_buy_without_cash(tmp_path):
    hands = PaperHands(_settings(paper_starting_usdt=5.0), Store(tmp_path / "x.db"))
    pos = hands.execute(GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79200.00"), _snap())
    assert pos.qty == 0.0
    assert hands.cash == 5.0


def test_paper_stop_does_not_fire_on_a_missing_bid(tmp_path):
    """Finding 7: snap.bid is 0.0 until a bookTicker frame arrives, and a deals-only
    frame already marks the feed healthy -- so poll_quotes skips REST and mark()
    saw `0.0 <= stop_price`. It then closed at min(0.0, stop) * (1 - slip) == 0.0,
    booking pnl = -entry*qty and crediting nothing back to the persisted ledger."""
    store = Store(tmp_path / "x.db")
    hands = PaperHands(_settings(paper_starting_usdt=450.0), store)
    hands.execute(
        GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79200.00"),
        _snap(),
    )
    cash_after_buy = hands.cash
    assert hands.position.qty > 0

    # price feed is alive on `last` but bid has never been seen
    hands.mark(_snap(last=80000.0, bid=0.0, ask=0.0))

    assert hands.position.qty > 0, "stop fired on a bid that was never quoted"
    assert hands.cash == cash_after_buy


def test_paper_stop_still_fires_on_a_real_bid(tmp_path):
    """The control: a genuine bid at or below the stop must still close."""
    store = Store(tmp_path / "x.db")
    hands = PaperHands(_settings(paper_starting_usdt=450.0), store)
    hands.execute(
        GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79200.00"),
        _snap(),
    )
    hands.mark(_snap(last=79100.0, bid=79100.0, ask=79102.0))
    assert hands.position.qty == 0.0
    assert hands.cash > 0


def test_paper_tp_recomputed_from_fill_persisted_and_sells_at_bid(tmp_path):
    settings = _settings(tp_atr_mult=3, paper_slippage_bps=2)
    db = tmp_path / "tp.db"
    hands = PaperHands(settings, Store(db))
    hands.execute(GateResult(True, "ok_buy", "BUY", qty=".00025", stop_price="79200", take_profit_price="81200"), _snap(ask=80000))
    assert hands.position.take_profit_price == pytest.approx(81216)
    hands = PaperHands(settings, Store(db))
    hands.mark(_snap(last=81300, bid=81215, ask=81301))
    assert hands.position.is_open()  # executable bid, not last, triggers TP
    hands.mark(_snap(last=81300, bid=81216, ask=81301))
    assert not hands.position.is_open()
    fill = hands.store.fills(1)[0]
    assert fill["source"] == "paper_take_profit"
    assert fill["price"] == pytest.approx(81216 * .9998)


def test_paper_time_limit_survives_restart_and_missing_quote(tmp_path, monkeypatch):
    settings = _settings(time_limit_minutes=60, tp_atr_mult=0)
    db = tmp_path / "ttl.db"
    monkeypatch.setattr("bot.hands.time.time", lambda: 1_700_000_000)
    hands = PaperHands(settings, Store(db))
    hands.execute(GateResult(True, "ok_buy", "BUY", qty=".00025", stop_price="79200"), _snap())
    opened = hands.store.load_position()["opened_ts"]
    monkeypatch.setattr("bot.hands.time.time", lambda: 1_700_003_599)
    hands._persist()
    hands = PaperHands(settings, Store(db))
    assert hands.store.load_position()["opened_ts"] == opened
    hands.mark(_snap())
    assert hands.position.is_open()
    monkeypatch.setattr("bot.hands.time.time", lambda: 1_700_003_600)
    hands.mark(_snap(last=0, bid=0, ask=0))
    assert hands.position.is_open()  # never credit a fabricated zero-price exit
    snap = _snap()
    snap.stale = True
    hands.mark(snap)
    assert not hands.position.is_open()  # stale entries blocked, timed exits allowed
    assert hands.store.fills(1)[0]["source"] == "paper_time_limit"


@pytest.mark.parametrize("bid", [float("nan"), float("inf"), -1])
def test_bad_quotes_cannot_fire_local_barrier(tmp_path, bid):
    from bot.hands import Position, local_exit_reason
    pos = Position(qty=1, entry=100, state="OPEN", take_profit_price=101, opened_ts="2020-01-01T00:00:00+00:00")
    assert local_exit_reason(pos, _snap(last=100, bid=bid), _settings(time_limit_minutes=1), 1_900_000_000_000) is None


def test_opt_out_suspends_even_a_previously_persisted_local_target():
    from bot.hands import Position, local_exit_reason
    pos = Position(qty=1, entry=100, state="OPEN", take_profit_price=101)
    assert local_exit_reason(pos, _snap(last=103, bid=102), _settings(tp_atr_mult=0, time_limit_minutes=0), 1) is None


def test_take_profit_never_fires_on_a_stale_depth_book():
    """Snapshot.stale only tracks the ticker (`last`); the order book (bid/ask)
    can freeze behind a healthy ticker (WS bookTicker down, REST depth top-up
    failing) while target crossed at a bid that may no longer exist."""
    from bot.hands import Position, local_exit_reason
    pos = Position(qty=1, entry=100, state="OPEN", take_profit_price=101)
    settings = _settings(tp_atr_mult=3, time_limit_minutes=0)
    snap = _snap(last=103, bid=102)  # target crossed
    snap.depth_stale = True
    assert local_exit_reason(pos, snap, settings, 1) is None


def test_time_limit_still_exits_on_a_stale_depth_book():
    """Exits must never be blocked by staleness the same way entries are --
    only the take_profit branch gains the freshness requirement; TTL must
    keep firing on a stale-but-valid quote."""
    from bot.hands import Position, local_exit_reason
    pos = Position(qty=1, entry=100, state="OPEN", take_profit_price=101,
                   opened_ts="2020-01-01T00:00:00+00:00")
    settings = _settings(tp_atr_mult=3, time_limit_minutes=1)
    snap = _snap(last=103, bid=102)  # target also crossed
    snap.depth_stale = True
    assert local_exit_reason(pos, snap, settings, 1_900_000_000_000) == "time_limit"


def test_take_profit_fires_on_a_fresh_depth_book():
    from bot.hands import Position, local_exit_reason
    pos = Position(qty=1, entry=100, state="OPEN", take_profit_price=101)
    settings = _settings(tp_atr_mult=3, time_limit_minutes=0)
    snap = _snap(last=103, bid=102)
    snap.depth_stale = False
    assert local_exit_reason(pos, snap, settings, 1) == "take_profit"


# --- L6: PaperHands fill + position + cash must be one local transaction ---
#
# Store.add_fill/save_position/kv_set each commit on their own by default. A
# crash (or any exception) between them used to leave the ledger and the
# position/cash disagreeing -- a fill with no matching position/cash change,
# or vice versa. PaperHands' cash lives in kv and its fills in the ledger; a
# half-applied paper settlement corrupts the only forward evidence this
# project has about whether the strategy works, exactly like a live one.
#
# These tests reuse the SAME Store connection across the failure and the
# following assertion/retry -- closing and reopening a fresh connection would
# only prove SQLite's own crash-recovery, not that this code rolls back
# in-process failures on a connection the CLI keeps reusing after backoff.


def _buy_gate(qty="0.00025", stop="79200.00"):
    return GateResult(True, "ok_buy", "BUY", qty=qty, notional=20, stop_price=stop)


def test_paper_buy_fill_and_position_roll_back_together_on_position_write_failure(tmp_path):
    """If the position write fails after the fill insert, the fill must not be
    durably committed either -- otherwise the ledger shows a BUY nothing else
    ever tracked, and a retry could book a second BUY on top of it."""
    store = Store(tmp_path / "x.db")
    hands = PaperHands(_settings(paper_starting_usdt=450.0), store)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-buy")

    store.save_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands.execute(_buy_gate(), _snap(ask=80010))

    assert store.fills() == [], "orphan BUY fill with no matching position row"
    assert store.load_position() is None
    assert store.kv_get("paper_cash") is None, "cash must not move without the matching fill/position"
    assert hands.cash == 450.0, "in-memory cash must not diverge from the rolled-back store"
    assert hands.position.qty == 0.0


def test_paper_buy_failure_leaves_no_open_transaction_on_the_same_connection(tmp_path):
    """The CLI reuses one Store after its backoff; it does not reopen. A
    failed settlement must roll back, not leave an open transaction for the
    next unrelated commit on this SAME connection to durably sweep up."""
    store = Store(tmp_path / "x.db")
    hands = PaperHands(_settings(paper_starting_usdt=450.0), store)

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-buy")

    store.save_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands.execute(_buy_gate(), _snap(ask=80010))

    assert store._conn.in_transaction is False
    store.kv_set("unrelated", "x")  # simulates the CLI's next incidental write
    assert store.fills() == [], "the orphan fill must not have been swept in by an unrelated later commit"
    assert store.load_position() is None


def test_paper_buy_retry_after_failed_write_books_exactly_once(tmp_path):
    store = Store(tmp_path / "x.db")
    hands = PaperHands(_settings(paper_starting_usdt=450.0), store)

    calls = {"n": 0}
    real_save_position = store.save_position

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient disk error")
        return real_save_position(*a, **kw)

    store.save_position = flaky
    with pytest.raises(RuntimeError, match="transient"):
        hands.execute(_buy_gate(), _snap(ask=80010))
    assert store.fills() == []
    assert hands.cash == 450.0

    pos = hands.execute(_buy_gate(), _snap(ask=80010))  # retry: same Hands, same Store
    assert pos.qty == 0.00025
    assert len(store.fills()) == 1, "retry must book exactly one fill, not zero or two"
    assert hands.cash == pytest.approx(450.0 - pos.entry * 0.00025)
    assert store.kv_get("paper_cash") == repr(hands.cash)


def test_paper_close_fill_and_position_clear_roll_back_together(tmp_path):
    """Mirrors the BUY case for the exit side (execute(SELL), the stop in
    mark(), and TP/TTL all funnel through PaperHands._close)."""
    store = Store(tmp_path / "x.db")
    hands = PaperHands(_settings(paper_starting_usdt=450.0), store)
    hands.execute(_buy_gate(), _snap(ask=80010))
    cash_after_buy = hands.cash
    position_after_buy = hands.store.load_position()

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-close")

    store.clear_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap(bid=81000))

    fills = store.fills()
    assert len(fills) == 1 and fills[0]["side"] == "BUY", "orphan SELL fill with the position never cleared"
    assert store.load_position() == position_after_buy, "position must not be half-closed"
    assert store.kv_get("paper_cash") == repr(cash_after_buy), "cash must not move without the matching fill/clear"
    assert hands.cash == pytest.approx(cash_after_buy), "in-memory cash must not diverge from the rolled-back store"
    assert hands.position.qty == pytest.approx(0.00025), "in-memory position must not be cleared early"


def test_paper_close_failure_leaves_no_open_transaction_on_the_same_connection(tmp_path):
    store = Store(tmp_path / "x.db")
    hands = PaperHands(_settings(paper_starting_usdt=450.0), store)
    hands.execute(_buy_gate(), _snap(ask=80010))

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-close")

    store.clear_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap(bid=81000))

    assert store._conn.in_transaction is False
    store.kv_set("unrelated", "x")
    fills = store.fills()
    assert len(fills) == 1 and fills[0]["side"] == "BUY", "the orphan SELL fill must not be swept in by an unrelated later commit"
    assert store.load_position()["qty"] == pytest.approx(0.00025)


def test_paper_close_retry_after_failed_write_books_exactly_once(tmp_path):
    store = Store(tmp_path / "x.db")
    hands = PaperHands(_settings(paper_starting_usdt=450.0), store)
    hands.execute(_buy_gate(), _snap(ask=80010))

    calls = {"n": 0}
    real_clear_position = store.clear_position

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient disk error")
        return real_clear_position(*a, **kw)

    store.clear_position = flaky
    with pytest.raises(RuntimeError, match="transient"):
        hands.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap(bid=81000))
    assert len(store.fills()) == 1  # only the BUY from setup; no orphan SELL

    pos = hands.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap(bid=81000))  # retry
    assert pos.qty == 0.0
    fills = store.fills()
    assert len(fills) == 2 and fills[0]["side"] == "SELL", "retry must book exactly one SELL fill, not zero or two"
    assert store.load_position() is None


def test_paper_stop_hit_fill_and_close_are_one_transaction(tmp_path, monkeypatch):
    """mark()'s stop-hit path also funnels through _close(); prove the same
    atomicity there, not only through execute(SELL)."""
    store = Store(tmp_path / "x.db")
    hands = PaperHands(_settings(paper_starting_usdt=450.0), store)
    hands.execute(_buy_gate(), _snap(ask=80010))
    cash_after_buy = hands.cash

    def boom(*a, **kw):
        raise RuntimeError("disk full mid-stop")

    store.clear_position = boom
    with pytest.raises(RuntimeError, match="disk full"):
        hands.mark(_snap(last=79100, bid=79090, ask=79110))

    fills = store.fills()
    assert len(fills) == 1 and fills[0]["side"] == "BUY"
    assert hands.position.qty == pytest.approx(0.00025)
    assert hands.cash == pytest.approx(cash_after_buy)
    assert store._conn.in_transaction is False
