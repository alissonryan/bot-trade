"""Synthetic pagination contract, NOT captured KCEX response fixtures."""
import pytest

from bot.hands import open_order_ids
from kcex.orders import IncompleteOrderList


class Pages:
    def __init__(self, payloads):
        self.payloads = payloads
        self.pages = []
        self.sizes = []

    def open_orders(self, *, page_num=1, page_size=100):
        self.pages.append(page_num)
        self.sizes.append(page_size)
        value = self.payloads[page_num - 1]
        if isinstance(value, Exception):
            raise value
        return value


def page(ids, number, total, size):
    return {"data": {"records": [{"id": i} for i in ids], "pageNum": number,
                     "total": total, "pageSize": size}}


def test_all_orders_seen_when_server_caps_page_size():
    client = Pages([page(["first"], 1, 2, 1), page(["resident-stop"], 2, 2, 1)])
    assert open_order_ids(client) == {"first", "resident-stop"}
    assert client.pages == [1, 2]
    assert client.sizes == [100, 1]


def test_short_unannotated_page_needs_explicit_empty_page():
    client = Pages([{"data": [{"id": "a"}]}, {"data": [{"id": "b"}]}, {"data": []}])
    assert open_order_ids(client) == {"a", "b"}
    assert client.pages == [1, 2, 3]


def test_page_limit_never_returns_partial_ids():
    client = Pages([{"data": [{"id": "a"}]}, {"data": [{"id": "b"}]}])
    with pytest.raises(IncompleteOrderList, match="limit"):
        open_order_ids(client, max_pages=2)


@pytest.mark.parametrize("key", ["records", "resultList", "list", "orders", "rows", "data"])
def test_supported_envelopes_honor_top_level_total(key):
    client = Pages([{"data": {key: [{"id": "a"}]}, "total": "1", "pageSize": "20", "pageNum": "1"}])
    assert open_order_ids(client) == {"a"}
    assert client.pages == [1]


@pytest.mark.parametrize("payload", [
    {}, {"data": None}, {"data": {}}, {"data": [{}]}, {"data": [{"id": None}]},
    {"data": [{"id": True}]}, {"data": [{"id": " "}]}, {"data": [{"id": {"x": 1}}]},
    {"data": {"records": [], "total": -1}}, {"data": {"records": [], "total": 1.5}},
    {"data": {"records": [], "total": True}}, {"data": {"records": [], "total": "nan"}},
    {"data": {"records": [], "pageSize": 0}}, {"data": {"records": [], "pageNum": 2}},
    {"data": {"records": [], "total": 1}, "total": 0},
    {"data": {"records": [], "list": []}},
    {"code": 90001, "data": []}, {"success": False, "data": []},
])
def test_unknown_or_invalid_response_is_never_an_empty_confirmation(payload):
    with pytest.raises(IncompleteOrderList):
        open_order_ids(Pages([payload]))


@pytest.mark.parametrize("payloads", [
    [page(["a"], 1, 2, 1), page(["a"], 2, 2, 1)],
    [page(["a", "a"], 1, 2, 2)],
    [page(["a"], 1, 2, 1), page([], 2, 2, 1)],
    [page(["a", "b"], 1, 1, 2)],
    [page(["a", "b"], 1, 2, 1)],
    [page(["a"], 1, 2, 1), page(["b"], 2, 3, 1)],
    [page(["a"], 1, 2, 1), page(["b"], 2, 2, 2)],
    [page(["a"], 1, 2, 1), {"data": [{"id": "b"}]}],
])
def test_repetition_or_inconsistent_metadata_is_an_error(payloads):
    with pytest.raises(IncompleteOrderList):
        open_order_ids(Pages(payloads))


def live(tmp_path, payloads):
    from bot.store import Store
    from test_hands_live import FakeClient, _hands, _open_position, FOREIGN_BTC
    store = Store(tmp_path / "p8.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + .00025])
    pages = Pages(payloads)
    def orders(**kwargs):
        if kwargs["page_num"] == 1:
            pages.pages.clear()
        return pages.open_orders(**kwargs)
    client.open_orders = orders
    return store, client, _hands(store, client)


def test_sell_never_confirms_absence_of_stop_on_second_page(tmp_path):
    """M1 note: `execute(SELL)` itself now refuses before any write
    (TerminalEvidenceUnavailable; see tests/bot/test_exit_latch.py). `_sell()`'s
    own pagination-aware cancel-confirm logic remains correct and is
    exercised directly (white-box) here."""
    from test_hands_live import _snap
    store, client, hands = live(tmp_path, [page(["owner"], 1, 2, 1), page(["oid-t"], 2, 2, 1)])
    hands._sell(_snap(), exit_reason=None)
    assert [c for c in client.calls if c[0] in ("market", "trigger")] == []
    assert [c for c in client.calls if c[0] == "cancel"] == [("cancel", "oid-t")]
    assert store.load_position()["stop_order_id"] == "oid-t"
    assert store.fills() == []


def test_reconcile_sees_second_page_stop_without_creating_another(tmp_path):
    store, client, hands = live(tmp_path, [page(["owner"], 1, 2, 1), page(["oid-t"], 2, 2, 1)])
    assert hands.reconcile() == "ok"
    assert [c for c in client.calls if c[0] in ("market", "trigger", "cancel")] == []


def test_incomplete_entry_list_keeps_pending_without_cancel_or_stop(tmp_path):
    from bot.store import Store
    from test_hands_live import FakeClient, _hands, _buy_gate, _snap, FOREIGN_BTC
    store = Store(tmp_path / "entry.db", mode="live")
    client = FakeClient(btc=[FOREIGN_BTC])  # no confirmed entry fill
    client.open_orders = Pages([page(["owner"], 1, 2, 1), RuntimeError("page outage")]).open_orders
    hands = _hands(store, client)
    hands.execute(_buy_gate(), _snap())
    assert store.load_position()["state"] == "PENDING"
    assert [(c[0], c[1].get("side")) for c in client.calls if c[0] in ("market", "trigger")] == [("market", "BUY")]
    assert not [c for c in client.calls if c[0] == "cancel"]
    assert store.fills() == []


@pytest.mark.parametrize("mode", ["sell", "reconcile"])
@pytest.mark.parametrize("fault", ["limit", "network", "malformed"])
def test_incomplete_list_blocks_sell_and_reconcile_writes(tmp_path, mode, fault):
    """mode="sell" calls `_sell()` directly: `execute(SELL)` itself now refuses
    before any write (TerminalEvidenceUnavailable), so this exercises the
    pagination-fault handling `_sell()` still has as tested infrastructure."""
    from test_hands_live import _snap
    payloads = ([page([f"owner-{i}"], i, 51, 1) for i in range(1, 51)] if fault == "limit"
                else [page(["owner"], 1, 2, 1), RuntimeError("page outage") if fault == "network" else {}])
    store, client, hands = live(tmp_path, payloads)
    with pytest.raises(RuntimeError):
        if mode == "sell":
            hands._sell(_snap(), exit_reason=None)
        else:
            hands.reconcile()
    assert [c for c in client.calls if c[0] in ("market", "trigger")] == []
    assert store.load_position()["stop_order_id"] == "oid-t"
    assert store.fills() == []
