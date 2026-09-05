# KCEX Spot API (web reverse-engineering)

Captured on 2026-09-04 from a logged-in session on `https://www.kcex.com/exchange/BTC_USDT`. WebSocket, WAF behaviour and the futures note added the same day from public probes.

This is the **website** API (`www.kcex.com`), not a documented HMAC OpenAPI. `/user/openapi` returns 404 and `api.kcex.com` answers 403 to everything. The bot should treat the session token as a secret: it is the whole account.

## Transport notes

- **User-Agent matters.** The WAF returns `406` for curl's default UA. A Chrome UA and python-requests' default both passed on 2026-09-04. The client sends a browser UA (`KCEX_USER_AGENT` to override).
- The client retries `GET` on 429/5xx/network errors. `POST`/`DELETE` are **never** retried: a retried order is a duplicate order.
- Success codes: `0` or `200` (also `"0"`, `"200"`), or `success: true`.

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

Do **not** automate Geetest. Do **not** copy the token by hand. Run `python -m kcex.cli login` — it waits for the human (captcha + Google Authenticator) and writes `KCEX_TOKEN` and `KCEX_TOKEN_AT` to `.env` (mode 600).

## Session lifetime

2FA / Geetest run **only at human login**. After that, every bot call is the same opaque session token (`WEB` + 64 hex, 67 chars). It is not a JWT: there is no `exp` inside the string.

Evidence from the live session:

- Login checkbox: **Stay logged in on this device for 7 days** (we left it checked).
- Cookie `Authorization` is readable from JS (not HttpOnly).
- `GET /uc/user_api/user_info` **requires** the `authorization` header. Cookie alone returned `401`.
- `POST /uc/user_api/login/validation` with that header returns the **same** token. It does not rotate or refresh a new secret.
- `wsToken` from `/uc/user_api/ws_token` is a different short-lived value for the private socket. REST trading does not use it.

Practical model:

1. Human runs `python -m kcex.cli login` (password + captcha + Google Authenticator in the bot's Chrome).
2. CLI writes `authorization` into `.env` as `KCEX_TOKEN`, plus `KCEX_TOKEN_AT`.
3. The bot reuses that token on every request and warns from day 6.
4. When the server returns `401` on `user_info`, the session is dead. Run `login` again. Logout on the site, password change, or security reset will also kill it.

Place-order JS uses `needDolos: 27`. CORS also allows `content-sign` and `content-time`. Server-side POSTs may be rejected without that anti-bot header. **No live order has been sent through this client yet.** Test at the venue minimum before trusting live size.

## Market data (REST)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/spot/api/common/ping` | health |
| GET | `/spot/api/market-2/spot/market/v2/web/symbol/ticker?symbol=BTC_USDT` | last `c`, 24h `h/l/a/q`, change `tzr` |
| GET | `/spot/api/market-2/spot/market/v2/web/symbol/trade?symbol=BTC_USDT` | `ps` price scale, `qs` qty scale, `aol` allowed order types, fees `tfr/mfr/bfr`, limits `li/la` (limit min/max amount) and `mi/ma` (market min/max amount, USDT; semantics inferred) |
| GET | `/spot/api/spot/market/depth?symbol=BTC_USDT&pricePrecision=0.01` | `data.data.bids` (or `bestBids`) / `asks` as `{p,q}` |
| GET | `/spot/api/spot/market/deals?symbol=BTC_USDT` | last trades `{p,q,T,t}` (`T=1` buy, `T=2` sell) |
| GET | `/spot/api/spot/market/kline?symbol=BTC_USDT&interval=Min15&start=&end=&openPriceMode=LAST_CLOSE` | arrays `t,o,c,h,l,v`; `t` in **seconds**; the last element is the bar still forming |

BTC/USDT rules captured: price scale 2, qty scale 5, limit + market + stop-limit enabled, maker/taker fee `0`, `mi` = 1, `ma` = 600000. Kline history is deep (Day1 back to 2018 in one request; Min1 not capped at 3 days).

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

BTC parked in a trigger order shows up as `frozen`. The bot confirms fills with `available + frozen`.

Open order fields: `id`, `symbol`, `tradeType` (`1` buy / `2` sell in list responses), `orderType`, `price`, `triggerPrice`, `quantity`, `state`, `triggerType` (`GE`/`LE`).

The private deals payload shape was **not** captured. `bot/hands.py::avg_fill_from_deals` tries common key names (`orderId`, `price`/`p`, `quantity`/`q`/`v`) and falls back to an estimate when nothing matches. Capture one real payload and pin it in a test.

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

Market body uses the same fields plus `orderType: "MARKET_ORDER"` on `/spot/api/spot/v4/order/place`. Quantity **or** quote `amount` depending on the form mode (the bot sends `quantity`; untested live).

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

## Futures (out of scope for this bot)

**Public URL (confirmed):** `wss://wbs.kcex.com/ws?platform=web` (`kcex.ws.DEFAULT_WS_URL`). Also reachable at `wbs.kcex.io`. The site config defines `mainSocketUrl = "wss://wbs." + domain`. This is the bot's `KCEX_WS_URL` default when the env var is unset — no longer "unconfirmed."

**No auth required.** The public ticker/deals/depth channels need no token at all. `GET /uc/user_api/ws_token` still exists and still returns a short-lived `wsToken`, but that is for a *different*, private/authenticated socket use case — it is **not used** by this feature and nothing in the bot calls it for the public WS.

Do not invent any *other* `wss://` host or path beyond the one above — this one was captured from a live session and is the only one confirmed. Anything else must be captured from Chrome DevTools first, then documented here.

### Two channel conventions, both live

KCEX is a MEXC white-label and accepts **both** naming conventions on the same
connection. Verified live on 2026-09-05 by subscribing to all five on one socket:
every one acked and streamed.

| channel | convention | symbol form | frames/25s |
| --- | --- | --- | --- |
| `spot@public.miniTicker@BTC_USDT@UTC+0` | legacy | `BTC_USDT` | 25 |
| `spot@public.aggre.deals@BTC_USDT` | legacy | `BTC_USDT` | 53 |
| `spot@public.limit.precision.depth@BTC_USDT@0.01` | legacy | `BTC_USDT` | 83 |
| `spot@public.deals.v3.api@BTCUSDT` | v3 | `BTCUSDT` | 64 |
| `spot@public.bookTicker.v3.api@BTCUSDT` | v3 | `BTCUSDT` | 32 |

Also documented from the v3 family but not subscribed:
`spot@public.limit.depth.v3.api@BTCUSDT@5`,
`spot@public.miniTicker.v3.api@BTCUSDT@UTC+0`,
`spot@public.kline.v3.api@BTCUSDT@Min15` (`{"k":{"t","o","c","h","l","v","a","T","i"}}`).

### Subscribe

On connect, send one `SUBSCRIPTION` message covering the three channels the bot
actually uses (`kcex.ws.subscribe_message(symbol)`):

```json
{
  "method": "SUBSCRIPTION",
  "params": [
    "spot@public.miniTicker@BTC_USDT@UTC+0",
    "spot@public.aggre.deals@BTC_USDT",
    "spot@public.bookTicker.v3.api@BTCUSDT"
  ],
  "id": 3
}
```

Top of book comes from **`bookTicker`**, not the depth ladder: it is a ~90-byte
frame carrying the exact best bid/ask (`{"a": ask, "A": askQty, "b": bid, "B": bidQty}`),
where `limit.precision.depth@...@0.01` sends the whole ladder and only at 0.01
rounding. `kcex.ws.ws_symbol()` strips the underscore for the v3 channel;
`_unws_symbol()` puts it back on parse so `Hub.symbol` does not flip between the
two conventions. The legacy depth parser is kept so older captures still parse.

### Ping

Client sends `{"method": "PING"}` (`kcex.ws.ping_message()`); server replies `{"msg": "PONG", ...}` (recognized and dropped in `parse_frame`, never surfaced as an event).

Timing, implemented in `kcex.ws.PublicSpotWs.pump`: a ping is due every `PING_INTERVAL_S = 15.0` seconds of wall-clock time. The read loop calls `sock.recv(timeout=RECV_TIMEOUT_S)` with `RECV_TIMEOUT_S = 5.0`, so it wakes at least every 5s even on a quiet connection, checks whether a ping is due, sends one if so, then goes back to waiting for the next frame. This was a deliberate fix during implementation review: an earlier version blocked on `recv()` with no timeout, so a quiet connection could starve the ping entirely and eventually get dropped by the server.

### Frame shapes (captured)

`tests/fixtures/kcex_ws_frames.jsonl` holds captures from both conventions
(look frames up by channel, not by line number):

1. `{"ch":"ticker","symbol":"BTC_USDT","last":"80000.1","bid":"80000.0","ask":"80000.2"}` — an older **synthetic** fixture line, kept only for backward-compat coverage of `Eye.apply_frame`'s permissive `{ch,last,bid,ask}`-and-aliases parsing. Not a real captured KCEX frame.
2. A real captured `spot@public.miniTicker@BTC_USDT@UTC+0` frame:
   ```json
   {"c":"spot@public.miniTicker@BTC_USDT@UTC+0","s":"BTC_USDT","t":1788553351046,"d":{"s":"BTC_USDT","p":"79801.99","h":"82267.72","l":"78660.01","q":"18384.29538","v":"1477022288.46","r":"0.0046","tr":"-0.018"}}
   ```
3. A real captured `spot@public.aggre.deals@BTC_USDT` frame:
   ```json
   {"c":"spot@public.aggre.deals@BTC_USDT","s":"BTC_USDT","d":{"deals":[{"M":0,"T":2,"p":"79801.99","q":"0.00239","t":1788553351631}]}}
   ```

4. A real captured `spot@public.deals.v3.api@BTCUSDT` frame — note qty is `v` (not `q`) and side is `S` (1 buy / 2 sell):
   ```json
   {"c":"spot@public.deals.v3.api@BTCUSDT","d":{"deals":[{"p":"79802.15","v":"0.00437","S":1,"t":1788553416797}],"e":"spot@public.deals.v3.api"},"s":"BTCUSDT","t":1788553416802}
   ```
5. A real captured `spot@public.bookTicker.v3.api@BTCUSDT` frame:
   ```json
   {"c":"spot@public.bookTicker.v3.api@BTCUSDT","d":{"A":"6.42732","B":"6.35862","a":"79562.02","b":"79562.01"},"s":"BTCUSDT","t":1788569995303}
   ```

`kcex.ws.parse_frame` turns these into `TickerEvent` / `DealEvent` / `DepthEvent` dataclasses (`kcex/ws.py`). It ignores `PONG` replies and any message shaped like a plain subscribe ack (`code` present, no `c` channel key). On `bookTicker`, a key that is *present but unparseable* rejects the whole frame rather than applying only one side — bid/ask feed the collar's spread check.

- Private socket: `GET /uc/user_api/ws_token` returns a short-lived `wsToken`; the private channel names were not captured. Not used.

### How the bot uses it

- `bot/hub.py`'s `Hub` is the in-process holder of the latest `last`/`bid`/`ask`/`ts_ms`/`ws_ok` for the symbol, updated from parsed events via `Hub.apply(event)`.
- `Eye.start_ws_thread()` (called from `bot/cli.py`; no-ops if `settings.ws_url` is empty or `WS_ENABLED=0`) spawns exactly one daemon thread that owns the process's single `PublicSpotWs` connection and feeds events into the shared `Hub`, reconnecting with a 2s backoff on any error. It is **idempotent** — `main()` and `_loop()` both reach for the socket, and this process must hold exactly one KCEX connection.
- **v1 production path is now public WS + REST fallback**, not REST-poll-primary: `Eye.poll_quotes()` calls `Eye.sync_hub()` first (pulling the `Hub` snapshot, stamping `last_update_ms` from the event's own timestamp) and only falls back to the REST ticker/depth calls when the WS is down or stale (no fresh frame within `STALE_MS`, default 30000ms). This covers both a hard error (`hub.mark_down()`) and a WS thread that goes silent without erroring.
- Chart candles are **still REST kline** — there is no kline-over-WS. `bot/chart_server.py`'s `GET /kline` proxies `client.kline(...)` the same way `Eye.poll_heavy()` does; only the live tick stream (`GET /ws`) is WS-driven, and it re-broadcasts our own encoded tick JSON (`bot/chart_encode.py`), never the raw KCEX frame.
- `KCEX_WS_URL=-` forces REST-only quotes (`Settings.ws_url` resolves to `""`, `start_ws_thread` no-ops), as does `WS_ENABLED=0`. Leaving `KCEX_WS_URL` unset uses the confirmed default above.

For the record: `https://www.kcex.com/fapi/v1/contract/{ping,detail,ticker,depth/BTC_USDT,kline/BTC_USDT,deals/BTC_USDT,funding_rate/BTC_USDT,...}` answer without authentication in the MEXC contract API format, private routes live under `/fapi/v1/private/...` (401 without the session), and the futures socket is `wss://www.kcex.com/fapi/edge` (`{"method":"sub.ticker","param":{"symbol":"BTC_USDT"}}`). Not used by this bot.

## Snapshot of this account (read-only)

- Spot USDT available: `450.6362432`
- Spot BTC: `0.00064` **frozen** in a trigger sell
- Fill: buy `0.00064 BTC` @ `77287.12` (~49.46 USDT), taker, fee 0
- Open order: stop-market sell `0.00064 BTC` trigger `75722` (`orderType` 103, `LE`)
