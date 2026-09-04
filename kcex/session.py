"""Persist KCEX session token in the local .env file."""

from __future__ import annotations

from pathlib import Path

ENV_KEY = "KCEX_TOKEN"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def env_path() -> Path:
    return project_root() / ".env"


def profile_dir() -> Path:
    return project_root() / ".kcex-profile"


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
    return target


def save_token(token: str) -> Path:
    token = token.strip()
    if not token.startswith("WEB"):
        raise ValueError("token inesperado: deveria comecar com WEB")
    return upsert_env(ENV_KEY, token)


def token_preview(token: str) -> str:
    if len(token) <= 10:
        return token[:3] + "…"
    return f"{token[:6]}…{token[-4:]}"
