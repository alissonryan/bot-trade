import pytest

import fut.panel.narrate as narrate_module
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


def test_observational_jev_ab_rows_are_silent_but_unknown_kinds_stay_visible():
    assert narrate_module.SILENT_KINDS == {"jev_ab"}
    assert narrate(row("jev_ab", {"variant": "label-a", "cost_usd": 0.1})) is None
    assert narrate(row("brand_new_kind", {}))["texto"] == "Evento brand_new_kind"


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
    (dict(gate="atr"), "info", ["sem medida de volatilidade"]),
    (dict(error="TimeoutError: slow"), "alerta", ["demorou demais"]),
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
    win = narrate(row("exit", {"reason": "time_limit"}), 0.048)
    assert "+0,0480 USD" in win["texto"] and win["tom"] == "bom"
    loss = narrate(llm(intent={"action": "CLOSE", "confidence": 1, "reason": "r"}, outcome="closed"),
                   -0.032)
    assert "-0,0320 USD" in loss["texto"] and loss["tom"] == "ruim"
    liq = narrate(row("exit", {"reason": "liquidation"}), -15.0)
    assert liq["tom"] == "alerta"  # an alert stays an alert


def test_unmonitored_and_unknown_and_malformed_rows():
    assert narrate(row("unmonitored", {"silent_ms": 61000}))["tom"] == "alerta"
    assert narrate(row("brand_new_kind", {})) == {"id": 7, "ts_ms": 1000, "tipo": "evento", "tom": "info",
                                                    "texto": "Evento brand_new_kind", "grupo": None}
    assert narrate(row("llm", {})) is not None  # empty payload: "não respondeu", never raises
    assert narrate({"id": 1, "ts_ms": 1, "kind": "jev", "payload": {"wake": "entry_signal", "answers": "oops"}})


@pytest.mark.parametrize("error, expected", [
    ("TypeSafeInternalServerError: POST https://api.typesafe.ai/v1/systemone: 529 We a…",
     "Jev sobrecarregado (servidor da TypeSafe com excesso de demanda)"),
    ("TypeSafeInternalServerError: POST https://api.typesafe.ai/v1/systemone: 503 The model is unavailable. "
     "If this issue persists, please contact support. (request_id=req_01a0b0ba5c987a32965d338a6decfbbb)",
     "Jev fora do ar (servidor da TypeSafe indisponível)"),
    ("TypeSafeAPITimeoutError: Request timed out (timeout=2.0).", "Jev demorou demais para responder"),
    ("request timed out", "Jev demorou demais para responder"),
    ("429 rate limit request_id=req-123", "Jev recusou por limite de uso"),
    ("403 forbidden request_id=req-123", "Jev recusou a chave de acesso"),
    ("TypeSafeThingError: POST https://api.typesafe.ai/v1/systemone: 418 unusual request_id=req_123",
     "Jev falhou (POST 418 unusual)"),
])
def test_jev_errors_are_short_plain_and_grouped(error, expected):
    event = narrate(jev(error=error))
    assert event["texto"] == expected
    assert "http" not in event["texto"] and "request_id" not in event["texto"] and "req_" not in event["texto"]
    assert event["grupo"] == "jev_erro:" + expected


@pytest.mark.parametrize("error, expected", [
    ("500 Internal Server Error", "Jev falhou (500 Internal Server Error)"),
    ("502 Bad Gateway", "Jev falhou (502 Bad Gateway)"),
    ("504 Gateway Failure", "Jev falhou (504 Gateway Failure)"),
    ("TypeSafeAPIOError: connect failed", "Jev sem conexão"),
])
def test_jev_server_and_connection_error_families_are_localized(error, expected):
    assert narrate(jev(error=error))["texto"] == expected


@pytest.mark.parametrize("error, expected", [
    ("Request timed out (timeout=500)", "Jev demorou demais para responder"),
    ("latency_ms=512", "Jev falhou"),
    ("attempts=503", "Jev falhou"),
])
def test_jev_error_numbers_in_metadata_do_not_become_server_errors(error, expected):
    assert narrate(jev(error=error))["texto"] == expected


def test_only_sdk_status_after_a_colon_is_a_generic_server_error():
    assert narrate(jev(error="TypeSafeInternalServerError: POST https://api.typesafe.ai/v1/systemone: 502 Bad Gateway"))["texto"] == "Jev com erro no servidor da TypeSafe"


def test_fallback_keeps_safe_text_only():
    event = narrate(jev(error="weird failure with token=abc123 and https://x.io/y"))
    assert event["texto"] == "Jev falhou (weird failure with and)"
    assert "=" not in event["texto"] and "://" not in event["texto"]


def test_non_error_events_have_no_group():
    assert narrate(jev(wake="exit_signal"))["grupo"] is None


@pytest.mark.parametrize("bad_row, close_fill", [
    (jev(wake="entry_signal", gate={}), None),
    (jev(wake="entry_signal", dispatch=[]), None),
    (jev(wake="entry_signal", answers={"direction": []}), None),
    (row("exit", {"reason": []}), None),
    (jev(wake="entry_signal", gate=[]), None),
    (llm(intent={"action": "CLOSE", "reason": "r"}, outcome="closed"), {"pnl": "abc", "fee": 0.002}),
])
def test_unhashable_and_non_numeric_values_never_raise(bad_row, close_fill):
    result = narrate(bad_row, close_fill)
    assert isinstance(result, (dict, type(None)))
