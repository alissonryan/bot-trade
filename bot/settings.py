# bot/settings.py
from __future__ import annotations

import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    mode: str
    symbol: str
    cycle_minutes: int
    wake_move_pct: float
    max_order_usdt: float
    max_portfolio_pct: float
    max_day_loss_usdt: float
    atr_period: int
    atr_mult: float
    min_stop_pct: float
    max_stop_pct: float
    llm_daily_budget_usd: float
    llm_model: str
    openrouter_api_key: str
    openrouter_base_url: str
    qty_scale: int
    paper_slippage_bps: float
    paper_starting_usdt: float
    ws_url: str
    stale_ms: int

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            mode=os.getenv("MODE", "paper").strip().lower(),
            symbol=os.getenv("SYMBOL", "BTC_USDT").strip(),
            cycle_minutes=_i("CYCLE_MINUTES", 15),
            wake_move_pct=_f("WAKE_MOVE_PCT", 0.004),
            max_order_usdt=_f("MAX_ORDER_USDT", 20),
            max_portfolio_pct=_f("MAX_PORTFOLIO_PCT", 0.05),
            max_day_loss_usdt=_f("MAX_DAY_LOSS_USDT", 20),
            atr_period=_i("ATR_PERIOD", 14),
            atr_mult=_f("ATR_MULT", 2.0),
            min_stop_pct=_f("MIN_STOP_PCT", 0.004),
            max_stop_pct=_f("MAX_STOP_PCT", 0.04),
            llm_daily_budget_usd=_f("LLM_DAILY_BUDGET_USD", 2.0),
            llm_model=os.getenv("LLM_MODEL", "").strip(),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            openrouter_base_url=os.getenv(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ).rstrip("/"),
            qty_scale=_i("QTY_SCALE", 5),
            paper_slippage_bps=_f("PAPER_SLIPPAGE_BPS", 5.0),
            paper_starting_usdt=_f("PAPER_STARTING_USDT", 450.0),
            ws_url=os.getenv("KCEX_WS_URL", "").strip(),
            stale_ms=_i("STALE_MS", 30000),
        )
