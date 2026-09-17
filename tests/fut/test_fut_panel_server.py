import json
import urllib.error
import urllib.request

import pytest

from fut.panel.reader import PanelReader
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


def test_missing_database_is_a_calm_answer_and_busy_is_a_retry(tmp_path):
    index = tmp_path / "index.html"
    index.write_text("x", encoding="utf-8")
    server = PanelServer(reader=PanelReader(tmp_path / "none.db"), index_path=index, port=0)
    server.start()
    try:
        assert json.loads(get(server, "/api/state")[2]) == {"estado": "sem_banco"}
        assert json.loads(get(server, "/api/events")[2]) == {"estado": "sem_banco", "events": [], "last_id": 0}
    finally:
        server.shutdown()
    assert not (tmp_path / "none.db").exists()

    db = tmp_path / "fut.db"
    conn = make_db(db)
    server = PanelServer(reader=PanelReader(db, timeout_s=0.05), index_path=index, port=0)
    server.start()
    conn.execute("BEGIN EXCLUSIVE")
    try:
        status, headers, _ = get(server, "/api/state")
        assert status == 503 and headers["Retry-After"] == "1"
    finally:
        conn.rollback()
        server.shutdown()
