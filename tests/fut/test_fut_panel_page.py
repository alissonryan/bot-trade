import re
from pathlib import Path

PAGE = Path(__file__).resolve().parents[2] / "panel" / "index.html"


def test_page_is_self_contained_and_read_only():
    html = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)\s*=\s*["']https?://""", html)  # no CDN, works offline
    assert "/api/state" in html and "/api/events" in html
    assert "<form" not in html and "<button" not in html  # nothing to press
    assert 'lang="pt-BR"' in html


def test_page_escapes_feed_text():
    html = PAGE.read_text(encoding="utf-8")
    assert "innerHTML" not in html  # LLM reasons are untrusted text: textContent only


def test_page_distinguishes_a_recent_silent_market_from_a_stopped_bot():
    html = PAGE.read_text(encoding="utf-8")
    assert "SEM SINAL DO BOT há" in html
    assert "se o mercado estiver parado isso é normal" in html
    assert "se passar de alguns minutos, confira o terminal" in html
    assert "> 120" in html


def test_page_handles_null_position_levels_and_sustained_database_failures():
    html = PAGE.read_text(encoding="utf-8")
    assert 'p.stop == null ? "—"' in html and 'p.liq == null ? "—"' in html
    assert "stateFailures >= 5" in html
    assert "Painel sem resposta do banco — tentando de novo…" in html
    assert "stateFailures = 0" in html
