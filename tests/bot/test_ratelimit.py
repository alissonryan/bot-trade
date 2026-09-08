import pytest

from bot.ratelimit import ENTRY, PROTECTIVE, WriteCounts, WriteMeter, WriteStormHalt, rate_limited
from bot.settings import Settings
from bot.store import Store

HOUR = 3_600_000
DAY = 86_400_000


def _settings(**kwargs) -> Settings:
    data = Settings.from_env().__dict__.copy()
    data.update(kwargs)
    return Settings(**data)


def _meter(tmp_path, **kwargs) -> WriteMeter:
    return WriteMeter(Store(tmp_path / "bot.db", mode="paper"), _settings(**kwargs))


def test_counts_are_rolling_windows(tmp_path):
    meter = _meter(tmp_path)
    now = 10 * DAY
    meter.record(ENTRY, now - HOUR - 1)        # outside both? no: inside 24h
    meter.record(ENTRY, now - 1)               # inside both
    meter.record(PROTECTIVE, now - 1)
    counts = meter.counts(now)
    assert counts.writes_1h == 2               # the two at now-1
    assert counts.entries_24h == 2             # both ENTRY rows are inside 24h


def test_record_prunes_only_beyond_48h(tmp_path):
    meter = _meter(tmp_path)
    now = 10 * DAY
    meter.record(ENTRY, now - 47 * HOUR)
    meter.record(ENTRY, now - 49 * HOUR)
    meter.record(ENTRY, now)
    assert meter.store.count_writes(since_ms=0) == 2   # the 49h row is gone


def test_rate_limited_on_each_knob():
    on = _settings(max_writes_per_hour=30, max_entries_per_day=20)
    assert rate_limited(WriteCounts(writes_1h=30, entries_24h=0), on) is True
    assert rate_limited(WriteCounts(writes_1h=29, entries_24h=20), on) is True
    assert rate_limited(WriteCounts(writes_1h=29, entries_24h=19), on) is False


def test_zero_disables_each_knob_independently():
    only_entries = _settings(max_writes_per_hour=0, max_entries_per_day=20)
    assert rate_limited(WriteCounts(writes_1h=10_000, entries_24h=0), only_entries) is False
    assert rate_limited(WriteCounts(writes_1h=10_000, entries_24h=20), only_entries) is True
    off = _settings(max_writes_per_hour=0, max_entries_per_day=0)
    assert rate_limited(WriteCounts(writes_1h=10_000, entries_24h=10_000), off) is False


def test_check_storm_raises_at_the_ceiling(tmp_path):
    meter = _meter(tmp_path, max_writes_per_hour=1, kill_writes_per_hour=2)
    now = 10 * DAY
    meter.record(PROTECTIVE, now)
    meter.check_storm(now)                       # one write: below the ceiling
    meter.record(PROTECTIVE, now)
    with pytest.raises(WriteStormHalt) as exc:
        meter.check_storm(now, stop_observation="stop_present")
    assert "stop_present" in str(exc.value)


def test_check_storm_reports_unknown_when_nothing_was_observed(tmp_path):
    meter = _meter(tmp_path, max_writes_per_hour=1, kill_writes_per_hour=1)
    now = 10 * DAY
    meter.record(PROTECTIVE, now)
    with pytest.raises(WriteStormHalt) as exc:
        meter.check_storm(now)
    assert "unknown" in str(exc.value)


def test_kill_switch_zero_never_raises(tmp_path):
    meter = _meter(tmp_path, max_writes_per_hour=0, kill_writes_per_hour=0)
    now = 10 * DAY
    for _ in range(100):
        meter.record(ENTRY, now)
    meter.check_storm(now)


class _FakeClient:
    def __init__(self):
        self.calls = []

    def place_market(self, **kw):
        self.calls.append("place_market")
        return {"data": {"orderId": "1"}}

    def place_limit(self, **kw):
        self.calls.append("place_limit")
        return {"data": {"orderId": "3"}}

    def place_trigger(self, **kw):
        self.calls.append("place_trigger")
        return {"data": {"orderId": "2"}}

    def cancel_order(self, order_id):
        self.calls.append("cancel_order")
        return {"code": 0}

    def balances(self, **kw):
        self.calls.append("balances")
        return {}


def test_wrap_records_entry_and_protective(tmp_path):
    meter = _meter(tmp_path)
    client = meter.wrap(_FakeClient())
    client.place_market(side="BUY", quantity="0.001")
    client.place_trigger(side="SELL", price="1")
    client.cancel_order("abc")
    counts = meter.counts(now_ms=int(__import__("time").time() * 1000))
    assert counts.writes_1h == 3
    assert counts.entries_24h == 1          # only the BUY market order is an ENTRY


def test_a_sell_market_order_is_protective(tmp_path):
    meter = _meter(tmp_path)
    client = meter.wrap(_FakeClient())
    client.place_market(side="SELL", quantity="0.001")
    assert meter.counts(now_ms=int(__import__("time").time() * 1000)).entries_24h == 0


def test_reads_are_passed_through_uncounted(tmp_path):
    meter = _meter(tmp_path)
    client = meter.wrap(_FakeClient())
    client.balances(currencies="BTC,USDT")
    assert meter.store.count_writes(since_ms=0) == 0


def test_the_row_is_written_even_when_the_post_raises(tmp_path):
    class Boom(_FakeClient):
        def place_market(self, **kw):
            raise RuntimeError("network")

    meter = _meter(tmp_path)
    client = meter.wrap(Boom())
    with pytest.raises(RuntimeError):
        client.place_market(side="BUY", quantity="0.001")
    assert meter.store.count_writes(since_ms=0) == 1


def test_every_write_method_on_the_real_client_is_metered():
    """Guard: a new POST/DELETE on KcexClient must not slip through uncounted."""
    import inspect

    from bot.ratelimit import METERED_WRITE_METHODS
    from kcex.client import KcexClient

    writes = set()
    for name, fn in inspect.getmembers(KcexClient, inspect.isfunction):
        if name.startswith("_") or name in {"request", "get", "post", "delete"}:
            continue
        source = inspect.getsource(fn)
        # cancel_order calls self.request("DELETE", ...) directly, not self.delete().
        if ("self.post(" in source or "self.delete(" in source
                or 'self.request("POST"' in source or 'self.request("DELETE"' in source):
            writes.add(name)
    assert writes == set(METERED_WRITE_METHODS), (
        f"unmetered venue writes: {sorted(writes - set(METERED_WRITE_METHODS))}; "
        f"stale entries: {sorted(set(METERED_WRITE_METHODS) - writes)}"
    )
