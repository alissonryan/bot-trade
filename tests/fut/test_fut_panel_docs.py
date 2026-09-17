from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_panel_docs_describe_incremental_shared_refresh_and_possible_contention():
    reader_doc = (ROOT / "fut" / "panel" / "reader.py").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    panel_docs = claude + "\n" + agents
    assert "bounded incremental" in reader_doc
    assert "one refresher" in panel_docs and "all browser tabs" in panel_docs
    assert "can still contend" in panel_docs
    assert "lock-free" not in panel_docs
    assert "Evento <kind>" in agents
    assert "SILENT_KINDS" in agents
    assert "shows raw" not in agents


def test_panel_docs_match_cached_state_and_error_contract():
    spec = (ROOT / "docs" / "superpowers" / "specs" / "2026-09-17-fut-panel-design.md").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "fut/panel/cache.py" in spec and "decision_facts" in spec
    assert "estado\": \"carregando\"" in spec and "estado\": \"banco_invalido\"" in spec
    assert "estado\": \"erro_painel\"" in spec and "bytes cacheados" in spec
    assert "503" in spec and "/api/events" in spec
    assert "FUT_JEV_EVERY_SECONDS" in spec and "max(10 s" in spec
    assert "carregando" in claude and "carregando" in agents
    assert "banco_ocupado_desde_ms" in spec
    assert "mostrando a última leitura" in spec


def test_panel_docs_describe_jev_error_translation_and_health():
    spec = (ROOT / "docs" / "superpowers" / "specs" / "2026-09-17-fut-panel-design.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Jev com erro no servidor da TypeSafe" in spec and "request_id" not in spec
    assert "Jev sobrecarregado" in spec and "Jev com erro no servidor da TypeSafe" in spec
    assert "Jev sem conexão" in spec and "https?://" in spec
    assert "falhas_seguidas" in spec and "jev_erro:" in spec
    assert "jev_ab" in spec and "SILENT_KINDS" in spec
    assert "de probabilidade" in spec and "confiança" in spec
    assert "The state also exposes Jev health" in agents
