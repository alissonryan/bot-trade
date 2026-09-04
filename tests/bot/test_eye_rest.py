from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.eye import Eye
from bot.hub import Hub
from bot.settings import Settings
from kcex.ws import TickerEvent


class FakeKcex:
    def ticker(self, symbol="BTC_USDT"):
        return {"data": {"c": "80000.0"}, "code": 0}

    def depth(self, symbol="BTC_USDT", price_precision="0.01"):
        return {
            "data": {"data": {"bids": [{"p": "79999", "q": "1"}], "asks": [{"p": "80001", "q": "1"}]}},
            "code": 200,
        }

    def kline(self, symbol="BTC_USDT", interval="Min15", start=0, end=0, open_price_mode="LAST_CLOSE"):
        t = [1 + i * 900 for i in range(20)]
        return {"data": {"t": t, "o": [100]*20, "h": [102]*20, "l": [99]*20, "c": [101]*20, "v": [1]*20}}

    def balances(self, currencies="BTC,USDT"):
        return {"data": [{"currency": "USDT", "available": "450.0", "total": "450.0", "frozen": "0"}]}

    def ws_token(self):
        return {"code": 0, "data": {"wsToken": "abc"}}


def test_rest_snapshot_not_stale():
    eye = Eye(FakeKcex(), Settings.from_env(), bot_qty=0.0, bot_avg_entry=None)
    snap = eye.snapshot_rest()
    assert snap.last == 80000.0
    assert snap.bid == 79999.0
    assert snap.ask == 80001.0
    assert snap.free_usdt == 450.0
    assert snap.atr is not None
    assert snap.stale is False
    assert snap.ws_ok is False


def test_poll_quotes_skipped_when_ws_fresh():
    client = FakeKcex()
    hub = Hub()
    eye = Eye(client, Settings.from_env(), hub=hub)
    hub.apply(TickerEvent(last=111.0, ts_ms=int(time.time() * 1000), symbol="BTC_USDT"))
    eye.sync_hub()
    eye.poll_quotes()
    assert eye.last == 111.0  # REST ticker 80000 not applied


def test_poll_quotes_runs_when_ws_down():
    eye = Eye(FakeKcex(), Settings.from_env(), hub=Hub())
    eye.poll_quotes()
    assert eye.last == 80000.0
    assert eye.ws_ok is False
