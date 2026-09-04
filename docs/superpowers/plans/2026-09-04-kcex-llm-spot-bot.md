# KCEX LLM Spot Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a paper-first BTC/USDT bot that watches KCEX over WebSocket, asks one OpenRouter model every 15 minutes (or on a 0.4% move) for BUY/SELL/HOLD, then a code collar sizes a 20 USDT market order and parks an ATR stop-market on the exchange when `MODE=live`.

**Architecture:** Keep `kcex/` as the only venue client. Add `bot/` with Eye (WS + REST snapshot), Brain (OpenRouter JSON), Collar (pure rules), Hands (paper ledger or live place+trigger), Store (SQLite audit), Cycle (scheduler). LLM never sets size or stop. Never cancel order ids that are not in the bot ledger.

**Tech Stack:** Python 3.14, pytest, requests, python-dotenv, playwright (existing login), websockets, sqlite3, OpenRouter chat completions.

---

## File map

| Path | Responsibility |
| --- | --- |
| `bot/settings.py` | Env defaults from the spec |
| `bot/types.py` | `Bar`, `Snapshot`, `TradeIntent`, `GateResult` |
| `bot/atr.py` | ATR(14) on 15m highs/lows/closes |
| `bot/collar.py` | Pure gate: HOLD, caps, session, ATR stop price |
| `bot/brain.py` | Parse LLM JSON; OpenRouter HTTP |
| `bot/store.py` | SQLite: audit rows, paper fills, bot order ids |
| `bot/hands.py` | Paper sim + live market then trigger 103 |
| `bot/eye.py` | REST snapshot now; WS apply-frame + reconnect |
| `bot/cycle.py` | Timer + wake-on-move orchestration |
| `bot/cli.py` | `python -m bot run` |
| `docs/kcex-spot-api.md` | Append discovered WS URL and frame schema |
| `tests/bot/*.py` | Unit tests, no live orders |
| `tests/fixtures/kcex_ws_frames.jsonl` | Captured or synthetic frames |

Do not replace `kcex/client.py` with CCXT. Extend it only if a tiny helper is missing.

This workspace may not be a git repo yet. Task 1 initializes git so later commit steps work. If `git init` is refused, skip commit steps and keep going.

---

### Task 1: Git + pytest + settings/types

**Files:**
- Create: `bot/__init__.py`
- Create: `bot/settings.py`
- Create: `bot/types.py`
- Create: `tests/bot/test_settings.py`
- Modify: `requirements.txt`
- Modify: `.env.example`

- [ ] **Step 1: Init git if needed**

```bash
cd /Users/alissonryan/code/bot-trade
git status -sb || git init
```

Expected: a git repo exists.

- [ ] **Step 2: Add pytest**

Append to `requirements.txt`:

```
pytest>=8.0.0
websockets>=14.0
```

Run: `.venv/bin/pip install -q -r requirements.txt`

- [ ] **Step 3: Write failing settings test**

```python
# tests/bot/test_settings.py
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.settings import Settings


def test_defaults(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("MODE", "SYMBOL", "CYCLE", "WAKE", "MAX_", "ATR", "LLM_", "OPENROUTER")):
            monkeypatch.delenv(key, raising=False)
    s = Settings.from_env()
    assert s.mode == "paper"
    assert s.symbol == "BTC_USDT"
    assert s.cycle_minutes == 15
    assert s.wake_move_pct == 0.004
    assert s.max_order_usdt == 20.0
    assert s.max_portfolio_pct == 0.05
    assert s.max_day_loss_usdt == 20.0
    assert s.atr_period == 14
    assert s.atr_mult == 2.0
    assert s.min_stop_pct == 0.004
    assert s.max_stop_pct == 0.04
    assert s.llm_daily_budget_usd == 2.0
    assert s.qty_scale == 5
```

- [ ] **Step 4: Run test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_settings.py -v`

Expected: FAIL with `ModuleNotFoundError: bot.settings`

- [ ] **Step 5: Implement settings and types**

`bot/__init__.py` empty.

```python
# bot/types.py
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass
class Snapshot:
    ts_ms: int
    last: float
    bid: float
    ask: float
    spread: float
    bars_15m: list[Bar]
    atr: float | None
    free_usdt: float
    bot_qty: float
    bot_avg_entry: float | None
    ws_ok: bool
    stale: bool
    last_intent_action: str | None = None
    last_bot_pnl_usdt: float = 0.0


@dataclass
class TradeIntent:
    action: str
    confidence: float
    reason: str
    regime: str


@dataclass
class GateResult:
    ok: bool
    rule: str
    action: str
    qty: str | None = None
    notional: float | None = None
    stop_price: str | None = None
```

```python
# bot/settings.py
from __future__ import annotations

import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    mode: str
    symbol: str
    cycle_minutes: int
    wake_move_pct: float
    max_order_usdt: float
    max_portfolio_pct: float
    max_day_loss_usdt: float
    atr_period: int
    atr_mult: float
    min_stop_pct: float
    max_stop_pct: float
    llm_daily_budget_usd: float
    llm_model: str
    openrouter_api_key: str
    openrouter_base_url: str
    qty_scale: int
    paper_slippage_bps: float
    ws_url: str
    stale_ms: int

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            mode=os.getenv("MODE", "paper").strip().lower(),
            symbol=os.getenv("SYMBOL", "BTC_USDT").strip(),
            cycle_minutes=_i("CYCLE_MINUTES", 15),
            wake_move_pct=_f("WAKE_MOVE_PCT", 0.004),
            max_order_usdt=_f("MAX_ORDER_USDT", 20),
            max_portfolio_pct=_f("MAX_PORTFOLIO_PCT", 0.05),
            max_day_loss_usdt=_f("MAX_DAY_LOSS_USDT", 20),
            atr_period=_i("ATR_PERIOD", 14),
            atr_mult=_f("ATR_MULT", 2.0),
            min_stop_pct=_f("MIN_STOP_PCT", 0.004),
            max_stop_pct=_f("MAX_STOP_PCT", 0.04),
            llm_daily_budget_usd=_f("LLM_DAILY_BUDGET_USD", 2.0),
            llm_model=os.getenv("LLM_MODEL", "").strip(),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            openrouter_base_url=os.getenv(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ).rstrip("/"),
            qty_scale=_i("QTY_SCALE", 5),
            paper_slippage_bps=_f("PAPER_SLIPPAGE_BPS", 5.0),
            ws_url=os.getenv("KCEX_WS_URL", "").strip(),
            stale_ms=_i("STALE_MS", 30000),
        )
```

Append to `.env.example`:

```
MODE=paper
SYMBOL=BTC_USDT
CYCLE_MINUTES=15
WAKE_MOVE_PCT=0.004
MAX_ORDER_USDT=20
MAX_PORTFOLIO_PCT=0.05
MAX_DAY_LOSS_USDT=20
ATR_PERIOD=14
ATR_MULT=2.0
MIN_STOP_PCT=0.004
MAX_STOP_PCT=0.04
LLM_DAILY_BUDGET_USD=2
OPENROUTER_API_KEY=
LLM_MODEL=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
KCEX_WS_URL=
```

- [ ] **Step 6: Run tests**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_settings.py -v`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add bot tests/bot/test_settings.py requirements.txt .env.example
git commit -m "feat: add bot settings and snapshot types"
```

---

### Task 2: ATR

**Files:**
- Create: `bot/atr.py`
- Create: `tests/bot/test_atr.py`

- [ ] **Step 1: Write failing test**

```python
# tests/bot/test_atr.py
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.atr import atr
from bot.types import Bar


def test_atr_needs_period_plus_one_bars():
    bars = [Bar(t=i, o=10, h=12, l=9, c=11) for i in range(14)]
    assert atr(bars, period=14) is None


def test_atr_constant_range():
    bars = []
    for i in range(20):
        bars.append(Bar(t=i, o=100, h=102, l=100, c=101))
    value = atr(bars, period=14)
    assert value is not None
    assert abs(value - 2.0) < 1e-6
```

- [ ] **Step 2: Run to fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_atr.py -v`

Expected: FAIL `ModuleNotFoundError: bot.atr`

- [ ] **Step 3: Implement**

```python
# bot/atr.py
from __future__ import annotations

from bot.types import Bar


def atr(bars: list[Bar], period: int = 14) -> float | None:
    if period < 1 or len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        high = bars[i].h
        low = bars[i].l
        prev_close = bars[i - 1].c
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    window = trs[-period:]
    if len(window) < period:
        return None
    return sum(window) / period
```

- [ ] **Step 4: Run to pass**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_atr.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/atr.py tests/bot/test_atr.py
git commit -m "feat: compute ATR from 15m bars"
```

---

### Task 3: Collar (pure gate)

**Files:**
- Create: `bot/collar.py`
- Create: `tests/bot/test_collar.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/bot/test_collar.py
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.collar import decide
from bot.settings import Settings
from bot.types import Bar, GateResult, Snapshot, TradeIntent


def _settings(**kwargs) -> Settings:
    base = Settings.from_env()
    data = base.__dict__.copy()
    data.update(kwargs)
    return Settings(**data)


def _snap(**kwargs) -> Snapshot:
    bars = [Bar(t=i, o=100, h=101, l=99, c=100) for i in range(20)]
    fields = dict(
        ts_ms=1,
        last=100_000.0,
        bid=99_999.0,
        ask=100_001.0,
        spread=2.0,
        bars_15m=bars,
        atr=500.0,
        free_usdt=450.0,
        bot_qty=0.0,
        bot_avg_entry=None,
        ws_ok=True,
        stale=False,
    )
    fields.update(kwargs)
    return Snapshot(**fields)


def test_hold_is_not_ok():
    r = decide(
        TradeIntent("HOLD", 0.9, "wait", "range"),
        _snap(),
        _settings(),
        session_ok=True,
        day_pnl_usdt=0.0,
    )
    assert r.ok is False
    assert r.rule == "hold"


def test_reject_wrong_symbol():
    r = decide(
        TradeIntent("BUY", 0.8, "go", "trend"),
        _snap(),
        _settings(symbol="ETH_USDT"),
        session_ok=True,
        day_pnl_usdt=0.0,
    )
    assert r.ok is False
    assert r.rule == "symbol"


def test_reject_bad_session():
    r = decide(
        TradeIntent("BUY", 0.8, "go", "trend"),
        _snap(),
        _settings(),
        session_ok=False,
        day_pnl_usdt=0.0,
    )
    assert r.rule == "session"


def test_reject_second_position_on_buy():
    r = decide(
        TradeIntent("BUY", 0.8, "go", "trend"),
        _snap(bot_qty=0.0002, bot_avg_entry=80_000),
        _settings(),
        session_ok=True,
        day_pnl_usdt=0.0,
    )
    assert r.rule == "already_long"


def test_sell_flat_is_hold():
    r = decide(
        TradeIntent("SELL", 0.8, "out", "trend"),
        _snap(bot_qty=0.0),
        _settings(),
        session_ok=True,
        day_pnl_usdt=0.0,
    )
    assert r.rule == "flat"


def test_buy_caps_notional_at_20():
    r = decide(
        TradeIntent("BUY", 0.8, "go", "trend"),
        _snap(free_usdt=450, last=80_000, atr=400),
        _settings(),
        session_ok=True,
        day_pnl_usdt=0.0,
    )
    assert r.ok is True
    assert r.notional == 20.0
    assert r.qty == "0.00025"
    assert r.stop_price is not None
    stop = float(r.stop_price)
    assert stop < 80_000


def test_day_loss_halts():
    r = decide(
        TradeIntent("BUY", 0.8, "go", "trend"),
        _snap(),
        _settings(),
        session_ok=True,
        day_pnl_usdt=-20.0,
    )
    assert r.rule == "day_loss"


def test_missing_atr_rejects_buy():
    r = decide(
        TradeIntent("BUY", 0.8, "go", "trend"),
        _snap(atr=None),
        _settings(),
        session_ok=True,
        day_pnl_usdt=0.0,
    )
    assert r.rule == "atr"


def test_stale_market_rejects():
    r = decide(
        TradeIntent("BUY", 0.8, "go", "trend"),
        _snap(stale=True),
        _settings(),
        session_ok=True,
        day_pnl_usdt=0.0,
    )
    assert r.rule == "stale"


def test_sell_long_closes_without_new_stop():
    r = decide(
        TradeIntent("SELL", 0.7, "exit", "trend"),
        _snap(bot_qty=0.00025, bot_avg_entry=80_000, last=81_000),
        _settings(),
        session_ok=True,
        day_pnl_usdt=0.0,
    )
    assert r.ok is True
    assert r.qty == "0.00025"
    assert r.stop_price is None
```

- [ ] **Step 2: Run to fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_collar.py -v`

Expected: FAIL missing `bot.collar`

- [ ] **Step 3: Implement collar**

```python
# bot/collar.py
from __future__ import annotations

from bot.settings import Settings
from bot.types import GateResult, Snapshot, TradeIntent

ALLOWED_ACTIONS = {"BUY", "SELL", "HOLD"}


def _round_qty(qty: float, scale: int) -> str:
    q = round(qty, scale)
    fmt = f"{{:.{scale}f}}"
    return fmt.format(q)


def _stop_price(entry: float, atr_value: float, settings: Settings) -> str:
    raw = settings.atr_mult * atr_value
    lo = entry * settings.min_stop_pct
    hi = entry * settings.max_stop_pct
    dist = min(max(raw, lo), hi)
    return f"{entry - dist:.2f}"


def decide(
    intent: TradeIntent,
    snap: Snapshot,
    settings: Settings,
    *,
    session_ok: bool,
    day_pnl_usdt: float,
) -> GateResult:
    if settings.symbol != "BTC_USDT":
        return GateResult(False, "symbol", intent.action)
    if settings.mode not in {"paper", "live"}:
        return GateResult(False, "mode", intent.action)
    if not session_ok:
        return GateResult(False, "session", intent.action)
    if snap.stale:
        return GateResult(False, "stale", intent.action)
    if intent.action not in ALLOWED_ACTIONS:
        return GateResult(False, "action", intent.action)
    if intent.action == "HOLD":
        return GateResult(False, "hold", "HOLD")
    if day_pnl_usdt <= -abs(settings.max_day_loss_usdt):
        return GateResult(False, "day_loss", intent.action)

    if intent.action == "SELL":
        if snap.bot_qty <= 0:
            return GateResult(False, "flat", "SELL")
        return GateResult(
            True,
            "ok_close",
            "SELL",
            qty=_round_qty(snap.bot_qty, settings.qty_scale),
            notional=round(snap.bot_qty * snap.last, 8),
            stop_price=None,
        )

    if snap.bot_qty > 0:
        return GateResult(False, "already_long", "BUY")
    if snap.atr is None or snap.atr <= 0 or snap.last <= 0:
        return GateResult(False, "atr", "BUY")

    cap_pct = settings.max_portfolio_pct * snap.free_usdt
    notional = min(settings.max_order_usdt, cap_pct)
    if notional <= 0:
        return GateResult(False, "no_cash", "BUY")
    qty = notional / snap.last
    qty_s = _round_qty(qty, settings.qty_scale)
    if float(qty_s) <= 0:
        return GateResult(False, "dust", "BUY")
    notional = float(qty_s) * snap.last
    stop = _stop_price(snap.last, snap.atr, settings)
    return GateResult(True, "ok_buy", "BUY", qty=qty_s, notional=notional, stop_price=stop)
```

- [ ] **Step 4: Run to pass**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_collar.py tests/bot/test_atr.py -v`

Expected: PASS. If `test_buy_caps_notional_at_20` qty string differs by rounding, fix `_round_qty` so `20/80000` is `0.00025`.

- [ ] **Step 5: Commit**

```bash
git add bot/collar.py tests/bot/test_collar.py
git commit -m "feat: add deterministic risk collar"
```

---

### Task 4: Brain — parse + OpenRouter

**Files:**
- Create: `bot/brain.py`
- Create: `tests/bot/test_brain.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/bot/test_brain.py
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.brain import Budget, parse_intent, think
from bot.types import Snapshot, TradeIntent, Bar


def test_parse_strips_fences_and_extra_keys():
    raw = """```json
    {"action":"BUY","confidence":0.7,"reason":"breakout","regime":"trend","qty":99}
    ```"""
    intent = parse_intent(raw)
    assert intent == TradeIntent("BUY", 0.7, "breakout", "trend")


def test_parse_rejects_bad_action():
    assert parse_intent('{"action":"YEET","confidence":1,"reason":"x","regime":"trend"}') is None


def test_parse_truncates_reason():
    reason = "n" * 400
    intent = parse_intent(
        '{"action":"HOLD","confidence":0.1,"reason":"%s","regime":"unknown"}' % reason
    )
    assert intent is not None
    assert len(intent.reason) == 240


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http")


def test_think_returns_none_over_budget():
    budget = Budget(spent_usd=2.0, cap_usd=2.0, day="2026-09-04")
    called = []

    def post(*args, **kwargs):
        called.append(1)
        return FakeResp({})

    snap = Snapshot(
        ts_ms=1, last=1, bid=1, ask=1, spread=0, bars_15m=[], atr=1,
        free_usdt=1, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )
    from bot.settings import Settings
    s = Settings.from_env()
    out = think(snap, s, budget, http_post=post)
    assert out is None
    assert called == []
```

- [ ] **Step 2: Run to fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_brain.py -v`

Expected: FAIL missing `bot.brain`

- [ ] **Step 3: Implement**

```python
# bot/brain.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

import requests

from bot.settings import Settings
from bot.types import Snapshot, TradeIntent

ACTIONS = {"BUY", "SELL", "HOLD"}
REGIMES = {"trend", "range", "shock", "unknown"}
FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)

SYSTEM = (
    "You are a BTC/USDT spot decision module. "
    "Reply with JSON only: action (BUY|SELL|HOLD), confidence (0-1), "
    "reason (<=240 chars), regime (trend|range|shock|unknown). "
    "Do not output quantity, stop, or price. HOLD if unsure."
)


@dataclass
class Budget:
    spent_usd: float
    cap_usd: float
    day: str

    def remaining(self) -> float:
        return self.cap_usd - self.spent_usd


def parse_intent(text: str) -> TradeIntent | None:
    raw = text.strip()
    m = FENCE.search(raw)
    if m:
        raw = m.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action", "")).upper()
    if action not in ACTIONS:
        return None
    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    conf = min(max(conf, 0.0), 1.0)
    reason = str(data.get("reason", ""))[:240]
    regime = str(data.get("regime", "unknown")).lower()
    if regime not in REGIMES:
        regime = "unknown"
    return TradeIntent(action, conf, reason, regime)


def _user_payload(snap: Snapshot) -> str:
    last_bars = [
        {"t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c}
        for b in snap.bars_15m[-20:]
    ]
    return json.dumps(
        {
            "last": snap.last,
            "bid": snap.bid,
            "ask": snap.ask,
            "spread": snap.spread,
            "atr": snap.atr,
            "free_usdt": snap.free_usdt,
            "bot_qty": snap.bot_qty,
            "bot_avg_entry": snap.bot_avg_entry,
            "last_intent": snap.last_intent_action,
            "last_bot_pnl_usdt": snap.last_bot_pnl_usdt,
            "bars_15m": last_bars,
        },
        separators=(",", ":"),
    )


def think(
    snap: Snapshot,
    settings: Settings,
    budget: Budget,
    *,
    http_post: Callable[..., Any] | None = None,
) -> TradeIntent | None:
    if budget.remaining() <= 0:
        return None
    if not settings.openrouter_api_key or not settings.llm_model:
        return None
    post = http_post or requests.post
    url = f"{settings.openrouter_base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.llm_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": _user_payload(snap)},
        ],
    }
    try:
        resp = post(url, headers=headers, json=body, timeout=45)
        resp.raise_for_status()
        payload = resp.json()
        text = payload["choices"][0]["message"]["content"]
    except Exception:
        return None
    return parse_intent(text)
```

- [ ] **Step 4: Run to pass**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_brain.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/brain.py tests/bot/test_brain.py
git commit -m "feat: parse TradeIntent and call OpenRouter"
```

---

### Task 5: SQLite store

**Files:**
- Create: `bot/store.py`
- Create: `tests/bot/test_store.py`

- [ ] **Step 1: Write failing test**

```python
# tests/bot/test_store.py
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.store import Store
from bot.types import GateResult, TradeIntent


def test_audit_and_bot_order_ids(tmp_path):
    db = tmp_path / "bot.db"
    store = Store(db)
    store.append_audit(
        intent=TradeIntent("BUY", 0.5, "x", "trend"),
        gate=GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79000.00"),
        mode="paper",
        order_id="bot-1",
    )
    store.remember_order("bot-1")
    store.remember_order("bot-stop-1")
    assert store.is_bot_order("bot-1") is True
    assert store.is_bot_order("C02__723550870020620296064") is False
    assert store.day_pnl("2026-09-04") == 0.0
    store.add_fill("2026-09-04", 1.5)
    store.add_fill("2026-09-04", -0.5)
    assert store.day_pnl("2026-09-04") == 1.0
```

- [ ] **Step 2: Run to fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_store.py -v`

Expected: FAIL missing `bot.store`

- [ ] **Step 3: Implement**

```python
# bot/store.py
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bot.types import GateResult, TradeIntent


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY,
                ts TEXT,
                action TEXT,
                ok INTEGER,
                rule TEXT,
                payload TEXT
            )"""
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS bot_orders (order_id TEXT PRIMARY KEY)"
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY,
                day TEXT,
                pnl REAL
            )"""
        )
        self._conn.commit()

    def append_audit(
        self,
        intent: TradeIntent,
        gate: GateResult,
        mode: str,
        order_id: str | None = None,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(
            {
                "mode": mode,
                "intent": intent.__dict__,
                "gate": gate.__dict__,
                "order_id": order_id,
            }
        )
        self._conn.execute(
            "INSERT INTO audit(ts, action, ok, rule, payload) VALUES (?,?,?,?,?)",
            (ts, intent.action, int(gate.ok), gate.rule, payload),
        )
        self._conn.commit()

    def remember_order(self, order_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO bot_orders(order_id) VALUES (?)", (order_id,)
        )
        self._conn.commit()

    def is_bot_order(self, order_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM bot_orders WHERE order_id=?", (order_id,)
        ).fetchone()
        return row is not None

    def add_fill(self, day: str, pnl: float) -> None:
        self._conn.execute("INSERT INTO fills(day, pnl) VALUES (?,?)", (day, pnl))
        self._conn.commit()

    def day_pnl(self, day: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM fills WHERE day=?", (day,)
        ).fetchone()
        return float(row[0])
```

- [ ] **Step 4: Run to pass**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_store.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/store.py tests/bot/test_store.py
git commit -m "feat: sqlite audit log and bot order id set"
```

---

### Task 6: Paper hands

**Files:**
- Create: `bot/hands.py`
- Create: `tests/bot/test_hands_paper.py`

- [ ] **Step 1: Write failing test**

```python
# tests/bot/test_hands_paper.py
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.hands import PaperHands, Position
from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, Snapshot, Bar


def _snap(last=80000.0, bid=79999.0, ask=80001.0):
    return Snapshot(
        ts_ms=1, last=last, bid=bid, ask=ask, spread=ask - bid,
        bars_15m=[Bar(1, last, last, last, last)], atr=400,
        free_usdt=450, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )


def test_paper_buy_then_stop(tmp_path):
    store = Store(tmp_path / "x.db")
    hands = PaperHands(Settings.from_env(), store)
    gate = GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79200.00")
    pos = hands.execute(gate, _snap(ask=80010))
    assert pos.qty == 0.00025
    assert pos.stop_price == 79200.00
    assert pos.entry > 80000
    stopped = hands.mark(_snap(last=79100, bid=79090, ask=79110))
    assert stopped.qty == 0.0
    assert hands.position.qty == 0.0
    assert store.day_pnl(hands.today()) < 0
```

- [ ] **Step 2: Run to fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_hands_paper.py -v`

Expected: FAIL missing `bot.hands`

- [ ] **Step 3: Implement paper executor inside `bot/hands.py`**

```python
# bot/hands.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, Snapshot
from kcex.client import KcexClient


@dataclass
class Position:
    qty: float = 0.0
    entry: float = 0.0
    stop_price: float | None = None


class PaperHands:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.position = Position()

    def today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def execute(self, gate: GateResult, snap: Snapshot) -> Position:
        slip = self.settings.paper_slippage_bps / 10_000.0
        if gate.action == "BUY" and gate.qty:
            px = snap.ask * (1 + slip)
            qty = float(gate.qty)
            self.position = Position(qty=qty, entry=px, stop_price=float(gate.stop_price or 0) or None)
            self.store.remember_order("paper-entry")
            if self.position.stop_price:
                self.store.remember_order("paper-stop")
        elif gate.action == "SELL" and self.position.qty > 0:
            px = snap.bid * (1 - slip)
            pnl = (px - self.position.entry) * self.position.qty
            self.store.add_fill(self.today(), pnl)
            self.position = Position()
        return self.position

    def mark(self, snap: Snapshot) -> Position:
        if self.position.qty > 0 and self.position.stop_price is not None:
            if snap.last <= self.position.stop_price or snap.bid <= self.position.stop_price:
                px = min(snap.bid, self.position.stop_price)
                pnl = (px - self.position.entry) * self.position.qty
                self.store.add_fill(self.today(), pnl)
                self.position = Position()
        return self.position


class LiveHands:
    """Filled in Task 7."""

    def __init__(self, settings: Settings, store: Store, client: KcexClient):
        self.settings = settings
        self.store = store
        self.client = client
        self.position = Position()

    def execute(self, gate: GateResult, snap: Snapshot) -> Position:
        raise NotImplementedError
```

- [ ] **Step 4: Run to pass**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_hands_paper.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/hands.py tests/bot/test_hands_paper.py
git commit -m "feat: paper executor with simulated ATR stop"
```

---

### Task 7: Live hands with mocked KcexClient

**Files:**
- Modify: `bot/hands.py`
- Create: `tests/bot/test_hands_live.py`

- [ ] **Step 1: Write failing test**

```python
# tests/bot/test_hands_live.py
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.hands import LiveHands
from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, Snapshot, Bar


class FakeClient:
    def __init__(self):
        self.calls = []

    def place_market(self, **kwargs):
        self.calls.append(("market", kwargs))
        return {"code": 0, "data": "oid-m"}

    def place_trigger(self, **kwargs):
        self.calls.append(("trigger", kwargs))
        return {"code": 0, "data": "oid-t"}

    def cancel_order(self, order_id: str):
        self.calls.append(("cancel", order_id))
        return {"code": 200}


def test_live_buy_places_market_then_trigger(tmp_path):
    store = Store(tmp_path / "l.db")
    client = FakeClient()
    hands = LiveHands(Settings.from_env(), store, client)
    snap = Snapshot(
        ts_ms=1, last=80000, bid=79999, ask=80001, spread=2,
        bars_15m=[Bar(1, 80000, 80000, 80000, 80000)], atr=400,
        free_usdt=450, bot_qty=0, bot_avg_entry=None, ws_ok=True, stale=False,
    )
    gate = GateResult(True, "ok_buy", "BUY", qty="0.00025", notional=20, stop_price="79200.00")
    hands.execute(gate, snap)
    assert client.calls[0][0] == "market"
    assert client.calls[1][0] == "trigger"
    assert client.calls[1][1]["order_type"] == 103 or client.calls[1][1].get("market_order") is True
    assert store.is_bot_order("oid-m")
    assert store.is_bot_order("oid-t")


def test_live_never_cancels_foreign_id(tmp_path):
    store = Store(tmp_path / "l.db")
    client = FakeClient()
    hands = LiveHands(Settings.from_env(), store, client)
    foreign = "C02__723550870020620296064"
    assert store.is_bot_order(foreign) is False
    ok = hands.cancel_if_ours(foreign)
    assert ok is False
    assert client.calls == []
```

- [ ] **Step 2: Run to fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_hands_live.py -v`

Expected: FAIL `NotImplementedError` or missing `cancel_if_ours`

- [ ] **Step 3: Implement LiveHands.execute and cancel_if_ours**

Replace `LiveHands` in `bot/hands.py` with:

```python
class LiveHands:
    def __init__(self, settings: Settings, store: Store, client: KcexClient):
        self.settings = settings
        self.store = store
        self.client = client
        self.position = Position()
        self.entry_order_id: str | None = None
        self.stop_order_id: str | None = None

    def cancel_if_ours(self, order_id: str) -> bool:
        if not self.store.is_bot_order(order_id):
            return False
        self.client.cancel_order(order_id)
        return True

    def execute(self, gate: GateResult, snap: Snapshot) -> Position:
        if self.settings.mode != "live":
            raise RuntimeError("LiveHands requires MODE=live")
        if gate.action == "BUY" and gate.qty and gate.stop_price:
            market = self.client.place_market(
                currency="BTC",
                market="USDT",
                side="BUY",
                price=str(snap.last),
                quantity=gate.qty,
            )
            entry_id = str(market.get("data") or market)
            self.store.remember_order(entry_id)
            self.entry_order_id = entry_id
            try:
                trig = self.client.place_trigger(
                    currency="BTC",
                    market="USDT",
                    side="SELL",
                    trigger_price=gate.stop_price,
                    trigger_type="LE",
                    quantity=gate.qty,
                    amount="0",
                    market_order=True,
                )
            except Exception:
                self.client.place_market(
                    currency="BTC",
                    market="USDT",
                    side="SELL",
                    price=str(snap.last),
                    quantity=gate.qty,
                )
                self.position = Position()
                return self.position
            stop_id = str(trig.get("data") or trig)
            self.store.remember_order(stop_id)
            self.stop_order_id = stop_id
            self.position = Position(
                qty=float(gate.qty),
                entry=snap.last,
                stop_price=float(gate.stop_price),
            )
        elif gate.action == "SELL" and gate.qty:
            if self.stop_order_id:
                self.cancel_if_ours(self.stop_order_id)
            self.client.place_market(
                currency="BTC",
                market="USDT",
                side="SELL",
                price=str(snap.last),
                quantity=gate.qty,
            )
            self.position = Position()
            self.stop_order_id = None
        return self.position
```

`place_market` / `place_trigger` already exist on `kcex.client.KcexClient`. FakeClient must use the same kwargs: `market_order=True` for stop-market.

- [ ] **Step 4: Run to pass**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_hands_live.py tests/bot/test_hands_paper.py -v`

Expected: PASS. Adjust the trigger kwargs assert to match `place_trigger(..., market_order=True)`.

- [ ] **Step 5: Commit**

```bash
git add bot/hands.py tests/bot/test_hands_live.py
git commit -m "feat: live market entry plus ATR stop-market trigger"
```

---

### Task 8: Eye REST snapshot (works before WS mapping)

**Files:**
- Create: `bot/eye.py`
- Create: `tests/bot/test_eye_rest.py`

- [ ] **Step 1: Write failing test using a fake client returning recorded REST shapes**

```python
# tests/bot/test_eye_rest.py
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.eye import Eye
from bot.settings import Settings


class FakeKcex:
    def ticker(self, symbol="BTC_USDT"):
        return {"data": {"c": "80000.0"}, "code": 0}

    def depth(self, symbol="BTC_USDT", price_precision="0.01"):
        return {
            "data": {"data": {"bids": [{"p": "79999", "q": "1"}], "asks": [{"p": "80001", "q": "1"}]}},
            "code": 200,
        }

    def kline(self, symbol="BTC_USDT", interval="Min15", start=0, end=0, open_price_mode="LAST_CLOSE"):
        t = [1 + i * 900 for i in range(20)]
        return {"data": {"t": t, "o": [100]*20, "h": [102]*20, "l": [99]*20, "c": [101]*20, "v": [1]*20}}

    def balances(self, currencies="BTC,USDT"):
        return {"data": [{"currency": "USDT", "available": "450.0", "total": "450.0", "frozen": "0"}]}

    def ws_token(self):
        return {"code": 0, "data": {"wsToken": "abc"}}


def test_rest_snapshot_not_stale():
    eye = Eye(FakeKcex(), Settings.from_env(), bot_qty=0.0, bot_avg_entry=None)
    snap = eye.snapshot_rest()
    assert snap.last == 80000.0
    assert snap.bid == 79999.0
    assert snap.ask == 80001.0
    assert snap.free_usdt == 450.0
    assert snap.atr is not None
    assert snap.stale is False
    assert snap.ws_ok is False
```

- [ ] **Step 2: Run to fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_eye_rest.py -v`

Expected: FAIL missing `bot.eye`

- [ ] **Step 3: Implement Eye.snapshot_rest and apply_ws_price**

```python
# bot/eye.py
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
```

- [ ] **Step 4: Run to pass**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_eye_rest.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/eye.py tests/bot/test_eye_rest.py
git commit -m "feat: REST market snapshot for the eye"
```

---

### Task 9: Map KCEX WebSocket and parse frames

**Files:**
- Modify: `docs/kcex-spot-api.md`
- Create: `tests/fixtures/kcex_ws_frames.jsonl`
- Create: `tests/bot/test_eye_ws_parse.py`
- Modify: `bot/eye.py` (add `apply_frame`)

- [ ] **Step 1: Capture live frames**

With Chrome DevTools MCP (or Playwright) on `https://www.kcex.com/exchange/BTC_USDT` while logged in:

1. `list_network_requests` with `resourceTypes: ["websocket"]`.
2. If empty, `evaluate_script` that patches `window.WebSocket` to log `url` and first messages, then reload.
3. Also search JS for `wss://` as in the original reverse-engineering session.
4. Call `GET /uc/user_api/ws_token` via the existing client and try the discovered URL with `?token=` or subscribe JSON.

Write what you find at the end of `docs/kcex-spot-api.md` under `## WebSocket` with: URL, auth, subscribe payload, example ticker frame, example private fill frame.

If the public stream still cannot be opened, document that and keep REST as fallback. Still add `apply_frame` so a later URL drop-in works.

- [ ] **Step 2: Write a fixture + failing parse test**

Create `tests/fixtures/kcex_ws_frames.jsonl` with at least one line in this **canonical** shape (map live frames into this in `apply_frame`):

```json
{"ch":"ticker","symbol":"BTC_USDT","last":"80000.1","bid":"80000.0","ask":"80000.2"}
```

```python
# tests/bot/test_eye_ws_parse.py
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.eye import Eye
from bot.settings import Settings


class FakeKcex:
    pass


def test_apply_frame_updates_last():
    eye = Eye(FakeKcex(), Settings.from_env())
    line = Path("tests/fixtures/kcex_ws_frames.jsonl").read_text().splitlines()[0]
    eye.apply_frame(json.loads(line))
    assert eye.last == 80000.1
    assert eye.ws_ok is True
    assert eye.bid == 80000.0
```

- [ ] **Step 3: Run to fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_eye_ws_parse.py -v`

Expected: FAIL `apply_frame` missing

- [ ] **Step 4: Implement apply_frame**

Add to `Eye`:

```python
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
```

- [ ] **Step 5: Run to pass**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_eye_ws_parse.py tests/bot/test_eye_rest.py -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add bot/eye.py tests/bot/test_eye_ws_parse.py tests/fixtures/kcex_ws_frames.jsonl docs/kcex-spot-api.md
git commit -m "feat: parse KCEX websocket ticker frames"
```

---

### Task 10: Cycle + CLI

**Files:**
- Create: `bot/cycle.py`
- Create: `bot/cli.py`
- Create: `bot/__main__.py`
- Create: `tests/bot/test_cycle.py`

- [ ] **Step 1: Write failing scheduler tests**

```python
# tests/bot/test_cycle.py
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.cycle import due
from bot.settings import Settings


def test_due_on_timer():
    s = Settings.from_env()
    assert due(now_ms=1_000_000, last_llm_ms=1_000_000 - 15 * 60_000, last_px=100, px=100, settings=s) is True


def test_not_due_early():
    s = Settings.from_env()
    assert due(now_ms=1_000_000, last_llm_ms=1_000_000 - 60_000, last_px=100, px=100, settings=s) is False


def test_due_on_wake_move():
    s = Settings.from_env()
    assert due(now_ms=1_000_000, last_llm_ms=1_000_000 - 1000, last_px=100, px=100.5, settings=s) is True
```

- [ ] **Step 2: Run to fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_cycle.py -v`

Expected: FAIL missing `bot.cycle`

- [ ] **Step 3: Implement due + run_once + cli**

```python
# bot/cycle.py
from __future__ import annotations

import time
from datetime import datetime, timezone

from bot.brain import Budget, think
from bot.collar import decide
from bot.eye import Eye
from bot.hands import LiveHands, PaperHands
from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, TradeIntent
from kcex.client import KcexClient, KcexError


def due(now_ms: int, last_llm_ms: int, last_px: float, px: float, settings: Settings) -> bool:
    if last_llm_ms == 0:
        return True
    if now_ms - last_llm_ms >= settings.cycle_minutes * 60_000:
        return True
    if last_px > 0 and abs(px / last_px - 1.0) >= settings.wake_move_pct:
        return True
    return False


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def run_once(
    *,
    settings: Settings,
    eye: Eye,
    store: Store,
    client: KcexClient,
    hands: PaperHands | LiveHands,
    budget: Budget,
    last_llm_ms: int,
    last_px: float,
) -> tuple[int, float, GateResult | None]:
    snap = eye.snapshot()
    if snap.last <= 0:
        snap = eye.snapshot_rest()
    now = int(time.time() * 1000)
    if not due(now, last_llm_ms, last_px, snap.last, settings):
        if isinstance(hands, PaperHands):
            hands.mark(snap)
        return last_llm_ms, last_px, None
    session_ok = True
    try:
        if settings.mode == "live":
            client.user_info()
    except KcexError as exc:
        if exc.payload.get("code") == 401:
            session_ok = False
        else:
            session_ok = False
    except Exception:
        session_ok = settings.mode != "live"
    intent = think(snap, settings, budget)
    if intent is None:
        intent = TradeIntent("HOLD", 0.0, "no_llm", "unknown")
    gate = decide(
        intent,
        snap,
        settings,
        session_ok=session_ok,
        day_pnl_usdt=store.day_pnl(utc_day()),
    )
    order_id = None
    if gate.ok:
        hands.execute(gate, snap)
        eye.bot_qty = hands.position.qty
        eye.bot_avg_entry = hands.position.entry or None
    store.append_audit(intent, gate, settings.mode, order_id)
    eye.last_intent_action = intent.action
    return now, snap.last, gate
```

```python
# bot/cli.py
from __future__ import annotations

import argparse
import time
from pathlib import Path

from dotenv import load_dotenv

from bot.brain import Budget
from bot.cycle import run_once, utc_day
from bot.eye import Eye
from bot.hands import LiveHands, PaperHands
from bot.settings import Settings
from bot.store import Store
from kcex.client import KcexClient
from kcex.login import require_live_token


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["run"])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    token = ""
    if settings.mode == "live":
        token = require_live_token()
    client = KcexClient(token=token or None)
    store = Store(Path("data/bot.db"))
    eye = Eye(client, settings)
    if settings.mode == "live":
        hands: PaperHands | LiveHands = LiveHands(settings, store, client)
    else:
        hands = PaperHands(settings, store)
    budget = Budget(spent_usd=0.0, cap_usd=settings.llm_daily_budget_usd, day=utc_day())
    last_llm_ms = 0
    last_px = 0.0
    eye.snapshot_rest()
    while True:
        last_llm_ms, last_px, gate = run_once(
            settings=settings,
            eye=eye,
            store=store,
            client=client,
            hands=hands,
            budget=budget,
            last_llm_ms=last_llm_ms,
            last_px=last_px,
        )
        if gate is not None:
            print(gate)
        if args.once:
            return 0
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
```

```python
# bot/__main__.py
from bot.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run unit tests**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot/test_cycle.py -v`

Expected: PASS (`100.5` is 0.5% > 0.4%).

- [ ] **Step 5: Smoke paper once without LLM key**

Run: `MODE=paper PYTHONPATH=. .venv/bin/python -m bot run --once`

Expected: prints a `GateResult` with `rule='hold'` or `'no_llm'` path via HOLD. Must not place KCEX orders.

- [ ] **Step 6: Commit**

```bash
git add bot/cycle.py bot/cli.py bot/__main__.py tests/bot/test_cycle.py
git commit -m "feat: run loop with timer and wake-on-move"
```

---

### Task 11: README + full pytest + WS connect stub

**Files:**
- Modify: `README.md`
- Modify: `bot/eye.py` (optional `run_ws` loop using `websockets` if `KCEX_WS_URL` set)

- [ ] **Step 1: Document how to run**

Add to `README.md`:

```markdown
## Bot (paper)

```bash
# .env: OPENROUTER_API_KEY, LLM_MODEL=google/gemini-2.5-flash
# MODE=paper
PYTHONPATH=. python -m bot run
```

Live: `MODE=live` after `python -m kcex.cli login`. Max 20 USDT, BTC/USDT only, ATR stop on the exchange. Existing manual stops are never cancelled.

WebSocket: set `KCEX_WS_URL` from `docs/kcex-spot-api.md`. Without it the eye polls REST.
```

- [ ] **Step 2: If Task 9 found a URL, add a short `Eye.connect_ws` using the `websockets` package that reads frames and calls `apply_frame`. If not, skip connect and keep REST. Do not invent a URL.**

- [ ] **Step 3: Run full unit suite**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/bot -v`

Expected: all PASS. Zero live orders.

- [ ] **Step 4: Commit**

```bash
git add README.md bot/eye.py
git commit -m "docs: paper run instructions and ws url hook"
```

---

## Spec coverage

| Spec item | Task |
| --- | --- |
| OpenRouter one model | 4, 10 |
| Collar caps / HOLD / session / ATR / day loss | 3 |
| Paper ledger | 6 |
| Live market + trigger 103 | 7 |
| Never cancel foreign ids | 7 |
| REST snapshot | 8 |
| WS primary + document | 9, 11 |
| REST fallback | 8, 10 |
| 15 min + 0.4% wake | 10 |
| MODE paper/live | 1, 7, 10 |
| Existing BTC untouched | 5, 7 |
| Audit log | 5 |
| LLM budget skip | 4 |
| Stale no trade | 3 |
| Trigger fail flatten | 7 |
| Config keys | 1 |
| No second LLM / no ETH / no CCXT | out of scope, not tasked |

## Placeholder scan

WS **URL** is discovered in Task 9, not guessed. `apply_frame` uses a canonical fixture so tests do not block on capture. `LiveHands` flatten on trigger exception is specified in Task 7.

## Type consistency

`TradeIntent`, `GateResult`, `Snapshot`, `Bar`, `Settings.from_env()`, `Store.is_bot_order`, `Eye.snapshot` / `snapshot_rest` / `apply_frame`, `due(...)`, `PaperHands.execute/mark`, `LiveHands.execute/cancel_if_ours` are named the same in every task.
