from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kcex.cli import build_client


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
