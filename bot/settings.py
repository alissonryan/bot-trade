# bot/settings.py
from __future__ import annotations

import os
import math
from dataclasses import dataclass

# Verified live against the exchange: see docs/kcex-spot-api.md. The single
# definition lives in kcex/ws.py so the client and the settings cannot drift.
from kcex.ws import DEFAULT_WS_URL


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
    min_confidence: float
    llm_daily_budget_usd: float
    llm_model: str
    openrouter_api_key: str
    openrouter_base_url: str
    llm_max_tokens: int
    llm_json_mode: bool
    llm_fallback_cost_usd: float
    qty_scale: int
    paper_slippage_bps: float
    paper_starting_usdt: float
    ws_enabled: bool
    ws_url: str
    poll_seconds: float
    stale_ms: int
    chart_port: int
    chart_host: str
    fill_confirm_tries: int
    fill_confirm_wait_s: float
    log_level: str
    tp_atr_mult: float = 0.0
    min_tp_pct: float = 0.006
    max_tp_pct: float = 0.06
    time_limit_minutes: float = 0.0
    cooldown_minutes: float = 0.0
    journal_enabled: bool = False
    max_writes_per_hour: int = 30
    max_entries_per_day: int = 20
    kill_writes_per_hour: int = 90

    def __post_init__(self) -> None:
        if not math.isfinite(self.cooldown_minutes) or self.cooldown_minutes < 0:
            raise ValueError("invalid cooldown: finite nonnegative minutes required")
        values = (self.tp_atr_mult, self.min_tp_pct, self.max_tp_pct, self.time_limit_minutes)
        if any(not math.isfinite(v) or v < 0 for v in values) or self.min_tp_pct > self.max_tp_pct:
            raise ValueError("invalid barrier configuration: finite nonnegative values and MIN_TP_PCT <= MAX_TP_PCT required")
        for name in ("max_writes_per_hour", "max_entries_per_day", "kill_writes_per_hour"):
            if getattr(self, name) < 0:
                raise ValueError(f"invalid {name}: nonnegative integer required (0 disables)")
        if 0 < self.kill_writes_per_hour < self.max_writes_per_hour:
            raise ValueError(
                "invalid KILL_WRITES_PER_HOUR: a nonzero ceiling below MAX_WRITES_PER_HOUR "
                "halts the process before the soft gate can refuse anything"
            )

    @classmethod
    def from_env(cls) -> Settings:
        raw_ws = os.getenv("KCEX_WS_URL", "").strip()
        if raw_ws == "-":
            ws_url = ""
        elif raw_ws == "":
            ws_url = DEFAULT_WS_URL
        else:
            ws_url = raw_ws

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
            min_confidence=_f("MIN_CONFIDENCE", 0.0),
            llm_daily_budget_usd=_f("LLM_DAILY_BUDGET_USD", 2.0),
            llm_model=os.getenv("LLM_MODEL", "").strip(),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            openrouter_base_url=os.getenv(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ).rstrip("/"),
            llm_max_tokens=_i("LLM_MAX_TOKENS", 200),
            llm_json_mode=_b("LLM_JSON_MODE", False),
            llm_fallback_cost_usd=_f("LLM_FALLBACK_COST_USD", 0.02),
            qty_scale=_i("QTY_SCALE", 5),
            paper_slippage_bps=_f("PAPER_SLIPPAGE_BPS", 5.0),
            paper_starting_usdt=_f("PAPER_STARTING_USDT", 450.0),
            ws_enabled=_b("WS_ENABLED", True),
            # `ws_url` keeps the `KCEX_WS_URL=-` escape hatch that forces
            # REST-only; an unset value falls back to the confirmed default.
            ws_url=ws_url,
            poll_seconds=_f("POLL_SECONDS", 5.0),
            stale_ms=_i("STALE_MS", 30000),
            chart_port=_i("CHART_PORT", 8765),
            chart_host=os.getenv("CHART_HOST", "127.0.0.1").strip() or "127.0.0.1",
            fill_confirm_tries=_i("FILL_CONFIRM_TRIES", 6),
            fill_confirm_wait_s=_f("FILL_CONFIRM_WAIT_S", 0.5),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            tp_atr_mult=_f("TP_ATR_MULT", 0.0),
            min_tp_pct=_f("MIN_TP_PCT", 0.006),
            max_tp_pct=_f("MAX_TP_PCT", 0.06),
            time_limit_minutes=_f("TIME_LIMIT_MINUTES", 0.0),
            cooldown_minutes=_f("COOLDOWN_MINUTES", 0.0),
            journal_enabled=_b("JOURNAL_ENABLED", False),
            max_writes_per_hour=_i("MAX_WRITES_PER_HOUR", 30),
            max_entries_per_day=_i("MAX_ENTRIES_PER_DAY", 20),
            kill_writes_per_hour=_i("KILL_WRITES_PER_HOUR", 90),
        )
