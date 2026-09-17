from datetime import datetime, timezone
import threading
import time

from fut.panel.cache import PanelCache


def fact(id, ts_ms, *, kind="jev", cost_usd=0.0, bid=100.0, ask=102.0, last=101.0):
    return {"id": id, "ts_ms": ts_ms, "kind": kind, "cost_usd": cost_usd,
            "bid": bid, "ask": ask, "last": last}


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
