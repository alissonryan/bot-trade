from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any


DEFAULT_WS_URL = "wss://wbs.kcex.com/ws?platform=web"
PING_INTERVAL_S = 15.0
# Must be shorter than PING_INTERVAL_S so the pump loop wakes up on a
# schedule to check the ping timer even when the socket stays quiet
# (websockets.sync.client's recv(timeout=...) raises the builtin
# TimeoutError, not a websockets-specific exception, when it elapses).
RECV_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class TickerEvent:
    last: float
    ts_ms: int
    symbol: str


@dataclass(frozen=True)
class DealEvent:
    price: float
    qty: float
    side: str
    ts_ms: int
    symbol: str


@dataclass(frozen=True)
class DepthEvent:
    bid: float | None
    ask: float | None
    symbol: str


# KCEX (a MEXC white-label) accepts two naming conventions on the same socket,
# both verified live against wss://wbs.kcex.com/ws: the legacy `spot@public.X@BTC_USDT`
# form and the v3 `spot@public.X.v3.api@BTCUSDT` form, which takes the symbol with
# no underscore. We subscribe to a mix, picking the best channel for each job.
_QUOTES = ("USDT", "USDC", "USD", "BTC", "ETH")


def ws_symbol(symbol: str) -> str:
    """`BTC_USDT` -> `BTCUSDT`, the form the v3 channels expect."""
    return symbol.replace("_", "").upper()


def _unws_symbol(symbol: str) -> str:
    """Reverse of `ws_symbol`, so events from v3 channels report the same symbol
    string as the legacy ones and `Hub.symbol` does not flip between the two."""
    if "_" in symbol:
        return symbol
    for quote in _QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[: -len(quote)]}_{quote}"
    return symbol


def subscribe_message(symbol: str) -> dict[str, Any]:
    return {
        "method": "SUBSCRIPTION",
        "params": [
            f"spot@public.miniTicker@{symbol}@UTC+0",
            f"spot@public.aggre.deals@{symbol}",
            # Top of book comes from bookTicker rather than the depth ladder:
            # it is a ~90-byte frame carrying the exact best bid/ask, where
            # `limit.precision.depth@...@0.01` sends the whole ladder and only
            # at 0.01 rounding. Verified live: both ack and stream.
            f"spot@public.bookTicker.v3.api@{ws_symbol(symbol)}",
        ],
        "id": 3,
    }


def ping_message() -> dict[str, str]:
    return {"method": "PING"}


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_frame(msg: dict[str, Any] | None) -> TickerEvent | DealEvent | DepthEvent | None:
    if not isinstance(msg, dict):
        return None
    if msg.get("msg") == "PONG":
        return None
    if "code" in msg and "c" not in msg:
        return None
    channel = str(msg.get("c") or "")
    data = msg.get("d") if isinstance(msg.get("d"), dict) else {}
    symbol = str(msg.get("s") or data.get("s") or "")
    # Both the legacy `spot@public.miniTicker@BTC_USDT@UTC+0` and the v3
    # `spot@public.miniTicker.v3.api@BTCUSDT@UTC+0` form carry last as `d.p`.
    if channel.startswith("spot@public.miniTicker@") or channel.startswith("spot@public.miniTicker.v3.api@"):
        last = _f(data.get("p"))
        if last is None:
            return None
        ts = int(msg.get("t") or data.get("t") or 0)
        return TickerEvent(last=last, ts_ms=ts, symbol=_unws_symbol(symbol))
    if channel.startswith("spot@public.aggre.deals@"):
        deals = data.get("deals") or []
        if not deals:
            return None
        row = deals[0]
        price = _f(row.get("p"))
        qty = _f(row.get("q")) or 0.0
        if price is None:
            return None
        side = "buy" if int(row.get("T") or 0) == 1 else "sell"
        return DealEvent(
            price=price,
            qty=qty,
            side=side,
            ts_ms=int(row.get("t") or 0),
            symbol=symbol,
        )
    if channel.startswith("spot@public.deals.v3.api@"):
        # v3 deals: qty is `v` (not `q`) and side is `S` (1 buy / 2 sell).
        deals = data.get("deals") or []
        if not deals:
            return None
        row = deals[0]
        price = _f(row.get("p"))
        if price is None:
            return None
        return DealEvent(
            price=price,
            qty=_f(row.get("v")) or 0.0,
            side="buy" if int(row.get("S") or 0) == 1 else "sell",
            ts_ms=int(row.get("t") or msg.get("t") or 0),
            symbol=_unws_symbol(symbol),
        )
    if channel.startswith("spot@public.bookTicker.v3.api@"):
        bid = _f(data.get("b"))
        ask = _f(data.get("a"))
        # A key that is present but unparseable is corruption, not absence:
        # reject the whole frame rather than half-applying it, since bid/ask
        # feed the collar's spread check.
        if ("b" in data and bid is None) or ("a" in data and ask is None):
            return None
        if bid is None and ask is None:
            return None
        return DepthEvent(bid=bid, ask=ask, symbol=_unws_symbol(symbol))
    if "limit.precision.depth" in channel:
        bid = None
        ask = None
        bids = data.get("bids") or data.get("bestBids") or []
        asks = data.get("asks") or data.get("bestAsks") or []
        if bids and isinstance(bids[0], dict):
            bid = _f(bids[0].get("p"))
        if asks and isinstance(asks[0], dict):
            ask = _f(asks[0].get("p"))
        if bid is None and ask is None:
            return None
        return DepthEvent(bid=bid, ask=ask, symbol=symbol)
    return None


def parse_text(text: str) -> TickerEvent | DealEvent | DepthEvent | None:
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(msg, dict):
        return None
    return parse_frame(msg)


class PublicSpotWs:
    def __init__(self, url: str, symbol: str, connect):
        self.url = url
        self.symbol = symbol
        self._connect = connect

    def pump(self, *, on_event, on_error, max_messages: int | None = None) -> None:
        sock = self._connect(self.url)
        # Every exit path closes the socket: without this, each reconnect cycle
        # in Eye.start_ws_thread() leaks a connection object (and, for a real
        # websockets.sync connection, a background thread) in a process meant
        # to run for days.
        try:
            sock.send(json.dumps(subscribe_message(self.symbol)))
            last_ping = time.monotonic()
            n = 0
            while max_messages is None or n < max_messages:
                now = time.monotonic()
                if now - last_ping >= PING_INTERVAL_S:
                    sock.send(json.dumps(ping_message()))
                    last_ping = now
                try:
                    raw = sock.recv(timeout=RECV_TIMEOUT_S)
                except TimeoutError:
                    # No message within RECV_TIMEOUT_S: not a connection failure,
                    # just a quiet stretch. Loop back so the ping-interval check
                    # above keeps running on schedule instead of being starved
                    # by a recv() that would otherwise block indefinitely.
                    continue
                except Exception as exc:
                    on_error(exc)
                    return
                n += 1
                try:
                    event = parse_text(raw) if isinstance(raw, str) else None
                except Exception:
                    continue
                if event is not None:
                    on_event(event)
        finally:
            # Closing must never mask the original exit reason.
            try:
                sock.close()
            except Exception:
                pass


def default_connect(url: str):
    from websockets.sync.client import connect
    return connect(url, open_timeout=10)
