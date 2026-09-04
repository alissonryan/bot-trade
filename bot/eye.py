from __future__ import annotations

import time
from typing import Any

from bot.atr import atr
from bot.hub import Hub
from bot.settings import Settings
from bot.types import Bar, Snapshot
from kcex.client import KcexClient


class Eye:
    def __init__(
        self,
        client: KcexClient,
        settings: Settings,
        *,
        bot_qty: float = 0.0,
        bot_avg_entry: float | None = None,
        hub: Hub | None = None,
    ):
        self.client = client
        self.settings = settings
        self.bot_qty = bot_qty
        self.bot_avg_entry = bot_avg_entry
        self.hub = hub or Hub()
        self.last = 0.0
        self.bid = 0.0
        self.ask = 0.0
        self.bars: list[Bar] = []
        self.free_usdt = 0.0
        self.ws_ok = False
        self.last_update_ms = 0
        # Tracked separately from last_update_ms: the depth (bid/ask) channel
        # can die while the ticker channel stays healthy, and vice versa.
        self.depth_update_ms = 0
        self._depth_rest_failed = False
        self.last_intent_action: str | None = None
        self.last_bot_pnl_usdt = 0.0

    def _stale(self) -> bool:
        if self.last_update_ms == 0:
            return True
        return (int(time.time() * 1000) - self.last_update_ms) > self.settings.stale_ms

    def _depth_stale(self) -> bool:
        """True when bid/ask have not been refreshed recently enough.

        Independent of `_stale()` (which only tracks `last`). Reuses the same
        `stale_ms` threshold rather than introducing another env var.
        """
        if self.depth_update_ms == 0:
            return True
        return (int(time.time() * 1000) - self.depth_update_ms) > self.settings.stale_ms

    def apply_ws_price(self, last: float, bid: float | None = None, ask: float | None = None) -> None:
        self.last = last
        now_ms = int(time.time() * 1000)
        if bid is not None:
            self.bid = bid
        if ask is not None:
            self.ask = ask
        if bid is not None or ask is not None:
            self.depth_update_ms = now_ms
        self.ws_ok = True
        self.last_update_ms = now_ms

    def apply_frame(self, msg: dict) -> None:
        last = msg.get("last") or msg.get("c") or msg.get("p")
        if last is None and isinstance(msg.get("data"), dict):
            last = msg["data"].get("last") or msg["data"].get("c")
        if last is None:
            return
        bid = msg.get("bid")
        ask = msg.get("ask")
        data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
        if bid is None:
            bid = data.get("bid")
        if ask is None:
            ask = data.get("ask")
        self.apply_ws_price(
            float(last),
            float(bid) if bid is not None else None,
            float(ask) if ask is not None else None,
        )

    def sync_hub(self) -> None:
        """Pull the shared Hub's latest snapshot into this Eye's own fields."""
        # A hub that is "up" but has never seen a real price (last == 0.0) must
        # never be copied in: a zero `last` would look fresh to poll_quotes(),
        # silence REST forever, and reach the collar / stop-out logic.
        if not self.hub.ws_ok or self.hub.last <= 0:
            return
        self.last = self.hub.last
        if self.hub.bid:
            self.bid = self.hub.bid
        if self.hub.ask:
            self.ask = self.hub.ask
        if self.hub.depth_ts_ms and self.hub.depth_ts_ms > self.depth_update_ms:
            self.depth_update_ms = self.hub.depth_ts_ms
        self.ws_ok = True
        self.last_update_ms = self.hub.ts_ms or int(time.time() * 1000)

    def apply_event(self, event) -> None:
        self.hub.apply(event)
        self.sync_hub()

    def connect_ws(self) -> None:
        # Full socket loop wiring lands in Task 8; this stays a safe no-op
        # regardless of whether KCEX_WS_URL is configured. REST stays primary.
        return

    def start_ws_thread(self) -> None:
        """Start the (single) background WS thread for this process, if configured.

        No-ops when `settings.ws_url` is empty (REST-only). Otherwise spawns
        exactly one daemon thread that owns the one-and-only KCEX public WS
        connection for this process: it feeds parsed events into the shared
        `Hub` via `apply_event`, and reconnects with a short backoff on any
        error so the loop never dies quietly.
        """
        if not self.settings.ws_url:
            return
        import threading

        from kcex.ws import PublicSpotWs, default_connect

        # Printed state is transition-based so a sustained outage logs once,
        # not every 2s reconnect attempt. Starts True so the first failure is
        # always reported, even if it happens on the very first connect.
        state = {"healthy": True}

        def _mark_down(exc: Exception | None) -> None:
            self.hub.mark_down()
            self.ws_ok = False
            if state["healthy"]:
                state["healthy"] = False
                print(f"ws caiu: {exc}. usando REST ate reconectar")

        def on_event(ev: Any) -> None:
            if not state["healthy"]:
                state["healthy"] = True
                print("ws reconectado")
            self.apply_event(ev)

        def on_error(exc: Exception) -> None:
            _mark_down(exc)

        def loop() -> None:
            while True:
                try:
                    ws = PublicSpotWs(self.settings.ws_url, self.settings.symbol, default_connect)
                    ws.pump(on_event=on_event, on_error=on_error)
                except Exception as exc:
                    _mark_down(exc)
                time.sleep(2)

        threading.Thread(target=loop, daemon=True).start()

    def snapshot(self) -> Snapshot:
        spread = self.ask - self.bid if self.ask and self.bid else 0.0
        return Snapshot(
            ts_ms=int(time.time() * 1000),
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

    def _poll_depth_rest(self) -> None:
        depth = self.client.depth(self.settings.symbol)
        book = depth["data"]["data"]
        self.bid = float(book["bids"][0]["p"])
        self.ask = float(book["asks"][0]["p"])
        self.depth_update_ms = int(time.time() * 1000)

    def poll_quotes(self) -> None:
        """Cheap REST tick used every loop when WS is not live (LLM-Auto-Trader style)."""
        self.sync_hub()
        if self.ws_ok and not self._stale():
            # The ticker/last path is satisfied by WS, so skip the full REST
            # snapshot -- but depth (bid/ask) rides a separate channel that can
            # be silently absent, so it gets its own freshness check and its
            # own cheap REST top-up rather than being silenced along with it.
            if self._depth_stale():
                # Best-effort: the ticker path is healthy, so a failed depth
                # top-up must degrade (stale bid/ask, still visible via
                # depth_update_ms) rather than kill the unattended loop.
                try:
                    self._poll_depth_rest()
                    self._depth_rest_failed = False
                except Exception as exc:
                    if not self._depth_rest_failed:
                        self._depth_rest_failed = True
                        print(f"depth REST falhou: {exc}. bid/ask seguem defasados")
            return
        ticker = self.client.ticker(self.settings.symbol)
        last = float(ticker["data"]["c"])
        self.last = last
        self._poll_depth_rest()
        self.last_update_ms = int(time.time() * 1000)
        self.ws_ok = False

    def poll_heavy(self) -> None:
        end = int(time.time() * 1000)
        start = end - 20 * 15 * 60 * 1000
        kl = self.client.kline(
            self.settings.symbol,
            interval="Min15",
            start=start,
            end=end,
        )
        data = kl["data"]
        bars = []
        for i, t in enumerate(data["t"]):
            bars.append(
                Bar(
                    t=int(t),
                    o=float(data["o"][i]),
                    h=float(data["h"][i]),
                    l=float(data["l"][i]),
                    c=float(data["c"][i]),
                    v=float(data.get("v", [0] * len(data["t"]))[i] if data.get("v") else 0),
                )
            )
        free = 0.0
        try:
            bals = self.client.balances("USDT")
            for row in bals.get("data") or []:
                if row.get("currency") == "USDT":
                    free = float(row.get("available") or 0)
        except Exception:
            free = 0.0
        if self.settings.mode == "paper" and free <= 0:
            free = self.settings.paper_starting_usdt
        self.bars = bars
        self.free_usdt = free

    def snapshot_rest(self) -> Snapshot:
        self.poll_quotes()
        self.poll_heavy()
        return self.snapshot()
