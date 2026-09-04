from __future__ import annotations

import time

from kcex.ws import DealEvent, DepthEvent, TickerEvent


class Hub:
    def __init__(self) -> None:
        self.last = 0.0
        self.bid = 0.0
        self.ask = 0.0
        # ts_ms is ticker/deal-driven only: it stamps when `last` last moved.
        self.ts_ms = 0
        # depth_ts_ms advances only when a real depth frame is applied, so a
        # depth outage stays visible even while the ticker feed is healthy.
        self.depth_ts_ms = 0
        self.ws_ok = False
        self.symbol = ""

    def mark_down(self) -> None:
        self.ws_ok = False

    def apply(self, event: TickerEvent | DealEvent | DepthEvent | None) -> None:
        if event is None:
            return
        if isinstance(event, TickerEvent):
            self.last = event.last
            self.ts_ms = event.ts_ms
            self.symbol = event.symbol
            self.ws_ok = True
        elif isinstance(event, DealEvent):
            self.last = event.price
            self.ts_ms = event.ts_ms
            self.symbol = event.symbol
            self.ws_ok = True
        elif isinstance(event, DepthEvent):
            if event.bid is not None:
                self.bid = event.bid
            if event.ask is not None:
                self.ask = event.ask
            self.symbol = event.symbol
            # Deliberately does NOT set ws_ok: a DepthEvent carries no `last`
            # price, so on its own it must never make a hub with last == 0.0
            # read as a healthy, fresh price feed. DepthEvent has no ts_ms
            # field, so stamp arrival wall-clock time instead.
            self.depth_ts_ms = int(time.time() * 1000)
