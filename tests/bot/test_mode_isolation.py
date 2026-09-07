"""Paper state must never be able to become a live order.

The bug this file pins: `bot/cli.py` opened one database with no mode identity
and handed it to `LiveHands`. Following the README -- run paper, then set
MODE=live -- let a simulated position be adopted as real, and `reconcile()`
would place a resident stop-market SELL sized for BTC the bot never bought,
on an account where the owner keeps their own coins.

Three independent layers, each tested here, because any one of them can be
bypassed by a human pointing a flag somewhere unexpected:
  1. different database file per mode, so the state cannot even be reached;
  2. a mode stamped inside the database, refusing a mismatched open;
  3. LiveHands refusing to adopt a position row that carries paper provenance.
"""
from pathlib import Path

import pytest

from bot.store import Store, StoreIdentityMismatch


def _paper_position(store: Store) -> None:
    store.save_position(qty=0.00025, entry=100_000.0, stop_price=90_000.0,
                        entry_order_id="paper-entry", stop_order_id="paper-stop",
                        state="OPEN", entry_source="paper")


# --- layer 2: the stamp ------------------------------------------------------

def test_a_paper_database_refuses_to_open_as_live(tmp_path):
    path = tmp_path / "bot.db"
    Store(path, mode="paper").close()
    with pytest.raises(StoreIdentityMismatch, match="paper"):
        Store(path, mode="live")


def test_a_live_database_refuses_to_open_as_paper(tmp_path):
    path = tmp_path / "bot.db"
    Store(path, mode="live").close()
    with pytest.raises(StoreIdentityMismatch, match="live"):
        Store(path, mode="paper")


def test_an_unstamped_database_is_never_adopted_as_live(tmp_path):
    """A legacy file carries no proof of what wrote it, so live must not guess.

    Paper may adopt it: being wrong there costs a simulated number. Being wrong
    in the other direction sends a real order sized from a fictional position.
    """
    path = tmp_path / "legacy.db"
    legacy = Store(path)  # no mode argument: pre-identity behaviour
    _paper_position(legacy)
    legacy.close()

    with pytest.raises(StoreIdentityMismatch, match="unstamped|unknown"):
        Store(path, mode="live")

    adopted = Store(path, mode="paper")
    assert adopted.load_position()["qty"] == 0.00025
    adopted.close()
    Store(path, mode="paper").close()  # the stamp now persists


def test_reopening_with_the_same_mode_keeps_working(tmp_path):
    path = tmp_path / "bot.db"
    first = Store(path, mode="paper")
    _paper_position(first)
    first.close()
    again = Store(path, mode="paper")
    assert again.load_position()["qty"] == 0.00025


# --- layer 1: separate files -------------------------------------------------

def test_cli_never_hands_the_same_file_to_both_modes():
    from bot.cli import db_path_for_mode
    assert db_path_for_mode("paper") != db_path_for_mode("live")
    # The existing paper history must keep its home; live is the new file.
    assert db_path_for_mode("paper") == Path("data") / "bot.db"


# --- layer 3: provenance of the row itself -----------------------------------

@pytest.mark.parametrize("field,value", [
    ("entry_source", "paper"),
    ("entry_order_id", "paper-entry"),
    ("stop_order_id", "paper-stop"),
])
def test_live_hands_refuses_a_position_with_paper_provenance(tmp_path, field, value):
    """Even inside a correctly stamped live database, a paper-shaped row is a bug.

    Refusing costs one manual inspection. Adopting places a real trigger over
    the owner's coins.
    """
    from unittest.mock import Mock
    from dataclasses import replace
    from bot.hands import LiveHands
    from bot.settings import Settings

    store = Store(tmp_path / "live.db", mode="live")
    row = dict(qty=0.00025, entry=100_000.0, stop_price=90_000.0,
               entry_order_id="live-entry", stop_order_id="live-stop",
               state="OPEN", entry_source="live")
    row[field] = value
    store.save_position(**row)

    settings = replace(Settings.from_env(), mode="live")
    with pytest.raises(StoreIdentityMismatch, match="paper"):
        LiveHands(settings, store, Mock(), rules=None)


def test_live_hands_accepts_a_genuine_live_position(tmp_path):
    from unittest.mock import Mock
    from dataclasses import replace
    from bot.hands import LiveHands
    from bot.settings import Settings

    store = Store(tmp_path / "live.db", mode="live")
    store.save_position(qty=0.00025, entry=100_000.0, stop_price=90_000.0,
                        entry_order_id="9912", stop_order_id="9913",
                        state="OPEN", entry_source="live")
    hands = LiveHands(replace(Settings.from_env(), mode="live"), store, Mock(), rules=None)
    assert hands.position.qty == 0.00025


# --- the bypasses the first version of this guard left open -------------------
# Layer 3 originally inspected only the position row, so an EMPTY database had
# nothing to inspect and live hands attached to a paper store happily traded.

def test_live_hands_refuse_a_paper_stamped_store_even_when_empty(tmp_path):
    from unittest.mock import Mock
    from dataclasses import replace
    from bot.hands import LiveHands
    from bot.settings import Settings

    store = Store(tmp_path / "paper.db", mode="paper")  # no rows at all
    with pytest.raises(StoreIdentityMismatch, match="live"):
        LiveHands(replace(Settings.from_env(), mode="live"), store, Mock(), rules=None)


def test_live_hands_refuse_a_store_with_no_identity(tmp_path):
    from unittest.mock import Mock
    from dataclasses import replace
    from bot.hands import LiveHands
    from bot.settings import Settings

    store = Store(tmp_path / "anon.db")  # mode=None bypassed layer 2 entirely
    with pytest.raises(StoreIdentityMismatch, match="live"):
        LiveHands(replace(Settings.from_env(), mode="live"), store, Mock(), rules=None)


def test_kv_only_paper_state_is_not_adopted_as_live(tmp_path):
    """`paper_cash` alone is unambiguous paper state; the first `_has_history`
    looked at position/fills/bot_orders/audit and missed kv and journal."""
    path = tmp_path / "legacy.db"
    legacy = Store(path)
    legacy.kv_set("paper_cash", "450.0")
    legacy.close()
    with pytest.raises(StoreIdentityMismatch):
        Store(path, mode="live")


def test_a_refused_open_leaves_the_file_untouched(tmp_path):
    """Identity is settled before any schema work: creating tables or running
    forward migrations on another mode's database is already a write."""
    path = tmp_path / "paper.db"
    Store(path, mode="paper").close()
    before = path.read_bytes()
    with pytest.raises(StoreIdentityMismatch):
        Store(path, mode="live")
    assert path.read_bytes() == before
