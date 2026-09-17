import pytest

from fut.market import MarketState
from fut.settings import FutSettings
from kcex.fws import FutDeal, FutDepth, FutFair, FutTicker
from tests.fut.helpers import SPEC


def ticker(bid, ask, last=None, ts=0, fair=0.0, funding=0.0001):
    return FutTicker(ts, last if last is not None else bid, bid, ask, fair, 0.0, funding)


def market(**settings):
    return MarketState(FutSettings(**settings), SPEC)


def test_snapshot_uses_synced_book_for_bid_ask_depth_and_imbalance():
    m = market()
    m.load_book(10, [(100.0, 5), (99.98, 7), (99.0, 1)], [(100.02, 4), (101.0, 9)])
    m.apply(ticker(99.0, 102.0, last=100.0), now_ms=1000)
    s = m.snapshot(1000)
    assert (s.bid, s.ask, s.last) == (100.0, 100.02, 100.0)
    assert s.mid == pytest.approx(100.01)
    assert s.spread_bps == pytest.approx(0.02 / 100.01 * 10_000)
    assert s.depth_bps["5"] == {"bid": 12, "ask": 4}
    assert s.depth_bps["25"] == {"bid": 12, "ask": 4}
    assert s.imbalance == pytest.approx(0.5)


def test_falls_back_to_ticker_quotes_when_book_unsynced():
    m = market()
    m.apply(ticker(99.0, 101.0, last=100.0), now_ms=0)
    s = m.snapshot(0)
    assert (s.bid, s.ask) == (99.0, 101.0)
    assert s.depth_bps["5"] == {"bid": 0, "ask": 0}
    assert s.imbalance == 0.0


def test_stale_ws_uses_rest_ticker_quotes_and_neutral_depth():
    m = market(stale_market_s=5)
    m.load_book(10, [(100.0, 5)], [(100.1, 4)])
    m.apply(ticker(100.0, 100.1), now_ms=0, source="ws")
    m.apply(ticker(97.95, 98.05, last=98.0), now_ms=6_000, source="rest")

    s = m.snapshot(6_000)

    assert (s.bid, s.ask, s.last) == (97.95, 98.05, 98.0)
    assert s.depth_bps == {"5": {"bid": 0, "ask": 0}, "10": {"bid": 0, "ask": 0},
                           "25": {"bid": 0, "ask": 0}}
    assert s.imbalance == 0.0


def test_fair_funding_and_next_settle():
    m = market()
    m.apply(ticker(100.0, 100.0, fair=100.2, funding=0.0002), now_ms=0)
    m.apply(FutFair(0, 100.3), now_ms=1)
    m.set_funding(0.0003, 5000)
    s = m.snapshot(1)
    assert (s.fair, s.funding_rate, s.next_funding_ms) == (100.3, 0.0003, 5000)


def test_returns_use_mid_history():
    m = market()
    m.apply(ticker(100.0, 100.0), now_ms=0)
    m.apply(ticker(101.0, 101.0), now_ms=10_000)
    s = m.snapshot(10_000)
    assert s.returns_bps["10s"] == pytest.approx(100.0)
    assert s.returns_bps["60s"] is None


def test_flow_windows_split_aggressor_volume():
    m = market()
    m.apply(FutDeal(1000, 100.0, 10, "buy"), now_ms=1000)
    m.apply(FutDeal(20_000, 101.0, 4, "sell"), now_ms=20_000)
    s = m.snapshot(40_000)
    assert s.flow["30s"] == {"buy": 0, "sell": 4, "cvd": -4, "vwap": pytest.approx(101.0)}
    assert s.flow["120s"] == {"buy": 10, "sell": 4, "cvd": 6, "vwap": pytest.approx(1404 / 14)}
    assert market().snapshot(0).flow["30s"]["vwap"] is None


def test_rest_prices_do_not_make_the_market_fresh_for_entries():
    m = market(stale_market_s=5)
    m.apply(ticker(100.0, 100.0), now_ms=0, source="ws")
    assert m.snapshot(3000).stale is False
    m.apply(ticker(100.0, 100.0), now_ms=10_000, source="rest")
    assert m.last_event_ms == 10_000 and m.ws_last_ms == 0
    assert m.snapshot(10_000).stale is True


def test_no_price_is_stale():
    assert market().snapshot(0).stale is True


def test_atr_from_one_minute_bars():
    m = market(atr_period=14)
    m.set_bars([(i * 60, 100.0, 101.0, 99.0, 100.0, 1.0) for i in range(16)])
    assert m.snapshot(0).atr_1m == pytest.approx(2.0)


def test_depth_gap_reports_resync_needed():
    m = market()
    m.load_book(10, [(100.0, 5)], [(101.0, 5)])
    assert m.apply(FutDepth(0, 11, ((100.0, 6),), ()), now_ms=1) is True
    assert m.apply(FutDepth(0, 13, (), ()), now_ms=2) is False
    assert not m.book.synced
