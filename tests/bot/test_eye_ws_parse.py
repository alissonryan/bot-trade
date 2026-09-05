import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.eye import Eye
from bot.settings import Settings

FRAMES = [json.loads(line) for line in (ROOT / "tests/fixtures/kcex_ws_frames.jsonl").read_text().splitlines() if line.strip()]


class FakeKcex:
    base_url = "https://www.kcex.com"


def test_real_frames_update_last_bid_ask():
    eye = Eye(FakeKcex(), Settings.from_env())
    ack, deals, book, *_ = FRAMES
    assert eye.apply_frame(ack) is False
    assert eye.ws_ok is False
    assert eye.apply_frame(deals) is True
    assert eye.last == 79802.15
    assert eye.ws_ok is True
    assert eye.apply_frame(book) is True
    assert eye.bid == 79562.01
    assert eye.ask == 79562.02
    snap = eye.snapshot()
    assert snap.stale is False
    assert snap.ws_ok is True
    assert snap.spread > 0


def test_connect_ws_is_noop_when_disabled():
    d = Settings.from_env().__dict__.copy()
    d["ws_enabled"] = False
    eye = Eye(FakeKcex(), Settings(**d))
    assert eye.connect_ws() is None
    assert eye.socket is None
