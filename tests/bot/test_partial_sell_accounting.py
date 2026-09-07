"""Regressions introduced by the partial-fill recovery, found in external review.

The fix that stopped an oversized stop being placed after a partial sell was
correct about the stop and wrong about everything around it:

  A. it shrank the position without booking the BTC that actually left, so the
     ledger lost that PnL permanently -- day-loss, the post-loss cooldown and
     the journal all stopped seeing a loss that really happened;
  B. it did arithmetic on a balance that can be NaN. `max(0, qty - max(nan, 0))`
     collapses to zero, so a malformed balance erased the position and sent a
     trigger for 0.00000 BTC. Before the fix the same input kept the row and a
     full-size stop, so this one is a genuine regression, not an inherited gap.

Both are money-path: the first corrupts the accounting every later decision
reads, the second throws the position away on a bad parse.
"""
import math
from dataclasses import replace
from unittest.mock import Mock

import pytest

from bot.hands import LiveHands, UnprotectedPosition
from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, Snapshot, SymbolRules


def _snap(bid=79_000.0):
    return Snapshot(ts_ms=1, last=bid, bid=bid, ask=bid + 1, spread=1.0, bars_15m=[],
                    atr=100.0, free_usdt=450.0, bot_qty=0.0, bot_avg_entry=None,
                    ws_ok=True, stale=False)


def _hands(tmp_path, client):
    store = Store(tmp_path / "live.db", mode="live")
    settings = replace(Settings.from_env(), mode="live", fill_confirm_tries=1,
                       fill_confirm_wait_s=0)
    hands = LiveHands(settings, store, client, rules=SymbolRules(qty_scale=5),
                      sleep=lambda _s: None)
    return hands, store


def _open_position(hands, store, qty=0.00025, entry=80_000.0):
    hands.position.qty = qty
    hands.position.entry = entry
    hands.position.stop_price = 70_000.0
    hands.position.state = "OPEN"
    hands.position.entry_source = "live"
    hands.entry_order_id = "e1"
    hands.stop_order_id = None
    store.save_position(qty=qty, entry=entry, stop_price=70_000.0,
                        entry_order_id="e1", stop_order_id=None,
                        state="OPEN", entry_source="live")


def test_partial_sell_books_the_btc_that_actually_left(tmp_path):
    """0.00015 of 0.00025 really sold; the ledger must record that loss."""
    client = Mock()
    # start 0.00025, then 0.00010 after the partial fill.
    client.balances.side_effect = lambda *_a, **_k: {
        "data": [{"currency": "BTC", "available": next(balances), "frozen": 0}]
    }
    balances = iter([0.00025, 0.00010, 0.00010, 0.00010])
    client.place_market.side_effect = RuntimeError("sell POST failed")
    client.place_trigger.return_value = {"data": "stop-1"}

    hands, store = _hands(tmp_path, client)
    _open_position(hands, store)
    hands.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap())

    fills = store.fills(10)
    sells = [f for f in fills if f["side"] == "SELL"]
    assert sells, "the partially sold BTC was never booked"
    assert sum(f["qty"] for f in sells) == pytest.approx(0.00015, abs=1e-9)
    # entry 80000 -> 79000 on 0.00015 is a real loss the ledger must carry.
    assert sum(f["pnl"] for f in sells) < 0
    assert store.day_pnl(hands.today()) < 0, "day-loss and cooldown must see it"
    assert hands.position.qty == pytest.approx(0.00010, abs=1e-9)


def test_a_non_finite_balance_never_sizes_an_order_or_drops_the_position(tmp_path):
    """A malformed balance is ignorance, not zero. Halt; never send 0.00000."""
    client = Mock()
    reads = iter([0.00025, float("nan")])
    client.balances.side_effect = lambda *_a, **_k: {
        "data": [{"currency": "BTC", "available": next(reads), "frozen": 0}]
    }
    client.place_market.side_effect = RuntimeError("sell POST failed")
    client.place_trigger.return_value = {"data": "stop-1"}

    hands, store = _hands(tmp_path, client)
    _open_position(hands, store)
    with pytest.raises(UnprotectedPosition):
        hands.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap())

    client.place_trigger.assert_not_called()
    row = store.load_position()
    assert row is not None, "a bad parse must not erase a real position"
    assert row["qty"] == pytest.approx(0.00025, abs=1e-9)


def test_btc_total_refuses_a_non_finite_balance(tmp_path):
    from bot.hands import btc_total
    client = Mock()
    client.balances.return_value = {
        "data": [{"currency": "BTC", "available": "NaN", "frozen": 0}]
    }
    with pytest.raises(ValueError, match="finite|balance"):
        btc_total(client)


def test_an_empty_book_is_not_a_fresh_book(tmp_path):
    """`_poll_depth_rest` stamped freshness even with no levels, so the stale
    bid it kept read as live and a take-profit could fire against it."""
    from bot.eye import Eye
    client = Mock()
    client.depth.return_value = {"data": {"data": {"bids": [], "asks": []}}}
    eye = Eye(client, Settings.from_env())
    eye.bid, eye.ask = 82_000.0, 82_001.0
    eye.depth_update_ms = 0
    eye._poll_depth_rest()
    assert eye.depth_update_ms == 0, "an empty book must not refresh depth age"
    assert eye._depth_stale() is True
