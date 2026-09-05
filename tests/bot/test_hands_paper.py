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
