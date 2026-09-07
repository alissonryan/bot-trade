from pathlib import Path
import json
import sys
import urllib.error
import urllib.parse
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


def test_ws_keeps_a_receive_only_browser_connected():
    """Browsers don't send application frames while receiving the tick stream."""
    import time

    from websockets.sync.client import connect

    server = ChartServer(hub=Hub(), client=FakeKcex(), host="127.0.0.1", port=0)
    server.start()
    try:
        with connect(f"ws://127.0.0.1:{server.port}/ws", open_timeout=3, ping_interval=None) as ws:
            deadline = time.monotonic() + 2.2
            while time.monotonic() < deadline:
                assert json.loads(ws.recv(timeout=3))["type"] == "tick"
            assert ws.ping(b"still-connected").wait(timeout=3)
    finally:
        server.shutdown()


# --- timeframe selection ------------------------------------------------------
# The interval whitelist is a *captured* surface, not a guessed one: every entry
# was probed against the public endpoint on 2026-09-07 and returned bars at
# exactly its step, epoch-aligned. Anything outside it must be refused rather
# than forwarded, so a crafted loopback request can never make us send an
# unverified interval string to the exchange.

class RecordingKcex:
    def __init__(self):
        self.calls = []

    def kline(self, symbol="BTC_USDT", interval="Min15", start=0, end=0, open_price_mode="LAST_CLOSE"):
        self.calls.append({"interval": interval, "start": start, "end": end})
        return {"code": 200, "data": {"t": [1], "o": [1], "h": [2], "l": [0.5], "c": [1.5], "v": [10]}}


def _serve(client):
    server = ChartServer(hub=Hub(), client=client, host="127.0.0.1", port=0)
    server.start()
    return server


def test_kline_defaults_to_min15_when_no_interval_is_given():
    client = RecordingKcex()
    server = _serve(client)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/kline", timeout=3).read()
    finally:
        server.shutdown()
    assert client.calls[0]["interval"] == "Min15"


def test_blank_interval_falls_back_to_the_default_rather_than_forwarding_it():
    """`?interval=` carries no value, so it means "unspecified", not "send an
    empty interval to the exchange". parse_qs drops the blank and the default
    applies; what matters is that "" is never forwarded."""
    client = RecordingKcex()
    server = _serve(client)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/kline?interval=", timeout=3).read()
    finally:
        server.shutdown()
    assert [c["interval"] for c in client.calls] == ["Min15"]


def test_kline_forwards_every_whitelisted_interval_with_a_matching_window():
    from bot.chart_server import KLINE_BARS, KLINE_INTERVALS

    client = RecordingKcex()
    server = _serve(client)
    try:
        for name in KLINE_INTERVALS:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.port}/kline?interval={name}", timeout=3
            ).read()
    finally:
        server.shutdown()
    assert [c["interval"] for c in client.calls] == list(KLINE_INTERVALS)
    for call in client.calls:
        step_ms = KLINE_INTERVALS[call["interval"]] * 1000
        assert call["end"] - call["start"] == KLINE_BARS * step_ms


def test_kline_refuses_an_interval_outside_the_whitelist_without_calling_the_exchange():
    client = RecordingKcex()
    server = _serve(client)
    try:
        for bogus in ("Hour1", "Week1", "Month1", "Min7", "'; DROP--", "min15", "Min15 "):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.port}/kline?interval={urllib.parse.quote(bogus)}",
                    timeout=3,
                )
                assert False, f"{bogus!r} should have been refused"
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
                assert json.loads(exc.read())["error"] == "unsupported interval"
    finally:
        server.shutdown()
    assert client.calls == [], "a rejected interval must never reach the exchange"


def test_whitelisted_intervals_are_all_epoch_aligned_steps():
    """Week1/Month1 are deliberately absent: the page buckets the live tick with
    `t - (t % step)`, which is only correct for a fixed step aligned to the
    epoch. Week1 bars land at t % 604800 == 345600 and Month1's step varies by
    calendar month, so including either would silently misplace the forming bar."""
    from bot.chart_server import KLINE_INTERVALS

    assert "Week1" not in KLINE_INTERVALS and "Month1" not in KLINE_INTERVALS
    assert "Hour1" not in KLINE_INTERVALS, "not a KCEX interval; the hourly bar is Min60"
    for name, step in KLINE_INTERVALS.items():
        assert isinstance(step, int) and step > 0
        assert 86400 % step == 0 or step == 86400, f"{name} does not tile the day"


def test_chart_page_offers_exactly_the_whitelisted_intervals():
    """The buttons and the server must not drift apart: a button the server
    refuses is a dead control, and an interval with no button is unreachable."""
    from bot.chart_server import CHART_DIR, KLINE_INTERVALS

    page = (CHART_DIR / "index.html").read_text()
    for name in KLINE_INTERVALS:
        assert f'data-interval="{name}"' in page, f"no button for {name}"
    assert page.count("data-interval=") == len(KLINE_INTERVALS)


def test_live_tick_is_gated_while_the_series_does_not_match_the_interval():
    """Regression guard for a bug found only in a browser, never by this suite.

    Switching interval clears the forming bar, but a WS tick can arrive before
    the new bars are in the series. The tick was then bucketed on the NEW grid
    (e.g. Day1 -> today 00:00) while the series still held the OLD interval's
    bars (1m, 22:14), and lightweight-charts throws "Cannot update oldest data".
    Verified fixed by driving the real page: the same rapid 1m<->1D switching
    that produced 13 uncaught exceptions now produces none.

    This asserts the guards are still *present*, not that they work -- pytest
    does not execute this JavaScript. Deleting either line would make the chart
    throw again with this suite still green, so treat a failure here as a
    prompt to re-verify in a browser, not as coverage of the behaviour.
    """
    from bot.chart_server import CHART_DIR

    page = (CHART_DIR / "index.html").read_text()
    assert "if (!seriesReady) return;" in page, "tick must not draw before the series matches"
    assert "if (barTime < newestBarTime) return;" in page, "tick must never rewind the series"
    assert "seriesReady = false;" in page, "choosing an interval must close the gate"
