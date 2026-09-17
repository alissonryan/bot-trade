"""Semantic, low-cardinality state for the observational Jev A/B variant."""

from __future__ import annotations

import math
from typing import Any

from fut.types import FutPosition, FutSnapshot

DEFAULT_ROUND_TRIP_COST_BPS = 3.0
DEFAULT_MAX_SPREAD_BPS = 3.0
FLOW_STRONG_RATIO = 0.60
FLOW_MILD_RATIO = 0.20
PRICE_FAST_COST_MULTIPLIER = 2.0
BOOK_HEAVY_IMBALANCE = 0.25
SPREAD_TIGHT_RATIO = 0.50
VOLATILITY_MARGINAL_COST_RATIO = 0.50
FUNDING_NEUTRAL_BPS = 1.0
POSITION_MID_AGE_RATIO = 1 / 3
POSITION_OLD_AGE_RATIO = 2 / 3
POSITION_NEAR_STOP_RATIO = 0.75
POSITION_FLAT_STOP_RATIO = 0.25
BOUNDARY_EPSILON_BPS = 1e-9


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _cost_bps(settings) -> float:
    value = _number(getattr(settings, "move_cost_bps", None))
    return value if value is not None and value > 0 else DEFAULT_ROUND_TRIP_COST_BPS


def _market_mid(snap: FutSnapshot) -> float | None:
    bid, ask, last = (_number(getattr(snap, key, None)) for key in ("bid", "ask", "last"))
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2
    return last if last is not None and last > 0 else None


def _flow_bucket(raw: Any) -> str:
    if not isinstance(raw, dict):
        return "unknown"
    buy, sell, cvd = (_number(raw.get(key)) for key in ("buy", "sell", "cvd"))
    if buy is None or sell is None or cvd is None or buy + sell <= 0:
        return "unknown"
    ratio = cvd / (buy + sell)
    if ratio >= FLOW_STRONG_RATIO:
        return "strong_buy"
    if ratio >= FLOW_MILD_RATIO:
        return "mild_buy"
    if ratio <= -FLOW_STRONG_RATIO:
        return "strong_sell"
    if ratio <= -FLOW_MILD_RATIO:
        return "mild_sell"
    return "balanced"


def _price_bucket(value: Any, cost_bps: float) -> str:
    value = _number(value)
    if value is None:
        return "unknown"
    fast = cost_bps * PRICE_FAST_COST_MULTIPLIER
    if value >= fast:
        return "rising_fast"
    if value >= cost_bps:
        return "rising"
    if value <= -fast:
        return "falling_fast"
    if value <= -cost_bps:
        return "falling"
    return "flat"


def _book_bucket(value: Any) -> str:
    value = _number(value)
    if value is None:
        return "unknown"
    if value >= BOOK_HEAVY_IMBALANCE:
        return "bid_heavy"
    if value <= -BOOK_HEAVY_IMBALANCE:
        return "ask_heavy"
    return "balanced"


def _spread_bucket(value: Any, settings) -> str:
    value = _number(value)
    limit = _number(getattr(settings, "max_spread_bps", None))
    limit = limit if limit is not None and limit > 0 else DEFAULT_MAX_SPREAD_BPS
    if value is None:
        return "unknown"
    if value <= limit * SPREAD_TIGHT_RATIO:
        return "tight"
    if value <= limit:
        return "normal"
    return "wide"


def _volatility_bucket(atr: Any, snap: FutSnapshot, cost_bps: float) -> str:
    atr = _number(atr)
    mid = _market_mid(snap)
    if atr is None or mid is None or mid <= 0 or atr < 0:
        return "unknown"
    atr_bps = atr / mid * 10_000
    if atr_bps + BOUNDARY_EPSILON_BPS >= cost_bps:
        return "pays_cost"
    if atr_bps + BOUNDARY_EPSILON_BPS >= cost_bps * VOLATILITY_MARGINAL_COST_RATIO:
        return "marginal"
    return "too_quiet"


def _funding_bucket(value: Any) -> str:
    value = _number(value)
    if value is None:
        return "unknown"
    funding_bps = value * 10_000
    if funding_bps >= FUNDING_NEUTRAL_BPS:
        return "long_crowded"
    if funding_bps <= -FUNDING_NEUTRAL_BPS:
        return "short_crowded"
    return "neutral"


def _position_labels(position: FutPosition, snap: FutSnapshot, *, now_ms: int, settings) -> dict[str, str]:
    if not position.is_open():
        return {}
    max_hold_s = _number(getattr(settings, "max_hold_s", None))
    opened_ms = _number(position.opened_ms)
    age_s = max(0.0, (now_ms - opened_ms) / 1000) if opened_ms is not None else 0.0
    if max_hold_s is None or max_hold_s <= 0 or opened_ms is None:
        age = "unknown"
    elif age_s < max_hold_s * POSITION_MID_AGE_RATIO:
        age = "fresh"
    elif age_s < max_hold_s * POSITION_OLD_AGE_RATIO:
        age = "mid"
    else:
        age = "old"

    mid = _market_mid(snap)
    entry = _number(position.entry)
    stop = _number(position.stop)
    if mid is None or entry is None or entry <= 0 or stop is None or mid <= 0:
        unrealized = "unknown"
    else:
        sign = 1 if position.side == "long" else -1
        pnl_bps = sign * (mid - entry) / entry * 10_000
        stop_distance_bps = abs(mid - stop) / mid * 10_000
        if stop_distance_bps <= 0:
            unrealized = "unknown"
        elif pnl_bps <= -stop_distance_bps * POSITION_NEAR_STOP_RATIO:
            unrealized = "near_stop"
        elif pnl_bps < -stop_distance_bps * POSITION_FLAT_STOP_RATIO:
            unrealized = "losing"
        elif pnl_bps <= stop_distance_bps * POSITION_FLAT_STOP_RATIO:
            unrealized = "flat"
        else:
            unrealized = "winning"
    return {"position_side": position.side if position.side in ("long", "short") else "unknown",
            "age": age, "unrealized": unrealized}


def label_state(snap: FutSnapshot, position: FutPosition, *, now_ms: int, settings) -> dict[str, str]:
    """Return only semantic buckets; missing/invalid inputs remain explicitly unknown."""
    cost_bps = _cost_bps(settings)
    flow = snap.flow if isinstance(snap.flow, dict) else {}
    returns = snap.returns_bps if isinstance(snap.returns_bps, dict) else {}
    result = {
        "flow_30s": _flow_bucket(flow.get("30s")),
        "flow_120s": _flow_bucket(flow.get("120s")),
        "price_10s": _price_bucket(returns.get("10s"), cost_bps),
        "price_60s": _price_bucket(returns.get("60s"), cost_bps),
        "book": _book_bucket(snap.imbalance),
        "spread": _spread_bucket(snap.spread_bps, settings),
        "volatility_vs_cost": _volatility_bucket(snap.atr_1m, snap, cost_bps),
        "funding_pressure": _funding_bucket(snap.funding_rate),
    }
    result.update(_position_labels(position, snap, now_ms=now_ms, settings=settings))
    return result
