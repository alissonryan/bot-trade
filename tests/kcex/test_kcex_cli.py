from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kcex.cli import build_client


class FakeResponse:
    status_code = 200

    def json(self):
        return {"code": 0, "data": {"c": "80000.0"}}


class FakeSession:
    """Scripted transport that records the exact outbound request."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse()


def test_public_client_never_loads_a_token_even_with_one_in_the_environment(tmp_path, monkeypatch):
    """Finding 2: `build_client()` backs ping/ticker/depth/kline -- price-only
    commands with no auth need. `KcexClient()` with no explicit token falls
    back to reading KCEX_TOKEN from the environment; these commands must never
    load a secret just to fetch a public price, even when .env/the shell
    happens to carry one (e.g. left over from a prior `kcex.cli login`)."""
    monkeypatch.setenv("KCEX_TOKEN", "leaked-live-token")
    monkeypatch.chdir(tmp_path)  # load_dotenv() must not find a real .env here

    client = build_client()

    assert client.token == ""


def test_public_client_never_reads_a_real_env_file_and_sends_no_auth_header(tmp_path, monkeypatch):
    """The real regression: a sentinel env var plus a `client.token` check
    does not prove the outbound HTTP contract -- `build_client()` used to
    call `load_dotenv()` unconditionally, which would populate os.environ
    from a REAL .env file (even one KcexClient never directly reads) for the
    rest of the process. Plant a genuine .env with a distinct token, run a
    real request through a fake transport, and check two things directly:
    the request never carries an Authorization header, and the token from
    that .env file never lands in os.environ at all (proof load_dotenv() was
    never called, not just that KcexClient ignored what it loaded)."""
    monkeypatch.delenv("KCEX_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("KCEX_TOKEN=from-a-real-dotenv-file\n")

    client = build_client()
    session = FakeSession()
    client.session = session

    client.ticker("BTC_USDT")

    assert len(session.calls) == 1
    _, _, kwargs = session.calls[0]
    headers = {k.lower(): v for k, v in kwargs["headers"].items()}
    assert "authorization" not in headers
    import os
    assert "KCEX_TOKEN" not in os.environ, "build_client() must never call load_dotenv()"
