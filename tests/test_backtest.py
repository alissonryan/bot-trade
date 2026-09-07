from dataclasses import asdict
from unittest.mock import Mock

import pytest

from bot.eye import Eye
from bot.settings import Settings
from bot.types import Bar


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network is forbidden in replay tests")
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("kcex.ws.default_connect", forbidden)


def bars(n=25):
    return [Bar(1800000000 + i * 900, 100 + i, 102 + i, 99 + i, 101 + i, 10) for i in range(n)]


def test_replay_journal_is_point_in_time_and_offline():
    from dataclasses import replace
    from bot.backtest import replay
    from bot.brain import ThinkResult, request_body
    from bot.types import TradeIntent
    history = [Bar(1800000000+i*900,100,101,99,100) for i in range(25)]
    history[22] = Bar(history[22].t,100,101,90,100)  # intrabar stop not known at open
    settings = replace(Settings.from_env(), mode='paper', journal_enabled=True,
                       cooldown_minutes=0, tp_atr_mult=0, time_limit_minutes=0)
    prompts = []
    def policy(snap, budget, **context):
        import json
        prompts.append(json.loads(request_body(snap,settings,**context)['messages'][1]['content']))
        return ThinkResult(TradeIntent('BUY' if len(prompts)==1 else 'HOLD',1,'fixed','range'),'fixed_intent')
    result = replay(history,settings,policy,spread_bps=0)
    assert prompts[0]['lessons'] == []
    assert prompts[1]['lessons'] == []
    assert len(prompts[2]['lessons']) == 1
    assert prompts[2]['lessons'][0]['outcome_known_ms'] == history[23].t*1000
    assert 'reflection' not in prompts[2]['lessons'][0]
    assert result['journal'][0]['reflection_audit']['reason'] == 'reflection_offline'
    assert result['metrics']['trades'] == 1


@pytest.mark.parametrize("exit_type", ["gap_stop", "intrabar_stop", "llm_sell"])
def test_replay_cooldown_uses_exit_time_and_preserves_sells(exit_type):
    from dataclasses import replace
    from bot.backtest import replay, fixed_policy
    history = [Bar(1800000000 + i * 900, 100, 101, 99, 100) for i in range(27)]
    actions = ["BUY"] * 6
    if exit_type == "gap_stop":
        history[22] = Bar(history[22].t, 95, 101, 95, 100)
    elif exit_type == "intrabar_stop":
        history[22] = Bar(history[22].t, 100, 101, 95, 100)
    else:
        actions[1] = "SELL"
        # The cooldown only arms on a LOSS, so the discretionary exit has to be
        # one. A flat book would settle at breakeven and correctly not arm it.
        history[22] = Bar(history[22].t, 99, 99.5, 98.5, 99)
    s = replace(Settings.from_env(), mode="paper", cooldown_minutes=30,
                tp_atr_mult=0, time_limit_minutes=0)
    intents = [{"action": action, "confidence": 1, "reason": "fixed", "regime": "range"}
               for action in actions]
    result = replay(history, s, fixed_policy(intents), spread_bps=0)
    gates = [d['gate'] for d in result['decisions']]
    assert gates[2]['rule'] == 'cooldown'
    if exit_type == 'intrabar_stop':
        # We cannot know when inside the candle the stop fired: start at end.
        assert gates[3]['rule'] == 'cooldown'
        assert gates[4]['rule'] == 'ok_buy'
    else:
        assert gates[3]['rule'] == 'ok_buy'
    if exit_type == 'gap_stop':
        assert gates[1]['rule'] == 'cooldown'
    elif exit_type == 'llm_sell':
        assert gates[1]['rule'] == 'ok_close'


@pytest.mark.parametrize("kind", ["take_profit", "time_limit", "stop"])
def test_replay_local_barriers_and_stop_priority(kind):
    from dataclasses import replace
    from bot.backtest import replay, fixed_policy
    history = [Bar(1800000000 + i * 900, 100, 101, 99, 100) for i in range(26)]
    settings = replace(Settings.from_env(), mode="paper", tp_atr_mult=3, time_limit_minutes=30,
                       min_tp_pct=.006, max_tp_pct=.06)
    if kind == "take_profit":
        history[22] = Bar(history[22].t, 107, 108, 106, 107)
    elif kind == "stop":
        history[22] = Bar(history[22].t, 100, 110, 90, 100)  # dual-touch; no assumed intrabar TP fill
    intents = [{"action": "BUY" if i == 0 else "HOLD", "confidence": 1, "reason": "fixed", "regime": "trend"} for i in range(5)]
    result = replay(history, settings, fixed_policy(intents), spread_bps=0)
    assert result["metrics"]["exit_types"][kind] == 1
    trade = result["trades"][0]
    expected_t = history[23 if kind == "time_limit" else 22].t
    assert trade["exit_t"] == expected_t
    if kind == "take_profit":
        assert trade["exit_price"] == 107


def test_replay_does_not_promise_intrabar_local_take_profit():
    from dataclasses import replace
    from bot.backtest import replay, fixed_policy
    history = [Bar(1800000000 + i * 900, 100, 101, 99, 100) for i in range(24)]
    history[22] = Bar(history[22].t, 100, 110, 99, 100)
    settings = replace(Settings.from_env(), mode="paper", tp_atr_mult=3, time_limit_minutes=0)
    intents = [{"action": "BUY" if i == 0 else "HOLD", "confidence": 1, "reason": "fixed", "regime": "trend"} for i in range(3)]
    result = replay(history, settings, fixed_policy(intents), spread_bps=0)
    assert result["metrics"]["exit_types"]["take_profit"] == 0
    assert result["metrics"]["exit_types"]["end_of_data"] == 1


def test_replay_snapshot_matches_real_eye():
    from bot.backtest import replay_snapshot

    settings = Settings.from_env()
    history = bars()
    t = history[-1].t + 900
    snap = replay_snapshot(history, t, 126, settings, spread_bps=1,
                           cash=430, qty=0.2, entry=100, last_action="BUY", day_pnl=-2)
    eye = Eye(Mock(), settings, bot_qty=0.2, bot_avg_entry=100)
    eye._now_ms = lambda: t * 1000
    eye.last, eye.bid, eye.ask = 126, snap.bid, snap.ask
    eye.last_update_ms = t * 1000
    eye.depth_update_ms = t * 1000
    eye.bars = history[-21:]
    eye.free_usdt = 430
    eye.last_intent_action = "BUY"
    eye.last_bot_pnl_usdt = -2
    assert asdict(snap) == asdict(eye.snapshot())
    assert snap.bid < snap.last < snap.ask
    assert snap.spread == snap.ask - snap.bid


def test_history_pagination_persistence_and_forming_bar(tmp_path):
    from bot.backtest import History

    history = bars(7)
    calls = []

    class Client:
        def kline(self, symbol, *, interval, start, end):
            calls.append((start, end))
            selected = [b for b in history if start <= b.t * 1000 <= end]
            return {"data": {k: [getattr(b, k) for b in selected] for k in "tohlcv"}}

    with History(tmp_path / "history.db") as db:
        db.download(Client(), history[0].t, history[-1].t + 400, page_bars=2,
                    now_s=history[-1].t + 400)
        assert db.load(history[0].t, history[-1].t + 400) == history[:-1]
        assert len(calls) == 4
    with History(tmp_path / "history.db") as db:
        assert db.load(history[0].t, history[-1].t) == history[:-1]


def test_history_rejects_missing_bars(tmp_path):
    from bot.backtest import History

    with History(tmp_path / "history.db") as db:
        with pytest.raises(ValueError, match="missing"):
            db.load(1800000000, 1800001800)


def test_cached_brain_reuses_response_and_budget_without_network(tmp_path):
    from dataclasses import replace
    from bot.backtest import CachedBrain, replay_snapshot
    from bot.brain import Budget

    settings = replace(Settings.from_env(), llm_model="test/model", openrouter_api_key="test")
    snap = replay_snapshot(bars(), bars()[-1].t + 900, 126, settings)
    response = {"choices": [{"message": {"content": '{"action":"BUY","confidence":1,"reason":"test","regime":"trend"}'}}], "usage": {"cost": 0.003}}
    post = Mock(return_value=Mock(status_code=200, json=lambda: response))
    with CachedBrain(tmp_path / "cache.db", settings, allow_network=True, max_cost_usd=1, http_post=post) as brain:
        budget = Budget(0, 2, "day")
        first = brain(snap, budget)
        assert budget.spent_usd == 0.003
        assert brain.paid_usd == 0.003
    with CachedBrain(tmp_path / "cache.db", settings) as brain:
        budget = Budget(0, 2, "day")
        assert brain(snap, budget) == first
        assert budget.spent_usd == 0.003
        assert brain.paid_usd == 0
        with pytest.raises(RuntimeError, match="cache miss"):
            brain(replace(snap, free_usdt=429), budget)
        with pytest.raises(RuntimeError, match="cache miss"):
            brain(snap, budget, lessons=[], as_of_ms=snap.ts_ms)
    assert post.call_count == 1


def test_replay_twice_is_identical_with_cache_and_real_collar(tmp_path):
    from dataclasses import replace
    from bot.backtest import CachedBrain, replay
    from bot.types import SymbolRules

    settings = replace(Settings.from_env(), mode="paper", llm_model="test", openrouter_api_key="test")
    history = [Bar(1800000000 + i * 900, 100, 101, 99, 100) for i in range(25)]
    seen = []
    def post(*args, **kw):
        seen.append(kw["json"])
        action = "BUY" if len(seen) == 1 else "SELL"
        return Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": '{"action":"' + action + '","confidence":1}'}}], "usage": {"cost": 0.001}})
    with CachedBrain(tmp_path / "cache.db", settings, allow_network=True, max_cost_usd=1, http_post=post) as brain:
        first = replay(history, settings, brain, rules=SymbolRules(min_amount=1), spread_bps=2)
    with CachedBrain(tmp_path / "cache.db", settings) as brain:
        second = replay(history, settings, brain, rules=SymbolRules(min_amount=1), spread_bps=2)
        assert brain.misses == 0
    assert first == second
    assert first["metrics"]["trades"] == 1
    assert first["metrics"]["exit_types"]["llm_sell"] == 1
    assert first["metrics"]["net_pnl_usdt"] == pytest.approx(-0.004)
    assert first["metrics"]["execution_cost_usdt"] == pytest.approx(0.004)
    assert len(seen) == 4
    assert all(__import__("json").loads(req["messages"][1]["content"])["last"] == history[21+i].o
               for i, req in enumerate(seen))
    assert all(b["t"] < history[21+i].t for i, req in enumerate(seen)
               for b in __import__("json").loads(req["messages"][1]["content"])["bars_15m"])


def test_gap_stop_fills_at_open_not_optimistic_trigger():
    from dataclasses import replace
    from bot.backtest import replay
    from bot.brain import ThinkResult
    from bot.types import TradeIntent

    settings = replace(Settings.from_env(), mode="paper")
    history = [Bar(1800000000 + i * 900, 100, 101, 99, 100) for i in range(23)]
    history[-1] = Bar(history[-1].t, 90, 92, 89, 91)
    def policy(snap, budget):
        return ThinkResult(TradeIntent("BUY" if snap.last == 100 else "HOLD", 1, "", "trend"), "ok")
    result = replay(history, settings, policy, spread_bps=0)
    assert result["trades"][0]["exit_price"] == 90
    assert result["metrics"]["exit_types"]["stop"] == 1
    assert result["metrics"]["net_pnl_usdt"] == pytest.approx(-2)


def test_exhausted_budget_is_error_not_a_fabricated_hold(tmp_path):
    from bot.backtest import CachedBrain, replay_snapshot
    from bot.brain import Budget

    settings = Settings.from_env()
    with CachedBrain(tmp_path / "cache.db", settings) as brain:
        with pytest.raises(RuntimeError, match="budget"):
            brain(replay_snapshot(bars(), bars()[-1].t+900, 126, settings), Budget(1, 1, "day"))


def test_snapshot_parity_through_real_eye_poll_heavy():
    from bot.backtest import replay_snapshot

    history = bars(21)
    t = history[-1].t + 900
    forming = Bar(t, 126, 999999, 1, 2)
    client = Mock()
    client.kline.return_value = {"data": {k: [getattr(b, k) for b in history + [forming]] for k in "tohlcv"}}
    client.balances.return_value = {"data": [{"currency": "USDT", "available": "430"}]}
    settings = Settings.from_env()
    eye = Eye(client, settings)
    eye._now_ms = lambda: t * 1000
    eye.poll_heavy()
    snap = replay_snapshot(history, t, forming.o, settings, cash=430)
    eye.last, eye.bid, eye.ask = forming.o, snap.bid, snap.ask
    eye.last_update_ms = t * 1000
    eye.depth_update_ms = t * 1000
    assert asdict(eye.snapshot()) == asdict(snap)
    assert snap.last == forming.o != forming.c
    assert forming not in snap.bars_15m


def test_baselines_same_costs_random_marginals_and_distribution():
    from dataclasses import replace
    from collections import Counter
    from bot.backtest import buy_and_hold, compare, replay
    from bot.brain import ThinkResult
    from bot.types import TradeIntent

    settings = replace(Settings.from_env(), mode="paper")
    history = [Bar(1800000000+i*900, 100, 101, 99, 100) for i in range(31)]
    actions = iter(["BUY", "SELL", "HOLD", "BUY", "HOLD", "SELL", "HOLD", "HOLD", "HOLD", "HOLD"])
    result = replay(history, settings, lambda snap, budget: ThinkResult(TradeIntent(next(actions), 1, "", "trend"), "ok"), spread_bps=2)
    bh = buy_and_hold(history, settings, spread_bps=2)
    assert bh["metrics"]["net_pnl_usdt"] < 0
    assert bh["metrics"]["net_pnl_usdt"] == pytest.approx(-bh["metrics"]["execution_cost_usdt"])
    assert bh["metrics"]["trades"] == 1
    comparison = compare(history, settings, result, spread_bps=2, seeds=30, sweep=(1, 3, 5, 10))
    assert len(comparison["random"]) == 30
    assert comparison == compare(history, settings, result, spread_bps=2, seeds=30, sweep=(1, 3, 5, 10))
    original = Counter(d["intent"]["action"] for d in result["decisions"])
    assert all(r["action_counts"] == original for r in comparison["random"])
    assert comparison["random_summary"]["p05"] <= comparison["random_summary"]["median"] <= comparison["random_summary"]["p95"]
    assert [r["spread_bps"] for r in comparison["sensitivity"]] == [1, 3, 5, 10]
    assert [r["slippage_bps"] for r in comparison["slippage_sensitivity"]] == [0, 2, 5]
    assert comparison["slippage_sensitivity"][0]["metrics"] == result["metrics"]


def test_markdown_report_is_honest_about_losing_to_baselines():
    from bot.backtest import markdown_report, metrics
    m = metrics([450, 449], [], 1, .1, 20)
    data = {"llm": m, "buy_and_hold": dict(m, net_pnl_usdt=2), "buy_and_hold_order_cap": m,
            "random": [{"seed": 0, "metrics": m}],
            "random_summary": {"p05": -2, "median": 0, "p95": 2, "llm_percentile": 40, "central_interval": True},
            "sensitivity": [{"spread_bps": 1, "metrics": m}],
            "slippage_sensitivity": [{"slippage_bps": s, "metrics": m} for s in (0, 2, 5)]}
    text = markdown_report(data, {"start": 1800000000, "end": 1800000900, "bars": 1, "model": "test", "spread_bps": 1, "slippage_bps": 0})
    assert "não bateu buy-and-hold" in text
    assert "sem evidência de edge" in text
    assert "decisões fixas" in text
    assert "stop" in text
    assert "Slippage por lado" in text
    assert "não é raiz exata" in text


def test_cli_help_exposes_offline_default(capsys):
    from bot.backtest import main
    with pytest.raises(SystemExit) as exc:
        main(["run", "--help"])
    assert exc.value.code == 0
    assert "--allow-network" in capsys.readouterr().out


def test_slippage_applies_to_quote_like_paper_hands():
    from bot.backtest import buy_and_hold, replay
    from bot.brain import ThinkResult
    from bot.types import TradeIntent
    from dataclasses import replace

    settings = replace(Settings.from_env(), mode="paper")
    history = [Bar(1800000000+i*900, 100, 101, 99, 100) for i in range(22)]
    result = replay(history, settings, lambda s, b: ThinkResult(TradeIntent("BUY",1,"","trend"),"ok"), spread_bps=20, slippage_bps=20)
    assert result["trades"][0]["entry_price"] == pytest.approx(100 * 1.001 * 1.002, abs=1e-10)
    assert result["trades"][0]["exit_price"] == pytest.approx(100 * .999 * .998, abs=1e-10)
    baseline = buy_and_hold(history, settings, spread_bps=20, slippage_bps=20)
    assert baseline["trades"][0]["entry_price"] == result["trades"][0]["entry_price"]
    assert baseline["trades"][0]["exit_price"] == result["trades"][0]["exit_price"]
