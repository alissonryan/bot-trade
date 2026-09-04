# KCEX public WS + local chart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Feed the bot Eye from the captured KCEX public WebSocket and, with `--chart`, republish the same ticks on localhost plus a 15m candle page.

**Architecture:** `kcex/ws.py` parses public frames (no login). `bot/hub.py` holds last/bid/ask. Eye applies hub updates and REST-polls only when stale. `bot/chart_server.py` binds `127.0.0.1` and serves HTML + `/kline` + `/ws`. One process, one KCEX socket.

**Tech Stack:** Python 3, pytest, `websockets` (already in requirements), stdlib `http.server` + threading, existing `KcexClient.kline`.

**Spec:** `docs/superpowers/specs/2026-09-04-kcex-public-ws-and-local-chart-design.md`

---

## File map

| Path | Responsibility |
| --- | --- |
| `kcex/ws.py` | `parse_frame`, subscribe JSON, `PublicSpotWs` loop with injectable connect |
| `bot/hub.py` | In-process last/bid/ask/`ws_ok` |
| `bot/eye.py` | Map events → hub/eye; skip `poll_quotes` when WS fresh |
| `bot/settings.py` | Default `KCEX_WS_URL`, `CHART_PORT`, `CHART_HOST`, `-` disables WS |
| `bot/chart_encode.py` | Our tick/deal JSON |
| `bot/chart_server.py` | Loopback HTTP + local WS fan-out |
| `chart/index.html` | 15m candles + last price, no order UI |
| `bot/cli.py` | Start WS thread; `--chart` |
| `tests/fixtures/kcex_ws_frames.jsonl` | Real captured frames appended |
| `docs/kcex-spot-api.md` | Confirmed `wss://wbs.kcex.com/ws` |
| `AGENTS.md`, `CLAUDE.md`, `README.md`, `.env.example` | New defaults |

CI never opens `wss://wbs.kcex.com`. Do not use `ws_token` for this feature. Do not bind `0.0.0.0`.

---

### Task 1: Parse captured public frames

**Files:**
- Create: `kcex/ws.py`
- Create: `tests/kcex/test_ws_parse.py`
- Modify: `tests/fixtures/kcex_ws_frames.jsonl`

- [ ] **Step 1: Append real fixtures** (keep the existing first line so `test_apply_frame_updates_last` still passes)

Append these two lines to `tests/fixtures/kcex_ws_frames.jsonl`:

```json
{"c":"spot@public.miniTicker@BTC_USDT@UTC+0","s":"BTC_USDT","t":1788553351046,"d":{"s":"BTC_USDT","p":"79801.99","h":"82267.72","l":"78660.01","q":"18384.29538","v":"1477022288.46","r":"0.0046","tr":"-0.018"}}
{"c":"spot@public.aggre.deals@BTC_USDT","s":"BTC_USDT","d":{"deals":[{"M":0,"T":2,"p":"79801.99","q":"0.00239","t":1788553351631}]}}
```

- [ ] **Step 2: Write failing tests**

```python
# tests/kcex/test_ws_parse.py
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kcex.ws import DealEvent, DepthEvent, TickerEvent, parse_frame, subscribe_message


def test_parse_miniticker():
    lines = (ROOT / "tests/fixtures/kcex_ws_frames.jsonl").read_text().splitlines()
    msg = json.loads(lines[1])
    ev = parse_frame(msg)
    assert isinstance(ev, TickerEvent)
    assert ev.last == 79801.99
    assert ev.symbol == "BTC_USDT"
    assert ev.ts_ms == 1788553351046


def test_parse_deal_sell():
    lines = (ROOT / "tests/fixtures/kcex_ws_frames.jsonl").read_text().splitlines()
    ev = parse_frame(json.loads(lines[2]))
    assert isinstance(ev, DealEvent)
    assert ev.price == 79801.99
    assert ev.qty == 0.00239
    assert ev.side == "sell"
    assert ev.ts_ms == 1788553351631


def test_parse_pong_and_ack_ignored():
    assert parse_frame({"msg": "PONG"}) is None
    assert parse_frame({"id": 3, "code": 0, "msg": "spot@public.aggre.deals@BTC_USDT"}) is None
    assert parse_frame({"not": "json-shape"}) is None


def test_parse_depth_best_or_none():
    ev = parse_frame(
        {
            "c": "spot@public.limit.precision.depth@BTC_USDT@0.01",
            "d": {
                "bids": [{"p": "79801.90", "q": "1"}],
                "asks": [{"p": "79802.00", "q": "1"}],
            },
        }
    )
    assert isinstance(ev, DepthEvent)
    assert ev.bid == 79801.9
    assert ev.ask == 79802.0
    assert parse_frame({"c": "spot@public.limit.precision.depth@BTC_USDT@0.01", "d": {}}) is None or (
        isinstance(parse_frame({"c": "spot@public.limit.precision.depth@BTC_USDT@0.01", "d": {}}), DepthEvent)
        and parse_frame({"c": "spot@public.limit.precision.depth@BTC_USDT@0.01", "d": {}}).bid is None
    )


def test_subscribe_message():
    body = subscribe_message("BTC_USDT")
    assert body["method"] == "SUBSCRIPTION"
    assert "spot@public.miniTicker@BTC_USDT@UTC+0" in body["params"]
    assert "spot@public.aggre.deals@BTC_USDT" in body["params"]
    assert "spot@public.limit.precision.depth@BTC_USDT@0.01" in body["params"]
```

For empty depth, pick one behavior and stick to it: **return `None`** if neither bid nor ask can be read.

- [ ] **Step 3: Run tests to verify they fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/kcex/test_ws_parse.py -q`

Expected: FAIL (`ModuleNotFoundError` or `ImportError` for `kcex.ws`)

- [ ] **Step 4: Minimal `kcex/ws.py`**

```python
# kcex/ws.py
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


DEFAULT_WS_URL = "wss://wbs.kcex.com/ws?platform=web"


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


def subscribe_message(symbol: str) -> dict[str, Any]:
    return {
        "method": "SUBSCRIPTION",
        "params": [
            f"spot@public.miniTicker@{symbol}@UTC+0",
            f"spot@public.aggre.deals@{symbol}",
            f"spot@public.limit.precision.depth@{symbol}@0.01",
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
    if channel.startswith("spot@public.miniTicker@"):
        last = _f(data.get("p"))
        if last is None:
            return None
        ts = int(msg.get("t") or data.get("t") or 0)
        return TickerEvent(last=last, ts_ms=ts, symbol=symbol)
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/kcex/test_ws_parse.py tests/bot/test_eye_ws_parse.py -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add kcex/ws.py tests/kcex/test_ws_parse.py tests/fixtures/kcex_ws_frames.jsonl
git commit -m "feat: parse KCEX public miniTicker, deals, and depth frames"
```

---

### Task 2: Settings — default WS URL and chart bind

**Files:**
- Modify: `bot/settings.py`
- Modify: `tests/bot/test_settings.py`
- Modify: `.env.example`

- [ ] **Step 1: Write failing tests**

Add to `tests/bot/test_settings.py`:

```python
def test_ws_url_default_is_captured_public(monkeypatch):
    monkeypatch.delenv("KCEX_WS_URL", raising=False)
    s = Settings.from_env()
    assert s.ws_url == "wss://wbs.kcex.com/ws?platform=web"


def test_ws_url_dash_disables(monkeypatch):
    monkeypatch.setenv("KCEX_WS_URL", "-")
    s = Settings.from_env()
    assert s.ws_url == ""


def test_chart_bind_defaults(monkeypatch):
    monkeypatch.delenv("CHART_PORT", raising=False)
    monkeypatch.delenv("CHART_HOST", raising=False)
    s = Settings.from_env()
    assert s.chart_port == 8765
    assert s.chart_host == "127.0.0.1"
```

- [ ] **Step 2: Run to verify fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_settings.py -q`

Expected: FAIL (`ws_url == ""` or missing `chart_port`)

- [ ] **Step 3: Implement settings**

In `bot/settings.py` add fields `chart_port: int` and `chart_host: str`. In `from_env`:

```python
from kcex.ws import DEFAULT_WS_URL

raw_ws = os.getenv("KCEX_WS_URL", "").strip()
if raw_ws == "-":
    ws_url = ""
elif raw_ws == "":
    ws_url = DEFAULT_WS_URL
else:
    ws_url = raw_ws
```

```python
chart_port=_i("CHART_PORT", 8765),
chart_host=os.getenv("CHART_HOST", "127.0.0.1").strip() or "127.0.0.1",
ws_url=ws_url,
```

Update `.env.example`:

```
# Public spot WS. Empty = captured default wss://wbs.kcex.com/ws?platform=web
# Set to - for REST-only quotes.
KCEX_WS_URL=
CHART_HOST=127.0.0.1
CHART_PORT=8765
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_settings.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/settings.py tests/bot/test_settings.py .env.example
git commit -m "feat: default KCEX public WS URL and chart bind settings"
```

---

### Task 3: Hub

**Files:**
- Create: `bot/hub.py`
- Create: `tests/bot/test_hub.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/bot/test_hub.py
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.hub import Hub
from kcex.ws import DealEvent, DepthEvent, TickerEvent


def test_ticker_sets_last_and_ws_ok():
    hub = Hub()
    hub.apply(TickerEvent(last=79801.99, ts_ms=1, symbol="BTC_USDT"))
    assert hub.last == 79801.99
    assert hub.ws_ok is True
    assert hub.ts_ms == 1


def test_deal_updates_last():
    hub = Hub()
    hub.apply(DealEvent(price=10.0, qty=1, side="buy", ts_ms=2, symbol="BTC_USDT"))
    assert hub.last == 10.0


def test_depth_sets_bid_ask():
    hub = Hub()
    hub.apply(DepthEvent(bid=1.0, ask=2.0, symbol="BTC_USDT"))
    assert hub.bid == 1.0
    assert hub.ask == 2.0


def test_ignore_none():
    hub = Hub()
    hub.apply(None)
    assert hub.ws_ok is False
    assert hub.last == 0.0


def test_mark_down():
    hub = Hub()
    hub.apply(TickerEvent(last=1, ts_ms=1, symbol="BTC_USDT"))
    hub.mark_down()
    assert hub.ws_ok is False
```

- [ ] **Step 2: Run to verify fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_hub.py -q`

Expected: FAIL import `bot.hub`

- [ ] **Step 3: Implement**

```python
# bot/hub.py
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
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_hub.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/hub.py tests/bot/test_hub.py
git commit -m "feat: in-process market hub for WS ticks"
```

---

### Task 4: Eye uses hub; REST only when stale

**Files:**
- Modify: `bot/eye.py`
- Modify: `tests/bot/test_eye_rest.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/bot/test_eye_rest.py`:

```python
from bot.hub import Hub
from kcex.ws import TickerEvent


def test_poll_quotes_skipped_when_ws_fresh():
    client = FakeKcex()
    hub = Hub()
    eye = Eye(client, Settings.from_env(), hub=hub)
    hub.apply(TickerEvent(last=111.0, ts_ms=1, symbol="BTC_USDT"))
    eye.sync_hub()
    eye.poll_quotes()
    assert eye.last == 111.0  # REST ticker 80000 not applied


def test_poll_quotes_runs_when_ws_down():
    eye = Eye(FakeKcex(), Settings.from_env(), hub=Hub())
    eye.poll_quotes()
    assert eye.last == 80000.0
    assert eye.ws_ok is False
```

- [ ] **Step 2: Run to verify fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_eye_rest.py -q`

Expected: FAIL (`hub=` unexpected or last overwritten)

- [ ] **Step 3: Wire Eye to Hub**

In `Eye.__init__` add `hub: Hub | None = None` and `self.hub = hub or Hub()`.

```python
def sync_hub(self) -> None:
    if not self.hub.ws_ok:
        return
    self.last = self.hub.last
    if self.hub.bid:
        self.bid = self.hub.bid
    if self.hub.ask:
        self.ask = self.hub.ask
    self.ws_ok = True
    self.last_update_ms = self.hub.ts_ms or int(time.time() * 1000)


def poll_quotes(self) -> None:
    self.sync_hub()
    if self.ws_ok and not self._stale():
        return
    ticker = self.client.ticker(self.settings.symbol)
    last = float(ticker["data"]["c"])
    depth = self.client.depth(self.settings.symbol)
    book = depth["data"]["data"]
    bid = float(book["bids"][0]["p"])
    ask = float(book["asks"][0]["p"])
    self.last, self.bid, self.ask = last, bid, ask
    self.last_update_ms = int(time.time() * 1000)
    self.ws_ok = False
```

Keep `apply_frame` working for the old fixture (maps `last`/`c`/`p`). Also add:

```python
def apply_event(self, event) -> None:
    self.hub.apply(event)
    self.sync_hub()
```

Replace `connect_ws` stub: if not `self.settings.ws_url`, return. Do **not** raise `NotImplementedError`. Full socket loop is Task 5; here `connect_ws` may still be empty besides the no-op, but it must not raise.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_eye_rest.py tests/bot/test_eye_ws_parse.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/eye.py tests/bot/test_eye_rest.py
git commit -m "feat: skip REST quote poll when public WS snapshot is fresh"
```

---

### Task 5: PublicSpotWs with injectable transport (no live KCEX)

**Files:**
- Modify: `kcex/ws.py`
- Create: `tests/kcex/test_ws_client.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/kcex/test_ws_client.py
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kcex.ws import PublicSpotWs, TickerEvent, parse_text


class FakeSock:
    def __init__(self, incoming: list[str]):
        self.incoming = list(incoming)
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def recv(self) -> str:
        if not self.incoming:
            raise ConnectionError("closed")
        return self.incoming.pop(0)


def test_run_once_subscribes_and_yields_ticker():
    frame = {
        "c": "spot@public.miniTicker@BTC_USDT@UTC+0",
        "s": "BTC_USDT",
        "t": 1,
        "d": {"p": "100.5", "s": "BTC_USDT"},
    }
    sock = FakeSock([json.dumps(frame)])
    events = []
    ws = PublicSpotWs(url="wss://example.invalid/ws", symbol="BTC_USDT", connect=lambda url: sock)
    ws.pump(on_event=events.append, on_error=lambda e: None, max_messages=1)
    assert any(isinstance(e, TickerEvent) and e.last == 100.5 for e in events)
    sub = json.loads(sock.sent[0])
    assert sub["method"] == "SUBSCRIPTION"


def test_bad_json_does_not_raise():
    sock = FakeSock(["not-json", json.dumps({"msg": "PONG"})])
    events = []
    ws = PublicSpotWs(url="wss://example.invalid/ws", symbol="BTC_USDT", connect=lambda url: sock)
    ws.pump(on_event=events.append, on_error=lambda e: None, max_messages=2)
    assert events == []
```

- [ ] **Step 2: Run to verify fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/kcex/test_ws_client.py -q`

Expected: FAIL (`PublicSpotWs` missing)

- [ ] **Step 3: Implement `PublicSpotWs`**

Add to `kcex/ws.py`:

```python
class PublicSpotWs:
    def __init__(self, url: str, symbol: str, connect):
        self.url = url
        self.symbol = symbol
        self._connect = connect

    def pump(self, *, on_event, on_error, max_messages: int | None = None) -> None:
        sock = self._connect(self.url)
        sock.send(json.dumps(subscribe_message(self.symbol)))
        n = 0
        while max_messages is None or n < max_messages:
            try:
                raw = sock.recv()
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
```

Default `connect` (runtime only, not CI):

```python
def default_connect(url: str):
    from websockets.sync.client import connect
    return connect(url, open_timeout=10)
```

`pump` runs in a daemon thread from CLI (Task 8). This task only adds the class + tests.

If subscribe ack `code` not 0 appears as a later frame with `c` missing, `parse_frame` already returns None.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/kcex/test_ws_client.py tests/kcex/test_ws_parse.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add kcex/ws.py tests/kcex/test_ws_client.py
git commit -m "feat: public spot WS pump with injectable transport"
```

---

### Task 6: Local tick encoder

**Files:**
- Create: `bot/chart_encode.py`
- Create: `tests/bot/test_chart_encode.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/bot/test_chart_encode.py
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.chart_encode import encode_deal, encode_tick
from bot.hub import Hub
from kcex.ws import DealEvent, TickerEvent


def test_encode_tick_from_hub():
    hub = Hub()
    hub.apply(TickerEvent(last=79801.99, ts_ms=9, symbol="BTC_USDT"))
    hub.bid = 79801.9
    hub.ask = 79802.0
    msg = encode_tick(hub)
    assert msg["type"] == "tick"
    assert msg["symbol"] == "BTC_USDT"
    assert msg["last"] == 79801.99
    assert msg["bid"] == 79801.9
    assert msg["ask"] == 79802.0
    assert msg["ts_ms"] == 9
    assert "c" not in msg and "d" not in msg


def test_encode_deal():
    msg = encode_deal(DealEvent(price=1.5, qty=0.2, side="sell", ts_ms=3, symbol="BTC_USDT"))
    assert msg == {
        "type": "deal",
        "symbol": "BTC_USDT",
        "price": 1.5,
        "qty": 0.2,
        "side": "sell",
        "ts_ms": 3,
    }
```

- [ ] **Step 2: Run to verify fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_chart_encode.py -q`

Expected: FAIL import

- [ ] **Step 3: Implement**

```python
# bot/chart_encode.py
from __future__ import annotations

from bot.hub import Hub
from kcex.ws import DealEvent


def encode_tick(hub: Hub) -> dict:
    return {
        "type": "tick",
        "symbol": hub.symbol or "BTC_USDT",
        "last": hub.last,
        "bid": hub.bid,
        "ask": hub.ask,
        "ts_ms": hub.ts_ms,
    }


def encode_deal(event: DealEvent) -> dict:
    return {
        "type": "deal",
        "symbol": event.symbol,
        "price": event.price,
        "qty": event.qty,
        "side": event.side,
        "ts_ms": event.ts_ms,
    }
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_chart_encode.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/chart_encode.py tests/bot/test_chart_encode.py
git commit -m "feat: encode localhost chart ticks without raw KCEX frames"
```

---

### Task 7: Chart HTTP loopback + kline + static page

**Files:**
- Create: `bot/chart_server.py`
- Create: `chart/index.html`
- Create: `tests/bot/test_chart_server.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/bot/test_chart_server.py
from pathlib import Path
import sys
import threading
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.chart_server import ChartServer, require_loopback
from bot.hub import Hub


class FakeKcex:
    def kline(self, symbol="BTC_USDT", interval="Min15", start=0, end=0, open_price_mode="LAST_CLOSE"):
        return {"code": 200, "data": {"t": [1], "o": [1], "h": [2], "l": [0.5], "c": [1.5], "v": [10]}}


def test_require_loopback_rejects_public():
    try:
        require_loopback("0.0.0.0")
        assert False, "should have raised"
    except ValueError:
        pass
    assert require_loopback("127.0.0.1") == "127.0.0.1"


def test_http_index_and_kline():
    server = ChartServer(hub=Hub(), client=FakeKcex(), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    server.start()
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        index = urllib.request.urlopen(base + "/", timeout=3).read().decode()
        assert "BTC" in index or "chart" in index.lower()
        raw = urllib.request.urlopen(base + "/kline", timeout=3).read().decode()
        assert "1.5" in raw
    finally:
        server.shutdown()
```

`port=0` is **only for tests** so they do not collide. Production uses `CHART_PORT` and must fail if in use (`ChartServer.start` uses the given port, not 0).

If `start()` + `serve_forever` API is awkward, use: `server = ChartServer(...); server.start()` which internally threads, and `server.port` is the bound port.

- [ ] **Step 2: Run to verify fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_chart_server.py -q`

Expected: FAIL import

- [ ] **Step 3: Implement server + HTML**

`require_loopback(host)` allows only `127.0.0.1`, `localhost`, `::1`.

One `ThreadingHTTPServer` on `CHART_PORT`:

- `GET /` → `chart/index.html`
- `GET /kline` → JSON from `client.kline(symbol, interval="Min15", start, end)`
- `GET /ws` → RFC6455 upgrade (`Sec-WebSocket-Accept`). Keep the socket. `ChartServer.clients` holds connections; publish `encode_tick(hub)` as text frames.
- Bind error → raise `OSError` that includes the port. No fallback port.
- `start()` launches a daemon thread; `shutdown()` stops it.

Test WS with `websockets.sync.client.connect(f"ws://127.0.0.1:{server.port}/ws")`.

`chart/index.html`: last-price node; `fetch("/kline")`; Lightweight Charts from `https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js`; `new WebSocket((location.protocol === "https:" ? "wss" : "ws") + "://" + location.host + "/ws")`; **no** buy/sell controls.


- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_chart_server.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/chart_server.py chart/index.html tests/bot/test_chart_server.py
git commit -m "feat: localhost chart HTTP, kline, and WS tick stream"
```

---

### Task 8: CLI — WS thread + `--chart`

**Files:**
- Modify: `bot/cli.py`
- Modify: `bot/eye.py` (`start_ws_thread`)
- Create: `tests/bot/test_cli_chart.py`

- [ ] **Step 1: Write failing test**

```python
# tests/bot/test_cli_chart.py
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.cli import build_parser


def test_chart_flag():
    p = build_parser()
    ns = p.parse_args(["run", "--chart"])
    assert ns.chart is True
    ns2 = p.parse_args(["run"])
    assert ns2.chart is False
```

- [ ] **Step 2: Run to verify fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_cli_chart.py -q`

Expected: FAIL (`build_parser` missing)

- [ ] **Step 3: Implement**

Extract `build_parser()` from `main`. Add `--chart`.

After `Eye` is constructed:

```python
hub = eye.hub
if settings.ws_url:
    eye.start_ws_thread()
chart = None
if args.chart:
    from bot.chart_server import ChartServer
    chart = ChartServer(hub=hub, client=client, host=settings.chart_host, port=settings.chart_port)
    chart.start()
    print(f"chart http://{settings.chart_host}:{settings.chart_port}/")
```

`Eye.start_ws_thread`:

```python
def start_ws_thread(self) -> None:
    if not self.settings.ws_url:
        return
    from kcex.ws import PublicSpotWs, default_connect
    import threading

    def on_event(ev):
        self.apply_event(ev)

    def on_error(exc):
        self.hub.mark_down()
        self.ws_ok = False

    def loop():
        while True:
            try:
                ws = PublicSpotWs(self.settings.ws_url, self.settings.symbol, default_connect)
                ws.pump(on_event=on_event, on_error=on_error)
            except Exception:
                self.hub.mark_down()
            time.sleep(2)

    threading.Thread(target=loop, daemon=True).start()
```

Inside `pump`, every 15 seconds call `sock.send(json.dumps(ping_message()))`. `connect_ws` may call `start_ws_thread`. On `--chart` bind failure: print and `return 1`.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests -q`

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add bot/cli.py bot/eye.py kcex/ws.py tests/bot/test_cli_chart.py
git commit -m "feat: run public WS in a thread and optional --chart server"
```

---

### Task 9: Docs

**Files:**
- Modify: `docs/kcex-spot-api.md` (WebSocket section)
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Replace “WS URL not confirmed”**

In `docs/kcex-spot-api.md` WebSocket section, state:

- Public: `wss://wbs.kcex.com/ws?platform=web`
- Subscribe JSON and channels from the spec
- Ping `{"method":"PING"}` / `{"msg":"PONG"}`
- Chart candles still REST kline
- `ws_token` is **not** used for this public socket
- `KCEX_WS_URL=-` forces REST quotes

- [ ] **Step 2: AGENTS / CLAUDE / README**

- Eye v1 production path is public WS + REST fallback
- `python -m bot run --chart` → `http://127.0.0.1:8765/`
- Do not invent other `wss://` hosts; this one was captured
- Do not bind chart off loopback

- [ ] **Step 3: Commit**

```bash
git add docs/kcex-spot-api.md AGENTS.md CLAUDE.md README.md
git commit -m "docs: confirmed KCEX public WS and local chart usage"
```

---

## Spec coverage

| Spec item | Task |
| --- | --- |
| Parse miniTicker / deals / depth | 1 |
| Default URL / `-` REST-only / chart bind | 2 |
| Hub | 3 |
| Eye skip REST when fresh | 4 |
| PublicSpotWs inject / no live CI | 5 |
| Our tick JSON, no raw frames | 6 |
| Localhost HTTP + kline + WS + HTML | 7 |
| `--chart`, WS thread, PING | 8 |
| Docs + stop “do not invent wss” | 9 |
| No `ws_token` | 5, 8 (unused) |
| Port in use fails | 7, 8 |
| BTC/USDT only | subscribe_message uses `settings.symbol` |

## Notes for implementers

- Keep `tests/bot/test_eye_ws_parse.py` green (old `{ch,last,bid,ask}` fixture line 1).
- Never log full session tokens.
- Do not open a second KCEX WS for the chart.
