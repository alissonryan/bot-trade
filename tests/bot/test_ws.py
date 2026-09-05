import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.ws import PriceUpdate, SpotSocket, channels_for, is_ack, parse_frame, subscribe_message, ws_symbol

FRAMES = [json.loads(line) for line in (ROOT / "tests/fixtures/kcex_ws_frames.jsonl").read_text().splitlines() if line.strip()]


def test_symbol_and_subscription_shape():
    assert ws_symbol("BTC_USDT") == "BTCUSDT"
    assert channels_for("BTC_USDT") == [
        "spot@public.deals.v3.api@BTCUSDT",
        "spot@public.bookTicker.v3.api@BTCUSDT",
    ]
    assert subscribe_message("BTC_USDT") == {"method": "SUBSCRIPTION", "params": channels_for("BTC_USDT")}


def test_parse_real_frames():
    ack, deals, book, mini, kline, pong = FRAMES
    assert is_ack(ack) and parse_frame(ack) is None
    assert parse_frame(deals) == PriceUpdate(last=79802.15, ts_ms=1788553416797)
    assert parse_frame(book) == PriceUpdate(bid=79562.01, ask=79562.02, ts_ms=1788569995303)
    assert parse_frame(mini) == PriceUpdate(last=79583.99, ts_ms=1788570010040)
    assert parse_frame(kline) is None  # bars still come from REST
    assert is_ack(pong) and parse_frame(pong) is None


def test_parse_rejects_garbage():
    assert parse_frame(None) is None
    assert parse_frame({"c": "spot@public.deals.v3.api@BTCUSDT", "d": {"deals": []}}) is None
    assert parse_frame({"c": "spot@public.bookTicker.v3.api@BTCUSDT", "d": {"a": "x", "b": "1"}}) is None
    assert parse_frame("not a dict") is None


class FakeWs:
    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_socket_subscribes_dispatches_and_reconnects():
    updates = []
    sessions = []
    scripts = [
        [json.dumps(FRAMES[0]), json.dumps(FRAMES[1]), json.dumps(FRAMES[2]), ConnectionError("dropped")],
        [json.dumps(FRAMES[1]), StopIteration("done")],
    ]

    def connect(url, **kwargs):
        ws = FakeWs(scripts[len(sessions)])
        sessions.append(ws)
        return ws

    sock = SpotSocket("wss://example/ws", "BTC_USDT", updates.append, connect=connect, backoff_max=0.01)

    # Run one session at a time, synchronously.
    try:
        sock._session()
    except ConnectionError:
        pass
    assert sessions[0].sent[0] == subscribe_message("BTC_USDT")
    assert updates[0].last == 79802.15
    assert updates[1].bid == 79562.01
    assert sock.frames == 3

    try:
        sock._session()
    except StopIteration:
        pass
    assert len(sessions) == 2
    assert len(updates) == 3
