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

Do **not** automate Geetest. Export `KCEX_TOKEN` after a manual login.

## Session lifetime

2FA / Geetest run **only at human login**. After that, every bot call is the same opaque session token (`WEB` + 64 hex, 67 chars). It is not a JWT: there is no `exp` inside the string.

Evidence from the live session:

- Login checkbox: **Stay logged in on this device for 7 days** (we left it checked).
- Cookie `Authorization` is readable from JS (not HttpOnly).
- `GET /uc/user_api/user_info` **requires** the `authorization` header. Cookie alone returned `401`.
- `POST /uc/user_api/login/validation` with that header returns the **same** token (`id`, `account`, `token`, `authLevel`). It does not rotate or refresh a new secret.
- `wsToken` from `/uc/user_api/ws_token` is a different short-lived value for sockets. REST trading does not use it.

Practical model:

1. You log in once in the browser (password + captcha + Google Authenticator).
2. Copy `authorization` into `.env` as `KCEX_TOKEN`.
3. The bot reuses that token on every request. No reconnect per call.
4. When the server returns `401` / `success: false` on `user_info`, the 7-day session is dead. Log in again, paste the new token. Logout on the site, password change, or security reset will also kill it.

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

The live UI polled REST for depth and deals. No WebSocket frames were captured in DevTools for this page.

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

## Snapshot of this account (read-only)

- Spot USDT available: `450.6362432`
- Spot BTC: `0.00064` **frozen** in a trigger sell
- Fill: buy `0.00064 BTC` @ `77287.12` (~49.46 USDT), taker, fee 0
- Open order: stop-market sell `0.00064 BTC` trigger `75722` (`orderType` 103, `LE`)
