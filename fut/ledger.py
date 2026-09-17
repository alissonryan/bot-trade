"""Paper ledger for one futures book. Fill, position and balance commit together or not at all."""

from __future__ import annotations

from dataclasses import replace

from fut.pricing import fill_price
from fut.settings import FutSettings
from fut.store import FutStore
from fut.types import FutGate, FutPosition, FutSnapshot
from kcex.fapi import ContractSpec


class PaperLedger:
    def __init__(self, store: FutStore, settings: FutSettings, spec: ContractSpec, *, book: str = "main"):
        self.store = store
        self.settings = settings
        self.spec = spec
        self.book = book
        self.position = store.load_fut_position(book)
        self.balance = store.balance(book, settings.starting_usdt)

    def _commit(self, *, fill: dict, position: FutPosition, balance: float) -> None:
        try:
            self.store.add_fut_fill(self.book, commit=False, **fill)
            self.store.save_fut_position(self.book, position, commit=False)
            self.store.set_balance(self.book, balance, commit=False)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        # In-memory state changes only after the transaction is durable.
        self.position = position
        self.balance = balance

    def open(self, gate: FutGate, *, now_ms: int) -> FutPosition:
        if not gate.ok or gate.action not in ("LONG", "SHORT") or gate.side not in ("long", "short"):
            raise ValueError(f"open() needs an ok LONG/SHORT gate, got {gate}")
        if self.position.is_open():
            raise ValueError("position already open")
        fee = gate.notional * self.spec.taker_fee
        position = FutPosition(side=gate.side, contracts=gate.contracts, entry=gate.price, stop=gate.stop,
                               liq=gate.liq, margin=gate.margin, leverage=gate.leverage, opened_ms=now_ms,
                               funding_through_ms=now_ms)
        self._commit(fill=dict(ts_ms=now_ms, kind="open", side=gate.side, contracts=gate.contracts,
                               price=gate.price, fee=fee, funding=0.0, pnl=0.0, reason="entry"),
                     position=position, balance=self.balance - fee)
        return position

    def close(self, price: float, *, now_ms: int, reason: str) -> float:
        pos = self.position
        if not pos.is_open():
            raise ValueError("no open position")
        qty = pos.contracts * self.spec.contract_size
        if reason == "liquidation":
            pnl, fee = -pos.margin, 0.0
        else:
            pnl = (price - pos.entry) * qty if pos.side == "long" else (pos.entry - price) * qty
            fee = price * qty * self.spec.taker_fee
        self._commit(fill=dict(ts_ms=now_ms, kind="close", side=pos.side, contracts=pos.contracts, price=price,
                               fee=fee, funding=0.0, pnl=pnl, reason=reason),
                     position=FutPosition(), balance=self.balance + pnl - fee)
        return pnl - fee

    def market_exit_price(self, snap: FutSnapshot) -> float | None:
        return fill_price(bid=snap.bid, ask=snap.ask, last=snap.last, buy=self.position.side == "short",
                          slippage_bps=self.settings.slippage_bps)

    def mark(self, snap: FutSnapshot, *, now_ms: int) -> str | None:
        pos = self.position
        if not pos.is_open():
            return None
        slip = self.settings.slippage_bps / 10_000.0
        fair = snap.fair if snap.fair > 0 else snap.last

        if pos.liq is not None and fair > 0 and (
                (pos.side == "long" and fair <= pos.liq) or (pos.side == "short" and fair >= pos.liq)):
            self.close(pos.liq, now_ms=now_ms, reason="liquidation")
            return "liquidation"

        if pos.stop is not None:
            if pos.side == "long":
                refs = [x for x in (snap.bid, snap.last) if x > 0]
                if refs and min(refs) <= pos.stop:
                    self.close(min(min(refs), pos.stop) * (1 - slip), now_ms=now_ms, reason="stop")
                    return "stop"
            else:
                refs = [x for x in (snap.ask, snap.last) if x > 0]
                if refs and max(refs) >= pos.stop:
                    self.close(max(max(refs), pos.stop) * (1 + slip), now_ms=now_ms, reason="stop")
                    return "stop"

        if now_ms - pos.opened_ms >= self.settings.max_hold_s * 1000:
            price = self.market_exit_price(snap)
            if price is not None:
                self.close(price, now_ms=now_ms, reason="time_limit")
                return "time_limit"

        nxt = snap.next_funding_ms
        if nxt is not None and pos.funding_through_ms < nxt <= now_ms and fair > 0:
            amount = pos.contracts * self.spec.contract_size * fair * snap.funding_rate
            paid = amount if pos.side == "long" else -amount
            self._commit(fill=dict(ts_ms=now_ms, kind="funding", side=pos.side, contracts=pos.contracts,
                                   price=fair, fee=0.0, funding=paid, pnl=0.0, reason="funding"),
                         position=replace(pos, funding_through_ms=nxt), balance=self.balance - paid)
            return "funding"
        return None

    def unrealized(self, snap: FutSnapshot) -> float:
        pos = self.position
        if not pos.is_open() or snap.mid <= 0:
            return 0.0
        qty = pos.contracts * self.spec.contract_size
        return (snap.mid - pos.entry) * qty if pos.side == "long" else (pos.entry - snap.mid) * qty
