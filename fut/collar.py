"""Futures paper risk gate. Pure rules between the LLM intent and the paper ledger.

CLOSE is evaluated before every freshness/loss/confidence check so nothing here can
trap the bot in a position. Entries go through every check, in the spec's order.
"""

from __future__ import annotations

import math

from fut.pricing import fill_price, liquidation_price, round_to_unit
from fut.settings import MAX_LEVERAGE, FutSettings
from fut.types import FutGate, FutIntent, FutPosition, FutSnapshot
from kcex.fapi import ContractSpec

ACTIONS = {"LONG", "SHORT", "CLOSE", "HOLD"}


def check(intent: FutIntent, snap: FutSnapshot, *, position: FutPosition, balance: float,
          day_pnl_usdt: float, spec: ContractSpec, settings: FutSettings) -> FutGate:
    action = intent.action
    if action not in ACTIONS:
        return FutGate(False, "action", str(action))
    if action == "HOLD":
        return FutGate(False, "hold", "HOLD")
    if action == "CLOSE":
        if not position.is_open():
            return FutGate(False, "flat", "CLOSE")
        return FutGate(True, "ok_close", "CLOSE", side=position.side, contracts=position.contracts,
                       leverage=position.leverage)

    side = "long" if action == "LONG" else "short"
    if snap.stale:
        return FutGate(False, "stale", action)
    if spec.state != 0:
        return FutGate(False, "contract_state", action)
    if position.is_open():
        return FutGate(False, "already_open", action)
    if day_pnl_usdt <= -abs(settings.max_day_loss_usdt):
        return FutGate(False, "day_loss", action)
    if not math.isfinite(intent.confidence) or intent.confidence < settings.min_confidence:
        return FutGate(False, "confidence", action)
    if snap.atr_1m is None or not math.isfinite(snap.atr_1m) or snap.atr_1m <= 0:
        return FutGate(False, "atr", action)
    leverage = settings.leverage
    if not 1 <= leverage <= min(MAX_LEVERAGE, spec.max_leverage):
        return FutGate(False, "leverage", action)
    price = fill_price(bid=snap.bid, ask=snap.ask, last=snap.last, buy=side == "long",
                       slippage_bps=settings.slippage_bps)
    if price is None:
        return FutGate(False, "no_price", action)
    if not math.isfinite(balance) or balance <= 0:
        return FutGate(False, "no_cash", action)

    target = min(settings.margin_usdt, settings.max_balance_pct * balance) * leverage
    contracts = min(int(math.floor(target / (price * spec.contract_size))), spec.max_vol)
    if contracts < spec.min_vol:
        return FutGate(False, "dust", action)

    distance = min(max(settings.atr_mult * snap.atr_1m, settings.min_stop_pct * price),
                   settings.max_stop_pct * price)
    if side == "long":
        stop = round_to_unit(price - distance, spec.price_unit, up=False)
    else:
        stop = round_to_unit(price + distance, spec.price_unit, up=True)
    liq = liquidation_price(side, price, leverage, spec.mmr)
    if abs(price - stop) > settings.liq_stop_ratio * abs(price - liq):
        return FutGate(False, "liq_too_close", action)

    notional = contracts * spec.contract_size * price
    margin = notional / leverage
    if margin + notional * spec.taker_fee > balance:
        return FutGate(False, "no_cash", action)
    return FutGate(True, "ok_open", action, side=side, contracts=contracts, price=price, notional=notional,
                   margin=margin, stop=stop, liq=liq, leverage=leverage)
