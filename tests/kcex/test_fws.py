import json
from pathlib import Path

from kcex.fws import (
    FutDeal,
    FutDepth,
    FutFair,
    FutTicker,
    OrderBook,
    PublicFuturesWs,
    parse_frame,
    parse_text,
    ping_message,
    subscribe_messages,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kcex_fut_ws_frames.jsonl"


def frames():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def first(channel):
    return next(f for f in frames() if f["channel"] == channel)


def test_ack_and_pong_frames_yield_no_events():
    acks = [f for f in frames() if f["channel"] in ("rs.sub.depth", "pong")]
    assert len(acks) == 2
    assert all(parse_frame(f) == [] for f in acks)


def test_ticker_frame():
    assert parse_frame(first("push.ticker")) == [FutTicker(
        ts_ms=1789614158008, last=76353.8, bid=76353.8, ask=76353.9,
        fair=76354.2, index=76387.6, funding_rate=0.000071)]


def test_fair_price_frame():
    assert parse_frame(first("push.fair.price")) == [FutFair(1789614158366, 76355.6)]


def test_deal_side_follows_captured_prints():
    deals = [e for f in frames() if f["channel"] == "push.deal" for e in parse_frame(f)]
    assert deals == [
        FutDeal(1789614159388, 76356.9, 2200, "buy"),
        FutDeal(1789614161493, 76356.8, 1499, "sell"),
    ]


def test_deal_with_unknown_side_is_dropped():
    assert parse_frame({"channel": "push.deal", "data": {"p": 1, "v": 1, "T": 9, "t": 1}}) == []


def test_deal_list_payload_is_supported():
    msg = {"channel": "push.deal", "data": [
        {"p": 10, "v": 1, "T": 2, "t": 1}, {"p": 11, "v": 2, "T": 1, "t": 2}]}
    assert [d.side for d in parse_frame(msg)] == ["buy", "sell"]


def test_depth_frames_parse_versions_and_zero_volume():
    depths = [e for f in frames() if f["channel"] == "push.depth" for e in parse_frame(f)]
    assert [d.version for d in depths] == [14452998433, 14452998434, 14452998435, 14452998671]
    assert depths[0].asks == ((76356.9, 1286398),)
    assert depths[-1].bids == ((76352.9, 0),)


def test_parse_text_ignores_garbage():
    assert parse_text("not json") == []
    assert parse_text("[1, 2]") == []


def test_subscribe_and_ping_messages():
    msgs = subscribe_messages("BTC_USDT")
    assert [m["method"] for m in msgs] == ["sub.ticker", "sub.deal", "sub.depth", "sub.fair.price"]
    assert all(m["param"] == {"symbol": "BTC_USDT"} for m in msgs)
    assert ping_message() == {"method": "ping"}


def _delta(version, bids=(), asks=()):
    return FutDepth(0, version, tuple(bids), tuple(asks))


def test_book_applies_contiguous_deltas_and_removes_zero_volume():
    book = OrderBook()
    book.load_snapshot(10, [(100.0, 5), (99.0, 3)], [(101.0, 4)])
    assert book.apply(_delta(11, bids=[(100.0, 0)], asks=[(100.5, 2)]))
    assert book.version == 11
    assert book.best_bid() == 99.0
    assert book.best_ask() == 100.5
    assert book.levels("ask") == [(100.5, 2), (101.0, 4)]
    assert book.levels("bid") == [(99.0, 3)]


def test_book_skips_versions_already_in_the_snapshot():
    book = OrderBook()
    book.load_snapshot(10, [(100.0, 5)], [(101.0, 4)])
    assert book.apply(_delta(9, bids=[(100.0, 0)]))
    assert book.best_bid() == 100.0


def test_book_gap_unsyncs_and_clears():
    book = OrderBook()
    book.load_snapshot(10, [(100.0, 5)], [(101.0, 4)])
    assert book.apply(_delta(12)) is False
    assert not book.synced
    assert book.best_bid() is None and book.best_ask() is None


def test_unsynced_book_refuses_deltas():
    assert OrderBook().apply(_delta(1)) is False


class FakeSock:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout=None):
        if not self.incoming:
            raise ConnectionError("closed")
        return self.incoming.pop(0)

    def close(self):
        self.closed = True


def test_pump_subscribes_every_channel_and_emits_parsed_events():
    sock = FakeSock(FIXTURE.read_text().splitlines())
    events, errors = [], []
    ws = PublicFuturesWs("wss://example", "BTC_USDT", lambda url: sock)
    ws.pump(on_event=events.append, on_error=errors.append)
    assert [json.loads(s)["method"] for s in sock.sent[:4]] == [
        "sub.ticker", "sub.deal", "sub.depth", "sub.fair.price"]
    assert len(events) == 8  # 1 ticker, 1 fair, 2 deals, 4 depth
    assert len(errors) == 1
    assert sock.closed
