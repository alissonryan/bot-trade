from pathlib import Path
import json
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.chart_server import ChartServer, require_loopback
from bot.hub import Hub


class FakeKcex:
    def kline(self, symbol="BTC_USDT", interval="Min15", start=0, end=0, open_price_mode="LAST_CLOSE"):
        return {"code": 200, "data": {"t": [1], "o": [1], "h": [2], "l": [0.5], "c": [1.5], "v": [10]}}


class BoomKcex:
    def kline(self, *a, **kw):
        raise RuntimeError("exchange down")


def test_require_loopback_rejects_public():
    try:
        require_loopback("0.0.0.0")
        assert False, "should have raised"
    except ValueError:
        pass
    assert require_loopback("127.0.0.1") == "127.0.0.1"


def test_start_alone_serves_requests():
    """Regression: start() must launch the accept loop itself.

    Production (bot/cli.py) only ever calls start(); nobody calls
    serve_forever(). This test deliberately does NO manual threading, so it
    fails if start() only binds the port without serving.
    """
    server = ChartServer(hub=Hub(), client=FakeKcex(), host="127.0.0.1", port=0)
    server.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/", timeout=3) as resp:
            assert resp.status == 200
            assert resp.read()
    finally:
        server.shutdown()


def test_kline_failure_returns_502_json():
    server = ChartServer(hub=Hub(), client=BoomKcex(), host="127.0.0.1", port=0)
    server.start()
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}/kline", timeout=3)
            assert False, "should have raised HTTPError"
        except urllib.error.HTTPError as exc:
            assert exc.code == 502
            body = json.loads(exc.read().decode())
            assert "error" in body
    finally:
        server.shutdown()


def test_ws_rejects_foreign_origin():
    import socket

    server = ChartServer(hub=Hub(), client=FakeKcex(), host="127.0.0.1", port=0)
    server.start()
    try:
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=3)
        sock.sendall(
            b"GET /ws HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n"
            b"Origin: http://evil.example\r\n"
            b"\r\n"
        )
        head = sock.recv(64)
        sock.close()
        assert b"403" in head
    finally:
        server.shutdown()


def test_http_index_and_kline():
    server = ChartServer(hub=Hub(), client=FakeKcex(), host="127.0.0.1", port=0)
    server.start()
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
    server.start()
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
