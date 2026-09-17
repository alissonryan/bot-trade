"""Baselines on the same ticks, collar and ledger as the main book.

- flat: never trades (net 0, no ledger needed)
- jev_only: trades Jev's direction whenever Jev would wake the LLM
- random: enters LONG/SHORT at random at the LLM's observed entry rate
"""

from __future__ import annotations

import random

from fut import collar
from fut.ledger import PaperLedger
from fut.questions import entry_qualifies, jev_side, should_wake
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
        self._entry_side: str | None = None
        self._entry_streak = 0

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

    def reset_entry_streak(self) -> None:
        self._entry_side = None
        self._entry_streak = 0

    def _jev_wake(self, verdict: JevVerdict, snap: FutSnapshot, now_ms: int) -> str | None:
        jev = self.ledgers["jev_only"]
        if jev.position.is_open():
            self.reset_entry_streak()
            return should_wake(verdict, jev.position, threshold=self.settings.wake_threshold,
                               now_ms=now_ms, min_hold_s=self.settings.min_hold_s,
                               streak=0, wake_streak=self.settings.wake_streak,
                               regimes=self.settings.wake_regimes)
        if snap.stale:
            self.reset_entry_streak()
            return None
        side = entry_qualifies(verdict, threshold=self.settings.wake_threshold,
                               regimes=self.settings.wake_regimes)
        if side is None:
            self.reset_entry_streak()
        elif side == self._entry_side:
            self._entry_streak += 1
        else:
            self._entry_side = side
            self._entry_streak = 1
        return should_wake(verdict, jev.position, threshold=self.settings.wake_threshold,
                           now_ms=now_ms, min_hold_s=self.settings.min_hold_s,
                           streak=self._entry_streak, wake_streak=self.settings.wake_streak,
                           regimes=self.settings.wake_regimes)

    def on_jev(self, verdict: JevVerdict, snap: FutSnapshot, *, now_ms: int, wake: str | None,
               entry_rate: float) -> dict[str, str]:
        out: dict[str, str] = {}
        jev = self.ledgers["jev_only"]
        shadow_wake = self._jev_wake(verdict, snap, now_ms)
        side = jev_side(verdict)
        if shadow_wake == "entry_signal" and side and not jev.position.is_open():
            action = "LONG" if side == "long" else "SHORT"
            cost_rule = collar.cost_gate(snap, spec=self.spec, settings=self.settings)
            out["jev_only"] = cost_rule or self._open(jev, FutIntent(action, verdict.direction_conf, "jev_only"),
                                                       snap, now_ms)
        elif shadow_wake in ("exit_signal", "reversal_signal") and jev.position.is_open():
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
