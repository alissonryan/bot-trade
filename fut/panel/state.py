"""Turns the read-only database view into what the page shows. Pure over a PanelReader."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from fut.panel.cache import PanelCache
from fut.panel.narrate import narrate
from fut.panel.reader import PanelReader

MIN_ALIVE_MS = 10_000
OLD_PRICE_MS = 15_000
SERIES_MS = 2 * 3_600_000
MAX_TRADES = 50
FIRST_PAGE = 200
EVENT_PAGE = 500
# BTC_USDT contractSize from docs/kcex-futures-api.md. The panel never calls KCEX, so it cannot
# read ContractSpec; the position card is an estimate, the ledger fills are the record.
CONTRACT_SIZE = 0.0001
BOOKS = (("main", "Bot (Jev + LLM)"), ("shadow:jev_only", "Só o Jev (sem LLM)"), ("shadow:random", "Aleatório"))


@dataclass(frozen=True)
class StateData:
    """Database-backed facts retained while a later read is unavailable."""

    today: list[dict]
    balances: dict[str, float]
    fills_by_book: dict[str, list[dict]]
    position: dict[str, Any] | None


def alive_threshold_ms(jev_every_s: float) -> int:
    return int(max(MIN_ALIVE_MS, 5 * float(jev_every_s) * 1000))


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


def read_state_data(cache: PanelCache, *, now_ms: int) -> StateData:
    reader = cache.reader
    day = _day_of(now_ms)
    today = reader.fills("main", day=day)
    balances = reader.balances()
    fills_by_book = {book: reader.fills(book) for book, _ in BOOKS}
    return StateData(today, balances, fills_by_book, reader.position("main"))


def build_state_from_data(cache: PanelCache, data: StateData, *, now_ms: int, max_hold_s: float = 300.0,
                          jev_every_s: float = 2.0) -> dict[str, Any]:
    day = _day_of(now_ms)
    since = now_ms - SERIES_MS
    view = cache.view(day=day, since_ms=since)
    last_id, last_ts = view["last_id"], view["last_ts_ms"]
    price = _price(view["snapshot"], now_ms)
    gross = sum(f["pnl"] for f in data.today)
    fees = sum(f["fee"] for f in data.today)
    funding = sum(f["funding"] for f in data.today)
    board = []
    for book, name in BOOKS:
        net = _net(data.fills_by_book[book])
        if book == "main":
            net -= view["lifetime_costs"]["jev"] + view["lifetime_costs"]["llm"]
        board.append({"carteira": book, "nome": name, "saldo": data.balances.get(book),
                      "trades": len(_pair_trades(data.fills_by_book[book])), "liquido": net})
    marks = []
    for f in data.fills_by_book["main"]:
        if f["ts_ms"] < since or f["kind"] not in ("open", "close"):
            continue
        tipo = "saida" if f["kind"] == "close" else f"entrada_{f['side']}"
        marks.append({"ts_ms": f["ts_ms"], "preco": f["price"], "tipo": tipo})
    return {
        "estado": "ok",
        "agora_ms": now_ms,
        "carregando": view["loading"],
        "jev": view["jev"],
        "bot": {"vivo": bool(last_id) and now_ms - last_ts <= alive_threshold_ms(jev_every_s),
                "ultimo_sinal_s": (now_ms - last_ts) // 1000 if last_id else None},
        "preco": price,
        "posicao": _position(data.position, price, now_ms, max_hold_s),
        "dia": {"bruto": gross, "taxas": fees, "funding": funding, "custo_jev": view["day_costs"]["jev"],
                "custo_llm": view["day_costs"]["llm"],
                "liquido": gross - fees - funding - view["day_costs"]["jev"] - view["day_costs"]["llm"]},
        "placar": board,
        "trades": _pair_trades(data.fills_by_book["main"])[-MAX_TRADES:][::-1],
        "serie": {"pontos": view["price_series"], "marcas": marks},
    }


def build_state(cache: PanelCache, *, now_ms: int, max_hold_s: float = 300.0,
                jev_every_s: float = 2.0) -> dict[str, Any]:
    data = read_state_data(cache, now_ms=now_ms)
    return build_state_from_data(cache, data, now_ms=now_ms, max_hold_s=max_hold_s, jev_every_s=jev_every_s)


def build_events(reader: PanelReader, after_id: int | None) -> dict[str, Any]:
    # Read the high-water mark FIRST and bound the page by it: a row the bot inserts between
    # the two queries is picked up by the next poll instead of being skipped forever.
    last_id, _ = reader.last_decision()
    rows = reader.event_rows(after_id, last_id, FIRST_PAGE if after_id is None else EVENT_PAGE)
    if rows and after_id is not None and len(rows) == EVENT_PAGE:
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
