# KCEX Spot API (web reverse-engineering)

Captured on 2026-09-04 from a logged-in session on `https://www.kcex.com/exchange/BTC_USDT`.

This is the **website** API (`www.kcex.com`), not a documented HMAC OpenAPI. `/user/openapi` returns 404. The bot should treat the session token as a secret.

## Auth

Every private call sends:

| Header | Example |
| --- | --- |
| `authorization` | `WEB` + hex token |
| `cookie` | `Authorization=<same token>` |
| `platform` | `WEB` |
| `language` / `accept-language` | `en-US` |
| `version` | `1.0.0` |
| `user-device` | base64 JSON `{visitorId, requestId, isp}` (login only) |

Login flow observed:

1. Email + password on `/login`
2. Geetest slider captcha
3. Google Authenticator (2FA)
4. `POST /uc/user_api/login/validation` (empty body, session already in cookie) → `{ id, account, token, authLevel }`
5. `GET /uc/user_api/user_info`

Do **not** automate Geetest. Do **not** copy the token by hand. Run `python -m kcex.cli login` — it waits for the human (captcha + Google Authenticator) and writes `KCEX_TOKEN` to `.env`.

## Session lifetime

2FA / Geetest run **only at human login**. After that, every bot call is the same opaque session token (`WEB` + 64 hex, 67 chars). It is not a JWT: there is no `exp` inside the string.

Evidence from the live session:

- Login checkbox: **Stay logged in on this device for 7 days** (we left it checked).
- Cookie `Authorization` is readable from JS (not HttpOnly).
- `GET /uc/user_api/user_info` **requires** the `authorization` header. Cookie alone returned `401`.
- `POST /uc/user_api/login/validation` with that header returns the **same** token (`id`, `account`, `token`, `authLevel`). It does not rotate or refresh a new secret.
- `wsToken` from `/uc/user_api/ws_token` is a different short-lived value for sockets. REST trading does not use it.

Practical model:

1. Human runs `python -m kcex.cli login` (password + captcha + Google Authenticator in the bot’s Chrome).
2. CLI writes `authorization` into `.env` as `KCEX_TOKEN`.
3. The bot reuses that token on every request. No reconnect per call.
4. When the server returns `401` / `success: false` on `user_info`, the 7-day session is dead. Run `login` again. Logout on the site, password change, or security reset will also kill it.

There is no silent refresh we can call without 2FA. The CLI command `python -m kcex.cli login` is the OAuth-style stand-in: it opens a persistent Chrome profile, you complete captcha + authenticator once, it writes `KCEX_TOKEN` to `.env`. After ~7 days the same command opens the window again. If the profile is still logged in, capture is instant.

Place-order JS uses `needDolos: 27`. CORS also allows `content-sign` and `content-time`. Server-side POSTs may be rejected without that anti-bot header. Test with a tiny order before trusting live size.

## Market data

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/spot/api/common/ping` | health |
| GET | `/spot/api/market-2/spot/market/v2/web/symbol/ticker?symbol=BTC_USDT` | last `c`, 24h `h/l/a/q`, change `tzr` |
| GET | `/spot/api/market-2/spot/market/v2/web/symbol/trade?symbol=BTC_USDT` | filters: `ps` price scale, `qs` qty scale, `aol` allowed order types, fees `tfr/mfr` |
| GET | `/spot/api/spot/market/depth?symbol=BTC_USDT&pricePrecision=0.01` | `bids`/`asks` as `{p,q}` |
| GET | `/spot/api/spot/market/deals?symbol=BTC_USDT` | last trades `{p,q,T,t}` (`T=1` buy, `T=2` sell) |
| GET | `/spot/api/spot/market/kline?symbol=BTC_USDT&interval=Min15&start=&end=&openPriceMode=LAST_CLOSE` | arrays `t,o,c,h,l` |

BTC/USDT rules captured: price scale 2, qty scale 5, limit + market + stop-limit enabled, maker/taker fee `0`.

The live UI polled REST for depth and deals during this initial capture. The public WebSocket was captured separately — see the [WebSocket](#websocket) section below.

## Account

| Method | Path |
| --- | --- |
| GET | `/uc/user_api/user_info` |
| GET | `/uc/user_api/ws_token` |
| GET | `/spot/api/spot/asset/currency/balances?currency=BTC,USDT` |
| GET | `/spot/api/spot/order/current/orders/v2?orderTypes=1,2,3,4,5,100,101,102,103,20&pageNum=1&pageSize=100&states=0,1,3` |
| GET | `/spot/api/spot/order/history/orders/v2` |
| GET | `/spot/api/spot/deal/deals?currency=BTC&market=USDT&needPage=false&pageNum=1&pageSize=1000&startTime=&endTime=` |

Balance shape:

```json
{"currency":"USDT","total":"...","available":"...","frozen":"..."}
```

Open order fields: `id`, `symbol`, `tradeType` (`1` buy / `2` sell in list responses), `orderType`, `price`, `triggerPrice`, `quantity`, `state`, `triggerType` (`GE`/`LE`).

## Place / cancel (from JS bundles)

| Action | Method | Path |
| --- | --- | --- |
| Limit / post-only / IOC / FOK | POST | `/spot/api/spot/order/place` |
| Market | POST | `/spot/api/spot/v4/order/place` |
| OCO | POST | `/spot/api/spot/v4/order/place/oco` |
| Trigger | POST | `/spot/api/spot/order/place/trigger/v2` |
| TP/SL attach | POST | `/spot/api/spot/order/place/stop_profit_or_loss` |
| Modify | POST | `/spot/api/spot/order/modify` |
| Cancel | DELETE | `/spot/api/spot/order/cancel/v2?orderId=` |

Order type map in the UI:

| Name | Value |
| --- | --- |
| LIMIT_ORDER | `1` / `"LIMIT_ORDER"` |
| POST_ONLY | `2` |
| IMMEDIATE_OR_CANCEL | `3` |
| FILL_OR_KILL | `4` |
| MARKET_ORDER | `5` / `"MARKET_ORDER"` |
| STOP_LIMIT | `100` |
| STOP_MARKET | `103` |

Limit body:

```json
{
  "currency": "BTC",
  "market": "USDT",
  "tradeType": "BUY",
  "price": "79000.00",
  "quantity": "0.00010",
  "orderType": "LIMIT_ORDER"
}
```

Market body uses the same fields plus `orderType: "MARKET_ORDER"` on `/spot/api/spot/v4/order/place`. Quantity **or** quote `amount` depending on the form mode.

Trigger body (stop-market example already live on the account):

```json
{
  "currency": "BTC",
  "market": "USDT",
  "tradeType": "SELL",
  "triggerType": "LE",
  "triggerPrice": "75722.00",
  "quantity": "0.00064",
  "amount": "0",
  "orderType": 103
}
```

`triggerType`: `GE` = greater-or-equal, `LE` = less-or-equal.

Success codes: `0` or `200`. Place response `data` is the new order id.

## WebSocket

**Public URL (confirmed):** `wss://wbs.kcex.com/ws?platform=web` (`kcex.ws.DEFAULT_WS_URL`). Also reachable at `wbs.kcex.io`. This is the bot's `KCEX_WS_URL` default when the env var is unset — no longer "unconfirmed."

**No auth required.** The public ticker/deals/depth channels need no token at all. `GET /uc/user_api/ws_token` still exists and still returns a short-lived `wsToken`, but that is for a *different*, private/authenticated socket use case — it is **not used** by this feature and nothing in the bot calls it for the public WS.

Do not invent any *other* `wss://` host or path beyond the one above — this one was captured from a live session and is the only one confirmed. Anything else must be captured from Chrome DevTools first, then documented here.

### Subscribe

On connect, send one `SUBSCRIPTION` message covering all three channels for the symbol (`kcex.ws.subscribe_message(symbol)`):

```json
{
  "method": "SUBSCRIPTION",
  "params": [
    "spot@public.miniTicker@BTC_USDT@UTC+0",
    "spot@public.aggre.deals@BTC_USDT",
    "spot@public.limit.precision.depth@BTC_USDT@0.01"
  ],
  "id": 3
}
```

### Ping

Client sends `{"method": "PING"}` (`kcex.ws.ping_message()`); server replies `{"msg": "PONG", ...}` (recognized and dropped in `parse_frame`, never surfaced as an event).

Timing, implemented in `kcex.ws.PublicSpotWs.pump`: a ping is due every `PING_INTERVAL_S = 15.0` seconds of wall-clock time. The read loop calls `sock.recv(timeout=RECV_TIMEOUT_S)` with `RECV_TIMEOUT_S = 5.0`, so it wakes at least every 5s even on a quiet connection, checks whether a ping is due, sends one if so, then goes back to waiting for the next frame. This was a deliberate fix during implementation review: an earlier version blocked on `recv()` with no timeout, so a quiet connection could starve the ping entirely and eventually get dropped by the server.

### Frame shapes (captured)

`tests/fixtures/kcex_ws_frames.jsonl` holds 3 lines:

1. `{"ch":"ticker","symbol":"BTC_USDT","last":"80000.1","bid":"80000.0","ask":"80000.2"}` — an older **synthetic** fixture line, kept only for backward-compat coverage of `Eye.apply_frame`'s permissive `{ch,last,bid,ask}`-and-aliases parsing. Not a real captured KCEX frame.
2. A real captured `spot@public.miniTicker@BTC_USDT@UTC+0` frame:
   ```json
   {"c":"spot@public.miniTicker@BTC_USDT@UTC+0","s":"BTC_USDT","t":1788553351046,"d":{"s":"BTC_USDT","p":"79801.99","h":"82267.72","l":"78660.01","q":"18384.29538","v":"1477022288.46","r":"0.0046","tr":"-0.018"}}
   ```
3. A real captured `spot@public.aggre.deals@BTC_USDT` frame:
   ```json
   {"c":"spot@public.aggre.deals@BTC_USDT","s":"BTC_USDT","d":{"deals":[{"M":0,"T":2,"p":"79801.99","q":"0.00239","t":1788553351631}]}}
   ```

`kcex.ws.parse_frame` turns these into `TickerEvent` / `DealEvent` / `DepthEvent` dataclasses (`kcex/ws.py`). It ignores `PONG` replies and any message shaped like a plain subscribe ack (`code` present, no `c` channel key).

### How the bot uses it

- `bot/hub.py`'s `Hub` is the in-process holder of the latest `last`/`bid`/`ask`/`ts_ms`/`ws_ok` for the symbol, updated from parsed events via `Hub.apply(event)`.
- `Eye.start_ws_thread()` (called unconditionally from `bot/cli.py`, no-ops if `settings.ws_url` is empty) spawns exactly one daemon thread that owns the process's single `PublicSpotWs` connection and feeds events into the shared `Hub`, reconnecting with a 2s backoff on any error.
- **v1 production path is now public WS + REST fallback**, not REST-poll-primary: `Eye.poll_quotes()` calls `Eye.sync_hub()` first (pulling the `Hub` snapshot, stamping `last_update_ms` from the event's own timestamp) and only falls back to the REST ticker/depth calls when the WS is down or stale (no fresh frame within `STALE_MS`, default 30000ms). This covers both a hard error (`hub.mark_down()`) and a WS thread that goes silent without erroring.
- Chart candles are **still REST kline** — there is no kline-over-WS. `bot/chart_server.py`'s `GET /kline` proxies `client.kline(...)` the same way `Eye.poll_heavy()` does; only the live tick stream (`GET /ws`) is WS-driven, and it re-broadcasts our own encoded tick JSON (`bot/chart_encode.py`), never the raw KCEX frame.
- `KCEX_WS_URL=-` forces REST-only quotes (`Settings.ws_url` resolves to `""`, `start_ws_thread` no-ops). Leaving `KCEX_WS_URL` unset uses the confirmed default above.

## Snapshot of this account (read-only)

- Spot USDT available: `450.6362432`
- Spot BTC: `0.00064` **frozen** in a trigger sell
- Fill: buy `0.00064 BTC` @ `77287.12` (~49.46 USDT), taker, fee 0
- Open order: stop-market sell `0.00064 BTC` trigger `75722` (`orderType` 103, `LE`)
