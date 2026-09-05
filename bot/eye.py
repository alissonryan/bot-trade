"""Market data for the bot: socket first, REST fallback, plus the slow REST calls the LLM
cycle needs (klines, balances, symbol rules).

* ``poll_quotes`` never raises. A REST failure marks the quotes stale and counts the error;
  the loop keeps running and the collar blocks entries while stale.
* ``poll_heavy`` may raise in live mode when balances cannot be read: a bot that trades
  on ``free_usdt = 0`` because a call failed would be lying to itself.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from bot.atr import atr
from bot.settings import Settings
from bot.types import Bar, Snapshot, SymbolRules
from bot.ws import PriceUpdate, SpotSocket, parse_frame
from kcex.client import DEFAULT_USER_AGENT, KcexClient

log = logging.getLogger(__name__)

BAR_SECONDS = 15 * 60


class EyeError(RuntimeError):
    """A REST read the bot cannot trade without has failed."""


class Eye:
    def __init__(
        self,
        client: KcexClient,
        settings: Settings,
        *,
        bot_qty: float = 0.0,
        bot_avg_entry: float | None = None,
    ):
        self.client = client
        self.settings = settings
        self.bot_qty = bot_qty
        self.bot_avg_entry = bot_avg_entry
        self.last = 0.0
        self.bid = 0.0
        self.ask = 0.0
        self.bars: list[Bar] = []
        self.free_usdt = 0.0
        self.ws_ok = False
        self.ws_frames = 0
        self.last_update_ms = 0
        self.last_rest_ms = 0
        self.rest_errors = 0
        self.last_rest_error: str | None = None
        self.rules: SymbolRules | None = None
        self.socket: SpotSocket | None = None
        self.last_intent_action: str | None = None
        self.last_bot_pnl_usdt = 0.0

    # -- time / state -----------------------------------------------------------

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _stale(self) -> bool:
        if self.last_update_ms == 0:
            return True
        return (self._now_ms() - self.last_update_ms) > self.settings.stale_ms

    # -- socket -----------------------------------------------------------------

    def apply_update(self, update: PriceUpdate) -> None:
        if update.last is not None:
            self.last = update.last
        if update.bid is not None:
            self.bid = update.bid
        if update.ask is not None:
            self.ask = update.ask
        self.ws_ok = True
        self.ws_frames += 1
        self.last_update_ms = self._now_ms()

    def apply_ws_price(self, last: float, bid: float | None = None, ask: float | None = None) -> None:
        self.apply_update(PriceUpdate(last=last, bid=bid, ask=ask))

    def apply_frame(self, msg: dict) -> bool:
        update = parse_frame(msg)
        if update is None:
            return False
        self.apply_update(update)
        return True

    def connect_ws(self) -> SpotSocket | None:
        if not self.settings.ws_enabled or not self.settings.ws_url:
            return None
        self.socket = SpotSocket(
            self.settings.ws_url,
            self.settings.symbol,
            self.apply_update,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Origin": self.client.base_url},
        )
        self.socket.start()
        log.info("ws %s started for %s", self.settings.ws_url, self.settings.symbol)
        return self.socket

    # -- REST -------------------------------------------------------------------

    def load_rules(self) -> SymbolRules | None:
        try:
            self.rules = SymbolRules.from_trade_rules(self.client.symbol_trade_rules(self.settings.symbol))
        except Exception as exc:  # noqa: BLE001
            log.warning("symbol rules unavailable, using settings defaults: %s", exc)
            self.rules = None
        return self.rules

    def poll_quotes(self, *, force: bool = False) -> bool:
        """Refresh last/bid/ask over REST when the socket is not delivering. Never raises.
        Returns True when the quotes are fresh."""
        now = self._now_ms()
        if not force:
            if self.ws_ok and not self._stale():
                return True
            if self.last_rest_ms and now - self.last_rest_ms < self.settings.poll_seconds * 1000:
                return not self._stale()
        self.last_rest_ms = now
        try:
            ticker = self.client.ticker(self.settings.symbol)
            last = float(ticker["data"]["c"])
            depth = self.client.depth(self.settings.symbol)
            book = depth["data"]["data"]
            bids = book.get("bids") or book.get("bestBids") or []
            asks = book.get("asks") or []
            bid = float(bids[0]["p"]) if bids else last
            ask = float(asks[0]["p"]) if asks else last
        except Exception as exc:  # noqa: BLE001
            self.rest_errors += 1
            self.last_rest_error = f"{type(exc).__name__}: {exc}"
            log.warning("rest quotes failed (%d in a row): %s", self.rest_errors, self.last_rest_error)
            return False
        self.last, self.bid, self.ask = last, bid, ask
        self.last_update_ms = self._now_ms()
        self.ws_ok = False
        self.rest_errors = 0
        self.last_rest_error = None
        return True

    def poll_heavy(self) -> None:
        """Klines and balances for the LLM cycle. Raises EyeError in live mode when the
        balance cannot be read."""
        end = self._now_ms()
        start = end - 21 * BAR_SECONDS * 1000
        kl = self.client.kline(
            self.settings.symbol,
            interval="Min15",
            start=start,
            end=end,
        )
        data = kl["data"]
        volumes = data.get("v") or []
        bars = []
        for i, t in enumerate(data["t"]):
            bars.append(
                Bar(
                    t=int(t),
                    o=float(data["o"][i]),
                    h=float(data["h"][i]),
                    l=float(data["l"][i]),
                    c=float(data["c"][i]),
                    v=float(volumes[i]) if i < len(volumes) else 0.0,
                )
            )
        # The last kline is the bar still forming; ATR must only see closed bars.
        now_s = end // 1000
        if bars and bars[-1].t + BAR_SECONDS > now_s:
            bars.pop()
        self.bars = bars

        free = 0.0
        try:
            bals = self.client.balances("USDT")
            for row in bals.get("data") or []:
                if row.get("currency") == "USDT":
                    free = float(row.get("available") or 0)
        except Exception as exc:  # noqa: BLE001
            if self.settings.mode == "live":
                raise EyeError(f"balances unavailable: {exc}") from exc
            free = 0.0
        if self.settings.mode == "paper" and free <= 0:
            free = self.settings.paper_starting_usdt
        self.free_usdt = free

    def snapshot(self) -> Snapshot:
        spread = self.ask - self.bid if self.ask and self.bid else 0.0
        return Snapshot(
            ts_ms=self._now_ms(),
            last=self.last,
            bid=self.bid,
            ask=self.ask,
            spread=spread,
            bars_15m=list(self.bars),
            atr=atr(self.bars, self.settings.atr_period),
            free_usdt=self.free_usdt,
            bot_qty=self.bot_qty,
            bot_avg_entry=self.bot_avg_entry,
            ws_ok=self.ws_ok,
            stale=self._stale(),
            last_intent_action=self.last_intent_action,
            last_bot_pnl_usdt=self.last_bot_pnl_usdt,
        )

    def snapshot_rest(self) -> Snapshot:
        self.poll_quotes(force=True)
        self.poll_heavy()
        return self.snapshot()

    def health(self) -> dict[str, Any]:
        return {
            "ws_ok": self.ws_ok,
            "ws_frames": self.ws_frames,
            "ws_connected": bool(self.socket and self.socket.connected),
            "ws_reconnects": self.socket.reconnects if self.socket else 0,
            "rest_errors": self.rest_errors,
            "stale": self._stale(),
            "last_update_ms": self.last_update_ms,
        }
