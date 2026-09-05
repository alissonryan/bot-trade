import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.eye import Eye
from bot.settings import Settings
from kcex.ws import parse_frame

FRAMES = [
    json.loads(line)
    for line in (ROOT / "tests/fixtures/kcex_ws_frames.jsonl").read_text().splitlines()
    if line.strip()
]


def _frame(channel_fragment: str) -> dict:
    """Look frames up by channel rather than by position, so adding a captured
    frame to the fixture cannot silently repoint a test at a different one."""
    for msg in FRAMES:
        if channel_fragment in str(msg.get("c") or ""):
            return msg
    raise AssertionError(f"no fixture frame for {channel_fragment!r}")


class FakeKcex:
    base_url = "https://www.kcex.com"


def test_real_frames_update_last_bid_ask():
    """Exercises the production path: parse_frame -> apply_event -> Hub -> Eye."""
    eye = Eye(FakeKcex(), Settings.from_env())

    ack = {"id": 0, "code": 0, "msg": "spot@public.deals.v3.api@BTCUSDT"}
    eye.apply_event(parse_frame(ack))
    assert eye.ws_ok is False  # an ack carries no price

    eye.apply_event(parse_frame(_frame("deals.v3.api")))
    assert eye.last == 79802.15
    assert eye.ws_ok is True

    eye.apply_event(parse_frame(_frame("bookTicker.v3.api")))
    assert eye.bid == 79562.01
    assert eye.ask == 79562.02

    snap = eye.snapshot()
    assert snap.ws_ok is True
    assert snap.spread > 0
    # Staleness is measured from the frame's own exchange timestamp, not from
    # arrival time, so replayed captures correctly read as stale.
    assert snap.stale is True


def test_freshness_follows_the_frame_timestamp():
    import time

    from kcex.ws import TickerEvent

    eye = Eye(FakeKcex(), Settings.from_env())
    now_ms = int(time.time() * 1000)
    eye.apply_event(TickerEvent(last=79802.15, ts_ms=now_ms, symbol="BTC_USDT"))
    assert eye.snapshot().stale is False

    stale_ms = Settings.from_env().stale_ms
    eye.apply_event(TickerEvent(last=79802.15, ts_ms=now_ms - stale_ms - 1000, symbol="BTC_USDT"))
    assert eye.snapshot().stale is True


def test_book_ticker_alone_does_not_mark_the_feed_healthy():
    """A depth-only frame carries no `last`, so on its own it must never make an
    Eye that has never seen a price read as a fresh, healthy feed."""
    eye = Eye(FakeKcex(), Settings.from_env())
    eye.apply_event(parse_frame(_frame("bookTicker.v3.api")))
    assert eye.ws_ok is False
    assert eye.last == 0.0
    assert eye.snapshot().stale is True


def test_apply_frame_still_reads_the_synthetic_shape():
    """`apply_frame` is the permissive `{ch,last,bid,ask}` parser kept for the
    older synthetic fixture line; the live socket goes through apply_event."""
    eye = Eye(FakeKcex(), Settings.from_env())
    synthetic = FRAMES[0]
    assert "ch" in synthetic
    assert eye.apply_frame(synthetic) is True
    assert eye.last == 80000.1
    assert eye.bid == 80000.0
    assert eye.ask == 80000.2
    assert eye.apply_frame({"nothing": "useful"}) is False


def test_connect_ws_is_noop_when_disabled():
    d = Settings.from_env().__dict__.copy()
    d["ws_enabled"] = False
    eye = Eye(FakeKcex(), Settings(**d))
    assert eye.connect_ws() is None
    assert eye._ws_thread_started is False


def test_connect_ws_starts_at_most_one_thread(monkeypatch):
    """main() and _loop() both reach for the socket; this process must end up
    with exactly one KCEX connection."""
    started = []
    eye = Eye(FakeKcex(), Settings.from_env())

    class FakeThread:
        def __init__(self, *a, **kw):
            started.append(kw.get("target"))

        def start(self):
            pass

    monkeypatch.setattr("bot.eye.threading.Thread", FakeThread)
    eye.start_ws_thread()
    eye.connect_ws()
    eye.start_ws_thread()
    assert len(started) == 1
