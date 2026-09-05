"""Persist the KCEX session token in the local .env file.

The token is the whole account (there is no scoped API key), so the file is written
with mode 600 and the login time is stored next to it (``KCEX_TOKEN_AT``) so the bot can
warn before the ~7-day session dies.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

ENV_KEY = "KCEX_TOKEN"
ENV_AT_KEY = "KCEX_TOKEN_AT"
SESSION_DAYS = 7.0


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def env_path() -> Path:
    return project_root() / ".env"


def profile_dir() -> Path:
    return project_root() / ".kcex-profile"


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def upsert_env(key: str, value: str, path: Path | None = None) -> Path:
    target = path or env_path()
    lines: list[str] = []
    found = False
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"{key}={value}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _restrict(target)
    return target


def save_token(token: str, path: Path | None = None, *, now: datetime | None = None) -> Path:
    token = token.strip()
    if not token.startswith("WEB"):
        raise ValueError("token inesperado: deveria comecar com WEB")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    target = upsert_env(ENV_KEY, token, path)
    upsert_env(ENV_AT_KEY, stamp, target)
    return target


def token_age_days(token_at: str | None, *, now: datetime | None = None) -> float | None:
    """Days since the token was captured, or None if the timestamp is missing or unreadable."""
    if not token_at:
        return None
    try:
        then = datetime.fromisoformat(token_at.strip())
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (current - then).total_seconds() / 86400.0


def token_preview(token: str) -> str:
    if len(token) <= 10:
        return token[:3] + "…"
    return f"{token[:6]}…{token[-4:]}"
