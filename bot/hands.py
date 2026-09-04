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


def _load_position(store: Store) -> tuple[Position, str | None, str | None]:
    row = store.load_position()
    if not row:
        return Position(), None, None
    pos = Position(qty=row["qty"], entry=row["entry"], stop_price=row["stop_price"])
    return pos, row.get("entry_order_id"), row.get("stop_order_id")


class PaperHands:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.position, _, _ = _load_position(store)

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
            self._persist()
        elif gate.action == "SELL" and self.position.qty > 0:
            px = snap.bid * (1 - slip)
            pnl = (px - self.position.entry) * self.position.qty
            self.store.add_fill(self.today(), pnl)
            self.position = Position()
            self._persist()
        return self.position

    def _persist(self) -> None:
        self.store.save_position(
            qty=self.position.qty,
            entry=self.position.entry,
            stop_price=self.position.stop_price,
            entry_order_id="paper-entry" if self.position.qty else None,
            stop_order_id="paper-stop" if self.position.stop_price else None,
        )

    def mark(self, snap: Snapshot) -> Position:
        if self.position.qty > 0 and self.position.stop_price is not None:
            if snap.last <= self.position.stop_price or snap.bid <= self.position.stop_price:
                px = min(snap.bid, self.position.stop_price)
                pnl = (px - self.position.entry) * self.position.qty
                self.store.add_fill(self.today(), pnl)
                self.position = Position()
                self._persist()
        return self.position


class LiveHands:
    def __init__(self, settings: Settings, store: Store, client: KcexClient):
        self.settings = settings
        self.store = store
        self.client = client
        self.position, self.entry_order_id, self.stop_order_id = _load_position(store)

    def today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _persist(self) -> None:
        self.store.save_position(
            qty=self.position.qty,
            entry=self.position.entry,
            stop_price=self.position.stop_price,
            entry_order_id=self.entry_order_id,
            stop_order_id=self.stop_order_id,
        )

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
            trig = None
            for _ in range(2):
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
                    break
                except Exception:
                    trig = None
            if trig is None:
                self.client.place_market(
                    currency="BTC",
                    market="USDT",
                    side="SELL",
                    price=str(snap.last),
                    quantity=gate.qty,
                )
                pnl = (snap.bid - snap.last) * float(gate.qty)
                self.store.add_fill(self.today(), pnl)
                self.position = Position()
                self.entry_order_id = None
                self.stop_order_id = None
                self._persist()
                return self.position
            stop_id = str(trig.get("data") or trig)
            self.store.remember_order(stop_id)
            self.stop_order_id = stop_id
            self.position = Position(
                qty=float(gate.qty),
                entry=snap.last,
                stop_price=float(gate.stop_price),
            )
            self._persist()
        elif gate.action == "SELL" and gate.qty:
            if self.stop_order_id:
                self.cancel_if_ours(self.stop_order_id)
            exit_px = snap.bid or snap.last
            self.client.place_market(
                currency="BTC",
                market="USDT",
                side="SELL",
                price=str(snap.last),
                quantity=gate.qty,
            )
            if self.position.qty > 0:
                pnl = (exit_px - self.position.entry) * self.position.qty
                self.store.add_fill(self.today(), pnl)
            self.position = Position()
            self.stop_order_id = None
            self.entry_order_id = None
            self._persist()
        return self.position
