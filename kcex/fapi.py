"""Public KCEX perpetual-futures REST (``/fapi/v1/contract/...``). GET only, never authenticated.

Paper futures must never carry a session: the client is built with ``token=""`` so an
exported ``KCEX_TOKEN`` cannot leak into these calls. See docs/kcex-futures-api.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kcex.client import KcexClient, KcexError
from kcex.fws import FutDeal, FutTicker, _num, parse_deal, parse_levels


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    contract_size: float
    min_vol: int
    max_vol: int
    price_unit: float
    taker_fee: float
    maker_fee: float
    mmr: float
    max_leverage: int
    state: int

    @classmethod
    def from_detail(cls, data: Any) -> "ContractSpec":
        data = data if isinstance(data, dict) else {}

        def req(key: str) -> float:
            value = _num(data.get(key))
            if value is None:
                raise KcexError(f"futures contract detail missing {key}", {"status": None})
            return value

        spec = cls(
            symbol=str(data.get("symbol") or ""),
            contract_size=req("contractSize"),
            min_vol=int(req("minVol")),
            max_vol=int(req("maxVol")),
            price_unit=req("priceUnit"),
            taker_fee=req("takerFeeRate"),
            maker_fee=req("makerFeeRate"),
            mmr=req("maintenanceMarginRate"),
            max_leverage=int(req("maxLeverage")),
            state=int(req("state")),
        )
        if (spec.contract_size <= 0 or spec.min_vol < 1 or spec.max_vol < spec.min_vol
                or spec.price_unit <= 0 or spec.taker_fee < 0 or not 0 < spec.mmr < 1
                or spec.max_leverage < 1):
            raise KcexError(f"futures contract detail has invalid values: {spec}", {"status": None})
        return spec


class FuturesPublic:
    def __init__(self, client: KcexClient | None = None):
        self.client = client or KcexClient(token="", user_device="")

    def _data(self, path: str, **params: Any) -> Any:
        payload = self.client.get(path, **params)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise KcexError(f"GET {path} unexpected envelope", payload if isinstance(payload, dict) else {})
        return payload.get("data")

    def contract_detail(self, symbol: str) -> ContractSpec:
        return ContractSpec.from_detail(self._data("/fapi/v1/contract/detail", symbol=symbol))

    def ticker(self, symbol: str) -> FutTicker:
        data = self._data("/fapi/v1/contract/ticker", symbol=symbol)
        data = data if isinstance(data, dict) else {}
        last = _num(data.get("lastPrice"))
        if last is None or last <= 0:
            raise KcexError("futures ticker without lastPrice", {"status": None})
        return FutTicker(
            ts_ms=int(_num(data.get("timestamp")) or 0),
            last=last,
            bid=_num(data.get("bid1")) or 0.0,
            ask=_num(data.get("ask1")) or 0.0,
            fair=_num(data.get("fairPrice")) or 0.0,
            index=_num(data.get("indexPrice")) or 0.0,
            funding_rate=_num(data.get("fundingRate")) or 0.0,
        )

    def depth(self, symbol: str, limit: int = 50):
        data = self._data(f"/fapi/v1/contract/depth/{symbol}", limit=limit)
        data = data if isinstance(data, dict) else {}
        version = _num(data.get("version"))
        if version is None:
            raise KcexError("futures depth without version", {"status": None})
        return int(version), parse_levels(data.get("bids")), parse_levels(data.get("asks"))

    def deals(self, symbol: str, limit: int = 100) -> list[FutDeal]:
        data = self._data(f"/fapi/v1/contract/deals/{symbol}", limit=limit)
        items = data if isinstance(data, list) else []
        return sorted((d for d in (parse_deal(x) for x in items) if d is not None), key=lambda d: d.ts_ms)

    def klines_1m(self, symbol: str, start_s: int, end_s: int):
        data = self._data(f"/fapi/v1/contract/kline/{symbol}", interval="Min1", start=int(start_s), end=int(end_s))
        data = data if isinstance(data, dict) else {}
        columns = [data.get(key) or [] for key in ("time", "open", "high", "low", "close", "vol")]
        rows = []
        for values in zip(*columns):
            nums = [_num(v) for v in values]
            if any(n is None for n in nums):
                continue
            rows.append((int(nums[0]), nums[1], nums[2], nums[3], nums[4], nums[5]))
        return rows

    def funding(self, symbol: str) -> tuple[float, int | None]:
        data = self._data(f"/fapi/v1/contract/funding_rate/{symbol}")
        data = data if isinstance(data, dict) else {}
        rate = _num(data.get("fundingRate"))
        if rate is None:
            raise KcexError("futures funding without fundingRate", {"status": None})
        nxt = _num(data.get("nextSettleTime"))
        return rate, int(nxt) if nxt else None
