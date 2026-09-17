import random

from fut.ledger import PaperLedger
from fut.settings import FutSettings
from fut.shadow import ShadowBooks
from fut.store import FutStore
from fut.types import JevVerdict
from tests.fut.helpers import SPEC, make_snap


def verdict(direction="up", exit_now=None):
    return JevVerdict(direction, 0.9, 0.9, 0.5, "trend", exit_now, 100, 1000, "jev-1")


def books(tmp_path, seed=7, process_start_ms=None):
    store = FutStore(tmp_path / "fut.db")
    return store, ShadowBooks(store, FutSettings(shadow_seed=seed), SPEC, rng=random.Random(seed),
                                process_start_ms=process_start_ms)


def test_jev_only_opens_on_entry_signal_and_closes_on_exit_signal(tmp_path):
    store, shadow = books(tmp_path)
    out = shadow.on_jev(verdict("down"), make_snap(), now_ms=1000, wake="entry_signal", entry_rate=0.0)
    assert out["jev_only"] == "opened"
    assert shadow.ledgers["jev_only"].position.side == "short"
    out = shadow.on_jev(verdict(), make_snap(), now_ms=2000, wake="exit_signal", entry_rate=0.0)
    assert out["jev_only"] == "closed"
    assert [f["kind"] for f in store.fut_fills("shadow:jev_only")] == ["open", "close"]


def test_jev_only_uses_its_own_reversal_when_main_is_flat(tmp_path):
    _, shadow = books(tmp_path)
    shadow.on_jev(verdict("up"), make_snap(), now_ms=1000, wake=None, entry_rate=0.0)

    out = shadow.on_jev(verdict("down"), make_snap(), now_ms=2000, wake=None, entry_rate=0.0)

    assert out["jev_only"] == "closed"


def test_jev_only_ignores_main_reversal_wake_when_its_direction_still_holds(tmp_path):
    _, shadow = books(tmp_path)
    shadow.on_jev(verdict("up"), make_snap(), now_ms=1000, wake=None, entry_rate=0.0)

    out = shadow.on_jev(verdict("up", exit_now=0.1), make_snap(), now_ms=2000,
                        wake="reversal_signal", entry_rate=0.0)

    assert "jev_only" not in out
    assert shadow.ledgers["jev_only"].position.is_open()


def test_jev_only_min_hold_uses_its_own_open_timestamp(tmp_path):
    store = FutStore(tmp_path / "fut.db")
    shadow = ShadowBooks(store, FutSettings(min_hold_s=60.0), SPEC, rng=random.Random(7))
    shadow.on_jev(verdict("up"), make_snap(), now_ms=1000, wake=None, entry_rate=0.0)

    assert shadow.on_jev(verdict("down"), make_snap(), now_ms=30_000, wake=None, entry_rate=0.0) == {}
    assert shadow.on_jev(verdict("down"), make_snap(), now_ms=61_000, wake=None, entry_rate=0.0)["jev_only"] == "closed"


def test_jev_only_can_enter_while_main_is_open(tmp_path):
    _, shadow = books(tmp_path)

    out = shadow.on_jev(verdict("up"), make_snap(), now_ms=1000, wake=None, entry_rate=0.0)

    assert out["jev_only"] == "opened"


def test_jev_only_reports_collar_refusals(tmp_path):
    _, shadow = books(tmp_path)
    out = shadow.on_jev(verdict(), make_snap(stale=True), now_ms=1000, wake="entry_signal", entry_rate=0.0)
    assert out == {}


def test_random_book_follows_entry_rate(tmp_path):
    _, never = books(tmp_path / "a")
    for t in range(20):
        never.on_jev(verdict(), make_snap(), now_ms=t, wake=None, entry_rate=0.0)
    assert not never.ledgers["random"].position.is_open()
    _, always = books(tmp_path / "b")
    assert always.on_jev(verdict(), make_snap(), now_ms=0, wake=None, entry_rate=1.0)["random"] == "opened"


def test_shadow_books_never_touch_main(tmp_path):
    store, shadow = books(tmp_path)
    shadow.on_jev(verdict(), make_snap(), now_ms=1000, wake="entry_signal", entry_rate=1.0)
    main = PaperLedger(store, FutSettings(), SPEC)
    assert not main.position.is_open() and main.balance == 450.0
    assert store.fut_fills("main") == []


def test_mark_runs_stops_on_shadow_books(tmp_path):
    _, shadow = books(tmp_path)
    shadow.on_jev(verdict(), make_snap(), now_ms=1000, wake="entry_signal", entry_rate=0.0)
    out = shadow.mark(make_snap(bid=70000.0, ask=70000.1, last=70000.0, fair=70000.0), now_ms=2000)
    assert out == {"jev_only": "stop"}


def test_shadow_entry_rate_uses_its_own_recent_open_count(tmp_path):
    store, shadow = books(tmp_path)
    store.add_fut_fill("shadow:jev_only", ts_ms=500, kind="open", side="long", contracts=1, price=1.0,
                       fee=0.0, funding=0.0, pnl=0.0, reason="entry")
    shadow.settings = FutSettings(max_entries_per_hour=1)
    out = shadow.on_jev(verdict(), make_snap(), now_ms=1_000, wake="entry_signal", entry_rate=0.0)
    assert out["jev_only"] == "entry_rate"


def test_shadow_effective_seed_changes_with_process_start(tmp_path):
    _, first = books(tmp_path / "a", process_start_ms=1000)
    _, second = books(tmp_path / "b", process_start_ms=2000)

    assert first.effective_seed == (7 ^ 1000)
    assert second.effective_seed == (7 ^ 2000)
