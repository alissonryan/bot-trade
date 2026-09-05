"""KCEX spot public WebSocket (MEXC v3 wire protocol), captured and verified on 2026-09-04.

URL        wss://wbs.kcex.com/ws  (site config: ``mainSocketUrl = "wss://wbs." + domain``)
Subscribe  {"method":"SUBSCRIPTION","params":["spot@public.deals.v3.api@BTCUSDT", ...]}
Ack        {"id":0,"code":0,"msg":"spot@public.deals.v3.api@BTCUSDT"}
Keepalive  {"method":"PING"}  ->  {"id":0,"code":0,"msg":"PONG"}
Frame      {"c": <channel>, "d": {...}, "s": "BTCUSDT", "t": <ms>}

  deals       d.deals[] = {"p": price, "v": qty, "S": 1 buy | 2 sell, "t": ms}
  bookTicker  d = {"a": ask, "A": ask qty, "b": bid, "B": bid qty}
  miniTicker  d.p = last (plus r, h, l, v, q)              channel ...@BTCUSDT@UTC+0
  kline       d.k = {t, o, c, h, l, v, a, T, i}             channel ...@BTCUSDT@Min15
  depth       d = {bids: [{p, v}], asks: [{p, v}]}          channel ...limit.depth...@BTCUSDT@5

Symbols carry no underscore on the socket (BTC_USDT -> BTCUSDT). Real frames live in
tests/fixtures/kcex_ws_frames.jsonl.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

DEALS = "spot@public.deals.v3.api@{sym}"
BOOK_TICKER = "spot@public.bookTicker.v3.api@{sym}"
PING = {"method": "PING"}
PING_EVERY_S = 15.0


def ws_symbol(symbol: str) -> str:
    return symbol.replace("_", "").upper()


def channels_for(symbol: str) -> list[str]:
    sym = ws_symbol(symbol)
    return [DEALS.format(sym=sym), BOOK_TICKER.format(sym=sym)]


def subscribe_message(symbol: str) -> dict[str, Any]:
    return {"method": "SUBSCRIPTION", "params": channels_for(symbol)}


@dataclass(frozen=True)
class PriceUpdate:
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    ts_ms: int | None = None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_ack(msg: Any) -> bool:
    return isinstance(msg, dict) and "c" not in msg and "code" in msg


def parse_frame(msg: Any) -> PriceUpdate | None:
    """Map one socket frame to a price update. Acks, pongs and unknown channels give None."""
    if not isinstance(msg, dict):
        return None
    channel = str(msg.get("c") or "")
    data = msg.get("d")
    if not channel or not isinstance(data, dict):
        return None
    ts = _int(msg.get("t"))
    try:
        if ".deals." in channel:
            deals = data.get("deals") or []
            if not deals:
                return None
            last = deals[-1]
            return PriceUpdate(last=float(last["p"]), ts_ms=_int(last.get("t")) or ts)
        if ".bookTicker." in channel:
            return PriceUpdate(bid=float(data["b"]), ask=float(data["a"]), ts_ms=ts)
        if ".miniTicker." in channel:
            return PriceUpdate(last=float(data["p"]), ts_ms=ts)
    except (KeyError, TypeError, ValueError):
        return None
    return None


class SpotSocket(threading.Thread):
    """Background reader: subscribe, ping every 15 s, hand PriceUpdates to ``on_update``,
    reconnect with backoff. ``connect`` is injectable so tests never open a socket."""

    def __init__(
        self,
        url: str,
        symbol: str,
        on_update: Callable[[PriceUpdate], None],
        *,
        connect: Callable[..., Any] | None = None,
        headers: dict[str, str] | None = None,
        recv_timeout: float = 5.0,
        backoff_max: float = 30.0,
    ) -> None:
        super().__init__(name="kcex-ws", daemon=True)
        self.url = url
        self.symbol = symbol
        self.on_update = on_update
        self.headers = headers or {}
        self.recv_timeout = recv_timeout
        self.backoff_max = backoff_max
        self._connect = connect
        self._stop = threading.Event()
        self.connected = False
        self.frames = 0
        self.reconnects = 0
        self.last_error: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001 - any socket failure means reconnect
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.reconnects += 1
                log.warning("ws %s: %s; reconnect in %.0fs", self.url, self.last_error, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2.0, self.backoff_max)

    def _session(self) -> None:
        connect = self._connect or self._default_connect()
        with connect(self.url, additional_headers=self.headers, open_timeout=10) as ws:
            ws.send(json.dumps(subscribe_message(self.symbol)))
            self.connected = True
            last_ping = time.monotonic()
            while not self._stop.is_set():
                try:
                    raw = ws.recv(timeout=self.recv_timeout)
                except TimeoutError:
                    raw = None
                if raw is not None:
                    self._handle(raw)
                if time.monotonic() - last_ping >= PING_EVERY_S:
                    ws.send(json.dumps(PING))
                    last_ping = time.monotonic()
        self.connected = False

    def _handle(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        self.frames += 1
        update = parse_frame(msg)
        if update is not None:
            self.on_update(update)

    @staticmethod
    def _default_connect() -> Callable[..., Any]:
        from websockets.sync.client import connect

        return connect
