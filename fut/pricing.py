"""One price basis shared by the collar (sizing) and the paper ledger (fills)."""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, ROUND_UP, Decimal


def fill_price(*, bid: float, ask: float, last: float, buy: bool, slippage_bps: float) -> float | None:
    if not math.isfinite(slippage_bps) or slippage_bps < 0:
        return None
    base = ask if buy else bid
    if not (math.isfinite(base) and base > 0):
        base = last
    if not (math.isfinite(base) and base > 0):
        return None
    factor = slippage_bps / 10_000.0
    return base * (1 + factor) if buy else base * (1 - factor)


def liquidation_price(side: str, entry: float, leverage: int, mmr: float) -> float:
    if side == "long":
        return entry * (1 - 1 / leverage + mmr)
    if side == "short":
        return entry * (1 + 1 / leverage - mmr)
    raise ValueError(f"unknown side {side!r}")


def round_to_unit(price: float, unit: float, *, up: bool) -> float:
    quantum = Decimal(str(unit))
    steps = (Decimal(str(price)) / quantum).quantize(Decimal(1), rounding=ROUND_UP if up else ROUND_DOWN)
    return float(steps * quantum)
