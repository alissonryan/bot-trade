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

    def send(self, text: str) -> None:
        self.sent.append(text)

    def recv(self) -> str:
        if not self.incoming:
            raise ConnectionError("closed")
        return self.incoming.pop(0)


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


def test_bad_json_does_not_raise():
    sock = FakeSock(["not-json", json.dumps({"msg": "PONG"})])
    events = []
    ws = PublicSpotWs(url="wss://example.invalid/ws", symbol="BTC_USDT", connect=lambda url: sock)
    ws.pump(on_event=events.append, on_error=lambda e: None, max_messages=2)
    assert events == []
