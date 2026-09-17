import pytest

from fut.ledger import PaperLedger
from fut.settings import FutSettings
from fut.store import FutStore, day_of
from fut.types import FutGate
from tests.fut.helpers import SPEC, make_snap


def make(tmp_path, **settings):
    store = FutStore(tmp_path / "fut.db")
    ledger = PaperLedger(store, FutSettings(**settings), SPEC)
    return store, ledger


def gate(side="long", price=76000.0, contracts=2, stop=None, liq=None):
    notional = contracts * 0.0001 * price
    if stop is None:
        stop = 75900.0 if side == "long" else 76100.0
    if liq is None:
        liq = price * 0.005 if side == "long" else price * 1.995
    return FutGate(True, "ok_open", "LONG" if side == "long" else "SHORT", side=side, contracts=contracts,
                   price=price, notional=notional, margin=notional, stop=stop, liq=liq, leverage=1)


def test_open_charges_taker_fee_and_survives_restart(tmp_path):
    store, ledger = make(tmp_path)
    pos = ledger.open(gate(), now_ms=1000)
    assert pos.is_open() and pos.funding_through_ms == 1000
    assert ledger.balance == pytest.approx(450 - 15.2 * 0.0001)
    again = PaperLedger(store, FutSettings(), SPEC)
    assert again.position == pos
    assert again.balance == pytest.approx(ledger.balance)
    assert [f["kind"] for f in store.fut_fills("main")] == ["open"]


def test_open_refuses_non_ok_gate_and_double_open(tmp_path):
    _, ledger = make(tmp_path)
    with pytest.raises(ValueError):
        ledger.open(FutGate(False, "stale", "LONG"), now_ms=1)
    ledger.open(gate(), now_ms=1)
    with pytest.raises(ValueError):
        ledger.open(gate(), now_ms=2)


def test_close_long_books_price_pnl_minus_fee(tmp_path):
    _, ledger = make(tmp_path)
    ledger.open(gate(), now_ms=1000)
    net = ledger.close(76100.0, now_ms=2000, reason="llm_close")
    close_fee = 76100.0 * 0.0002 * 0.0001
    assert net == pytest.approx(0.02 - close_fee)
    assert ledger.balance == pytest.approx(450 - 15.2 * 0.0001 + 0.02 - close_fee)
    assert not ledger.position.is_open()


def test_short_profits_when_price_falls(tmp_path):
    _, ledger = make(tmp_path)
    ledger.open(gate("short"), now_ms=1000)
    assert ledger.close(75900.0, now_ms=2000, reason="llm_close") == pytest.approx(0.02 - 75900.0 * 0.0002 * 0.0001)


def test_long_stop_fills_at_worse_of_stop_and_book_with_slippage(tmp_path):
    store, ledger = make(tmp_path)
    ledger.open(gate(), now_ms=1000)
    assert ledger.mark(make_snap(bid=75850.0, last=75860.0, ask=75850.1), now_ms=2000) == "stop"
    close = store.fut_fills("main")[-1]
    assert close["reason"] == "stop"
    assert close["price"] == pytest.approx(75850.0 * 0.9998)


def test_short_stop(tmp_path):
    store, ledger = make(tmp_path)
    ledger.open(gate("short"), now_ms=1000)
    assert ledger.mark(make_snap(bid=76149.9, ask=76150.0, last=76140.0, fair=76140.0), now_ms=2000) == "stop"
    assert store.fut_fills("main")[-1]["price"] == pytest.approx(76150.0 * 1.0002)


def test_liquidation_by_fair_price_wins_over_stop_and_loses_margin(tmp_path):
    _, ledger = make(tmp_path)
    ledger.open(gate(stop=75600.0, liq=75000.0), now_ms=1000)
    assert ledger.mark(make_snap(bid=75500.0, last=75500.0, fair=74990.0), now_ms=2000) == "liquidation"
    assert ledger.balance == pytest.approx(450 - 15.2 * 0.0001 - 15.2)


def test_time_limit_closes_at_market(tmp_path):
    store, ledger = make(tmp_path, max_hold_s=300)
    ledger.open(gate(), now_ms=1000)
    assert ledger.mark(make_snap(), now_ms=300_999) is None
    assert ledger.mark(make_snap(), now_ms=301_000) == "time_limit"
    assert store.fut_fills("main")[-1]["price"] == pytest.approx(76000.0 * 0.9998)


def test_funding_long_pays_positive_rate_once(tmp_path):
    store, ledger = make(tmp_path)
    ledger.open(gate(), now_ms=1000)
    before = ledger.balance
    snap = make_snap(next_funding_ms=5000, funding_rate=0.0001)
    assert ledger.mark(snap, now_ms=4999) is None
    assert ledger.mark(snap, now_ms=6000) == "funding"
    assert ledger.mark(snap, now_ms=7000) is None
    assert ledger.balance == pytest.approx(before - 2 * 0.0001 * 76000.0 * 0.0001)
    assert ledger.position.funding_through_ms == 5000
    assert store.fut_fills("main")[-1]["kind"] == "funding"


def test_funding_short_receives(tmp_path):
    _, ledger = make(tmp_path)
    ledger.open(gate("short"), now_ms=1000)
    before = ledger.balance
    ledger.mark(make_snap(next_funding_ms=5000, funding_rate=0.0001), now_ms=6000)
    assert ledger.balance == pytest.approx(before + 2 * 0.0001 * 76000.0 * 0.0001)


def test_failed_write_rolls_back_everything(tmp_path, monkeypatch):
    store, ledger = make(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("disk")

    monkeypatch.setattr(store, "set_balance", boom)
    with pytest.raises(RuntimeError):
        ledger.open(gate(), now_ms=1000)
    monkeypatch.undo()
    fresh = PaperLedger(store, FutSettings(), SPEC)
    assert not fresh.position.is_open()
    assert fresh.balance == 450.0
    assert store.fut_fills("main") == []
    assert not ledger.position.is_open()


def test_books_are_independent_and_day_net_counts_fees_and_funding(tmp_path):
    store, ledger = make(tmp_path)
    shadow = PaperLedger(store, FutSettings(), SPEC, book="shadow:random")
    ledger.open(gate(), now_ms=1000)
    assert not shadow.position.is_open() and shadow.balance == 450.0
    ledger.close(76100.0, now_ms=2000, reason="llm_close")
    expected = 0.02 - 15.2 * 0.0001 - 76100.0 * 0.0002 * 0.0001
    assert store.day_net("main", day_of(2000)) == pytest.approx(expected)
    assert store.day_net("shadow:random", day_of(2000)) == 0.0


def test_unrealized_marks_to_mid(tmp_path):
    _, ledger = make(tmp_path)
    ledger.open(gate(), now_ms=1000)
    assert ledger.unrealized(make_snap(bid=76100.0, ask=76100.2)) == pytest.approx(100.1 * 0.0002)


def test_store_refuses_a_database_stamped_by_another_mode(tmp_path):
    from bot.store import Store, StoreIdentityMismatch

    Store(tmp_path / "x.db", mode="paper").close()
    with pytest.raises(StoreIdentityMismatch):
        FutStore(tmp_path / "x.db")


def test_decisions_and_model_cost(tmp_path):
    store, _ = make(tmp_path)
    store.log_decision("jev", {"cost_usd": 0.001, "model": "jev-1"}, ts_ms=1000)
    store.log_decision("llm", {"cost_usd": 0.01}, ts_ms=2000)
    store.log_decision("exit", {"reason": "stop"}, ts_ms=3000)
    assert [d["kind"] for d in store.decisions()] == ["jev", "llm", "exit"]
    assert store.decisions("llm")[0]["payload"] == {"cost_usd": 0.01}
    assert store.model_cost_between(0, 2500) == pytest.approx(0.011)
    assert store.model_cost_between(1500, 2500) == pytest.approx(0.01)
