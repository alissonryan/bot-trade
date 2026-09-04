from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.cycle import due
from bot.settings import Settings


def test_due_on_timer():
    s = Settings.from_env()
    assert due(now_ms=1_000_000, last_llm_ms=1_000_000 - 15 * 60_000, last_px=100, px=100, settings=s) is True


def test_not_due_early():
    s = Settings.from_env()
    assert due(now_ms=1_000_000, last_llm_ms=1_000_000 - 60_000, last_px=100, px=100, settings=s) is False


def test_due_on_wake_move():
    s = Settings.from_env()
    assert due(now_ms=1_000_000, last_llm_ms=1_000_000 - 1000, last_px=100, px=100.5, settings=s) is True
