"""In-memory futures market: WS/REST events in, FutSnapshot features out. Single-threaded use only."""

from __future__ import annotations

from collections import deque

from bot.atr import atr
from bot.types import Bar
from fut.settings import FutSettings
from fut.types import FutSnapshot
from kcex.fapi import ContractSpec
from kcex.fws import FutDeal, FutDepth, FutFair, FutTicker, OrderBook

RETURN_WINDOWS = (("2s", 2), ("10s", 10), ("60s", 60), ("300s", 300))
FLOW_WINDOWS = (("30s", 30), ("120s", 120))
DEPTH_BANDS_BPS = (5, 10, 25)
MID_HISTORY_MS = 600_000


class MarketState:
    def __init__(self, settings: FutSettings, spec: ContractSpec):
        self.settings = settings
        self.spec = spec
        self.book = OrderBook()
        self.ticker: FutTicker | None = None
        self.fair = 0.0
        self.funding_rate = 0.0
        self.next_funding_ms: int | None = None
        self.deals: deque[FutDeal] = deque(maxlen=20_000)
        self.mids: deque[tuple[int, float]] = deque(maxlen=20_000)
        self.bars_1m: list[Bar] = []
        self.last_event_ms = 0
        self.ws_last_ms = 0

    def apply(self, event, *, now_ms: int, source: str = "ws") -> bool:
        ok = True
        if isinstance(event, FutTicker):
            self.ticker = event
            if event.fair > 0:
                self.fair = event.fair
            self.funding_rate = event.funding_rate
        elif isinstance(event, FutFair):
            self.fair = event.price
        elif isinstance(event, FutDeal):
            self.deals.append(event)
        elif isinstance(event, FutDepth):
            ok = self.book.apply(event)
        self.last_event_ms = now_ms
        if source == "ws":
            self.ws_last_ms = now_ms
        self._record_mid(now_ms)
        return ok

    def load_book(self, version: int, bids, asks) -> None:
        self.book.load_snapshot(version, bids, asks)

    def set_funding(self, rate: float, next_ms: int | None) -> None:
        self.funding_rate = rate
        self.next_funding_ms = next_ms

    def set_bars(self, rows) -> None:
        self.bars_1m = [Bar(t=int(r[0]), o=r[1], h=r[2], l=r[3], c=r[4], v=r[5]) for r in rows]

    def bid(self) -> float:
        best = self.book.best_bid() if self.book.synced else None
        return best if best else (self.ticker.bid if self.ticker else 0.0)

    def ask(self) -> float:
        best = self.book.best_ask() if self.book.synced else None
        return best if best else (self.ticker.ask if self.ticker else 0.0)

    def last(self) -> float:
        if self.ticker is not None:
            return self.ticker.last
        return self.deals[-1].price if self.deals else 0.0

    def mid(self) -> float:
        bid, ask = self.bid(), self.ask()
        return (bid + ask) / 2 if bid > 0 and ask > 0 else self.last()

    def _record_mid(self, now_ms: int) -> None:
        mid = self.mid()
        if mid > 0:
            self.mids.append((now_ms, mid))
        while self.mids and self.mids[0][0] < now_ms - MID_HISTORY_MS:
            self.mids.popleft()

    def _mid_at(self, ts_ms: int) -> float | None:
        for t, mid in reversed(self.mids):
            if t <= ts_ms:
                return mid
        return None

    def snapshot(self, now_ms: int) -> FutSnapshot:
        bid, ask, last = self.bid(), self.ask(), self.last()
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
        spread_bps = (ask - bid) / mid * 10_000 if bid > 0 and ask > 0 and mid > 0 else 0.0

        bids = self.book.levels("bid") if self.book.synced else []
        asks = self.book.levels("ask") if self.book.synced else []
        depth: dict[str, dict[str, float]] = {}
        for band in DEPTH_BANDS_BPS:
            lo, hi = mid * (1 - band / 10_000), mid * (1 + band / 10_000)
            depth[str(band)] = {"bid": sum(v for p, v in bids if p >= lo),
                                "ask": sum(v for p, v in asks if p <= hi)}
        wide = depth[str(DEPTH_BANDS_BPS[-1])]
        total = wide["bid"] + wide["ask"]
        imbalance = (wide["bid"] - wide["ask"]) / total if total else 0.0

        returns: dict[str, float] = {}
        for label, seconds in RETURN_WINDOWS:
            past = self._mid_at(now_ms - seconds * 1000)
            returns[label] = (mid - past) / past * 10_000 if past and mid > 0 else 0.0

        flow: dict[str, dict] = {}
        for label, seconds in FLOW_WINDOWS:
            recent = [d for d in self.deals if d.ts_ms > now_ms - seconds * 1000]
            buy = sum(d.vol for d in recent if d.side == "buy")
            sell = sum(d.vol for d in recent if d.side == "sell")
            volume = buy + sell
            vwap = sum(d.price * d.vol for d in recent) / volume if volume else None
            flow[label] = {"buy": buy, "sell": sell, "cvd": buy - sell, "vwap": vwap}

        stale = mid <= 0 or now_ms - self.ws_last_ms > self.settings.stale_market_s * 1000
        fair = self.fair or (self.ticker.fair if self.ticker else 0.0)
        return FutSnapshot(
            ts_ms=now_ms, last=last, bid=bid, ask=ask, fair=fair,
            index=self.ticker.index if self.ticker else 0.0,
            funding_rate=self.funding_rate, next_funding_ms=self.next_funding_ms,
            spread_bps=spread_bps, imbalance=imbalance, depth_bps=depth, returns_bps=returns, flow=flow,
            atr_1m=atr(self.bars_1m, self.settings.atr_period), stale=stale,
        )
