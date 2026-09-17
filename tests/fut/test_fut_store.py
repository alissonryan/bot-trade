from fut.store import FutStore


def add_fill(store, book, ts_ms, kind):
    store.add_fut_fill(book, ts_ms=ts_ms, kind=kind, side="long", contracts=1, price=1.0,
                       fee=0.0, funding=0.0, pnl=0.0, reason=kind)


def test_count_opens_is_inclusive_and_scoped_to_book(tmp_path):
    store = FutStore(tmp_path / "fut.db")
    add_fill(store, "main", 1_000, "open")
    add_fill(store, "main", 2_000, "close")
    add_fill(store, "main", 3_000, "open")
    add_fill(store, "shadow:random", 3_000, "open")

    assert store.count_opens("main", 3_000) == 1
    assert store.count_opens("main", 3_001) == 0
    assert store.count_opens("shadow:random", 0) == 1
