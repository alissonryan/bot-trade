from __future__ import annotations

from kcex.ws import DealEvent, DepthEvent, TickerEvent


class Hub:
    def __init__(self) -> None:
        self.last = 0.0
        self.bid = 0.0
        self.ask = 0.0
        self.ts_ms = 0
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
            self.ws_ok = True
