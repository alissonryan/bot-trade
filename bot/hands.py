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

    def execute(self, gate: GateResult, snap: Snapshot) -> Position:
        raise NotImplementedError
