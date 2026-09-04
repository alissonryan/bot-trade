from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.collar import decide
from bot.settings import Settings
from bot.types import Bar, Snapshot, TradeIntent


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
