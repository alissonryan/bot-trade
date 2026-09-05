"""Risk collar: pure rules between the LLM intent and any order.

Check order matters. Exits (SELL of the bot position) are evaluated before the
stale-market, day-loss and confidence checks so nothing the collar does can trap the
bot in a position. Entries go through every check.
"""

from __future__ import annotations

from bot.settings import Settings
from bot.types import GateResult, Snapshot, SymbolRules, TradeIntent

ALLOWED_ACTIONS = {"BUY", "SELL", "HOLD"}


def _round_qty(qty: float, scale: int) -> str:
    q = round(qty, scale)
    fmt = f"{{:.{scale}f}}"
    return fmt.format(q)


def _stop_price(entry: float, atr_value: float, settings: Settings, price_scale: int) -> str:
    raw = settings.atr_mult * atr_value
    lo = entry * settings.min_stop_pct
    hi = entry * settings.max_stop_pct
    dist = min(max(raw, lo), hi)
    return f"{entry - dist:.{price_scale}f}"


def stop_for_entry(entry: float, atr_value: float, settings: Settings, rules: SymbolRules | None = None) -> str:
    """Stop price for a long entered at ``entry``; used again after the real fill is known."""
    return _stop_price(entry, atr_value, settings, rules.price_scale if rules else 2)


def decide(
    intent: TradeIntent,
    snap: Snapshot,
    settings: Settings,
    *,
    session_ok: bool,
    day_pnl_usdt: float,
    unrealized_pnl_usdt: float = 0.0,
    rules: SymbolRules | None = None,
) -> GateResult:
    qty_scale = rules.qty_scale if rules else settings.qty_scale
    price_scale = rules.price_scale if rules else 2

    if settings.symbol != "BTC_USDT":
        return GateResult(False, "symbol", intent.action)
    if settings.mode not in {"paper", "live"}:
        return GateResult(False, "mode", intent.action)
    if not session_ok:
        return GateResult(False, "session", intent.action)
    if intent.action not in ALLOWED_ACTIONS:
        return GateResult(False, "action", intent.action)
    if intent.action == "HOLD":
        return GateResult(False, "hold", "HOLD")

    if intent.action == "SELL":
        # Same invalid-price guard the BUY branch applies via its `atr` rule:
        # a zero/negative `last` (e.g. a WS feed that went "up" without ever
        # carrying a price) must never size or price a live order.
        if snap.last <= 0:
            return GateResult(False, "no_price", "SELL")
        if snap.bot_qty <= 0:
            return GateResult(False, "flat", "SELL")
        return GateResult(
            True,
            "ok_close",
            "SELL",
            qty=_round_qty(snap.bot_qty, qty_scale),
            notional=round(snap.bot_qty * snap.last, 8),
            stop_price=None,
        )

    # BUY from here on.
    if snap.stale:
        return GateResult(False, "stale", "BUY")
    if day_pnl_usdt + unrealized_pnl_usdt <= -abs(settings.max_day_loss_usdt):
        return GateResult(False, "day_loss", "BUY")
    if intent.confidence < settings.min_confidence:
        return GateResult(False, "confidence", "BUY")
    if snap.bot_qty > 0:
        return GateResult(False, "already_long", "BUY")
    if snap.atr is None or snap.atr <= 0 or snap.last <= 0:
        return GateResult(False, "atr", "BUY")

    cap_pct = settings.max_portfolio_pct * snap.free_usdt
    notional = min(settings.max_order_usdt, cap_pct)
    if rules and rules.max_amount:
        notional = min(notional, rules.max_amount)
    if notional <= 0:
        return GateResult(False, "no_cash", "BUY")
    qty = notional / snap.last
    qty_s = _round_qty(qty, qty_scale)
    if float(qty_s) <= 0:
        return GateResult(False, "dust", "BUY")
    notional = float(qty_s) * snap.last
    if rules and rules.min_amount and notional < rules.min_amount:
        return GateResult(False, "min_notional", "BUY")
    stop = _stop_price(snap.last, snap.atr, settings, price_scale)
    return GateResult(True, "ok_buy", "BUY", qty=qty_s, notional=notional, stop_price=stop)
