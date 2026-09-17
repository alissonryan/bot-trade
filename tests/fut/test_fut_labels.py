import pytest

from fut.labels import label_state
from fut.settings import FutSettings
from fut.types import FutPosition
from tests.fut.helpers import make_snap


def labels(**overrides):
    return label_state(make_snap(**overrides), FutPosition(), now_ms=100_000, settings=FutSettings())


@pytest.mark.parametrize("cvd, expected", [
    (60.0, "strong_buy"),
    (20.0, "mild_buy"),
    (0.0, "balanced"),
    (-20.0, "mild_sell"),
    (-60.0, "strong_sell"),
])
def test_flow_buckets_include_boundaries(cvd, expected):
    snap_flow = {"30s": {"buy": 50.0, "sell": 50.0, "cvd": cvd},
                 "120s": {"buy": 50.0, "sell": 50.0, "cvd": cvd}}
    result = labels(flow=snap_flow)
    assert result["flow_30s"] == expected
    assert result["flow_120s"] == expected


def test_missing_flow_is_unknown_not_balanced():
    assert labels(flow={})["flow_30s"] == "unknown"
    assert labels(flow={"30s": {"buy": 0, "sell": 0, "cvd": 0}})["flow_30s"] == "unknown"


@pytest.mark.parametrize("value, expected", [
    (6.0, "rising_fast"), (3.0, "rising"), (0.0, "flat"), (-3.0, "falling"), (-6.0, "falling_fast")
])
def test_price_buckets_use_round_trip_cost_boundaries(value, expected):
    result = labels(returns_bps={"10s": value, "60s": value})
    assert result["price_10s"] == expected
    assert result["price_60s"] == expected


def test_missing_price_is_unknown():
    result = labels(returns_bps={"10s": None, "60s": None})
    assert result["price_10s"] == "unknown"
    assert result["price_60s"] == "unknown"


@pytest.mark.parametrize("imbalance, expected", [(0.25, "bid_heavy"), (0.0, "balanced"), (-0.25, "ask_heavy")])
def test_book_buckets_include_boundaries(imbalance, expected):
    assert labels(imbalance=imbalance)["book"] == expected


@pytest.mark.parametrize("spread, expected", [(1.5, "tight"), (3.0, "normal"), (3.1, "wide")])
def test_spread_buckets_use_configured_limit(spread, expected):
    snap = make_snap(spread_bps=spread)
    assert label_state(snap, FutPosition(), now_ms=0, settings=FutSettings(max_spread_bps=3.0))["spread"] == expected


def test_zero_spread_limit_uses_named_default():
    assert labels(spread_bps=3.0)["spread"] == "normal"
    assert labels(spread_bps=3.1)["spread"] == "wide"


@pytest.mark.parametrize("atr, expected", [(22.8, "pays_cost"), (11.4, "marginal"), (1.0, "too_quiet")])
def test_volatility_buckets_compare_atr_to_cost(atr, expected):
    assert labels(atr_1m=atr, bid=76000.0, ask=76000.0)["volatility_vs_cost"] == expected


def test_missing_volatility_is_unknown():
    assert labels(atr_1m=None)["volatility_vs_cost"] == "unknown"


@pytest.mark.parametrize("funding, expected", [(0.0002, "long_crowded"), (0.0, "neutral"), (-0.0002, "short_crowded")])
def test_funding_buckets_include_direction_and_unknown(funding, expected):
    assert labels(funding_rate=funding)["funding_pressure"] == expected
    assert labels(funding_rate=None)["funding_pressure"] == "unknown"


def test_open_position_gets_side_age_and_unrealized_buckets():
    position = FutPosition(side="long", contracts=1, entry=76000.0, stop=75900.0, opened_ms=0)
    result = label_state(make_snap(), position, now_ms=99_000, settings=FutSettings(max_hold_s=300.0))
    assert result["position_side"] == "long"
    assert result["age"] == "fresh"
    assert result["unrealized"] == "flat"

    losing = label_state(make_snap(last=75970.0, bid=75970.0, ask=75970.1), position,
                         now_ms=150_000, settings=FutSettings(max_hold_s=300.0))
    assert losing["age"] == "mid"
    assert losing["unrealized"] == "losing"

    old = label_state(make_snap(last=76100.0, bid=76100.0, ask=76100.1), position,
                      now_ms=300_000, settings=FutSettings(max_hold_s=300.0))
    assert old["age"] == "old"
    assert old["unrealized"] == "winning"


def test_open_position_with_missing_stop_or_bad_quote_is_unknown():
    position = FutPosition(side="short", contracts=1, entry=76000.0, stop=None, opened_ms=0)
    result = label_state(make_snap(last=0.0, bid=0.0, ask=0.0), position, now_ms=100_000,
                         settings=FutSettings())
    assert result["position_side"] == "short"
    assert result["unrealized"] == "unknown"


def test_labeled_state_contains_no_raw_market_numbers():
    result = labels()

    def values(value):
        if isinstance(value, dict):
            return [item for child in value.values() for item in values(child)]
        if isinstance(value, list):
            return [item for child in value for item in values(child)]
        return [value]

    assert all(not isinstance(value, (int, float)) for value in values(result))
