import pytest

from fut.panel.narrate import fmt_price, fmt_usd, narrate


def row(kind, payload, id=7, ts_ms=1000):
    return {"id": id, "ts_ms": ts_ms, "kind": kind, "payload": payload}


def jev(**kw):
    base = {"error": None, "wake": None, "dispatch": None, "gate": None,
            "answers": {"direction": "up", "direction_conf": 0.72}}
    base.update(kw)
    return row("jev", base)


def llm(**kw):
    base = {"intent": {"action": "HOLD", "confidence": 0.5, "reason": "fluxo fraco"}, "verdict": "ok",
            "outcome": "ok", "gate": None, "llm": {"reason": "ok"}}
    base.update(kw)
    return row("llm", base)


def test_formatters_use_brazilian_separators():
    assert fmt_price(76840.56) == "76.840,6"
    assert fmt_usd(0.01234) == "+0,0123 USD" and fmt_usd(-1.5) == "-1,5000 USD"


def test_quiet_jev_row_is_not_an_event():
    assert narrate(jev()) is None


@pytest.mark.parametrize("payload, tom, parts", [
    (dict(wake="entry_signal", dispatch="dispatched"), "info", ["ALTA", "72%", "perguntando à LLM"]),
    (dict(wake="entry_signal", dispatch="suppressed_inflight", answers={"direction": "down", "direction_conf": 0.6}),
     "info", ["QUEDA", "60%", "LLM ainda ocupada"]),
    (dict(wake="entry_signal", dispatch="suppressed_cooldown"), "info", ["acabou de dizer para esperar"]),
    (dict(wake="entry_signal", dispatch="suppressed_budget"), "alerta", ["orçamento de IA"]),
    (dict(wake="exit_signal", dispatch="dispatched"), "info", ["hora de sair"]),
    (dict(wake="reversal_signal", dispatch="dispatched"), "info", ["virou contra a posição"]),
    (dict(gate="spread_too_wide"), "info", ["ignorado", "spread"]),
    (dict(gate="move_lt_cost"), "info", ["ignorado", "não paga o custo"]),
    (dict(error="TimeoutError: slow"), "alerta", ["Jev falhou", "TimeoutError"]),
])
def test_jev_rows(payload, tom, parts):
    event = narrate(jev(**payload))
    assert event["tipo"] == "jev" and event["tom"] == tom and event["id"] == 7 and event["ts_ms"] == 1000
    for part in parts:
        assert part in event["texto"]


@pytest.mark.parametrize("payload, tom, parts", [
    (dict(), "info", ["ESPERAR", "fluxo fraco"]),
    (dict(intent={"action": "LONG", "confidence": 0.6, "reason": "fluxo forte"}, outcome="opened",
          gate={"side": "long", "price": 76840.56, "stop": 76741.9}), "info",
     ["ENTROU COMPRADO", "76.840,6", "stop 76.741,9", "fluxo forte"]),
    (dict(intent={"action": "SHORT", "confidence": 0.6, "reason": "r"}, outcome="opened",
          gate={"side": "short", "price": 76000.0, "stop": 76100.0}), "info", ["ENTROU VENDIDO"]),
    (dict(intent={"action": "CLOSE", "confidence": 0.6, "reason": "virou"}, outcome="closed"), "info",
     ["mandou FECHAR", "virou"]),
    (dict(intent={"action": "CLOSE", "confidence": 0.6, "reason": "r"}, outcome="close_no_price"), "alerta",
     ["sem preço"]),
    (dict(intent={"action": "LONG", "confidence": 0.6, "reason": "r"}, verdict="stale_timeout",
          outcome="stale_timeout"), "info", ["chegou tarde", "descartada"]),
    (dict(intent={"action": "LONG", "confidence": 0.6, "reason": "r"}, verdict="stale_price",
          outcome="stale_price"), "info", ["preço já tinha andado", "descartada"]),
    (dict(intent={"action": "LONG", "confidence": 0.6, "reason": "r"}, outcome="gate_day_loss"), "alerta",
     ["trava de risco", "perda máxima do dia"]),
    (dict(intent={"action": "LONG", "confidence": 0.6, "reason": "r"}, outcome="gate_something_new"), "alerta",
     ["trava de risco", "something_new"]),
    (dict(intent=None, llm={"reason": "llm_timeout"}), "alerta", ["não respondeu", "llm_timeout"]),
])
def test_llm_rows(payload, tom, parts):
    event = narrate(llm(**payload))
    assert event["tipo"] == "llm" and event["tom"] == tom
    for part in parts:
        assert part in event["texto"]


@pytest.mark.parametrize("reason, tom, part", [
    ("stop", "ruim", "STOP"), ("time_limit", "info", "TEMPO MÁXIMO"),
    ("liquidation", "alerta", "LIQUIDAÇÃO"), ("funding", "info", "funding"), ("weird", "info", "weird"),
])
def test_exit_rows(reason, tom, part):
    event = narrate(row("exit", {"reason": reason}))
    assert event["tipo"] == "saida" and event["tom"] == tom and part in event["texto"]


def test_close_fill_adds_the_money_result_and_sets_the_tone():
    win = narrate(row("exit", {"reason": "time_limit"}), {"pnl": 0.05, "fee": 0.002})
    assert "+0,0480 USD" in win["texto"] and win["tom"] == "bom"
    loss = narrate(llm(intent={"action": "CLOSE", "confidence": 1, "reason": "r"}, outcome="closed"),
                   {"pnl": -0.03, "fee": 0.002})
    assert "-0,0320 USD" in loss["texto"] and loss["tom"] == "ruim"
    liq = narrate(row("exit", {"reason": "liquidation"}), {"pnl": -15.0, "fee": 0.0})
    assert liq["tom"] == "alerta"  # an alert stays an alert


def test_unmonitored_and_unknown_and_malformed_rows():
    assert narrate(row("unmonitored", {"silent_ms": 61000}))["tom"] == "alerta"
    assert narrate(row("brand_new_kind", {})) is None
    assert narrate(row("llm", {})) is not None  # empty payload: "não respondeu", never raises
    assert narrate({"id": 1, "ts_ms": 1, "kind": "jev", "payload": {"wake": "entry_signal", "answers": "oops"}})
