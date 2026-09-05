from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.collar import decide, stop_for_entry
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
