"""Turns the read-only database view into what the page shows. Pure over a PanelReader."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fut.panel.cache import PanelCache
from fut.panel.narrate import narrate
from fut.panel.reader import PanelReader

ALIVE_MS = 10_000        # the bot logs a Jev row every ~2.5 s; 10 s of silence means it stopped
OLD_PRICE_MS = 15_000
SERIES_MS = 2 * 3_600_000
MAX_TRADES = 50
FIRST_PAGE = 200
# BTC_USDT contractSize from docs/kcex-futures-api.md. The panel never calls KCEX, so it cannot
# read ContractSpec; the position card is an estimate, the ledger fills are the record.
CONTRACT_SIZE = 0.0001
BOOKS = (("main", "Bot (Jev + LLM)"), ("shadow:jev_only", "Só o Jev (sem LLM)"), ("shadow:random", "Aleatório"))


def _day_of(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def _day_bounds_ms(day: str) -> tuple[int, int]:
    start = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return start, start + 86_400_000


def _net(fills: list[dict]) -> float:
    return sum(f["pnl"] - f["fee"] - f["funding"] for f in fills)


def _pair_trades(fills: list[dict]) -> list[dict[str, Any]]:
    trades, current = [], None
    for f in fills:
        if f["kind"] == "open":
            current = {"entrada_ms": f["ts_ms"], "lado": f["side"], "entrada": f["price"], "custos": f["fee"]}
        elif f["kind"] == "funding" and current is not None:
            current["custos"] += f["funding"]
        elif f["kind"] == "close" and current is not None:
            costs = current.pop("custos") + f["fee"]
            current.update(saida_ms=f["ts_ms"], saida=f["price"], motivo=f["reason"],
                           duracao_s=(f["ts_ms"] - current["entrada_ms"]) // 1000,
                           liquido_usd=f["pnl"] - costs)
            trades.append(current)
            current = None
    return trades


def _price(snap: dict | None, now_ms: int) -> dict[str, Any] | None:
    if not snap:
        return None
    bid, ask, last = float(snap.get("bid") or 0), float(snap.get("ask") or 0), float(snap.get("last") or 0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    if mid <= 0:
        return None
    ts = int(snap.get("ts_ms") or 0)
    return {"mid": mid, "bid": bid, "ask": ask, "spread_bps": float(snap.get("spread_bps") or 0.0), "ts_ms": ts,
            "velho": bool(snap.get("stale")) or now_ms - ts > OLD_PRICE_MS}


def _position(pos: dict | None, price: dict | None, now_ms: int, max_hold_s: float) -> dict[str, Any] | None:
    if not pos:
        return None
    open_s = max(0, (now_ms - int(pos["opened_ms"])) // 1000)
    out = {"lado": pos["side"], "entrada": pos["entry"], "stop": pos["stop"], "liq": pos["liq"],
           "contratos": pos["contracts"], "aberto_ha_s": open_s,
           "fecha_em_s": max(0, int(max_hold_s) - open_s), "resultado_bps": None, "resultado_usd": None}
    if price and pos["entry"]:
        sign = 1 if pos["side"] == "long" else -1
        move = sign * (price["mid"] - pos["entry"])
        out["resultado_usd"] = move * pos["contracts"] * CONTRACT_SIZE
        out["resultado_bps"] = move / pos["entry"] * 10_000
    return out


def build_state(cache: PanelCache, *, now_ms: int, max_hold_s: float = 300.0) -> dict[str, Any]:
    reader = cache.reader
    last_id, last_ts = cache.last_id, cache.last_ts_ms
    price = _price(cache.snapshot, now_ms)
    day = _day_of(now_ms)
    today = reader.fills("main", day=day)
    costs = cache.costs_for_day(day)
    gross = sum(f["pnl"] for f in today)
    fees = sum(f["fee"] for f in today)
    funding = sum(f["funding"] for f in today)
    balances = reader.balances()
    all_costs = cache.lifetime_costs
    fills_by_book = {book: reader.fills(book) for book, _ in BOOKS}
    board = []
    for book, name in BOOKS:
        net = _net(fills_by_book[book])
        if book == "main":
            net -= all_costs["jev"] + all_costs["llm"]
        board.append({"carteira": book, "nome": name, "saldo": balances.get(book),
                      "trades": len(_pair_trades(fills_by_book[book])), "liquido": net})
    since = now_ms - SERIES_MS
    marks = []
    for f in fills_by_book["main"]:
        if f["ts_ms"] < since or f["kind"] not in ("open", "close"):
            continue
        tipo = "saida" if f["kind"] == "close" else f"entrada_{f['side']}"
        marks.append({"ts_ms": f["ts_ms"], "preco": f["price"], "tipo": tipo})
    return {
        "estado": "ok",
        "agora_ms": now_ms,
        "carregando": cache.loading,
        "bot": {"vivo": bool(last_id) and now_ms - last_ts <= ALIVE_MS,
                "ultimo_sinal_s": (now_ms - last_ts) // 1000 if last_id else None},
        "preco": price,
        "posicao": _position(reader.position("main"), price, now_ms, max_hold_s),
        "dia": {"bruto": gross, "taxas": fees, "funding": funding, "custo_jev": costs["jev"],
                "custo_llm": costs["llm"], "liquido": gross - fees - funding - costs["jev"] - costs["llm"]},
        "placar": board,
        "trades": _pair_trades(fills_by_book["main"])[-MAX_TRADES:][::-1],
        "serie": {"pontos": cache.price_series(since), "marcas": marks},
    }


def build_events(reader: PanelReader, after_id: int | None) -> dict[str, Any]:
    # Read the high-water mark FIRST and bound the page by it: a row the bot inserts between
    # the two queries is picked up by the next poll instead of being skipped forever.
    last_id, _ = reader.last_decision()
    rows = reader.event_rows(after_id, last_id, FIRST_PAGE if after_id is None else 500)
    if rows and after_id is not None and len(rows) == 500:
        last_id = rows[-1]["id"]  # a full page: continue from its end on the next poll
    closes = {}
    if rows:
        closes = {trade["saida_ms"]: trade["liquido_usd"] for trade in _pair_trades(reader.fills("main"))}
    events = []
    for row in rows:
        closing = row["kind"] == "exit" or (row["kind"] == "llm" and row["payload"].get("outcome") == "closed")
        try:
            event = narrate(row, closes.get(row["ts_ms"]) if closing else None)
        except Exception:
            continue
        if event is not None:
            events.append(event)
    return {"events": events, "last_id": last_id}
