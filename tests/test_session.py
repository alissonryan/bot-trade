from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kcex.session import token_preview, upsert_env  # noqa: E402


def test_upsert_env(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("FOO=1\nKCEX_TOKEN=old\nBAR=2\n", encoding="utf-8")
    upsert_env("KCEX_TOKEN", "WEBabc", env)
    text = env.read_text(encoding="utf-8")
    assert "KCEX_TOKEN=WEBabc" in text
    assert "FOO=1" in text
    assert "BAR=2" in text
    assert "old" not in text


def test_token_preview() -> None:
    assert token_preview("WEBabcdefghijklmnopqrstuvwxyz").startswith("WEBabc")
