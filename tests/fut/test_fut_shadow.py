import random

from fut.ledger import PaperLedger
from fut.settings import FutSettings
from fut.shadow import ShadowBooks
from fut.store import FutStore
from fut.types import JevVerdict
from tests.fut.helpers import SPEC, make_snap


def verdict(direction="up"):
    return JevVerdict(direction, 0.9, 0.9, 0.5, "trend", None, 100, 1000, "jev-1")


def books(tmp_path, seed=7):
    store = FutStore(tmp_path / "fut.db")
    return store, ShadowBooks(store, FutSettings(), SPEC, rng=random.Random(seed))


def test_jev_only_opens_on_entry_signal_and_closes_on_exit_signal(tmp_path):
    store, shadow = books(tmp_path)
    out = shadow.on_jev(verdict("down"), make_snap(), now_ms=1000, wake="entry_signal", entry_rate=0.0)
    assert out["jev_only"] == "opened"
    assert shadow.ledgers["jev_only"].position.side == "short"
    out = shadow.on_jev(verdict(), make_snap(), now_ms=2000, wake="exit_signal", entry_rate=0.0)
    assert out["jev_only"] == "closed"
    assert [f["kind"] for f in store.fut_fills("shadow:jev_only")] == ["open", "close"]


def test_jev_only_reports_collar_refusals(tmp_path):
    _, shadow = books(tmp_path)
    out = shadow.on_jev(verdict(), make_snap(stale=True), now_ms=1000, wake="entry_signal", entry_rate=0.0)
    assert out["jev_only"] == "stale"


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
