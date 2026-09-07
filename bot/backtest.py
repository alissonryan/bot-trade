"""Deterministic Min15 replay; no live hands, exchange writes, or runtime ledger.

Decide at bar OPEN using only the previous 21 completed bars. Quotes are modeled
with a configurable total spread (default 1 bps); WS is false, fresh REST-like data.
"""
from __future__ import annotations

import math
import hashlib
import json
import random
import sqlite3
import time
from dataclasses import asdict, astuple
from collections import Counter
from datetime import datetime, timezone
from statistics import mean, stdev
from pathlib import Path

import requests

from bot.brain import Budget, ThinkResult, request_body, think_result
from bot.collar import decide, stop_for_entry, take_profit_for_entry
from bot.eye import Eye
from bot.hands import Position, local_exit_reason
from bot.journal import record_decision, deferred_reflection
from bot.settings import Settings
from bot.store import Store
from bot.types import Bar, GateResult, Snapshot, SymbolRules, TradeIntent
from kcex.client import KcexClient

BAR_SECONDS = 900


class CachedBrain:
    """Cache raw provider responses, then run the real brain parser on every replay.

    Cache misses are errors unless network is explicitly enabled. The spend ceiling
    covers this cache's cumulative paid responses, across interrupted/resumed runs.
    A provider charge is known only after a response: reserve fallback cost before
    sending and stop on an overrun (one call can exceed the estimate).
    """

    def __init__(self, path, settings, *, allow_network=False, max_cost_usd=0.0, http_post=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS responses (model TEXT, key TEXT, payload TEXT, cost REAL, PRIMARY KEY(model,key))")
        self.settings = settings
        self.allow_network = allow_network
        self.max_cost_usd = max_cost_usd
        self.http_post = http_post or requests.post
        self.paid_usd = 0.0
        self.spending = Budget(self.db.execute("SELECT COALESCE(SUM(cost),0) FROM responses").fetchone()[0], max_cost_usd, "run")
        self.hits = 0
        self.misses = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def __call__(self, snap: Snapshot, budget: Budget, *, lessons=None, as_of_ms=None) -> ThinkResult:
        if budget.remaining() <= 0:
            raise RuntimeError("historical LLM budget exhausted; incomplete replay")
        body = request_body(snap, self.settings, lessons=lessons, as_of_ms=as_of_ms)
        key = hashlib.sha256(json.dumps([self.settings.openrouter_base_url, body], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        row = self.db.execute("SELECT payload FROM responses WHERE model=? AND key=?", (self.settings.llm_model, key)).fetchone()
        if row:
            payload = json.loads(row[0])
            self.hits += 1
        else:

            if not self.allow_network:
                raise RuntimeError(f"cache miss for {self.settings.llm_model} {key}; enable network explicitly")
            if not self.settings.openrouter_api_key or not self.settings.llm_model:
                raise RuntimeError("LLM configuration missing")
            if self.spending.remaining() < self.settings.llm_fallback_cost_usd:
                raise RuntimeError("LLM cache spend ceiling reached")
            resp = self.http_post(f"{self.settings.openrouter_base_url}/chat/completions", json=body,
                                  headers={"Authorization": f"Bearer {self.settings.openrouter_api_key}", "Content-Type": "application/json"}, timeout=45)
            if resp.status_code >= 400:
                raise RuntimeError(f"LLM HTTP {resp.status_code}; incomplete run, no fabricated HOLD")
            payload = resp.json()
            usage = payload.get("usage") or {}
            cost = float(usage.get("cost", self.settings.llm_fallback_cost_usd))
            if not math.isfinite(cost) or cost < 0:
                raise RuntimeError("invalid provider cost")
            with self.db:
                self.db.execute("INSERT INTO responses VALUES (?,?,?,?)", (self.settings.llm_model, key, json.dumps(payload), cost))
            self.paid_usd += cost
            self.spending.spend(cost)
            self.misses += 1
            if self.spending.remaining() < 0:
                raise RuntimeError("LLM spend ceiling exceeded by last provider response; stopped")

        class Response:
            status_code = 200

            def json(self):
                return payload

        # The placeholder allows offline cache replay without retaining credentials.
        from dataclasses import replace
        settings = replace(self.settings, openrouter_api_key="cached")
        return think_result(snap, settings, budget, http_post=lambda *a, **kw: Response(),
                            lessons=lessons, as_of_ms=as_of_ms)


class History:
    """BTC_USDT Min15 only. Seconds on disk, milliseconds in KCEX requests."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS bars (t INTEGER PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, v REAL)")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def download(self, client, start: int, end: int, *, page_bars: int = 200, now_s: int | None = None):
        if start >= end or page_bars < 1 or start % BAR_SECONDS:
            raise ValueError("invalid history range/page size; start must be Min15 aligned")
        closed_at = min(end, int(time.time()) if now_s is None else now_s)
        for left in range(start, end, page_bars * BAR_SECONDS):
            right = min(end, left + page_bars * BAR_SECONDS)
            data = client.kline("BTC_USDT", interval="Min15", start=left * 1000, end=right * 1000)["data"]
            lengths = {len(data[k]) for k in "tohlc"}
            if len(lengths) != 1:
                raise ValueError("mismatched kline arrays")
            batch = {}
            for i, t in enumerate(data["t"]):
                t = int(t)
                # Drop ONLY forming bars, not the last *closed* bar of each historical page.
                if not left <= t < right or t + BAR_SECONDS > closed_at:
                    continue
                volume = data.get("v") or []
                b = Bar(t, float(data["o"][i]), float(data["h"][i]), float(data["l"][i]),
                        float(data["c"][i]), float(volume[i]) if i < len(volume) else 0)
                if t % BAR_SECONDS or not all(math.isfinite(x) for x in astuple(b)) or not 0 < b.l <= min(b.o, b.c) <= max(b.o, b.c) <= b.h or b.v < 0:
                    raise ValueError("invalid OHLCV bar")
                batch[t] = b
            expected = set(range(left, min(right, closed_at - BAR_SECONDS + 1), BAR_SECONDS))
            if set(batch) != expected:
                raise ValueError(f"missing historical bars in page {left}:{right}")
            with self.db:
                self.db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?)", [astuple(b) for b in batch.values()])

    def load(self, start: int, end: int) -> list[Bar]:
        rows = self.db.execute("SELECT t,o,h,l,c,v FROM bars WHERE t>=? AND t+?<=? ORDER BY t", (start, BAR_SECONDS, end))
        result = [Bar(*row) for row in rows]
        if [b.t for b in result] != list(range(start, end - BAR_SECONDS + 1, BAR_SECONDS)):
            raise ValueError("missing historical bars; download the complete range first")
        return result


def replay_snapshot(
    history: list[Bar], t: int, price: float, settings: Settings, *,
    spread_bps: float = 1.0, cash: float = 450.0, qty: float = 0.0,
    entry: float | None = None, last_action: str | None = None, day_pnl: float = 0.0,
) -> Snapshot:
    # Reuse Eye.snapshot (and therefore bot.atr.atr) to keep the full schema aligned.
    eye = Eye(KcexClient(token=""), settings, bot_qty=qty, bot_avg_entry=entry)  # no I/O methods called
    eye._now_ms = lambda: t * 1000
    eye.last_update_ms = t * 1000
    # bid/ask below are derived synthetically from `price` at this same `t`,
    # so the replayed book is fresh by construction -- depth_update_ms must
    # say so, or every replayed snapshot reads as permanently depth-stale
    # (Eye defaults depth_update_ms to 0 == stale) and take_profit could
    # never fire in the backtest.
    eye.depth_update_ms = t * 1000
    eye.last = price
    eye.bid = price * (1 - spread_bps / 20000)
    eye.ask = price * (1 + spread_bps / 20000)
    eye.bars = history[-21:]
    eye.free_usdt = cash
    eye.last_intent_action = last_action
    eye.last_bot_pnl_usdt = day_pnl
    return eye.snapshot()


def metrics(equity, trades, execution_cost, llm_cost, notional):
    """Close-marked equity, annualized Min15 Sharpe (365 days, zero risk-free).

    Trades are completed round trips; final liquidation counts as end_of_data.
    LLM cost is separate from trading PnL, with an explicit after-LLM net field.
    """
    peak = equity[0]
    dd = dd_pct = 0.0
    for value in equity:
        peak = max(peak, value)
        dd = max(dd, peak - value)
        dd_pct = max(dd_pct, (peak - value) / peak * 100)
    returns = [b / a - 1 for a, b in zip(equity, equity[1:])]
    volatility = stdev(returns) if len(returns) > 1 else 0
    wins = sum(max(0, t["pnl_usdt"]) for t in trades)
    losses = -sum(min(0, t["pnl_usdt"]) for t in trades)
    exits = dict.fromkeys(("stop", "llm_sell", "take_profit", "time_limit", "trailing", "end_of_data"), 0)
    for trade in trades:
        exits[trade["exit_type"]] += 1
    net = equity[-1] - equity[0]
    return {"net_pnl_usdt": net, "net_pnl_pct": net / equity[0] * 100,
            "pnl_pct_order_cap": net / notional * 100 if notional else None,
            "execution_cost_usdt": execution_cost, "llm_cost_usd": llm_cost,
            "net_after_llm_usdt": net - llm_cost,
            "max_drawdown_usdt": dd, "max_drawdown_pct": dd_pct,
            "sharpe": mean(returns) / volatility * math.sqrt(365 * 96) if volatility else None,
            "profit_factor": wins / losses if losses else None,
            "trades": len(trades), "exit_types": exits}


def replay(history: list[Bar], settings: Settings, policy, *, rules: SymbolRules | None = None,
           spread_bps: float = 1.0, slippage_bps: float = 0.0, reflect=None):
    """One decision per Min15 open, no forming/future OHLC in the snapshot.

    Existing gap stops precede the decision; intrabar lows trigger stops after it,
    including newly entered positions. Stops fill at min(open, trigger), less
    half-spread/slippage. Local TP/TTL sample only observed Min15 opens: a high
    alone never promises a local TP fill. Resident SL still uses intrabar lows.
    A local exit suppresses re-entry on that bar; policy is consumed for aligned
    fixed-intent ablations, not a claim to reproduce a counterfactual LLM response.
    """
    if settings.mode != "paper" or settings.symbol != "BTC_USDT":
        raise ValueError("replay requires paper BTC_USDT settings")
    if len(history) <= 21 or any(b.t - a.t != BAR_SECONDS for a, b in zip(history, history[1:])):
        raise ValueError("replay requires contiguous Min15 bars plus 21 warmup bars")
    if not all(math.isfinite(v) and v >= 0 for v in (spread_bps, slippage_bps)) or spread_bps / 20000 + slippage_bps / 10000 >= 1:
        raise ValueError("invalid spread/slippage")
    if settings.paper_starting_usdt <= 0:
        raise ValueError("initial capital must be positive")
    rules = rules or SymbolRules(qty_scale=settings.qty_scale)
    cash = settings.paper_starting_usdt
    qty = entry = stop = entry_fee = 0.0
    entry_t = 0
    last_loss_exit_ms = None
    target = None
    last_action = None
    day = ""
    day_pnl = 0.0
    cost = llm_cost = 0.0
    equity = [cash]
    trades, decisions = [], []
    journal = Store(Path(":memory:")) if settings.journal_enabled else None
    journal_ids = []
    # Research evaluates every bar; monetary authorization lives in CachedBrain.
    budget = Budget(0, settings.llm_daily_budget_usd if journal else math.inf, "")
    buy_factor = (1 + spread_bps / 20000) * (1 + slippage_bps / 10000)
    sell_factor = (1 - spread_bps / 20000) * (1 - slippage_bps / 10000)

    def close(mid, t, reason, *, cooldown_t=None):
        nonlocal cash, qty, entry, stop, cost, day_pnl, entry_fee, target, last_loss_exit_ms
        exit_ms = (t if cooldown_t is None else cooldown_t) * 1000
        price = mid * sell_factor
        fee = qty * price * rules.taker_fee
        pnl = qty * (price - entry) - entry_fee - fee
        if pnl < 0:
            # Only a loss arms the cooldown; a win must not cut a continuation.
            last_loss_exit_ms = exit_ms
        cash += qty * price - fee
        cost += qty * (mid - price) + fee
        day_pnl += pnl
        trades.append({"entry_t": entry_t, "exit_t": t, "qty": qty, "entry_price": entry,
                       "exit_price": price, "pnl_usdt": pnl, "exit_type": reason})
        if journal:
            journal.add_fill(day, pnl, ts=datetime.fromtimestamp(t, timezone.utc).isoformat(),
                             side="SELL", qty=qty, price=price, fee=fee, source=reason,
                             known_ms=exit_ms)
        qty = entry = stop = entry_fee = 0.0
        target = None

    for i in range(21, len(history)):
        bar = history[i]
        current_day = datetime.fromtimestamp(bar.t, timezone.utc).strftime("%Y-%m-%d")
        if current_day != day:
            day, day_pnl = current_day, 0.0
        budget.roll_day(day)
        if qty and bar.o <= stop:
            close(bar.o, bar.t, "stop")
        snap = replay_snapshot(history[max(0, i-21):i], bar.t, bar.o, settings,
                               spread_bps=spread_bps, cash=cash, qty=qty, entry=entry or None,
                               last_action=last_action, day_pnl=day_pnl)
        pos = Position(qty=qty, entry=entry, stop_price=stop, state="OPEN",
                       take_profit_price=target,
                       opened_ts=datetime.fromtimestamp(entry_t, timezone.utc).isoformat())
        barrier = local_exit_reason(pos, snap, settings, bar.t * 1000)
        if barrier:
            close(bar.o, bar.t, barrier)
            snap = replay_snapshot(history[max(0, i-21):i], bar.t, bar.o, settings,
                                   spread_bps=spread_bps, cash=cash, last_action=last_action, day_pnl=day_pnl)
        context = ({"lessons": journal.journal_lessons(as_of_ms=bar.t * 1000), "as_of_ms": bar.t * 1000}
                   if journal else {})
        calls_before = budget.calls
        result = policy(snap, budget, **context)
        if journal and budget.calls == calls_before:
            budget.spend(result.cost_usd)
        llm_cost += result.cost_usd
        intent = result.intent or TradeIntent("HOLD", 0, result.reason, "unknown")
        gate = decide(intent, snap, settings, session_ok=True, day_pnl_usdt=day_pnl,
                      unrealized_pnl_usdt=qty * (snap.bid - entry), rules=rules,
                      last_loss_exit_ms=last_loss_exit_ms, now_ms=bar.t * 1000)
        if barrier:
            gate = GateResult(False, barrier, "HOLD")
        if journal:
            journal_ids.append(record_decision(journal, intent, snap, gate,
                                               decision_ms=bar.t * 1000, known_ms=bar.t * 1000))
        if gate.ok and gate.action == "SELL":
            close(bar.o, bar.t, "llm_sell")
        elif gate.ok and gate.action == "BUY":
            assert gate.qty is not None and snap.atr is not None
            fill_qty = float(gate.qty)
            price = bar.o * buy_factor
            fee = fill_qty * price * rules.taker_fee
            if fill_qty * price + fee > cash:
                raise ValueError("collar order exceeds replay cash after costs")
            qty, entry, entry_fee, entry_t = fill_qty, price, fee, bar.t
            stop = float(stop_for_entry(entry, snap.atr, settings, rules))
            tp = take_profit_for_entry(entry, snap.atr, settings, rules)
            target = float(tp) if tp else None
            cash -= qty * entry + fee
            cost += qty * (entry - bar.o) + fee
            if journal:
                journal.add_fill(day, 0, ts=datetime.fromtimestamp(bar.t, timezone.utc).isoformat(),
                                 side="BUY", qty=qty, price=entry, fee=fee, source="replay",
                                 known_ms=bar.t * 1000)
        last_action = intent.action
        decisions.append({"t": bar.t, "intent": asdict(intent), "gate": asdict(gate),
                          "snapshot": asdict(snap), "llm": result.as_audit()})
        if journal:
            decisions[-1].update(journal_id=journal_ids[-1], lessons=context["lessons"])
        if qty and bar.l <= stop:
            # OHLC gives no intrabar fill time. Use the candle end for cooldown
            # age, while preserving the historical trade timestamp convention.
            close(min(bar.o, stop), bar.t, "stop", cooldown_t=bar.t + BAR_SECONDS)
        if i == len(history) - 1 and qty:
            close(bar.c, bar.t + BAR_SECONDS, "end_of_data")
        if journal:
            end_ms = (bar.t + BAR_SECONDS) * 1000
            # After decision AND fills; offline unless the caller injects reflection.
            reflection = deferred_reflection(journal, settings, budget, as_of_ms=end_ms,
                                              completed_ms=lambda: end_ms, reflect=reflect)
            decisions[-1]["reflection"] = reflection
            llm_cost += reflection["cost_usd"]
        equity.append(cash + qty * bar.c * sell_factor * (1 - rules.taker_fee))
    output = {"metrics": metrics(equity, trades, cost, llm_cost, settings.max_order_usdt),
              "equity": equity, "trades": trades, "decisions": decisions}
    if journal:
        output["journal"] = [journal.journal_get(jid) for jid in journal_ids]
        journal._conn.close()
    return output


def buy_and_hold(history, settings, *, rules=None, spread_bps=1.0, slippage_bps=0.0, allocation=None):
    """Standard full-capital B&H; optionally hold only the bot's order cap.

    Same initial cash, cost rates, quantity precision and final liquidation;
    intentionally no collar/stops (otherwise this would not be buy-and-hold).
    """
    rules = rules or SymbolRules(qty_scale=settings.qty_scale)
    bars = history[21:]
    if not bars:
        raise ValueError("buy-and-hold requires warmup plus evaluation bars")
    buy_factor = (1 + spread_bps / 20000) * (1 + slippage_bps / 10000)
    sell_factor = (1 - spread_bps / 20000) * (1 - slippage_bps / 10000)
    initial = settings.paper_starting_usdt
    entry = bars[0].o * buy_factor
    allocated = min(initial, allocation if allocation is not None else initial)
    scale = 10 ** rules.qty_scale
    qty = math.floor(allocated / (entry * (1 + rules.taker_fee)) * scale) / scale
    entry_fee = qty * entry * rules.taker_fee
    cash = initial - qty * entry - entry_fee
    equity = [initial] + [cash + qty * b.c * sell_factor * (1 - rules.taker_fee) for b in bars]
    exit_price = bars[-1].c * sell_factor
    exit_fee = qty * exit_price * rules.taker_fee
    cost = qty * (entry - bars[0].o + bars[-1].c - exit_price) + entry_fee + exit_fee
    trades = [{"entry_t": bars[0].t, "exit_t": bars[-1].t + BAR_SECONDS, "qty": qty,
               "entry_price": entry, "exit_price": exit_price, "pnl_usdt": equity[-1] - initial,
               "exit_type": "end_of_data"}] if qty else []
    return {"metrics": metrics(equity, trades, cost, 0, allocated), "equity": equity, "trades": trades}


def fixed_policy(intents):
    """A fresh iterator per run, deliberately NOT a cache hit for changed snapshots."""
    iterator = iter(intents)

    def policy(snap, budget, **context):
        return ThinkResult(TradeIntent(**next(iterator)), "fixed_intent")

    return policy


def percentile(values, p):
    values = sorted(values)
    index = (len(values) - 1) * p
    left = int(index)
    right = min(left + 1, len(values) - 1)
    return values[left] + (values[right] - values[left]) * (index - left)


def compare(history, settings, llm, *, rules=None, spread_bps=1.0, slippage_bps=0.0,
            seeds=30, sweep=(1, 3, 5, 10)):
    if seeds < 30:
        raise ValueError("at least 30 random seeds required")
    intents = [d["intent"] for d in llm["decisions"]]
    if len(intents) != len(history) - 21:
        raise ValueError("decision coverage does not match comparison period")
    kwargs = {"rules": rules, "spread_bps": spread_bps, "slippage_bps": slippage_bps}
    random_runs = []
    for seed in range(seeds):
        shuffled = list(intents)
        random.Random(seed).shuffle(shuffled)
        result = replay(history, settings, fixed_policy(shuffled), **kwargs)
        random_runs.append({"seed": seed, "metrics": result["metrics"],
                            "action_counts": dict(Counter(i["action"] for i in shuffled))})
    values = [r["metrics"]["net_pnl_usdt"] for r in random_runs]
    net = llm["metrics"]["net_pnl_usdt"]
    summary = {"p05": percentile(values, .05), "median": percentile(values, .5),
               "p95": percentile(values, .95),
               "llm_percentile": 100 * (sum(v < net for v in values) + .5 * sum(v == net for v in values)) / seeds,
               "central_interval": percentile(values, .05) <= net <= percentile(values, .95)}
    sensitivity = []
    for spread in sweep:
        result = replay(history, settings, fixed_policy(intents), rules=rules,
                        spread_bps=spread, slippage_bps=slippage_bps)
        sensitivity.append({"spread_bps": spread, "metrics": result["metrics"]})
    slippage_sensitivity = []
    for slip in (0, 2, 5):
        result = replay(history, settings, fixed_policy(intents), rules=rules,
                        spread_bps=spread_bps, slippage_bps=slip)
        slippage_sensitivity.append({"slippage_bps": slip, "metrics": result["metrics"]})
    return {"llm": llm["metrics"], "buy_and_hold": buy_and_hold(history, settings, **kwargs)["metrics"],
            "buy_and_hold_order_cap": buy_and_hold(history, settings, allocation=settings.max_order_usdt, **kwargs)["metrics"],
            "random": random_runs, "random_summary": summary, "sensitivity": sensitivity,
            "slippage_sensitivity": slippage_sensitivity}


def markdown_report(comparison, metadata):
    def num(value):
        return "N/A" if value is None else f"{value:.6f}"

    llm = comparison["llm"]
    random_summary = comparison["random_summary"]
    representative = min(comparison["random"], key=lambda r: abs(r["metrics"]["net_pnl_usdt"] - random_summary["median"]))
    rows = [("LLM", llm), ("Buy-and-hold (capital inteiro)", comparison["buy_and_hold"]),
            ("Buy-and-hold (teto por ordem)", comparison["buy_and_hold_order_cap"]),
            (f"Aleatório seed {representative['seed']} (mais próximo da mediana)", representative["metrics"])]
    start = datetime.fromtimestamp(metadata["start"], timezone.utc).isoformat()
    end = datetime.fromtimestamp(metadata["end"], timezone.utc).isoformat()
    lines = ["# P0 — replay BTC/USDT KCEX", "",
             f"Período UTC: {start} até {end} (fim exclusivo); {metadata['bars']} barras/decisões, "
             f"{metadata['bars']/96:.4f} dias. Modelo: `{metadata['model']}`.", "",
             f"Spread total: {metadata['spread_bps']:.8f} bps; slippage por lado: {metadata['slippage_bps']:.4f} bps.", "",
             "**Limitação central:** esta janela pode representar poucos regimes; resultado positivo nesta janela não prova lucratividade futura.",
             "Uma decisão no open de cada Min15 subamostra o ciclo de 5 minutos e os wakes do runtime; não é réplica tick-a-tick.", "",
             "| Política | Net USDT | % capital | % alocação/teto | Custo execução | DD USDT | DD % | Sharpe | PF | Trades |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, m in rows:
        keys = ("net_pnl_usdt", "net_pnl_pct", "pnl_pct_order_cap", "execution_cost_usdt", "max_drawdown_usdt", "max_drawdown_pct", "sharpe", "profit_factor", "trades")
        lines.append(f"| {name} | " + " | ".join(num(m[k]) for k in keys) + " |")
    lines += ["", f"Custo histórico LLM: {llm['llm_cost_usd']:.6f} USD; net após LLM (USD≈USDT): {llm['net_after_llm_usdt']:.6f} USDT.",
              "Percentual sobre teto por ordem não é retorno sobre risco de stop nem capital simultaneamente investido ao longo da janela.", "",
              "## Controle aleatório", "",
              f"{len(comparison['random'])} permutações seeded dos mesmos intents: P5={random_summary['p05']:.6f}, "
              f"mediana={random_summary['median']:.6f}, P95={random_summary['p95']:.6f} USDT; "
              f"LLM no percentil empírico {random_summary['llm_percentile']:.2f} (empates divididos).",
              "A frequência/distribuição de sinais é preservada; trades executados podem divergir por estado/collar/stops."]
    if random_summary["central_interval"]:
        lines.append("**LLM dentro do intervalo central do aleatório: sem evidência de edge nesta janela.**")
    elif llm["net_pnl_usdt"] < random_summary["p05"]:
        lines.append("**LLM abaixo do P5 aleatório: desempenho pior que o controle nesta janela.**")
    else:
        lines.append("LLM acima do P95 aleatório nesta janela; ainda exige validação fora da amostra e múltiplos regimes.")
    if llm["net_pnl_usdt"] <= comparison["buy_and_hold"]["net_pnl_usdt"]:
        lines.append("**O LLM não bateu buy-and-hold em PnL absoluto.** As exposições são diferentes; consulte também B&H limitado ao teto por ordem.")
    else:
        lines.append("LLM bateu buy-and-hold em PnL absoluto nesta janela; isso sozinho não demonstra edge.")
    if llm["net_pnl_usdt"] <= random_summary["median"]:
        lines.append("**O LLM não bateu a mediana aleatória.**")
    lines += ["", "## Histograma de saídas", "", "| Política | stop | llm_sell | take_profit | time_limit | trailing | end_of_data |", "|---|---:|---:|---:|---:|---:|---:|"]
    for name, m in rows:
        lines.append(f"| {name} | " + " | ".join(str(v) for v in m["exit_types"].values()) + " |")
    lines += ["", "## Sensibilidade de custo com decisões fixas", "",
              "O modelo não reavalia os payloads alterados; estado da conta e gates podem divergir entre ramos.",
              "| Spread bps | Net USDT | Custo execução USDT |", "|---:|---:|---:|"]
    for row in comparison["sensitivity"]:
        lines.append(f"| {row['spread_bps']} | {row['metrics']['net_pnl_usdt']:.6f} | {row['metrics']['execution_cost_usdt']:.6f} |")
    losses = [r["spread_bps"] for r in comparison["sensitivity"] if r["metrics"]["net_pnl_usdt"] <= 0]
    lines += ["", "### Slippage por lado (spread mantido, decisões fixas)", "",
              "0 bps é hipótese não medida; 2 bps é cenário intermediário; 5 bps é cenário conservador, não estimativa de fills.",
              "| Slippage bps | Net USDT | Custo execução USDT |", "|---:|---:|---:|"]
    for row in comparison["slippage_sensitivity"]:
        lines.append(f"| {row['slippage_bps']} | {row['metrics']['net_pnl_usdt']:.6f} | {row['metrics']['execution_cost_usdt']:.6f} |")
    nonpositive = [r["slippage_bps"] for r in comparison["slippage_sensitivity"] if r["metrics"]["net_pnl_usdt"] <= 0]
    lines += ["", f"Primeiro slippage testado não positivo: {min(nonpositive)} bps (não é raiz exata)." if nonpositive else
              "Sem cruzamento de zero nos slippages testados; ponto de virada não determinado."]
    lines += ["", f"Primeiro spread testado não positivo: {min(losses)} bps (não é raiz exata)." if losses else "Nenhum cruzamento de zero nos spreads testados; não extrapolar fora da faixa.", "",
              "## Convenções", "",
              "- Só 21 barras anteriores fechadas no Eye/ATR; execução imediata no open assume latência zero.",
              "- Stops intrabar pelo low, gaps no min(open, stop); TP não implementado; futura colisão stop/TP deve assumir stop primeiro.",
              "- Equity marcada a bid líquido de slippage/taxa de saída em cada close; DD close-a-close não mede excursão intrabar.",
              "- Sharpe anualizado Min15 (365×96), taxa livre de risco zero; N/A se variância zero. PF=N/A sem perdas (inclusive sem trades).",
              "- Net trading inclui spread/slippage/taxas; LLM separado para comparação de execução; liquidação final explícita, sem posição escondida.",
              "- Slippage zero é hipótese não medida; bookTicker mede spread, não impacto, latência ou fills reais.",
              "- Custos de cache são custos históricos do experimento; uma repetição offline não paga novamente.", ""]
    return "\n".join(lines)


def main(argv=None):
    import argparse
    from dataclasses import replace

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("download", "run"):
        p = sub.add_parser(command)
        p.add_argument("--start", required=True, help="UTC ISO date/time, inclusive evaluation start")
        p.add_argument("--end", required=True, help="UTC ISO date/time, exclusive evaluation end")
        p.add_argument("--history", default="data/backtest/history.db")
        if command == "download":
            p.add_argument("--page-bars", type=int, default=200)
        else:
            p.add_argument("--cache", default="data/backtest/decisions.db")
            p.add_argument("--output", default="data/backtest/result.json")
            p.add_argument("--report", default="data/backtest/report.md")
            p.add_argument("--config", help="safe settings JSON saved by a previous run")
            p.add_argument("--rules", help="SymbolRules JSON; default captured BTC rules (mi=1, fees=0)")
            p.add_argument("--spread-bps", type=float, default=1)
            p.add_argument("--slippage-bps", type=float, default=0)
            p.add_argument("--max-tokens", type=int)
            p.add_argument("--model")
            p.add_argument("--allow-network", action="store_true", help="explicitly pay for cache misses; default offline")
            p.add_argument("--load-env", action="store_true", help="load local .env without logging credentials")
            p.add_argument("--max-cost-usd", type=float, default=0, help="cumulative spend ceiling for this cache, not per restart")
    args = parser.parse_args(argv)

    def timestamp(value):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.replace(tzinfo=timezone.utc).timestamp()) if dt.tzinfo is None else int(dt.timestamp())

    start, end = timestamp(args.start), timestamp(args.end)
    if start % BAR_SECONDS or end % BAR_SECONDS or start >= end:
        parser.error("range must be increasing and Min15 aligned")
    if args.command == "download":
        with History(args.history) as db:
            db.download(KcexClient(token=""), start-21*BAR_SECONDS, end, page_bars=args.page_bars)
            bars = db.load(start-21*BAR_SECONDS, end)
        print(json.dumps({"saved_bars": len(bars), "first": bars[0].t, "last": bars[-1].t}))
        return 0
    if args.load_env:
        from dotenv import load_dotenv
        load_dotenv()
    settings = Settings.from_env()
    if args.config:
        config = json.loads(Path(args.config).read_text())
        config.pop("openrouter_api_key", None)
        settings = replace(settings, **config)
    settings = replace(settings, mode="paper")
    if args.max_tokens is not None:
        settings = replace(settings, llm_max_tokens=args.max_tokens)
    if args.model:
        settings = replace(settings, llm_model=args.model)
    rules = SymbolRules(**json.loads(Path(args.rules).read_text())) if args.rules else SymbolRules(min_amount=1)
    with History(args.history) as db:
        bars = db.load(start-21*BAR_SECONDS, end)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    safe_settings = asdict(settings)
    safe_settings.pop("openrouter_api_key")
    output.with_suffix(".settings.json").write_text(json.dumps(safe_settings, indent=2))
    with CachedBrain(args.cache, settings, allow_network=args.allow_network, max_cost_usd=args.max_cost_usd) as brain:
        count = 0

        def policy(snap, budget, **context):
            nonlocal count
            result = brain(snap, budget, **context)
            count += 1
            if count % 100 == 0:
                print(json.dumps({"decisions": count, "total": len(bars)-21, "paid_usd": brain.paid_usd, "cache_hits": brain.hits}), flush=True)
            return result

        result = replay(bars, settings, policy, rules=rules, spread_bps=args.spread_bps, slippage_bps=args.slippage_bps)
        paid = brain.paid_usd
    comparison = compare(bars, settings, result, rules=rules, spread_bps=args.spread_bps, slippage_bps=args.slippage_bps)
    comparison["paper_slippage_5bps"] = comparison["slippage_sensitivity"][-1]["metrics"]
    metadata = {"start": start, "end": end, "bars": len(bars)-21, "model": settings.llm_model,
                "spread_bps": args.spread_bps, "slippage_bps": args.slippage_bps, "rules": asdict(rules),
                "history_sha256": hashlib.sha256(json.dumps([asdict(b) for b in bars], sort_keys=True).encode()).hexdigest()}
    output.write_text(json.dumps({"metadata": metadata, "comparison": comparison, "replay": result}, indent=2, allow_nan=False))
    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(markdown_report(comparison, metadata))
    print(json.dumps({"report": str(report), "llm": result["metrics"], "paid_this_run_usd": paid}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
