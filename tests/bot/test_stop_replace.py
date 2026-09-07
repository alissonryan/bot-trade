"""P6 safety foundation only: all exchange effects are synthetic and in-memory."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from bot.hands import LiveHands, PositionStuck, UnprotectedPosition
from bot.settings import Settings
from bot.store import Store
from bot.types import GateResult, Snapshot
from kcex.client import KcexError


QTY = 0.00025
BASE = 0.00064


class Exchange:
    def __init__(self):
        self.total = BASE + QTY
        self.ids = {"old", "owner"}
        self.calls = []
        self.trigger_error: BaseException | None = None
        self.cancel_error: BaseException | None = None
        self.sell_error: BaseException | None = None

    def balances(self, *args):
        return {"data": [{"currency": "BTC", "available": self.total, "frozen": 0}]}

    def open_orders(self, **kwargs):
        return {"data": [{"id": i} for i in self.ids], "total": len(self.ids)}

    def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))
        if self.cancel_error:
            raise self.cancel_error
        self.ids.remove(order_id)
        return {"code": 200}

    def place_trigger(self, **kwargs):
        self.calls.append(("trigger", kwargs))
        if self.trigger_error:
            raise self.trigger_error
        self.ids.add("new")
        return {"code": 0, "data": "new"}

    def place_market(self, **kwargs):
        self.calls.append(("market", kwargs))
        if self.sell_error:
            raise self.sell_error
        self.total -= float(kwargs["quantity"])
        return {"code": 0, "data": "exit"}

    def my_deals(self, *args, **kwargs):
        return {"data": []}


def snap():
    return Snapshot(1, 81000, 80999, 81001, 2, [], 400, 450, QTY, 80000, True, False)


def setup(tmp_path):
    store = Store(tmp_path / "bot.db", mode="live")
    store.remember_order("old")
    store.save_position(qty=QTY, entry=80000, stop_price=79200, entry_order_id="entry",
                        stop_order_id="old", btc_before=BASE, opened_ts="2026-09-01T00:00:00+00:00")
    client = Exchange()
    settings = replace(Settings.from_env(), mode="live", fill_confirm_tries=1, fill_confirm_wait_s=0)
    return store, client, LiveHands(settings, store, client, sleep=lambda _: None)


def test_cancel_ok_proven_replace_rejection_flattens(tmp_path):
    store, client, hands = setup(tmp_path)
    # Known request rejection, NOT a timeout or an undocumented business code.
    client.trigger_error = KcexError("WAF rejected request", {"status": 406}, http_status=406)
    hands.replace_stop(80000, snap())
    assert [c[0] for c in client.calls] == ["cancel", "trigger", "market"]
    assert client.calls[-1][1]["quantity"] == "0.00025"
    assert client.ids == {"owner"}
    assert not hands.position.is_open()
    assert store.load_position() is None
    assert [f["side"] for f in store.fills()] == ["SELL"]


def restart(store, client, hands):
    store._conn.close()
    return LiveHands(hands.settings, Store(store.path, mode="live"), client, sleep=lambda _: None)


def test_replacement_and_flatten_failure_stays_halted_after_restart(tmp_path):
    store, client, hands = setup(tmp_path)
    client.trigger_error = KcexError("rejected", {"status": 406}, http_status=406)
    client.sell_error = RuntimeError("lost sell response")
    with pytest.raises(UnprotectedPosition):
        hands.replace_stop(80000, snap())
    assert store.load_position()["state"] == "UNPROTECTED"
    recovered = restart(store, client, hands)
    with pytest.raises(UnprotectedPosition):
        recovered.reconcile()
    assert [c[0] for c in client.calls] == ["cancel", "trigger", "market"]
    assert recovered.store.fills() == []


def test_ambiguous_cancel_blocks_then_reconcile_keeps_owned_stop(tmp_path):
    store, client, hands = setup(tmp_path)
    client.cancel_error = RuntimeError("lost cancel response")
    with pytest.raises(UnprotectedPosition):
        hands.replace_stop(80000, snap())
    recovered = restart(store, client, hands)
    assert recovered.reconcile() == "replacement_cancel_not_confirmed"
    assert recovered.position.state == "OPEN"
    assert recovered.stop_order_id == "old"
    assert client.calls == [("cancel", "old")]


def test_crash_after_cancel_before_replacement_resumes_once(tmp_path):
    store, client, hands = setup(tmp_path)
    original = client.cancel_order
    def crash(order_id):
        original(order_id)
        raise SystemExit("power loss after cancel")
    client.cancel_order = crash
    with pytest.raises(SystemExit):
        hands.replace_stop(80000, snap())
    recovered = restart(store, client, hands)
    assert recovered.reconcile() == "stop_replaced"
    assert recovered.position.stop_price == 80000
    assert recovered.position.state == "OPEN"
    assert recovered.position.opened_ts == "2026-09-01T00:00:00+00:00"
    assert client.ids == {"owner", "new"}
    assert [c[0] for c in client.calls] == ["cancel", "trigger"]


@pytest.mark.parametrize("error", [RuntimeError("lost response"), SystemExit("power loss in POST")])
def test_replace_post_ambiguity_never_retries_or_flattens_on_restart(tmp_path, error):
    store, client, hands = setup(tmp_path)
    client.trigger_error = error
    with pytest.raises(SystemExit if isinstance(error, SystemExit) else UnprotectedPosition):
        hands.replace_stop(80000, snap())
    assert store.load_position()["state"] == "UNPROTECTED"
    recovered = restart(store, client, hands)
    with pytest.raises(UnprotectedPosition):
        recovered.reconcile()
    assert [c[0] for c in client.calls] == ["cancel", "trigger"]
    assert recovered.store.fills() == []


@pytest.mark.parametrize("price", [float("nan"), float("inf"), 0, -1, 79000, 81000])
def test_invalid_replacement_never_cancels(tmp_path, price):
    store, client, hands = setup(tmp_path)
    with pytest.raises(ValueError):
        hands.replace_stop(price, snap())
    assert client.calls == []
    assert store.load_position()["state"] == "OPEN"


def test_foreign_stop_is_never_cancelled(tmp_path):
    store, client, hands = setup(tmp_path)
    hands.stop_order_id = "owner"
    with pytest.raises(PositionStuck):
        hands.replace_stop(80000, snap())
    assert client.calls == []


@pytest.mark.parametrize("total", [BASE, BASE + QTY / 2, 0, float("nan")])
def test_balance_change_after_cancel_cannot_place_or_flatten(tmp_path, total):
    store, client, hands = setup(tmp_path)
    original = client.cancel_order
    def change_balance(order_id):
        original(order_id)
        client.total = total
    client.cancel_order = change_balance
    with pytest.raises(UnprotectedPosition):
        hands.replace_stop(80000, snap())
    assert [c[0] for c in client.calls] == ["cancel"]
    assert store.load_position()["state"] == "UNPROTECTED"
    assert store.fills() == []


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": [{}]}])
def test_malformed_order_list_is_not_cancel_confirmation(tmp_path, payload):
    store, client, hands = setup(tmp_path)
    client.open_orders = lambda **kwargs: payload
    with pytest.raises(UnprotectedPosition):
        hands.replace_stop(80000, snap())
    assert [c[0] for c in client.calls] == ["cancel"]


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": {"surprise": "new"}}])
def test_unknown_trigger_response_halts_and_does_not_invent_id(tmp_path, payload):
    store, client, hands = setup(tmp_path)
    def trigger(**kwargs):
        client.calls.append(("trigger", kwargs))
        return payload
    client.place_trigger = trigger
    with pytest.raises(UnprotectedPosition):
        hands.replace_stop(80000, snap())
    recovered = restart(store, client, hands)
    with pytest.raises(UnprotectedPosition):
        recovered.reconcile()
    assert [c[0] for c in client.calls] == ["cancel", "trigger"]


def test_cancel_confirmation_paginates_and_never_repeats_cancel(tmp_path):
    store, client, hands = setup(tmp_path)
    client.cancel_order = lambda oid: client.calls.append(("cancel", oid))
    pages = []
    def orders(**kwargs):
        page = kwargs.get("page_num", 1)
        pages.append(page)
        return {"data": [{"id": f"foreign-{i}"} for i in range(100)] if page == 1 else [{"id": "old"}],
                "total": 101, "pageSize": 100, "pageNum": page}
    client.open_orders = orders
    hands.replace_stop(80000, snap())
    assert pages == [1, 2]
    with pytest.raises(UnprotectedPosition):
        hands.execute(GateResult(True, "ok_sell", "SELL", qty="0.00025"), snap())
    assert client.calls == [("cancel", "old")]


def test_replacement_also_respects_capped_short_page(tmp_path):
    from test_open_order_pagination import Pages, page
    store, client, hands = setup(tmp_path)
    client.cancel_order = lambda oid: client.calls.append(("cancel", oid))
    client.open_orders = Pages([page(["owner"], 1, 2, 1), page(["old"], 2, 2, 1)]).open_orders
    hands.replace_stop(80000, snap())
    assert client.calls == [("cancel", "old")]
    assert store.load_position()["stop_order_id"] == "old"


def test_stop_id_persistence_failure_halts_without_retry(tmp_path):
    store, client, hands = setup(tmp_path)
    def unavailable(oid):
        raise RuntimeError("disk failed after successful POST")
    store.remember_order = unavailable
    with pytest.raises(UnprotectedPosition):
        hands.replace_stop(80000, snap())
    recovered = restart(store, client, hands)
    with pytest.raises(UnprotectedPosition):
        recovered.reconcile()
    assert [c[0] for c in client.calls] == ["cancel", "trigger"]


def test_acknowledged_stop_must_be_resident_before_open(tmp_path):
    store, client, hands = setup(tmp_path)
    def accepted_but_missing(**kwargs):
        client.calls.append(("trigger", kwargs))
        return {"code": 0, "data": "new"}
    client.place_trigger = accepted_but_missing
    with pytest.raises(UnprotectedPosition):
        hands.replace_stop(80000, snap())
    assert store.is_bot_order("new")
    assert store.load_position()["state"] == "UNPROTECTED"
    assert [c[0] for c in client.calls] == ["cancel", "trigger"]


@pytest.mark.parametrize("stage", ["cancel", "placing"])
def test_real_process_crash_preserves_write_ahead_protocol(tmp_path, stage):
    store, client, hands = setup(tmp_path)
    store._conn.close()
    script = """
import os, sys
from dataclasses import replace
from pathlib import Path
from test_stop_replace import Exchange, snap
from bot.hands import LiveHands
from bot.settings import Settings
from bot.store import Store
client = Exchange()
def crash(*args, **kwargs):
    os._exit(23)
if sys.argv[2] == 'cancel':
    client.cancel_order = crash
else:
    client.place_trigger = crash
hands = LiveHands(replace(Settings.from_env(), mode='live'), Store(Path(sys.argv[1]), mode="live"), client)
hands.replace_stop(80000, snap())
"""
    root = Path(__file__).resolve().parents[2]
    script = f"import sys; sys.path.insert(0, {str(root / 'tests' / 'bot')!r})\n" + script
    result = subprocess.run([sys.executable, "-c", script, str(store.path), stage], cwd=root,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 23, result.stderr
    reopened = Store(store.path, mode="live")
    assert json.loads(reopened.kv_get("stop_replacement"))["phase"] == stage
    # Synthetic exchange truth: the cancelled id is absent after the process died.
    client.ids.remove("old")
    recovered = LiveHands(hands.settings, reopened, client, sleep=lambda _: None)
    if stage == "cancel":
        assert recovered.reconcile() == "stop_replaced"
        assert [c[0] for c in client.calls] == ["trigger"]
    else:
        with pytest.raises(UnprotectedPosition):
            recovered.reconcile()
        assert client.calls == []


def test_ambiguous_replacement_boot_exits_two_before_market_data(tmp_path):
    from bot.cli import _loop
    store, client, hands = setup(tmp_path)
    client.trigger_error = RuntimeError("lost response")
    with pytest.raises(UnprotectedPosition):
        hands.replace_stop(80000, snap())
    recovered = restart(store, client, hands)
    assert _loop(True, recovered.settings, client, recovered.store, object(), recovered) == 2
    assert [c[0] for c in client.calls] == ["cancel", "trigger"]


@pytest.mark.parametrize("status", [401, 406])
def test_business_status_never_authorizes_replacement_flatten(tmp_path, status):
    store, client, hands = setup(tmp_path)
    client.trigger_error = KcexError("unknown business error", {"code": 90001, "status": status}, http_status=200)
    with pytest.raises(UnprotectedPosition):
        hands.replace_stop(80000, snap())
    assert [c[0] for c in client.calls] == ["cancel", "trigger"]
