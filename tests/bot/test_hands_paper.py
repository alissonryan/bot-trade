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
