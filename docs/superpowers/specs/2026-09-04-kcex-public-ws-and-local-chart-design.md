# KCEX public WS + local chart — design

Date: 2026-09-04  
Status: draft for user review  
Scope: v1. One KCEX public socket, bot Eye consumes it, localhost republishes ticks and a chart. BTC/USDT only.

This spec records the approved brainstorm (option C: bot + our WS/chart). Do not implement until this file is approved.

## Goal

1. Stop polling ticker/depth every second when the public KCEX socket is healthy.
2. Keep one connection to KCEX for the whole process.
3. Republish the same snapshot on a **localhost** WebSocket we own.
4. Serve a local chart: REST kline history + live last price from our WS.

Not a goal of v1: TradingView, public internet bind, auth on the local WS, extra pairs, private `ws_token` streams, placing orders from the chart, kline WebSocket (KCEX chart itself uses REST kline).

## Locked decisions

| Topic | Choice |
| --- | --- |
| Process | **One process.** `python -m bot run --chart` (chart on). Without `--chart`, Eye still uses KCEX public WS. |
| KCEX URL | `wss://wbs.kcex.com/ws?platform=web` (captured from production JS 2026-09-04; also works on `wbs.kcex.io`). Env override: `KCEX_WS_URL`. |
| Auth | Public. **No** `KCEX_TOKEN` / `ws_token` for this socket. |
| Subscribe | `{"method":"SUBSCRIPTION","params":[...],"id":3}` |
| Channels | `spot@public.miniTicker@BTC_USDT@UTC+0`, `spot@public.aggre.deals@BTC_USDT`, `spot@public.limit.precision.depth@BTC_USDT@0.01` |
| Heartbeat | Client sends `{"method":"PING"}` on an interval; ignore `{"msg":"PONG"}`. |
| Fallback | If WS is down or stale (`STALE_MS`, default 30s), existing `poll_quotes` REST path. |
| Local bind | `127.0.0.1` only. Default port **8765**. Env: `CHART_PORT`. |
| Local WS JSON | Our schema (below). Never forward raw KCEX frames to the browser. |
| Chart candles | 15m from existing REST kline. Live last from our WS. No order UI. |
| Pair | BTC/USDT (`SYMBOL`) only. |

## Captured KCEX frames (do not invent others)

Subscribe ack:

```json
{"id":3,"code":0,"msg":"spot@public.miniTicker@BTC_USDT@UTC+0,spot@public.aggre.deals@BTC_USDT"}
```

miniTicker (`c` = channel, `d.p` = last):

```json
{"c":"spot@public.miniTicker@BTC_USDT@UTC+0","s":"BTC_USDT","t":1788553351046,"d":{"s":"BTC_USDT","p":"79801.99","h":"82267.72","l":"78660.01","q":"18384.29538","v":"1477022288.46","r":"0.0046","tr":"-0.018"}}
```

Deal (`d.deals[].p` last trade, `T` 1 buy / 2 sell):

```json
{"c":"spot@public.aggre.deals@BTC_USDT","s":"BTC_USDT","d":{"deals":[{"M":0,"T":2,"p":"79801.99","q":"0.00239","t":1788553351631}]}}
```

Depth channel (from JS, not live-probed in the capture session): `spot@public.limit.precision.depth@BTC_USDT@0.01` with `d.bids` / `d.asks` / `bestBids` / `bestAsks`. Parser must tolerate missing depth without killing the socket.

## Architecture

```text
KCEX public WS (wbs.kcex.com)
        │  one connection
        ▼
┌───────────────┐     snapshot      ┌─────────────┐
│ kcex.ws       │ ───────────────▶  │ Hub         │
│ PublicSpotWs  │                   │ last/bid/ask│
└───────────────┘                   └──────┬──────┘
                                           │
                    ┌──────────────────────┼──────────────────┐
                    ▼                      ▼                  ▼
              bot Eye                 local WS            chart HTTP
              poll_quotes             127.0.0.1:8765      static page
              only if stale           our JSON            REST kline + WS
```

Four units. Each has one job.

### `kcex/ws.py` — PublicSpotWs

- Connect, subscribe, ping, reconnect with backoff.
- Parse JSON frames into a small typed event (`TickerEvent` / `DealEvent` / `DepthEvent` / `Ignored`).
- Does not know about LLM, collar, or HTTP.
- Tests: parse the captured frames; no live KCEX in CI.

### Hub

- Holds the latest `last`, `bid`, `ask`, `ts_ms`, `ws_ok`.
- Eye and the local broadcaster read this, they do not each open a KCEX socket.
- In-process (same `bot run`). Not a second OS process.

### Eye (change)

- `connect_ws` is no longer a no-op when URL is set. Default URL is the captured `wss://wbs.kcex.com/ws?platform=web` (so paper gets WS without the human pasting a host).
- `apply_frame` / a dedicated mapper: miniTicker `d.p` → last; depth best bid/ask when present; deals update last.
- `poll_quotes` stays as fallback when `not ws_ok` or `_stale()`.
- `poll_heavy` (kline + balances) unchanged.

### Local chart (`bot/chart_server.py` + `chart/`)

- HTTP `GET /` serves a single static page (candles + last price).
- HTTP `GET /kline` proxies or calls existing `KcexClient.kline` (public REST) for 15m history.
- WS `ws://127.0.0.1:8765/ws` broadcasts hub snapshots.
- Bind **127.0.0.1**. Refuse `0.0.0.0` in v1.
- Chart library: lightweight in-page (e.g. TradingView Lightweight Charts via CDN, or a minimal canvas). No order ticket.

## Our WS message

```json
{"type":"tick","symbol":"BTC_USDT","last":79801.99,"bid":79801.9,"ask":79802.0,"ts_ms":1788553351046}
```

Optional deal fan-out:

```json
{"type":"deal","symbol":"BTC_USDT","price":79801.99,"qty":0.00239,"side":"sell","ts_ms":1788553351631}
```

Unknown `type` → clients ignore. Do not pass through KCEX `c`/`d` blobs.

## Data flow

1. Process start: open PublicSpotWs (unless `KCEX_WS_URL` is explicitly `-` to force REST-only).
2. Frames → mapper → Hub.
3. Loop `run_once`: Eye snapshot from Hub; REST quotes only if stale.
4. If `--chart`: HTTP+WS thread/async task reads Hub and broadcasts ticks.
5. Browser: load `/`, fetch 15m kline once, subscribe local WS, update last candle/price.

## Error handling

| Event | Behavior |
| --- | --- |
| KCEX WS connect fail | Log, backoff reconnect, Eye uses REST `poll_quotes` |
| No frames for `STALE_MS` | `ws_ok=false`, REST fallback, keep reconnecting |
| Bad JSON frame | Drop frame, stay connected |
| Subscribe `code != 0` | Log, reconnect |
| Local WS client drop | Remove from fan-out; do not restart KCEX socket |
| Chart HTTP without WS | Page still shows REST kline; last price static until WS up |
| `--chart` port in use | Fail start with a clear error (do not silently pick another port) |

Private `GET /uc/user_api/ws_token` stays unused for this feature.

## CLI / env

```bash
PYTHONPATH=. python -m bot run              # Eye on KCEX public WS, REST fallback
PYTHONPATH=. python -m bot run --chart      # same + http://127.0.0.1:8765
```

| Env | Default | Role |
| --- | --- | --- |
| `KCEX_WS_URL` | `wss://wbs.kcex.com/ws?platform=web` | Empty used to mean “unset / REST only”. **Change:** empty now means the captured default. Set `KCEX_WS_URL=-` for REST-only. |
| `CHART_PORT` | `8765` | Local HTTP/WS port |
| `CHART_HOST` | `127.0.0.1` | Must remain loopback in v1 |

`.env.example` documents the new default and the `-` escape.

## Testing

- Parse captured miniTicker + deals fixtures (extend `tests/fixtures/kcex_ws_frames.jsonl` with **real** frames from 2026-09-04, not the old invented `{ch,last,bid,ask}` only).
- Mapper: ticker last; deal last; missing depth does not throw.
- Hub: one writer, Eye sees `ws_ok` and last.
- Eye: `poll_quotes` skipped when `ws_ok` and not stale; called when stale.
- Local message encoder: tick JSON shape.
- CI: **no** live `wss://wbs.kcex.com` connect. Mock the socket.

## Out of scope until asked

Second process, Cloudflare tunnel, mobile chart, ETH, private order WS, Geetest, TradingView charting library clone, writing kline from deals into SQLite.

## Implementation note

`Eye.connect_ws` currently raises if `KCEX_WS_URL` is set. This spec replaces that stub. `docs/kcex-spot-api.md` must be updated in the same work: the live URL is now confirmed; stop saying “do not invent wss”.
