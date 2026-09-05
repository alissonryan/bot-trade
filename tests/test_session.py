import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kcex.session import save_token, token_age_days, token_preview, upsert_env  # noqa: E402


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


def test_save_token_writes_timestamp_and_restricts_mode(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    save_token("WEB" + "a" * 64, env, now=now)
    text = env.read_text(encoding="utf-8")
    assert "KCEX_TOKEN=WEB" in text
    assert "KCEX_TOKEN_AT=2026-09-04T12:00:00+00:00" in text
    if os.name == "posix":
        assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_save_token_rejects_non_web_token(tmp_path: Path) -> None:
    try:
        save_token("abc", tmp_path / ".env")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_token_age_days() -> None:
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    at = (now - timedelta(days=6, hours=12)).isoformat()
    age = token_age_days(at, now=now)
    assert age is not None and abs(age - 6.5) < 1e-6
    assert token_age_days(None) is None
    assert token_age_days("garbage") is None
