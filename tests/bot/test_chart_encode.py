from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.chart_encode import encode_deal, encode_tick
from bot.hub import Hub
from kcex.ws import DealEvent, TickerEvent


def test_encode_tick_from_hub():
    hub = Hub()
    hub.apply(TickerEvent(last=79801.99, ts_ms=9, symbol="BTC_USDT"))
    hub.bid = 79801.9
    hub.ask = 79802.0
    msg = encode_tick(hub)
    assert msg["type"] == "tick"
    assert msg["symbol"] == "BTC_USDT"
    assert msg["last"] == 79801.99
    assert msg["bid"] == 79801.9
    assert msg["ask"] == 79802.0
    assert msg["ts_ms"] == 9
    assert "c" not in msg and "d" not in msg


def test_encode_deal():
    msg = encode_deal(DealEvent(price=1.5, qty=0.2, side="sell", ts_ms=3, symbol="BTC_USDT"))
    assert msg == {
        "type": "deal",
        "symbol": "BTC_USDT",
        "price": 1.5,
        "qty": 0.2,
        "side": "sell",
        "ts_ms": 3,
    }
