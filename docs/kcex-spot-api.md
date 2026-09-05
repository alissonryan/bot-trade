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

## WebSocket (public) — verified 2026-09-04

The site config defines `mainSocketUrl = "wss://wbs." + domain`. The socket speaks the MEXC spot v3 protocol.

| item | value |
| --- | --- |
| URL | `wss://wbs.kcex.com/ws` |
| Subscribe | `{"method":"SUBSCRIPTION","params":["spot@public.deals.v3.api@BTCUSDT","spot@public.bookTicker.v3.api@BTCUSDT"]}` |
| Ack | `{"id":0,"code":0,"msg":"spot@public.deals.v3.api@BTCUSDT"}` |
| Keepalive | `{"method":"PING"}` → `{"id":0,"code":0,"msg":"PONG"}` (the bot pings every 15 s) |
| Symbol | **no underscore** (`BTCUSDT`) |

Frame shape `{"c": channel, "d": {...}, "s": "BTCUSDT", "t": ms}`:

| channel | `d` |
| --- | --- |
| `spot@public.deals.v3.api@BTCUSDT` | `{"deals":[{"p":"79802.15","v":"0.00437","S":1,"t":1788553416797}]}` (`S` 1 buy / 2 sell) |
| `spot@public.bookTicker.v3.api@BTCUSDT` | `{"a":"79562.02","A":"6.42732","b":"79562.01","B":"6.35862"}` |
| `spot@public.limit.depth.v3.api@BTCUSDT@5` | `{"bids":[{"p","v"}...],"asks":[...]}` |
| `spot@public.miniTicker.v3.api@BTCUSDT@UTC+0` | `{"p": last, "r", "h", "l", "v", "q", ...}` |
| `spot@public.kline.v3.api@BTCUSDT@Min15` | `{"k":{"t","o","c","h","l","v","a","T","i"}}` |

Real frames: `tests/fixtures/kcex_ws_frames.jsonl`. Parser: `bot/ws.py`. The bot uses deals (last) and bookTicker (bid/ask); klines still come from REST when the LLM is due.

- Private socket: `GET /uc/user_api/ws_token` returns a short-lived `wsToken`; the private channel names were not captured. Not used.

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

For the record: `https://www.kcex.com/fapi/v1/contract/{ping,detail,ticker,depth/BTC_USDT,kline/BTC_USDT,deals/BTC_USDT,funding_rate/BTC_USDT,...}` answer without authentication in the MEXC contract API format, private routes live under `/fapi/v1/private/...` (401 without the session), and the futures socket is `wss://www.kcex.com/fapi/edge` (`{"method":"sub.ticker","param":{"symbol":"BTC_USDT"}}`). Not used by this bot.

## Snapshot of this account (read-only)

- Spot USDT available: `450.6362432`
- Spot BTC: `0.00064` **frozen** in a trigger sell
- Fill: buy `0.00064 BTC` @ `77287.12` (~49.46 USDT), taker, fee 0
- Open order: stop-market sell `0.00064 BTC` trigger `75722` (`orderType` 103, `LE`)
