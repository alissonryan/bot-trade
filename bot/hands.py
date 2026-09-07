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

import json
import logging
import math
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable, NoReturn

from bot.collar import stop_for_entry, take_profit_for_entry
from bot.settings import Settings
from bot.store import Store, StoreIdentityMismatch
from bot.types import GateResult, Snapshot, SymbolRules
from kcex.client import KcexClient, KcexError
from kcex.orders import complete_open_order_ids

log = logging.getLogger(__name__)

PAPER_CASH_KEY = "paper_cash"
# BTC on the account that is not the bot's. Seeded the first time reconcile()
# runs flat; an increase while flat means an entry filled without being recorded.
FOREIGN_BTC_KEY = "foreign_btc"
STOP_REPLACE_KEY = "stop_replacement"
STOP_SUBMISSION_KEY = "stop_submission"

# -- M1 EXIT latch -------------------------------------------------------------
#
# Durable record of a live discretionary exit (LLM SELL, local take-profit,
# time limit) in kv, following the same write-ahead shape as STOP_SUBMISSION_KEY
# and STOP_REPLACE_KEY above.
#
# HONEST SCOPING, sharpened by the 2026-09 re-review: the eight `EXIT_PHASE_*`
# names below are FUTURE DESIGN NOTES, not a tested, working resumable state
# machine. No code path in this process ever WRITES an EXIT latch (see below),
# so there is no writer and there are no transitions between phases to test --
# `test_exit_latch_at_every_phase_blocks_every_entry_point` proves only that
# _guard_exit() blocks unconditionally on ANY non-empty, well-formed latch
# record, regardless of which of the eight phase strings it names; it is not
# evidence that a `cancel_submitting -> cancel_pending -> ... -> settled`
# resumption is implemented, because nothing here ever performs one. Read the
# list purely as documentation of what a future capture-and-resume design would
# need to name, not as infrastructure ready to be switched on:
#
#   cancel_submitting  - persisted BEFORE the DELETE. A restart never re-sends it.
#   cancel_pending     - waiting for evidence tied to the exact stop; an ack or
#                        absence from the open-order book is NOT terminal proof.
#   ready_to_sell      - only once the stop is definitively inactive AND the
#                        already-executed quantity is known.
#   sell_submitting    - persisted BEFORE the market POST.
#   sell_pending       - id known and/or partial execution; reconcile only.
#   settlement_pending - execution proven; no new POST/DELETE; apply once.
#   settled            - fill(s), position, journal, completion committed atomically.
#   manual_review      - explicit ambiguous state; blocks writes until a human
#                        resolves it. A timeout resolves nothing.
#
# KCEX's order-history/deals payload shapes that would prove a cancelled stop
# is truly inactive are not captured (see docs/kcex-spot-api.md and AGENTS.md).
# Without that evidence there is no safe transition out of `cancel_pending` -- a
# balance delta cannot tell "stop cancelled" from "stop executed" apart (see the
# design review's counterexamples: an owner deposit/withdrawal exactly
# offsetting the bot's own quantity produces the SAME observable delta for two
# different, incompatible realities). So `execute()`/`mark()` refuse to start a
# discretionary exit before any write via `TerminalEvidenceUnavailable` (see
# below), instead of cancelling the stop and then discovering it cannot safely
# finish -- and no writer for this latch exists to reach any phase past the one
# it might be constructed in by hand. What IS implemented and tested today is
# narrower than "eight-phase state machine": a minimal guard against any latch
# record already present -- written by a foreign/future process, or
# reconstructed from a legacy CLOSING row -- that this process must never
# silently resolve, regardless of its phase. This is fail-closed hardening
# only, NOT a working autonomous exit -- M1 is explicitly incomplete without
# captured terminal evidence, and the phase list above is a plan, not a
# contract this code currently honours.
EXIT_LATCH_KEY = "exit_latch"
EXIT_LATCH_SCHEMA = 1
EXIT_LATCH_REQUIRED_FIELDS = ("schema", "operation_id", "phase", "entry_id", "stop_id", "qty_original")

EXIT_PHASE_CANCEL_SUBMITTING = "cancel_submitting"
EXIT_PHASE_CANCEL_PENDING = "cancel_pending"
EXIT_PHASE_READY_TO_SELL = "ready_to_sell"
EXIT_PHASE_SELL_SUBMITTING = "sell_submitting"
EXIT_PHASE_SELL_PENDING = "sell_pending"
EXIT_PHASE_SETTLEMENT_PENDING = "settlement_pending"
EXIT_PHASE_SETTLED = "settled"
EXIT_PHASE_MANUAL_REVIEW = "manual_review"


class UnprotectedPosition(RuntimeError):
    """The exchange holds a bot position without a resident stop and it could not be fixed."""


class PositionStuck(RuntimeError):
    """The position is protected but the bot cannot exit it on its own.

    Raised when the resident stop is not an id the bot recorded, so
    ``cancel_if_ours`` will never cancel it: without this the SELL path aborts
    silently and repeats that abort on every later cycle, forever.
    """


class TerminalEvidenceUnavailable(RuntimeError):
    """Refuses to start a live discretionary exit (LLM SELL, local take-profit,
    time limit) before any write.

    KCEX's order-history/deals payload shapes that would prove a cancelled
    stop is truly inactive (as opposed to having executed a moment before, or
    a moment after, our own read of it) are not captured. Without that
    evidence, cancelling the resident stop and then classifying the outcome
    from a balance delta is exactly the classifier the 2026-09 design review
    refuted: an owner deposit/withdrawal that happens to equal the bot's own
    quantity produces the same observable delta for two incompatible
    realities, and no threshold or repeated read recovers the missing causal
    information. Cancelling the stop and then discovering the next step
    cannot be made safe would leave a real position unprotected, which is
    worse than not starting -- so this refuses before any DELETE is sent.

    No settings flag disables this: a switch that turns the guard off is the
    bug, not the feature. This refusal itself sends no cancel/place -- zero
    order-side writes, including any reconcile-driven stop restoration that
    would otherwise run as a side effect of evaluating the same barrier (see
    the 2026-09 re-review, LiveHands.mark/reconcile(repair=False)). That is
    not the same claim as "the resident stop still protects the position": a
    zero-write attempt proves only that THIS call made no cancel/place, not
    that a stop is present and working on the exchange right now -- the stop
    could already be gone (executed or cancelled by something else) before
    this call ever started. Confirming protection requires inspecting the
    exchange directly. PaperHands is unaffected -- it places no real orders.

    HALT CONTRACT (2026-09 third-round re-review): the process that raises
    this exits immediately (bot/cli.py exit code 6) and does NOT retry,
    repair, or resume by itself. There is no bounded wait for a "next cycle"
    to fix this -- ``run_once`` raises this from the barrier tick, which runs
    BEFORE ``poll_heavy()``/the LLM section, so a raise here ends the process
    before either ever runs again. If the resident stop genuinely is gone,
    the position can sit on the exchange with NO protection at all until a
    human notices and acts; only a human restart after manual inspection
    resumes anything. The message carries the last observed resident-stop
    presence/absence when reconcile() actually looked (see
    ``LiveHands.last_stop_observation``), never invented when it did not.
    """


class ExitLatchBlocked(RuntimeError):
    """An EXIT latch is present, the position is a legacy CLOSING row with no
    latch, or the latch is corrupt/mismatched/coexists with another in-flight
    write -- every write path refuses until a human resolves it.

    Because terminal evidence for cancel-vs-execute is not captured, no
    software resolution is safe here: this is not auto-resumable, and a
    timeout does not manufacture the missing proof (a delayed DELETE can
    still remove the stop after a timeout gives up waiting for it).
    """


@dataclass
class Position:
    qty: float = 0.0
    entry: float = 0.0
    stop_price: float | None = None
    state: str = "FLAT"
    entry_source: str | None = None
    btc_before: float | None = None
    take_profit_price: float | None = None
    opened_ts: str | None = None
    exit_reason: str | None = None

    def is_open(self) -> bool:
        return self.qty > 0


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _opened_now() -> str:
    return datetime.fromtimestamp(time.time(), timezone.utc).isoformat()


def local_exit_reason(pos: Position, snap: Snapshot, settings: Settings, now_ms: int) -> str | None:
    """Local TP can miss a cross-and-return between ticks and needs the process alive.
    TTL uses wall time, not the quote timestamp (which freezes during an outage).
    """
    if not pos.is_open() or pos.state != "OPEN":
        return None
    if any(not math.isfinite(p) or p < 0 for p in (snap.bid, snap.last)):
        return None
    if not any(math.isfinite(p) and p > 0 for p in (snap.bid, snap.last)):
        return None
    if (settings.tp_atr_mult > 0 and pos.take_profit_price and not snap.stale
            and not snap.depth_stale and snap.bid >= pos.take_profit_price):
        return "take_profit"
    if settings.time_limit_minutes > 0 and pos.opened_ts:
        opened = datetime.fromisoformat(pos.opened_ts)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        if now_ms - opened.timestamp() * 1000 >= settings.time_limit_minutes * 60000:
            return "time_limit"
    return None


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
    """BTC available + frozen on the account (frozen includes BTC parked in a trigger).

    A non-finite figure is ignorance, not a quantity. It must never reach the
    arithmetic that sizes a stop: ``qty - nan`` collapses to zero and would send
    a 0.00000 trigger while erasing a real position. Callers already treat a
    raise here as "the balance could not be read", which is the honest reading.
    """
    total = 0.0
    for row in _extract_list(client.balances("BTC")):
        if isinstance(row, dict) and row.get("currency") == "BTC":
            for key in ("available", "frozen"):
                value = float(row.get(key) or 0)
                if not math.isfinite(value):
                    raise ValueError(f"non-finite BTC {key} in the balance response")
                total += value
    if not math.isfinite(total):
        raise ValueError("non-finite BTC balance total")
    return total


def open_order_ids(client: KcexClient, *, max_pages: int = 50, page_size: int = 100) -> set[str]:
    return complete_open_order_ids(client, max_pages=max_pages, page_size=page_size)


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
        take_profit_price=row.get("take_profit_price"),
        opened_ts=row.get("opened_ts"),
        exit_reason=row.get("exit_reason"),
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
        self.last_mark_reason: str | None = None

    def today(self) -> str:
        return _today()

    def reconcile(self) -> str:
        return "paper"

    def _persist(self, *, position: Position | None = None, cash: float | None = None,
                commit: bool = True) -> None:
        """Write the position row and the cash kv entry.

        Callers that need this combined with an ``add_fill``/``clear_position``
        in one local transaction pass ``commit=False`` and an explicit
        ``position``/``cash`` snapshot -- see ``execute``/``_close`` below,
        which never mutate ``self.position``/``self.cash`` until the whole
        transaction has actually committed (so a rollback needs no separate
        restore step: the in-memory attributes were simply never touched).
        """
        pos = position if position is not None else self.position
        self.store.save_position(
            qty=pos.qty,
            entry=pos.entry,
            stop_price=pos.stop_price,
            entry_order_id="paper-entry" if pos.qty else None,
            stop_order_id="paper-stop" if pos.stop_price and pos.qty else None,
            state="OPEN",
            entry_source="paper",
            take_profit_price=pos.take_profit_price,
            opened_ts=pos.opened_ts,
            commit=False,
        )
        self.store.kv_set(PAPER_CASH_KEY, repr(cash if cash is not None else self.cash), commit=False)
        if commit:
            self.store.commit()

    def execute(self, gate: GateResult, snap: Snapshot) -> Position:
        slip = self.settings.paper_slippage_bps / 10_000.0
        if gate.action == "BUY" and gate.qty:
            px = (snap.ask or snap.last) * (1 + slip)
            qty = float(gate.qty)
            cost = px * qty
            if cost > self.cash + 1e-9:
                log.warning("paper: not enough cash (%.2f) for %.2f USDT", self.cash, cost)
                return self.position
            new_cash = self.cash - cost
            target = take_profit_for_entry(px, snap.atr or 0, self.settings)
            new_position = Position(
                qty=qty,
                entry=px,
                stop_price=float(gate.stop_price or 0) or None,
                state="OPEN",
                entry_source="paper",
                take_profit_price=float(target) if target else None,
                opened_ts=_opened_now(),
            )
            # Idempotent (INSERT OR IGNORE), so remembering these ids ahead of
            # the transaction below is harmless even if that transaction rolls
            # back -- the ids are only a cancel-allowlist, not the ledger.
            self.store.remember_order("paper-entry")
            if new_position.stop_price:
                self.store.remember_order("paper-stop")
            # The fill, the position row and the cash kv entry either all land
            # or none do -- a fill with no matching position/cash change (or
            # the reverse) is exactly the ledger corruption this closes.
            try:
                self.store.add_fill(self.today(), 0.0, side="BUY", qty=qty, price=px, fee=0.0,
                                    order_id="paper-entry", source="paper", commit=False)
                self._persist(position=new_position, cash=new_cash, commit=False)
                self.store.commit()
            except Exception:
                self.store.rollback()
                raise
            # Only now, with the transaction durably committed, does in-memory
            # state change -- a retry on this same object must never see cash
            # or a position that disagrees with what was actually committed.
            self.cash = new_cash
            self.position = new_position
            self.entry_order_id = "paper-entry"
            self.stop_order_id = "paper-stop" if new_position.stop_price else None
        elif gate.action == "SELL" and self.position.qty > 0:
            px = (snap.bid or snap.last) * (1 - slip)
            self._close(px, source="paper", order_id="paper-exit")
        return self.position

    def _close(self, px: float, *, source: str, order_id: str) -> None:
        qty = self.position.qty
        pnl = (px - self.position.entry) * qty
        new_cash = self.cash + px * qty
        try:
            self.store.add_fill(self.today(), pnl, side="SELL", qty=qty, price=px, fee=0.0,
                                order_id=order_id, source=source, commit=False)
            self.store.clear_position(commit=False)
            self.store.kv_set(PAPER_CASH_KEY, repr(new_cash), commit=False)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        self.cash = new_cash
        self.position = Position()
        self.entry_order_id = None
        self.stop_order_id = None

    def mark(self, snap: Snapshot, *, now_ms: int | None = None) -> Position:
        self.last_mark_reason = None
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
                self.last_mark_reason = "stop"
                self._close(px, source="paper_stop", order_id="paper-stop")
        reason = local_exit_reason(self.position, snap, self.settings, now_ms if now_ms is not None else int(time.time() * 1000))
        if reason:
            self.last_mark_reason = reason
            px = (snap.bid or snap.last) * (1 - self.settings.paper_slippage_bps / 10000)
            self._close(px, source=f"paper_{reason}", order_id="paper-exit")
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
        self._require_live_store()
        self._reject_paper_provenance()
        self._exit_hint: float | None = None
        self.last_mark_reason: str | None = None
        # The single most useful fact for a 3am operator reading an exit-6 halt:
        # was the resident stop actually seen present/absent the last time
        # reconcile() looked? Set only where reconcile() actually observed it
        # (see reconcile() below); never inferred, and honestly "unknown" when
        # no observation was made (e.g. a pending stop replacement blocks the
        # read-only path from ever inspecting the resident stop at all).
        self.last_stop_observation: str | None = None

    def _require_live_store(self) -> None:
        """The store itself must be a live store, not merely row-clean.

        Checking only the position row left the hole open on an EMPTY database:
        a paper-stamped or unidentified store has no row to look at, so live
        hands attached to it happily sent a real market order and a trigger.
        The mode of the file is the fact; the row is only corroboration.
        """
        if getattr(self.store, "mode", None) != "live":
            raise StoreIdentityMismatch(
                f"live hands require a store opened as 'live'; got "
                f"{getattr(self.store, 'mode', None)!r} for {self.store.path}"
            )

    def _reject_paper_provenance(self) -> None:
        """Never adopt a simulated position as a real one.

        The database is already separated by mode and stamped, but this row is
        what `reconcile()` sizes a resident stop from. If anything about it says
        paper, the honest move is to stop and make a human look: the account
        holds the owner's own BTC, and a trigger sized from a position the bot
        never bought would sit on top of their coins.
        """
        if not self.position.is_open() and not self.entry_order_id and not self.stop_order_id:
            return
        marks = {
            "entry_source": self.position.entry_source,
            "entry_order_id": self.entry_order_id,
            "stop_order_id": self.stop_order_id,
        }
        tainted = {k: v for k, v in marks.items() if isinstance(v, str) and v.startswith("paper")}
        if tainted:
            raise StoreIdentityMismatch(
                f"live hands refuse a position carrying paper provenance {tainted}; "
                "square the account by hand and clear the row before trading live"
            )

    # -- helpers ----------------------------------------------------------------

    @property
    def qty_scale(self) -> int:
        return self.rules.qty_scale if self.rules else self.settings.qty_scale

    @property
    def tol(self) -> float:
        return 10 ** (-self.qty_scale)

    def today(self) -> str:
        return _today()

    def _persist(self, state: str | None = None, *, commit: bool = True) -> None:
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
            take_profit_price=self.position.take_profit_price,
            opened_ts=self.position.opened_ts,
            exit_reason=self.position.exit_reason,
            commit=commit,
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
        self._guard_exit()
        self._guard_stop_submission()
        if not stop_price:
            return None
        operation = {"phase": "submitting", "qty": qty_s, "stop_price": stop_price,
                     "entry_id": self.entry_order_id}
        self.store.kv_set(STOP_SUBMISSION_KEY, json.dumps(operation))
        self._persist()  # preserve entry PENDING; the durable latch survives a crash
        try:
            resp = self.client.place_trigger(
                currency="BTC", market="USDT", side="SELL",
                trigger_price=f"{stop_price:.{self.rules.price_scale if self.rules else 2}f}",
                trigger_type="LE", quantity=qty_s, amount="0", market_order=True,
            )
        except Exception as exc:
            if isinstance(exc, KcexError) and exc.request_rejected:
                operation["phase"] = "rejected"
                try:
                    self.store.kv_set(STOP_SUBMISSION_KEY, json.dumps(operation))
                    self._persist("UNPROTECTED")
                except Exception as storage_exc:
                    self._halt_stop_submission(storage_exc)
                log.error("stop request rejected by HTTP %s; no retry", exc.http_status)
                return None  # caller may flatten; a failed fallback stays halted
            self._halt_stop_submission(exc)
        try:
            stop_id = resp.get("data") if isinstance(resp, dict) else None
            if type(stop_id) not in (str, int) or not str(stop_id).strip() or stop_id == 0:
                raise ValueError("stop response contains no known order id")
            self.store.remember_order(str(stop_id))
            self.stop_order_id = str(stop_id)
            self._persist("OPEN")  # durable id before releasing the submission latch
            self.store.kv_set(STOP_SUBMISSION_KEY, "")
            return self.stop_order_id
        except Exception as exc:
            self._halt_stop_submission(exc)

    def _halt_stop_submission(self, cause: Exception | None = None) -> NoReturn:
        try:
            self._persist("UNPROTECTED")
        except Exception:
            log.exception("could not persist halted position; submission marker remains")
        raise UnprotectedPosition(
            "unfinished stop submission; inspect exchange and manually resolve orphan triggers; no retry or automatic SELL"
        ) from cause

    def _guard_stop_submission(self) -> None:
        if self.store.kv_get(STOP_SUBMISSION_KEY):
            self._halt_stop_submission()

    def _guarded_kv_get(self, key: str) -> str | None:
        """Read one kv key while guarding, treating a storage failure the same
        as an unresolved EXIT latch: fatal, human-review-required, never
        swallowed by the CLI's generic "keep the loop alive" retry branch. A
        bare RuntimeError from ``kv_get`` here used to propagate untyped, so
        nothing in ``bot/cli.py`` could tell it apart from an ordinary
        transient failure -- which is exactly the livelock this closes."""
        try:
            return self.store.kv_get(key)
        except Exception as exc:
            raise ExitLatchBlocked(
                f"could not read {key!r} from storage ({exc}); a guard read failure is "
                "treated the same as an unresolved exit latch -- human review required, "
                "never reinterpreted as 'no latch present'"
            ) from exc

    def _load_exit_latch(self) -> dict | None:
        """Parse the EXIT latch, or raise if it cannot be trusted.

        A read failure from the store itself (e.g. a corrupt database) is not
        swallowed or reinterpreted as "no latch present": it is wrapped as its
        own ExitLatchBlocked (see _guarded_kv_get) so the CLI maps it to the
        same explicit halt as every other latch failure, instead of falling
        into the generic retry-forever branch.
        """
        raw = self._guarded_kv_get(EXIT_LATCH_KEY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception as exc:
            raise ExitLatchBlocked(
                "exit latch is corrupt (invalid JSON); human review required, never cleared automatically"
            ) from exc
        if not isinstance(data, dict):
            raise ExitLatchBlocked(
                f"exit latch is not a single record ({type(data).__name__}); human review required"
            )
        if any(field not in data for field in EXIT_LATCH_REQUIRED_FIELDS):
            raise ExitLatchBlocked("exit latch is missing required fields; human review required")
        if data.get("schema") != EXIT_LATCH_SCHEMA:
            raise ExitLatchBlocked(
                f"exit latch has an unrecognised schema {data.get('schema')!r}; human review required"
            )
        if data.get("entry_id") not in (None, self.entry_order_id):
            raise ExitLatchBlocked(
                "exit latch entry id disagrees with the current position row; human review required"
            )
        return data

    def _guard_exit(self) -> None:
        """Fence execute/mark/reconcile/replace_stop/flatten/stop-restoration
        while an EXIT latch is active or ambiguous.

        Resolution happens only through the latch's own explicit transitions,
        never through this guard and never through the generic reconcile path
        that otherwise restores a stop. A legacy CLOSING row written before
        this guard existed is fenced the same way: a stop cancel may still be
        settling on the exchange and a timeout does not prove otherwise.
        """
        raw_exit = self._guarded_kv_get(EXIT_LATCH_KEY)
        if raw_exit and (self._guarded_kv_get(STOP_SUBMISSION_KEY) or self._guarded_kv_get(STOP_REPLACE_KEY)):
            raise ExitLatchBlocked(
                "exit latch coexists with an unfinished stop submission/replacement; "
                "halting rather than picking one and erasing the other -- human review required"
            )
        latch = self._load_exit_latch()
        if latch is not None:
            raise ExitLatchBlocked(
                f"exit latch active (op={latch.get('operation_id')!r}, phase={latch.get('phase')!r}); "
                "resolve only through its own explicit transitions or human review"
            )
        if self.position.state == "CLOSING":
            raise ExitLatchBlocked(
                "position is CLOSING with no exit latch (legacy row, or a crash before this "
                "guard existed); a stop cancel may still be settling on the exchange -- refusing "
                "further writes until a human confirms the exchange state"
            )

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

    # -- stop replacement foundation (not wired to a trailing policy) ------------

    def replace_stop(self, stop_price: float, snap: Snapshot) -> Position:
        """Replace one owned resident stop; never retry its POST."""
        self._guard_exit()
        self._guard_stop_submission()
        if self.store.kv_get(STOP_REPLACE_KEY):
            raise UnprotectedPosition("unfinished stop replacement; reconcile before any write")
        if self.settings.mode != "live" or self.position.state != "OPEN":
            raise ValueError("replacement requires an open live position")
        if not self.stop_order_id or not self.store.is_bot_order(self.stop_order_id):
            raise PositionStuck("replacement cannot cancel an unrecorded stop")
        if (not math.isfinite(stop_price) or not math.isfinite(snap.bid) or snap.stale
                or stop_price <= 0 or stop_price >= snap.bid
                or self.position.stop_price is None or stop_price <= self.position.stop_price):
            raise ValueError("replacement requires a higher positive stop below a fresh bid")
        scale = self.rules.price_scale if self.rules else 2
        stop_price = float(Decimal(str(stop_price)).quantize(Decimal(1).scaleb(-scale), rounding=ROUND_DOWN))
        if stop_price <= self.position.stop_price:
            raise ValueError("replacement must improve at venue precision")
        operation = {"phase": "cancel", "old_id": self.stop_order_id,
                     "stop_price": stop_price, "snapshot": snap.__dict__,
                     "entry_id": self.entry_order_id, "qty": self.position.qty}
        operation["snapshot"] = {**snap.__dict__, "bars_15m": []}
        self.store.kv_set(STOP_REPLACE_KEY, json.dumps(operation))
        self._persist("UNPROTECTED")  # durable BEFORE cancel, including lost responses
        try:
            self.cancel_if_ours(self.stop_order_id)
        except Exception as exc:
            raise UnprotectedPosition("stop cancel ambiguous; reconcile owned id") from exc
        self.reconcile()
        return self.position

    def _replacement_orders(self) -> set[str]:
        """Use the same complete-list proof as ordinary exits and reconciliation."""
        return open_order_ids(self.client)

    def _replacement_holding(self) -> None:
        """Require the exact known holding; a stop fill/partial/owner move is not
        permission to place another SELL over the owner's remaining BTC.
        """
        rows = _extract_list(self.client.balances("BTC"))
        btc = [row for row in rows if isinstance(row, dict) and row.get("currency") == "BTC"]
        if len(btc) != 1 or "available" not in btc[0] or "frozen" not in btc[0]:
            raise ValueError("unknown BTC balance")
        amounts = [float(btc[0][key]) for key in ("available", "frozen")]
        base = self.position.btc_before
        if (base is None or not math.isfinite(base) or base < 0
                or any(not math.isfinite(v) or v < 0 for v in amounts)
                or not math.isclose(sum(amounts), base + self.position.qty, rel_tol=0, abs_tol=1e-12)):
            raise ValueError("BTC holding changed during replacement; inspect before any SELL")

    def _reconcile_replacement(self, operation: dict) -> str:
        """Write-ahead phases deliberately survive restart, even with opt-out.

        A crash after writing `placing` cannot prove whether POST reached KCEX.
        No new POST/SELL is safe then: an unrecorded stop could sell owner's BTC.
        """
        if operation["phase"] != "cancel":
            self._persist("UNPROTECTED")
            raise UnprotectedPosition("stop replacement write ambiguous; human inspection required")
        if (self.settings.mode != "live" or not self.position.is_open()
                or operation["entry_id"] != self.entry_order_id or operation["qty"] != self.position.qty
                or not self.store.is_bot_order(operation["old_id"])):
            raise UnprotectedPosition("stop replacement does not match resident position")
        try:
            ids = self._replacement_orders()
            if operation["old_id"] in ids:
                self._persist("OPEN")
                # Keep the operation: a delayed cancel may still remove this stop.
                return "replacement_cancel_not_confirmed"
        except Exception as exc:
            self._persist("UNPROTECTED")
            raise UnprotectedPosition("stop cancel confirmation unavailable") from exc
        self.stop_order_id = None
        self._persist("UNPROTECTED")
        self._replacement_holding()
        operation["phase"] = "placing"
        self.store.kv_set(STOP_REPLACE_KEY, json.dumps(operation))
        snap = Snapshot(**operation["snapshot"])
        stop_price = operation["stop_price"]
        qty_s = _fmt_qty(self.position.qty, self.qty_scale)
        try:
            response = self.client.place_trigger(
                currency="BTC", market="USDT", side="SELL",
                trigger_price=f"{stop_price:.{self.rules.price_scale if self.rules else 2}f}",
                trigger_type="LE", quantity=qty_s, amount="0", market_order=True,
            )
        except Exception as exc:
            # Only these transport rejections prove the request was not accepted.
            # Unknown venue codes / server errors / lost responses are ambiguous.
            if isinstance(exc, KcexError) and exc.request_rejected:
                self._replacement_holding()
                operation["phase"] = "flattening"
                self.store.kv_set(STOP_REPLACE_KEY, json.dumps(operation))
                if self._flatten(qty_s, snap):
                    self.store.kv_set(STOP_REPLACE_KEY, "")
                    return "replacement_rejected_flattened"
            raise UnprotectedPosition("stop replacement failed; inspect exchange") from exc
        stop_id = response.get("data") if isinstance(response, dict) else None
        if type(stop_id) not in (str, int) or not str(stop_id).strip() or stop_id == 0:
            raise UnprotectedPosition("stop response has no known id; do not retry or flatten")
        self.stop_order_id = str(stop_id)
        self.store.remember_order(self.stop_order_id)
        ids = self._replacement_orders()
        if self.stop_order_id not in ids or operation["old_id"] in ids:
            raise UnprotectedPosition("replacement protection not confirmed; inspect known order ids")
        self.position.stop_price = stop_price
        self._persist("OPEN")
        self.store.kv_set(STOP_REPLACE_KEY, "")
        return "stop_replaced"

    # -- entry ------------------------------------------------------------------

    def execute(self, gate: GateResult, snap: Snapshot) -> Position:
        if self.settings.mode != "live":
            raise RuntimeError("LiveHands requires MODE=live")
        self._guard_exit()
        self._guard_stop_submission()
        if self.store.kv_get(STOP_REPLACE_KEY):
            raise UnprotectedPosition("unfinished stop replacement blocks order execution")
        if gate.action == "BUY" and gate.qty and gate.stop_price:
            return self._buy(gate, snap)
        if gate.action == "SELL" and gate.qty:
            # M1: refuse to start a discretionary exit -- see TerminalEvidenceUnavailable
            # and the EXIT_LATCH_KEY comment above. `_sell()` below is kept as tested
            # infrastructure for the day terminal evidence is captured; it is not
            # reachable from here until then, and it now fences itself with the
            # same _guard_exit() call every other write path uses (2026-09
            # re-review: calling it directly used to bypass an EXIT latch).
            observation = self.last_stop_observation or "unknown (no reconcile observation available)"
            raise TerminalEvidenceUnavailable(
                f"cannot safely start a discretionary SELL of {gate.qty} BTC "
                f"(rule={gate.rule!r}): KCEX order-history/deals payload shapes that "
                "would prove the resident stop is truly inactive after a cancel are not "
                "captured, so cancelling it now and discovering that later would leave "
                "a real position unprotected. This attempt changed no orders -- no "
                "cancel/place was sent -- but that alone does not prove any resident stop "
                "is still protecting the position; confirming protection requires "
                f"inspecting the exchange directly. Last observed resident-stop state "
                f"before this refusal: {observation}. HALT CONTRACT: this process exits "
                "now (exit code 6) and will NOT retry, repair, or resume on its own -- "
                "no later cycle (not the next LLM cycle, not a reconcile()) runs after "
                "this process exits, so if the resident stop is in fact gone the position "
                "may be sitting on the exchange completely UNPROTECTED for as long as it "
                "takes a human to notice. A human must inspect the exchange directly and "
                "either restore protection or close the position by hand before the bot "
                "runs again. Capture terminal evidence (docs/kcex-spot-api.md) to close "
                "this gap for good."
            )
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
            take_profit_price=float(gate.take_profit_price) if gate.take_profit_price else None,
            opened_ts=_opened_now(),
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
        # Snapshot before mutating: on a transaction failure below, memory must
        # not diverge from the still-uncommitted-old row (invariant 1's PENDING
        # write with the estimate) -- restoring it here means no separate
        # "undo" step, only "never applied".
        snapshot = replace(self.position)
        self.position.qty = qty
        self.position.entry = price
        self.position.entry_source = source
        target = take_profit_for_entry(price, snap.atr or 0, self.settings, self.rules)
        self.position.take_profit_price = float(target) if target else None
        if snap.atr:
            self.position.stop_price = float(stop_for_entry(price, snap.atr, self.settings, self.rules))
        # The confirmed position update and the BUY fill are one local
        # transaction: recording the fill without the matching qty/price/stop
        # update (or the reverse) is exactly the ledger corruption L6 closes.
        try:
            self._persist(commit=False)
            self.store.add_fill(self.today(), 0.0, side="BUY", qty=qty, price=price, fee=0.0,
                                order_id=entry_id, source=source, commit=False)
            self.store.commit()
        except Exception:
            self.store.rollback()
            self.position = snapshot
            raise

        qty_s = _fmt_qty(qty, self.qty_scale)
        stop_id = self._place_stop(qty_s, self.position.stop_price)
        if stop_id:
            self.stop_order_id = stop_id
            self._persist("OPEN")
            return self.position

        log.error("stop could not be placed for %s BTC; flattening", qty_s)
        if self._flatten(qty_s, snap):
            try:
                self.store.kv_set(STOP_SUBMISSION_KEY, "")
            except Exception as exc:
                self._halt_stop_submission(exc)
            return self.position
        self._persist("UNPROTECTED")
        raise UnprotectedPosition(f"long {qty_s} BTC (entry {entry_id}) has no stop and could not be flattened")

    def _flatten(self, qty_s: str, snap: Snapshot) -> bool:
        self._guard_exit()
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
        # The fill and the position clear are one local transaction -- an
        # orphan fill with the row still open (or the reverse) is exactly the
        # ledger corruption L6 closes. _flatten() never raises on its own
        # (every earlier failure here returns False, not an exception), so a
        # transaction failure is caught the same way: logged and reported as
        # "not confirmed", which is what already drives the caller's loud
        # UnprotectedPosition halt. The exchange-side sell already executed
        # (confirmed by _watch_balance above) regardless of whether this local
        # commit succeeds, so this is an AT-MOST-ONCE local record, not proof
        # the sale itself is undone.
        try:
            self.store.add_fill(self.today(), pnl, side="SELL", qty=qty, price=price, fee=0.0,
                                order_id=sell_id, source=f"flatten_{source}", commit=False)
            self.store.clear_position(commit=False)
            self.store.commit()
        except Exception as exc:
            self.store.rollback()
            log.error("flatten executed on the exchange but the local fill/clear could not be committed: %s", exc)
            return False
        self.position = Position()
        self.entry_order_id = None
        self.stop_order_id = None
        return True

    # -- exit -------------------------------------------------------------------

    def mark(self, snap: Snapshot, *, now_ms: int | None = None) -> Position:
        """Cheap every tick; private reads only when a local exit is due.
        One resident LE stop: GE exists, but exchange-side OCO is unproven.
        """
        self._guard_exit()
        self.last_mark_reason = None
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        reason = local_exit_reason(self.position, snap, self.settings, now)
        if reason:
            self.last_mark_reason = reason  # retain trigger context even on a reads outage
            # repair=False: read-only w.r.t. the exchange. execute() below
            # unconditionally refuses a live discretionary SELL right now
            # (TerminalEvidenceUnavailable), so a refused barrier must make
            # ZERO order-side writes -- including reconcile's own ordinary
            # "restore a missing stop" repair, which is a real place_trigger
            # POST. This still detects and books a position that ALREADY
            # closed on the exchange (the resident stop firing is the
            # exchange's own prior action, not a write this call chooses to
            # make); it only skips restoring a stop it finds missing. The
            # 2026-09 re-review's exact reproduction: stop absent from the
            # open-order book (cancelled or executed -- indistinguishable),
            # balance still reading as fully held -- used to restore a fresh
            # stop here before ever reaching the refusal below.
            self.reconcile(repair=False)
            reason = local_exit_reason(self.position, snap, self.settings, now)
            if reason:
                self.last_mark_reason = reason
                return self.execute(GateResult(True, reason, "SELL", qty=_fmt_qty(self.position.qty, self.qty_scale)), snap)
            self.last_mark_reason = "reconcile"  # no invented TP fill when exchange already exited
        return self.position

    def _sell(self, snap: Snapshot, *, exit_reason: str | None = None) -> Position:
        # Fenced like every other write path, even though execute()/mark() no
        # longer reach it: a latch present from a foreign/future writer, or a
        # legacy CLOSING row, must block a direct call too. The 2026-09
        # re-review found this method sent a real DELETE + market SELL when
        # called directly with a `sell_submitting` latch present -- proof it
        # was not actually "ready, fenced infrastructure" as claimed.
        self._guard_exit()
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

        self.position.exit_reason = exit_reason
        self._persist()
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
            # order that actually executed -- fully, partially, or not at all.
            # Re-read the balance before putting a stop back: a trigger sized
            # for BTC we no longer fully own would sit on top of the owner's
            # own coins and could freeze and sell them.
            log.error("sell failed: %s; re-reading the balance before restoring the stop", exc)
            try:
                after = btc_total(self.client)
            except Exception as read_exc:  # noqa: BLE001
                log.error("balance unreadable after the failed sell: %s", read_exc)
                after = None
            if after is None:
                # No evidence of how much, if any, actually sold. Guessing
                # here is exactly what places an oversized stop over BTC we
                # no longer hold (or leaves BTC we still hold unprotected).
                # Halt instead of guessing; no stop POST is sent.
                self._persist("UNPROTECTED")
                raise UnprotectedPosition(
                    f"sell failed and the balance could not be read afterwards; "
                    f"{qty_s} BTC position is of unknown remaining size and unprotected"
                ) from exc

            sold = start - after
            if sold >= qty - self.tol:
                log.warning("the sell had in fact executed; booking it instead of restoring a stop")
                price, source = self._fill_price("", default=snap.bid or snap.last)
                pnl = (price - self.position.entry) * qty
                # One local transaction: an orphan fill with the row still
                # open (or the reverse) is exactly the bug this closes.
                try:
                    self.store.add_fill(self.today(), pnl, side="SELL", qty=qty, price=price, fee=0.0,
                                        order_id=None, source=exit_reason or f"recovered_{source}", commit=False)
                    self.store.clear_position(commit=False)
                    self.store.commit()
                except Exception:
                    self.store.rollback()
                    raise
                self.position = Position()
                self.entry_order_id = None
                self.stop_order_id = None
                return self.position
            if sold < -self.tol:
                # The account gained BTC while we thought we were selling --
                # the numbers contradict each other and any stop size here
                # would be a guess.
                self._persist("UNPROTECTED")
                raise UnprotectedPosition(
                    f"sell failed and the balance moved the wrong way (start {start:.8f}, "
                    f"after {after:.8f}); cannot establish the remaining {qty_s} BTC position"
                ) from exc

            # Partial fill (or nothing sold, when sold <= tol): protect only
            # what we still actually hold, never the original full size.
            if not all(math.isfinite(v) for v in (start, after, sold)):
                # Never let a bad parse do arithmetic: qty - nan collapses to
                # zero, which would send a 0.00000 trigger and drop a live row.
                self._persist("UNPROTECTED")
                raise UnprotectedPosition(
                    f"sell failed and the balance is not a finite number; the {qty_s} BTC "
                    "position size cannot be established"
                ) from exc
            # Book and shrink by the SAME quantised figure. Using `> tol` for
            # the booking while shrinking unconditionally lost exactly one lot:
            # 0.00025 -> 0.00024 lands at 9.999999999999999e-06, just under the
            # tolerance, so no fill was written while the row still shrank.
            sold_q = _floor_qty(max(sold, 0.0), self.qty_scale)
            remaining = max(0.0, qty - sold_q)
            if sold_q > 0:
                # Book the BTC that actually left BEFORE shrinking the row.
                # Without this the ledger silently loses that PnL forever:
                # day-loss, the post-loss cooldown and the journal would all
                # stop seeing a loss that really happened.
                log.warning(
                    "sell failed but %.8f of %.8f BTC actually sold; booking the partial "
                    "and protecting only the remainder", sold_q, qty,
                )
                price, source = self._fill_price("", default=snap.bid or snap.last)
                # The partial fill and the qty reduction are one local
                # transaction (L6): recording the partial sale with the row
                # still at the OLD qty (or shrinking the row with no matching
                # fill) is exactly the ledger corruption this closes. This is
                # committed BEFORE any stop is attempted below -- a real POST,
                # checkpointed separately by _place_stop's own write-ahead
                # pattern -- so the durable fill/qty record does not depend on
                # whether protection can be restored afterwards.
                prev_qty = self.position.qty
                self.position.qty = remaining
                try:
                    self.store.add_fill(
                        self.today(), (price - self.position.entry) * sold_q, side="SELL",
                        qty=sold_q, price=price, fee=0.0, order_id=None,
                        source=f"partial_{exit_reason or source}", commit=False,
                    )
                    self._persist(commit=False)
                    self.store.commit()
                except Exception:
                    self.store.rollback()
                    self.position.qty = prev_qty
                    raise
            else:
                self.position.qty = remaining  # remaining == qty; nothing was sold, nothing to book
            remaining_s = _fmt_qty(remaining, self.qty_scale)
            stop_id = self._place_stop(remaining_s, self.position.stop_price)
            if stop_id:
                self.stop_order_id = stop_id
                self.position.exit_reason = None
                self._persist("OPEN")
                return self.position
            self._persist("UNPROTECTED")
            raise UnprotectedPosition(f"sell failed and stop could not be restored for {remaining_s} BTC") from exc

        sell_id = self._order_id(resp)
        self.store.remember_order(sell_id)
        sold = self._watch_balance(lambda total: start - total, qty)
        if sold is None or sold < qty - self.tol:
            log.warning("sell %s accepted but not confirmed by balance; reconcile will settle it", sell_id)
            self._persist("CLOSING")
            return self.position
        price, source = self._fill_price(sell_id, default=snap.bid or snap.last)
        pnl = (price - self.position.entry) * qty
        try:
            self.store.add_fill(self.today(), pnl, side="SELL", qty=qty, price=price, fee=0.0,
                                order_id=sell_id, source=exit_reason or source, commit=False)
            self.store.clear_position(commit=False)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        self.position = Position()
        self.entry_order_id = None
        self.stop_order_id = None
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

    def reconcile(self, *, repair: bool = True) -> str:
        """Bring the local row in line with the exchange. Returns a short verdict.

        ``repair=False`` (used only by ``mark()``'s barrier gate, above) makes
        the exact same read-only observation of exchange state and still
        settles a position that already closed on the exchange -- that is a
        store write reflecting a fact (the resident stop firing, or a manual
        sale), not an order this call chooses to place. What it will NOT do is
        place a fresh stop for one it finds missing (``_place_stop`` is a real
        ``place_trigger`` POST): it reports ``"stop_missing"`` instead. The
        ordinary boot/LLM-cycle ``reconcile()`` (the default, ``repair=True``)
        is unchanged and still restores a missing stop as before -- this
        parameter only lets a refused discretionary exit make zero order-side
        writes instead of repairing first and refusing second.

        The SAME rule applies to a pending stop replacement (``STOP_REPLACE_KEY``):
        the 2026-09 third-round re-review found ``_reconcile_replacement`` itself
        performs real exchange writes -- a ``place_trigger`` POST once it decides
        an ambiguous cancel is confirmed, or a ``place_market`` SELL via
        ``_flatten`` on a rejected replacement -- and ``repair=False`` did not
        gate that resolver at all, only the ordinary stop-restoration branch
        below. So under ``repair=False`` a pending replacement is never handed
        to the resolver, in ANY phase: the operation is left exactly as found
        (preserving the uncertainty, not resolving it "quickly first") and this
        reports ``"stop_replacement_pending"`` instead.
        """
        self._guard_exit()
        self._guard_stop_submission()
        operation = self.store.kv_get(STOP_REPLACE_KEY)
        if operation:
            if not repair:
                self.last_stop_observation = "unknown (stop replacement pending; not inspected)"
                return "stop_replacement_pending"
            try:
                return self._reconcile_replacement(json.loads(operation))
            except Exception as exc:
                # Even a storage/parser failure must preserve the fatal exit, not
                # let the main loop back off and resume ordinary order execution.
                try:
                    self._persist("UNPROTECTED")
                except Exception:
                    log.exception("replacement state persistence failed; durable marker remains")
                raise UnprotectedPosition("unfinished stop replacement; inspect exchange and durable marker") from exc
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
            # Balance math alone is not proof the entry is gone for good: an
            # order that is STILL RESTING on the book can fill at any moment,
            # and dropping the row now means a later fill leaves the bot
            # holding real BTC with nothing tracking or protecting it.
            # Invariant 2 requires proof it did not fill -- it was resting and
            # got cancelled -- not merely "the balance has not moved yet".
            if self.entry_order_id and self.entry_order_id in ids:
                log.warning(
                    "pending entry %s is still resting on the book; keeping PENDING "
                    "for a later reconcile", self.entry_order_id,
                )
                return "entry_still_open"
            # The entry never actually filled (see _buy: the row is kept PENDING
            # whenever the fill could not be confirmed). There was no position, so
            # there is no exit to book -- inventing a SELL here would write a
            # fabricated PnL into the ledger.
            log.warning("pending entry %s never filled; dropping the row", self.entry_order_id)
            self.store.kv_set(FOREIGN_BTC_KEY, repr(total))
            self._clear()
            return "entry_never_filled"

        if not holding:
            # Stop hit or a manual sale. (A CLOSING row never reaches this point any
            # more: `_guard_exit()` above fences it -- see the M1 EXIT latch comment.)
            # This is still the pre-existing invariant-5 balance heuristic (kept and
            # tested, AGENTS.md) for an AUTONOMOUS exchange-side close; it is NOT the
            # bot-initiated exit path, which `TerminalEvidenceUnavailable` blocks
            # instead of ever reaching here. The same indistinguishable-delta risk the
            # 2026-09 design review raises applies in principle to this heuristic too
            # (an owner deposit/withdrawal could coincidentally offset a real stop
            # fill) -- that is a known, pre-existing, documented residual risk this
            # task does not close; fixing it needs the same captured terminal
            # evidence the review says is missing.
            return self._settle_closed_on_exchange(total, qty)

        if stop_alive:
            if self.position.state != "OPEN":
                self._persist("OPEN")
            self.last_stop_observation = "present (seen in the open-order book)"
            return "ok"

        self.last_stop_observation = "absent (not found in the open-order book)"
        if not repair:
            return "stop_missing"

        stop_id = self._place_stop(qty_s, self.position.stop_price)
        if stop_id:
            self.stop_order_id = stop_id
            self._persist("OPEN")
            log.warning("stop restored as %s", stop_id)
            return "stop_restored"
        self._persist("UNPROTECTED")
        raise UnprotectedPosition(f"{qty_s} BTC on the exchange without a stop and none could be placed")

    def _settle_closed_on_exchange(self, total: float, qty: float) -> str:
        """Book the exit and clear the row as ONE local transaction, with a
        real rollback -- not merely a hope that a crash discards it.

        M1 item 6 / 2026-09 re-review blocker 1: `add_fill`/`kv_set`/
        `clear_position` each commit internally by default (Store); commit=False
        defers all three writes to the single explicit commit below. Two
        failure boundaries matter, and both are covered:

        1. Any of the three writes, or the commit itself, can raise. Without an
           explicit rollback the connection is left `in_transaction`, and the
           *next* commit on the SAME connection -- the CLI reuses one Store
           after its backoff, it does not reopen -- durably applies the orphan
           half of this settlement. `except: self.store.rollback(); raise`
           guarantees no later, unrelated write can ever sweep up a half
           settlement.
        2. The commit can succeed and a later, fallible auxiliary step (the
           operator log line below) can still raise. In-memory state is
           synchronised with the committed row BEFORE that log call -- not
           after -- so a retry on the SAME Hands object sees a flat position
           and cannot book the same exit a second time.

        This is still an AT-MOST-ONCE attempt, not a proof of exactly-once
        settlement: it does not cover a crash between SQLite's own fsync and
        this process observing the result, and `price` here is
        `stop_price`/`entry`, an estimate, not a proven execution price.
        """
        price = self.position.stop_price or self.position.entry
        pnl = (price - self.position.entry) * qty
        state = self.position.state
        stop_order_id = self.stop_order_id
        exit_reason = self.position.exit_reason
        try:
            self.store.add_fill(self.today(), pnl, side="SELL", qty=qty, price=price, fee=0.0,
                                order_id=stop_order_id, source=exit_reason or "reconcile",
                                commit=False)
            self.store.kv_set(FOREIGN_BTC_KEY, repr(total), commit=False)
            self.store.clear_position(commit=False)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        # Memory now matches the durably committed row. This MUST happen
        # before the log call below (or any other fallible auxiliary work):
        # once committed, a retry on this same object must see a flat
        # position, never a stale open one.
        self.position = Position()
        self.entry_order_id = None
        self.stop_order_id = None
        log.warning("position closed on the exchange (state %s); pnl est %.4f", state, pnl)
        return "closed_on_exchange"
