"""P7 stop submission safety; no real exchange requests."""
import pytest
import requests
import ast
import inspect
import json
from pathlib import Path
import subprocess
import sys

from bot.hands import LiveHands, UnprotectedPosition
from bot.store import Store
from bot.types import GateResult
from kcex.client import KcexClient, KcexError
from test_hands_live import FakeClient, _hands, _open_position, _buy_gate, _snap, FOREIGN_BTC


@pytest.mark.parametrize("error", [requests.Timeout("lost"), requests.ConnectionError("unknown send"),
                                  KcexError("server error", http_status=503),
                                  KcexError("business status", {"status": 406}, http_status=200)])
def test_lost_stop_response_never_reposts_or_sells_after_restart(tmp_path, error):
    store = Store(tmp_path / "p7.db", mode="live")
    _open_position(store)
    def timeout():
        raise error
    client = FakeClient(btc=[FOREIGN_BTC + .00025], on_trigger=timeout)
    hands = _hands(store, client)
    with pytest.raises(UnprotectedPosition):
        hands._place_stop("0.00025", 79200)
    assert [c[0] for c in client.calls] == ["trigger"]
    assert store.load_position()["state"] == "UNPROTECTED"
    store._conn.close()
    recovered = _hands(Store(store.path, mode="live"), client)
    with pytest.raises(UnprotectedPosition):
        recovered.reconcile()
    with pytest.raises(UnprotectedPosition):
        recovered.execute(GateResult(True, "ok_close", "SELL", qty="0.00025"), _snap())
    assert [c[0] for c in client.calls] == ["trigger"]
    assert recovered.store.fills() == []


@pytest.mark.parametrize("path", ["buy", "restore_sell", "reconcile"])
def test_each_stop_caller_halts_on_ambiguity(tmp_path, path):
    store = Store(tmp_path / "p7.db", mode="live")
    if path != "buy":
        _open_position(store)
    def timeout():
        raise requests.Timeout("accepted then lost response")
    btc = [FOREIGN_BTC, FOREIGN_BTC + .00025] if path == "buy" else [FOREIGN_BTC + .00025]
    client = FakeClient(btc=btc, open_ids=[set()], on_trigger=timeout, sell_fail=path == "restore_sell")
    hands = _hands(store, client)
    with pytest.raises(UnprotectedPosition):
        if path == "buy":
            hands.execute(_buy_gate(), _snap())
        elif path == "restore_sell":
            # M1 note: execute(SELL) itself now refuses before any write; this
            # exercises _sell()'s own stop-restore-on-failed-sell mechanics
            # directly (tested infrastructure, see test_exit_latch.py).
            hands._sell(_snap(), exit_reason=None)
        else:
            hands.reconcile()
    assert sum(c[0] == "trigger" for c in client.calls) == 1
    assert sum(c[0] == "market" and c[1]["side"] == "SELL" for c in client.calls) == (path == "restore_sell")
    before = list(client.calls)
    store._conn.close()
    recovered = _hands(Store(store.path, mode="live"), client)
    from bot.cli import _loop
    assert _loop(True, recovered.settings, client, recovered.store, object(), recovered) == 2
    assert client.calls == before


@pytest.mark.parametrize("sell_fail", [False, True])
def test_proven_stop_rejection_flattens_once_and_failed_flatten_halts(tmp_path, sell_fail):
    store = Store(tmp_path / "p7.db", mode="live")
    balances = [FOREIGN_BTC, FOREIGN_BTC + .00025, FOREIGN_BTC + .00025]
    if not sell_fail:
        balances.append(FOREIGN_BTC)
    client = FakeClient(btc=balances, trigger_fail=1, sell_fail=sell_fail)
    hands = _hands(store, client)
    if sell_fail:
        with pytest.raises(UnprotectedPosition):
            hands.execute(_buy_gate(), _snap())
        from bot.cli import _loop
        store._conn.close()
        recovered = _hands(Store(store.path, mode="live"), client)
        assert _loop(True, recovered.settings, client, recovered.store, object(), recovered) == 2
    else:
        assert not hands.execute(_buy_gate(), _snap()).is_open()
        assert not store.kv_get("stop_submission")
    assert sum(c[0] == "trigger" for c in client.calls) == 1
    assert sum(c[0] == "market" and c[1]["side"] == "SELL" for c in client.calls) == 1


def test_no_write_or_write_helper_inside_any_hands_loop():
    import bot.hands as module
    tree = ast.parse(inspect.getsource(module))
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    writes = {"place_trigger", "place_market", "place_order", "cancel_order", "post", "delete"}
    def reaches_write(node, seen):
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            func = call.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in writes:
                return True
            if name in functions and name not in seen and reaches_write(functions[name], seen | {name}):
                return True
        return False
    loops = [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.AsyncFor, ast.While, ast.comprehension))]
    assert not [getattr(n, "lineno", None) for n in loops if reaches_write(n, set())]


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": {"id": "unexpected"}}])
def test_malformed_stop_success_never_clears_submission_latch(tmp_path, payload):
    store = Store(tmp_path / "p7.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + .00025])
    def malformed(**kwargs):
        client.calls.append(("trigger", kwargs))
        return payload
    client.place_trigger = malformed
    hands = _hands(store, client)
    with pytest.raises(UnprotectedPosition):
        hands._place_stop("0.00025", 79200)
    assert json.loads(store.kv_get("stop_submission"))["phase"] == "submitting"
    assert store.load_position()["state"] == "UNPROTECTED"
    assert len(client.calls) == 1


def test_stop_id_persistence_failure_is_fatal_and_latched(tmp_path):
    store = Store(tmp_path / "p7.db", mode="live")
    _open_position(store)
    def unavailable(order_id):
        raise RuntimeError("disk failed after acceptance")
    store.remember_order = unavailable
    client = FakeClient(btc=[FOREIGN_BTC + .00025])
    hands = _hands(store, client)
    with pytest.raises(UnprotectedPosition):
        hands._place_stop("0.00025", 79200)
    assert store.kv_get("stop_submission")
    assert store.load_position()["state"] == "UNPROTECTED"
    assert len(client.calls) == 1


def test_hard_process_crash_leaves_stop_submission_latched(tmp_path):
    store = Store(tmp_path / "p7.db", mode="live")
    _open_position(store)
    store._conn.close()
    root = Path(__file__).resolve().parents[2]
    script = f"import sys; sys.path.insert(0, {str(root / 'tests' / 'bot')!r})\n" + """
import os, sys
from pathlib import Path
from test_hands_live import FakeClient, _hands, FOREIGN_BTC
from bot.store import Store
def crash():
    os._exit(23)
client = FakeClient(btc=[FOREIGN_BTC + .00025], on_trigger=crash)
_hands(Store(Path(sys.argv[1]), mode="live"), client)._place_stop('0.00025', 79200)
"""
    result = subprocess.run([sys.executable, "-c", script, str(store.path)], cwd=root,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 23, result.stderr
    client = FakeClient(btc=[FOREIGN_BTC + .00025])
    recovered = _hands(Store(store.path, mode="live"), client)
    from bot.cli import _loop
    assert _loop(True, recovered.settings, client, recovered.store, object(), recovered) == 2
    assert client.calls == []


@pytest.mark.parametrize("status,business", [(406, False), (200, True)])
def test_real_transport_provenance_controls_entry_fallback(tmp_path, status, business):
    class Response:
        status_code = status
        def json(self):
            return {"code": 90001, "status": 406} if business else {}
    class Transport:
        calls = 0
        def request(self, *args, **kwargs):
            self.calls += 1
            return Response()
    transport = Transport()
    store = Store(tmp_path / "p7.db", mode="live")
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + .00025, FOREIGN_BTC + .00025, FOREIGN_BTC])
    client.place_trigger = KcexClient(token="", session=transport).place_trigger
    hands = _hands(store, client)
    if business:
        with pytest.raises(UnprotectedPosition):
            hands.execute(_buy_gate(), _snap())
    else:
        assert not hands.execute(_buy_gate(), _snap()).is_open()
    assert transport.calls == 1
    assert sum(c[0] == "market" and c[1]["side"] == "SELL" for c in client.calls) == (not business)


def test_rejection_state_write_failure_cannot_replace_fatal_exit(tmp_path):
    store = Store(tmp_path / "p7.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + .00025], trigger_fail=1)
    hands = _hands(store, client)
    original = hands._persist
    def unavailable(state=None):
        if state == "UNPROTECTED":
            raise RuntimeError("position state write failed")
        original(state)
    hands._persist = unavailable
    with pytest.raises(UnprotectedPosition):
        hands._place_stop("0.00025", 79200)
    assert store.kv_get("stop_submission")
    assert [c[0] for c in client.calls] == ["trigger"]


@pytest.mark.parametrize("path", ["restore_sell", "reconcile"])
def test_proven_rejection_in_recovery_never_starts_another_sell(tmp_path, path):
    store = Store(tmp_path / "p7.db", mode="live")
    _open_position(store)
    client = FakeClient(btc=[FOREIGN_BTC + .00025], open_ids=[set()], trigger_fail=1,
                        sell_fail=path == "restore_sell")
    hands = _hands(store, client)
    with pytest.raises(UnprotectedPosition):
        if path == "reconcile":
            hands.reconcile()
        else:
            hands._sell(_snap(), exit_reason=None)  # direct call: execute(SELL) itself now refuses first
    before = list(client.calls)
    store._conn.close()
    with pytest.raises(UnprotectedPosition):
        _hands(Store(store.path, mode="live"), client).reconcile()
    assert client.calls == before
    assert sum(c[0] == "trigger" for c in client.calls) == 1
    assert sum(c[0] == "market" and c[1]["side"] == "SELL" for c in client.calls) == (path == "restore_sell")


@pytest.mark.parametrize("stage", ["persist_success", "clear_success", "clear_after_flatten"])
def test_completion_storage_fault_keeps_latch_and_fatal_exit(tmp_path, stage):
    store = Store(tmp_path / "p7.db", mode="live")
    client = FakeClient(btc=[FOREIGN_BTC, FOREIGN_BTC + .00025, FOREIGN_BTC + .00025, FOREIGN_BTC],
                        trigger_fail=int(stage == "clear_after_flatten"))
    hands = _hands(store, client)
    persist = hands._persist
    write = store.kv_set
    def fail_persist(state=None, *, commit=True):
        if state == "OPEN":
            raise RuntimeError("successful stop position write failed")
        persist(state, commit=commit)
    def fail_clear(key, value):
        if key == "stop_submission" and value == "":
            raise RuntimeError("latch clear failed")
        write(key, value)
    if stage == "persist_success":
        hands._persist = fail_persist
    else:
        store.kv_set = fail_clear
    with pytest.raises(UnprotectedPosition):
        hands.execute(_buy_gate(), _snap())
    before = list(client.calls)
    store._conn.close()
    with pytest.raises(UnprotectedPosition):
        _hands(Store(store.path, mode="live"), client).reconcile()
    assert client.calls == before
    assert sum(c[0] == "trigger" for c in client.calls) == 1
    assert sum(c[0] == "market" and c[1]["side"] == "SELL" for c in client.calls) == (stage == "clear_after_flatten")
