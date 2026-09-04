"""Browser login for KCEX — same idea as OAuth: human proves identity, CLI stores the session."""

from __future__ import annotations

import os
import time
from typing import Any

from .client import DEFAULT_BASE, KcexClient
from .session import profile_dir, save_token, token_preview

LOGIN_URL = f"{DEFAULT_BASE}/en-US/login?previous=%2Fexchange%2FBTC_USDT"
HOME_HINTS = ("/exchange/", "/assets/", "/markets")


def _extract_token(cookies: list[dict[str, Any]]) -> str | None:
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if name.lower() == "authorization" and value.startswith("WEB") and len(value) >= 20:
            return value
    return None


def _session_is_live(token: str) -> bool:
    try:
        payload = KcexClient(token=token).user_info()
    except Exception:
        return False
    return payload.get("code") in (0, 200, "0", "200") or payload.get("success") is True


def _try_fill_credentials(page: Any) -> None:
    email = os.getenv("KCEX_EMAIL", "").strip()
    password = os.getenv("KCEX_PASSWORD", "").strip()
    if not email or not password:
        return
    try:
        email_box = page.get_by_placeholder("Email/Phone Number")
        if email_box.count() == 0:
            email_box = page.locator('input[type="text"]').first
        pass_box = page.get_by_placeholder("Please enter your password")
        if pass_box.count() == 0:
            pass_box = page.locator('input[type="password"]').first
        email_box.fill(email, timeout=5000)
        pass_box.fill(password, timeout=5000)
        stay = page.get_by_text("Stay logged in")
        if stay.count():
            checkbox = page.locator('input[type="checkbox"]').first
            if checkbox.count() and not checkbox.is_checked():
                checkbox.check()
        login_btn = page.get_by_role("button", name="Log In")
        if login_btn.count():
            login_btn.click()
        print("E-mail e senha preenchidos. Resolva o captcha e o código do autenticador na janela.")
    except Exception:
        print("Não consegui preencher o formulário. Faça o login na janela que abriu.")


def login_interactive(*, timeout_sec: int = 600) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright não está instalado. Rode:\n"
            "  pip install playwright\n"
            "  python -m playwright install chrome"
        ) from exc

    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_sec

    print("Abrindo o Chrome do bot (perfil salvo em .kcex-profile/).")
    print("Se pedir captcha ou Google Authenticator, complete na janela — igual a uma tela de OAuth.")
    print("O terminal captura o token sozinho quando o login terminar.")

    with sync_playwright() as playwright:
        context = None
        last_error: Exception | None = None
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=False,
                    viewport={"width": 1280, "height": 860},
                    args=["--disable-blink-features=AutomationControlled"],
                    **kwargs,
                )
                break
            except Exception as exc:
                last_error = exc
        if context is None:
            raise RuntimeError(
                "Não abriu o browser. Instale o Chrome ou rode:\n"
                "  python -m playwright install chromium\n"
                f"Detalhe: {last_error}"
            )

        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            time.sleep(1.5)
            token = _extract_token(context.cookies())
            if token and _session_is_live(token):
                path = save_token(token)
                print(f"Sessão já estava viva. Token salvo em {path} ({token_preview(token)}).")
                return token

            _try_fill_credentials(page)

            while time.time() < deadline:
                token = _extract_token(context.cookies())
                url = page.url
                logged_in_url = any(hint in url for hint in HOME_HINTS) and "/login" not in url
                if token and (logged_in_url or _session_is_live(token)):
                    if _session_is_live(token):
                        path = save_token(token)
                        print(f"Login ok. Token salvo em {path} ({token_preview(token)}).")
                        print("Esse token vale pela sessão web (~7 dias). O bot reutiliza sem abrir o site de novo.")
                        return token
                time.sleep(1.2)

            raise TimeoutError(
                "Tempo esgotado esperando o login. Rode de novo: python -m kcex.cli login"
            )
        finally:
            context.close()


def require_live_token() -> str:
    from dotenv import load_dotenv

    load_dotenv()
    token = os.getenv("KCEX_TOKEN", "").strip()
    if token and _session_is_live(token):
        return token
    print("Sessão ausente ou expirada. Abrindo login no browser...")
    return login_interactive()
