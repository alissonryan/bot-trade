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


class UnprotectedPosition(RuntimeError):
    """The exchange holds a bot position without a resident stop and it could not be fixed."""


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
            if self.position.stop_price:
                self.store.remember_order("paper-stop")
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
        self._persist()

    def mark(self, snap: Snapshot) -> Position:
        if self.position.qty > 0 and self.position.stop_price is not None:
            if snap.last <= self.position.stop_price or snap.bid <= self.position.stop_price:
                slip = self.settings.paper_slippage_bps / 10_000.0
                px = min(snap.bid, self.position.stop_price) * (1 - slip)
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

    def _watch_balance(self, target: Callable[[float], float], expected: float) -> float:
        """Poll BTC total until ``target(total)`` reaches ``expected`` (full) or tries run
        out (returns the partial amount seen, floored to the quantity scale)."""
        seen = 0.0
        tries = max(1, self.settings.fill_confirm_tries)
        for i in range(tries):
            try:
                seen = max(0.0, target(btc_total(self.client)))
            except Exception as exc:  # noqa: BLE001
                log.warning("balance read failed while confirming fill: %s", exc)
            if seen >= expected - self.tol:
                return expected
            if i + 1 < tries:
                self._sleep(self.settings.fill_confirm_wait_s)
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
        if filled <= 0:
            if entry_id in open_order_ids(self.client):
                try:
                    self.cancel_if_ours(entry_id)
                except Exception as exc:  # noqa: BLE001
                    log.error("could not cancel unfilled entry %s: %s", entry_id, exc)
                    self._persist("PENDING")
                    raise UnprotectedPosition(f"entry {entry_id} unfilled and uncancelled") from exc
            log.warning("entry %s not filled; flat", entry_id)
            self._clear()
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
        if sold < qty - self.tol:
            log.error("flatten accepted (%s) but balance did not drop", sell_id)
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
            self.cancel_if_ours(self.stop_order_id)  # raises -> nothing sold, still protected
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
            log.error("sell failed: %s; restoring stop", exc)
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
        if sold < qty - self.tol:
            log.warning("sell %s accepted but not confirmed by balance; reconcile will settle it", sell_id)
            self._persist("CLOSING")
            return self.position
        price, source = self._fill_price(sell_id, default=snap.bid or snap.last)
        pnl = (price - self.position.entry) * qty
        self.store.add_fill(self.today(), pnl, side="SELL", qty=qty, price=price, fee=0.0, order_id=sell_id, source=source)
        self._clear()
        return self.position

    # -- reconciliation ---------------------------------------------------------

    def reconcile(self) -> str:
        """Bring the local row in line with the exchange. Returns a short verdict."""
        if not self.position.is_open():
            return "flat"
        total = btc_total(self.client)
        ids = open_order_ids(self.client)
        base = self.position.btc_before if self.position.btc_before is not None else 0.0
        holding = total >= base + self.position.qty - self.tol
        stop_alive = bool(self.stop_order_id) and self.stop_order_id in ids
        qty = self.position.qty
        qty_s = _fmt_qty(qty, self.qty_scale)

        if not holding:
            # Stop hit, manual sale, or a CLOSING sell that settled after we stopped watching.
            if self.position.state == "CLOSING" and self._exit_hint:
                price = self._exit_hint
            else:
                price = self.position.stop_price or self.position.entry
            pnl = (price - self.position.entry) * qty
            self.store.add_fill(self.today(), pnl, side="SELL", qty=qty, price=price, fee=0.0, order_id=self.stop_order_id, source="reconcile")
            log.warning("position closed on the exchange (state %s); pnl est %.4f", self.position.state, pnl)
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
