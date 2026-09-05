from pathlib import Path
import sys

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kcex.client import KcexClient, KcexError  # noqa: E402


class FakeResponse:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"code": 0, "data": "ok"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"http {self.status_code}")


class FakeSession:
    """Scripted transport: each call pops the next response (or raises the next exception)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(script, **kw):
    sleeps = []
    client = KcexClient(token="WEBtok", session=FakeSession(script), sleep=sleeps.append, **kw)
    return client, sleeps


def test_sends_browser_user_agent_and_auth_headers():
    client, _ = _client([FakeResponse(200)])
    client.ping()
    headers = client.session.calls[0][2]["headers"]
    assert headers["user-agent"].startswith("Mozilla/5.0")
    assert headers["authorization"] == "WEBtok"
    assert headers["platform"] == "WEB"


def test_get_retries_on_429_then_succeeds():
    client, sleeps = _client([FakeResponse(429), FakeResponse(200, {"code": 0, "data": 1})])
    assert client.ping()["data"] == 1
    assert len(client.session.calls) == 2
    assert sleeps == [0.5]


def test_get_retries_on_network_error():
    client, sleeps = _client([requests.ConnectionError("boom"), FakeResponse(200)])
    client.ping()
    assert len(client.session.calls) == 2
    assert sleeps == [0.5]


def test_get_gives_up_after_retries():
    client, _ = _client([FakeResponse(503), FakeResponse(503), FakeResponse(503)])
    with pytest.raises(KcexError) as exc:
        client.ping()
    assert exc.value.status == 503
    assert len(client.session.calls) == 3


def test_post_never_retries():
    client, sleeps = _client([FakeResponse(503), FakeResponse(200)])
    with pytest.raises(KcexError):
        client.place_market(currency="BTC", market="USDT", side="BUY", price="1", quantity="0.001")
    assert len(client.session.calls) == 1
    assert sleeps == []


def test_406_reports_waf_block_with_hint():
    client, _ = _client([FakeResponse(406)])
    with pytest.raises(KcexError) as exc:
        client.ticker()
    assert exc.value.status == 406
    assert "KCEX_USER_AGENT" in str(exc.value)


def test_401_reports_dead_session():
    client, _ = _client([FakeResponse(401)])
    with pytest.raises(KcexError) as exc:
        client.user_info()
    assert exc.value.status == 401
    assert "login" in str(exc.value)


def test_business_error_code_raises():
    client, _ = _client([FakeResponse(200, {"code": 30001, "msg": "insufficient balance"})])
    with pytest.raises(KcexError) as exc:
        client.balances()
    assert "insufficient balance" in str(exc.value)
