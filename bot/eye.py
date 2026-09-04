from __future__ import annotations

import time
from typing import Any

from bot.atr import atr
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
        self.last_update_ms = 0
        self.last_intent_action: str | None = None
        self.last_bot_pnl_usdt = 0.0

    def _stale(self) -> bool:
        if self.last_update_ms == 0:
            return True
        return (int(time.time() * 1000) - self.last_update_ms) > self.settings.stale_ms

    def apply_ws_price(self, last: float, bid: float | None = None, ask: float | None = None) -> None:
        self.last = last
        if bid is not None:
            self.bid = bid
        if ask is not None:
            self.ask = ask
        self.ws_ok = True
        self.last_update_ms = int(time.time() * 1000)

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

    def snapshot_rest(self) -> Snapshot:
        ticker = self.client.ticker(self.settings.symbol)
        last = float(ticker["data"]["c"])
        depth = self.client.depth(self.settings.symbol)
        book = depth["data"]["data"]
        bid = float(book["bids"][0]["p"])
        ask = float(book["asks"][0]["p"])
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
        bals = self.client.balances("USDT")
        free = 0.0
        for row in bals.get("data") or []:
            if row.get("currency") == "USDT":
                free = float(row.get("available") or 0)
        self.last, self.bid, self.ask = last, bid, ask
        self.bars = bars
        self.free_usdt = free
        self.last_update_ms = int(time.time() * 1000)
        self.ws_ok = False
        return self.snapshot()
