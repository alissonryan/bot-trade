from datetime import datetime, timezone
import threading
import time

import pytest

from fut.panel.cache import PanelCache


def fact(id, ts_ms, *, kind="jev", cost_usd=0.0, bid=100.0, ask=102.0, last=101.0, error=None):
    return {"id": id, "ts_ms": ts_ms, "kind": kind, "cost_usd": cost_usd,
            "bid": bid, "ask": ask, "last": last, "error": error}


class Facts(list):
    def __init__(self, rows, max_id):
        super().__init__(rows)
        self.max_id = max_id


class CountingReader:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def decision_facts(self, after_id, limit=2000):
        self.calls.append((after_id, limit))
        rows = [row for row in self.rows if row["id"] > after_id][:limit]
        return Facts(rows, max((row["id"] for row in self.rows), default=0))


DAY = 86_400_000


def test_refresh_folds_rows_and_never_rereads_old_ids():
    reader = CountingReader([fact(1, DAY, cost_usd=0.1), fact(2, DAY + 1, kind="llm", cost_usd=0.2),
                             fact(3, DAY + 2, cost_usd=0.3)])
    cache = PanelCache(reader, limit=2)
    cache.refresh(now_ms=DAY + 10)
    cache.refresh(now_ms=DAY + 10)
    assert reader.calls == [(0, 2), (2, 2)]
    assert cache.last_id == 3 and cache.lifetime_costs == {"jev": 0.4, "llm": 0.2}


def test_cold_start_reports_loading_until_a_short_chunk_arrives():
    reader = CountingReader([fact(1, 1), fact(2, 2), fact(3, 3), fact(4, 4), fact(5, 5)])
    cache = PanelCache(reader, limit=2)
    cache.refresh(now_ms=5)
    assert cache.loading is True
    cache.refresh(now_ms=5)
    assert cache.loading is True
    cache.refresh(now_ms=5)
    assert cache.loading is False


def test_costs_are_kept_by_utc_day():
    day1 = 1_800_000_000_000
    day2 = day1 + DAY
    reader = CountingReader([fact(1, day1, cost_usd=0.1), fact(2, day2, kind="llm", cost_usd=0.2)])
    cache = PanelCache(reader)
    cache.refresh(now_ms=day2)
    day1_name = datetime.fromtimestamp(day1 / 1000, tz=timezone.utc).date().isoformat()
    day2_name = datetime.fromtimestamp(day2 / 1000, tz=timezone.utc).date().isoformat()
    assert cache.costs_for_day(day1_name) == {"jev": 0.1, "llm": 0.0}
    assert cache.costs_for_day(day2_name) == {"jev": 0.0, "llm": 0.2}


def test_database_replacement_resets_the_incremental_cursor():
    reader = CountingReader([fact(9, 9)])
    cache = PanelCache(reader)
    cache.refresh(now_ms=9)
    reader.rows = [fact(1, 10, bid=200.0, ask=202.0)]
    cache.refresh(now_ms=10)
    assert cache.last_id == 0 and cache.loading is True
    cache.refresh(now_ms=10)
    assert cache.last_id == 1 and cache.snapshot["bid"] == 200.0


def test_concurrent_refreshes_are_serialized_without_double_counting():
    class SlowReader(CountingReader):
        def decision_facts(self, after_id, limit=2000):
            time.sleep(0.02)
            return super().decision_facts(after_id, limit)

    reader = SlowReader([fact(1, DAY, cost_usd=0.3)])
    cache = PanelCache(reader)
    threads = [threading.Thread(target=cache.refresh, kwargs={"now_ms": DAY + 1}) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert cache.lifetime_costs == {"jev": 0.3, "llm": 0.0}
    assert cache.price_series(DAY) == [[DAY, 101.0]]


def test_jev_failure_streak_resets_on_success_across_chunks():
    reader = CountingReader([
        fact(1, DAY, error="503 unavailable"),
        fact(2, DAY + 1, error="503 unavailable"),
        fact(3, DAY + 2, error=None),
        fact(4, DAY + 3, error="429 rate limit"),
    ])
    cache = PanelCache(reader, limit=2)
    cache.refresh(now_ms=DAY + 3)
    assert cache.jev_failures == 2 and cache.jev_failure_since_ms == DAY
    cache.refresh(now_ms=DAY + 3)
    assert cache.jev_failures == 1 and cache.jev_failure_since_ms == DAY + 3
    assert cache.jev_failure_reason == "Jev recusou por limite de uso"


def _gap(cache):
    return cache.view(day="1970-01-02", since_ms=0)["jev_gap_ms"]


def test_jev_gaps_come_only_from_consecutive_jev_rows():
    reader = CountingReader([
        fact(1, DAY),
        fact(2, DAY + 1_000, kind="jev_ab"),
        fact(3, DAY + 2_000, kind="llm"),
        fact(4, DAY + 10_000),
        fact(5, DAY + 10_500, kind="jev_ab"),
        fact(6, DAY + 20_000),
        fact(7, DAY + 20_100, kind="llm"),
        fact(8, DAY + 30_000),
        fact(9, DAY + 40_000),
        fact(10, DAY + 50_000),
        fact(11, DAY + 50_000),  # non-positive: ignored
        fact(12, DAY + 49_000),  # negative: ignored
    ])
    cache = PanelCache(reader)
    cache.refresh(now_ms=DAY + 50_000)
    assert _gap(cache) == 10_000


def test_jev_gap_is_the_median_not_the_mean():
    rows = [fact(i + 1, DAY + i * 10_000) for i in range(20)]
    rows.append(fact(21, DAY + 19 * 10_000 + 3_600_000))
    cache = PanelCache(CountingReader(rows))
    cache.refresh(now_ms=DAY + 4_000_000)
    assert _gap(cache) == 10_000


def test_jev_gap_is_none_until_five_gaps():
    rows = [fact(i + 1, DAY + i * 10_000) for i in range(5)]
    cache = PanelCache(CountingReader(rows))
    cache.refresh(now_ms=DAY + 50_000)
    assert _gap(cache) is None
    cache.reader.rows.append(fact(6, DAY + 50_000))
    cache.refresh(now_ms=DAY + 50_000)
    assert _gap(cache) == 10_000


def test_jev_gaps_survive_chunked_refresh_and_clear_on_database_replace():
    rows = [fact(i + 1, DAY + i * 10_000) for i in range(8)]
    reader = CountingReader(rows)
    cache = PanelCache(reader, limit=3)
    cache.refresh(now_ms=DAY + 80_000)
    cache.refresh(now_ms=DAY + 80_000)
    cache.refresh(now_ms=DAY + 80_000)
    assert _gap(cache) == 10_000
    reader.rows = [fact(1, DAY + 100_000)]
    cache.refresh(now_ms=DAY + 100_000)
    assert cache.last_id == 0 and _gap(cache) is None
    cache.refresh(now_ms=DAY + 100_000)
    assert _gap(cache) is None


def test_jev_ab_costs_are_jev_costs_but_never_market_or_health_facts():
    reader = CountingReader([
        fact(1, DAY, cost_usd=0.1, bid=100.0, ask=102.0, error="503 unavailable"),
        fact(2, 2 * DAY, kind="jev_ab", cost_usd=0.25, bid=900.0, ask=902.0, error=None),
        fact(3, 2 * DAY + 1, kind="jev_ab", cost_usd=0.5, bid=901.0, ask=903.0, error="529 overloaded"),
    ])
    cache = PanelCache(reader)
    cache.refresh(now_ms=DAY + 3)
    assert cache.last_id == 3 and cache.last_ts_ms == 2 * DAY + 1
    assert cache.lifetime_costs == {"jev": 0.85, "llm": 0.0}
    assert cache.costs_for_day("1970-01-02") == {"jev": 0.1, "llm": 0.0}
    assert cache.costs_for_day("1970-01-03") == {"jev": 0.75, "llm": 0.0}
    assert cache.jev_failures == 1 and cache.jev_failure_since_ms == DAY
    assert cache.snapshot == {"ts_ms": DAY, "last": 101.0, "bid": 100.0, "ask": 102.0,
                              "spread_bps": pytest.approx(198.019801980198), "stale": False}
    assert cache.price_series(DAY) == [[DAY, 101.0]]
