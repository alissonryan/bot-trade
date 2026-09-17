import pytest

from fut.settings import MAX_LEVERAGE, FutSettings
from fut.types import FutPosition, FutSnapshot, JevVerdict
from kcex.fws import DEFAULT_FUT_WS_URL


def test_defaults_match_the_spec():
    s = FutSettings()
    assert (s.leverage, s.margin_usdt, s.max_balance_pct, s.starting_usdt) == (1, 20.0, 0.05, 450.0)
    assert (s.max_hold_s, s.jev_every_s, s.llm_cooldown_s, s.llm_timeout_s) == (300.0, 2.0, 10.0, 8.0)
    assert (s.stale_price_bps, s.wake_threshold, s.move_cost_bps) == (5.0, 0.6, 3.0)
    assert s.ws_url == DEFAULT_FUT_WS_URL
    assert s.min_hold_s == 0.0
    assert (s.max_spread_bps, s.min_move_mult, s.max_entries_per_hour) == (0.0, 0.0, 0)
    assert (s.wake_streak, s.wake_regimes) == (1, ())
    assert MAX_LEVERAGE == 3


def test_from_env_reads_overrides(monkeypatch):
    monkeypatch.setenv("FUT_LEVERAGE", "3")
    monkeypatch.setenv("FUT_MARGIN_USDT", "15")
    monkeypatch.setenv("FUT_MAX_HOLD_SECONDS", "120")
    monkeypatch.setenv("FUT_WS_URL", "-")
    monkeypatch.setenv("FUT_LLM_REASONING", "1")
    monkeypatch.setenv("FUT_MIN_HOLD_SECONDS", "60")
    monkeypatch.setenv("FUT_MAX_SPREAD_BPS", "2.5")
    monkeypatch.setenv("FUT_MIN_MOVE_MULT", "2")
    monkeypatch.setenv("FUT_MAX_ENTRIES_PER_HOUR", "3")
    monkeypatch.setenv("FUT_WAKE_STREAK", "4")
    monkeypatch.setenv("FUT_WAKE_REGIMES", " Trend, VOLATILE ")
    s = FutSettings.from_env()
    assert s.min_hold_s == 60.0
    assert (s.leverage, s.margin_usdt, s.max_hold_s, s.ws_url, s.llm_reasoning) == (3, 15.0, 120.0, "", True)
    assert (s.max_spread_bps, s.min_move_mult, s.max_entries_per_hour) == (2.5, 2.0, 3)
    assert (s.wake_streak, s.wake_regimes) == (4, ("trend", "volatile"))


@pytest.mark.parametrize("leverage", [0, 4, 125])
def test_leverage_outside_one_to_three_is_refused(leverage):
    with pytest.raises(ValueError):
        FutSettings(leverage=leverage)


def test_other_invalid_values_are_refused():
    with pytest.raises(ValueError):
        FutSettings(slippage_bps=-1)
    with pytest.raises(ValueError):
        FutSettings(min_stop_pct=0.02, max_stop_pct=0.01)
    with pytest.raises(ValueError):
        FutSettings(symbol="ETH_USDT")
    with pytest.raises(ValueError):
        FutSettings(max_spread_bps=-1)
    with pytest.raises(ValueError):
        FutSettings(min_move_mult=float("inf"))
    with pytest.raises(ValueError):
        FutSettings(max_entries_per_hour=-1)
    with pytest.raises(ValueError):
        FutSettings(wake_streak=0)
    with pytest.raises(ValueError):
        FutSettings(wake_regimes=("range", "unknown"))


def test_mock_jev_without_key_or_with_mock_model():
    assert FutSettings().uses_mock_jev
    assert not FutSettings(typesafe_api_key="k").uses_mock_jev
    assert FutSettings(typesafe_api_key="k", jev_model="mock").uses_mock_jev


def test_position_and_snapshot_helpers():
    assert not FutPosition().is_open()
    assert FutPosition(side="short", contracts=2, entry=1.0).is_open()
    snap = FutSnapshot(1, 100.0, 99.0, 101.0, 100.0, 100.0, 0.0, None, 1.0, 0.0, {}, {}, {}, None, False)
    assert snap.mid == 100.0
    assert FutSnapshot(1, 100.0, 0.0, 101.0, 0.0, 0.0, 0.0, None, 0.0, 0.0, {}, {}, {}, None, True).mid == 100.0
    assert snap.compact()["bid"] == 99.0


def test_failed_verdict_carries_error():
    v = JevVerdict.failed("boom", latency_ms=5, model="jev-latest", state={"a": 1})
    assert v.error == "boom" and v.direction == "flat" and v.state == {"a": 1}
    assert set(v.answers()) == {"direction", "direction_conf", "beats_cost", "flow_aligned", "regime", "exit_now"}


def test_min_hold_must_be_non_negative_and_below_max_hold():
    with pytest.raises(ValueError):
        FutSettings(min_hold_s=-1.0)
    with pytest.raises(ValueError):
        FutSettings(min_hold_s=300.0, max_hold_s=300.0)
