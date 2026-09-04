from __future__ import annotations

from bot.types import Bar


def atr(bars: list[Bar], period: int = 14) -> float | None:
    if period < 1 or len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        high = bars[i].h
        low = bars[i].l
        prev_close = bars[i - 1].c
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    window = trs[-period:]
    if len(window) < period:
        return None
    return sum(window) / period
