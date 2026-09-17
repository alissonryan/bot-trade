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
