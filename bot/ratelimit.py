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

from dataclasses import dataclass

ENTRY = "ENTRY"
PROTECTIVE = "PROTECTIVE"

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
RETENTION_MS = 48 * HOUR_MS


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
        self.store.prune_writes(now_ms - RETENTION_MS)

    def counts(self, now_ms: int) -> WriteCounts:
        return WriteCounts(
            writes_1h=self.store.count_writes(since_ms=now_ms - HOUR_MS),
            entries_24h=self.store.count_writes(since_ms=now_ms - DAY_MS, kind=ENTRY),
        )

    def check_storm(self, now_ms: int, *, stop_observation: str | None = None) -> None:
        """Hard trip. Called at the cycle barrier, never in the middle of a write."""
        ceiling = self.settings.kill_writes_per_hour
        if not ceiling:
            return
        writes = self.store.count_writes(since_ms=now_ms - HOUR_MS)
        if writes < ceiling:
            return
        raise WriteStormHalt(
            f"{writes} venue writes in the last hour (ceiling {ceiling}); "
            f"resident stop last observed: {stop_observation or 'unknown'}"
        )
