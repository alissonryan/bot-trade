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


def test_page_appends_observed_cadence_on_the_alive_banner():
    html = PAGE.read_text(encoding="utf-8")
    assert "o bot avalia a cada ~" in html
    assert "s.bot.cadencia_s" in html
    assert "● BOT RODANDO — último sinal há" in html


def test_page_handles_null_position_levels_and_sustained_database_failures():
    html = PAGE.read_text(encoding="utf-8")
    assert 'p.stop == null ? "—"' in html and 'p.liq == null ? "—"' in html
    assert "stateFailures >= 5" in html
    assert "Painel sem resposta do banco — tentando de novo…" in html
    assert "stateFailures = 0" in html


def test_page_handles_loading_state_without_blocking_the_feed():
    html = PAGE.read_text(encoding="utf-8")
    assert 's.estado === "carregando"' in html
    assert "Carregando histórico do bot…" in html
    assert "carregando histórico — os totais ainda estão incompletos" in html
    assert "try { renderState(s); }" in html
    assert "try { renderEvents(e); }" in html


def test_page_rejects_stale_state_and_handles_panel_errors():
    html = PAGE.read_text(encoding="utf-8")
    assert '"Painel travado: dados de " + hora(s.agora_ms)' in html
    assert '" — reinicie o python -m fut panel"' in html
    assert "Math.abs(Date.now() - s.agora_ms) > 10000" in html
    assert 's.estado === "erro_painel"' in html
    assert "Erro no painel — tentando de novo…" in html


def test_page_marks_a_busy_database_without_calling_it_stuck():
    html = PAGE.read_text(encoding="utf-8")
    assert "banco_ocupado" in html
    assert 'banco ocupado desde " + hora(s.banco_ocupado_desde_ms)' in html
    assert "mostrando a última leitura" in html
    assert "o banco não responde há mais de 1 min — confira se o bot está rodando" in html
    assert "s.agora_ms - s.banco_ocupado_desde_ms > 60000" in html


def test_page_dims_cards_for_panel_errors_and_restores_them_on_success():
    html = PAGE.read_text(encoding="utf-8")
    assert ".dados-desatualizados" in html
    assert "ultimo_ok_ms" in html
    assert '"dados de " + hora(s.ultimo_ok_ms)' in html
    assert "classList.add(\"dados-desatualizados\")" in html
    assert "classList.remove(\"dados-desatualizados\")" in html


def test_page_collapses_repeated_jev_errors_and_shows_health_warning():
    html = PAGE.read_text(encoding="utf-8")
    assert "data-grupo" in html and "vezes seguidas" in html
    assert "firstElementChild" in html
    assert "falhas_seguidas >= 3" in html
    assert '" desde " + hora(s.jev.desde_ms).slice(0, 5)' in html
    assert "Jev fora do ar" in html
    assert "o bot continua protegendo posições abertas" in html


def test_page_makes_the_jev_health_warning_position_agnostic():
    html = PAGE.read_text(encoding="utf-8")
    assert "o bot não está avaliando novas entradas (não há posição aberta)." in html
    assert "const jevPositionNote = s.posicao" in html
    assert "o bot continua protegendo posições abertas (stop e tempo máximo)" in html


def test_page_clears_card_dimming_before_any_state_early_return():
    html = PAGE.read_text(encoding="utf-8")
    cards_at = html.index("const cards")
    first_return_at = html.index('if (s.estado === "sem_banco")')
    assert cards_at < first_return_at
    assert 's.estado === "erro_painel" || s.banco_ocupado' in html
    assert "card.classList.remove(\"dados-desatualizados\")" in html
