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
