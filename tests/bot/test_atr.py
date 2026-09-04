from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.atr import atr
from bot.types import Bar


def test_atr_needs_period_plus_one_bars():
    bars = [Bar(t=i, o=10, h=12, l=9, c=11) for i in range(14)]
    assert atr(bars, period=14) is None


def test_atr_constant_range():
    bars = []
    for i in range(20):
        bars.append(Bar(t=i, o=100, h=102, l=100, c=101))
    value = atr(bars, period=14)
    assert value is not None
    assert abs(value - 2.0) < 1e-6


def test_atr_uses_previous_close_gap():
    # prev close 110, next bar 100-101 so TR = |100-110|=10 not H-L=1
    bars = [Bar(t=0, o=110, h=111, l=109, c=110)]
    for i in range(1, 15):
        bars.append(Bar(t=i, o=100, h=101, l=100, c=100.5))
    value = atr(bars, period=14)
    assert value is not None
    assert value > 1.0  # would be ~1 if only H-L

