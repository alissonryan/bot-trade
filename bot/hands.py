"""Order execution.

PaperHands is a ledger on live prices: cash lives in the store (kv ``paper_cash``), fills
carry prices, the simulated stop pays the same slippage as the entry.

LiveHands talks to KCEX spot. Invariants, in the order they matter:

1. The position row is persisted the moment the entry order is accepted (state
   PENDING), before any stop is attempted, so a crash cannot forget a filled entry.
2. A fill is confirmed by the exchange balance delta (BTC total after minus before).
   The order id alone only proves the order was accepted.
3. A position never stays quietly unprotected. If the stop cannot be placed and the
   position cannot be flattened, the row is marked UNPROTECTED and
   UnprotectedPosition is raised so the process halts loudly.
4. SELL cancels the resident stop first (the BTC is frozen by it), confirms the cancel,
   then sells; if the sell fails the stop is put back.
5. reconcile() compares the local row with exchange balances and open orders at boot
   and on every LLM cycle.
6. The bot only ever cancels order ids it created.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from bot.collar import stop_for_entry
from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, Snapshot, SymbolRules
from kcex.client import KcexClient

log = logging.getLogger(__name__)

PAPER_CASH_KEY = "paper_cash"
# BTC on the account that is not the bot's. Seeded the first time reconcile()
# runs flat; an increase while flat means an entry filled without being recorded.
FOREIGN_BTC_KEY = "foreign_btc"


class UnprotectedPosition(RuntimeError):
    """The exchange holds a bot position without a resident stop and it could not be fixed."""


class PositionStuck(RuntimeError):
    """The position is protected but the bot cannot exit it on its own.

    Raised when the resident stop is not an id the bot recorded, so
    ``cancel_if_ours`` will never cancel it: without this the SELL path aborts
    silently and repeats that abort on every later cycle, forever.
    """


@dataclass
class Position:
    qty: float = 0.0
    entry: float = 0.0
    stop_price: float | None = None
    state: str = "FLAT"
    entry_source: str | None = None
    btc_before: float | None = None

    def is_open(self) -> bool:
        return self.qty > 0


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _floor_qty(qty: float, scale: int) -> float:
    factor = 10 ** scale
    return math.floor(qty * factor + 1e-9) / factor


def _fmt_qty(qty: float, scale: int) -> str:
    return f"{_floor_qty(qty, scale):.{scale}f}"


def _extract_list(payload: Any) -> list[Any]:
    """The venue wraps lists inconsistently; accept the shapes seen so far."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("resultList", "list", "orders", "records", "rows", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def btc_total(client: KcexClient) -> float:
    """BTC available + frozen on the account (frozen includes BTC parked in a trigger)."""
    total = 0.0
    for row in _extract_list(client.balances("BTC")):
        if isinstance(row, dict) and row.get("currency") == "BTC":
            total += float(row.get("available") or 0) + float(row.get("frozen") or 0)
    return total


def open_order_ids(client: KcexClient) -> set[str]:
    ids = set()
    for row in _extract_list(client.open_orders()):
        if isinstance(row, dict) and row.get("id") is not None:
            ids.add(str(row["id"]))
    return ids


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if row.get(key) not in (None, ""):
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return None


def avg_fill_from_deals(payload: Any, order_id: str) -> tuple[float, float] | None:
    """Best effort: average price and quantity of the deals that belong to ``order_id``.
    The private deals shape was not captured; common key names are tried and None is
    returned when nothing matches, in which case the caller keeps its estimate."""
    total_qty = 0.0
    total_pq = 0.0
    for row in _extract_list(payload):
        if not isinstance(row, dict):
            continue
        oid = row.get("orderId") or row.get("order_id") or row.get("oid")
        if oid is None or str(oid) != str(order_id):
            continue
        price = _first(row, ("price", "p", "dealPrice"))
        qty = _first(row, ("quantity", "q", "v", "dealQuantity"))
        if price is None or qty is None or qty <= 0:
            continue
        total_qty += qty
        total_pq += price * qty
    if total_qty <= 0:
        return None
    return total_pq / total_qty, total_qty


def _load(store: Store) -> tuple[Position, str | None, str | None]:
    row = store.load_position()
    if not row:
        return Position(), None, None
    pos = Position(
        qty=row["qty"],
        entry=row["entry"],
        stop_price=row["stop_price"],
        state=row.get("state") or "OPEN",
        entry_source=row.get("entry_source"),
        btc_before=row.get("btc_before"),
    )
    return pos, row.get("entry_order_id"), row.get("stop_order_id")


class PaperHands:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.position, _, _ = _load(store)
        cached = store.kv_get(PAPER_CASH_KEY)
        self.cash = float(cached) if cached is not None else float(settings.paper_starting_usdt)
        self.entry_order_id = "paper-entry" if self.position.is_open() else None
        self.stop_order_id = "paper-stop" if self.position.is_open() else None

    def today(self) -> str:
        return _today()

    def reconcile(self) -> str:
        return "paper"

    def _persist(self) -> None:
        self.store.save_position(
            qty=self.position.qty,
            entry=self.position.entry,
            stop_price=self.position.stop_price,
            entry_order_id="paper-entry" if self.position.qty else None,
            stop_order_id="paper-stop" if self.position.stop_price and self.position.qty else None,
            state="OPEN",
            entry_source="paper",
        )
        self.store.kv_set(PAPER_CASH_KEY, repr(self.cash))

    def execute(self, gate: GateResult, snap: Snapshot) -> Position:
        slip = self.settings.paper_slippage_bps / 10_000.0
        if gate.action == "BUY" and gate.qty:
            px = (snap.ask or snap.last) * (1 + slip)
            qty = float(gate.qty)
            cost = px * qty
            if cost > self.cash + 1e-9:
                log.warning("paper: not enough cash (%.2f) for %.2f USDT", self.cash, cost)
                return self.position
            self.cash -= cost
            self.position = Position(
                qty=qty,
                entry=px,
                stop_price=float(gate.stop_price or 0) or None,
                state="OPEN",
                entry_source="paper",
            )
            self.store.remember_order("paper-entry")
            self.entry_order_id = "paper-entry"
            if self.position.stop_price:
                self.store.remember_order("paper-stop")
                self.stop_order_id = "paper-stop"
            self.store.add_fill(self.today(), 0.0, side="BUY", qty=qty, price=px, fee=0.0, order_id="paper-entry", source="paper")
            self._persist()
        elif gate.action == "SELL" and self.position.qty > 0:
            px = (snap.bid or snap.last) * (1 - slip)
            self._close(px, source="paper", order_id="paper-exit")
        return self.position

    def _close(self, px: float, *, source: str, order_id: str) -> None:
        qty = self.position.qty
        pnl = (px - self.position.entry) * qty
        self.cash += px * qty
        self.store.add_fill(self.today(), pnl, side="SELL", qty=qty, price=px, fee=0.0, order_id=order_id, source=source)
        self.position = Position()
        self.entry_order_id = None
        self.stop_order_id = None
        self._persist()

    def mark(self, snap: Snapshot) -> Position:
        if self.position.qty > 0 and self.position.stop_price is not None:
            # Only quotes that actually exist can trigger a stop. bid/last sit at
            # 0.0 until the matching frame arrives, and a deals-only frame already
            # marks the feed healthy -- so an unguarded `<=` reads "price fell to
            # zero", closes at 0.0 and books pnl = -entry*qty against the ledger.
            hit = (snap.last > 0 and snap.last <= self.position.stop_price) or (
                snap.bid > 0 and snap.bid <= self.position.stop_price
            )
            if hit:
                slip = self.settings.paper_slippage_bps / 10_000.0
                reference = min(x for x in (snap.bid, snap.last) if x > 0)
                px = min(reference, self.position.stop_price) * (1 - slip)
                self._close(px, source="paper_stop", order_id="paper-stop")
        return self.position


class LiveHands:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        client: KcexClient,
        *,
        rules: SymbolRules | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.store = store
        self.client = client
        self.rules = rules
        self._sleep = sleep
        self.position, self.entry_order_id, self.stop_order_id = _load(store)
        self._exit_hint: float | None = None

    # -- helpers ----------------------------------------------------------------

    @property
    def qty_scale(self) -> int:
        return self.rules.qty_scale if self.rules else self.settings.qty_scale

    @property
    def tol(self) -> float:
        return 10 ** (-self.qty_scale)

    def today(self) -> str:
        return _today()

    def _persist(self, state: str | None = None) -> None:
        if state:
            self.position.state = state
        self.store.save_position(
            qty=self.position.qty,
            entry=self.position.entry,
            stop_price=self.position.stop_price,
            entry_order_id=self.entry_order_id,
            stop_order_id=self.stop_order_id,
            state=self.position.state if self.position.is_open() else "OPEN",
            entry_source=self.position.entry_source,
            btc_before=self.position.btc_before,
        )

    def _clear(self) -> None:
        self.position = Position()
        self.entry_order_id = None
        self.stop_order_id = None
        self.store.clear_position()

    def cancel_if_ours(self, order_id: str) -> bool:
        if not self.store.is_bot_order(order_id):
            return False
        self.client.cancel_order(order_id)
        return True

    def _order_id(self, response: Any) -> str:
        if isinstance(response, dict):
            return str(response.get("data") or response)
        return str(response)

    def _place_stop(self, qty_s: str, stop_price: float | None) -> str | None:
        if not stop_price:
            return None
        for attempt in range(2):
            try:
                resp = self.client.place_trigger(
                    currency="BTC",
                    market="USDT",
                    side="SELL",
                    trigger_price=f"{stop_price:.{self.rules.price_scale if self.rules else 2}f}",
                    trigger_type="LE",
                    quantity=qty_s,
                    amount="0",
                    market_order=True,
                )
                stop_id = self._order_id(resp)
                self.store.remember_order(stop_id)
                return stop_id
            except Exception as exc:  # noqa: BLE001
                log.error("stop placement attempt %d failed: %s", attempt + 1, exc)
        return None

    def _watch_balance(self, target: Callable[[float], float], expected: float) -> float | None:
        """Poll BTC total until ``target(total)`` reaches ``expected`` (full) or tries run
        out (returns the partial amount seen, floored to the quantity scale).

        Returns ``None`` when not one reading succeeded. "The balances endpoint is
        down" and "nothing filled" are different facts and the caller must not be
        allowed to confuse them: the first is ignorance, and acting on ignorance
        here means deleting the row for an order that may hold real BTC.
        """
        seen = 0.0
        read_ok = False
        tries = max(1, self.settings.fill_confirm_tries)
        for i in range(tries):
            try:
                seen = max(0.0, target(btc_total(self.client)))
                read_ok = True
            except Exception as exc:  # noqa: BLE001
                log.warning("balance read failed while confirming fill: %s", exc)
            if read_ok and seen >= expected - self.tol:
                return expected
            if i + 1 < tries:
                self._sleep(self.settings.fill_confirm_wait_s)
        if not read_ok:
            log.error("could not read the balance even once while confirming a fill")
            return None
        return _floor_qty(seen, self.qty_scale) if seen >= self.tol else 0.0

    def _fill_price(self, order_id: str, default: float) -> tuple[float, str]:
        try:
            now = int(time.time() * 1000)
            payload = self.client.my_deals("BTC", "USDT", start_time=now - 10 * 60 * 1000, end_time=now)
            got = avg_fill_from_deals(payload, order_id)
            if got:
                return got[0], "deals"
        except Exception as exc:  # noqa: BLE001
            log.warning("deals unavailable for %s: %s", order_id, exc)
        return default, "estimated"

    # -- entry ------------------------------------------------------------------

    def execute(self, gate: GateResult, snap: Snapshot) -> Position:
        if self.settings.mode != "live":
            raise RuntimeError("LiveHands requires MODE=live")
        if gate.action == "BUY" and gate.qty and gate.stop_price:
            return self._buy(gate, snap)
        if gate.action == "SELL" and gate.qty:
            return self._sell(snap)
        return self.position

    def _buy(self, gate: GateResult, snap: Snapshot) -> Position:
        if self.position.is_open():
            log.warning("buy ignored: position already open (%s)", self.position.state)
            return self.position
        before = btc_total(self.client)  # a failure here means no order is sent
        qty_req = float(gate.qty)
        market = self.client.place_market(
            currency="BTC",
            market="USDT",
            side="BUY",
            price=str(snap.last),
            quantity=gate.qty,
        )
        entry_id = self._order_id(market)
        self.store.remember_order(entry_id)
        self.entry_order_id = entry_id
        self.stop_order_id = None
        self.position = Position(
            qty=qty_req,
            entry=snap.ask or snap.last,
            stop_price=float(gate.stop_price),
            state="PENDING",
            entry_source="estimated",
            btc_before=before,
        )
        self._persist()  # invariant 1: the entry exists on disk before the stop is tried

        filled = self._watch_balance(lambda total: total - before, qty_req)
        if filled is None or filled <= 0:
            # The row is only safe to delete when we have *proof* the entry did not
            # fill: it was still resting on the book and we cancelled it. Anything
            # else -- a balances outage, or an order that already left the book --
            # is ambiguous, and deleting the row there is what makes the bot go
            # "flat" while holding real BTC with no stop. Leave it PENDING and let
            # reconcile() settle it against the exchange.
            still_open = False
            try:
                still_open = entry_id in open_order_ids(self.client)
            except Exception as exc:  # noqa: BLE001
                log.error("could not list open orders after entry %s: %s", entry_id, exc)
            if still_open:
                try:
                    cancelled = self.cancel_if_ours(entry_id)
                except Exception as exc:  # noqa: BLE001
                    log.error("could not cancel unfilled entry %s: %s", entry_id, exc)
                    self._persist("PENDING")
                    raise UnprotectedPosition(f"entry {entry_id} unfilled and uncancelled") from exc
                if cancelled:
                    log.warning("entry %s not filled; cancelled; flat", entry_id)
                    self._clear()
                    return self.position
            reason = "balance unreadable" if filled is None else "fill unconfirmed"
            log.error(
                "entry %s: %s and it is not on the book; keeping the row PENDING for "
                "reconcile rather than assuming we are flat", entry_id, reason,
            )
            self._persist("PENDING")
            return self.position

        qty = min(filled, qty_req)
        price, source = self._fill_price(entry_id, default=snap.ask or snap.last)
        self.position.qty = qty
        self.position.entry = price
        self.position.entry_source = source
        if snap.atr:
            self.position.stop_price = float(stop_for_entry(price, snap.atr, self.settings, self.rules))
        self._persist()
        self.store.add_fill(self.today(), 0.0, side="BUY", qty=qty, price=price, fee=0.0, order_id=entry_id, source=source)

        qty_s = _fmt_qty(qty, self.qty_scale)
        stop_id = self._place_stop(qty_s, self.position.stop_price)
        if stop_id:
            self.stop_order_id = stop_id
            self._persist("OPEN")
            return self.position

        log.error("stop could not be placed for %s BTC; flattening", qty_s)
        if self._flatten(qty_s, snap):
            return self.position
        self._persist("UNPROTECTED")
        raise UnprotectedPosition(f"long {qty_s} BTC (entry {entry_id}) has no stop and could not be flattened")

    def _flatten(self, qty_s: str, snap: Snapshot) -> bool:
        qty = float(qty_s)
        start = None
        try:
            start = btc_total(self.client)
        except Exception as exc:  # noqa: BLE001
            log.warning("balance read failed before flatten: %s", exc)
        try:
            resp = self.client.place_market(
                currency="BTC",
                market="USDT",
                side="SELL",
                price=str(snap.last),
                quantity=qty_s,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("flatten failed: %s", exc)
            return False
        sell_id = self._order_id(resp)
        self.store.remember_order(sell_id)
        base = start if start is not None else (self.position.btc_before or 0.0) + qty
        sold = self._watch_balance(lambda total: base - total, qty)
        if sold is None or sold < qty - self.tol:
            # Unconfirmed is not the same as failed, but for an unprotected
            # position the safe reading is the loud one: report failure so the
            # caller raises UnprotectedPosition and a human looks at it.
            log.error("flatten accepted (%s) but the balance drop could not be confirmed", sell_id)
            return False
        price, source = self._fill_price(sell_id, default=snap.bid or snap.last)
        pnl = (price - self.position.entry) * qty
        self.store.add_fill(self.today(), pnl, side="SELL", qty=qty, price=price, fee=0.0, order_id=sell_id, source=f"flatten_{source}")
        self._clear()
        return True

    # -- exit -------------------------------------------------------------------

    def _sell(self, snap: Snapshot) -> Position:
        if not self.position.is_open():
            return self.position
        qty = self.position.qty
        qty_s = _fmt_qty(qty, self.qty_scale)
        start = btc_total(self.client)
        self._exit_hint = snap.bid or snap.last

        if self.stop_order_id:
            # A False return means the id is not in our records, so no cancel was
            # ever sent. Retrying is pointless -- the confirm loop below would see
            # the stop still open every time and abort the exit for good.
            if not self.cancel_if_ours(self.stop_order_id):  # raises -> nothing sold, still protected
                self._persist("OPEN")
                raise PositionStuck(
                    f"stop {self.stop_order_id} guards {qty_s} BTC but is not a recorded bot "
                    "order, so the bot cannot cancel it and cannot exit. Close it by hand."
                )
            gone = False
            for i in range(max(1, self.settings.fill_confirm_tries)):
                if self.stop_order_id not in open_order_ids(self.client):
                    gone = True
                    break
                self._sleep(self.settings.fill_confirm_wait_s)
            if not gone:
                log.error("stop %s still open after cancel; sell aborted", self.stop_order_id)
                return self.position
            self.stop_order_id = None
            self._persist("UNPROTECTED")  # honest state while the sell is in flight

        try:
            resp = self.client.place_market(
                currency="BTC",
                market="USDT",
                side="SELL",
                price=str(snap.last),
                quantity=qty_s,
            )
        except Exception as exc:  # noqa: BLE001
            # The POST is never retried, so this may be a lost response for an
            # order that actually executed. Re-read the balance before putting a
            # stop back: a trigger for BTC we no longer own would sit on top of
            # the owner's own coins and could freeze and sell them.
            log.error("sell failed: %s; re-reading the balance before restoring the stop", exc)
            try:
                after = btc_total(self.client)
            except Exception as read_exc:  # noqa: BLE001
                log.error("balance unreadable after the failed sell: %s", read_exc)
                after = None
            if after is not None and start - after >= qty - self.tol:
                log.warning("the sell had in fact executed; booking it instead of restoring a stop")
                price, source = self._fill_price("", default=snap.bid or snap.last)
                pnl = (price - self.position.entry) * qty
                self.store.add_fill(self.today(), pnl, side="SELL", qty=qty, price=price, fee=0.0, order_id=None, source=f"recovered_{source}")
                self._clear()
                return self.position
            stop_id = self._place_stop(qty_s, self.position.stop_price)
            if stop_id:
                self.stop_order_id = stop_id
                self._persist("OPEN")
                return self.position
            self._persist("UNPROTECTED")
            raise UnprotectedPosition(f"sell failed and stop could not be restored for {qty_s} BTC") from exc

        sell_id = self._order_id(resp)
        self.store.remember_order(sell_id)
        sold = self._watch_balance(lambda total: start - total, qty)
        if sold is None or sold < qty - self.tol:
            log.warning("sell %s accepted but not confirmed by balance; reconcile will settle it", sell_id)
            self._persist("CLOSING")
            return self.position
        price, source = self._fill_price(sell_id, default=snap.bid or snap.last)
        pnl = (price - self.position.entry) * qty
        self.store.add_fill(self.today(), pnl, side="SELL", qty=qty, price=price, fee=0.0, order_id=sell_id, source=source)
        self._clear()
        return self.position

    # -- reconciliation ---------------------------------------------------------

    def _reconcile_flat(self) -> str:
        """Flat locally -- but the local row is exactly what a lost entry response
        destroys, so ask the exchange instead of trusting it.

        `foreign_btc` is the BTC on the account that is not ours (the owner holds
        their own, e.g. a manual 0.00064 stop). It is re-baselined downwards while
        we are genuinely flat; an *increase* while flat is BTC nobody accounted
        for, which is what an unrecorded fill looks like.
        """
        total = btc_total(self.client)
        raw = self.store.kv_get(FOREIGN_BTC_KEY)
        if raw is None:
            self.store.kv_set(FOREIGN_BTC_KEY, repr(total))
            return "flat"
        baseline = float(raw)
        if total > baseline + self.tol:
            log.critical(
                "flat locally but the account gained %.8f BTC (baseline %.8f, now %.8f). "
                "An entry may have filled without being recorded. Square the account on "
                "the exchange, then reset %s in the kv table.",
                total - baseline, baseline, total, FOREIGN_BTC_KEY,
            )
            raise UnprotectedPosition(
                f"{total - baseline:.8f} BTC on the exchange with no local position row"
            )
        if total < baseline:
            self.store.kv_set(FOREIGN_BTC_KEY, repr(total))
        return "flat"

    def reconcile(self) -> str:
        """Bring the local row in line with the exchange. Returns a short verdict."""
        if not self.position.is_open():
            return self._reconcile_flat()
        total = btc_total(self.client)
        ids = open_order_ids(self.client)
        base = self.position.btc_before if self.position.btc_before is not None else 0.0
        qty = self.position.qty
        qty_s = _fmt_qty(qty, self.qty_scale)
        stop_alive = bool(self.stop_order_id) and self.stop_order_id in ids

        # `missing` is how much less BTC the account holds than it should with our
        # position open. Comparing the *whole-account* total against a threshold
        # (the old `total >= base + qty`) cannot tell our position closing apart
        # from the owner moving their own coins, and it resolved that ambiguity
        # the dangerous way: booking a phantom exit while our long and its live
        # stop stayed on the exchange.
        missing = (base + qty) - total
        if missing <= self.tol:
            holding = True
        elif abs(missing - qty) <= self.tol:
            holding = False  # exactly our size left: our stop filled, or a manual sale
        else:
            # Some other amount moved. Assume our position is still there (the
            # asymmetry is deliberate: wrongly believing we are flat orphans a
            # real position and lets the next BUY stack a second one) and
            # re-baseline the foreign holding.
            log.warning(
                "account moved %.8f BTC, which is not our size %.8f -- treating the "
                "position as still open and re-baselining", missing, qty,
            )
            holding = True
            self.position.btc_before = max(0.0, total - qty)
            self._persist()

        if not holding and self.position.state == "PENDING":
            # The entry never actually filled (see _buy: the row is kept PENDING
            # whenever the fill could not be confirmed). There was no position, so
            # there is no exit to book -- inventing a SELL here would write a
            # fabricated PnL into the ledger.
            log.warning("pending entry %s never filled; dropping the row", self.entry_order_id)
            self.store.kv_set(FOREIGN_BTC_KEY, repr(total))
            self._clear()
            return "entry_never_filled"

        if not holding:
            # Stop hit, manual sale, or a CLOSING sell that settled after we stopped watching.
            if self.position.state == "CLOSING" and self._exit_hint:
                price = self._exit_hint
            else:
                price = self.position.stop_price or self.position.entry
            pnl = (price - self.position.entry) * qty
            self.store.add_fill(self.today(), pnl, side="SELL", qty=qty, price=price, fee=0.0, order_id=self.stop_order_id, source="reconcile")
            log.warning("position closed on the exchange (state %s); pnl est %.4f", self.position.state, pnl)
            self.store.kv_set(FOREIGN_BTC_KEY, repr(total))
            self._clear()
            return "closed_on_exchange"

        if stop_alive:
            if self.position.state != "OPEN":
                self._persist("OPEN")
            return "ok"

        stop_id = self._place_stop(qty_s, self.position.stop_price)
        if stop_id:
            self.stop_order_id = stop_id
            self._persist("OPEN")
            log.warning("stop restored as %s", stop_id)
            return "stop_restored"
        self._persist("UNPROTECTED")
        raise UnprotectedPosition(f"{qty_s} BTC on the exchange without a stop and none could be placed")
