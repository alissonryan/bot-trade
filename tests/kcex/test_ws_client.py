import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kcex.ws import PublicSpotWs, TickerEvent, parse_text


class FakeSock:
    def __init__(self, incoming: list[str]):
        self.incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    def send(self, text: str) -> None:
        self.sent.append(text)

    def recv(self, timeout: float | None = None) -> str:
        if not self.incoming:
            raise ConnectionError("closed")
        return self.incoming.pop(0)

    def close(self) -> None:
        self.closed = True


class TimeoutThenMessageSock:
    """Simulates a real websockets.sync connection: recv(timeout=...) raises
    the builtin TimeoutError on quiet stretches (not a connection failure),
    then eventually returns a queued message."""

    def __init__(self, timeouts_before_message: int, incoming: list[str]):
        self.timeouts_left = timeouts_before_message
        self.incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    def send(self, text: str) -> None:
        self.sent.append(text)

    def recv(self, timeout: float | None = None) -> str:
        if self.timeouts_left > 0:
            self.timeouts_left -= 1
            raise TimeoutError("timed out")
        if not self.incoming:
            raise ConnectionError("closed")
        return self.incoming.pop(0)

    def close(self) -> None:
        self.closed = True


def test_run_once_subscribes_and_yields_ticker():
    frame = {
        "c": "spot@public.miniTicker@BTC_USDT@UTC+0",
        "s": "BTC_USDT",
        "t": 1,
        "d": {"p": "100.5", "s": "BTC_USDT"},
    }
    sock = FakeSock([json.dumps(frame)])
    events = []
    ws = PublicSpotWs(url="wss://example.invalid/ws", symbol="BTC_USDT", connect=lambda url: sock)
    ws.pump(on_event=events.append, on_error=lambda e: None, max_messages=1)
    assert any(isinstance(e, TickerEvent) and e.last == 100.5 for e in events)
    sub = json.loads(sock.sent[0])
    assert sub["method"] == "SUBSCRIPTION"
    assert sock.closed is True  # normal loop exit still closes the socket


def test_bad_json_does_not_raise():
    sock = FakeSock(["not-json", json.dumps({"msg": "PONG"})])
    events = []
    ws = PublicSpotWs(url="wss://example.invalid/ws", symbol="BTC_USDT", connect=lambda url: sock)
    ws.pump(on_event=events.append, on_error=lambda e: None, max_messages=2)
    assert events == []


def test_recv_timeout_is_not_fatal_and_pump_continues():
    frame = {
        "c": "spot@public.miniTicker@BTC_USDT@UTC+0",
        "s": "BTC_USDT",
        "t": 1,
        "d": {"p": "100.5", "s": "BTC_USDT"},
    }
    sock = TimeoutThenMessageSock(timeouts_before_message=3, incoming=[json.dumps(frame)])
    events = []
    errors = []
    ws = PublicSpotWs(url="wss://example.invalid/ws", symbol="BTC_USDT", connect=lambda url: sock)
    ws.pump(on_event=events.append, on_error=errors.append, max_messages=1)
    assert errors == []
    assert any(isinstance(e, TickerEvent) and e.last == 100.5 for e in events)
    assert sock.timeouts_left == 0
    assert sock.closed is True


def test_recv_exception_calls_on_error_and_closes_socket():
    """Regression: the recv-failure path must report AND close.

    Previously pump() returned straight out of on_error without closing,
    leaking one connection (and its background thread) per reconnect cycle.
    """
    sock = FakeSock([])  # first recv() raises ConnectionError
    events = []
    errors = []
    ws = PublicSpotWs(url="wss://example.invalid/ws", symbol="BTC_USDT", connect=lambda url: sock)
    ws.pump(on_event=events.append, on_error=errors.append, max_messages=5)
    assert events == []
    assert len(errors) == 1
    assert isinstance(errors[0], ConnectionError)
    assert sock.closed is True


def test_send_failure_still_closes_socket():
    class SendBoomSock(FakeSock):
        def send(self, text: str) -> None:
            raise ConnectionError("send failed")

    sock = SendBoomSock([])
    ws = PublicSpotWs(url="wss://example.invalid/ws", symbol="BTC_USDT", connect=lambda url: sock)
    try:
        ws.pump(on_event=lambda e: None, on_error=lambda e: None, max_messages=1)
        assert False, "should have propagated"
    except ConnectionError:
        pass
    assert sock.closed is True
