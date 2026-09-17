"""One database row in, one plain-Portuguese event out. All layman vocabulary lives here.

Pure and total: unknown event kinds get a generic label, and malformed values never escape as exceptions.
"""

from __future__ import annotations

from typing import Any

DISPATCH = {
    "dispatched": "perguntando à LLM…",
    "suppressed_inflight": "LLM ainda ocupada com a pergunta anterior",
    "suppressed_cooldown": "a LLM acabou de dizer para esperar",
    "suppressed_budget": "o orçamento de IA do dia acabou",
}
WAKE_GATES = {
    "spread_too_wide": "spread alto demais",
    "move_lt_cost": "o movimento esperado não paga o custo",
    "atr": "sem medida de volatilidade",
}
COLLAR_RULES = {
    "stale": "preço desatualizado", "contract_state": "contrato fora de negociação",
    "already_open": "já existe posição aberta", "day_loss": "perda máxima do dia atingida",
    "spread_too_wide": "spread alto demais", "move_lt_cost": "o movimento esperado não paga o custo",
    "entry_rate": "limite de entradas por hora", "confidence": "confiança da LLM baixa demais",
    "atr": "sem medida de volatilidade", "leverage": "alavancagem fora do limite",
    "no_price": "sem preço executável", "no_cash": "saldo insuficiente", "dust": "tamanho abaixo do mínimo",
    "liq_too_close": "stop perto demais da liquidação", "flat": "não havia posição para fechar",
}
EXITS = {
    "stop": ("SAIU por STOP (o preço bateu no limite de perda)", "ruim"),
    "time_limit": ("SAIU por TEMPO MÁXIMO", "info"),
    "liquidation": ("SAIU por LIQUIDAÇÃO (perdeu a margem inteira)", "alerta"),
    "funding": ("Taxa de funding cobrada/recebida", "info"),
}


def fmt_price(value: Any) -> str:
    try:
        text = f"{float(value):,.1f}"
    except (TypeError, ValueError):
        return "?"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_usd(value: float) -> str:
    return f"{value:+.4f} USD".replace(".", ",")


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _quote(reason: Any) -> str:
    reason = str(reason or "").strip()
    return f" — “{reason}”" if reason else ""


def _jev(p: dict) -> tuple[str, str] | None:
    if p.get("error"):
        return f"Jev falhou ({p['error']})", "alerta"
    answers = _d(p.get("answers"))
    if p.get("gate"):
        gate = str(p.get("gate"))
        return f"Jev viu sinal, mas foi ignorado: {WAKE_GATES.get(gate, gate)}", "info"
    wake = p.get("wake")
    if not wake:
        return None
    dispatch = p.get("dispatch")
    dispatch_key = str(dispatch) if dispatch else ""
    suffix = DISPATCH.get(dispatch_key, dispatch_key)
    tone = "alerta" if dispatch_key == "suppressed_budget" else "info"
    if wake == "entry_signal":
        direction = {"up": "ALTA", "down": "QUEDA"}.get(str(answers.get("direction")), "MOVIMENTO")
        try:
            pct = f" ({float(answers.get('direction_conf')) * 100:.0f}%)"
        except (TypeError, ValueError):
            pct = ""
        head = f"Jev viu chance de {direction}{pct}"
    elif wake == "exit_signal":
        head = "Jev acha que é hora de sair"
    elif wake == "reversal_signal":
        head = "Jev virou contra a posição"
    else:
        head = f"Jev acordou ({wake})"
    return (f"{head} → {suffix}" if suffix else head), tone


def _llm(p: dict) -> tuple[str, str]:
    intent, outcome = _d(p.get("intent")), str(p.get("outcome") or "")
    if not intent:
        return f"LLM não respondeu direito ({_d(p.get('llm')).get('reason') or 'sem motivo'})", "alerta"
    why = _quote(intent.get("reason"))
    if p.get("verdict") == "stale_timeout":
        return "LLM quis entrar, mas a resposta chegou tarde demais — entrada descartada", "info"
    if p.get("verdict") == "stale_price":
        return "LLM quis entrar, mas o preço já tinha andado — entrada descartada", "info"
    if outcome == "opened":
        gate = _d(p.get("gate"))
        side = "COMPRADO" if gate.get("side") == "long" else "VENDIDO"
        return f"ENTROU {side} a {fmt_price(gate.get('price'))} · stop {fmt_price(gate.get('stop'))}{why}", "info"
    if outcome == "closed":
        return f"LLM mandou FECHAR a posição{why}", "info"
    if outcome == "close_no_price":
        return "LLM mandou fechar, mas sem preço; o bot tenta de novo", "alerta"
    if outcome.startswith("gate_"):
        rule = outcome[len("gate_"):]
        return f"Entrada barrada pela trava de risco: {COLLAR_RULES.get(rule, rule)}", "alerta"
    if intent.get("action") == "HOLD":
        return f"LLM decidiu ESPERAR{why}", "info"
    return f"LLM respondeu {intent.get('action')} ({outcome}){why}", "info"


def narrate(row: dict, net_usd: float | None = None) -> dict | None:
    kind, p = row.get("kind"), _d(row.get("payload"))
    if kind == "jev":
        told, tipo = _jev(p), "jev"
    elif kind == "llm":
        told, tipo = _llm(p), "llm"
    elif kind == "exit":
        reason = p.get("reason")
        reason_key = str(reason) if reason else ""
        told, tipo = EXITS.get(reason_key, (f"SAIU ({reason_key})", "info")), "saida"
    elif kind == "unmonitored":
        told, tipo = ("BOT PAROU: posição aberta ficou sem preço por tempo demais", "alerta"), "alerta"
    else:
        return {"id": row.get("id"), "ts_ms": row.get("ts_ms"), "tipo": "evento", "tom": "info",
                "texto": f"Evento {str(kind)}"}
    if told is None:
        return None
    text, tone = told
    if net_usd is not None:
        try:
            net = float(net_usd)
        except (TypeError, ValueError):
            net = None
        if net is not None:
            text = f"{text} · resultado {fmt_usd(net)}"
            if tone != "alerta":
                tone = "bom" if net > 0 else "ruim"
    return {"id": row.get("id"), "ts_ms": row.get("ts_ms"), "tipo": tipo, "tom": tone, "texto": text}
