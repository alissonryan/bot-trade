import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kcex.ws import DealEvent, DepthEvent, TickerEvent, parse_frame, subscribe_message


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
    assert "spot@public.limit.precision.depth@BTC_USDT@0.01" in body["params"]
