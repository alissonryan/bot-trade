import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.eye import Eye
from bot.settings import Settings


class FakeKcex:
    pass


def test_apply_frame_updates_last():
    eye = Eye(FakeKcex(), Settings.from_env())
    line = (ROOT / "tests/fixtures/kcex_ws_frames.jsonl").read_text().splitlines()[0]
    eye.apply_frame(json.loads(line))
    assert eye.last == 80000.1
    assert eye.ws_ok is True
    assert eye.bid == 80000.0
