from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.hub import Hub
from kcex.ws import DealEvent, DepthEvent, TickerEvent


def test_ticker_sets_last_and_ws_ok():
    hub = Hub()
    hub.apply(TickerEvent(last=79801.99, ts_ms=1, symbol="BTC_USDT"))
    assert hub.last == 79801.99
    assert hub.ws_ok is True
    assert hub.ts_ms == 1


def test_deal_updates_last():
    hub = Hub()
    hub.apply(DealEvent(price=10.0, qty=1, side="buy", ts_ms=2, symbol="BTC_USDT"))
    assert hub.last == 10.0


def test_depth_sets_bid_ask():
    hub = Hub()
    hub.apply(DepthEvent(bid=1.0, ask=2.0, symbol="BTC_USDT"))
    assert hub.bid == 1.0
    assert hub.ask == 2.0


def test_ignore_none():
    hub = Hub()
    hub.apply(None)
    assert hub.ws_ok is False
    assert hub.last == 0.0


def test_mark_down():
    hub = Hub()
    hub.apply(TickerEvent(last=1, ts_ms=1, symbol="BTC_USDT"))
    hub.mark_down()
    assert hub.ws_ok is False
