from pathlib import Path
from decimal import Decimal
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.collar import _round_qty, decide, stop_for_entry
from bot.ratelimit import WriteCounts
from bot.settings import Settings
from bot.types import Bar, Snapshot, SymbolRules, TradeIntent


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


def test_cooldown_blocks_only_buy_until_exact_expiry():
    settings = _settings(cooldown_minutes=30)
    for now, expected in [(1_799_999, False), (1_800_000, True)]:
        gate = decide(TradeIntent("BUY", 1, "go", "range"), _snap(), settings,
                      session_ok=True, day_pnl_usdt=0, last_loss_exit_ms=0, now_ms=now)
        assert gate.ok is expected
        assert gate.rule == ("ok_buy" if expected else "cooldown")
        assert gate.action == "BUY"


def test_cooldown_never_blocks_sell_even_with_entry_halts():
    gate = decide(TradeIntent("SELL", 0, "exit", "range"),
                  _snap(bot_qty=.001, stale=True),
                  _settings(cooldown_minutes=30, min_confidence=1),
                  session_ok=True, day_pnl_usdt=-100, last_loss_exit_ms=100, now_ms=101)
    assert gate.ok and gate.rule == "ok_close" and gate.action == "SELL"


@pytest.mark.parametrize("minutes,exited,now,rule", [
    (0, 100, 101, "ok_buy"),
    (30, None, 101, "ok_buy"),
    (30, 100, 99, "cooldown"),  # clock rollback cannot release an entry
    (30, 100, None, "cooldown"),
])
def test_cooldown_disabled_missing_exit_and_clock_edges(minutes, exited, now, rule):
    gate = decide(TradeIntent("BUY", 1, "go", "range"), _snap(),
                  _settings(cooldown_minutes=minutes), session_ok=True, day_pnl_usdt=0,
                  last_loss_exit_ms=exited, now_ms=now)
    assert gate.rule == rule


@pytest.mark.parametrize("qty,scale,expected", [
    (0.00022987654321, 5, "0.00022"),
    (0.000225, 5, "0.00022"),
    (0.00022999999999999998, 5, "0.00022"),
    (0.00023, 5, "0.00023"),
    (0.000008, 5, "0.00000"),
    (0.000891234567 - 0.00064, 5, "0.00025"),
    (1.239, 2, "1.23"),
    (1.9, 0, "1"),
])
def test_quantity_truncates_without_exceeding_input(qty, scale, expected):
    result = _round_qty(qty, scale)
    assert result == expected
    assert Decimal(result) <= Decimal(str(qty))


def test_sell_entire_balance_delta_does_not_round_up():
    qty = 0.00022987654321
    gate = decide(TradeIntent("SELL", 1, "exit", "trend"), _snap(bot_qty=qty),
                  _settings(), session_ok=True, day_pnl_usdt=0,
                  rules=SymbolRules(qty_scale=5, min_amount=1))
    assert gate.ok
    assert gate.qty is not None
    assert Decimal(gate.qty) <= Decimal(str(qty))
    assert gate.qty == "0.00022"


@pytest.mark.parametrize("free_usdt", [450, 399])
def test_buy_truncated_quantity_respects_order_and_portfolio_caps(free_usdt):
    settings = _settings(max_order_usdt=20, max_portfolio_pct=0.05)
    gate = decide(TradeIntent("BUY", 1, "go", "trend"),
                  _snap(last=87000, free_usdt=free_usdt), settings,
                  session_ok=True, day_pnl_usdt=0, rules=SymbolRules(qty_scale=5, min_amount=1))
    assert gate.ok
    assert gate.qty is not None
    notional = Decimal(gate.qty) * Decimal("87000")
    assert notional <= Decimal(str(settings.max_order_usdt))
    assert notional <= Decimal(str(settings.max_portfolio_pct)) * Decimal(str(free_usdt))


@pytest.mark.parametrize("cap,last,minimum,rule", [
    (20, 87000, 20, "min_notional"),
    (0.8, 100000, 1, "dust"),
])
def test_floor_below_venue_minimum_is_not_an_approved_order(cap, last, minimum, rule):
    gate = decide(TradeIntent("BUY", 1, "go", "trend"), _snap(last=last),
                  _settings(max_order_usdt=cap), session_ok=True, day_pnl_usdt=0,
                  rules=SymbolRules(qty_scale=5, min_amount=minimum))
    assert not gate.ok
    assert gate.rule == rule
    assert gate.qty is None


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


def test_day_loss_counts_unrealized():
    r = decide(
        TradeIntent("BUY", 0.8, "go", "trend"),
        _snap(),
        _settings(),
        session_ok=True,
        day_pnl_usdt=-12.0,
        unrealized_pnl_usdt=-8.5,
    )
    assert r.rule == "day_loss"


def test_day_loss_still_allows_sell():
    r = decide(
        TradeIntent("SELL", 0.7, "exit", "trend"),
        _snap(bot_qty=0.00025, bot_avg_entry=80_000, last=81_000),
        _settings(),
        session_ok=True,
        day_pnl_usdt=-20.0,
    )
    assert r.ok is True
    assert r.action == "SELL"


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


def test_stale_still_allows_sell():
    r = decide(
        TradeIntent("SELL", 0.7, "exit", "trend"),
        _snap(stale=True, bot_qty=0.00025, bot_avg_entry=80_000, last=81_000),
        _settings(),
        session_ok=True,
        day_pnl_usdt=0.0,
    )
    assert r.ok is True
    assert r.action == "SELL"


def test_zero_last_rejects_buy():
    r = decide(
        TradeIntent("BUY", 0.8, "go", "trend"),
        _snap(last=0.0),
        _settings(),
        session_ok=True,
        day_pnl_usdt=0.0,
    )
    assert r.ok is False
    assert r.rule == "atr"


def test_zero_last_rejects_sell():
    """A zero/invalid `last` must block SELL just as it blocks BUY."""
    r = decide(
        TradeIntent("SELL", 0.7, "exit", "trend"),
        _snap(last=0.0, bot_qty=0.00025, bot_avg_entry=80_000),
        _settings(),
        session_ok=True,
        day_pnl_usdt=0.0,
    )
    assert r.ok is False
    assert r.rule == "no_price"


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


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_confidence_never_reaches_ok_buy(confidence):
    # Defense in depth: even if a non-finite confidence somehow reaches the
    # collar (parse_intent() should already reject it upstream), comparisons
    # against NaN/Infinity are always False, so a bare `< min_confidence`
    # check would approve the BUY no matter how high MIN_CONFIDENCE is set.
    s = _settings(min_confidence=0.0)
    gate = decide(TradeIntent("BUY", confidence, "x", "trend"), _snap(), s,
                  session_ok=True, day_pnl_usdt=0.0)
    assert gate.ok is False
    assert gate.rule == "confidence"


def test_low_confidence_blocks_buy_but_not_sell():
    s = _settings(min_confidence=0.6)
    buy = decide(TradeIntent("BUY", 0.4, "meh", "range"), _snap(), s, session_ok=True, day_pnl_usdt=0.0)
    assert buy.rule == "confidence"
    sell = decide(
        TradeIntent("SELL", 0.1, "out", "range"),
        _snap(bot_qty=0.00025, bot_avg_entry=80_000),
        s,
        session_ok=True,
        day_pnl_usdt=0.0,
    )
    assert sell.ok is True


def test_symbol_rules_parse_observed_payload():
    payload = {
        "data": {"ps": 2, "qs": 5, "tfr": "0", "mfr": "0", "la": "600000", "li": "1", "ma": "600000", "mi": "1"},
        "code": 0,
    }
    rules = SymbolRules.from_trade_rules(payload)
    assert rules.price_scale == 2
    assert rules.qty_scale == 5
    assert rules.min_amount == 1.0
    assert rules.max_amount == 600000.0
    assert rules.taker_fee == 0.0
    assert SymbolRules.from_trade_rules(None) == SymbolRules()


def test_rules_drive_scales_and_min_notional():
    rules = SymbolRules(price_scale=1, qty_scale=3, min_amount=125.0)
    r = decide(
        TradeIntent("BUY", 0.8, "go", "trend"),
        _snap(free_usdt=4500, last=80_000, atr=400),
        _settings(max_order_usdt=100),
        session_ok=True,
        day_pnl_usdt=0.0,
        rules=rules,
    )
    assert r.rule == "min_notional"  # 0.001 BTC = 80 USDT after rounding < 125 USDT minimum
    rules = SymbolRules(price_scale=1, qty_scale=3, min_amount=1.0)
    r = decide(
        TradeIntent("BUY", 0.8, "go", "trend"),
        _snap(free_usdt=4500, last=80_000, atr=400),
        _settings(max_order_usdt=100),
        session_ok=True,
        day_pnl_usdt=0.0,
        rules=rules,
    )
    assert r.ok is True
    assert r.qty == "0.001"  # 3-decimal quantity scale
    assert len(r.stop_price.split(".")[1]) == 1  # 1-decimal price scale


def test_stop_for_entry_recomputes_on_real_fill():
    s = _settings()
    at_last = stop_for_entry(80_000.0, 400.0, s)
    at_fill = stop_for_entry(80_040.0, 400.0, s)
    assert float(at_fill) - float(at_last) == 40.0


def test_take_profit_opt_in_env_and_gate(monkeypatch):
    monkeypatch.setenv("TP_ATR_MULT", "3")
    monkeypatch.setenv("MIN_TP_PCT", "0.006")
    monkeypatch.setenv("MAX_TP_PCT", "0.06")
    monkeypatch.setenv("TIME_LIMIT_MINUTES", "60")
    settings = _settings()
    assert settings.time_limit_minutes == 60
    gate = decide(TradeIntent("BUY", 1, "go", "trend"), _snap(last=80000, atr=400),
                  settings, session_ok=True, day_pnl_usdt=0)
    assert gate.take_profit_price == "81200.00"


@pytest.mark.parametrize("kwargs", [
    {"tp_atr_mult": float("nan")}, {"tp_atr_mult": -1},
    {"time_limit_minutes": float("inf")}, {"time_limit_minutes": -1},
    {"min_tp_pct": -.1}, {"min_tp_pct": .1, "max_tp_pct": .01},
])
def test_invalid_barrier_config_rejected_before_trading(kwargs):
    with pytest.raises(ValueError, match="barrier"):
        _settings(**kwargs)


@pytest.mark.parametrize("atr,expected", [(1, "80480.00"), (400, "81200.00"), (10000, "84800.00")])
def test_take_profit_atr_clamps_and_fill_recomputation(atr, expected):
    from bot.collar import take_profit_for_entry
    settings = _settings(tp_atr_mult=3, min_tp_pct=.006, max_tp_pct=.06)
    assert take_profit_for_entry(80000, atr, settings) == expected
    assert take_profit_for_entry(80040, 400, settings) == "81240.00"
    assert take_profit_for_entry(80000, atr, _settings(tp_atr_mult=0)) is None


@pytest.mark.parametrize("entry,scale,expected", [
    (80000.009, 2, "79200.00"),
    (80000.09, 1, "79200.0"),
    (80000.9, 0, "79200"),
    (80000.01, 2, "79200.01"),
])
def test_long_stop_truncates_to_price_scale(entry, scale, expected):
    settings = _settings(atr_mult=2, min_stop_pct=0.004, max_stop_pct=0.04)
    rules = SymbolRules(price_scale=scale)
    stop = stop_for_entry(entry, 400, settings, rules)
    assert stop == expected
    assert Decimal(stop) <= Decimal(str(entry - 800))
    gate = decide(TradeIntent("BUY", 1, "go", "trend"), _snap(last=entry, atr=400),
                  settings, session_ok=True, day_pnl_usdt=0, rules=rules)
    assert gate.ok
    assert gate.stop_price == stop


def test_unrealized_day_loss_cannot_change_a_buy_outcome():
    """Finding 9, pinned as behaviour rather than fixed.

    `unrealized_pnl_usdt` is non-zero only while a position is open (see
    cycle.unrealized_pnl), and an open position is exactly what `already_long`
    rejects. So the term only ever changes the rejection *label*, never whether
    the BUY happens. This test exists so nobody assumes the unrealized day-loss
    halt does more than it does; making it bite means forcing an exit, which is
    a product decision.
    """
    from bot.cycle import unrealized_pnl
    from bot.hands import Position

    buy = TradeIntent("BUY", 0.9, "go", "trend")
    s = _settings(max_day_loss_usdt=20.0)

    class _Hands:
        def __init__(self, pos):
            self.position = pos

    # Flat: the caller can only ever supply 0.0, however far under water the last
    # trade went -- so the term cannot contribute to a BUY that is otherwise ok.
    assert unrealized_pnl(_Hands(Position()), 100_000.0) == 0.0
    flat = decide(buy, _snap(bot_qty=0.0), s, session_ok=True, day_pnl_usdt=0.0, unrealized_pnl_usdt=0.0)
    assert flat.ok is True

    # Holding: the term is non-zero exactly here, and here the BUY is already
    # rejected. Only the label differs.
    open_pos = Position(qty=0.0002, entry=350_000.0)
    assert unrealized_pnl(_Hands(open_pos), 100_000.0) < 0
    held = decide(buy, _snap(bot_qty=0.0002), s, session_ok=True, day_pnl_usdt=0.0, unrealized_pnl_usdt=-50.0)
    without = decide(buy, _snap(bot_qty=0.0002), s, session_ok=True, day_pnl_usdt=0.0, unrealized_pnl_usdt=0.0)
    assert held.ok is False and without.ok is False
    assert held.rule == "day_loss" and without.rule == "already_long"


def test_buy_refused_when_writes_exhausted():
    gate = decide(
        TradeIntent("BUY", 1.0, "", "trend"), _snap(),
        _settings(max_writes_per_hour=2),
        session_ok=True, day_pnl_usdt=0.0,
        write_counts=WriteCounts(writes_1h=2, entries_24h=0),
    )
    assert (gate.ok, gate.rule) == (False, "rate_limit")


def test_sell_is_never_rate_limited():
    snap = _snap(bot_qty=0.001, bot_avg_entry=100_000.0)
    gate = decide(
        TradeIntent("SELL", 1.0, "", "trend"), snap,
        _settings(max_writes_per_hour=1),
        session_ok=True, day_pnl_usdt=0.0,
        write_counts=WriteCounts(writes_1h=10_000, entries_24h=10_000),
    )
    assert gate.ok and gate.action == "SELL"


def test_no_write_counts_means_no_limiter():
    gate = decide(
        TradeIntent("BUY", 1.0, "", "trend"), _snap(),
        _settings(max_writes_per_hour=1),
        session_ok=True, day_pnl_usdt=0.0,
    )
    assert gate.ok and gate.rule == "ok_buy"


def test_a_spent_budget_refuses_buy_and_still_allows_sell():
    """No clock is manipulated here (F6): this proves the collar's own gating
    logic reads a spent ``WriteCounts`` the same way regardless of how it got
    spent -- refuse the entry, never the exit. The clock-jump scenarios
    themselves (backward AND forward) belong at the ``WriteMeter`` layer,
    which is what actually takes ``now_ms`` as an input -- see
    ``test_ratelimit.py``'s clock-skew tests."""
    spent = WriteCounts(writes_1h=10_000, entries_24h=10_000)
    settings = _settings(max_writes_per_hour=30)
    buy = decide(TradeIntent("BUY", 1.0, "", "trend"), _snap(), settings,
                 session_ok=True, day_pnl_usdt=0.0, write_counts=spent)
    held = _snap(bot_qty=0.001, bot_avg_entry=100_000.0)
    sell = decide(TradeIntent("SELL", 1.0, "", "trend"), held, settings,
                  session_ok=True, day_pnl_usdt=0.0, write_counts=spent)
    assert (buy.ok, buy.rule) == (False, "rate_limit")
    assert sell.ok and sell.action == "SELL"


def test_day_loss_outranks_rate_limit():
    # Ordering is observable in the audit; day_loss is the more serious fact.
    gate = decide(
        TradeIntent("BUY", 1.0, "", "trend"), _snap(),
        _settings(max_writes_per_hour=1, max_day_loss_usdt=10.0),
        session_ok=True, day_pnl_usdt=-50.0,
        write_counts=WriteCounts(writes_1h=10_000, entries_24h=0),
    )
    assert gate.rule == "day_loss"
