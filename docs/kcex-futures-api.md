# KCEX perpetual futures: public API notes

Captured 2026-09-16 without authentication. Used only by the paper bot in `fut/`.
Private routes (`/fapi/v1/private/...`) answer 401 without a session and are **not captured**;
nothing in this repo calls them.

## REST (GET, base `https://www.kcex.com`)

| Path | Params | Used fields |
|---|---|---|
| `/fapi/v1/contract/detail` | `symbol` | `contractSize` (0.0001), `minVol` (1), `maxVol`, `priceUnit` (0.1), `takerFeeRate` (0.0001), `makerFeeRate` (0), `maintenanceMarginRate` (0.005), `maxLeverage` (125), `state` (0 = trading) |
| `/fapi/v1/contract/ticker` | `symbol` | `lastPrice`, `bid1`, `ask1`, `fairPrice`, `indexPrice`, `fundingRate`, `timestamp` |
| `/fapi/v1/contract/depth/{symbol}` | `limit` | `asks`/`bids` as `[price, vol_contracts, order_count]`, `version`, `timestamp` |
| `/fapi/v1/contract/deals/{symbol}` | `limit` | list of `{p, v, T, O, M, t}`, newest first |
| `/fapi/v1/contract/kline/{symbol}` | `interval=Min1`, `start`, `end` (seconds) | column arrays `time, open, high, low, close, vol` |
| `/fapi/v1/contract/funding_rate/{symbol}` | | `fundingRate`, `collectCycle` (8 h), `nextSettleTime` (ms) |

Envelope: `{"success": true, "code": 0, "data": ...}`.

## WebSocket `wss://www.kcex.com/fapi/edge`

Subscribe (one message each): `{"method":"sub.ticker","param":{"symbol":"BTC_USDT"}}`, same for
`sub.deal`, `sub.depth`, `sub.fair.price`. Ack: `{"channel":"rs.sub.depth","data":"success"}`.
Ping `{"method":"ping"}` → `{"channel":"pong"}`.

| Channel | `data` | Rate observed |
|---|---|---|
| `push.ticker` | same fields as REST ticker | ~0.5/s |
| `push.fair.price` | `{"symbol","price"}` | ~0.6/s |
| `push.deal` | `{p, v, T, O, M, t}` (one object) | per trade |
| `push.depth` | `{asks, bids, version}` **incremental** | ~43/s |

- Depth deltas carry a contiguous `version` (0 gaps in 518 frames). Volume `0` removes the level.
  Keep a book from a REST depth snapshot (with its `version`) plus deltas `version = prev + 1`;
  on a gap, resync from REST.
- `push.funding.rate` was subscribed but sent nothing in 12 s; funding is read from REST.
- **Inferred, not documented:** deal `T=2` printed at the ask (aggressive buy) and `T=1` at the
  bid (aggressive sell) in every captured sample, WS and REST.
- Volumes are in contracts (`× contractSize` BTC).

Samples: `tests/fixtures/kcex_fut_ws_frames.jsonl`.
