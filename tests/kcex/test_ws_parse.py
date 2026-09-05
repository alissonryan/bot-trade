import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kcex.ws import (
    DealEvent,
    DepthEvent,
    TickerEvent,
    _unws_symbol,
    parse_frame,
    subscribe_message,
    ws_symbol,
)


def test_parse_miniticker():
    lines = (ROOT / "tests/fixtures/kcex_ws_frames.jsonl").read_text().splitlines()
    msg = json.loads(lines[1])
    ev = parse_frame(msg)
    assert isinstance(ev, TickerEvent)
    assert ev.last == 79801.99
    assert ev.symbol == "BTC_USDT"
    assert ev.ts_ms == 1788553351046


def test_parse_deal_sell():
    lines = (ROOT / "tests/fixtures/kcex_ws_frames.jsonl").read_text().splitlines()
    ev = parse_frame(json.loads(lines[2]))
    assert isinstance(ev, DealEvent)
    assert ev.price == 79801.99
    assert ev.qty == 0.00239
    assert ev.side == "sell"
    assert ev.ts_ms == 1788553351631


def test_parse_pong_and_ack_ignored():
    assert parse_frame({"msg": "PONG"}) is None
    assert parse_frame({"id": 3, "code": 0, "msg": "spot@public.aggre.deals@BTC_USDT"}) is None
    assert parse_frame({"not": "json-shape"}) is None


def test_parse_depth_best_or_none():
    ev = parse_frame(
        {
            "c": "spot@public.limit.precision.depth@BTC_USDT@0.01",
            "d": {
                "bids": [{"p": "79801.90", "q": "1"}],
                "asks": [{"p": "79802.00", "q": "1"}],
            },
        }
    )
    assert isinstance(ev, DepthEvent)
    assert ev.bid == 79801.9
    assert ev.ask == 79802.0
    assert parse_frame({"c": "spot@public.limit.precision.depth@BTC_USDT@0.01", "d": {}}) is None or (
        isinstance(parse_frame({"c": "spot@public.limit.precision.depth@BTC_USDT@0.01", "d": {}}), DepthEvent)
        and parse_frame({"c": "spot@public.limit.precision.depth@BTC_USDT@0.01", "d": {}}).bid is None
    )


def test_subscribe_message():
    body = subscribe_message("BTC_USDT")
    assert body["method"] == "SUBSCRIPTION"
    assert "spot@public.miniTicker@BTC_USDT@UTC+0" in body["params"]
    assert "spot@public.aggre.deals@BTC_USDT" in body["params"]
    # Top of book rides bookTicker (v3 naming, symbol without the underscore),
    # not the depth ladder.
    assert "spot@public.bookTicker.v3.api@BTCUSDT" in body["params"]
    assert not any("limit.precision.depth" in p for p in body["params"])


def test_ws_symbol_roundtrip():
    assert ws_symbol("BTC_USDT") == "BTCUSDT"
    assert _unws_symbol("BTCUSDT") == "BTC_USDT"
    assert _unws_symbol("BTC_USDT") == "BTC_USDT"


def test_parse_book_ticker():
    ev = parse_frame(
        {
            "c": "spot@public.bookTicker.v3.api@BTCUSDT",
            "d": {"A": "6.40051", "B": "10.95406", "a": "79711.8", "b": "79711.79"},
            "s": "BTCUSDT",
            "t": 1788613256150,
        }
    )
    assert isinstance(ev, DepthEvent)
    assert ev.bid == 79711.79
    assert ev.ask == 79711.8
    # Normalised so Hub.symbol does not flip between the two conventions.
    assert ev.symbol == "BTC_USDT"


def test_parse_book_ticker_rejects_corrupt_side():
    # `a` is present but unparseable: reject the frame rather than applying
    # only the bid.
    assert parse_frame({"c": "spot@public.bookTicker.v3.api@BTCUSDT", "d": {"a": "x", "b": "1"}}) is None
    assert parse_frame({"c": "spot@public.bookTicker.v3.api@BTCUSDT", "d": {}}) is None


def test_parse_deals_v3():
    ev = parse_frame(
        {
            "c": "spot@public.deals.v3.api@BTCUSDT",
            "d": {"deals": [{"p": "79711.80", "v": "0.00036", "S": 1, "t": 1788613253942}]},
            "s": "BTCUSDT",
            "t": 1788613253953,
        }
    )
    assert isinstance(ev, DealEvent)
    assert ev.price == 79711.8
    assert ev.qty == 0.00036  # v3 carries qty as `v`, not `q`
    assert ev.side == "buy"  # v3 carries side as `S` (1 buy / 2 sell)
    assert ev.symbol == "BTC_USDT"
    assert parse_frame({"c": "spot@public.deals.v3.api@BTCUSDT", "d": {"deals": []}}) is None
