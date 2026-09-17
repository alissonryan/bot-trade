"""Futures paper settings. Every number from the spec is an env var except MAX_LEVERAGE."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from bot.settings import Settings
from kcex.fws import DEFAULT_FUT_WS_URL

MAX_LEVERAGE = 3


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
class FutSettings:
    symbol: str = "BTC_USDT"
    leverage: int = 1
    margin_usdt: float = 20.0
    max_balance_pct: float = 0.05
    max_day_loss_usdt: float = 20.0
    starting_usdt: float = 450.0
    slippage_bps: float = 2.0
    atr_period: int = 14
    atr_mult: float = 2.0
    min_stop_pct: float = 0.001
    max_stop_pct: float = 0.01
    liq_stop_ratio: float = 0.5
    max_hold_s: float = 300.0
    min_hold_s: float = 0.0
    min_confidence: float = 0.0
    jev_every_s: float = 2.0
    jev_model: str = "jev-latest"
    typesafe_api_key: str = ""
    jev_usd_per_mtok: float = 0.042
    jev_timeout_s: float = 2.0
    wake_threshold: float = 0.6
    move_cost_bps: float = 3.0
    llm_cooldown_s: float = 10.0
    llm_timeout_s: float = 8.0
    stale_price_bps: float = 5.0
    stale_market_s: float = 5.0
    unmonitored_s: float = 60.0
    llm_reasoning: bool = False
    ws_url: str = DEFAULT_FUT_WS_URL
    shadow_seed: int = 7
    llm: Settings = field(default_factory=Settings.from_env)

    def __post_init__(self) -> None:
        if self.symbol != "BTC_USDT":
            raise ValueError("futures paper supports BTC_USDT only")
        if isinstance(self.leverage, bool) or not isinstance(self.leverage, int) or not 1 <= self.leverage <= MAX_LEVERAGE:
            raise ValueError(f"FUT_LEVERAGE must be an integer in 1..{MAX_LEVERAGE}")
        positive = ("margin_usdt", "starting_usdt", "atr_mult", "min_stop_pct", "max_stop_pct",
                    "max_hold_s", "jev_every_s", "jev_timeout_s", "llm_timeout_s",
                    "stale_market_s", "unmonitored_s")
        for name in positive:
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and > 0")
        non_negative = ("max_day_loss_usdt", "slippage_bps", "min_confidence", "jev_usd_per_mtok",
                        "move_cost_bps", "llm_cooldown_s", "stale_price_bps", "min_hold_s")
        for name in non_negative:
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and >= 0")
        if self.min_hold_s >= self.max_hold_s:
            raise ValueError("min_hold_s must be < max_hold_s")
        if not 0 < self.max_balance_pct <= 1:
            raise ValueError("max_balance_pct must be in (0, 1]")
        if self.min_stop_pct > self.max_stop_pct:
            raise ValueError("min_stop_pct must be <= max_stop_pct")
        if not 0 < self.liq_stop_ratio < 1:
            raise ValueError("liq_stop_ratio must be in (0, 1)")
        if not 0 < self.wake_threshold <= 1:
            raise ValueError("wake_threshold must be in (0, 1]")
        if self.atr_period < 1:
            raise ValueError("atr_period must be >= 1")

    @property
    def uses_mock_jev(self) -> bool:
        return self.jev_model == "mock" or not self.typesafe_api_key

    @classmethod
    def from_env(cls) -> "FutSettings":
        raw_ws = os.getenv("FUT_WS_URL", "").strip()
        ws_url = "" if raw_ws == "-" else (raw_ws or DEFAULT_FUT_WS_URL)
        return cls(
            symbol=os.getenv("FUT_SYMBOL", "BTC_USDT").strip() or "BTC_USDT",
            leverage=_i("FUT_LEVERAGE", 1),
            margin_usdt=_f("FUT_MARGIN_USDT", 20.0),
            max_balance_pct=_f("FUT_MAX_BALANCE_PCT", 0.05),
            max_day_loss_usdt=_f("FUT_MAX_DAY_LOSS_USDT", 20.0),
            starting_usdt=_f("FUT_PAPER_STARTING_USDT", 450.0),
            slippage_bps=_f("FUT_SLIPPAGE_BPS", 2.0),
            atr_period=_i("FUT_ATR_PERIOD", 14),
            atr_mult=_f("FUT_ATR_MULT", 2.0),
            min_stop_pct=_f("FUT_MIN_STOP_PCT", 0.001),
            max_stop_pct=_f("FUT_MAX_STOP_PCT", 0.01),
            liq_stop_ratio=_f("FUT_LIQ_STOP_RATIO", 0.5),
            max_hold_s=_f("FUT_MAX_HOLD_SECONDS", 300.0),
            min_hold_s=_f("FUT_MIN_HOLD_SECONDS", 0.0),
            min_confidence=_f("FUT_MIN_CONFIDENCE", 0.0),
            jev_every_s=_f("FUT_JEV_EVERY_SECONDS", 2.0),
            jev_model=os.getenv("FUT_JEV_MODEL", "jev-latest").strip() or "jev-latest",
            typesafe_api_key=os.getenv("TYPESAFE_API_KEY", "").strip(),
            jev_usd_per_mtok=_f("FUT_JEV_USD_PER_MTOK", 0.042),
            jev_timeout_s=_f("FUT_JEV_TIMEOUT_SECONDS", 2.0),
            wake_threshold=_f("FUT_WAKE_THRESHOLD", 0.6),
            move_cost_bps=_f("FUT_MOVE_COST_BPS", 3.0),
            llm_cooldown_s=_f("FUT_LLM_COOLDOWN_SECONDS", 10.0),
            llm_timeout_s=_f("FUT_LLM_TIMEOUT_SECONDS", 8.0),
            stale_price_bps=_f("FUT_STALE_PRICE_BPS", 5.0),
            stale_market_s=_f("FUT_STALE_MARKET_SECONDS", 5.0),
            unmonitored_s=_f("FUT_UNMONITORED_SECONDS", 60.0),
            llm_reasoning=_b("FUT_LLM_REASONING", False),
            ws_url=ws_url,
            shadow_seed=_i("FUT_SHADOW_SEED", 7),
            llm=Settings.from_env(),
        )
