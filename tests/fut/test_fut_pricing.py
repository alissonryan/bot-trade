import pytest

from fut.pricing import fill_price, liquidation_price, round_to_unit


def test_buy_fills_at_ask_plus_slippage_and_sell_at_bid_minus_slippage():
    assert fill_price(bid=100.0, ask=101.0, last=100.5, buy=True, slippage_bps=10) == pytest.approx(101.0 * 1.001)
    assert fill_price(bid=100.0, ask=101.0, last=100.5, buy=False, slippage_bps=10) == pytest.approx(100.0 * 0.999)


def test_missing_side_falls_back_to_last_and_nothing_valid_is_none():
    assert fill_price(bid=0.0, ask=float("nan"), last=100.5, buy=True, slippage_bps=0) == 100.5
    assert fill_price(bid=0.0, ask=0.0, last=0.0, buy=True, slippage_bps=0) is None


def test_negative_or_non_finite_slippage_is_none():
    assert fill_price(bid=100.0, ask=101.0, last=100.0, buy=True, slippage_bps=-1) is None
    assert fill_price(bid=100.0, ask=101.0, last=100.0, buy=True, slippage_bps=float("inf")) is None


def test_isolated_liquidation_prices():
    assert liquidation_price("long", 100.0, 1, 0.005) == pytest.approx(0.5)
    assert liquidation_price("long", 100.0, 3, 0.005) == pytest.approx(67.1666666)
    assert liquidation_price("short", 100.0, 3, 0.005) == pytest.approx(132.8333333)
    with pytest.raises(ValueError):
        liquidation_price("flat", 100.0, 1, 0.005)


def test_round_to_unit():
    assert round_to_unit(75939.2849, 0.1, up=False) == 75939.2
    assert round_to_unit(76091.31, 0.1, up=True) == 76091.4
    assert round_to_unit(76091.4, 0.1, up=True) == 76091.4
