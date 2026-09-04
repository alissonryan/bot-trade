from __future__ import annotations

from bot.settings import Settings
from bot.types import GateResult, Snapshot, TradeIntent

ALLOWED_ACTIONS = {"BUY", "SELL", "HOLD"}


def _round_qty(qty: float, scale: int) -> str:
    q = round(qty, scale)
    fmt = f"{{:.{scale}f}}"
    return fmt.format(q)


def _stop_price(entry: float, atr_value: float, settings: Settings) -> str:
    raw = settings.atr_mult * atr_value
    lo = entry * settings.min_stop_pct
    hi = entry * settings.max_stop_pct
    dist = min(max(raw, lo), hi)
    return f"{entry - dist:.2f}"


def decide(
    intent: TradeIntent,
    snap: Snapshot,
    settings: Settings,
    *,
    session_ok: bool,
    day_pnl_usdt: float,
) -> GateResult:
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
        if snap.bot_qty <= 0:
            return GateResult(False, "flat", "SELL")
        return GateResult(
            True,
            "ok_close",
            "SELL",
            qty=_round_qty(snap.bot_qty, settings.qty_scale),
            notional=round(snap.bot_qty * snap.last, 8),
            stop_price=None,
        )

    if snap.stale:
        return GateResult(False, "stale", intent.action)
    if day_pnl_usdt <= -abs(settings.max_day_loss_usdt):
        return GateResult(False, "day_loss", "BUY")
    if snap.bot_qty > 0:
        return GateResult(False, "already_long", "BUY")
    if snap.atr is None or snap.atr <= 0 or snap.last <= 0:
        return GateResult(False, "atr", "BUY")

    cap_pct = settings.max_portfolio_pct * snap.free_usdt
    notional = min(settings.max_order_usdt, cap_pct)
    if notional <= 0:
        return GateResult(False, "no_cash", "BUY")
    qty = notional / snap.last
    qty_s = _round_qty(qty, settings.qty_scale)
    if float(qty_s) <= 0:
        return GateResult(False, "dust", "BUY")
    notional = float(qty_s) * snap.last
    stop = _stop_price(snap.last, snap.atr, settings)
    return GateResult(True, "ok_buy", "BUY", qty=qty_s, notional=notional, stop_price=stop)
