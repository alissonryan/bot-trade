# KCEX Futures Paper (Jev trigger + LLM decision) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paper-trade the KCEX BTC_USDT perpetual on a seconds horizon: Jev evaluates every 2 s and wakes the LLM, the LLM decides LONG/SHORT/CLOSE/HOLD, code owns size/stop/limits, and a report checks a fixed edge criterion against three shadow baselines.

**Architecture:** New package `fut/` beside `bot/`, reusing only low-level pieces (`bot.store.Store` identity/kv/budget, `bot.brain` cost helpers and `Budget`, `bot.cli.InstanceLock`). Public futures transport lives in `kcex/fws.py` (WS) and `kcex/fapi.py` (REST). One process: a WS thread feeds a queue; the main loop drains it, marks the paper ledger every step, runs Jev every 2 s, and hands LLM calls to a single worker thread through `fut/dispatch.py`.

**Tech Stack:** Python 3.14 (`.venv`), requests, websockets (sync client), sqlite3, `typesafe-sdk>=0.6.0`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-17-kcex-futures-paper-jev-llm-design.md`

## Global Constraints

- Paper only. No `/fapi/v1/private/...` route, no `KCEX_TOKEN`, no order placement anywhere in `fut/` or `kcex/fapi.py`/`kcex/fws.py`. `FuturesPublic` builds its client with `token=""`.
- Symbol `BTC_USDT` only. Leverage integer 1..3 (`MAX_LEVERAGE = 3` hardcoded), default 1, isolated margin.
- Market orders only: open LONG/close SHORT at `ask1 × (1 + slippage)`, open SHORT/close LONG at `bid1 × (1 − slippage)`, fallback to `lastPrice` only when that side is missing. Taker fee from `contract/detail` (`takerFeeRate`), charged each side.
- Stop mandatory on every entry; max hold `FUT_MAX_HOLD_SECONDS=300`; LLM `CLOSE`; whichever first. Mark order per tick: liquidation (by `fairPrice`), stop, time limit, funding.
- Money: starting 450 USDT, margin 20 USDT/trade, max 5% of balance, 1 position, day loss 20 USDT including fees, funding and Jev+LLM cost.
- Dispatch: immediate; one in flight; 10 s cooldown only after HOLD for same trigger kind+side; `exit_signal` never cooled; LLM timeout 8 s; `stale_timeout`/`stale_price` (> 5 bps) discard **LONG/SHORT only**, never CLOSE.
- Edge criterion (never changed after a run starts): ≥ 200 closed trades, ≥ 14 days, no `mock` Jev, net > 0 after all costs, beats `flat`, `jev_only`, `random`, bootstrap 95% CI lower bound of per-trade net > 0, no day below the day-loss limit.
- Database `data/futures-paper.db` stamped mode `futures-paper`; lock `data/futures.lock`; log `data/futures.log`. Lock acquired before file logging.
- Exit codes: 0 ok, 3 already running, 8 unmonitored position (> 60 s without any price), 9 store identity mismatch.
- TDD for every task. Tests never touch the network, real Jev or real LLM. New test basenames must be unique across the repo (pytest rootdir import mode): futures tests are named `test_fut_*.py`.
- Run tests with `./scripts/test <paths>` (never bare `python -m pytest`). Full suite: `./scripts/test`.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

## File Structure

| Path | Responsibility |
|---|---|
| `kcex/fws.py` | Futures WS frames → typed events; `OrderBook` (snapshot + contiguous deltas); `PublicFuturesWs.pump` |
| `kcex/fapi.py` | `ContractSpec`; `FuturesPublic` GETs: detail, ticker, depth, deals, 1m klines, funding |
| `fut/__init__.py`, `fut/__main__.py` | package, `python -m fut` |
| `fut/settings.py` | `FutSettings` (env), `MAX_LEVERAGE` |
| `fut/types.py` | `FutPosition`, `FutSnapshot`, `JevVerdict`, `FutIntent`, `FutGate` |
| `fut/pricing.py` | fill price, liquidation price, tick rounding |
| `fut/collar.py` | `check()` risk gate and sizing |
| `fut/store.py` | `FutStore(Store)`: positions/fills per book, balances, decisions, model cost |
| `fut/ledger.py` | `PaperLedger`: open/close/mark (liquidation, stop, time limit, funding) |
| `fut/market.py` | `MarketState`: events → `FutSnapshot` features |
| `fut/questions.py` | Jev questions, `jev_state`, `should_wake`, `jev_side` |
| `fut/jev.py` | `JevClient` (typesafe-sdk), `MockJev`, `make_jev` |
| `fut/llm.py` | `decide()` with durable budget, `parse_action`, `LlmDecision` |
| `fut/dispatch.py` | `Trigger`, `Resolution`, `Dispatcher` |
| `fut/shadow.py` | `ShadowBooks` (`jev_only`, `random`; `flat` is implicit 0) |
| `fut/report.py` | totals, bootstrap, `summarize`, `evaluate`, `render` |
| `fut/loop.py` | `FutLoop.step`, `Unmonitored`, `seed_budget`, `start_ws_thread` |
| `fut/cli.py` | `main()`: `run [--max-seconds N]`, `report` |
| `docs/kcex-futures-api.md` | captured public endpoints and frames |
| `tests/fixtures/kcex_fut_ws_frames.jsonl` | real captured frames |
| `tests/kcex/test_fws.py`, `tests/kcex/test_fapi.py`, `tests/fut/test_fut_*.py` | tests |

---

### Task 1: Futures WS parser, order book and captured fixtures

**Files:**
- Create: `tests/fixtures/kcex_fut_ws_frames.jsonl`
- Create: `docs/kcex-futures-api.md`
- Create: `kcex/fws.py`
- Test: `tests/kcex/test_fws.py`

**Interfaces:**
- Produces: `DEFAULT_FUT_WS_URL: str`; dataclasses `FutTicker(ts_ms, last, bid, ask, fair, index, funding_rate)`, `FutDeal(ts_ms, price, vol, side)`, `FutDepth(ts_ms, version, bids, asks)`, `FutFair(ts_ms, price)`; `_num(value) -> float | None`; `parse_deal(data) -> FutDeal | None`; `parse_levels(raw) -> tuple[tuple[float, int], ...]`; `parse_frame(msg) -> list[FutEvent]`; `parse_text(text) -> list[FutEvent]`; `subscribe_messages(symbol) -> list[dict]`; `ping_message() -> dict`; `OrderBook` with `synced`, `version`, `load_snapshot(version, bids, asks)`, `apply(delta) -> bool`, `best_bid()`, `best_ask()`, `levels("bid"|"ask")`; `PublicFuturesWs(url, symbol, connect).pump(on_event, on_error, max_messages=None)`; `default_connect(url)`.

- [ ] **Step 1: Save the captured frames as a fixture**

Create `tests/fixtures/kcex_fut_ws_frames.jsonl` with exactly these lines (captured from `wss://www.kcex.com/fapi/edge` on 2026-09-16, no auth):

```
{"channel":"rs.sub.depth","data":"success","ts":1789614158917}
{"channel":"pong","data":1789614158917,"ts":1789614158917}
{"symbol":"BTC_USDT","data":{"symbol":"BTC_USDT","lastPrice":76353.8,"riseFallRate":0.0079,"fairPrice":76354.2,"indexPrice":76387.6,"volume24":966351187,"amount24":7332559529.68844,"maxBidPrice":87845.7,"minAskPrice":64929.4,"lower24Price":75025.1,"high24Price":76744.1,"timestamp":1789614158008,"bid1":76353.8,"ask1":76353.9,"holdVol":4310237,"riseFallValue":600.2,"fundingRate":0.000071,"zone":"UTC+8","riseFallRates":[0.0079,-0.0251,0.1916,0.2124,0.0799,-0.3453],"riseFallRatesOfTimezone":[0.0065,0.0023,0.0079]},"channel":"push.ticker","ts":1789614158008}
{"symbol":"BTC_USDT","data":{"symbol":"BTC_USDT","price":76355.6},"channel":"push.fair.price","ts":1789614158366}
{"symbol":"BTC_USDT","data":{"p":76356.9,"v":2200,"T":2,"O":3,"M":1,"t":1789614159388},"channel":"push.deal","ts":1789614159388}
{"symbol":"BTC_USDT","data":{"p":76356.8,"v":1499,"T":1,"O":3,"M":1,"t":1789614161493},"channel":"push.deal","ts":1789614161493}
{"symbol":"BTC_USDT","data":{"asks":[[76356.9,1286398,4]],"bids":[],"version":14452998433},"channel":"push.depth","ts":1789614159371}
{"symbol":"BTC_USDT","data":{"asks":[[76356.9,1709998,5]],"bids":[],"version":14452998434},"channel":"push.depth","ts":1789614159371}
{"symbol":"BTC_USDT","data":{"asks":[],"bids":[[76356.8,1206199,4]],"version":14452998435},"channel":"push.depth","ts":1789614159372}
{"symbol":"BTC_USDT","data":{"asks":[],"bids":[[76352.9,0,0]],"version":14452998671},"channel":"push.depth","ts":1789614162701}
```

- [ ] **Step 2: Document the captured API**

Create `docs/kcex-futures-api.md`:

````markdown
# KCEX perpetual futures: public API notes

Captured 2026-09-16 without authentication. Used only by the paper bot in `fut/`.
Private routes (`/fapi/v1/private/...`) answer 401 without a session and are **not captured**;
nothing in this repo calls them.

## REST (GET, base `https://www.kcex.com`)

| Path | Params | Used fields |
|---|---|---|
| `/fapi/v1/contract/detail` | `symbol` | `contractSize` (0.0001), `minVol` (1), `maxVol`, `priceUnit` (0.1), `takerFeeRate` (0.0001), `makerFeeRate` (0), `maintenanceMarginRate` (0.005), `maxLeverage` (125), `state` (0 = trading) |
| `/fapi/v1/contract/ticker` | `symbol` | `lastPrice`, `bid1`, `ask1`, `fairPrice`, `indexPrice`, `fundingRate`, `timestamp` |
| `/fapi/v1/contract/depth/{symbol}` | `limit` | `asks`/`bids` as `[price, vol_contracts, order_count]`, `version`, `timestamp` |
| `/fapi/v1/contract/deals/{symbol}` | `limit` | list of `{p, v, T, O, M, t}`, newest first |
| `/fapi/v1/contract/kline/{symbol}` | `interval=Min1`, `start`, `end` (seconds) | column arrays `time, open, high, low, close, vol` |
| `/fapi/v1/contract/funding_rate/{symbol}` | | `fundingRate`, `collectCycle` (8 h), `nextSettleTime` (ms) |

Envelope: `{"success": true, "code": 0, "data": ...}`.

## WebSocket `wss://www.kcex.com/fapi/edge`

Subscribe (one message each): `{"method":"sub.ticker","param":{"symbol":"BTC_USDT"}}`, same for
`sub.deal`, `sub.depth`, `sub.fair.price`. Ack: `{"channel":"rs.sub.depth","data":"success"}`.
Ping `{"method":"ping"}` → `{"channel":"pong"}`.

| Channel | `data` | Rate observed |
|---|---|---|
| `push.ticker` | same fields as REST ticker | ~0.5/s |
| `push.fair.price` | `{"symbol","price"}` | ~0.6/s |
| `push.deal` | `{p, v, T, O, M, t}` (one object) | per trade |
| `push.depth` | `{asks, bids, version}` **incremental** | ~43/s |

- Depth deltas carry a contiguous `version` (0 gaps in 518 frames). Volume `0` removes the level.
  Keep a book from a REST depth snapshot (with its `version`) plus deltas `version = prev + 1`;
  on a gap, resync from REST.
- `push.funding.rate` was subscribed but sent nothing in 12 s; funding is read from REST.
- **Inferred, not documented:** deal `T=2` printed at the ask (aggressive buy) and `T=1` at the
  bid (aggressive sell) in every captured sample, WS and REST.
- Volumes are in contracts (`× contractSize` BTC).

Samples: `tests/fixtures/kcex_fut_ws_frames.jsonl`.
````

- [ ] **Step 3: Write the failing tests**

Create `tests/kcex/test_fws.py`:

```python
import json
from pathlib import Path

from kcex.fws import (
    FutDeal,
    FutDepth,
    FutFair,
    FutTicker,
    OrderBook,
    PublicFuturesWs,
    parse_frame,
    parse_text,
    ping_message,
    subscribe_messages,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kcex_fut_ws_frames.jsonl"


def frames():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def first(channel):
    return next(f for f in frames() if f["channel"] == channel)


def test_ack_and_pong_frames_yield_no_events():
    acks = [f for f in frames() if f["channel"] in ("rs.sub.depth", "pong")]
    assert len(acks) == 2
    assert all(parse_frame(f) == [] for f in acks)


def test_ticker_frame():
    assert parse_frame(first("push.ticker")) == [FutTicker(
        ts_ms=1789614158008, last=76353.8, bid=76353.8, ask=76353.9,
        fair=76354.2, index=76387.6, funding_rate=0.000071)]


def test_fair_price_frame():
    assert parse_frame(first("push.fair.price")) == [FutFair(1789614158366, 76355.6)]


def test_deal_side_follows_captured_prints():
    deals = [e for f in frames() if f["channel"] == "push.deal" for e in parse_frame(f)]
    assert deals == [
        FutDeal(1789614159388, 76356.9, 2200, "buy"),
        FutDeal(1789614161493, 76356.8, 1499, "sell"),
    ]


def test_deal_with_unknown_side_is_dropped():
    assert parse_frame({"channel": "push.deal", "data": {"p": 1, "v": 1, "T": 9, "t": 1}}) == []


def test_deal_list_payload_is_supported():
    msg = {"channel": "push.deal", "data": [
        {"p": 10, "v": 1, "T": 2, "t": 1}, {"p": 11, "v": 2, "T": 1, "t": 2}]}
    assert [d.side for d in parse_frame(msg)] == ["buy", "sell"]


def test_depth_frames_parse_versions_and_zero_volume():
    depths = [e for f in frames() if f["channel"] == "push.depth" for e in parse_frame(f)]
    assert [d.version for d in depths] == [14452998433, 14452998434, 14452998435, 14452998671]
    assert depths[0].asks == ((76356.9, 1286398),)
    assert depths[-1].bids == ((76352.9, 0),)


def test_parse_text_ignores_garbage():
    assert parse_text("not json") == []
    assert parse_text("[1, 2]") == []


def test_subscribe_and_ping_messages():
    msgs = subscribe_messages("BTC_USDT")
    assert [m["method"] for m in msgs] == ["sub.ticker", "sub.deal", "sub.depth", "sub.fair.price"]
    assert all(m["param"] == {"symbol": "BTC_USDT"} for m in msgs)
    assert ping_message() == {"method": "ping"}


def _delta(version, bids=(), asks=()):
    return FutDepth(0, version, tuple(bids), tuple(asks))


def test_book_applies_contiguous_deltas_and_removes_zero_volume():
    book = OrderBook()
    book.load_snapshot(10, [(100.0, 5), (99.0, 3)], [(101.0, 4)])
    assert book.apply(_delta(11, bids=[(100.0, 0)], asks=[(100.5, 2)]))
    assert book.version == 11
    assert book.best_bid() == 99.0
    assert book.best_ask() == 100.5
    assert book.levels("ask") == [(100.5, 2), (101.0, 4)]
    assert book.levels("bid") == [(99.0, 3)]


def test_book_skips_versions_already_in_the_snapshot():
    book = OrderBook()
    book.load_snapshot(10, [(100.0, 5)], [(101.0, 4)])
    assert book.apply(_delta(9, bids=[(100.0, 0)]))
    assert book.best_bid() == 100.0


def test_book_gap_unsyncs_and_clears():
    book = OrderBook()
    book.load_snapshot(10, [(100.0, 5)], [(101.0, 4)])
    assert book.apply(_delta(12)) is False
    assert not book.synced
    assert book.best_bid() is None and book.best_ask() is None


def test_unsynced_book_refuses_deltas():
    assert OrderBook().apply(_delta(1)) is False


class FakeSock:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout=None):
        if not self.incoming:
            raise ConnectionError("closed")
        return self.incoming.pop(0)

    def close(self):
        self.closed = True


def test_pump_subscribes_every_channel_and_emits_parsed_events():
    sock = FakeSock(FIXTURE.read_text().splitlines())
    events, errors = [], []
    ws = PublicFuturesWs("wss://example", "BTC_USDT", lambda url: sock)
    ws.pump(on_event=events.append, on_error=errors.append)
    assert [json.loads(s)["method"] for s in sock.sent[:4]] == [
        "sub.ticker", "sub.deal", "sub.depth", "sub.fair.price"]
    assert len(events) == 8  # 1 ticker, 1 fair, 2 deals, 4 depth
    assert len(errors) == 1
    assert sock.closed
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `./scripts/test tests/kcex/test_fws.py`
Expected: collection error `ModuleNotFoundError: No module named 'kcex.fws'`

- [ ] **Step 5: Implement `kcex/fws.py`**

```python
"""Public KCEX perpetual-futures WebSocket (``wss://www.kcex.com/fapi/edge``).

Frame shapes were captured without authentication on 2026-09-16. Samples live in
``tests/fixtures/kcex_fut_ws_frames.jsonl`` and the notes in ``docs/kcex-futures-api.md``.
Nothing beyond those samples is assumed.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any

DEFAULT_FUT_WS_URL = "wss://www.kcex.com/fapi/edge"
CHANNELS = ("sub.ticker", "sub.deal", "sub.depth", "sub.fair.price")
PING_INTERVAL_S = 15.0
RECV_TIMEOUT_S = 1.0
# Inferred, not documented by the venue: every captured T=2 printed at the ask
# (aggressive buy) and every T=1 at the bid (aggressive sell).
DEAL_SIDE = {2: "buy", 1: "sell"}


@dataclass(frozen=True)
class FutTicker:
    ts_ms: int
    last: float
    bid: float
    ask: float
    fair: float
    index: float
    funding_rate: float


@dataclass(frozen=True)
class FutDeal:
    ts_ms: int
    price: float
    vol: int
    side: str


@dataclass(frozen=True)
class FutDepth:
    ts_ms: int
    version: int
    bids: tuple[tuple[float, int], ...]
    asks: tuple[tuple[float, int], ...]


@dataclass(frozen=True)
class FutFair:
    ts_ms: int
    price: float


FutEvent = FutTicker | FutDeal | FutDepth | FutFair


def subscribe_messages(symbol: str) -> list[dict[str, Any]]:
    return [{"method": method, "param": {"symbol": symbol}} for method in CHANNELS]


def ping_message() -> dict[str, str]:
    return {"method": "ping"}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_deal(data: Any) -> FutDeal | None:
    if not isinstance(data, dict):
        return None
    price, vol, ts = _num(data.get("p")), _num(data.get("v")), _num(data.get("t"))
    side = DEAL_SIDE.get(data.get("T"))
    if price is None or price <= 0 or vol is None or vol <= 0 or ts is None or side is None:
        return None
    return FutDeal(int(ts), price, int(vol), side)


def parse_levels(raw: Any) -> tuple[tuple[float, int], ...]:
    out: list[tuple[float, int]] = []
    for level in raw if isinstance(raw, list) else []:
        if not isinstance(level, list) or len(level) < 2:
            continue
        price, vol = _num(level[0]), _num(level[1])
        if price is None or price <= 0 or vol is None or vol < 0:
            continue
        out.append((price, int(vol)))
    return tuple(out)


def parse_frame(msg: Any) -> list[FutEvent]:
    if not isinstance(msg, dict):
        return []
    channel, data = msg.get("channel"), msg.get("data")
    ts = int(_num(msg.get("ts")) or 0)
    if channel == "push.ticker" and isinstance(data, dict):
        last = _num(data.get("lastPrice"))
        if last is None or last <= 0:
            return []
        return [FutTicker(
            ts_ms=int(_num(data.get("timestamp")) or ts),
            last=last,
            bid=_num(data.get("bid1")) or 0.0,
            ask=_num(data.get("ask1")) or 0.0,
            fair=_num(data.get("fairPrice")) or 0.0,
            index=_num(data.get("indexPrice")) or 0.0,
            funding_rate=_num(data.get("fundingRate")) or 0.0,
        )]
    if channel == "push.deal":
        items = data if isinstance(data, list) else [data]
        return [deal for deal in (parse_deal(item) for item in items) if deal is not None]
    if channel == "push.depth" and isinstance(data, dict):
        version = _num(data.get("version"))
        if version is None:
            return []
        return [FutDepth(ts, int(version), parse_levels(data.get("bids")), parse_levels(data.get("asks")))]
    if channel == "push.fair.price" and isinstance(data, dict):
        price = _num(data.get("price"))
        return [FutFair(ts, price)] if price is not None and price > 0 else []
    return []


def parse_text(text: str) -> list[FutEvent]:
    try:
        return parse_frame(json.loads(text))
    except json.JSONDecodeError:
        return []


class OrderBook:
    """REST snapshot plus contiguous WS deltas. Any version gap unsyncs the book."""

    def __init__(self) -> None:
        self.version: int | None = None
        self._bids: dict[float, int] = {}
        self._asks: dict[float, int] = {}

    @property
    def synced(self) -> bool:
        return self.version is not None

    def load_snapshot(self, version: int, bids, asks) -> None:
        self.version = int(version)
        self._bids = {price: vol for price, vol in bids if vol > 0}
        self._asks = {price: vol for price, vol in asks if vol > 0}

    def apply(self, delta: FutDepth) -> bool:
        if self.version is None:
            return False
        if delta.version <= self.version:
            return True
        if delta.version != self.version + 1:
            self.version = None
            self._bids.clear()
            self._asks.clear()
            return False
        for side, levels in ((self._bids, delta.bids), (self._asks, delta.asks)):
            for price, vol in levels:
                if vol <= 0:
                    side.pop(price, None)
                else:
                    side[price] = vol
        self.version = delta.version
        return True

    def best_bid(self) -> float | None:
        return max(self._bids) if self._bids else None

    def best_ask(self) -> float | None:
        return min(self._asks) if self._asks else None

    def levels(self, side: str) -> list[tuple[float, int]]:
        if side == "bid":
            return sorted(self._bids.items(), key=lambda level: -level[0])
        return sorted(self._asks.items(), key=lambda level: level[0])


class PublicFuturesWs:
    def __init__(self, url: str, symbol: str, connect):
        self.url = url
        self.symbol = symbol
        self._connect = connect

    def pump(self, *, on_event, on_error, max_messages: int | None = None) -> None:
        sock = self._connect(self.url)
        try:
            for message in subscribe_messages(self.symbol):
                sock.send(json.dumps(message))
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
                    continue
                except Exception as exc:
                    on_error(exc)
                    return
                n += 1
                if not isinstance(raw, str):
                    continue
                for event in parse_text(raw):
                    on_event(event)
        finally:
            try:
                sock.close()
            except Exception:
                pass


def default_connect(url: str):
    from websockets.sync.client import connect

    return connect(url, open_timeout=10)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./scripts/test tests/kcex/test_fws.py`
Expected: 14 passed

- [ ] **Step 7: Commit**

```bash
git add kcex/fws.py tests/kcex/test_fws.py tests/fixtures/kcex_fut_ws_frames.jsonl docs/kcex-futures-api.md
git commit -m "feat(kcex): public futures WS parser and incremental order book" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Futures public REST client

**Files:**
- Create: `kcex/fapi.py`
- Test: `tests/kcex/test_fapi.py`

**Interfaces:**
- Consumes: `kcex.client.KcexClient`, `KcexError`; from `kcex.fws`: `FutDeal`, `FutTicker`, `_num`, `parse_deal`, `parse_levels`.
- Produces: `ContractSpec(symbol, contract_size, min_vol, max_vol, price_unit, taker_fee, maker_fee, mmr, max_leverage, state)` with `from_detail(data)`; `FuturesPublic(client=None)` with `contract_detail(symbol) -> ContractSpec`, `ticker(symbol) -> FutTicker`, `depth(symbol, limit=50) -> tuple[int, levels, levels]`, `deals(symbol, limit=100) -> list[FutDeal]` (oldest first), `klines_1m(symbol, start_s, end_s) -> list[tuple[int, float, float, float, float, float]]` as `(t, o, h, l, c, v)`, `funding(symbol) -> tuple[float, int | None]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/kcex/test_fapi.py`:

```python
import pytest

from kcex.client import KcexClient, KcexError
from kcex.fapi import ContractSpec, FuturesPublic
from kcex.fws import FutDeal, FutTicker

DETAIL = {
    "symbol": "BTC_USDT", "contractSize": 0.0001, "minVol": 1, "maxVol": 714000,
    "priceUnit": 0.1, "takerFeeRate": 0.0001, "makerFeeRate": 0, "maintenanceMarginRate": 0.005,
    "maxLeverage": 125, "state": 0,
}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, headers))
        return FakeResponse(self.routes[url.split("/fapi/v1/")[1]])


def make(routes):
    session = FakeSession(routes)
    client = KcexClient(token="", user_device="", session=session, sleep=lambda s: None)
    return FuturesPublic(client), session


def ok(data):
    return {"success": True, "code": 0, "data": data}


def test_contract_detail_parses_spec_and_sends_no_auth():
    api, session = make({"contract/detail": ok(DETAIL)})
    assert api.contract_detail("BTC_USDT") == ContractSpec(
        symbol="BTC_USDT", contract_size=0.0001, min_vol=1, max_vol=714000, price_unit=0.1,
        taker_fee=0.0001, maker_fee=0.0, mmr=0.005, max_leverage=125, state=0)
    method, _url, params, headers = session.calls[0]
    assert method == "GET"
    assert params == {"symbol": "BTC_USDT"}
    assert "authorization" not in headers


def test_contract_detail_missing_field_raises():
    data = dict(DETAIL)
    del data["takerFeeRate"]
    api, _ = make({"contract/detail": ok(data)})
    with pytest.raises(KcexError):
        api.contract_detail("BTC_USDT")


def test_contract_detail_rejects_nonsense_values():
    api, _ = make({"contract/detail": ok(dict(DETAIL, contractSize=0))})
    with pytest.raises(KcexError):
        api.contract_detail("BTC_USDT")


def test_ticker():
    api, _ = make({"contract/ticker": ok({
        "lastPrice": 76413, "bid1": 76412.9, "ask1": 76413, "fairPrice": 76413.9,
        "indexPrice": 76446.3, "fundingRate": 0.000077, "timestamp": 1789613014008})})
    assert api.ticker("BTC_USDT") == FutTicker(1789613014008, 76413.0, 76412.9, 76413.0, 76413.9, 76446.3, 0.000077)


def test_depth_returns_version_and_levels():
    api, session = make({"contract/depth/BTC_USDT": ok({
        "asks": [[76360.4, 785199, 3], [76360.5, 17700, 1]],
        "bids": [[76360.3, 942300, 3]], "version": 14452999560})})
    assert api.depth("BTC_USDT", limit=5) == (
        14452999560, ((76360.3, 942300),), ((76360.4, 785199), (76360.5, 17700)))
    assert session.calls[0][2] == {"limit": 5}


def test_deals_are_sorted_oldest_first():
    api, _ = make({"contract/deals/BTC_USDT": ok([
        {"p": 76360.4, "v": 100, "T": 2, "O": 3, "M": 1, "t": 1789614184159},
        {"p": 76360.3, "v": 800, "T": 1, "O": 3, "M": 1, "t": 1789614180839}])})
    assert api.deals("BTC_USDT") == [
        FutDeal(1789614180839, 76360.3, 800, "sell"), FutDeal(1789614184159, 76360.4, 100, "buy")]


def test_klines_1m():
    api, session = make({"contract/kline/BTC_USDT": ok({
        "time": [1789614060, 1789614120], "open": [76380.0, 76356.9], "high": [76390.8, 76360.4],
        "low": [76356.9, 76338.2], "close": [76356.9, 76360.3], "vol": [233153.0, 206165.0]})})
    assert api.klines_1m("BTC_USDT", 1789614000, 1789614180) == [
        (1789614060, 76380.0, 76390.8, 76356.9, 76356.9, 233153.0),
        (1789614120, 76356.9, 76360.4, 76338.2, 76360.3, 206165.0)]
    assert session.calls[0][2] == {"interval": "Min1", "start": 1789614000, "end": 1789614180}


def test_funding():
    api, _ = make({"contract/funding_rate/BTC_USDT": ok({
        "fundingRate": 0.000077, "collectCycle": 8, "nextSettleTime": 1789632000000})})
    assert api.funding("BTC_USDT") == (0.000077, 1789632000000)


def test_unsuccessful_envelope_raises():
    api, _ = make({"contract/ticker": {"success": False, "code": 0, "data": None}})
    with pytest.raises(KcexError):
        api.ticker("BTC_USDT")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./scripts/test tests/kcex/test_fapi.py`
Expected: `ModuleNotFoundError: No module named 'kcex.fapi'`

- [ ] **Step 3: Implement `kcex/fapi.py`**

```python
"""Public KCEX perpetual-futures REST (``/fapi/v1/contract/...``). GET only, never authenticated.

Paper futures must never carry a session: the client is built with ``token=""`` so an
exported ``KCEX_TOKEN`` cannot leak into these calls. See docs/kcex-futures-api.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kcex.client import KcexClient, KcexError
from kcex.fws import FutDeal, FutTicker, _num, parse_deal, parse_levels


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    contract_size: float
    min_vol: int
    max_vol: int
    price_unit: float
    taker_fee: float
    maker_fee: float
    mmr: float
    max_leverage: int
    state: int

    @classmethod
    def from_detail(cls, data: Any) -> "ContractSpec":
        data = data if isinstance(data, dict) else {}

        def req(key: str) -> float:
            value = _num(data.get(key))
            if value is None:
                raise KcexError(f"futures contract detail missing {key}", {"status": None})
            return value

        spec = cls(
            symbol=str(data.get("symbol") or ""),
            contract_size=req("contractSize"),
            min_vol=int(req("minVol")),
            max_vol=int(req("maxVol")),
            price_unit=req("priceUnit"),
            taker_fee=req("takerFeeRate"),
            maker_fee=req("makerFeeRate"),
            mmr=req("maintenanceMarginRate"),
            max_leverage=int(req("maxLeverage")),
            state=int(req("state")),
        )
        if (spec.contract_size <= 0 or spec.min_vol < 1 or spec.max_vol < spec.min_vol
                or spec.price_unit <= 0 or spec.taker_fee < 0 or not 0 < spec.mmr < 1
                or spec.max_leverage < 1):
            raise KcexError(f"futures contract detail has invalid values: {spec}", {"status": None})
        return spec


class FuturesPublic:
    def __init__(self, client: KcexClient | None = None):
        self.client = client or KcexClient(token="", user_device="")

    def _data(self, path: str, **params: Any) -> Any:
        payload = self.client.get(path, **params)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise KcexError(f"GET {path} unexpected envelope", payload if isinstance(payload, dict) else {})
        return payload.get("data")

    def contract_detail(self, symbol: str) -> ContractSpec:
        return ContractSpec.from_detail(self._data("/fapi/v1/contract/detail", symbol=symbol))

    def ticker(self, symbol: str) -> FutTicker:
        data = self._data("/fapi/v1/contract/ticker", symbol=symbol)
        data = data if isinstance(data, dict) else {}
        last = _num(data.get("lastPrice"))
        if last is None or last <= 0:
            raise KcexError("futures ticker without lastPrice", {"status": None})
        return FutTicker(
            ts_ms=int(_num(data.get("timestamp")) or 0),
            last=last,
            bid=_num(data.get("bid1")) or 0.0,
            ask=_num(data.get("ask1")) or 0.0,
            fair=_num(data.get("fairPrice")) or 0.0,
            index=_num(data.get("indexPrice")) or 0.0,
            funding_rate=_num(data.get("fundingRate")) or 0.0,
        )

    def depth(self, symbol: str, limit: int = 50):
        data = self._data(f"/fapi/v1/contract/depth/{symbol}", limit=limit)
        data = data if isinstance(data, dict) else {}
        version = _num(data.get("version"))
        if version is None:
            raise KcexError("futures depth without version", {"status": None})
        return int(version), parse_levels(data.get("bids")), parse_levels(data.get("asks"))

    def deals(self, symbol: str, limit: int = 100) -> list[FutDeal]:
        data = self._data(f"/fapi/v1/contract/deals/{symbol}", limit=limit)
        items = data if isinstance(data, list) else []
        return sorted((d for d in (parse_deal(x) for x in items) if d is not None), key=lambda d: d.ts_ms)

    def klines_1m(self, symbol: str, start_s: int, end_s: int):
        data = self._data(f"/fapi/v1/contract/kline/{symbol}", interval="Min1", start=int(start_s), end=int(end_s))
        data = data if isinstance(data, dict) else {}
        columns = [data.get(key) or [] for key in ("time", "open", "high", "low", "close", "vol")]
        rows = []
        for values in zip(*columns):
            nums = [_num(v) for v in values]
            if any(n is None for n in nums):
                continue
            rows.append((int(nums[0]), nums[1], nums[2], nums[3], nums[4], nums[5]))
        return rows

    def funding(self, symbol: str) -> tuple[float, int | None]:
        data = self._data(f"/fapi/v1/contract/funding_rate/{symbol}")
        data = data if isinstance(data, dict) else {}
        rate = _num(data.get("fundingRate"))
        if rate is None:
            raise KcexError("futures funding without fundingRate", {"status": None})
        nxt = _num(data.get("nextSettleTime"))
        return rate, int(nxt) if nxt else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./scripts/test tests/kcex/test_fapi.py`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add kcex/fapi.py tests/kcex/test_fapi.py
git commit -m "feat(kcex): public futures REST client (detail, ticker, depth, deals, klines, funding)" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---
### Task 3: Futures settings, types and pricing helpers

**Files:**
- Create: `fut/__init__.py` (empty)
- Create: `fut/settings.py`
- Create: `fut/types.py`
- Create: `fut/pricing.py`
- Modify: `tests/conftest.py` (extend `_SETTINGS_ENV_VARS`)
- Test: `tests/fut/test_fut_settings.py`, `tests/fut/test_fut_pricing.py`

**Interfaces:**
- Consumes: `bot.settings.Settings`, `kcex.fws.DEFAULT_FUT_WS_URL`.
- Produces:
  - `MAX_LEVERAGE = 3`; `FutSettings` (frozen dataclass, all fields defaulted, `from_env()`, property `uses_mock_jev`). Field names: `symbol, leverage, margin_usdt, max_balance_pct, max_day_loss_usdt, starting_usdt, slippage_bps, atr_period, atr_mult, min_stop_pct, max_stop_pct, liq_stop_ratio, max_hold_s, min_confidence, jev_every_s, jev_model, typesafe_api_key, jev_usd_per_mtok, jev_timeout_s, wake_threshold, move_cost_bps, llm_cooldown_s, llm_timeout_s, stale_price_bps, stale_market_s, unmonitored_s, llm_reasoning, ws_url, shadow_seed, llm`.
  - `FutPosition(side=None, contracts=0, entry=0.0, stop=None, liq=None, margin=0.0, leverage=1, opened_ms=0, funding_through_ms=0)` frozen, `is_open()`.
  - `FutSnapshot(ts_ms, last, bid, ask, fair, index, funding_rate, next_funding_ms, spread_bps, imbalance, depth_bps, returns_bps, flow, atr_1m, stale)` frozen, property `mid`, `compact() -> dict`.
  - `JevVerdict(direction, direction_conf, beats_cost, flow_aligned, regime, exit_now, latency_ms, input_tokens, model, error=None, state={})` frozen, `answers() -> dict`, classmethod `failed(error, *, latency_ms, model, state)`.
  - `FutIntent(action, confidence, reason)` frozen.
  - `FutGate(ok, rule, action, side=None, contracts=0, price=0.0, notional=0.0, margin=0.0, stop=None, liq=None, leverage=1)` frozen.
  - `fill_price(*, bid, ask, last, buy, slippage_bps) -> float | None`; `liquidation_price(side, entry, leverage, mmr) -> float`; `round_to_unit(price, unit, *, up) -> float`.

- [ ] **Step 1: Write the failing tests**

Create `tests/fut/test_fut_settings.py`:

```python
import pytest

from fut.settings import MAX_LEVERAGE, FutSettings
from fut.types import FutPosition, FutSnapshot, JevVerdict
from kcex.fws import DEFAULT_FUT_WS_URL


def test_defaults_match_the_spec():
    s = FutSettings()
    assert (s.leverage, s.margin_usdt, s.max_balance_pct, s.starting_usdt) == (1, 20.0, 0.05, 450.0)
    assert (s.max_hold_s, s.jev_every_s, s.llm_cooldown_s, s.llm_timeout_s) == (300.0, 2.0, 10.0, 8.0)
    assert (s.stale_price_bps, s.wake_threshold, s.move_cost_bps) == (5.0, 0.6, 3.0)
    assert s.ws_url == DEFAULT_FUT_WS_URL
    assert MAX_LEVERAGE == 3


def test_from_env_reads_overrides(monkeypatch):
    monkeypatch.setenv("FUT_LEVERAGE", "3")
    monkeypatch.setenv("FUT_MARGIN_USDT", "15")
    monkeypatch.setenv("FUT_MAX_HOLD_SECONDS", "120")
    monkeypatch.setenv("FUT_WS_URL", "-")
    monkeypatch.setenv("FUT_LLM_REASONING", "1")
    s = FutSettings.from_env()
    assert (s.leverage, s.margin_usdt, s.max_hold_s, s.ws_url, s.llm_reasoning) == (3, 15.0, 120.0, "", True)


@pytest.mark.parametrize("leverage", [0, 4, 125])
def test_leverage_outside_one_to_three_is_refused(leverage):
    with pytest.raises(ValueError):
        FutSettings(leverage=leverage)


def test_other_invalid_values_are_refused():
    with pytest.raises(ValueError):
        FutSettings(slippage_bps=-1)
    with pytest.raises(ValueError):
        FutSettings(min_stop_pct=0.02, max_stop_pct=0.01)
    with pytest.raises(ValueError):
        FutSettings(symbol="ETH_USDT")


def test_mock_jev_without_key_or_with_mock_model():
    assert FutSettings().uses_mock_jev
    assert not FutSettings(typesafe_api_key="k").uses_mock_jev
    assert FutSettings(typesafe_api_key="k", jev_model="mock").uses_mock_jev


def test_position_and_snapshot_helpers():
    assert not FutPosition().is_open()
    assert FutPosition(side="short", contracts=2, entry=1.0).is_open()
    snap = FutSnapshot(1, 100.0, 99.0, 101.0, 100.0, 100.0, 0.0, None, 1.0, 0.0, {}, {}, {}, None, False)
    assert snap.mid == 100.0
    assert FutSnapshot(1, 100.0, 0.0, 101.0, 0.0, 0.0, 0.0, None, 0.0, 0.0, {}, {}, {}, None, True).mid == 100.0
    assert snap.compact()["bid"] == 99.0


def test_failed_verdict_carries_error():
    v = JevVerdict.failed("boom", latency_ms=5, model="jev-latest", state={"a": 1})
    assert v.error == "boom" and v.direction == "flat" and v.state == {"a": 1}
    assert set(v.answers()) == {"direction", "direction_conf", "beats_cost", "flow_aligned", "regime", "exit_now"}
```

Create `tests/fut/test_fut_pricing.py`:

```python
import pytest

from fut.pricing import fill_price, liquidation_price, round_to_unit


def test_buy_fills_at_ask_plus_slippage_and_sell_at_bid_minus_slippage():
    assert fill_price(bid=100.0, ask=101.0, last=100.5, buy=True, slippage_bps=10) == pytest.approx(101.0 * 1.001)
    assert fill_price(bid=100.0, ask=101.0, last=100.5, buy=False, slippage_bps=10) == pytest.approx(100.0 * 0.999)


def test_missing_side_falls_back_to_last_and_nothing_valid_is_none():
    assert fill_price(bid=0.0, ask=float("nan"), last=100.5, buy=True, slippage_bps=0) == 100.5
    assert fill_price(bid=0.0, ask=0.0, last=0.0, buy=True, slippage_bps=0) is None


def test_negative_or_non_finite_slippage_is_none():
    assert fill_price(bid=100.0, ask=101.0, last=100.0, buy=True, slippage_bps=-1) is None
    assert fill_price(bid=100.0, ask=101.0, last=100.0, buy=True, slippage_bps=float("inf")) is None


def test_isolated_liquidation_prices():
    assert liquidation_price("long", 100.0, 1, 0.005) == pytest.approx(0.5)
    assert liquidation_price("long", 100.0, 3, 0.005) == pytest.approx(67.1666666)
    assert liquidation_price("short", 100.0, 3, 0.005) == pytest.approx(132.8333333)
    with pytest.raises(ValueError):
        liquidation_price("flat", 100.0, 1, 0.005)


def test_round_to_unit():
    assert round_to_unit(75939.2849, 0.1, up=False) == 75939.2
    assert round_to_unit(76091.31, 0.1, up=True) == 76091.4
    assert round_to_unit(76091.4, 0.1, up=True) == 76091.4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./scripts/test tests/fut/test_fut_settings.py tests/fut/test_fut_pricing.py`
Expected: `ModuleNotFoundError: No module named 'fut'`

- [ ] **Step 3: Isolate the new env vars in tests**

In `tests/conftest.py`, append these names inside the `_SETTINGS_ENV_VARS` tuple (after `"KCEX_PLATFORM",`):

```python
    "FUT_SYMBOL", "FUT_LEVERAGE", "FUT_MARGIN_USDT", "FUT_MAX_BALANCE_PCT",
    "FUT_MAX_DAY_LOSS_USDT", "FUT_PAPER_STARTING_USDT", "FUT_SLIPPAGE_BPS", "FUT_ATR_PERIOD",
    "FUT_ATR_MULT", "FUT_MIN_STOP_PCT", "FUT_MAX_STOP_PCT", "FUT_LIQ_STOP_RATIO",
    "FUT_MAX_HOLD_SECONDS", "FUT_MIN_CONFIDENCE", "FUT_JEV_EVERY_SECONDS", "FUT_JEV_MODEL",
    "FUT_JEV_USD_PER_MTOK", "FUT_JEV_TIMEOUT_SECONDS", "FUT_WAKE_THRESHOLD", "FUT_MOVE_COST_BPS",
    "FUT_LLM_COOLDOWN_SECONDS", "FUT_LLM_TIMEOUT_SECONDS", "FUT_STALE_PRICE_BPS",
    "FUT_STALE_MARKET_SECONDS", "FUT_UNMONITORED_SECONDS", "FUT_LLM_REASONING", "FUT_WS_URL",
    "FUT_SHADOW_SEED", "TYPESAFE_API_KEY", "TYPESAFE_DEFAULT_MODEL", "TYPESAFE_BASE_URL",
```

- [ ] **Step 4: Implement `fut/__init__.py`, `fut/settings.py`, `fut/types.py`, `fut/pricing.py`**

`fut/__init__.py`: empty file.

`fut/settings.py`:

```python
"""Futures paper settings. Every number from the spec is an env var except MAX_LEVERAGE."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from bot.settings import Settings
from kcex.fws import DEFAULT_FUT_WS_URL

MAX_LEVERAGE = 3


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FutSettings:
    symbol: str = "BTC_USDT"
    leverage: int = 1
    margin_usdt: float = 20.0
    max_balance_pct: float = 0.05
    max_day_loss_usdt: float = 20.0
    starting_usdt: float = 450.0
    slippage_bps: float = 2.0
    atr_period: int = 14
    atr_mult: float = 2.0
    min_stop_pct: float = 0.001
    max_stop_pct: float = 0.01
    liq_stop_ratio: float = 0.5
    max_hold_s: float = 300.0
    min_confidence: float = 0.0
    jev_every_s: float = 2.0
    jev_model: str = "jev-latest"
    typesafe_api_key: str = ""
    jev_usd_per_mtok: float = 0.042
    jev_timeout_s: float = 2.0
    wake_threshold: float = 0.6
    move_cost_bps: float = 3.0
    llm_cooldown_s: float = 10.0
    llm_timeout_s: float = 8.0
    stale_price_bps: float = 5.0
    stale_market_s: float = 5.0
    unmonitored_s: float = 60.0
    llm_reasoning: bool = False
    ws_url: str = DEFAULT_FUT_WS_URL
    shadow_seed: int = 7
    llm: Settings = field(default_factory=Settings.from_env)

    def __post_init__(self) -> None:
        if self.symbol != "BTC_USDT":
            raise ValueError("futures paper supports BTC_USDT only")
        if isinstance(self.leverage, bool) or not isinstance(self.leverage, int) or not 1 <= self.leverage <= MAX_LEVERAGE:
            raise ValueError(f"FUT_LEVERAGE must be an integer in 1..{MAX_LEVERAGE}")
        positive = ("margin_usdt", "starting_usdt", "atr_mult", "min_stop_pct", "max_stop_pct",
                    "max_hold_s", "jev_every_s", "jev_timeout_s", "llm_timeout_s",
                    "stale_market_s", "unmonitored_s")
        for name in positive:
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and > 0")
        non_negative = ("max_day_loss_usdt", "slippage_bps", "min_confidence", "jev_usd_per_mtok",
                        "move_cost_bps", "llm_cooldown_s", "stale_price_bps")
        for name in non_negative:
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and >= 0")
        if not 0 < self.max_balance_pct <= 1:
            raise ValueError("max_balance_pct must be in (0, 1]")
        if self.min_stop_pct > self.max_stop_pct:
            raise ValueError("min_stop_pct must be <= max_stop_pct")
        if not 0 < self.liq_stop_ratio < 1:
            raise ValueError("liq_stop_ratio must be in (0, 1)")
        if not 0 < self.wake_threshold <= 1:
            raise ValueError("wake_threshold must be in (0, 1]")
        if self.atr_period < 1:
            raise ValueError("atr_period must be >= 1")

    @property
    def uses_mock_jev(self) -> bool:
        return self.jev_model == "mock" or not self.typesafe_api_key

    @classmethod
    def from_env(cls) -> "FutSettings":
        raw_ws = os.getenv("FUT_WS_URL", "").strip()
        ws_url = "" if raw_ws == "-" else (raw_ws or DEFAULT_FUT_WS_URL)
        return cls(
            symbol=os.getenv("FUT_SYMBOL", "BTC_USDT").strip() or "BTC_USDT",
            leverage=_i("FUT_LEVERAGE", 1),
            margin_usdt=_f("FUT_MARGIN_USDT", 20.0),
            max_balance_pct=_f("FUT_MAX_BALANCE_PCT", 0.05),
            max_day_loss_usdt=_f("FUT_MAX_DAY_LOSS_USDT", 20.0),
            starting_usdt=_f("FUT_PAPER_STARTING_USDT", 450.0),
            slippage_bps=_f("FUT_SLIPPAGE_BPS", 2.0),
            atr_period=_i("FUT_ATR_PERIOD", 14),
            atr_mult=_f("FUT_ATR_MULT", 2.0),
            min_stop_pct=_f("FUT_MIN_STOP_PCT", 0.001),
            max_stop_pct=_f("FUT_MAX_STOP_PCT", 0.01),
            liq_stop_ratio=_f("FUT_LIQ_STOP_RATIO", 0.5),
            max_hold_s=_f("FUT_MAX_HOLD_SECONDS", 300.0),
            min_confidence=_f("FUT_MIN_CONFIDENCE", 0.0),
            jev_every_s=_f("FUT_JEV_EVERY_SECONDS", 2.0),
            jev_model=os.getenv("FUT_JEV_MODEL", "jev-latest").strip() or "jev-latest",
            typesafe_api_key=os.getenv("TYPESAFE_API_KEY", "").strip(),
            jev_usd_per_mtok=_f("FUT_JEV_USD_PER_MTOK", 0.042),
            jev_timeout_s=_f("FUT_JEV_TIMEOUT_SECONDS", 2.0),
            wake_threshold=_f("FUT_WAKE_THRESHOLD", 0.6),
            move_cost_bps=_f("FUT_MOVE_COST_BPS", 3.0),
            llm_cooldown_s=_f("FUT_LLM_COOLDOWN_SECONDS", 10.0),
            llm_timeout_s=_f("FUT_LLM_TIMEOUT_SECONDS", 8.0),
            stale_price_bps=_f("FUT_STALE_PRICE_BPS", 5.0),
            stale_market_s=_f("FUT_STALE_MARKET_SECONDS", 5.0),
            unmonitored_s=_f("FUT_UNMONITORED_SECONDS", 60.0),
            llm_reasoning=_b("FUT_LLM_REASONING", False),
            ws_url=ws_url,
            shadow_seed=_i("FUT_SHADOW_SEED", 7),
            llm=Settings.from_env(),
        )
```

`fut/types.py`:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class FutPosition:
    side: str | None = None  # "long" | "short"
    contracts: int = 0
    entry: float = 0.0
    stop: float | None = None
    liq: float | None = None
    margin: float = 0.0
    leverage: int = 1
    opened_ms: int = 0
    funding_through_ms: int = 0

    def is_open(self) -> bool:
        return self.side in ("long", "short") and self.contracts > 0


@dataclass(frozen=True)
class FutSnapshot:
    ts_ms: int
    last: float
    bid: float
    ask: float
    fair: float
    index: float
    funding_rate: float
    next_funding_ms: int | None
    spread_bps: float
    imbalance: float
    depth_bps: dict[str, dict[str, float]]
    returns_bps: dict[str, float]
    flow: dict[str, dict[str, Any]]
    atr_1m: float | None
    stale: bool

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last

    def compact(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JevVerdict:
    direction: str  # "up" | "down" | "flat"
    direction_conf: float
    beats_cost: float
    flow_aligned: float
    regime: str
    exit_now: float | None
    latency_ms: int
    input_tokens: int
    model: str
    error: str | None = None
    state: dict[str, Any] = field(default_factory=dict)

    def answers(self) -> dict[str, Any]:
        return {"direction": self.direction, "direction_conf": self.direction_conf,
                "beats_cost": self.beats_cost, "flow_aligned": self.flow_aligned,
                "regime": self.regime, "exit_now": self.exit_now}

    @classmethod
    def failed(cls, error: str, *, latency_ms: int, model: str, state: dict[str, Any]) -> "JevVerdict":
        return cls("flat", 0.0, 0.0, 0.0, "unknown", None, latency_ms, 0, model, error=error, state=state)


@dataclass(frozen=True)
class FutIntent:
    action: str  # LONG | SHORT | CLOSE | HOLD
    confidence: float
    reason: str


@dataclass(frozen=True)
class FutGate:
    ok: bool
    rule: str
    action: str
    side: str | None = None
    contracts: int = 0
    price: float = 0.0
    notional: float = 0.0
    margin: float = 0.0
    stop: float | None = None
    liq: float | None = None
    leverage: int = 1
```

`fut/pricing.py`:

```python
"""One price basis shared by the collar (sizing) and the paper ledger (fills)."""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, ROUND_UP, Decimal


def fill_price(*, bid: float, ask: float, last: float, buy: bool, slippage_bps: float) -> float | None:
    if not math.isfinite(slippage_bps) or slippage_bps < 0:
        return None
    base = ask if buy else bid
    if not (math.isfinite(base) and base > 0):
        base = last
    if not (math.isfinite(base) and base > 0):
        return None
    factor = slippage_bps / 10_000.0
    return base * (1 + factor) if buy else base * (1 - factor)


def liquidation_price(side: str, entry: float, leverage: int, mmr: float) -> float:
    if side == "long":
        return entry * (1 - 1 / leverage + mmr)
    if side == "short":
        return entry * (1 + 1 / leverage - mmr)
    raise ValueError(f"unknown side {side!r}")


def round_to_unit(price: float, unit: float, *, up: bool) -> float:
    quantum = Decimal(str(unit))
    steps = (Decimal(str(price)) / quantum).quantize(Decimal(1), rounding=ROUND_UP if up else ROUND_DOWN)
    return float(steps * quantum)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./scripts/test tests/fut/test_fut_settings.py tests/fut/test_fut_pricing.py`
Expected: 14 passed

- [ ] **Step 6: Run the whole suite (conftest changed)**

Run: `./scripts/test`
Expected: all passed, 0 failed

- [ ] **Step 7: Commit**

```bash
git add fut/__init__.py fut/settings.py fut/types.py fut/pricing.py tests/conftest.py tests/fut/test_fut_settings.py tests/fut/test_fut_pricing.py
git commit -m "feat(fut): futures paper settings, types and shared pricing" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---
### Task 4: Futures collar (risk gate and sizing)

**Files:**
- Create: `fut/collar.py`
- Create: `tests/fut/helpers.py` (shared fixtures for later tasks)
- Test: `tests/fut/test_fut_collar.py`

**Interfaces:**
- Consumes: `FutSettings`, `MAX_LEVERAGE`, `FutGate`, `FutIntent`, `FutPosition`, `FutSnapshot`, `fill_price`, `liquidation_price`, `round_to_unit`, `ContractSpec`.
- Produces: `ACTIONS = {"LONG", "SHORT", "CLOSE", "HOLD"}`; `check(intent, snap, *, position, balance, day_pnl_usdt, spec, settings) -> FutGate`. Rules (exact strings): `action`, `hold`, `flat`, `ok_close`, `stale`, `contract_state`, `already_open`, `day_loss`, `confidence`, `atr`, `leverage`, `no_price`, `no_cash`, `dust`, `liq_too_close`, `ok_open`.
- Produces (tests only): `tests/fut/helpers.py` with `SPEC`, `make_snap(**overrides) -> FutSnapshot`.

- [ ] **Step 1: Create shared test helpers**

Create `tests/fut/helpers.py`:

```python
from fut.types import FutSnapshot
from kcex.fapi import ContractSpec

SPEC = ContractSpec(symbol="BTC_USDT", contract_size=0.0001, min_vol=1, max_vol=714000, price_unit=0.1,
                    taker_fee=0.0001, maker_fee=0.0, mmr=0.005, max_leverage=125, state=0)


def make_snap(**overrides) -> FutSnapshot:
    base = dict(ts_ms=0, last=76000.0, bid=76000.0, ask=76000.1, fair=76000.0, index=76000.0,
                funding_rate=0.0001, next_funding_ms=None, spread_bps=0.013, imbalance=0.0,
                depth_bps={}, returns_bps={}, flow={}, atr_1m=30.0, stale=False)
    base.update(overrides)
    return FutSnapshot(**base)
```

- [ ] **Step 2: Write the failing tests**

Create `tests/fut/test_fut_collar.py`:

```python
import pytest

from fut.collar import check
from fut.settings import FutSettings
from fut.types import FutIntent, FutPosition
from tests.fut.helpers import SPEC, make_snap

FLAT = FutPosition()
OPEN = FutPosition(side="long", contracts=2, entry=76000.0, stop=75900.0, liq=380.0, margin=15.2)


def gate(action, snap=None, *, position=FLAT, balance=450.0, day_pnl=0.0, spec=SPEC, confidence=0.8, **settings):
    return check(FutIntent(action, confidence, "r"), snap or make_snap(), position=position, balance=balance,
                 day_pnl_usdt=day_pnl, spec=spec, settings=FutSettings(**settings))


def test_long_sizes_off_ask_plus_slippage_with_atr_stop_below():
    g = gate("LONG")
    assert (g.ok, g.rule, g.side, g.contracts, g.leverage) == (True, "ok_open", "long", 2, 1)
    assert g.price == pytest.approx(76000.1 * 1.0002)
    assert g.stop == pytest.approx(75939.2)
    assert g.liq == pytest.approx(g.price * 0.005)
    assert g.notional == pytest.approx(2 * 0.0001 * g.price)
    assert g.margin == pytest.approx(g.notional)


def test_short_sizes_off_bid_minus_slippage_with_stop_above():
    g = gate("SHORT")
    assert (g.ok, g.side, g.contracts) == (True, "short", 2)
    assert g.price == pytest.approx(76000.0 * 0.9998)
    assert g.stop == pytest.approx(76060.8)
    assert g.liq == pytest.approx(g.price * (1 + 1 - 0.005))


def test_leverage_three_scales_exposure_not_margin_budget():
    g = gate("LONG", leverage=3)
    assert g.ok and g.contracts == 7 and g.leverage == 3
    assert g.margin == pytest.approx(g.notional / 3)


def test_stop_too_close_to_liquidation_is_refused():
    g = gate("LONG", make_snap(atr_1m=20000.0), leverage=3, max_stop_pct=0.5)
    assert (g.ok, g.rule) == (False, "liq_too_close")


def test_hold_invalid_and_close_rules():
    assert gate("HOLD").rule == "hold"
    assert gate("BUY").rule == "action"
    assert gate("CLOSE").rule == "flat"
    closing = gate("CLOSE", position=OPEN)
    assert (closing.ok, closing.rule, closing.side, closing.contracts) == (True, "ok_close", "long", 2)


def test_close_passes_even_when_stale_or_after_day_loss():
    assert gate("CLOSE", make_snap(stale=True), position=OPEN, day_pnl=-100.0).ok


@pytest.mark.parametrize("kwargs, rule", [
    (dict(snap=make_snap(stale=True)), "stale"),
    (dict(spec=SPEC.__class__(**{**SPEC.__dict__, "state": 1})), "contract_state"),
    (dict(position=OPEN), "already_open"),
    (dict(day_pnl=-20.0), "day_loss"),
    (dict(confidence=float("nan")), "confidence"),
    (dict(snap=make_snap(atr_1m=None)), "atr"),
    (dict(snap=make_snap(bid=0.0, ask=0.0, last=0.0)), "no_price"),
    (dict(balance=0.0), "no_cash"),
    (dict(balance=100.0), "dust"),
])
def test_entry_refusals(kwargs, rule):
    g = gate("LONG", **kwargs)
    assert (g.ok, g.rule) == (False, rule)


def test_min_confidence_is_enforced():
    assert gate("LONG", confidence=0.4, min_confidence=0.5).rule == "confidence"


def test_leverage_above_contract_max_is_refused():
    small = SPEC.__class__(**{**SPEC.__dict__, "max_leverage": 2})
    assert gate("LONG", spec=small, leverage=3).rule == "leverage"
```

Do not add `__init__.py` files: `tests` is imported as a namespace package with `PYTHONPATH=.` (set by `./scripts/test`), the same way `tests/conftest.py` already does `from tests._env_guard import ...`.

- [ ] **Step 3: Run tests to verify they fail**

Run: `./scripts/test tests/fut/test_fut_collar.py`
Expected: `ModuleNotFoundError: No module named 'fut.collar'`

- [ ] **Step 4: Implement `fut/collar.py`**

```python
"""Futures paper risk gate. Pure rules between the LLM intent and the paper ledger.

CLOSE is evaluated before every freshness/loss/confidence check so nothing here can
trap the bot in a position. Entries go through every check, in the spec's order.
"""

from __future__ import annotations

import math

from fut.pricing import fill_price, liquidation_price, round_to_unit
from fut.settings import MAX_LEVERAGE, FutSettings
from fut.types import FutGate, FutIntent, FutPosition, FutSnapshot
from kcex.fapi import ContractSpec

ACTIONS = {"LONG", "SHORT", "CLOSE", "HOLD"}


def check(intent: FutIntent, snap: FutSnapshot, *, position: FutPosition, balance: float,
          day_pnl_usdt: float, spec: ContractSpec, settings: FutSettings) -> FutGate:
    action = intent.action
    if action not in ACTIONS:
        return FutGate(False, "action", str(action))
    if action == "HOLD":
        return FutGate(False, "hold", "HOLD")
    if action == "CLOSE":
        if not position.is_open():
            return FutGate(False, "flat", "CLOSE")
        return FutGate(True, "ok_close", "CLOSE", side=position.side, contracts=position.contracts,
                       leverage=position.leverage)

    side = "long" if action == "LONG" else "short"
    if snap.stale:
        return FutGate(False, "stale", action)
    if spec.state != 0:
        return FutGate(False, "contract_state", action)
    if position.is_open():
        return FutGate(False, "already_open", action)
    if day_pnl_usdt <= -abs(settings.max_day_loss_usdt):
        return FutGate(False, "day_loss", action)
    if not math.isfinite(intent.confidence) or intent.confidence < settings.min_confidence:
        return FutGate(False, "confidence", action)
    if snap.atr_1m is None or not math.isfinite(snap.atr_1m) or snap.atr_1m <= 0:
        return FutGate(False, "atr", action)
    leverage = settings.leverage
    if not 1 <= leverage <= min(MAX_LEVERAGE, spec.max_leverage):
        return FutGate(False, "leverage", action)
    price = fill_price(bid=snap.bid, ask=snap.ask, last=snap.last, buy=side == "long",
                       slippage_bps=settings.slippage_bps)
    if price is None:
        return FutGate(False, "no_price", action)
    if not math.isfinite(balance) or balance <= 0:
        return FutGate(False, "no_cash", action)

    target = min(settings.margin_usdt, settings.max_balance_pct * balance) * leverage
    contracts = min(int(math.floor(target / (price * spec.contract_size))), spec.max_vol)
    if contracts < spec.min_vol:
        return FutGate(False, "dust", action)

    distance = min(max(settings.atr_mult * snap.atr_1m, settings.min_stop_pct * price),
                   settings.max_stop_pct * price)
    if side == "long":
        stop = round_to_unit(price - distance, spec.price_unit, up=False)
    else:
        stop = round_to_unit(price + distance, spec.price_unit, up=True)
    liq = liquidation_price(side, price, leverage, spec.mmr)
    if abs(price - stop) > settings.liq_stop_ratio * abs(price - liq):
        return FutGate(False, "liq_too_close", action)

    notional = contracts * spec.contract_size * price
    margin = notional / leverage
    if margin + notional * spec.taker_fee > balance:
        return FutGate(False, "no_cash", action)
    return FutGate(True, "ok_open", action, side=side, contracts=contracts, price=price, notional=notional,
                   margin=margin, stop=stop, liq=liq, leverage=leverage)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./scripts/test tests/fut/test_fut_collar.py`
Expected: 17 passed

- [ ] **Step 6: Commit**

```bash
git add fut/collar.py tests/fut/helpers.py tests/fut/test_fut_collar.py
git commit -m "feat(fut): futures collar with leverage cap, ATR stop and liquidation distance" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Futures store and paper ledger

**Files:**
- Create: `fut/store.py`
- Create: `fut/ledger.py`
- Test: `tests/fut/test_fut_ledger.py`

**Interfaces:**
- Consumes: `bot.store.Store` (constructor `Store(path, *, mode)`, `kv_get`, `kv_set(key, value, *, commit)`, `commit()`, `rollback()`, `self._conn`), `FutPosition`, `FutGate`, `FutSnapshot`, `FutSettings`, `ContractSpec`, `fill_price`.
- Produces:
  - `fut.store`: `MODE = "futures-paper"`; `day_of(ts_ms) -> "YYYY-MM-DD"`; `day_bounds_ms(day) -> (start_ms, end_ms)`; `FutStore(path, *, mode=MODE)` with `load_fut_position(book) -> FutPosition`, `save_fut_position(book, position, *, commit=True)`, `balance(book, starting) -> float`, `set_balance(book, value, *, commit=True)`, `add_fut_fill(book, *, ts_ms, kind, side, contracts, price, fee, funding, pnl, reason, commit=True)`, `fut_fills(book) -> list[dict]` (keys `id, ts_ms, day, kind, side, contracts, price, fee, funding, pnl, reason`), `day_net(book, day) -> float`, `log_decision(kind, payload, *, ts_ms)`, `decisions(kind=None) -> list[dict]` (keys `id, ts_ms, kind, payload`), `model_cost_between(start_ms, end_ms) -> float`.
  - `fut.ledger`: `PaperLedger(store, settings, spec, *, book="main")` with attributes `position`, `balance`, `book`, `spec`, methods `open(gate, *, now_ms) -> FutPosition`, `close(price, *, now_ms, reason) -> float` (net of fee), `market_exit_price(snap) -> float | None`, `mark(snap, *, now_ms) -> str | None` returning `"liquidation" | "stop" | "time_limit" | "funding" | None`, `unrealized(snap) -> float`.
  - Fill `kind` values: `open`, `close`, `funding`. Funding `funding` column is positive when paid.

- [ ] **Step 1: Write the failing tests**

Create `tests/fut/test_fut_ledger.py`:

```python
import pytest

from fut.ledger import PaperLedger
from fut.settings import FutSettings
from fut.store import FutStore, day_of
from fut.types import FutGate
from tests.fut.helpers import SPEC, make_snap


def make(tmp_path, **settings):
    store = FutStore(tmp_path / "fut.db")
    ledger = PaperLedger(store, FutSettings(**settings), SPEC)
    return store, ledger


def gate(side="long", price=76000.0, contracts=2, stop=None, liq=None):
    notional = contracts * 0.0001 * price
    if stop is None:
        stop = 75900.0 if side == "long" else 76100.0
    if liq is None:
        liq = price * 0.005 if side == "long" else price * 1.995
    return FutGate(True, "ok_open", "LONG" if side == "long" else "SHORT", side=side, contracts=contracts,
                   price=price, notional=notional, margin=notional, stop=stop, liq=liq, leverage=1)


def test_open_charges_taker_fee_and_survives_restart(tmp_path):
    store, ledger = make(tmp_path)
    pos = ledger.open(gate(), now_ms=1000)
    assert pos.is_open() and pos.funding_through_ms == 1000
    assert ledger.balance == pytest.approx(450 - 15.2 * 0.0001)
    again = PaperLedger(store, FutSettings(), SPEC)
    assert again.position == pos
    assert again.balance == pytest.approx(ledger.balance)
    assert [f["kind"] for f in store.fut_fills("main")] == ["open"]


def test_open_refuses_non_ok_gate_and_double_open(tmp_path):
    _, ledger = make(tmp_path)
    with pytest.raises(ValueError):
        ledger.open(FutGate(False, "stale", "LONG"), now_ms=1)
    ledger.open(gate(), now_ms=1)
    with pytest.raises(ValueError):
        ledger.open(gate(), now_ms=2)


def test_close_long_books_price_pnl_minus_fee(tmp_path):
    _, ledger = make(tmp_path)
    ledger.open(gate(), now_ms=1000)
    net = ledger.close(76100.0, now_ms=2000, reason="llm_close")
    close_fee = 76100.0 * 0.0002 * 0.0001
    assert net == pytest.approx(0.02 - close_fee)
    assert ledger.balance == pytest.approx(450 - 15.2 * 0.0001 + 0.02 - close_fee)
    assert not ledger.position.is_open()


def test_short_profits_when_price_falls(tmp_path):
    _, ledger = make(tmp_path)
    ledger.open(gate("short"), now_ms=1000)
    assert ledger.close(75900.0, now_ms=2000, reason="llm_close") == pytest.approx(0.02 - 75900.0 * 0.0002 * 0.0001)


def test_long_stop_fills_at_worse_of_stop_and_book_with_slippage(tmp_path):
    store, ledger = make(tmp_path)
    ledger.open(gate(), now_ms=1000)
    assert ledger.mark(make_snap(bid=75850.0, last=75860.0, ask=75850.1), now_ms=2000) == "stop"
    close = store.fut_fills("main")[-1]
    assert close["reason"] == "stop"
    assert close["price"] == pytest.approx(75850.0 * 0.9998)


def test_short_stop(tmp_path):
    store, ledger = make(tmp_path)
    ledger.open(gate("short"), now_ms=1000)
    assert ledger.mark(make_snap(bid=76149.9, ask=76150.0, last=76140.0, fair=76140.0), now_ms=2000) == "stop"
    assert store.fut_fills("main")[-1]["price"] == pytest.approx(76150.0 * 1.0002)


def test_liquidation_by_fair_price_wins_over_stop_and_loses_margin(tmp_path):
    _, ledger = make(tmp_path)
    ledger.open(gate(stop=75600.0, liq=75000.0), now_ms=1000)
    assert ledger.mark(make_snap(bid=75500.0, last=75500.0, fair=74990.0), now_ms=2000) == "liquidation"
    assert ledger.balance == pytest.approx(450 - 15.2 * 0.0001 - 15.2)


def test_time_limit_closes_at_market(tmp_path):
    store, ledger = make(tmp_path, max_hold_s=300)
    ledger.open(gate(), now_ms=1000)
    assert ledger.mark(make_snap(), now_ms=300_999) is None
    assert ledger.mark(make_snap(), now_ms=301_000) == "time_limit"
    assert store.fut_fills("main")[-1]["price"] == pytest.approx(76000.0 * 0.9998)


def test_funding_long_pays_positive_rate_once(tmp_path):
    store, ledger = make(tmp_path)
    ledger.open(gate(), now_ms=1000)
    before = ledger.balance
    snap = make_snap(next_funding_ms=5000, funding_rate=0.0001)
    assert ledger.mark(snap, now_ms=4999) is None
    assert ledger.mark(snap, now_ms=6000) == "funding"
    assert ledger.mark(snap, now_ms=7000) is None
    assert ledger.balance == pytest.approx(before - 2 * 0.0001 * 76000.0 * 0.0001)
    assert ledger.position.funding_through_ms == 5000
    assert store.fut_fills("main")[-1]["kind"] == "funding"


def test_funding_short_receives(tmp_path):
    _, ledger = make(tmp_path)
    ledger.open(gate("short"), now_ms=1000)
    before = ledger.balance
    ledger.mark(make_snap(next_funding_ms=5000, funding_rate=0.0001), now_ms=6000)
    assert ledger.balance == pytest.approx(before + 2 * 0.0001 * 76000.0 * 0.0001)


def test_failed_write_rolls_back_everything(tmp_path, monkeypatch):
    store, ledger = make(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("disk")

    monkeypatch.setattr(store, "set_balance", boom)
    with pytest.raises(RuntimeError):
        ledger.open(gate(), now_ms=1000)
    monkeypatch.undo()
    fresh = PaperLedger(store, FutSettings(), SPEC)
    assert not fresh.position.is_open()
    assert fresh.balance == 450.0
    assert store.fut_fills("main") == []
    assert not ledger.position.is_open()


def test_books_are_independent_and_day_net_counts_fees_and_funding(tmp_path):
    store, ledger = make(tmp_path)
    shadow = PaperLedger(store, FutSettings(), SPEC, book="shadow:random")
    ledger.open(gate(), now_ms=1000)
    assert not shadow.position.is_open() and shadow.balance == 450.0
    ledger.close(76100.0, now_ms=2000, reason="llm_close")
    expected = 0.02 - 15.2 * 0.0001 - 76100.0 * 0.0002 * 0.0001
    assert store.day_net("main", day_of(2000)) == pytest.approx(expected)
    assert store.day_net("shadow:random", day_of(2000)) == 0.0


def test_unrealized_marks_to_mid(tmp_path):
    _, ledger = make(tmp_path)
    ledger.open(gate(), now_ms=1000)
    assert ledger.unrealized(make_snap(bid=76100.0, ask=76100.2)) == pytest.approx(100.1 * 0.0002)


def test_store_refuses_a_database_stamped_by_another_mode(tmp_path):
    from bot.store import Store, StoreIdentityMismatch

    Store(tmp_path / "x.db", mode="paper").close()
    with pytest.raises(StoreIdentityMismatch):
        FutStore(tmp_path / "x.db")


def test_decisions_and_model_cost(tmp_path):
    store, _ = make(tmp_path)
    store.log_decision("jev", {"cost_usd": 0.001, "model": "jev-1"}, ts_ms=1000)
    store.log_decision("llm", {"cost_usd": 0.01}, ts_ms=2000)
    store.log_decision("exit", {"reason": "stop"}, ts_ms=3000)
    assert [d["kind"] for d in store.decisions()] == ["jev", "llm", "exit"]
    assert store.decisions("llm")[0]["payload"] == {"cost_usd": 0.01}
    assert store.model_cost_between(0, 2500) == pytest.approx(0.011)
    assert store.model_cost_between(1500, 2500) == pytest.approx(0.01)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./scripts/test tests/fut/test_fut_ledger.py`
Expected: `ModuleNotFoundError: No module named 'fut.ledger'`

- [ ] **Step 3: Implement `fut/store.py`**

```python
"""SQLite state for futures paper: per-book positions, fills and balances, plus a decision log.

Subclasses bot.store.Store to inherit its mode stamp (StoreIdentityMismatch), kv table and
durable LLM budget. The spot tables it also creates stay unused here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.store import Store
from fut.types import FutPosition

MODE = "futures-paper"
_BALANCE_KEY = "fut_balance:{book}"


def day_of(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def day_bounds_ms(day: str) -> tuple[int, int]:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    return start_ms, start_ms + 86_400_000


class FutStore(Store):
    def __init__(self, path: Path, *, mode: str = MODE):
        super().__init__(path, mode=mode)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS fut_position (
                book TEXT PRIMARY KEY, side TEXT NOT NULL, contracts INTEGER NOT NULL,
                entry REAL NOT NULL, stop REAL, liq REAL, margin REAL NOT NULL,
                leverage INTEGER NOT NULL, opened_ms INTEGER NOT NULL,
                funding_through_ms INTEGER NOT NULL)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS fut_fills (
                id INTEGER PRIMARY KEY, book TEXT NOT NULL, ts_ms INTEGER NOT NULL, day TEXT NOT NULL,
                kind TEXT NOT NULL, side TEXT, contracts INTEGER, price REAL,
                fee REAL NOT NULL DEFAULT 0, funding REAL NOT NULL DEFAULT 0,
                pnl REAL NOT NULL DEFAULT 0, reason TEXT)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS fut_decisions (
                id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)"""
        )
        self.commit()

    # -- positions ---------------------------------------------------------------

    def load_fut_position(self, book: str) -> FutPosition:
        row = self._conn.execute(
            "SELECT side, contracts, entry, stop, liq, margin, leverage, opened_ms, funding_through_ms "
            "FROM fut_position WHERE book=?", (book,)).fetchone()
        if row is None:
            return FutPosition()
        return FutPosition(side=row[0], contracts=int(row[1]), entry=float(row[2]), stop=row[3], liq=row[4],
                           margin=float(row[5]), leverage=int(row[6]), opened_ms=int(row[7]),
                           funding_through_ms=int(row[8]))

    def save_fut_position(self, book: str, position: FutPosition, *, commit: bool = True) -> None:
        if not position.is_open():
            self._conn.execute("DELETE FROM fut_position WHERE book=?", (book,))
        else:
            self._conn.execute(
                """INSERT INTO fut_position(book, side, contracts, entry, stop, liq, margin, leverage,
                       opened_ms, funding_through_ms) VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(book) DO UPDATE SET side=excluded.side, contracts=excluded.contracts,
                       entry=excluded.entry, stop=excluded.stop, liq=excluded.liq, margin=excluded.margin,
                       leverage=excluded.leverage, opened_ms=excluded.opened_ms,
                       funding_through_ms=excluded.funding_through_ms""",
                (book, position.side, position.contracts, position.entry, position.stop, position.liq,
                 position.margin, position.leverage, position.opened_ms, position.funding_through_ms))
        if commit:
            self.commit()

    # -- balances ----------------------------------------------------------------

    def balance(self, book: str, starting: float) -> float:
        raw = self.kv_get(_BALANCE_KEY.format(book=book))
        return float(raw) if raw is not None else float(starting)

    def set_balance(self, book: str, value: float, *, commit: bool = True) -> None:
        self.kv_set(_BALANCE_KEY.format(book=book), repr(float(value)), commit=commit)

    # -- fills -------------------------------------------------------------------

    def add_fut_fill(self, book: str, *, ts_ms: int, kind: str, side: str | None, contracts: int,
                     price: float, fee: float, funding: float, pnl: float, reason: str,
                     commit: bool = True) -> None:
        self._conn.execute(
            "INSERT INTO fut_fills(book, ts_ms, day, kind, side, contracts, price, fee, funding, pnl, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (book, ts_ms, day_of(ts_ms), kind, side, contracts, price, fee, funding, pnl, reason))
        if commit:
            self.commit()

    def fut_fills(self, book: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, ts_ms, day, kind, side, contracts, price, fee, funding, pnl, reason "
            "FROM fut_fills WHERE book=? ORDER BY id", (book,)).fetchall()
        keys = ("id", "ts_ms", "day", "kind", "side", "contracts", "price", "fee", "funding", "pnl", "reason")
        return [dict(zip(keys, row)) for row in rows]

    def day_net(self, book: str, day: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl),0) - COALESCE(SUM(fee),0) - COALESCE(SUM(funding),0) "
            "FROM fut_fills WHERE book=? AND day=?", (book, day)).fetchone()
        return float(row[0])

    # -- decisions ---------------------------------------------------------------

    def log_decision(self, kind: str, payload: dict[str, Any], *, ts_ms: int) -> None:
        self._conn.execute("INSERT INTO fut_decisions(ts_ms, kind, payload) VALUES (?,?,?)",
                           (ts_ms, kind, json.dumps(payload, default=str)))
        self.commit()

    def decisions(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is None:
            rows = self._conn.execute("SELECT id, ts_ms, kind, payload FROM fut_decisions ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, ts_ms, kind, payload FROM fut_decisions WHERE kind=? ORDER BY id", (kind,)).fetchall()
        return [{"id": r[0], "ts_ms": r[1], "kind": r[2], "payload": json.loads(r[3])} for r in rows]

    def model_cost_between(self, start_ms: int, end_ms: int) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(json_extract(payload, '$.cost_usd')), 0) FROM fut_decisions "
            "WHERE kind IN ('jev', 'llm') AND ts_ms >= ? AND ts_ms < ?", (start_ms, end_ms)).fetchone()
        return float(row[0] or 0.0)
```

- [ ] **Step 4: Implement `fut/ledger.py`**

```python
"""Paper ledger for one futures book. Fill, position and balance commit together or not at all."""

from __future__ import annotations

from dataclasses import replace

from fut.pricing import fill_price
from fut.settings import FutSettings
from fut.store import FutStore
from fut.types import FutGate, FutPosition, FutSnapshot
from kcex.fapi import ContractSpec


class PaperLedger:
    def __init__(self, store: FutStore, settings: FutSettings, spec: ContractSpec, *, book: str = "main"):
        self.store = store
        self.settings = settings
        self.spec = spec
        self.book = book
        self.position = store.load_fut_position(book)
        self.balance = store.balance(book, settings.starting_usdt)

    def _commit(self, *, fill: dict, position: FutPosition, balance: float) -> None:
        try:
            self.store.add_fut_fill(self.book, commit=False, **fill)
            self.store.save_fut_position(self.book, position, commit=False)
            self.store.set_balance(self.book, balance, commit=False)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        # In-memory state changes only after the transaction is durable.
        self.position = position
        self.balance = balance

    def open(self, gate: FutGate, *, now_ms: int) -> FutPosition:
        if not gate.ok or gate.action not in ("LONG", "SHORT") or gate.side not in ("long", "short"):
            raise ValueError(f"open() needs an ok LONG/SHORT gate, got {gate}")
        if self.position.is_open():
            raise ValueError("position already open")
        fee = gate.notional * self.spec.taker_fee
        position = FutPosition(side=gate.side, contracts=gate.contracts, entry=gate.price, stop=gate.stop,
                               liq=gate.liq, margin=gate.margin, leverage=gate.leverage, opened_ms=now_ms,
                               funding_through_ms=now_ms)
        self._commit(fill=dict(ts_ms=now_ms, kind="open", side=gate.side, contracts=gate.contracts,
                               price=gate.price, fee=fee, funding=0.0, pnl=0.0, reason="entry"),
                     position=position, balance=self.balance - fee)
        return position

    def close(self, price: float, *, now_ms: int, reason: str) -> float:
        pos = self.position
        if not pos.is_open():
            raise ValueError("no open position")
        qty = pos.contracts * self.spec.contract_size
        if reason == "liquidation":
            pnl, fee = -pos.margin, 0.0
        else:
            pnl = (price - pos.entry) * qty if pos.side == "long" else (pos.entry - price) * qty
            fee = price * qty * self.spec.taker_fee
        self._commit(fill=dict(ts_ms=now_ms, kind="close", side=pos.side, contracts=pos.contracts, price=price,
                               fee=fee, funding=0.0, pnl=pnl, reason=reason),
                     position=FutPosition(), balance=self.balance + pnl - fee)
        return pnl - fee

    def market_exit_price(self, snap: FutSnapshot) -> float | None:
        return fill_price(bid=snap.bid, ask=snap.ask, last=snap.last, buy=self.position.side == "short",
                          slippage_bps=self.settings.slippage_bps)

    def mark(self, snap: FutSnapshot, *, now_ms: int) -> str | None:
        pos = self.position
        if not pos.is_open():
            return None
        slip = self.settings.slippage_bps / 10_000.0
        fair = snap.fair if snap.fair > 0 else snap.last

        if pos.liq is not None and fair > 0 and (
                (pos.side == "long" and fair <= pos.liq) or (pos.side == "short" and fair >= pos.liq)):
            self.close(pos.liq, now_ms=now_ms, reason="liquidation")
            return "liquidation"

        if pos.stop is not None:
            if pos.side == "long":
                refs = [x for x in (snap.bid, snap.last) if x > 0]
                if refs and min(refs) <= pos.stop:
                    self.close(min(min(refs), pos.stop) * (1 - slip), now_ms=now_ms, reason="stop")
                    return "stop"
            else:
                refs = [x for x in (snap.ask, snap.last) if x > 0]
                if refs and max(refs) >= pos.stop:
                    self.close(max(max(refs), pos.stop) * (1 + slip), now_ms=now_ms, reason="stop")
                    return "stop"

        if now_ms - pos.opened_ms >= self.settings.max_hold_s * 1000:
            price = self.market_exit_price(snap)
            if price is not None:
                self.close(price, now_ms=now_ms, reason="time_limit")
                return "time_limit"

        nxt = snap.next_funding_ms
        if nxt is not None and pos.funding_through_ms < nxt <= now_ms and fair > 0:
            amount = pos.contracts * self.spec.contract_size * fair * snap.funding_rate
            paid = amount if pos.side == "long" else -amount
            self._commit(fill=dict(ts_ms=now_ms, kind="funding", side=pos.side, contracts=pos.contracts,
                                   price=fair, fee=0.0, funding=paid, pnl=0.0, reason="funding"),
                         position=replace(pos, funding_through_ms=nxt), balance=self.balance - paid)
            return "funding"
        return None

    def unrealized(self, snap: FutSnapshot) -> float:
        pos = self.position
        if not pos.is_open() or snap.mid <= 0:
            return 0.0
        qty = pos.contracts * self.spec.contract_size
        return (snap.mid - pos.entry) * qty if pos.side == "long" else (pos.entry - snap.mid) * qty
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./scripts/test tests/fut/test_fut_ledger.py`
Expected: 15 passed

- [ ] **Step 6: Commit**

```bash
git add fut/store.py fut/ledger.py tests/fut/test_fut_ledger.py
git commit -m "feat(fut): futures paper store and ledger (fees, stop, time limit, liquidation, funding)" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---
### Task 6: Market state and snapshot features

**Files:**
- Create: `fut/market.py`
- Test: `tests/fut/test_fut_market.py`

**Interfaces:**
- Consumes: `kcex.fws` (`FutTicker`, `FutDeal`, `FutDepth`, `FutFair`, `OrderBook`), `bot.atr.atr(bars, period)`, `bot.types.Bar`, `FutSettings`, `FutSnapshot`, `ContractSpec`.
- Produces: `MarketState(settings, spec)` with attributes `book`, `spec` (reassignable), `last_event_ms`, `ws_last_ms`, `bars_1m`; methods `apply(event, *, now_ms, source="ws") -> bool` (False only when a depth delta unsynced the book), `load_book(version, bids, asks)`, `set_funding(rate, next_ms)`, `set_bars(rows)` (rows `(t, o, h, l, c, v)`), `bid()`, `ask()`, `last()`, `mid()`, `snapshot(now_ms) -> FutSnapshot`.
- Snapshot feature keys: `depth_bps` → `{"5": {"bid", "ask"}, "10": ..., "25": ...}` in contracts; `imbalance` from the 25 bps band; `returns_bps` → `"2s", "10s", "60s", "300s"` (0.0 when history is missing); `flow` → `"30s"`/`"120s"` → `{"buy", "sell", "cvd", "vwap"}` in contracts (`vwap` None without volume); `stale` = no price, or no **WS** event for `stale_market_s` (REST fallback prices do not make the market fresh for entries).

- [ ] **Step 1: Write the failing tests**

Create `tests/fut/test_fut_market.py`:

```python
import pytest

from fut.market import MarketState
from fut.settings import FutSettings
from kcex.fws import FutDeal, FutDepth, FutFair, FutTicker
from tests.fut.helpers import SPEC


def ticker(bid, ask, last=None, ts=0, fair=0.0, funding=0.0001):
    return FutTicker(ts, last if last is not None else bid, bid, ask, fair, 0.0, funding)


def market(**settings):
    return MarketState(FutSettings(**settings), SPEC)


def test_snapshot_uses_synced_book_for_bid_ask_depth_and_imbalance():
    m = market()
    m.load_book(10, [(100.0, 5), (99.98, 7), (99.0, 1)], [(100.02, 4), (101.0, 9)])
    m.apply(ticker(99.0, 102.0, last=100.0), now_ms=1000)
    s = m.snapshot(1000)
    assert (s.bid, s.ask, s.last) == (100.0, 100.02, 100.0)
    assert s.mid == pytest.approx(100.01)
    assert s.spread_bps == pytest.approx(0.02 / 100.01 * 10_000)
    assert s.depth_bps["5"] == {"bid": 12, "ask": 4}
    assert s.depth_bps["25"] == {"bid": 12, "ask": 4}
    assert s.imbalance == pytest.approx(0.5)


def test_falls_back_to_ticker_quotes_when_book_unsynced():
    m = market()
    m.apply(ticker(99.0, 101.0, last=100.0), now_ms=0)
    s = m.snapshot(0)
    assert (s.bid, s.ask) == (99.0, 101.0)
    assert s.depth_bps["5"] == {"bid": 0, "ask": 0}
    assert s.imbalance == 0.0


def test_fair_funding_and_next_settle():
    m = market()
    m.apply(ticker(100.0, 100.0, fair=100.2, funding=0.0002), now_ms=0)
    m.apply(FutFair(0, 100.3), now_ms=1)
    m.set_funding(0.0003, 5000)
    s = m.snapshot(1)
    assert (s.fair, s.funding_rate, s.next_funding_ms) == (100.3, 0.0003, 5000)


def test_returns_use_mid_history():
    m = market()
    m.apply(ticker(100.0, 100.0), now_ms=0)
    m.apply(ticker(101.0, 101.0), now_ms=10_000)
    s = m.snapshot(10_000)
    assert s.returns_bps["10s"] == pytest.approx(100.0)
    assert s.returns_bps["60s"] == 0.0


def test_flow_windows_split_aggressor_volume():
    m = market()
    m.apply(FutDeal(1000, 100.0, 10, "buy"), now_ms=1000)
    m.apply(FutDeal(20_000, 101.0, 4, "sell"), now_ms=20_000)
    s = m.snapshot(40_000)
    assert s.flow["30s"] == {"buy": 0, "sell": 4, "cvd": -4, "vwap": pytest.approx(101.0)}
    assert s.flow["120s"] == {"buy": 10, "sell": 4, "cvd": 6, "vwap": pytest.approx(1404 / 14)}
    assert market().snapshot(0).flow["30s"]["vwap"] is None


def test_rest_prices_do_not_make_the_market_fresh_for_entries():
    m = market(stale_market_s=5)
    m.apply(ticker(100.0, 100.0), now_ms=0, source="ws")
    assert m.snapshot(3000).stale is False
    m.apply(ticker(100.0, 100.0), now_ms=10_000, source="rest")
    assert m.last_event_ms == 10_000 and m.ws_last_ms == 0
    assert m.snapshot(10_000).stale is True


def test_no_price_is_stale():
    assert market().snapshot(0).stale is True


def test_atr_from_one_minute_bars():
    m = market(atr_period=14)
    m.set_bars([(i * 60, 100.0, 101.0, 99.0, 100.0, 1.0) for i in range(16)])
    assert m.snapshot(0).atr_1m == pytest.approx(2.0)


def test_depth_gap_reports_resync_needed():
    m = market()
    m.load_book(10, [(100.0, 5)], [(101.0, 5)])
    assert m.apply(FutDepth(0, 11, ((100.0, 6),), ()), now_ms=1) is True
    assert m.apply(FutDepth(0, 13, (), ()), now_ms=2) is False
    assert not m.book.synced
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./scripts/test tests/fut/test_fut_market.py`
Expected: `ModuleNotFoundError: No module named 'fut.market'`

- [ ] **Step 3: Implement `fut/market.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./scripts/test tests/fut/test_fut_market.py`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add fut/market.py tests/fut/test_fut_market.py
git commit -m "feat(fut): market state with book depth, returns, aggressor flow and staleness" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Jev questions, wake rule and client

**Files:**
- Modify: `requirements.txt` (add `typesafe-sdk>=0.6.0`)
- Create: `fut/questions.py`
- Create: `fut/jev.py`
- Test: `tests/fut/test_fut_jev.py`

**Interfaces:**
- Consumes: `typesafe_sdk.Choice`, `Noul`, `NoulCriteria`, `RetryPolicy`, `TypeSafeClient` (`system_one(state, questions)` → response with `.choices[key].choice/.confidence`, `.nouls[key].noul`, `.usage.input_tokens`, `.model`); `FutSettings`, `FutSnapshot`, `FutPosition`, `JevVerdict`.
- Produces:
  - `fut.questions`: `build_questions(*, has_position, move_cost_bps) -> dict` with keys `direction_60s` (Choice up/down/flat), `move_beats_cost` (Noul), `flow_aligned` (Noul), `regime` (Choice trend/range/volatile), plus `exit_now` (Noul) only with a position; `jev_state(snap, position, *, now_ms) -> dict`; `jev_side(verdict) -> "long" | "short" | None`; `should_wake(verdict, position, *, threshold) -> "entry_signal" | "exit_signal" | "reversal_signal" | None`.
  - `fut.jev`: `JevClient(settings, *, client=None)` and `MockJev(settings)`, both with `name: str` and `evaluate(snap, position, *, now_ms) -> JevVerdict`; `make_jev(settings)`.

- [ ] **Step 1: Install and pin the SDK**

Run: `.venv/bin/pip install "typesafe-sdk>=0.6.0"`
Expected: `Successfully installed ... typesafe-sdk-0.6.0`

Append to `requirements.txt`:

```
typesafe-sdk>=0.6.0
```

Run: `./scripts/test tests/test_env_guard.py`
Expected: passed (the env guard now requires `typesafe-sdk` and finds it)

- [ ] **Step 2: Write the failing tests**

Create `tests/fut/test_fut_jev.py`:

```python
from types import SimpleNamespace

import pytest

from fut.jev import JevClient, MockJev, make_jev
from fut.questions import build_questions, jev_side, jev_state, should_wake
from fut.settings import FutSettings
from fut.types import FutPosition, JevVerdict
from tests.fut.helpers import make_snap

LONG = FutPosition(side="long", contracts=2, entry=76000.0, stop=75900.0, liq=380.0, margin=15.2, opened_ms=0)


def verdict(direction="up", conf=0.8, beats=0.8, exit_now=None, error=None):
    return JevVerdict(direction, conf, beats, 0.5, "trend", exit_now, 100, 1000, "jev-1", error=error)


def test_questions_without_and_with_position():
    flat = build_questions(has_position=False, move_cost_bps=3.0)
    assert set(flat) == {"direction_60s", "move_beats_cost", "flow_aligned", "regime"}
    assert set(flat["direction_60s"].criteria) == {"up", "down", "flat"}
    assert set(flat["regime"].criteria) == {"trend", "range", "volatile"}
    assert "3 bps" in str(flat["move_beats_cost"].instructions)
    assert "exit_now" in build_questions(has_position=True, move_cost_bps=3.0)


def test_jev_state_flat_and_open():
    snap = make_snap()
    assert jev_state(snap, FutPosition(), now_ms=0)["position"] == "flat"
    state = jev_state(snap, LONG, now_ms=30_000)
    pos = state["position"]
    assert pos["side"] == "long" and pos["seconds_open"] == 30
    assert pos["stop_distance_bps"] == pytest.approx((snap.mid - 75900.0) / snap.mid * 10_000, abs=0.01)
    assert {"mid", "spread_bps", "imbalance", "depth_bps", "returns_bps", "flow", "funding_rate"} <= set(state)


def test_should_wake_entry_needs_direction_and_cost():
    flat = FutPosition()
    assert should_wake(verdict(), flat, threshold=0.6) == "entry_signal"
    assert should_wake(verdict(conf=0.5), flat, threshold=0.6) is None
    assert should_wake(verdict(beats=0.5), flat, threshold=0.6) is None
    assert should_wake(verdict(direction="flat"), flat, threshold=0.6) is None
    assert should_wake(verdict(error="timeout"), flat, threshold=0.6) is None


def test_should_wake_with_position():
    assert should_wake(verdict(exit_now=0.7), LONG, threshold=0.6) == "exit_signal"
    assert should_wake(verdict(direction="down", exit_now=0.1), LONG, threshold=0.6) == "reversal_signal"
    assert should_wake(verdict(direction="up", exit_now=0.1), LONG, threshold=0.6) is None
    assert jev_side(verdict(direction="down")) == "short"


class FakeClient:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    def system_one(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        if self.exc:
            raise self.exc
        return self.response


def response():
    return SimpleNamespace(
        choices={"direction_60s": SimpleNamespace(choice="down", confidence=0.72),
                 "regime": SimpleNamespace(choice="trend", confidence=0.9)},
        nouls={"move_beats_cost": SimpleNamespace(noul=0.66), "flow_aligned": SimpleNamespace(noul=0.8),
               "exit_now": SimpleNamespace(noul=0.1)},
        usage=SimpleNamespace(input_tokens=1500), model="jev-1.13.0")


def test_jev_client_maps_answers():
    fake = FakeClient(response())
    v = JevClient(FutSettings(typesafe_api_key="k"), client=fake).evaluate(make_snap(), LONG, now_ms=1000)
    assert (v.direction, v.direction_conf, v.beats_cost, v.flow_aligned) == ("down", 0.72, 0.66, 0.8)
    assert (v.regime, v.exit_now, v.input_tokens, v.model, v.error) == ("trend", 0.1, 1500, "jev-1.13.0", None)
    assert "exit_now" in fake.calls[0][1]
    assert v.state["position"]["side"] == "long"


def test_jev_client_failure_is_a_named_verdict():
    v = JevClient(FutSettings(typesafe_api_key="k"), client=FakeClient(exc=TimeoutError("slow"))).evaluate(
        make_snap(), FutPosition(), now_ms=1000)
    assert v.error.startswith("TimeoutError") and v.direction == "flat"
    assert should_wake(v, FutPosition(), threshold=0.6) is None


def test_mock_jev_follows_momentum_and_flow():
    snap = make_snap(returns_bps={"10s": 20.0, "60s": 6.0}, imbalance=0.5,
                     flow={"30s": {"buy": 10, "sell": 0, "cvd": 10, "vwap": 76000.0}})
    v = MockJev(FutSettings()).evaluate(snap, FutPosition(), now_ms=0)
    assert v.direction == "up" and v.direction_conf > 0.9 and v.beats_cost == 1.0
    assert v.model == "mock" and v.exit_now is None


def test_make_jev_uses_mock_without_key():
    assert isinstance(make_jev(FutSettings()), MockJev)
    assert isinstance(make_jev(FutSettings(typesafe_api_key="k", jev_model="mock")), MockJev)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./scripts/test tests/fut/test_fut_jev.py`
Expected: `ModuleNotFoundError: No module named 'fut.jev'`

- [ ] **Step 4: Implement `fut/questions.py`**

```python
"""Every question Jev is asked and every threshold that turns its answers into an LLM call.

Review this file, not the loop, when the trigger behaves unexpectedly.
"""

from __future__ import annotations

from typesafe_sdk import Choice, Noul, NoulCriteria

from fut.types import FutPosition, FutSnapshot, JevVerdict


def _r(value, digits=2):
    return round(float(value), digits)


def build_questions(*, has_position: bool, move_cost_bps: float) -> dict:
    cost = f"{move_cost_bps:g} bps"
    questions = {
        "direction_60s": Choice(
            instructions={
                "question": "Where will the BTC_USDT perpetual mid be 60 seconds after `ts_ms`, relative to `mid`?",
                "goal": f"Seconds-horizon futures trading. A trade only pays if the move beats about {cost} of cost.",
                "inputs": "`flow` (aggressor contracts and cvd over 30s/120s) and `depth_bps`/`imbalance` are the "
                          "fastest signals; `returns_bps` is the recent path; `funding_rate` and "
                          "`fair_minus_last_bps` show positioning pressure.",
            },
            criteria={
                "up": f"Mid more than {cost} above the current mid after 60 seconds",
                "down": f"Mid more than {cost} below the current mid after 60 seconds",
                "flat": f"Mid within {cost} of the current mid after 60 seconds",
            },
        ),
        "move_beats_cost": Noul(
            instructions=f"Will the mid move more than {cost} in either direction within the next 60 seconds?",
            criteria=NoulCriteria(true=f"A move larger than {cost} is likely within 60 seconds",
                                  false=f"The mid is likely to stay within {cost} for 60 seconds"),
        ),
        "flow_aligned": Noul(
            instructions="Does the recent aggressor flow in `flow` push in the same direction as the recent "
                         "price path in `returns_bps`?",
        ),
        "regime": Choice(
            instructions="Which regime describes the last few minutes of this market?",
            criteria={
                "trend": "Persistent one-directional movement with aggressor flow on the same side",
                "range": "Price oscillating around a level without follow-through",
                "volatile": "Large fast swings in both directions",
            },
        ),
    }
    if has_position:
        questions["exit_now"] = Noul(
            instructions="Given `position`, has the case for keeping this position weakened enough that closing "
                         "now is better than waiting?",
            criteria=NoulCriteria(true="Flow, depth or the price path now point against the position side",
                                  false="The market still supports the position side, or nothing changed"),
        )
    return questions


def jev_state(snap: FutSnapshot, position: FutPosition, *, now_ms: int) -> dict:
    mid = snap.mid
    state = {
        "market": "BTC_USDT perpetual (KCEX)",
        "ts_ms": snap.ts_ms,
        "mid": _r(mid, 1),
        "spread_bps": _r(snap.spread_bps, 3),
        "imbalance": _r(snap.imbalance, 3),
        "depth_bps": snap.depth_bps,
        "returns_bps": {k: _r(v) for k, v in snap.returns_bps.items()},
        "flow": {k: {kk: (_r(vv, 1) if vv is not None else None) for kk, vv in v.items()} for k, v in snap.flow.items()},
        "funding_rate": snap.funding_rate,
        "fair_minus_last_bps": _r((snap.fair - snap.last) / snap.last * 10_000) if snap.fair > 0 and snap.last > 0 else 0.0,
    }
    if position.is_open() and mid > 0:
        sign = 1 if position.side == "long" else -1
        state["position"] = {
            "side": position.side,
            "seconds_open": max(0, (now_ms - position.opened_ms) // 1000),
            "unrealized_bps": _r(sign * (mid - position.entry) / position.entry * 10_000),
            "stop_distance_bps": _r(abs(mid - position.stop) / mid * 10_000) if position.stop else None,
            "liq_distance_bps": _r(abs(mid - position.liq) / mid * 10_000) if position.liq else None,
        }
    else:
        state["position"] = "flat"
    return state


def jev_side(verdict: JevVerdict) -> str | None:
    return {"up": "long", "down": "short"}.get(verdict.direction)


def should_wake(verdict: JevVerdict, position: FutPosition, *, threshold: float) -> str | None:
    if verdict.error:
        return None
    side = jev_side(verdict)
    if not position.is_open():
        if side and verdict.direction_conf >= threshold and verdict.beats_cost >= threshold:
            return "entry_signal"
        return None
    if verdict.exit_now is not None and verdict.exit_now >= threshold:
        return "exit_signal"
    if side and side != position.side and verdict.direction_conf >= threshold:
        return "reversal_signal"
    return None
```

- [ ] **Step 5: Implement `fut/jev.py`**

```python
"""Jev (TypeSafe System One) as the fast trigger, plus a deterministic stand-in.

Sessions run with MockJev are marked `model="mock"` and never count toward the edge criterion.
"""

from __future__ import annotations

import math
import time

from fut.questions import build_questions, jev_state
from fut.settings import FutSettings
from fut.types import FutPosition, FutSnapshot, JevVerdict


class JevClient:
    def __init__(self, settings: FutSettings, *, client=None):
        self.settings = settings
        self.name = settings.jev_model
        if client is None:
            from typesafe_sdk import RetryPolicy, TypeSafeClient

            client = TypeSafeClient(api_key=settings.typesafe_api_key, model=settings.jev_model,
                                    retry=RetryPolicy(max_retries=0), timeout=settings.jev_timeout_s)
        self.client = client

    def evaluate(self, snap: FutSnapshot, position: FutPosition, *, now_ms: int) -> JevVerdict:
        state = jev_state(snap, position, now_ms=now_ms)
        questions = build_questions(has_position=position.is_open(), move_cost_bps=self.settings.move_cost_bps)
        started = time.monotonic()
        try:
            r = self.client.system_one(state, questions)
            direction, regime = r.choices["direction_60s"], r.choices["regime"]
            exit_now = float(r.nouls["exit_now"].noul) if "exit_now" in questions else None
            return JevVerdict(
                direction=str(direction.choice), direction_conf=float(direction.confidence),
                beats_cost=float(r.nouls["move_beats_cost"].noul), flow_aligned=float(r.nouls["flow_aligned"].noul),
                regime=str(regime.choice), exit_now=exit_now,
                latency_ms=int((time.monotonic() - started) * 1000),
                input_tokens=int(getattr(r.usage, "input_tokens", 0) or 0), model=str(r.model), state=state)
        except Exception as exc:  # noqa: BLE001 - any failure means "do not wake the LLM", named in the audit
            return JevVerdict.failed(f"{type(exc).__name__}: {exc}"[:200],
                                     latency_ms=int((time.monotonic() - started) * 1000),
                                     model=self.name, state=state)


class MockJev:
    name = "mock"

    def __init__(self, settings: FutSettings):
        self.settings = settings

    def evaluate(self, snap: FutSnapshot, position: FutPosition, *, now_ms: int) -> JevVerdict:
        state = jev_state(snap, position, now_ms=now_ms)
        f30 = snap.flow.get("30s", {})
        volume = (f30.get("buy") or 0) + (f30.get("sell") or 0)
        flow = (f30.get("cvd") or 0) / volume if volume else 0.0
        r10 = snap.returns_bps.get("10s", 0.0)
        signal = max(-50.0, min(50.0, r10 / 2 + snap.imbalance * 1.5 + flow * 2))
        p_up = 1 / (1 + math.exp(-signal))
        direction = "up" if p_up >= 0.6 else "down" if p_up <= 0.4 else "flat"
        beats = min(1.0, abs(snap.returns_bps.get("60s", 0.0)) / max(self.settings.move_cost_bps, 1e-9))
        aligned = 1.0 if (flow > 0) == (r10 > 0) else 0.0
        exit_now = None
        if position.is_open():
            exit_now = 1 - p_up if position.side == "long" else p_up
        return JevVerdict(direction, min(1.0, abs(p_up - 0.5) * 2), beats, aligned, "range", exit_now,
                          0, 0, "mock", state=state)


def make_jev(settings: FutSettings):
    return MockJev(settings) if settings.uses_mock_jev else JevClient(settings)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./scripts/test tests/fut/test_fut_jev.py`
Expected: 8 passed

- [ ] **Step 7: Commit**

```bash
git add requirements.txt fut/questions.py fut/jev.py tests/fut/test_fut_jev.py
git commit -m "feat(fut): Jev questions, wake rule, typesafe-sdk client and mock stand-in" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---
### Task 8: LLM decision with durable budget

**Files:**
- Create: `fut/llm.py`
- Test: `tests/fut/test_fut_llm.py`

**Interfaces:**
- Consumes from `bot.brain`: `Budget` (`day`, `cap_usd`, `remaining()`, `is_valid()`, `spend(usd)`, `settle(delta)`, `blocked_reason`), `FENCE`, `_first_json_object`, `_cost_from(payload, settings)`, `_cost_from_http_error(resp)`, and constants `REASON_OK, REASON_BUDGET, REASON_BUDGET_STATE, REASON_CONFIG, REASON_TIMEOUT, REASON_NETWORK, REASON_BAD_RESPONSE, REASON_EMPTY, REASON_TRUNCATED, REASON_PARSE, REASON_INVALID_RESERVE, REASON_SETTLEMENT_FAILED`. From `bot.store`: `Store.reserve_budget(today=, cap_usd=, reserve_usd=)`, `Store.settle_budget(day=, delta_usd=)`. `FutSettings` (`llm` is a `bot.settings.Settings`, plus `llm_timeout_s`, `llm_reasoning`), `FutIntent`.
- Produces: `SYSTEM: str`; `REASON_INVALID_ACTION = "llm_invalid_action"`; `LlmDecision(intent, reason, cost_usd=0.0, cost_source="none", http_status=None, model="", raw=None, request=None, latency_ms=0)` with `as_audit() -> dict`; `request_body(state, llm, *, reasoning) -> dict`; `parse_action(text) -> FutIntent | None`; `valid_for_position(intent, has_position) -> bool`; `decide(state, *, has_position, settings, budget, store, http_post=None, clock=time.monotonic) -> LlmDecision`.

- [ ] **Step 1: Write the failing tests**

Create `tests/fut/test_fut_llm.py`:

```python
import json
from dataclasses import replace

import pytest
import requests

from bot.brain import Budget
from bot.settings import Settings
from fut.llm import REASON_INVALID_ACTION, decide, parse_action, request_body
from fut.settings import FutSettings
from fut.store import FutStore
from fut.types import FutIntent

STATE = {"mid": 76000.0, "position": "flat"}


def settings(**kwargs):
    llm = replace(Settings.from_env(), openrouter_api_key="key", llm_model="deepseek/x",
                  llm_fallback_cost_usd=0.02)
    return FutSettings(llm=llm, **kwargs)


def budget(cap=1.0, spent=0.0):
    return Budget(spent_usd=spent, cap_usd=cap, day="2026-09-17")


class Resp:
    def __init__(self, payload=None, status=200, bad_json=False):
        self.status_code, self._payload, self._bad = status, payload, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._payload


def completion(content, cost=0.001, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}], "usage": {"cost": cost}}


class Post:
    def __init__(self, resp=None, exc=None, on_call=None):
        self.resp, self.exc, self.on_call, self.calls = resp, exc, on_call, []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.on_call:
            self.on_call()
        if self.exc:
            raise self.exc
        return self.resp


def test_parse_action_variants():
    assert parse_action('{"action":"long","confidence":0.7,"reason":"flow"}') == FutIntent("LONG", 0.7, "flow")
    assert parse_action('```json\n{"action":"CLOSE","confidence":2,"reason":"x"}\n```') == FutIntent("CLOSE", 1.0, "x")
    assert parse_action('{"action":"HOLD","confidence":0.1,"reason":"x"} trailing "}') == FutIntent("HOLD", 0.1, "x")
    assert parse_action('{"action":"BUY","confidence":0.7}') is None
    assert parse_action('{"action":"LONG","confidence":"NaN"}') is None
    assert parse_action("garbage") is None
    assert parse_action(None) is None


def test_request_body_disables_reasoning_by_default():
    body = request_body(STATE, settings().llm, reasoning=False)
    assert body["reasoning"] == {"enabled": False}
    assert body["usage"] == {"include": True}
    assert json.loads(body["messages"][1]["content"]) == STATE
    assert "reasoning" not in request_body(STATE, settings().llm, reasoning=True)


def test_ok_long_when_flat_settles_real_cost():
    post = Post(Resp(completion('{"action":"LONG","confidence":0.7,"reason":"flow"}')))
    b = budget()
    d = decide(STATE, has_position=False, settings=settings(), budget=b, store=None, http_post=post)
    assert (d.reason, d.intent, d.cost_usd, d.cost_source) == ("ok", FutIntent("LONG", 0.7, "flow"), 0.001, "usage")
    assert b.spent_usd == pytest.approx(0.001)
    assert post.calls[0]["timeout"] == 8.0
    assert post.calls[0]["url"].endswith("/chat/completions")
    assert d.request == post.calls[0]["json"]
    assert "Authorization" not in json.dumps(d.as_audit())


@pytest.mark.parametrize("content, has_position", [
    ('{"action":"LONG","confidence":0.7,"reason":"x"}', True),
    ('{"action":"SHORT","confidence":0.7,"reason":"x"}', True),
    ('{"action":"CLOSE","confidence":0.7,"reason":"x"}', False),
])
def test_action_invalid_for_position_is_refused_but_still_charged(content, has_position):
    b = budget()
    d = decide(STATE, has_position=has_position, settings=settings(), budget=b, store=None,
               http_post=Post(Resp(completion(content))))
    assert (d.reason, d.intent) == (REASON_INVALID_ACTION, None)
    assert b.spent_usd == pytest.approx(0.001)


def test_timeout_keeps_the_reservation_as_charge():
    b = budget()
    d = decide(STATE, has_position=False, settings=settings(), budget=b, store=None,
               http_post=Post(exc=requests.Timeout("slow")))
    assert (d.reason, d.cost_usd, d.cost_source) == ("llm_timeout", 0.02, "fallback_uncertain")
    assert b.spent_usd == pytest.approx(0.02)


def test_http_error_without_usage_keeps_reservation():
    d = decide(STATE, has_position=False, settings=settings(), budget=budget(), store=None,
               http_post=Post(Resp({"error": "x"}, status=500)))
    assert (d.reason, d.http_status, d.cost_source) == ("llm_http_500", 500, "fallback_uncertain")


def test_truncated_completion_is_named():
    d = decide(STATE, has_position=False, settings=settings(), budget=budget(), store=None,
               http_post=Post(Resp(completion("", finish="length"))))
    assert d.reason == "llm_truncated"


def test_missing_key_and_exhausted_budget_never_post():
    post = Post(Resp(completion("{}")))
    no_key = FutSettings(llm=replace(Settings.from_env(), openrouter_api_key="", llm_model="x"))
    assert decide(STATE, has_position=False, settings=no_key, budget=budget(), store=None, http_post=post).reason == "llm_config"
    assert decide(STATE, has_position=False, settings=settings(), budget=budget(cap=0.01, spent=0.01),
                  store=None, http_post=post).reason == "llm_budget"
    assert post.calls == []


def test_reservation_is_durable_before_the_request(tmp_path):
    store = FutStore(tmp_path / "fut.db")
    seen = {}
    post = Post(Resp(completion('{"action":"HOLD","confidence":0.2,"reason":"x"}')),
                on_call=lambda: seen.update(store.budget_load()))
    d = decide(STATE, has_position=False, settings=settings(), budget=budget(), store=store, http_post=post)
    assert d.reason == "ok"
    assert seen["spent_usd"] == pytest.approx(0.02)
    assert store.budget_load()["spent_usd"] == pytest.approx(0.001)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./scripts/test tests/fut/test_fut_llm.py`
Expected: `ModuleNotFoundError: No module named 'fut.llm'`

- [ ] **Step 3: Implement `fut/llm.py`**

```python
"""The LLM has the final word on LONG/SHORT/CLOSE/HOLD. Code owns size, stop and limits.

Budget handling mirrors bot.brain.think_result: reserve durably BEFORE the HTTP dispatch,
settle to the real cost after, keep the reservation as the charge when the outcome is unknown.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from bot.brain import (
    FENCE,
    REASON_BAD_RESPONSE,
    REASON_BUDGET,
    REASON_BUDGET_STATE,
    REASON_CONFIG,
    REASON_EMPTY,
    REASON_INVALID_RESERVE,
    REASON_NETWORK,
    REASON_OK,
    REASON_PARSE,
    REASON_SETTLEMENT_FAILED,
    REASON_TIMEOUT,
    REASON_TRUNCATED,
    Budget,
    _cost_from,
    _cost_from_http_error,
    _first_json_object,
)
from bot.settings import Settings
from fut.settings import FutSettings
from fut.types import FutIntent

log = logging.getLogger(__name__)

ACTIONS = {"LONG", "SHORT", "CLOSE", "HOLD"}
REASON_INVALID_ACTION = "llm_invalid_action"
SYSTEM = (
    "You are a BTC_USDT perpetual futures decision module on a seconds horizon. "
    "Reply with JSON only: action (LONG|SHORT|CLOSE|HOLD), confidence (0-1), reason (<=240 chars). "
    "LONG or SHORT only when position is flat; CLOSE only with an open position. "
    "Do not output size, stop or price. A round trip costs about 2 bps plus slippage. HOLD if unsure."
)


@dataclass
class LlmDecision:
    intent: FutIntent | None
    reason: str
    cost_usd: float = 0.0
    cost_source: str = "none"
    http_status: int | None = None
    model: str = ""
    raw: str | None = None
    request: dict[str, Any] | None = None
    latency_ms: int = 0

    def as_audit(self) -> dict[str, Any]:
        return {"reason": self.reason, "cost_usd": round(self.cost_usd, 8), "cost_source": self.cost_source,
                "http_status": self.http_status, "model": self.model, "raw": self.raw,
                "request": self.request, "latency_ms": self.latency_ms}


def request_body(state: dict[str, Any], llm: Settings, *, reasoning: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": llm.llm_model,
        "temperature": 0,
        "max_tokens": llm.llm_max_tokens,
        "usage": {"include": True},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(state, separators=(",", ":"), default=str)},
        ],
    }
    if not reasoning:
        body["reasoning"] = {"enabled": False}
    if llm.llm_json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def parse_action(text: str | None) -> FutIntent | None:
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    fenced = FENCE.search(raw)
    if fenced:
        raw = fenced.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sliced = _first_json_object(raw)
        if sliced is None:
            return None
        try:
            data = json.loads(sliced)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action", "")).strip().upper()
    if action not in ACTIONS:
        return None
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence):
        return None
    return FutIntent(action, min(max(confidence, 0.0), 1.0), str(data.get("reason", ""))[:240])


def valid_for_position(intent: FutIntent, has_position: bool) -> bool:
    if intent.action in ("LONG", "SHORT"):
        return not has_position
    if intent.action == "CLOSE":
        return has_position
    return True


def decide(state: dict[str, Any], *, has_position: bool, settings: FutSettings, budget: Budget, store,
           http_post: Callable[..., Any] | None = None, clock: Callable[[], float] = time.monotonic) -> LlmDecision:
    llm = settings.llm
    model = llm.llm_model
    started = clock()

    def done(intent, reason, cost=0.0, source="none", status=None, raw=None, request=None) -> LlmDecision:
        return LlmDecision(intent, reason, cost, source, status, model, raw, request,
                           int((clock() - started) * 1000))

    if budget.blocked_reason:
        return done(None, budget.blocked_reason)
    if not budget.is_valid():
        return done(None, REASON_BUDGET_STATE)
    if budget.remaining() <= 0:
        return done(None, REASON_BUDGET)
    if not llm.openrouter_api_key or not model:
        return done(None, REASON_CONFIG)
    reserve = llm.llm_fallback_cost_usd
    if not math.isfinite(reserve) or reserve <= 0:
        return done(None, REASON_INVALID_RESERVE)

    reserved_durably = False
    if store is not None:
        try:
            admitted = store.reserve_budget(today=budget.day, cap_usd=budget.cap_usd, reserve_usd=reserve)
        except Exception as exc:  # noqa: BLE001 - untrustworthy budget state blocks spend
            log.error("budget state untrustworthy, refusing LLM spend: %s", exc)
            return done(None, REASON_BUDGET_STATE)
        if not admitted:
            return done(None, REASON_BUDGET)
        reserved_durably = True
    elif reserve > budget.remaining() + 1e-9:
        return done(None, REASON_BUDGET)
    budget.spend(reserve)

    def settle(actual: float) -> None:
        delta = actual - reserve
        budget.settle(delta)
        if reserved_durably:
            try:
                store.settle_budget(day=budget.day, delta_usd=delta)
            except Exception as exc:  # noqa: BLE001 - ledger may understate spend; block further calls
                log.error("budget settlement failed; blocking further LLM spend: %s", exc)
                budget.blocked_reason = REASON_SETTLEMENT_FAILED

    body = request_body(state, llm, reasoning=settings.llm_reasoning)
    post = http_post or requests.post
    headers = {"Authorization": f"Bearer {llm.openrouter_api_key}", "Content-Type": "application/json"}
    try:
        resp = post(f"{llm.openrouter_base_url}/chat/completions", headers=headers, json=body,
                    timeout=settings.llm_timeout_s)
    except requests.Timeout:
        return done(None, REASON_TIMEOUT, reserve, "fallback_uncertain", request=body)
    except Exception as exc:  # noqa: BLE001 - network layer, named in the audit
        log.warning("llm network error: %s: %s", type(exc).__name__, exc)
        return done(None, REASON_NETWORK, reserve, "fallback_uncertain", request=body)

    status = getattr(resp, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status is not None and status >= 400:
        real = _cost_from_http_error(resp)
        if real is not None:
            settle(real)
            return done(None, f"llm_http_{status}", real, "usage", status, request=body)
        return done(None, f"llm_http_{status}", reserve, "fallback_uncertain", status, request=body)
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return done(None, REASON_BAD_RESPONSE, reserve, "fallback_uncertain", status, request=body)

    cost, source = _cost_from(payload, llm)
    settle(cost)
    try:
        choice = payload["choices"][0]
        text = choice["message"]["content"]
    except Exception:  # noqa: BLE001
        return done(None, REASON_BAD_RESPONSE, cost, source, status, request=body)
    if not isinstance(text, str) or not text.strip():
        reason = REASON_TRUNCATED if choice.get("finish_reason") == "length" else REASON_EMPTY
        return done(None, reason, cost, source, status, request=body)
    intent = parse_action(text)
    if intent is None:
        return done(None, REASON_PARSE, cost, source, status, raw=text[:500], request=body)
    if not valid_for_position(intent, has_position):
        return done(None, REASON_INVALID_ACTION, cost, source, status, raw=text[:500], request=body)
    return done(intent, REASON_OK, cost, source, status, raw=text[:500], request=body)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./scripts/test tests/fut/test_fut_llm.py`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add fut/llm.py tests/fut/test_fut_llm.py
git commit -m "feat(fut): LLM LONG/SHORT/CLOSE/HOLD decision with durable budget reservation" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Dispatcher (immediate call, one in flight, cooldown, stale discard)

**Files:**
- Create: `fut/dispatch.py`
- Test: `tests/fut/test_fut_dispatch.py`

**Interfaces:**
- Consumes: `FutSettings` (`llm_cooldown_s`, `llm_timeout_s`, `stale_price_bps`), `LlmDecision`.
- Produces: `Trigger(kind, side, ts_ms, mid, state)` frozen; `Resolution(trigger, decision, verdict, elapsed_ms)` frozen; `Dispatcher(settings, *, run_llm, clock_ms, submit=None)` with property `busy`, `offer(trigger, *, budget_ok) -> "dispatched" | "suppressed_inflight" | "suppressed_budget" | "suppressed_cooldown"`, `poll(*, mid_now) -> Resolution | None` with `verdict in {"ok", "stale_timeout", "stale_price"}`. `submit(fn, *args)` must return a `concurrent.futures.Future`; default is a 1-worker `ThreadPoolExecutor.submit`.

- [ ] **Step 1: Write the failing tests**

Create `tests/fut/test_fut_dispatch.py`:

```python
from concurrent.futures import Future

from fut.dispatch import Dispatcher, Trigger
from fut.llm import LlmDecision
from fut.settings import FutSettings
from fut.types import FutIntent


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class ManualSubmit:
    def __init__(self):
        self.jobs = []

    def __call__(self, fn, *args):
        future = Future()
        self.jobs.append((future, fn, args))
        return future

    def finish(self):
        future, fn, args = self.jobs.pop(0)
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)


def trig(kind="entry_signal", side="long", mid=100.0):
    return Trigger(kind, side, 0, mid, {})


def make(action="LONG", run=None, **settings):
    clock, submit = Clock(), ManualSubmit()

    def default_run(trigger):
        return LlmDecision(intent=FutIntent(action, 0.7, "r"), reason="ok")

    return Dispatcher(FutSettings(**settings), run_llm=run or default_run, clock_ms=clock, submit=submit), clock, submit


def cycle(d, clock, submit, trigger, at, mid_now=None):
    clock.now = at
    assert d.offer(trigger, budget_ok=True) == "dispatched"
    submit.finish()
    return d.poll(mid_now=trigger.mid if mid_now is None else mid_now)


def test_dispatches_immediately_and_resolves():
    d, clock, submit = make()
    assert d.offer(trig(), budget_ok=True) == "dispatched"
    assert d.busy and d.poll(mid_now=100.0) is None
    submit.finish()
    clock.now = 500
    res = d.poll(mid_now=100.0)
    assert (res.verdict, res.elapsed_ms, res.decision.intent.action) == ("ok", 500, "LONG")
    assert not d.busy


def test_one_call_in_flight_and_budget_gate():
    d, _, submit = make()
    assert d.offer(trig(), budget_ok=False) == "suppressed_budget"
    assert submit.jobs == []
    d.offer(trig(), budget_ok=True)
    assert d.offer(trig(side="short"), budget_ok=True) == "suppressed_inflight"
    assert len(submit.jobs) == 1


def test_hold_arms_cooldown_for_same_kind_and_side_only():
    d, clock, submit = make("HOLD")
    cycle(d, clock, submit, trig(), at=0)
    clock.now = 5000
    assert d.offer(trig(), budget_ok=True) == "suppressed_cooldown"
    assert d.offer(trig(side="short"), budget_ok=True) == "dispatched"


def test_cooldown_expires():
    d, clock, submit = make("HOLD")
    cycle(d, clock, submit, trig(), at=0)
    clock.now = 10_001
    assert d.offer(trig(), budget_ok=True) == "dispatched"


def test_exit_signal_is_never_cooled_down():
    d, clock, submit = make("HOLD")
    cycle(d, clock, submit, trig("exit_signal", None), at=0)
    clock.now = 1000
    assert d.offer(trig("exit_signal", None), budget_ok=True) == "dispatched"


def test_late_entry_is_stale_timeout():
    d, clock, submit = make("LONG")
    d.offer(trig(), budget_ok=True)
    submit.finish()
    clock.now = 9000
    assert d.poll(mid_now=100.0).verdict == "stale_timeout"


def test_entry_after_price_moved_is_stale_price():
    d, clock, submit = make("SHORT")
    assert cycle(d, clock, submit, trig(side="short"), at=0, mid_now=100.06).verdict == "stale_price"
    assert cycle(d, clock, submit, trig(side="short"), at=1, mid_now=100.04).verdict == "ok"


def test_close_is_never_discarded_for_latency_or_price():
    d, clock, submit = make("CLOSE")
    d.offer(trig("exit_signal", None), budget_ok=True)
    submit.finish()
    clock.now = 60_000
    assert d.poll(mid_now=150.0).verdict == "ok"


def test_crashing_llm_becomes_a_named_non_decision_and_arms_cooldown():
    def boom(trigger):
        raise RuntimeError("bug")

    d, clock, submit = make(run=boom)
    res = cycle(d, clock, submit, trig(), at=0)
    assert res.decision.intent is None and res.decision.reason == "llm_crash:RuntimeError"
    clock.now = 100
    assert d.offer(trig(), budget_ok=True) == "suppressed_cooldown"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./scripts/test tests/fut/test_fut_dispatch.py`
Expected: `ModuleNotFoundError: No module named 'fut.dispatch'`

- [ ] **Step 3: Implement `fut/dispatch.py`**

```python
"""When Jev fires, call the LLM now; never two calls at once; never discard an exit."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from fut.llm import LlmDecision
from fut.settings import FutSettings

ENTRY_ACTIONS = ("LONG", "SHORT")


@dataclass(frozen=True)
class Trigger:
    kind: str  # entry_signal | exit_signal | reversal_signal
    side: str | None
    ts_ms: int
    mid: float
    state: dict[str, Any]


@dataclass(frozen=True)
class Resolution:
    trigger: Trigger
    decision: LlmDecision
    verdict: str  # ok | stale_timeout | stale_price
    elapsed_ms: int


class Dispatcher:
    def __init__(self, settings: FutSettings, *, run_llm: Callable[[Trigger], LlmDecision],
                 clock_ms: Callable[[], int], submit: Callable[..., Any] | None = None):
        self.settings = settings
        self._run = run_llm
        self._clock = clock_ms
        if submit is None:
            submit = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fut-llm").submit
        self._submit = submit
        self._future = None
        self._trigger: Trigger | None = None
        self._started_ms = 0
        self._last_hold: tuple[Trigger, int] | None = None

    @property
    def busy(self) -> bool:
        return self._future is not None

    def offer(self, trigger: Trigger, *, budget_ok: bool) -> str:
        now = self._clock()
        if self._future is not None:
            return "suppressed_inflight"
        if not budget_ok:
            return "suppressed_budget"
        if self._last_hold is not None and trigger.kind != "exit_signal":
            held, at = self._last_hold
            same = trigger.kind == held.kind and trigger.side == held.side
            if same and now - at < self.settings.llm_cooldown_s * 1000:
                return "suppressed_cooldown"
        self._future = self._submit(self._run, trigger)
        self._trigger = trigger
        self._started_ms = now
        return "dispatched"

    def poll(self, *, mid_now: float) -> Resolution | None:
        if self._future is None or not self._future.done():
            return None
        future, trigger = self._future, self._trigger
        self._future, self._trigger = None, None
        now = self._clock()
        elapsed = now - self._started_ms
        try:
            decision = future.result()
        except Exception as exc:  # noqa: BLE001 - a crashing worker is a non-decision, never a trade
            decision = LlmDecision(intent=None, reason=f"llm_crash:{type(exc).__name__}")

        verdict = "ok"
        if decision.intent is not None and decision.intent.action in ENTRY_ACTIONS:
            if elapsed > self.settings.llm_timeout_s * 1000:
                verdict = "stale_timeout"
            elif trigger.mid > 0 and mid_now > 0 and \
                    abs(mid_now - trigger.mid) / trigger.mid * 10_000 > self.settings.stale_price_bps:
                verdict = "stale_price"

        if decision.intent is None or decision.intent.action == "HOLD":
            self._last_hold = (trigger, now)
        else:
            self._last_hold = None
        return Resolution(trigger, decision, verdict, elapsed)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./scripts/test tests/fut/test_fut_dispatch.py`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add fut/dispatch.py tests/fut/test_fut_dispatch.py
git commit -m "feat(fut): LLM dispatcher with one call in flight, HOLD cooldown and entry-only stale discard" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---
### Task 10: Shadow baselines

**Files:**
- Create: `fut/shadow.py`
- Test: `tests/fut/test_fut_shadow.py`

**Interfaces:**
- Consumes: `PaperLedger`, `FutStore.day_net`, `day_of`, `collar.check`, `jev_side`, `FutIntent`, `FutSnapshot`, `JevVerdict`, `FutSettings`, `ContractSpec`.
- Produces: `SHADOW_BOOKS = ("shadow:jev_only", "shadow:random")`; `ShadowBooks(store, settings, spec, *, rng=None)` with attribute `ledgers: dict[str, PaperLedger]` keyed `"jev_only"`, `"random"`; `set_spec(spec)`; `mark(snap, *, now_ms) -> dict[str, str]`; `on_jev(verdict, snap, *, now_ms, wake, entry_rate) -> dict[str, str]`. `flat` has no ledger (its net is 0 by definition).

- [ ] **Step 1: Write the failing tests**

Create `tests/fut/test_fut_shadow.py`:

```python
import random

from fut.ledger import PaperLedger
from fut.settings import FutSettings
from fut.shadow import ShadowBooks
from fut.store import FutStore
from fut.types import JevVerdict
from tests.fut.helpers import SPEC, make_snap


def verdict(direction="up"):
    return JevVerdict(direction, 0.9, 0.9, 0.5, "trend", None, 100, 1000, "jev-1")


def books(tmp_path, seed=7):
    store = FutStore(tmp_path / "fut.db")
    return store, ShadowBooks(store, FutSettings(), SPEC, rng=random.Random(seed))


def test_jev_only_opens_on_entry_signal_and_closes_on_exit_signal(tmp_path):
    store, shadow = books(tmp_path)
    out = shadow.on_jev(verdict("down"), make_snap(), now_ms=1000, wake="entry_signal", entry_rate=0.0)
    assert out["jev_only"] == "opened"
    assert shadow.ledgers["jev_only"].position.side == "short"
    out = shadow.on_jev(verdict(), make_snap(), now_ms=2000, wake="exit_signal", entry_rate=0.0)
    assert out["jev_only"] == "closed"
    assert [f["kind"] for f in store.fut_fills("shadow:jev_only")] == ["open", "close"]


def test_jev_only_reports_collar_refusals(tmp_path):
    _, shadow = books(tmp_path)
    out = shadow.on_jev(verdict(), make_snap(stale=True), now_ms=1000, wake="entry_signal", entry_rate=0.0)
    assert out["jev_only"] == "stale"


def test_random_book_follows_entry_rate(tmp_path):
    _, never = books(tmp_path / "a")
    for t in range(20):
        never.on_jev(verdict(), make_snap(), now_ms=t, wake=None, entry_rate=0.0)
    assert not never.ledgers["random"].position.is_open()
    _, always = books(tmp_path / "b")
    assert always.on_jev(verdict(), make_snap(), now_ms=0, wake=None, entry_rate=1.0)["random"] == "opened"


def test_shadow_books_never_touch_main(tmp_path):
    store, shadow = books(tmp_path)
    shadow.on_jev(verdict(), make_snap(), now_ms=1000, wake="entry_signal", entry_rate=1.0)
    main = PaperLedger(store, FutSettings(), SPEC)
    assert not main.position.is_open() and main.balance == 450.0
    assert store.fut_fills("main") == []


def test_mark_runs_stops_on_shadow_books(tmp_path):
    _, shadow = books(tmp_path)
    shadow.on_jev(verdict(), make_snap(), now_ms=1000, wake="entry_signal", entry_rate=0.0)
    out = shadow.mark(make_snap(bid=70000.0, ask=70000.1, last=70000.0, fair=70000.0), now_ms=2000)
    assert out == {"jev_only": "stop"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./scripts/test tests/fut/test_fut_shadow.py`
Expected: `ModuleNotFoundError: No module named 'fut.shadow'`

- [ ] **Step 3: Implement `fut/shadow.py`**

```python
"""Baselines on the same ticks, collar and ledger as the main book.

- flat: never trades (net 0, no ledger needed)
- jev_only: trades Jev's direction whenever Jev would wake the LLM
- random: enters LONG/SHORT at random at the LLM's observed entry rate
"""

from __future__ import annotations

import random

from fut import collar
from fut.ledger import PaperLedger
from fut.questions import jev_side
from fut.settings import FutSettings
from fut.store import FutStore, day_of
from fut.types import FutIntent, FutSnapshot, JevVerdict
from kcex.fapi import ContractSpec

SHADOW_BOOKS = ("shadow:jev_only", "shadow:random")


class ShadowBooks:
    def __init__(self, store: FutStore, settings: FutSettings, spec: ContractSpec, *, rng=None):
        self.store = store
        self.settings = settings
        self.spec = spec
        self.ledgers = {
            "jev_only": PaperLedger(store, settings, spec, book="shadow:jev_only"),
            "random": PaperLedger(store, settings, spec, book="shadow:random"),
        }
        self.rng = rng or random.Random(settings.shadow_seed)

    def set_spec(self, spec: ContractSpec) -> None:
        self.spec = spec
        for ledger in self.ledgers.values():
            ledger.spec = spec

    def mark(self, snap: FutSnapshot, *, now_ms: int) -> dict[str, str]:
        out = {}
        for name, ledger in self.ledgers.items():
            reason = ledger.mark(snap, now_ms=now_ms)
            if reason:
                out[name] = reason
        return out

    def _open(self, ledger: PaperLedger, intent: FutIntent, snap: FutSnapshot, now_ms: int) -> str:
        gate = collar.check(intent, snap, position=ledger.position, balance=ledger.balance,
                            day_pnl_usdt=self.store.day_net(ledger.book, day_of(now_ms)),
                            spec=self.spec, settings=self.settings)
        if gate.ok and gate.action in ("LONG", "SHORT"):
            ledger.open(gate, now_ms=now_ms)
            return "opened"
        return gate.rule

    def on_jev(self, verdict: JevVerdict, snap: FutSnapshot, *, now_ms: int, wake: str | None,
               entry_rate: float) -> dict[str, str]:
        out: dict[str, str] = {}
        jev = self.ledgers["jev_only"]
        side = jev_side(verdict)
        if wake == "entry_signal" and side and not jev.position.is_open():
            action = "LONG" if side == "long" else "SHORT"
            out["jev_only"] = self._open(jev, FutIntent(action, verdict.direction_conf, "jev_only"), snap, now_ms)
        elif wake in ("exit_signal", "reversal_signal") and jev.position.is_open():
            price = jev.market_exit_price(snap)
            if price is not None:
                jev.close(price, now_ms=now_ms, reason="jev_signal")
                out["jev_only"] = "closed"

        rnd = self.ledgers["random"]
        draw = self.rng.random()
        pick = self.rng.choice(("LONG", "SHORT"))
        if not rnd.position.is_open() and draw < entry_rate:
            out["random"] = self._open(rnd, FutIntent(pick, 1.0, "random"), snap, now_ms)
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./scripts/test tests/fut/test_fut_shadow.py`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add fut/shadow.py tests/fut/test_fut_shadow.py
git commit -m "feat(fut): shadow baselines (jev_only, random) on the same ticks and collar" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Report and edge criterion

**Files:**
- Create: `fut/report.py`
- Test: `tests/fut/test_fut_report.py`

**Interfaces:**
- Consumes: `FutStore.fut_fills`, `FutStore.decisions`, `FutStore.model_cost_between`, `day_bounds_ms`, `FutSettings.max_day_loss_usdt`.
- Produces: `EdgeCriterion(min_trades=200, min_days=14.0)` frozen; `book_totals(store, book) -> dict` (keys `trades` list of `{open_ms, close_ms, side, reason, pnl, fees, funding, net}`, `gross_usd`, `fees_usd`, `funding_usd`, `net`, `daily`); `bootstrap_ci(values, *, n=10_000, seed=0, alpha=0.05) -> tuple[float, float]`; `summarize(store, settings) -> dict`; `evaluate(summary, settings, criterion=EdgeCriterion()) -> {"passed": bool, "checks": dict[str, bool]}`; `render(summary, verdict) -> str`. Check names: `min_trades, min_days, real_jev_only, net_positive, beats_flat, beats_jev_only, beats_random, ci_lower_positive, day_loss_ok`.

- [ ] **Step 1: Write the failing tests**

Create `tests/fut/test_fut_report.py`:

```python
import pytest

from fut.report import EdgeCriterion, book_totals, bootstrap_ci, evaluate, render, summarize
from fut.settings import FutSettings
from fut.store import FutStore

DAY_MS = 86_400_000
T0 = 1_789_000_000_000


def trade(store, book, t, pnl, fee=0.001, funding=0.0):
    store.add_fut_fill(book, ts_ms=t, kind="open", side="long", contracts=2, price=76000.0, fee=fee,
                       funding=0.0, pnl=0.0, reason="entry")
    if funding:
        store.add_fut_fill(book, ts_ms=t + 1, kind="funding", side="long", contracts=2, price=76000.0,
                           fee=0.0, funding=funding, pnl=0.0, reason="funding")
    store.add_fut_fill(book, ts_ms=t + 2, kind="close", side="long", contracts=2, price=76010.0, fee=fee,
                       funding=0.0, pnl=pnl, reason="stop")


def build(tmp_path, *, n=200, days=15, main_pnl=0.05, model="jev-1.13.0", shadow_pnl=-0.01, jev_cost=0.001):
    store = FutStore(tmp_path / "fut.db")
    step = days * DAY_MS // n
    for i in range(n):
        t = T0 + i * step
        trade(store, "main", t, main_pnl + (0.001 if i % 2 else -0.001))
        trade(store, "shadow:jev_only", t, shadow_pnl)
        trade(store, "shadow:random", t, shadow_pnl)
    store.log_decision("jev", {"model": model, "cost_usd": jev_cost}, ts_ms=T0)
    store.log_decision("llm", {"cost_usd": 0.002, "verdict": "ok", "elapsed_ms": 900}, ts_ms=T0 + days * DAY_MS)
    return store


def test_book_totals_pairs_trades_and_attributes_funding(tmp_path):
    store = FutStore(tmp_path / "fut.db")
    trade(store, "main", T0, 0.05, funding=0.002)
    totals = book_totals(store, "main")
    (t,) = totals["trades"]
    assert (t["pnl"], t["fees"], t["funding"]) == (0.05, pytest.approx(0.002), 0.002)
    assert t["net"] == pytest.approx(0.046)
    assert totals["net"] == pytest.approx(0.046)


def test_bootstrap_ci_of_a_constant_is_that_constant():
    assert bootstrap_ci([0.5] * 50, n=200) == (pytest.approx(0.5), pytest.approx(0.5))


def test_strong_synthetic_run_passes_every_check(tmp_path):
    store = build(tmp_path)
    summary = summarize(store, FutSettings())
    verdict = evaluate(summary, FutSettings())
    assert verdict["checks"] == {k: True for k in verdict["checks"]}
    assert verdict["passed"] is True
    assert summary["n_trades"] == 200 and summary["days"] >= 14
    assert "PASSED" in render(summary, verdict)


@pytest.mark.parametrize("kwargs, failing", [
    (dict(n=150), "min_trades"),
    (dict(days=10), "min_days"),
    (dict(model="mock"), "real_jev_only"),
    (dict(main_pnl=-0.05), "net_positive"),
    (dict(shadow_pnl=0.5), "beats_random"),
])
def test_each_check_can_fail(tmp_path, kwargs, failing):
    store = build(tmp_path, **kwargs)
    verdict = evaluate(summarize(store, FutSettings()), FutSettings())
    assert verdict["checks"][failing] is False
    assert verdict["passed"] is False


def test_day_loss_check_uses_daily_net_after_model_cost(tmp_path):
    store = build(tmp_path)
    store.log_decision("llm", {"cost_usd": 25.0}, ts_ms=T0 + DAY_MS // 2)
    verdict = evaluate(summarize(store, FutSettings()), FutSettings(max_day_loss_usdt=20.0))
    assert verdict["checks"]["day_loss_ok"] is False


def test_empty_database_fails_cleanly(tmp_path):
    store = FutStore(tmp_path / "fut.db")
    verdict = evaluate(summarize(store, FutSettings()), FutSettings(), EdgeCriterion())
    assert verdict["passed"] is False
    assert "FAILED" in render(summarize(store, FutSettings()), verdict)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./scripts/test tests/fut/test_fut_report.py`
Expected: `ModuleNotFoundError: No module named 'fut.report'`

- [ ] **Step 3: Implement `fut/report.py`**

```python
"""Futures paper report and the fixed edge criterion. Read-only over the store."""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import mean

from fut.settings import FutSettings
from fut.store import FutStore, day_bounds_ms, day_of

BOOKS = ("main", "shadow:jev_only", "shadow:random")


@dataclass(frozen=True)
class EdgeCriterion:
    min_trades: int = 200
    min_days: float = 14.0


def book_totals(store: FutStore, book: str) -> dict:
    fills = store.fut_fills(book)
    trades, current = [], None
    daily: dict[str, float] = defaultdict(float)
    for f in fills:
        daily[f["day"]] += f["pnl"] - f["fee"] - f["funding"]
        if f["kind"] == "open":
            current = {"open_ms": f["ts_ms"], "side": f["side"], "fees": f["fee"], "funding": 0.0}
        elif f["kind"] == "funding" and current is not None:
            current["funding"] += f["funding"]
        elif f["kind"] == "close" and current is not None:
            current.update(close_ms=f["ts_ms"], reason=f["reason"], pnl=f["pnl"])
            current["fees"] += f["fee"]
            current["net"] = current["pnl"] - current["fees"] - current["funding"]
            trades.append(current)
            current = None
    gross = sum(f["pnl"] for f in fills)
    fees = sum(f["fee"] for f in fills)
    funding = sum(f["funding"] for f in fills)
    return {"trades": trades, "gross_usd": gross, "fees_usd": fees, "funding_usd": funding,
            "net": gross - fees - funding, "daily": dict(daily)}


def bootstrap_ci(values, *, n: int = 10_000, seed: int = 0, alpha: float = 0.05) -> tuple[float, float]:
    values = list(values)
    rng = random.Random(seed)
    means = sorted(mean(rng.choices(values, k=len(values))) for _ in range(n))
    lo = means[int(alpha / 2 * (n - 1))]
    hi = means[int((1 - alpha / 2) * (n - 1))]
    return lo, hi


def _percentile(sorted_values, q):
    if not sorted_values:
        return None
    return sorted_values[min(len(sorted_values) - 1, int(q * (len(sorted_values) - 1)))]


def summarize(store: FutStore, settings: FutSettings) -> dict:
    decisions = store.decisions()
    jev = [d for d in decisions if d["kind"] == "jev"]
    llm = [d for d in decisions if d["kind"] == "llm"]
    jev_cost = sum(float(d["payload"].get("cost_usd") or 0.0) for d in jev)
    llm_cost = sum(float(d["payload"].get("cost_usd") or 0.0) for d in llm)
    stamps = [d["ts_ms"] for d in decisions]
    days = (max(stamps) - min(stamps)) / 86_400_000 if stamps else 0.0

    totals = {book: book_totals(store, book) for book in BOOKS}
    main_trades = totals["main"]["trades"]
    per_trade = [t["net"] - (jev_cost + llm_cost) / len(main_trades) for t in main_trades]
    daily_after_models = {}
    for day, net in totals["main"]["daily"].items():
        start, end = day_bounds_ms(day)
        daily_after_models[day] = net - store.model_cost_between(start, end)
    for d in decisions:
        if d["kind"] in ("jev", "llm"):
            day = day_of(d["ts_ms"])
            if day not in daily_after_models:
                start, end = day_bounds_ms(day)
                daily_after_models[day] = -store.model_cost_between(start, end)

    latencies = sorted(int(d["payload"].get("elapsed_ms") or 0) for d in llm)
    return {
        "days": days,
        "jev_models": sorted({str(d["payload"].get("model")) for d in jev}),
        "jev_cost_usd": jev_cost,
        "llm_cost_usd": llm_cost,
        "n_trades": len(main_trades),
        "main_net_usd": totals["main"]["net"] - jev_cost - llm_cost,
        "jev_only_net_usd": totals["shadow:jev_only"]["net"] - jev_cost,
        "random_net_usd": totals["shadow:random"]["net"],
        "flat_net_usd": 0.0,
        "per_trade_net": per_trade,
        "ci95": bootstrap_ci(per_trade) if per_trade else None,
        "daily_net_after_models": daily_after_models,
        "llm_latency_ms": {"p50": _percentile(latencies, 0.5), "p90": _percentile(latencies, 0.9),
                           "max": latencies[-1] if latencies else None},
        "llm_verdicts": dict(Counter(str(d["payload"].get("verdict")) for d in llm)),
        "dispatch": dict(Counter(str(d["payload"].get("dispatch")) for d in jev if d["payload"].get("dispatch"))),
        "books": {book: {k: v for k, v in t.items() if k != "trades"} | {"trades": len(t["trades"])}
                  for book, t in totals.items()},
    }


def evaluate(summary: dict, settings: FutSettings, criterion: EdgeCriterion = EdgeCriterion()) -> dict:
    main = summary["main_net_usd"]
    ci = summary["ci95"]
    checks = {
        "min_trades": summary["n_trades"] >= criterion.min_trades,
        "min_days": summary["days"] >= criterion.min_days,
        "real_jev_only": bool(summary["jev_models"]) and "mock" not in summary["jev_models"],
        "net_positive": main > 0,
        "beats_flat": main > summary["flat_net_usd"],
        "beats_jev_only": main > summary["jev_only_net_usd"],
        "beats_random": main > summary["random_net_usd"],
        "ci_lower_positive": ci is not None and ci[0] > 0,
        "day_loss_ok": all(v >= -abs(settings.max_day_loss_usdt) for v in summary["daily_net_after_models"].values()),
    }
    return {"passed": all(checks.values()), "checks": checks}


def render(summary: dict, verdict: dict) -> str:
    lines = [
        f"Edge criterion: {'PASSED' if verdict['passed'] else 'FAILED'}",
        f"days {summary['days']:.2f} | trades {summary['n_trades']} | jev models {summary['jev_models']}",
        f"net after models: main {summary['main_net_usd']:.6f} | jev_only {summary['jev_only_net_usd']:.6f} "
        f"| random {summary['random_net_usd']:.6f} | flat 0",
        f"costs: jev {summary['jev_cost_usd']:.6f} | llm {summary['llm_cost_usd']:.6f}",
        f"per-trade CI95: {summary['ci95']}",
        f"llm latency ms: {summary['llm_latency_ms']} | verdicts {summary['llm_verdicts']} | dispatch {summary['dispatch']}",
        "checks:",
    ]
    lines += [f"  {'ok ' if ok else 'NO '} {name}" for name, ok in verdict["checks"].items()]
    return "\n".join(lines)
```


- [ ] **Step 4: Run tests to verify they pass**

Run: `./scripts/test tests/fut/test_fut_report.py`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add fut/report.py tests/fut/test_fut_report.py
git commit -m "feat(fut): report with bootstrap CI and the fixed edge criterion" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---
### Task 12: Main loop, CLI and entry point

**Files:**
- Create: `fut/loop.py`
- Create: `fut/cli.py`
- Create: `fut/__main__.py`
- Modify: `tests/conftest.py` (`_DOTENV_ALIAS_MODULES`)
- Test: `tests/fut/test_fut_loop.py`, `tests/fut/test_fut_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1–11; `bot.cli.InstanceLock`, `AlreadyRunning`, `setup_logging(level, log_path=None)`, `add_file_logging(path)`; `bot.store.BudgetStateCorrupt`, `StoreIdentityMismatch`; `bot.brain.Budget`, `REASON_BUDGET_STATE`.
- Produces:
  - `fut.loop`: `Unmonitored(RuntimeError)`; `now_ms() -> int`; `seed_budget(store, llm, *, today) -> Budget`; `start_ws_thread(settings, events, stop, *, connect=default_connect, sleep=time.sleep) -> threading.Thread` (puts `("ws", event)` on `events`); `FutLoop(*, settings, store, spec, rest, jev, budget, events, clock_ms=now_ms, llm_decide=fut.llm.decide, submit=None, store_factory=None, rng=None)` with attributes `market`, `ledger`, `shadow`, `dispatcher`, `jev_evals`, `llm_entries`, and `step()`.
  - Decision log kinds written by the loop: `jev` (every Jev call: `model, error, latency_ms, input_tokens, cost_usd, answers, wake, dispatch, shadow, snapshot`), `llm` (every resolution: `trigger, llm, intent, verdict, elapsed_ms, mid_at_response, gate, outcome, cost_usd`), `exit` (ledger-driven exits), `unmonitored`.
  - `fut.cli`: `DB_PATH`, `LOCK_PATH`, `LOG_PATH`, `ENV_PATH`, `EXIT_OK=0`, `EXIT_ALREADY_RUNNING=3`, `EXIT_UNMONITORED=8`, `EXIT_STORE_MISMATCH=9`, `main(argv=None) -> int`, `report() -> int`, `run_loop(max_seconds) -> int`.

- [ ] **Step 1: Write the failing loop tests**

Create `tests/fut/test_fut_loop.py`:

```python
import json
import queue
from concurrent.futures import Future
from dataclasses import replace

import pytest

from bot.brain import REASON_BUDGET_STATE, Budget
from bot.settings import Settings
from fut.llm import LlmDecision
from fut.loop import FutLoop, Unmonitored, seed_budget
from fut.questions import jev_state
from fut.settings import FutSettings
from fut.store import FutStore
from fut.types import FutGate, FutIntent, JevVerdict
from kcex.client import KcexError
from kcex.fws import FutDepth, FutTicker
from tests.fut.helpers import SPEC

T0 = 1_789_000_000_000
UP = JevVerdict("up", 0.9, 0.9, 0.8, "trend", None, 100, 1000, "jev-1.13.0")


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class FakeRest:
    def __init__(self, ticker_error=False):
        self.calls, self.ticker_error = [], ticker_error

    def depth(self, symbol, limit=50):
        self.calls.append("depth")
        return 1, ((76000.0, 50),), ((76000.1, 50),)

    def klines_1m(self, symbol, start_s, end_s):
        self.calls.append("klines")
        return [(i * 60, 76000.0, 76030.0, 75970.0, 76000.0, 1.0) for i in range(20)]

    def funding(self, symbol):
        self.calls.append("funding")
        return 0.0001, None

    def ticker(self, symbol):
        self.calls.append("ticker")
        if self.ticker_error:
            raise KcexError("down", {"status": None})
        return FutTicker(0, 76000.0, 76000.0, 76000.1, 76000.0, 76000.0, 0.0001)

    def contract_detail(self, symbol):
        return SPEC


class FakeJev:
    name = "jev-1.13.0"

    def __init__(self, verdict):
        self.verdict, self.calls = verdict, 0

    def evaluate(self, snap, position, *, now_ms):
        self.calls += 1
        return replace(self.verdict, state=jev_state(snap, position, now_ms=now_ms))


def sync_submit(fn, *args):
    future = Future()
    future.set_result(fn(*args))
    return future


def build(tmp_path, *, action="LONG", rest=None, verdict=UP):
    store = FutStore(tmp_path / "fut.db")
    events, clock, calls = queue.SimpleQueue(), Clock(), []

    def decide(state, *, has_position, settings, budget, store):
        calls.append(has_position)
        return LlmDecision(intent=FutIntent(action, 0.8, "flow"), reason="ok", cost_usd=0.001)

    loop = FutLoop(settings=FutSettings(), store=store, spec=SPEC, rest=rest or FakeRest(), jev=FakeJev(verdict),
                   budget=Budget(0.0, 1.0, "2026-09-17"), events=events, clock_ms=clock, llm_decide=decide,
                   submit=sync_submit, store_factory=lambda: store)
    return loop, store, events, clock, calls


def ws_tick(events):
    events.put(("ws", FutTicker(0, 76000.0, 76000.0, 76000.1, 76000.0, 76000.0, 0.0001)))


def open_long(loop):
    notional = 2 * 0.0001 * 76000.0
    loop.ledger.open(FutGate(True, "ok_open", "LONG", side="long", contracts=2, price=76000.0, notional=notional,
                             margin=notional, stop=75900.0, liq=380.0, leverage=1), now_ms=T0)


def test_jev_wake_calls_llm_and_opens_long(tmp_path):
    loop, store, events, _, calls = build(tmp_path)
    ws_tick(events)
    loop.step()
    loop.step()
    assert loop.ledger.position.side == "long"
    assert calls == [False]
    jev = store.decisions("jev")[0]["payload"]
    assert (jev["wake"], jev["dispatch"]) == ("entry_signal", "dispatched")
    assert jev["cost_usd"] == pytest.approx(1000 / 1e6 * 0.042)
    llm = store.decisions("llm")[0]["payload"]
    assert (llm["verdict"], llm["outcome"], llm["cost_usd"]) == ("ok", "opened", 0.001)
    assert loop.llm_entries == 1


def test_hold_trades_nothing(tmp_path):
    loop, store, events, _, _ = build(tmp_path, action="HOLD")
    ws_tick(events)
    loop.step()
    loop.step()
    assert not loop.ledger.position.is_open()
    assert store.decisions("llm")[0]["payload"]["outcome"] == "ok"


def test_llm_close_on_exit_signal(tmp_path):
    exit_verdict = replace(UP, direction="flat", exit_now=0.9)
    loop, store, events, _, calls = build(tmp_path, action="CLOSE", verdict=exit_verdict)
    open_long(loop)
    ws_tick(events)
    loop.step()
    loop.step()
    assert not loop.ledger.position.is_open()
    assert calls == [True]
    assert store.decisions("llm")[0]["payload"]["outcome"] == "closed"


def test_entry_after_price_moved_is_logged_not_traded(tmp_path):
    loop, store, events, _, _ = build(tmp_path)
    ws_tick(events)
    loop.step()
    events.put(("ws", FutDepth(0, 2, ((76000.0, 0), (76100.0, 50)), ((76000.1, 0), (76100.1, 50)))))
    loop.step()
    assert not loop.ledger.position.is_open()
    assert store.decisions("llm")[0]["payload"]["verdict"] == "stale_price"


def test_stale_ws_uses_rest_prices_but_skips_jev_when_flat(tmp_path):
    rest = FakeRest()
    loop, _, _, _, _ = build(tmp_path, rest=rest)
    loop.step()
    assert {"depth", "klines", "funding", "ticker"} <= set(rest.calls)
    assert loop.jev.calls == 0


def test_unmonitored_open_position_halts(tmp_path):
    loop, store, _, _, _ = build(tmp_path, rest=FakeRest(ticker_error=True))
    open_long(loop)
    with pytest.raises(Unmonitored):
        loop.step()
    assert store.decisions("unmonitored")


def test_seed_budget_resumes_same_day_and_blocks_corrupt_state(tmp_path):
    store = FutStore(tmp_path / "fut.db")
    llm = Settings.from_env()
    store.kv_set("llm_budget", json.dumps({"day": "2026-09-17", "spent_usd": 0.3, "calls": 2}))
    resumed = seed_budget(store, llm, today="2026-09-17")
    assert (resumed.spent_usd, resumed.calls, resumed.blocked_reason) == (0.3, 2, None)
    assert seed_budget(store, llm, today="2026-09-18").spent_usd == 0.0
    store.kv_set("llm_budget", "not json")
    assert seed_budget(store, llm, today="2026-09-17").blocked_reason == REASON_BUDGET_STATE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./scripts/test tests/fut/test_fut_loop.py`
Expected: `ModuleNotFoundError: No module named 'fut.loop'`

- [ ] **Step 3: Implement `fut/loop.py`**

```python
"""One futures paper process step: drain WS events, refresh REST, mark, resolve the LLM, run Jev."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import asdict

from bot.brain import REASON_BUDGET_STATE, Budget
from bot.store import BudgetStateCorrupt
from fut import collar
from fut.dispatch import Dispatcher, Trigger
from fut.ledger import PaperLedger
from fut.llm import decide as default_llm_decide
from fut.market import MarketState
from fut.questions import jev_side, should_wake
from fut.settings import FutSettings
from fut.shadow import ShadowBooks
from fut.store import FutStore, day_bounds_ms, day_of
from kcex.fws import PublicFuturesWs, default_connect

log = logging.getLogger("fut")

BARS_EVERY_MS = 60_000
FUNDING_EVERY_MS = 60_000
TICKER_FALLBACK_EVERY_MS = 1_000
SPEC_EVERY_MS = 3_600_000


class Unmonitored(RuntimeError):
    """An open paper position has had no price at all for FUT_UNMONITORED_SECONDS."""


def now_ms() -> int:
    return int(time.time() * 1000)


def seed_budget(store: FutStore, llm, *, today: str) -> Budget:
    cap = llm.llm_daily_budget_usd
    try:
        persisted = store.budget_load()
    except BudgetStateCorrupt as exc:
        log.error("persisted LLM budget is unreadable; blocking LLM spend: %s", exc)
        return Budget(0.0, cap, today, blocked_reason=REASON_BUDGET_STATE)
    if persisted and persisted["day"] == today:
        return Budget(persisted["spent_usd"], cap, today, calls=persisted["calls"])
    return Budget(0.0, cap, today)


def start_ws_thread(settings: FutSettings, events: queue.SimpleQueue, stop: threading.Event, *,
                    connect=default_connect, sleep=time.sleep) -> threading.Thread:
    def run() -> None:
        ws = PublicFuturesWs(settings.ws_url, settings.symbol, connect)
        while not stop.is_set():
            try:
                ws.pump(on_event=lambda event: events.put(("ws", event)),
                        on_error=lambda exc: log.warning("futures ws error: %s", exc))
            except Exception as exc:  # noqa: BLE001 - reconnect on any transport failure
                log.warning("futures ws connect failed: %s", exc)
            if not stop.is_set():
                sleep(2.0)

    thread = threading.Thread(target=run, name="fut-ws", daemon=True)
    thread.start()
    return thread


class FutLoop:
    def __init__(self, *, settings: FutSettings, store: FutStore, spec, rest, jev, budget: Budget,
                 events: queue.SimpleQueue, clock_ms=now_ms, llm_decide=default_llm_decide, submit=None,
                 store_factory=None, rng=None):
        self.settings = settings
        self.store = store
        self.spec = spec
        self.rest = rest
        self.jev = jev
        self.budget = budget
        self.events = events
        self._clock = clock_ms
        self._llm_decide = llm_decide
        self._store_factory = store_factory or (lambda: FutStore(store.path))
        self._llm_store = None
        self.market = MarketState(settings, spec)
        self.ledger = PaperLedger(store, settings, spec)
        self.shadow = ShadowBooks(store, settings, spec, rng=rng)
        self.dispatcher = Dispatcher(settings, run_llm=self._run_llm, clock_ms=clock_ms, submit=submit)
        self._next = {"jev": 0, "bars": 0, "funding": 0, "ticker": 0, "spec": clock_ms() + SPEC_EVERY_MS}
        self._book_dirty = True
        self.jev_evals = 0
        self.llm_entries = 0

    # -- worker thread -----------------------------------------------------------

    def _run_llm(self, trigger: Trigger):
        # sqlite connections are bound to their thread: the worker opens its own.
        if self._llm_store is None:
            self._llm_store = self._store_factory()
        return self._llm_decide(trigger.state, has_position=trigger.state.get("position") != "flat",
                                settings=self.settings, budget=self.budget, store=self._llm_store)

    # -- main thread -------------------------------------------------------------

    def step(self) -> None:
        now = self._clock()
        self._drain(now)
        self._refresh(now)
        snap = self.market.snapshot(now)
        self._check_monitoring(now)
        exit_reason = self.ledger.mark(snap, now_ms=now)
        if exit_reason:
            self.store.log_decision("exit", {"reason": exit_reason, "balance": self.ledger.balance,
                                             "snapshot": snap.compact()}, ts_ms=now)
        self.shadow.mark(snap, now_ms=now)
        self._resolve(snap, now)
        if now >= self._next["jev"]:
            self._next["jev"] = now + int(self.settings.jev_every_s * 1000)
            self._jev(snap, now)

    def _drain(self, now: int) -> None:
        while True:
            try:
                source, event = self.events.get_nowait()
            except queue.Empty:
                return
            if not self.market.apply(event, now_ms=now, source=source):
                self._book_dirty = True

    def _refresh(self, now: int) -> None:
        symbol = self.settings.symbol
        if self._book_dirty or not self.market.book.synced:
            try:
                version, bids, asks = self.rest.depth(symbol)
                self.market.load_book(version, bids, asks)
                self._book_dirty = False
            except Exception as exc:  # noqa: BLE001
                log.warning("depth resync failed: %s", exc)
        if now >= self._next["bars"]:
            self._next["bars"] = now + BARS_EVERY_MS
            try:
                span_s = (self.settings.atr_period + 5) * 60
                self.market.set_bars(self.rest.klines_1m(symbol, now // 1000 - span_s, now // 1000))
            except Exception as exc:  # noqa: BLE001
                log.warning("kline refresh failed: %s", exc)
        if now >= self._next["funding"]:
            self._next["funding"] = now + FUNDING_EVERY_MS
            try:
                rate, next_ms = self.rest.funding(symbol)
                self.market.set_funding(rate, next_ms)
            except Exception as exc:  # noqa: BLE001
                log.warning("funding refresh failed: %s", exc)
        ws_silent = now - self.market.ws_last_ms > self.settings.stale_market_s * 1000
        if ws_silent and now >= self._next["ticker"]:
            self._next["ticker"] = now + TICKER_FALLBACK_EVERY_MS
            try:
                self.market.apply(self.rest.ticker(symbol), now_ms=now, source="rest")
            except Exception as exc:  # noqa: BLE001
                log.warning("REST ticker fallback failed: %s", exc)
        if now >= self._next["spec"]:
            self._next["spec"] = now + SPEC_EVERY_MS
            try:
                self._set_spec(self.rest.contract_detail(symbol))
            except Exception as exc:  # noqa: BLE001
                log.warning("contract detail refresh failed: %s", exc)

    def _set_spec(self, spec) -> None:
        self.spec = spec
        self.market.spec = spec
        self.ledger.spec = spec
        self.shadow.set_spec(spec)

    def _check_monitoring(self, now: int) -> None:
        if not self.ledger.position.is_open():
            return
        silent_ms = now - self.market.last_event_ms
        if silent_ms > self.settings.unmonitored_s * 1000:
            self.store.log_decision("unmonitored", {"silent_ms": silent_ms, "position": asdict(self.ledger.position)},
                                    ts_ms=now)
            raise Unmonitored(f"open paper position had no price for {silent_ms} ms")

    def _day_net(self, now: int) -> float:
        day = day_of(now)
        start, end = day_bounds_ms(day)
        return self.store.day_net("main", day) - self.store.model_cost_between(start, end)

    def _resolve(self, snap, now: int) -> None:
        res = self.dispatcher.poll(mid_now=snap.mid)
        if res is None:
            return
        decision, gate, outcome = res.decision, None, res.verdict
        if res.verdict == "ok" and decision.intent is not None and decision.intent.action != "HOLD":
            gate = collar.check(decision.intent, snap, position=self.ledger.position, balance=self.ledger.balance,
                                day_pnl_usdt=self._day_net(now), spec=self.spec, settings=self.settings)
            if gate.ok and gate.action in ("LONG", "SHORT"):
                self.ledger.open(gate, now_ms=now)
                self.llm_entries += 1
                outcome = "opened"
            elif gate.ok and gate.action == "CLOSE":
                price = self.ledger.market_exit_price(snap)
                if price is None:
                    outcome = "close_no_price"
                else:
                    self.ledger.close(price, now_ms=now, reason="llm_close")
                    outcome = "closed"
            else:
                outcome = f"gate_{gate.rule}"
        trigger = res.trigger
        self.store.log_decision("llm", {
            "trigger": {"kind": trigger.kind, "side": trigger.side, "ts_ms": trigger.ts_ms, "mid": trigger.mid},
            "llm": decision.as_audit(),
            "intent": asdict(decision.intent) if decision.intent else None,
            "verdict": res.verdict,
            "elapsed_ms": res.elapsed_ms,
            "mid_at_response": snap.mid,
            "gate": asdict(gate) if gate else None,
            "outcome": outcome,
            "cost_usd": decision.cost_usd,
        }, ts_ms=now)

    def _jev(self, snap, now: int) -> None:
        position = self.ledger.position
        if snap.stale and not position.is_open():
            return
        verdict = self.jev.evaluate(snap, position, now_ms=now)
        self.jev_evals += 1
        cost = verdict.input_tokens / 1e6 * self.settings.jev_usd_per_mtok
        wake = should_wake(verdict, position, threshold=self.settings.wake_threshold)
        shadow = self.shadow.on_jev(verdict, snap, now_ms=now, wake=wake,
                                    entry_rate=self.llm_entries / self.jev_evals)
        dispatch = None
        if wake:
            self.budget.roll_day(day_of(now))
            state = dict(verdict.state, jev=verdict.answers(), trigger=wake)
            trigger = Trigger(wake, jev_side(verdict), now, snap.mid, state)
            budget_ok = not self.budget.blocked_reason and self.budget.remaining() > 0
            dispatch = self.dispatcher.offer(trigger, budget_ok=budget_ok)
        self.store.log_decision("jev", {
            "model": verdict.model, "error": verdict.error, "latency_ms": verdict.latency_ms,
            "input_tokens": verdict.input_tokens, "cost_usd": cost, "answers": verdict.answers(),
            "wake": wake, "dispatch": dispatch, "shadow": shadow, "snapshot": snap.compact(),
        }, ts_ms=now)
```

- [ ] **Step 4: Run loop tests to verify they pass**

Run: `./scripts/test tests/fut/test_fut_loop.py`
Expected: 7 passed

- [ ] **Step 5: Write the failing CLI tests**

Create `tests/fut/test_fut_cli.py`:

```python
from bot.cli import InstanceLock
from bot.store import Store
import fut.cli as cli
from fut.store import FutStore


def test_report_without_database_creates_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "none.db")
    assert cli.main(["report"]) == cli.EXIT_OK
    assert "no futures paper database yet" in capsys.readouterr().out
    assert not (tmp_path / "none.db").exists()


def test_report_prints_the_criterion(tmp_path, monkeypatch, capsys):
    db = tmp_path / "fut.db"
    FutStore(db).close()
    monkeypatch.setattr(cli, "DB_PATH", db)
    assert cli.main(["report"]) == cli.EXIT_OK
    assert "Edge criterion: FAILED" in capsys.readouterr().out


def test_report_refuses_a_database_from_another_mode(tmp_path, monkeypatch):
    db = tmp_path / "bot.db"
    Store(db, mode="paper").close()
    monkeypatch.setattr(cli, "DB_PATH", db)
    assert cli.main(["report"]) == cli.EXIT_STORE_MISMATCH


def patch_run(tmp_path, monkeypatch):
    # add_file_logging attaches a FileHandler to the root logger for the rest of the
    # process; record the call instead so later tests (bot CLI log assertions) are unaffected.
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "futures.lock")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "futures.log")
    calls = {"run": [], "log": []}
    monkeypatch.setattr(cli, "run_loop", lambda max_seconds: calls["run"].append(max_seconds) or 0)
    monkeypatch.setattr(cli, "add_file_logging", lambda path: calls["log"].append(path))
    return calls


def test_run_exits_3_without_logging_when_lock_is_held(tmp_path, monkeypatch):
    calls = patch_run(tmp_path, monkeypatch)
    with InstanceLock(tmp_path / "futures.lock"):
        assert cli.main(["run", "--max-seconds", "1"]) == cli.EXIT_ALREADY_RUNNING
    assert calls == {"run": [], "log": []}


def test_run_takes_the_lock_then_logs_then_runs(tmp_path, monkeypatch):
    calls = patch_run(tmp_path, monkeypatch)
    assert cli.main(["run", "--max-seconds", "1"]) == cli.EXIT_OK
    assert calls == {"run": [1.0], "log": [tmp_path / "futures.log"]}
```

- [ ] **Step 6: Run CLI tests to verify they fail**

Run: `./scripts/test tests/fut/test_fut_cli.py`
Expected: `ModuleNotFoundError: No module named 'fut.cli'`

- [ ] **Step 7: Implement `fut/cli.py` and `fut/__main__.py`**

`fut/cli.py`:

```python
"""``python -m fut run [--max-seconds N]`` and ``python -m fut report``.

Paper only: no private route, no KCEX_TOKEN, no order is ever sent.

Exit codes: 0 ok, 3 another futures instance holds data/futures.lock, 8 an open paper position
had no price for FUT_UNMONITORED_SECONDS, 9 data/futures-paper.db was written by another mode.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from bot.cli import AlreadyRunning, InstanceLock, add_file_logging, setup_logging
from bot.store import StoreIdentityMismatch
from fut.jev import make_jev
from fut.loop import FutLoop, Unmonitored, now_ms, seed_budget, start_ws_thread
from fut.report import evaluate, render, summarize
from fut.settings import FutSettings
from fut.store import FutStore, day_of
from kcex.fapi import FuturesPublic

log = logging.getLogger("fut")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "futures-paper.db"
LOCK_PATH = DATA_DIR / "futures.lock"
LOG_PATH = DATA_DIR / "futures.log"
ENV_PATH = ROOT / ".env"

EXIT_OK = 0
EXIT_ALREADY_RUNNING = 3
EXIT_UNMONITORED = 8
EXIT_STORE_MISMATCH = 9
STEP_SLEEP_S = 0.2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fut")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the futures paper loop")
    run.add_argument("--max-seconds", type=float, default=None)
    sub.add_parser("report", help="print the edge report")
    args = parser.parse_args(argv)
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    if args.cmd == "report":
        return report()
    try:
        with InstanceLock(LOCK_PATH):
            add_file_logging(LOG_PATH)
            return run_loop(args.max_seconds)
    except AlreadyRunning as exc:
        log.error("%s", exc)
        return EXIT_ALREADY_RUNNING


def report() -> int:
    if not DB_PATH.exists():
        print(f"no futures paper database yet at {DB_PATH}")
        return EXIT_OK
    try:
        store = FutStore(DB_PATH)
    except StoreIdentityMismatch as exc:
        log.error("%s", exc)
        return EXIT_STORE_MISMATCH
    load_dotenv(ENV_PATH)
    settings = FutSettings.from_env()
    summary = summarize(store, settings)
    print(render(summary, evaluate(summary, settings)))
    return EXIT_OK


def run_loop(max_seconds: float | None) -> int:
    load_dotenv(ENV_PATH)
    settings = FutSettings.from_env()
    try:
        store = FutStore(DB_PATH)
    except StoreIdentityMismatch as exc:
        log.error("%s", exc)
        return EXIT_STORE_MISMATCH
    rest = FuturesPublic()
    spec = rest.contract_detail(settings.symbol)
    events: queue.SimpleQueue = queue.SimpleQueue()
    stop = threading.Event()
    if settings.ws_url:
        start_ws_thread(settings, events, stop)
    else:
        log.warning("FUT_WS_URL disabled: REST-only prices, entries stay blocked as stale")
    if settings.uses_mock_jev:
        log.warning("Jev is the mock stand-in: this session never counts toward the edge criterion")
    loop = FutLoop(settings=settings, store=store, spec=spec, rest=rest, jev=make_jev(settings),
                   budget=seed_budget(store, settings.llm, today=day_of(now_ms())), events=events,
                   store_factory=lambda: FutStore(DB_PATH))
    log.info("futures paper: leverage %sx, margin %s USDT, jev %s, llm %s, taker fee %s",
             settings.leverage, settings.margin_usdt, getattr(loop.jev, "name", "?"),
             settings.llm.llm_model or "(unset)", spec.taker_fee)
    started = time.monotonic()
    try:
        while max_seconds is None or time.monotonic() - started < max_seconds:
            loop.step()
            time.sleep(STEP_SLEEP_S)
    except Unmonitored as exc:
        log.error("%s", exc)
        return EXIT_UNMONITORED
    except KeyboardInterrupt:
        log.info("stopped by operator")
    finally:
        stop.set()
    return EXIT_OK
```

`fut/__main__.py`:

```python
from fut.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 8: Block implicit dotenv loads from `fut.cli` in tests**

In `tests/conftest.py`, change:

```python
_DOTENV_ALIAS_MODULES = ("bot.cli",)
```

to:

```python
_DOTENV_ALIAS_MODULES = ("bot.cli", "fut.cli")
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `./scripts/test tests/fut/test_fut_cli.py tests/fut/test_fut_loop.py`
Expected: 12 passed

Run: `./scripts/test`
Expected: all passed, 0 failed

- [ ] **Step 10: Smoke run against the real public market (paper, no keys needed)**

Run: `FUT_JEV_MODEL=mock OPENROUTER_API_KEY= LOG_LEVEL=INFO PYTHONPATH=. .venv/bin/python -m fut run --max-seconds 30; echo "exit=$?"`
Expected: log line `futures paper: leverage 1x ...`, a warning that Jev is the mock stand-in, no traceback, `exit=0`. Then:

Run: `PYTHONPATH=. .venv/bin/python -m fut report`
Expected: `Edge criterion: FAILED` with `jev models ['mock']` (a mock session must never pass).

Run: `sqlite3 data/futures-paper.db "SELECT kind, COUNT(*) FROM fut_decisions GROUP BY kind"`
Expected: a `jev` row with a count > 0 (the market was fresh long enough for Jev to run). If it is 0, read `data/futures.log` for `depth resync failed` / `futures ws error` and fix before committing; do not commit a loop that never evaluates.

Note: with `OPENROUTER_API_KEY` empty, any wake is recorded as `llm_config` and nothing trades; that is the expected smoke outcome.

- [ ] **Step 11: Commit**

```bash
git add fut/loop.py fut/cli.py fut/__main__.py tests/conftest.py tests/fut/test_fut_loop.py tests/fut/test_fut_cli.py
git commit -m "feat(fut): futures paper loop and CLI (run, report) with lock, monitoring and exit codes" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---
### Task 13: Project docs and env template

**Files:**
- Modify: `CLAUDE.md`
- Modify: `AGENTS.md`
- Modify: `docs/kcex-spot-api.md`
- Modify: `.env.example`

**Interfaces:**
- Consumes: the final names from Tasks 1–12 (commands, env vars, paths, exit codes).
- Produces: documentation only. No code.

- [ ] **Step 1: `CLAUDE.md`**

Replace the bullet:

```markdown
- Add a second "judge" model, extra pairs, Telegram, or futures unless asked.
```

with:

```markdown
- Add a second "judge" model, extra pairs, Telegram, or **live** futures unless asked. Futures **paper** lives in `fut/` (spec `docs/superpowers/specs/2026-09-17-kcex-futures-paper-jev-llm-design.md`): public `/fapi` reads only, never a private route or `KCEX_TOKEN`.
```

Add these rows at the end of the Layout table:

```markdown
| `kcex/fws.py`, `kcex/fapi.py` | Public futures WS (incremental book) and REST — GET only, no auth |
| `fut/` | Futures paper: Jev trigger → LLM decision → collar → paper ledger, shadow baselines, report |
| `docs/kcex-futures-api.md` | Captured public futures endpoints and frames |
```

Add under `## Run`:

```bash
PYTHONPATH=. python -m fut run                  # futures paper loop (Jev every 2 s, LLM on wake)
PYTHONPATH=. python -m fut report               # edge criterion vs flat/jev_only/random baselines
```

Add a section before `## Resume`:

```markdown
## Futures paper (fut/)

Paper only, BTC_USDT perpetual, leverage 1x default and 3x hard cap, isolated margin, market orders at book ± slippage with the venue taker fee. Jev (`typesafe-sdk`, `FUT_JEV_MODEL`, `TYPESAFE_API_KEY`) evaluates every 2 s; `fut/questions.py` holds every question and threshold. A wake calls the LLM immediately, one call at a time, 10 s cooldown only after HOLD; late or price-moved LONG/SHORT are discarded, CLOSE never is. Stop, 5 min max hold and liquidation (by `fairPrice`) are enforced every step. Own DB `data/futures-paper.db` (mode `futures-paper`), own lock `data/futures.lock`. Exit codes: 3 already running, 8 unmonitored position, 9 DB of another mode. Sessions with the mock Jev never count toward the edge criterion; passing the criterion only allows writing a live spec.
```

- [ ] **Step 2: `AGENTS.md`**

In `## Out of scope until the owner asks`, replace `futures (a public futures API exists, see the API doc; not used)` with `live futures orders (futures paper exists in `fut/`, see docs/kcex-futures-api.md and the 2026-09-17 spec)`.

Add a section right after `## P4 — decision journal and deferred reflection (opt-in)` and its body:

```markdown
## Futures paper (fut/)

- Spec: `docs/superpowers/specs/2026-09-17-kcex-futures-paper-jev-llm-design.md`. Plan: `docs/superpowers/plans/2026-09-17-kcex-futures-paper-jev-llm.md`.
- Transport: `kcex/fws.py` (public WS `wss://www.kcex.com/fapi/edge`, incremental depth kept from a REST snapshot + contiguous `version`) and `kcex/fapi.py` (GET only, client built with `token=""`). Deal side `T=2` buy / `T=1` sell is inferred from captured prints, not documented.
- Flow per step: drain WS queue → REST refresh (depth resync on gap, 1m klines, funding, ticker fallback when WS is silent) → ledger mark (liquidation, stop, time limit, funding) → shadow mark → resolve LLM → Jev every 2 s.
- REST fallback prices never make the market fresh for entries (`MarketState.ws_last_ms`).
- `fut/collar.py` sizes off the same `fut/pricing.fill_price` the ledger fills at. CLOSE is never blocked.
- The LLM worker thread opens its own `FutStore` connection (sqlite connections are thread-bound) for the durable budget reservation.
- Baselines `shadow:jev_only` and `shadow:random` share ticks, collar and ledger; `flat` is 0.
- Edge criterion (fixed): ≥ 200 trades, ≥ 14 days, no mock Jev, net > 0 after fees/funding/slippage/Jev/LLM, beats all three baselines, bootstrap CI95 lower bound of per-trade net > 0, no day below `FUT_MAX_DAY_LOSS_USDT`.
- Tests: `tests/fut/test_fut_*.py`, `tests/kcex/test_fws.py`, `tests/kcex/test_fapi.py`. No network.
```

- [ ] **Step 3: `docs/kcex-spot-api.md`**

Replace the heading `## Futures (out of scope for this bot)` with `## Public WebSocket (spot) and futures note`.

Replace the last sentence of the "For the record" paragraph, `Not used by this bot.`, with `The spot bot does not use it; the futures paper bot in `fut/` does — see docs/kcex-futures-api.md.`

- [ ] **Step 4: `.env.example`**

Append:

```bash

# --- Futures paper (python -m fut run). Paper only; no KCEX login needed. ---
FUT_LEVERAGE=1
FUT_MARGIN_USDT=20
FUT_MAX_BALANCE_PCT=0.05
FUT_MAX_DAY_LOSS_USDT=20
FUT_PAPER_STARTING_USDT=450
FUT_SLIPPAGE_BPS=2
FUT_ATR_PERIOD=14
FUT_ATR_MULT=2
FUT_MIN_STOP_PCT=0.001
FUT_MAX_STOP_PCT=0.01
FUT_LIQ_STOP_RATIO=0.5
FUT_MAX_HOLD_SECONDS=300
FUT_MIN_CONFIDENCE=0
# Jev (TypeSafe). Empty key or FUT_JEV_MODEL=mock uses the stand-in (never counts for edge).
TYPESAFE_API_KEY=
FUT_JEV_MODEL=jev-latest
FUT_JEV_EVERY_SECONDS=2
FUT_JEV_TIMEOUT_SECONDS=2
FUT_JEV_USD_PER_MTOK=0.042
FUT_WAKE_THRESHOLD=0.6
FUT_MOVE_COST_BPS=3
# LLM call rules (model/key/budget come from LLM_MODEL, OPENROUTER_API_KEY, LLM_DAILY_BUDGET_USD).
FUT_LLM_COOLDOWN_SECONDS=10
FUT_LLM_TIMEOUT_SECONDS=8
FUT_STALE_PRICE_BPS=5
FUT_LLM_REASONING=0
# Market freshness. Empty = wss://www.kcex.com/fapi/edge; - = REST only (entries stay blocked).
FUT_WS_URL=
FUT_STALE_MARKET_SECONDS=5
FUT_UNMONITORED_SECONDS=60
FUT_SHADOW_SEED=7
```

- [ ] **Step 5: Verify nothing else changed behavior**

Run: `./scripts/test`
Expected: all passed, 0 failed

Run: `git diff --stat`
Expected: only the four documentation files listed above.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md AGENTS.md docs/kcex-spot-api.md .env.example
git commit -m "docs: futures paper (fut/) in agent docs, API notes and env template" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Spec coverage

| Spec section | Task |
|---|---|
| Fatos do contrato (lidos em runtime, refresh horário) | 2, 12 (`_refresh` spec every hour) |
| Arquitetura (`fut/`, banco, lock, reaproveitamento) | 3, 5, 12 |
| Fluxo (tick → mark; 2 s → Jev → dispatch → LLM → collar → ledger; auditoria) | 12 |
| Jev (estado, perguntas, regra de acordar, cliente, mock) | 7 |
| LLM (entrada, saída, validação, modelo, orçamento durável) | 8 |
| Dispatch | 9 |
| Collar | 4 |
| Ledger (fills, taxa, margem, liquidação, stop, tempo, funding, atomicidade, reinício) | 5 |
| Shadow | 10 |
| Falhas (WS mudo, REST, Jev, LLM, orçamento, identidade, lock) | 6, 7, 8, 12 |
| Captura do WS de futuros | 1 |
| Medição e critério de edge | 11 |
| Testes | every task |
| Documentação | 1, 13 |
