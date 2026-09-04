from pathlib import Path
import json
import sys
import threading
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.chart_server import ChartServer, require_loopback
from bot.hub import Hub


class FakeKcex:
    def kline(self, symbol="BTC_USDT", interval="Min15", start=0, end=0, open_price_mode="LAST_CLOSE"):
        return {"code": 200, "data": {"t": [1], "o": [1], "h": [2], "l": [0.5], "c": [1.5], "v": [10]}}


def test_require_loopback_rejects_public():
    try:
        require_loopback("0.0.0.0")
        assert False, "should have raised"
    except ValueError:
        pass
    assert require_loopback("127.0.0.1") == "127.0.0.1"


def test_http_index_and_kline():
    server = ChartServer(hub=Hub(), client=FakeKcex(), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    server.start()
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        index = urllib.request.urlopen(base + "/", timeout=3).read().decode()
        assert "BTC" in index or "chart" in index.lower()
        raw = urllib.request.urlopen(base + "/kline", timeout=3).read().decode()
        assert "1.5" in raw
    finally:
        server.shutdown()


def test_ws_streams_tick_json():
    from websockets.sync.client import connect

    hub = Hub()
    hub.symbol = "BTC_USDT"
    hub.last = 12345.6
    hub.bid = 12345.0
    hub.ask = 12346.0
    hub.ts_ms = 1700000000000

    server = ChartServer(hub=hub, client=FakeKcex(), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    server.start()
    thread.start()
    try:
        with connect(f"ws://127.0.0.1:{server.port}/ws", open_timeout=3) as ws:
            raw = ws.recv(timeout=3)
            msg = json.loads(raw)
            assert msg["type"] == "tick"
            assert msg["symbol"] == "BTC_USDT"
            assert msg["last"] == 12345.6
            assert msg["bid"] == 12345.0
            assert msg["ask"] == 12346.0
            assert msg["ts_ms"] == 1700000000000
    finally:
        server.shutdown()
