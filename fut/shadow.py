"""Baselines on the same ticks, collar and ledger as the main book.

- flat: never trades (net 0, no ledger needed)
- jev_only: trades Jev's direction whenever Jev would wake the LLM
- random: enters LONG/SHORT at random at the LLM's observed entry rate
"""

from __future__ import annotations

import random

from fut import collar
from fut.ledger import PaperLedger
from fut.questions import jev_side
from fut.settings import FutSettings
from fut.store import FutStore, day_of
from fut.types import FutIntent, FutSnapshot, JevVerdict
from kcex.fapi import ContractSpec

SHADOW_BOOKS = ("shadow:jev_only", "shadow:random")


class ShadowBooks:
    def __init__(self, store: FutStore, settings: FutSettings, spec: ContractSpec, *, rng=None):
        self.store = store
        self.settings = settings
        self.spec = spec
        self.ledgers = {
            "jev_only": PaperLedger(store, settings, spec, book="shadow:jev_only"),
            "random": PaperLedger(store, settings, spec, book="shadow:random"),
        }
        self.rng = rng or random.Random(settings.shadow_seed)

    def set_spec(self, spec: ContractSpec) -> None:
        self.spec = spec
        for ledger in self.ledgers.values():
            ledger.spec = spec

    def mark(self, snap: FutSnapshot, *, now_ms: int) -> dict[str, str]:
        out = {}
        for name, ledger in self.ledgers.items():
            reason = ledger.mark(snap, now_ms=now_ms)
            if reason:
                out[name] = reason
        return out

    def _open(self, ledger: PaperLedger, intent: FutIntent, snap: FutSnapshot, now_ms: int) -> str:
        gate = collar.check(intent, snap, position=ledger.position, balance=ledger.balance,
                            day_pnl_usdt=self.store.day_net(ledger.book, day_of(now_ms)),
                            spec=self.spec, settings=self.settings,
                            recent_entries=self.store.count_opens(ledger.book, now_ms - 3_600_000))
        if gate.ok and gate.action in ("LONG", "SHORT"):
            ledger.open(gate, now_ms=now_ms)
            return "opened"
        return gate.rule

    def on_jev(self, verdict: JevVerdict, snap: FutSnapshot, *, now_ms: int, wake: str | None,
               entry_rate: float) -> dict[str, str]:
        out: dict[str, str] = {}
        jev = self.ledgers["jev_only"]
        side = jev_side(verdict)
        if wake == "entry_signal" and side and not jev.position.is_open():
            action = "LONG" if side == "long" else "SHORT"
            out["jev_only"] = self._open(jev, FutIntent(action, verdict.direction_conf, "jev_only"), snap, now_ms)
        elif wake in ("exit_signal", "reversal_signal") and jev.position.is_open():
            price = jev.market_exit_price(snap)
            if price is not None:
                jev.close(price, now_ms=now_ms, reason="jev_signal")
                out["jev_only"] = "closed"

        rnd = self.ledgers["random"]
        draw = self.rng.random()
        pick = self.rng.choice(("LONG", "SHORT"))
        if not rnd.position.is_open() and draw < entry_rate:
            out["random"] = self._open(rnd, FutIntent(pick, 1.0, "random"), snap, now_ms)
        return out
