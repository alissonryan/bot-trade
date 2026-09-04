from pathlib import Path
import sys

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


def test_paper_buy_then_stop(tmp_path):
    store = Store(tmp_path / "x.db")
    hands = PaperHands(Settings.from_env(), store)
    gate = GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79200.00")
    pos = hands.execute(gate, _snap(ask=80010))
    assert pos.qty == 0.00025
    assert pos.stop_price == 79200.00
    assert pos.entry > 80000
    stopped = hands.mark(_snap(last=79100, bid=79090, ask=79110))
    assert stopped.qty == 0.0
    assert hands.position.qty == 0.0
    assert store.day_pnl(hands.today()) < 0
