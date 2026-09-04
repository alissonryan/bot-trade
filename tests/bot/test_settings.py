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
