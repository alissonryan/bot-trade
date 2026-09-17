import json
import threading
import urllib.error
import urllib.request

import pytest

from fut.panel.reader import PanelReader
from fut.panel.reader import PanelDbBroken
from fut.panel.server import PanelServer
from tests.fut.panel_db import add_decision, make_db

T0 = 1_789_000_000_000


@pytest.fixture
def served(tmp_path):
    db = tmp_path / "fut.db"
    conn = make_db(db)
    index = tmp_path / "index.html"
    index.write_text("<h1>painel</h1>", encoding="utf-8")
    server = PanelServer(reader=PanelReader(db, timeout_s=0.05), index_path=index, port=0, clock_ms=lambda: T0)
    server.start()
    server.refresh_now()
    yield server, conn
    server.shutdown()


def get(server, path, headers=None, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_refuses_to_bind_off_loopback(tmp_path):
    with pytest.raises(ValueError):
        PanelServer(reader=PanelReader(tmp_path / "x.db"), index_path=tmp_path / "i.html", host="0.0.0.0")


def test_serves_the_page_state_and_events(served):
    server, conn = served
    first = add_decision(conn, T0 - 1000, "exit", {"reason": "stop"})
    server.refresh_now()
    status, headers, body = get(server, "/")
    assert status == 200 and b"painel" in body and headers["Content-Type"].startswith("text/html")
    status, headers, body = get(server, "/api/state")
    state = json.loads(body)
    assert status == 200 and state["estado"] == "ok" and state["bot"]["vivo"] is True
    assert headers["Cache-Control"] == "no-store"
    status, _, body = get(server, "/api/events")
    page = json.loads(body)
    assert [e["id"] for e in page["events"]] == [first] and page["last_id"] == first
    second = add_decision(conn, T0, "exit", {"reason": "time_limit"})
    page = json.loads(get(server, f"/api/events?after={first}")[2])
    assert [e["id"] for e in page["events"]] == [second]


@pytest.mark.parametrize("path, code", [("/nope", 404), ("/api/events?after=abc", 400), ("/api/events?after=-1", 400)])
def test_bad_requests(served, path, code):
    assert get(served[0], path)[0] == code


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_only_get_is_allowed(served, method):
    assert get(served[0], "/api/state", method=method)[0] == 405


def test_foreign_origin_or_host_is_forbidden_on_the_api(served):
    server, _ = served
    assert get(server, "/api/state", headers={"Origin": "http://evil.example"})[0] == 403
    assert get(server, "/api/state", headers={"Host": "evil.example"})[0] == 403
    assert get(server, "/api/state", headers={"Origin": f"http://127.0.0.1:{server.port}"})[0] == 200


def test_two_state_gets_do_not_refresh_the_database(tmp_path):
    db = tmp_path / "fut.db"
    make_db(db).close()
    index = tmp_path / "index.html"
    index.write_text("x", encoding="utf-8")
    reader = PanelReader(db)
    server = PanelServer(reader=reader, index_path=index, port=0, clock_ms=lambda: T0)
    server.start()
    try:
        server.refresh_now()
        server._refresh_stop.set()
        original = reader.decision_facts
        calls = []
        reader.decision_facts = lambda *args, **kwargs: calls.append((args, kwargs))
        assert get(server, "/api/state")[0] == 200
        assert get(server, "/api/state")[0] == 200
        assert calls == []
        reader.decision_facts = original
    finally:
        server.shutdown()


def test_missing_state_is_calm_and_busy_keeps_last_cached_state(tmp_path):
    index = tmp_path / "index.html"
    index.write_text("x", encoding="utf-8")
    server = PanelServer(reader=PanelReader(tmp_path / "none.db"), index_path=index, port=0)
    server.start()
    try:
        server.refresh_now()
        assert json.loads(get(server, "/api/state")[2]) == {"estado": "sem_banco"}
        assert json.loads(get(server, "/api/events")[2]) == {"estado": "sem_banco", "events": [], "last_id": 0}
    finally:
        server.shutdown()
    assert not (tmp_path / "none.db").exists()

    db = tmp_path / "fut.db"
    conn = make_db(db)
    server = PanelServer(reader=PanelReader(db, timeout_s=0.05), index_path=index, port=0)
    server.start()
    server.refresh_now()
    before = get(server, "/api/state")[2]
    conn.execute("BEGIN EXCLUSIVE")
    try:
        status, headers, _ = get(server, "/api/state")
        assert status == 200 and json.loads(get(server, "/api/state")[2]) == json.loads(before)
    finally:
        conn.rollback()
        server.shutdown()


def test_broken_database_is_a_calm_invalid_database_answer(tmp_path):
    db = tmp_path / "fut.db"
    make_db(db).close()
    index = tmp_path / "index.html"
    index.write_text("x", encoding="utf-8")
    server = PanelServer(reader=PanelReader(db), index_path=index, port=0)
    server.cache.refresh = lambda **kwargs: (_ for _ in ()).throw(PanelDbBroken("broken"))
    server.refresh_now()
    server.start()
    try:
        assert json.loads(get(server, "/api/state")[2]) == {"estado": "banco_invalido"}
    finally:
        server.shutdown()


def test_refresh_loop_survives_an_unexpected_error_and_recovers(tmp_path):
    db = tmp_path / "fut.db"
    make_db(db).close()
    index = tmp_path / "index.html"
    index.write_text("x", encoding="utf-8")
    server = PanelServer(reader=PanelReader(db), index_path=index, port=0)
    calls = []
    first_failed = threading.Event()
    waits = []

    def refresh():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            first_failed.set()
            raise RuntimeError("transient")
        server._refresh_stop.set()

    def wait(timeout):
        waits.append(timeout)
        return len(waits) >= 2

    server.refresh_now = refresh
    server._refresh_stop.wait = wait
    thread = threading.Thread(target=server._refresh_loop)
    thread.start()
    assert first_failed.wait(1)
    thread.join(1)
    assert calls == [1, 2]
    assert not thread.is_alive()
    assert json.loads(server.state_bytes) == {"estado": "erro_painel", "detalhe": "RuntimeError"}


def test_refresh_loop_catches_up_quickly_while_cache_is_loading(tmp_path):
    db = tmp_path / "fut.db"
    make_db(db).close()
    index = tmp_path / "index.html"
    index.write_text("x", encoding="utf-8")
    server = PanelServer(reader=PanelReader(db), index_path=index, port=0)
    waits = []
    calls = []

    def refresh():
        calls.append(len(calls) + 1)
        server.cache.loading = len(calls) == 1
        if len(calls) == 2:
            server._refresh_stop.set()

    def wait(timeout):
        waits.append(timeout)
        return len(waits) == 2

    server.refresh_now = refresh
    server._refresh_stop.wait = wait
    thread = threading.Thread(target=server._refresh_loop)
    thread.start()
    thread.join(1)
    assert calls == [1, 2] and waits == [0.05, 1.0]


def test_refresh_now_reads_the_clock_once(tmp_path):
    db = tmp_path / "fut.db"
    make_db(db).close()
    index = tmp_path / "index.html"
    index.write_text("x", encoding="utf-8")
    clock_calls = []
    server = PanelServer(reader=PanelReader(db), index_path=index, port=0,
                         clock_ms=lambda: clock_calls.append(T0) or T0)
    server.refresh_now()
    assert clock_calls == [T0]
