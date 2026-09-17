import json

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


def build(tmp_path, *, n=200, days=15, main_pnl=0.05, model="jev-1.13.0", shadow_pnl=-0.01,
          jev_only_pnl=None, random_pnl=None, jev_cost=0.001):
    store = FutStore(tmp_path / "fut.db")
    step = days * DAY_MS // n
    for i in range(n):
        t = T0 + i * step
        trade(store, "main", t, main_pnl + (0.001 if i % 2 else -0.001))
        trade(store, "shadow:jev_only", t, shadow_pnl if jev_only_pnl is None else jev_only_pnl)
        trade(store, "shadow:random", t, shadow_pnl if random_pnl is None else random_pnl)
    for i in range(n):
        store.log_decision("jev", {"model": model, "cost_usd": jev_cost}, ts_ms=T0 + i * step)
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
    criterion = EdgeCriterion(min_jev_rows_per_day=1)
    summary = summarize(store, FutSettings(), criterion)
    verdict = evaluate(summary, FutSettings(), criterion)
    assert verdict["checks"] == {k: True for k in verdict["checks"]}
    assert verdict["passed"] is True
    assert summary["n_trades"] == 200 and summary["days"] >= 14
    assert "PASSED" in render(summary, verdict)


def test_min_days_counts_distinct_real_jev_utc_dates_not_elapsed_span(tmp_path):
    store = FutStore(tmp_path / "fut.db")
    store.log_decision("jev", {"model": "jev-real", "cost_usd": 0.0}, ts_ms=T0)
    store.log_decision("jev", {"model": "jev-real", "cost_usd": 0.0}, ts_ms=T0 + 15 * DAY_MS)

    summary = summarize(store, FutSettings())

    assert summary["days"] == 0
    assert evaluate(summary, FutSettings(), EdgeCriterion(min_trades=0, min_days=1))["checks"]["min_days"] is False


def test_min_days_requires_real_jev_coverage_per_utc_day(tmp_path):
    store = FutStore(tmp_path / "fut.db")
    rows = [(T0 + day * DAY_MS, "jev", json.dumps({"model": "jev-real", "cost_usd": 0.0}))
            for day in range(14)]
    store._conn.executemany("INSERT INTO fut_decisions(ts_ms, kind, payload) VALUES (?,?,?)", rows)
    store.commit()

    summary = summarize(store, FutSettings())

    assert summary["days"] == 0
    assert evaluate(summary, FutSettings(), EdgeCriterion(min_trades=0, min_days=14))["checks"]["min_days"] is False


def test_min_days_accepts_fourteen_utc_days_with_minimum_real_jev_coverage(tmp_path):
    store = FutStore(tmp_path / "fut.db")
    rows = []
    for day in range(14):
        for row_number in range(10_000):
            rows.append((T0 + day * DAY_MS + row_number, "jev",
                         json.dumps({"model": "jev-real", "cost_usd": 0.0})))
    store._conn.executemany("INSERT INTO fut_decisions(ts_ms, kind, payload) VALUES (?,?,?)", rows)
    store.commit()

    summary = summarize(store, FutSettings())

    assert summary["days"] == 14
    assert evaluate(summary, FutSettings(), EdgeCriterion(min_trades=0, min_days=14))["checks"]["min_days"] is True


def passing_summary():
    return {
        "main_net_usd": 1.0, "n_trades": 200, "days": 14,
        "jev_models": ["jev-real"], "flat_net_usd": 0.0,
        "jev_only_net_usd": -1.0, "random_net_usd": -1.0,
        "ci95": (0.1, 0.2), "daily_net_after_models": {},
    }


@pytest.mark.parametrize("failing, update", [
    ("min_trades", {"n_trades": 199}),
    ("min_days", {"days": 13}),
    ("real_jev_only", {"jev_models": ["mock"]}),
    ("net_positive", {"main_net_usd": 0.0, "flat_net_usd": -1.0}),
    ("beats_flat", {"flat_net_usd": 2.0}),
    ("beats_jev_only", {"jev_only_net_usd": 2.0}),
    ("beats_random", {"random_net_usd": 2.0}),
    ("ci_lower_positive", {"ci95": (-0.1, 0.2)}),
    ("day_loss_ok", {"daily_net_after_models": {"2026-09-17": -21.0}}),
])
def test_each_check_can_fail_in_isolation(failing, update):
    summary = passing_summary()
    summary.update(update)
    verdict = evaluate(summary, FutSettings())
    assert verdict["checks"][failing] is False
    assert sum(not passed for passed in verdict["checks"].values()) == 1
    assert verdict["passed"] is False


def test_ci_lower_positive_fails_with_positive_total_net(tmp_path):
    summary = {
        "main_net_usd": 1.0, "n_trades": 200, "days": 14,
        "jev_models": ["jev-real"], "flat_net_usd": 0.0,
        "jev_only_net_usd": -1.0, "random_net_usd": -1.0,
        "ci95": (-0.1, 0.2), "daily_net_after_models": {},
    }

    verdict = evaluate(summary, FutSettings())

    assert verdict["checks"]["net_positive"] is True
    assert verdict["checks"]["ci_lower_positive"] is False
    assert verdict["passed"] is False


def test_jev_only_total_deducts_jev_cost(tmp_path):
    summary = summarize(build(tmp_path, jev_cost=0.01), FutSettings())

    assert summary["jev_only_net_usd"] == pytest.approx(
        summary["books"]["shadow:jev_only"]["net"] - summary["jev_cost_usd"]
    )


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
