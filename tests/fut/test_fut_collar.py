import pytest

from fut.collar import check
from fut.settings import FutSettings
from fut.types import FutIntent, FutPosition
from tests.fut.helpers import SPEC, make_snap

FLAT = FutPosition()
OPEN = FutPosition(side="long", contracts=2, entry=76000.0, stop=75900.0, liq=380.0, margin=15.2)


def gate(action, snap=None, *, position=FLAT, balance=450.0, day_pnl=0.0, spec=SPEC, confidence=0.8, **settings):
    return check(FutIntent(action, confidence, "r"), snap or make_snap(), position=position, balance=balance,
                 day_pnl_usdt=day_pnl, spec=spec, settings=FutSettings(**settings))


def test_long_sizes_off_ask_plus_slippage_with_atr_stop_below():
    g = gate("LONG")
    assert (g.ok, g.rule, g.side, g.contracts, g.leverage) == (True, "ok_open", "long", 2, 1)
    assert g.price == pytest.approx(76000.1 * 1.0002)
    assert g.stop == pytest.approx(75939.2)
    assert g.liq == pytest.approx(g.price * 0.005)
    assert g.notional == pytest.approx(2 * 0.0001 * g.price)
    assert g.margin == pytest.approx(g.notional)


def test_short_sizes_off_bid_minus_slippage_with_stop_above():
    g = gate("SHORT")
    assert (g.ok, g.side, g.contracts) == (True, "short", 2)
    assert g.price == pytest.approx(76000.0 * 0.9998)
    assert g.stop == pytest.approx(76060.8)
    assert g.liq == pytest.approx(g.price * (1 + 1 - 0.005))


def test_leverage_three_scales_exposure_not_margin_budget():
    g = gate("LONG", leverage=3)
    assert g.ok and g.contracts == 7 and g.leverage == 3
    assert g.margin == pytest.approx(g.notional / 3)


def test_stop_too_close_to_liquidation_is_refused():
    g = gate("LONG", make_snap(atr_1m=20000.0), leverage=3, max_stop_pct=0.5)
    assert (g.ok, g.rule) == (False, "liq_too_close")


def test_hold_invalid_and_close_rules():
    assert gate("HOLD").rule == "hold"
    assert gate("BUY").rule == "action"
    assert gate("CLOSE").rule == "flat"
    closing = gate("CLOSE", position=OPEN)
    assert (closing.ok, closing.rule, closing.side, closing.contracts) == (True, "ok_close", "long", 2)


def test_close_passes_even_when_stale_or_after_day_loss():
    assert gate("CLOSE", make_snap(stale=True), position=OPEN, day_pnl=-100.0).ok


@pytest.mark.parametrize("kwargs, rule", [
    (dict(snap=make_snap(stale=True)), "stale"),
    (dict(spec=SPEC.__class__(**{**SPEC.__dict__, "state": 1})), "contract_state"),
    (dict(position=OPEN), "already_open"),
    (dict(day_pnl=-20.0), "day_loss"),
    (dict(confidence=float("nan")), "confidence"),
    (dict(snap=make_snap(atr_1m=None)), "atr"),
    (dict(snap=make_snap(bid=0.0, ask=0.0, last=0.0)), "no_price"),
    (dict(balance=0.0), "no_cash"),
    (dict(balance=100.0), "dust"),
])
def test_entry_refusals(kwargs, rule):
    g = gate("LONG", **kwargs)
    assert (g.ok, g.rule) == (False, rule)


def test_min_confidence_is_enforced():
    assert gate("LONG", confidence=0.4, min_confidence=0.5).rule == "confidence"


def test_leverage_above_contract_max_is_refused():
    small = SPEC.__class__(**{**SPEC.__dict__, "max_leverage": 2})
    assert gate("LONG", spec=small, leverage=3).rule == "leverage"
