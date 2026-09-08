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
    meter.record(ENTRY, now - HOUR - 1)        # outside the 1h window, inside the 24h window
    meter.record(ENTRY, now - 1)               # inside both
    meter.record(PROTECTIVE, now - 1)
    counts = meter.counts(now)
    assert counts.writes_1h == 2               # the two at now-1
    assert counts.entries_24h == 2             # both ENTRY rows are inside 24h


def test_a_future_stamped_row_does_not_wedge_counts_or_check_storm(tmp_path):
    """A forward clock jump (VM resume, a container with no RTC, an NTP step)
    can stamp a row far in the future. Once the clock reads correctly again,
    that row must not be counted as "in the last hour" for as long as it takes
    real time to catch up to it -- or a single boot's writes look like a storm
    on every subsequent boot, and the halt never self-clears (F1)."""
    meter = _meter(tmp_path, max_writes_per_hour=0, kill_writes_per_hour=1)
    real_now = 10 * DAY
    skewed_future = real_now + 30 * DAY
    meter.record(PROTECTIVE, skewed_future)     # written while the clock was 30 days ahead
    counts = meter.counts(real_now)
    assert counts.writes_1h == 0
    assert counts.entries_24h == 0
    meter.check_storm(real_now)                 # must not raise WriteStormHalt


def test_a_backward_clock_jump_still_counts_writes_made_moments_before_it(tmp_path):
    """The other clock direction (F6): a backward jump (an NTP correction, a
    VM pause) must not let a genuinely recent write silently fall out of the
    counting window -- that undercounting is exactly what would let a BUY
    through that should have been refused. Before F1's upper bound existed,
    the window had no ceiling at all, so a backward jump could only ever
    widen it (the count could rise, never drop -- the property the collar
    test `test_a_spent_budget_refuses_buy_and_still_allows_sell` exercises
    from the collar side). The upper bound added for F1 must stay tolerant
    enough that an ordinary backward step never reproduces the opposite bug:
    a row written moments ago must still count even though "moments ago" now
    reads as later than "now"."""
    meter = _meter(tmp_path)
    real_now = 10 * DAY
    meter.record(PROTECTIVE, real_now)          # written right before the clock steps back
    jumped_back_now = real_now - 5 * 60_000     # a 5-minute backward correction
    counts = meter.counts(jumped_back_now)
    assert counts.writes_1h == 1                # still counted, not silently dropped
    meter.check_storm(jumped_back_now)           # (kill_writes_per_hour default: no trip at 1)


def test_backward_jump_just_inside_tolerance_still_counts_the_write(tmp_path):
    """N2 boundary probe, inside edge: MAX_CLOCK_SKEW_MS is 1h, so a write
    stamped moments ago is still within ``until_ms = now_ms + MAX_CLOCK_SKEW_MS``
    for any backward step up to 1h. This pins the edge that
    ``test_a_backward_clock_jump_still_counts_writes_made_moments_before_it``
    (a 5-minute step) only demonstrates deep inside."""
    meter = _meter(tmp_path, max_writes_per_hour=0, kill_writes_per_hour=1)
    real_now = 10 * DAY
    meter.record(PROTECTIVE, real_now)
    jumped_back_now = real_now - (HOUR - 60_000)   # 59 minutes back: inside the 1h tolerance
    counts = meter.counts(jumped_back_now)
    assert counts.writes_1h == 1                    # still counted
    with pytest.raises(WriteStormHalt):
        meter.check_storm(jumped_back_now)           # the write is still visible to the hard ceiling too


def test_backward_jump_just_outside_tolerance_silently_undercounts(tmp_path):
    """N2 boundary probe, outside edge -- documenting the real, accepted
    residual, not an ideal the code does not implement. Once a backward step
    exceeds MAX_CLOCK_SKEW_MS (1h), the write's timestamp reads as further
    ahead of the jumped-back `now` than the tolerance allows, and
    ``until_ms = now_ms + MAX_CLOCK_SKEW_MS`` excludes it -- the same bound
    that stops a forward-jumped row from being counted (F1) here drops a row
    that really was written moments ago. The row itself is not deleted (see
    ``test_record_does_not_prune_future_stamped_rows``); it is only invisible
    to counting until real time closes the gap. That undercount reaches the
    hard ceiling too: ``check_storm`` does not raise even though the write
    happened and the ceiling is 1."""
    meter = _meter(tmp_path, max_writes_per_hour=0, kill_writes_per_hour=1)
    real_now = 10 * DAY
    meter.record(PROTECTIVE, real_now)
    jumped_back_now = real_now - (HOUR + 60_000)   # 61 minutes back: outside the 1h tolerance
    counts = meter.counts(jumped_back_now)
    assert counts.writes_1h == 0                    # undercounted -- the write is real and recent
    meter.check_storm(jumped_back_now)               # does not raise: same undercount at the ceiling


def test_record_does_not_prune_future_stamped_rows(tmp_path):
    """N1: an earlier revision also had ``record()`` delete rows stamped
    implausibly far ahead of its own ``now_ms`` (a "future prune"), on top of
    the read-side upper bound. That delete is gone. On a host with no RTC --
    the very environment the skew tolerance's own justification cites -- the
    clock boots BEHIND real time, and the boot ``reconcile()`` in
    ``bot/cli.py`` can be exactly the ordinary write that runs this prune
    while ``now_ms`` still lags; the removed bound would then treat every
    genuinely recent row as "future" and delete it permanently, wiping the
    ledger on the very restart this table exists to survive. A far-future row
    is still never *counted* as recent (see
    ``test_a_future_stamped_row_does_not_wedge_counts_or_check_storm``); it is
    simply no longer deleted for being far-future -- it ages out through the
    ordinary ``before_ms`` retention prune once real time reaches it, like any
    other row."""
    meter = _meter(tmp_path)
    now = 10 * DAY
    meter.record(PROTECTIVE, now + 30 * DAY)    # an implausible, far-future stamp
    meter.record(PROTECTIVE, now)               # an ordinary write; its own prune runs
    remaining = {row for (row,) in meter.store._conn.execute("SELECT ts_ms FROM order_writes")}
    assert now + 30 * DAY in remaining          # not deleted -- only ever excluded from counts
    assert now in remaining


def test_record_prunes_only_beyond_48h(tmp_path):
    # Chronological order here matches how writes actually arrive, but it is
    # not load-bearing: record() only prunes rows strictly OLDER than
    # RETENTION_MS behind its own now_ms (N1 removed the future-side delete),
    # so an out-of-order now_ms cannot make an earlier call's row look old by
    # comparison the way it could before.
    meter = _meter(tmp_path)
    now = 10 * DAY
    meter.record(ENTRY, now - 49 * HOUR)
    meter.record(ENTRY, now - 47 * HOUR)
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
    meter = _meter(tmp_path, max_writes_per_hour=0, kill_writes_per_hour=1)
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
