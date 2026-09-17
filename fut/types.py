from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class FutPosition:
    side: str | None = None  # "long" | "short"
    contracts: int = 0
    entry: float = 0.0
    stop: float | None = None
    liq: float | None = None
    margin: float = 0.0
    leverage: int = 1
    opened_ms: int = 0
    funding_through_ms: int = 0

    def is_open(self) -> bool:
        return self.side in ("long", "short") and self.contracts > 0


@dataclass(frozen=True)
class FutSnapshot:
    ts_ms: int
    last: float
    bid: float
    ask: float
    fair: float
    index: float
    funding_rate: float
    next_funding_ms: int | None
    spread_bps: float
    imbalance: float
    depth_bps: dict[str, dict[str, float]]
    returns_bps: dict[str, float | None]
    flow: dict[str, dict[str, Any]]
    atr_1m: float | None
    stale: bool

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last

    def compact(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JevVerdict:
    direction: str  # "up" | "down" | "flat"
    direction_conf: float
    beats_cost: float | None
    flow_aligned: float
    regime: str
    exit_now: float | None
    latency_ms: int
    input_tokens: int
    model: str
    error: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    probabilities: dict[str, float] | None = None

    def answers(self) -> dict[str, Any]:
        return {"direction": self.direction, "direction_conf": self.direction_conf,
                "beats_cost": self.beats_cost, "flow_aligned": self.flow_aligned,
                "regime": self.regime, "exit_now": self.exit_now}

    @classmethod
    def failed(cls, error: str, *, latency_ms: int, model: str, state: dict[str, Any]) -> "JevVerdict":
        return cls("flat", 0.0, 0.0, 0.0, "unknown", None, latency_ms, 0, model, error=error, state=state)


@dataclass(frozen=True)
class FutIntent:
    action: str  # LONG | SHORT | CLOSE | HOLD
    confidence: float
    reason: str


@dataclass(frozen=True)
class FutGate:
    ok: bool
    rule: str
    action: str
    side: str | None = None
    contracts: int = 0
    price: float = 0.0
    notional: float = 0.0
    margin: float = 0.0
    stop: float | None = None
    liq: float | None = None
    leverage: int = 1
