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
