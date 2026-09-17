from fut.types import FutSnapshot
from kcex.fapi import ContractSpec

SPEC = ContractSpec(symbol="BTC_USDT", contract_size=0.0001, min_vol=1, max_vol=714000, price_unit=0.1,
                    taker_fee=0.0001, maker_fee=0.0, mmr=0.005, max_leverage=125, state=0)


def make_snap(**overrides) -> FutSnapshot:
    base = dict(ts_ms=0, last=76000.0, bid=76000.0, ask=76000.1, fair=76000.0, index=76000.0,
                funding_rate=0.0001, next_funding_ms=None, spread_bps=0.013, imbalance=0.0,
                depth_bps={}, returns_bps={}, flow={}, atr_1m=30.0, stale=False)
    base.update(overrides)
    return FutSnapshot(**base)
