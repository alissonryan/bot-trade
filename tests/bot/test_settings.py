# tests/bot/test_settings.py
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.settings import DEFAULT_WS_URL, Settings

PREFIXES = ("MODE", "SYMBOL", "CYCLE", "WAKE", "MAX_", "ATR", "LLM_", "OPENROUTER", "MIN_", "WS_", "KCEX_WS", "POLL_", "FILL_", "LOG_", "PAPER_", "STALE_")


def _clear(monkeypatch):
    for key in list(os.environ):
        if key.startswith(PREFIXES):
            monkeypatch.delenv(key, raising=False)


def test_defaults(monkeypatch):
    _clear(monkeypatch)
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
    assert s.min_confidence == 0.0
    assert s.llm_daily_budget_usd == 2.0
    assert s.llm_max_tokens == 200
    assert s.llm_json_mode is False
    assert s.llm_fallback_cost_usd == 0.02
    assert s.qty_scale == 5
    assert s.ws_enabled is True
    assert s.ws_url == DEFAULT_WS_URL
    assert s.poll_seconds == 5.0
    assert s.stale_ms == 30000
    assert s.fill_confirm_tries == 6
    assert s.fill_confirm_wait_s == 0.5
    assert s.log_level == "INFO"


def test_bool_and_ws_overrides(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("WS_ENABLED", "0")
    monkeypatch.setenv("LLM_JSON_MODE", "true")
    monkeypatch.setenv("KCEX_WS_URL", "")
    s = Settings.from_env()
    assert s.ws_enabled is False
    assert s.llm_json_mode is True
    assert s.ws_url == DEFAULT_WS_URL  # empty override falls back to the verified URL


def test_ws_url_default_is_captured_public(monkeypatch):
    monkeypatch.delenv("KCEX_WS_URL", raising=False)
    s = Settings.from_env()
    assert s.ws_url == "wss://wbs.kcex.com/ws?platform=web"


def test_ws_url_dash_disables(monkeypatch):
    monkeypatch.setenv("KCEX_WS_URL", "-")
    s = Settings.from_env()
    assert s.ws_url == ""


def test_chart_bind_defaults(monkeypatch):
    monkeypatch.delenv("CHART_PORT", raising=False)
    monkeypatch.delenv("CHART_HOST", raising=False)
    s = Settings.from_env()
    assert s.chart_port == 8765
    assert s.chart_host == "127.0.0.1"
