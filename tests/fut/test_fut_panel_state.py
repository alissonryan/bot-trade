import pytest

from fut.panel.reader import PanelReader
from fut.panel.state import build_events, build_state
from tests.fut.panel_db import SNAP, add_decision, add_fill, make_db, set_balance, set_position

DAY = 86_400_000
T0 = 20_000 * DAY  # a UTC midnight


def jev_row(conn, ts, bid, ask, **extra):
    payload = {"wake": None, "error": None, "gate": None, "cost_usd": 0.001, "snapshot": SNAP(ts_ms=ts, bid=bid, ask=ask)}
    payload.update(extra)
    return add_decision(conn, ts, "jev", payload)


def test_empty_database_gives_a_calm_state(tmp_path):
    db = tmp_path / "fut.db"
    make_db(db).close()
    state = build_state(PanelReader(db), now_ms=T0)
    assert state["estado"] == "ok" and state["bot"]["vivo"] is False and state["bot"]["ultimo_sinal_s"] is None
    assert state["preco"] is None and state["posicao"] is None and state["trades"] == []
    assert state["dia"]["liquido"] == 0.0


def test_alive_price_and_long_position_marked_to_mid(tmp_path):
    db = tmp_path / "fut.db"
    conn = make_db(db)
    jev_row(conn, T0 + 100_000, 76100.0, 76100.2)
    set_position(conn, "main", side="long", contracts=2, entry=76000.0, stop=75900.0, opened_ms=T0 + 40_000)
    state = build_state(PanelReader(db), now_ms=T0 + 104_000, max_hold_s=300.0)
    assert state["bot"] == {"vivo": True, "ultimo_sinal_s": 4}
    assert state["preco"]["mid"] == pytest.approx(76100.1) and state["preco"]["velho"] is False
    pos = state["posicao"]
    assert pos["lado"] == "long" and pos["aberto_ha_s"] == 64 and pos["fecha_em_s"] == 236
    assert pos["resultado_usd"] == pytest.approx((76100.1 - 76000.0) * 2 * 0.0001)
    assert pos["resultado_bps"] == pytest.approx((76100.1 - 76000.0) / 76000.0 * 10_000)


def test_short_position_profits_when_price_falls_and_stale_bot_is_flagged(tmp_path):
    db = tmp_path / "fut.db"
    conn = make_db(db)
    jev_row(conn, T0, 75900.0, 75900.2)
    set_position(conn, "main", side="short", entry=76000.0, stop=76100.0, opened_ms=T0)
    state = build_state(PanelReader(db), now_ms=T0 + 400_000)
    assert state["bot"]["vivo"] is False and state["preco"]["velho"] is True
    assert state["posicao"]["resultado_usd"] > 0 and state["posicao"]["fecha_em_s"] == 0


def test_day_result_separates_model_costs_and_ignores_yesterday(tmp_path):
    db = tmp_path / "fut.db"
    conn = make_db(db)
    add_fill(conn, "main", T0 - 5000, "close", pnl=-9.0, reason="stop")  # yesterday (UTC)
    jev_row(conn, T0 - 5000, 1.0, 1.2, cost_usd=7.0)                      # yesterday
    add_fill(conn, "main", T0 + 1000, "open", fee=0.002)
    add_fill(conn, "main", T0 + 2000, "funding", funding=0.001, reason="funding")
    add_fill(conn, "main", T0 + 3000, "close", pnl=0.05, fee=0.002, reason="time_limit")
    jev_row(conn, T0 + 1000, 76000.0, 76000.2, cost_usd=0.01)
    add_decision(conn, T0 + 1500, "llm", {"outcome": "ok", "cost_usd": 0.02})
    dia = build_state(PanelReader(db), now_ms=T0 + 4000)["dia"]
    assert dia["bruto"] == pytest.approx(0.05) and dia["taxas"] == pytest.approx(0.004)
    assert dia["funding"] == pytest.approx(0.001)
    assert dia["custo_jev"] == pytest.approx(0.01) and dia["custo_llm"] == pytest.approx(0.02)
    assert dia["liquido"] == pytest.approx(0.05 - 0.004 - 0.001 - 0.03)


def test_trades_scoreboard_and_chart_marks(tmp_path):
    db = tmp_path / "fut.db"
    conn = make_db(db)
    jev_row(conn, T0 + 1000, 76000.0, 76000.2)
    add_fill(conn, "main", T0 + 1000, "open", side="short", price=76000.0, fee=0.002)
    add_fill(conn, "main", T0 + 61_000, "close", side="short", price=75950.0, fee=0.002, pnl=0.01, reason="llm_close")
    add_fill(conn, "main", T0 + 70_000, "open", side="long", price=75960.0, fee=0.002)  # still open: not a trade
    add_fill(conn, "shadow:random", T0 + 1000, "open", fee=0.002)
    add_fill(conn, "shadow:random", T0 + 2000, "close", fee=0.002, pnl=-0.02, reason="stop")
    set_balance(conn, "main", 450.004)
    set_balance(conn, "shadow:random", 449.976)
    state = build_state(PanelReader(db), now_ms=T0 + 80_000)
    (trade,) = state["trades"]
    assert trade == {"entrada_ms": T0 + 1000, "saida_ms": T0 + 61_000, "lado": "short", "entrada": 76000.0,
                     "saida": 75950.0, "motivo": "llm_close", "duracao_s": 60,
                     "liquido_usd": pytest.approx(0.01 - 0.004)}
    placar = {p["carteira"]: p for p in state["placar"]}
    assert list(placar) == ["main", "shadow:jev_only", "shadow:random"]
    assert placar["main"]["trades"] == 1 and placar["main"]["saldo"] == 450.004
    assert placar["shadow:random"]["liquido"] == pytest.approx(-0.024)
    assert placar["shadow:jev_only"] == {"carteira": "shadow:jev_only", "nome": "Só o Jev (sem LLM)",
                                          "saldo": None, "trades": 0, "liquido": 0.0}
    assert [m["tipo"] for m in state["serie"]["marcas"]] == ["entrada_short", "saida", "entrada_long"]
    assert state["serie"]["pontos"] == [[T0 + 1000, pytest.approx(76000.1)]]


def test_events_are_narrated_paged_and_carry_the_close_result(tmp_path):
    db = tmp_path / "fut.db"
    conn = make_db(db)
    jev_row(conn, T0, 76000.0, 76000.2)  # quiet: never an event
    wake = jev_row(conn, T0 + 2000, 76000.0, 76000.2, wake="entry_signal", dispatch="dispatched",
                   answers={"direction": "up", "direction_conf": 0.7})
    close = add_decision(conn, T0 + 9000, "exit", {"reason": "stop"})
    add_fill(conn, "main", T0 + 9000, "close", pnl=-0.03, fee=0.002, reason="stop")
    quiet_tail = jev_row(conn, T0 + 11_000, 76000.0, 76000.2)
    reader = PanelReader(db)
    first = build_events(reader, None)
    assert [e["id"] for e in first["events"]] == [wake, close] and first["last_id"] == quiet_tail
    assert "-0,0320 USD" in first["events"][1]["texto"] and first["events"][1]["tom"] == "ruim"
    assert build_events(reader, quiet_tail) == {"events": [], "last_id": quiet_tail}
    assert [e["id"] for e in build_events(reader, wake)["events"]] == [close]
