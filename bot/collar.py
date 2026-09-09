"""Risk collar: pure rules between the LLM intent and any order.

Check order matters. Exits (SELL of the bot position) are evaluated before the
stale-market, day-loss and confidence checks so nothing the collar does can trap the
bot in a position. Entries go through every check.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN

from bot.settings import Settings
from bot.types import GateResult, Snapshot, SymbolRules, TradeIntent

ALLOWED_ACTIONS = {"BUY", "SELL", "HOLD"}


def executable_entry_price(ask: float, last: float, slippage_bps: float = 0.0) -> float:
    """The price a BUY should be sized, stopped and targeted against.

    Mirrors the price PaperHands actually pays: the best ask, or ``last`` when
    the ask is missing/non-finite/non-positive, marked up by slippage. This is
    the single source of truth for both sides -- sizing off ``last`` while
    filling off ``ask*(1+slippage)`` is exactly what let a nominal 20 USDT gate
    debit 22.011 USDT when last=100/ask=110 at 5 bps slippage (the reproduced
    Terra-review blocker). Callers that do not model slippage pass 0.
    """
    base = ask if math.isfinite(ask) and ask > 0 else last
    return base * (1 + slippage_bps / 10_000.0)


def _round_qty(qty: float, scale: int) -> str:
    # Never add a venue tick to a balance or capped order. Convert via str so
    # an exact decimal tick (e.g. 0.00023) is not lost to binary float noise.
    quantum = Decimal(1).scaleb(-scale)
    return format(Decimal(str(qty)).quantize(quantum, rounding=ROUND_DOWN), "f")


def _stop_price(entry: float, atr_value: float, settings: Settings, price_scale: int) -> str:
    raw = settings.atr_mult * atr_value
    lo = entry * settings.min_stop_pct
    hi = entry * settings.max_stop_pct
    dist = min(max(raw, lo), hi)
    quantum = Decimal(1).scaleb(-price_scale)
    return format(Decimal(str(entry - dist)).quantize(quantum, rounding=ROUND_DOWN), "f")


def stop_for_entry(entry: float, atr_value: float, settings: Settings, rules: SymbolRules | None = None) -> str:
    """Stop price for a long entered at ``entry``; used again after the real fill is known."""
    return _stop_price(entry, atr_value, settings, rules.price_scale if rules else 2)


def take_profit_for_entry(entry: float, atr_value: float, settings: Settings, rules: SymbolRules | None = None) -> str | None:
    """Opt-in local TP; re-evaluate from the confirmed fill, not a forming bar."""
    if settings.tp_atr_mult <= 0:
        return None
    distance = min(max(settings.tp_atr_mult * atr_value, entry * settings.min_tp_pct),
                   entry * settings.max_tp_pct)
    quantum = Decimal(1).scaleb(-(rules.price_scale if rules else 2))
    return format(Decimal(str(entry + distance)).quantize(quantum, rounding=ROUND_DOWN), "f")


def decide(
    intent: TradeIntent,
    snap: Snapshot,
    settings: Settings,
    *,
    session_ok: bool,
    day_pnl_usdt: float,
    unrealized_pnl_usdt: float = 0.0,
    rules: SymbolRules | None = None,
    last_loss_exit_ms: int | None = None,
    now_ms: int | None = None,
    entry_slippage_bps: float | None = None,
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
        # SELL gate.qty is advisory/audit data: LiveHands._sell sizes again
        # from its resident position, using its own float-boundary tolerance.
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
    # NOTE: `unrealized_pnl_usdt` cannot change the outcome as the bot stands.
    # It is non-zero only while a position is open (cycle.unrealized_pnl returns
    # 0.0 at qty <= 0), and an open position already fails `already_long` below,
    # so the term only ever changes which rejection label is reported. It is kept
    # because it is correct in principle and becomes load-bearing the moment the
    # bot can hold more than one position or add to one. Making an unrealized
    # drawdown actually *do* something means forcing an exit, which is a product
    # decision, not a bug fix -- do not add it here without asking the owner.
    if day_pnl_usdt + unrealized_pnl_usdt <= -abs(settings.max_day_loss_usdt):
        return GateResult(False, "day_loss", "BUY")
    # parse_intent() already rejects a non-finite confidence, but this gate is
    # the last line of defense before an order is sized: every comparison
    # against NaN/Infinity is False, so `confidence < min_confidence` alone
    # would let a corrupted or directly-constructed intent through no matter
    # how high MIN_CONFIDENCE is set.
    if not math.isfinite(intent.confidence) or intent.confidence < settings.min_confidence:
        return GateResult(False, "confidence", "BUY")
    if snap.bot_qty > 0:
        return GateResult(False, "already_long", "BUY")
    # Only entries wait, and only after a LOSING exit. A profitable exit does
    # not arm this: re-entering the same direction while the move continues is
    # riding it, not revenge, and `already_long` plus the fresh-signal path
    # already stop stacking. Blocking after a win only ever cancels profit --
    # Rafael Vargas measured that cost on his own book and retired the
    # post-any-exit form (Apex Brief v17, Rule 3: NO CHASING -> NO REVENGE).
    # Wall-clock time comes from the caller, not a potentially stale quote;
    # missing time fails closed for BUY.
    if settings.cooldown_minutes > 0 and last_loss_exit_ms is not None:
        if now_ms is None or now_ms - last_loss_exit_ms < settings.cooldown_minutes * 60_000:
            return GateResult(False, "cooldown", "BUY")
    if snap.atr is None or snap.atr <= 0 or snap.last <= 0:
        return GateResult(False, "atr", "BUY")

    slip_bps = entry_slippage_bps
    if slip_bps is None:
        # Live has no modeled slippage buffer of its own -- the confirmed fill
        # recomputes stop/TP for real anyway (see LiveHands._buy). Paper's
        # buffer must match PaperHands.execute() exactly, or sizing off one
        # price while filling off another reopens the debit-over-cap bug.
        slip_bps = settings.paper_slippage_bps if settings.mode == "paper" else 0.0
    # Negative slippage is not a real market condition for a market BUY (it
    # would mean paying LESS than ask on a worse fill) -- a computed
    # entry_price can still land positive (e.g. -1 bps -> factor 0.9999) and
    # slip past the finite/positive check below, so the CONFIGURED value is
    # rejected here, before it ever reaches the price computation.
    if not math.isfinite(slip_bps) or slip_bps < 0:
        return GateResult(False, "no_price", "BUY")
    entry_price = executable_entry_price(snap.ask, snap.last, slip_bps)
    # A pathological slippage config (NaN, or <= -10000 bps inverting/
    # zeroing the price) must never reach qty/Decimal math: `notional /
    # entry_price` would divide by zero or NaN, and Decimal(str(nan))
    # raises InvalidOperation on quantize(). snap.last/snap.atr are already
    # validated above, so the only way entry_price goes bad here is a broken
    # slippage input -- refuse the same way an invalid quote is refused.
    if not math.isfinite(entry_price) or entry_price <= 0:
        return GateResult(False, "no_price", "BUY")

    cap_pct = settings.max_portfolio_pct * snap.free_usdt
    notional = min(settings.max_order_usdt, cap_pct)
    if rules and rules.max_amount:
        notional = min(notional, rules.max_amount)
    if notional <= 0:
        return GateResult(False, "no_cash", "BUY")
    qty = notional / entry_price
    qty_s = _round_qty(qty, qty_scale)
    if float(qty_s) <= 0:
        return GateResult(False, "dust", "BUY")
    notional = float(qty_s) * entry_price
    if rules and rules.min_amount and notional < rules.min_amount:
        return GateResult(False, "min_notional", "BUY")
    stop = _stop_price(entry_price, snap.atr, settings, price_scale)
    target = take_profit_for_entry(entry_price, snap.atr, settings, rules)
    return GateResult(True, "ok_buy", "BUY", qty=qty_s, notional=notional, stop_price=stop,
                      take_profit_price=target)
