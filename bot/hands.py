from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, Snapshot
from kcex.client import KcexClient


@dataclass
class Position:
    qty: float = 0.0
    entry: float = 0.0
    stop_price: float | None = None


class PaperHands:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.position = Position()

    def today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def execute(self, gate: GateResult, snap: Snapshot) -> Position:
        slip = self.settings.paper_slippage_bps / 10_000.0
        if gate.action == "BUY" and gate.qty:
            px = snap.ask * (1 + slip)
            qty = float(gate.qty)
            self.position = Position(qty=qty, entry=px, stop_price=float(gate.stop_price or 0) or None)
            self.store.remember_order("paper-entry")
            if self.position.stop_price:
                self.store.remember_order("paper-stop")
        elif gate.action == "SELL" and self.position.qty > 0:
            px = snap.bid * (1 - slip)
            pnl = (px - self.position.entry) * self.position.qty
            self.store.add_fill(self.today(), pnl)
            self.position = Position()
        return self.position

    def mark(self, snap: Snapshot) -> Position:
        if self.position.qty > 0 and self.position.stop_price is not None:
            if snap.last <= self.position.stop_price or snap.bid <= self.position.stop_price:
                px = min(snap.bid, self.position.stop_price)
                pnl = (px - self.position.entry) * self.position.qty
                self.store.add_fill(self.today(), pnl)
                self.position = Position()
        return self.position


class LiveHands:
    def __init__(self, settings: Settings, store: Store, client: KcexClient):
        self.settings = settings
        self.store = store
        self.client = client
        self.position = Position()
        self.entry_order_id: str | None = None
        self.stop_order_id: str | None = None

    def cancel_if_ours(self, order_id: str) -> bool:
        if not self.store.is_bot_order(order_id):
            return False
        self.client.cancel_order(order_id)
        return True

    def execute(self, gate: GateResult, snap: Snapshot) -> Position:
        if self.settings.mode != "live":
            raise RuntimeError("LiveHands requires MODE=live")
        if gate.action == "BUY" and gate.qty and gate.stop_price:
            market = self.client.place_market(
                currency="BTC",
                market="USDT",
                side="BUY",
                price=str(snap.last),
                quantity=gate.qty,
            )
            entry_id = str(market.get("data") or market)
            self.store.remember_order(entry_id)
            self.entry_order_id = entry_id
            try:
                trig = self.client.place_trigger(
                    currency="BTC",
                    market="USDT",
                    side="SELL",
                    trigger_price=gate.stop_price,
                    trigger_type="LE",
                    quantity=gate.qty,
                    amount="0",
                    market_order=True,
                )
            except Exception:
                self.client.place_market(
                    currency="BTC",
                    market="USDT",
                    side="SELL",
                    price=str(snap.last),
                    quantity=gate.qty,
                )
                self.position = Position()
                return self.position
            stop_id = str(trig.get("data") or trig)
            self.store.remember_order(stop_id)
            self.stop_order_id = stop_id
            self.position = Position(
                qty=float(gate.qty),
                entry=snap.last,
                stop_price=float(gate.stop_price),
            )
        elif gate.action == "SELL" and gate.qty:
            if self.stop_order_id:
                self.cancel_if_ours(self.stop_order_id)
            self.client.place_market(
                currency="BTC",
                market="USDT",
                side="SELL",
                price=str(snap.last),
                quantity=gate.qty,
            )
            self.position = Position()
            self.stop_order_id = None
        return self.position
