"""Submission-rate collar: how OFTEN this bot may write to the venue.

`bot/collar.py` gates position risk. Nothing gated submission rate, and the
exposure here is specific: the venue session is a browser token, POST/DELETE are
never retried (so a duplicate order can only come from our own control flow), and
`bot/cli.py` restarts with backoff -- a crash after a successful write, repeated,
is a write loop no in-process counter would ever see. Hence SQLite, not a field.

The meter OBSERVES every write and REFUSES only where refusing is safe: the
collar's BUY branch, and the barrier before a cycle begins. It must never refuse
a stop, a cancel, a flatten or a SELL -- a safety feature that leaves a position
unprotected has broken live invariant 3, which is worse than the storm it stopped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

ENTRY = "ENTRY"
PROTECTIVE = "PROTECTIVE"

METERED_WRITE_METHODS = frozenset(
    # Every KcexClient method that issues a POST or DELETE, not only the ones
    # bot/hands.py calls today. place_limit is unused by the bot right now; it is
    # metered anyway so that using it later cannot silently bypass the counter.
    {"place_market", "place_limit", "place_trigger", "cancel_order"}
)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
RETENTION_MS = 48 * HOUR_MS
# A row stamped further ahead of "now" than this cannot be a real write -- ordinary
# clock skew (NTP correction, a VM resume gap) is seconds to minutes, not hours. This
# must stay well below RETENTION_MS/HOUR_MS so a small forward skew is never treated
# as bogus and silently dropped.
MAX_CLOCK_SKEW_MS = HOUR_MS


class WriteStormHalt(RuntimeError):
    """Writes are being issued in a pattern nobody designed. Exit code 8."""


@dataclass(frozen=True)
class WriteCounts:
    """The only thing the pure collar sees. No I/O behind it."""

    writes_1h: int
    entries_24h: int


def rate_limited(counts: WriteCounts, settings) -> bool:
    """Soft trip: refuse an ENTRY. Each knob is disabled independently by 0."""
    if settings.max_writes_per_hour and counts.writes_1h >= settings.max_writes_per_hour:
        return True
    if settings.max_entries_per_day and counts.entries_24h >= settings.max_entries_per_day:
        return True
    return False


class WriteMeter:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings

    def record(self, kind: str, now_ms: int) -> None:
        """Called BEFORE the POST. A write that times out may still have executed."""
        self.store.record_write(kind, now_ms)
        self.store.prune_writes(now_ms - RETENTION_MS, after_ms=now_ms + MAX_CLOCK_SKEW_MS)

    def counts(self, now_ms: int) -> WriteCounts:
        # An upper bound on the window, but a TOLERANT one (now_ms + skew, not a
        # bare now_ms): a strict "now" cutoff would fix the forward-jump wedge
        # (F1) at the cost of breaking the opposite, pre-existing safety property
        # -- a BACKWARD clock jump must not let genuinely recent writes silently
        # fall out of the count, which is what refuses a BUY on a shrunk window
        # in the first place. A row within the tolerance of "now" is always kept;
        # only a row implausibly far ahead of it (cannot be real -- see
        # MAX_CLOCK_SKEW_MS) is excluded. That is the only direction in which
        # this bound is allowed to make the limiter more permissive.
        until_ms = now_ms + MAX_CLOCK_SKEW_MS
        return WriteCounts(
            writes_1h=self.store.count_writes(since_ms=now_ms - HOUR_MS, until_ms=until_ms),
            entries_24h=self.store.count_writes(since_ms=now_ms - DAY_MS, kind=ENTRY, until_ms=until_ms),
        )

    def check_storm(self, now_ms: int, *, stop_observation: str | None = None) -> None:
        """Hard trip. Called at the cycle barrier, never in the middle of a write."""
        ceiling = self.settings.kill_writes_per_hour
        if not ceiling:
            return
        writes = self.store.count_writes(since_ms=now_ms - HOUR_MS, until_ms=now_ms + MAX_CLOCK_SKEW_MS)
        if writes < ceiling:
            return
        raise WriteStormHalt(
            f"{writes} venue writes in the last hour (ceiling {ceiling}); "
            f"resident stop last observed: {stop_observation or 'unknown'}"
        )

    def wrap(self, client) -> "_MeteredClient":
        return _MeteredClient(client, self)


class _MeteredClient:
    """Thin proxy: record, then delegate. Reads pass straight through.

    It cannot refuse a call. A transport that can refuse is a transport that can
    refuse the place_trigger protecting a fresh position -- see the module docstring.
    """

    def __init__(self, client, meter: "WriteMeter"):
        self._client = client
        self._meter = meter

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if name not in METERED_WRITE_METHODS:
            return attr

        def metered(*args, **kwargs):
            kind = ENTRY if _is_entry(name, args, kwargs) else PROTECTIVE
            self._meter.record(kind, int(time.time() * 1000))
            return attr(*args, **kwargs)

        return metered


def _is_entry(name: str, args, kwargs) -> bool:
    """Only a BUY market order is an entry.

    KcexClient.place_market/place_limit/place_trigger take ``side`` as a
    keyword-only str ("BUY"/"SELL", upper-cased by the client) -- confirmed by
    reading kcex/client.py, not the numeric "1"/"2" encoding used elsewhere in
    this codebase for order-type constants. All three placement methods are
    keyword-only, so ``side`` never arrives positionally; the ``args`` fallback
    below is defensive only.
    """
    if name != "place_market":
        return False
    side = kwargs.get("side", args[0] if args else None)
    return str(side).upper() == "BUY"
