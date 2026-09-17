"""One futures paper process step: drain WS events, refresh REST, mark, resolve the LLM, run Jev."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import asdict

from bot.brain import REASON_BUDGET_STATE, Budget
from bot.store import BudgetStateCorrupt
from fut import collar
from fut.dispatch import Dispatcher, Trigger
from fut.ledger import PaperLedger
from fut.llm import decide as default_llm_decide
from fut.market import MarketState
from fut.questions import jev_side, should_wake
from fut.settings import FutSettings
from fut.shadow import ShadowBooks
from fut.store import FutStore, day_bounds_ms, day_of
from kcex.fws import PublicFuturesWs, default_connect

log = logging.getLogger("fut")

BARS_EVERY_MS = 60_000
FUNDING_EVERY_MS = 60_000
TICKER_FALLBACK_EVERY_MS = 1_000
SPEC_EVERY_MS = 3_600_000


class Unmonitored(RuntimeError):
    """An open paper position has had no price at all for FUT_UNMONITORED_SECONDS."""


def now_ms() -> int:
    return int(time.time() * 1000)


def seed_budget(store: FutStore, llm, *, today: str) -> Budget:
    cap = llm.llm_daily_budget_usd
    try:
        persisted = store.budget_load()
    except BudgetStateCorrupt as exc:
        log.error("persisted LLM budget is unreadable; blocking LLM spend: %s", exc)
        return Budget(0.0, cap, today, blocked_reason=REASON_BUDGET_STATE)
    if persisted and persisted["day"] == today:
        return Budget(persisted["spent_usd"], cap, today, calls=persisted["calls"])
    return Budget(0.0, cap, today)


def start_ws_thread(settings: FutSettings, events: queue.SimpleQueue, stop: threading.Event, *,
                    connect=default_connect, sleep=time.sleep) -> threading.Thread:
    def run() -> None:
        ws = PublicFuturesWs(settings.ws_url, settings.symbol, connect)
        while not stop.is_set():
            try:
                ws.pump(on_event=lambda event: events.put(("ws", event)),
                        on_error=lambda exc: log.warning("futures ws error: %s", exc))
            except Exception as exc:  # noqa: BLE001 - reconnect on any transport failure
                log.warning("futures ws connect failed: %s", exc)
            if not stop.is_set():
                sleep(2.0)

    thread = threading.Thread(target=run, name="fut-ws", daemon=True)
    thread.start()
    return thread


class FutLoop:
    def __init__(self, *, settings: FutSettings, store: FutStore, spec, rest, jev, budget: Budget,
                 events: queue.SimpleQueue, clock_ms=now_ms, llm_decide=default_llm_decide, submit=None,
                 store_factory=None, rng=None):
        self.settings = settings
        self.store = store
        self.spec = spec
        self.rest = rest
        self.jev = jev
        self.budget = budget
        self.events = events
        self._clock = clock_ms
        self._llm_decide = llm_decide
        self._store_factory = store_factory or (lambda: FutStore(store.path))
        self._llm_store = None
        self.market = MarketState(settings, spec)
        self.ledger = PaperLedger(store, settings, spec)
        self.shadow = ShadowBooks(store, settings, spec, rng=rng)
        self.dispatcher = Dispatcher(settings, run_llm=self._run_llm, clock_ms=clock_ms, submit=submit)
        self._next = {"jev": 0, "bars": 0, "funding": 0, "ticker": 0, "spec": clock_ms() + SPEC_EVERY_MS}
        self._book_dirty = True
        self.jev_evals = 0
        self.llm_entries = 0

    # -- worker thread -----------------------------------------------------------

    def _run_llm(self, trigger: Trigger):
        # sqlite connections are bound to their thread: the worker opens its own.
        if self._llm_store is None:
            self._llm_store = self._store_factory()
        return self._llm_decide(trigger.state, has_position=trigger.state.get("position") != "flat",
                                settings=self.settings, budget=self.budget, store=self._llm_store)

    # -- main thread -------------------------------------------------------------

    def step(self) -> None:
        now = self._clock()
        self._drain(now)
        self._refresh(now)
        snap = self.market.snapshot(now)
        self._check_monitoring(now)
        exit_reason = self.ledger.mark(snap, now_ms=now)
        if exit_reason:
            self.store.log_decision("exit", {"reason": exit_reason, "balance": self.ledger.balance,
                                             "snapshot": snap.compact()}, ts_ms=now)
        self.shadow.mark(snap, now_ms=now)
        self._resolve(snap, now)
        if now >= self._next["jev"]:
            self._next["jev"] = now + int(self.settings.jev_every_s * 1000)
            self._jev(snap, now)

    def _drain(self, now: int) -> None:
        while True:
            try:
                source, event = self.events.get_nowait()
            except queue.Empty:
                return
            if not self.market.apply(event, now_ms=now, source=source):
                self._book_dirty = True

    def _refresh(self, now: int) -> None:
        symbol = self.settings.symbol
        if self._book_dirty or not self.market.book.synced:
            try:
                version, bids, asks = self.rest.depth(symbol)
                self.market.load_book(version, bids, asks)
                self._book_dirty = False
            except Exception as exc:  # noqa: BLE001
                log.warning("depth resync failed: %s", exc)
        if now >= self._next["bars"]:
            self._next["bars"] = now + BARS_EVERY_MS
            try:
                span_s = (self.settings.atr_period + 5) * 60
                self.market.set_bars(self.rest.klines_1m(symbol, now // 1000 - span_s, now // 1000))
            except Exception as exc:  # noqa: BLE001
                log.warning("kline refresh failed: %s", exc)
        if now >= self._next["funding"]:
            self._next["funding"] = now + FUNDING_EVERY_MS
            try:
                rate, next_ms = self.rest.funding(symbol)
                self.market.set_funding(rate, next_ms)
            except Exception as exc:  # noqa: BLE001
                log.warning("funding refresh failed: %s", exc)
        ws_silent = now - self.market.ws_last_ms > self.settings.stale_market_s * 1000
        if ws_silent and now >= self._next["ticker"]:
            self._next["ticker"] = now + TICKER_FALLBACK_EVERY_MS
            try:
                self.market.apply(self.rest.ticker(symbol), now_ms=now, source="rest")
            except Exception as exc:  # noqa: BLE001
                log.warning("REST ticker fallback failed: %s", exc)
        if now >= self._next["spec"]:
            self._next["spec"] = now + SPEC_EVERY_MS
            try:
                self._set_spec(self.rest.contract_detail(symbol))
            except Exception as exc:  # noqa: BLE001
                log.warning("contract detail refresh failed: %s", exc)

    def _set_spec(self, spec) -> None:
        self.spec = spec
        self.market.spec = spec
        self.ledger.spec = spec
        self.shadow.set_spec(spec)

    def _check_monitoring(self, now: int) -> None:
        if not self.ledger.position.is_open():
            return
        silent_ms = now - self.market.last_event_ms
        if silent_ms > self.settings.unmonitored_s * 1000:
            self.store.log_decision("unmonitored", {"silent_ms": silent_ms, "position": asdict(self.ledger.position)},
                                    ts_ms=now)
            raise Unmonitored(f"open paper position had no price for {silent_ms} ms")

    def _day_net(self, now: int) -> float:
        day = day_of(now)
        start, end = day_bounds_ms(day)
        return self.store.day_net("main", day) - self.store.model_cost_between(start, end)

    def _resolve(self, snap, now: int) -> None:
        res = self.dispatcher.poll(mid_now=snap.mid)
        if res is None:
            return
        decision, gate, outcome = res.decision, None, res.verdict
        if res.verdict == "ok" and decision.intent is not None and decision.intent.action != "HOLD":
            gate = collar.check(decision.intent, snap, position=self.ledger.position, balance=self.ledger.balance,
                                day_pnl_usdt=self._day_net(now), spec=self.spec, settings=self.settings)
            if gate.ok and gate.action in ("LONG", "SHORT"):
                self.ledger.open(gate, now_ms=now)
                self.llm_entries += 1
                outcome = "opened"
            elif gate.ok and gate.action == "CLOSE":
                price = self.ledger.market_exit_price(snap)
                if price is None:
                    outcome = "close_no_price"
                else:
                    self.ledger.close(price, now_ms=now, reason="llm_close")
                    outcome = "closed"
            else:
                outcome = f"gate_{gate.rule}"
        trigger = res.trigger
        self.store.log_decision("llm", {
            "trigger": {"kind": trigger.kind, "side": trigger.side, "ts_ms": trigger.ts_ms, "mid": trigger.mid},
            "llm": decision.as_audit(),
            "intent": asdict(decision.intent) if decision.intent else None,
            "verdict": res.verdict,
            "elapsed_ms": res.elapsed_ms,
            "mid_at_response": snap.mid,
            "gate": asdict(gate) if gate else None,
            "outcome": outcome,
            "cost_usd": decision.cost_usd,
        }, ts_ms=now)

    def _jev(self, snap, now: int) -> None:
        position = self.ledger.position
        if snap.stale and not position.is_open():
            return
        verdict = self.jev.evaluate(snap, position, now_ms=now)
        self.jev_evals += 1
        cost = verdict.input_tokens / 1e6 * self.settings.jev_usd_per_mtok
        wake = should_wake(verdict, position, threshold=self.settings.wake_threshold)
        shadow = self.shadow.on_jev(verdict, snap, now_ms=now, wake=wake,
                                    entry_rate=self.llm_entries / self.jev_evals)
        dispatch = None
        if wake:
            self.budget.roll_day(day_of(now))
            state = dict(verdict.state, jev=verdict.answers(), trigger=wake)
            trigger = Trigger(wake, jev_side(verdict), now, snap.mid, state)
            budget_ok = not self.budget.blocked_reason and self.budget.remaining() > 0
            dispatch = self.dispatcher.offer(trigger, budget_ok=budget_ok)
        self.store.log_decision("jev", {
            "model": verdict.model, "error": verdict.error, "latency_ms": verdict.latency_ms,
            "input_tokens": verdict.input_tokens, "cost_usd": cost, "answers": verdict.answers(),
            "wake": wake, "dispatch": dispatch, "shadow": shadow, "snapshot": snap.compact(),
        }, ts_ms=now)
