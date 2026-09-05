# KCEX LLM spot bot — design

Date: 2026-09-04  
Status: draft for user review  
Scope: v1 only. Paper then live, BTC/USDT, one OpenRouter model, code-owned risk, WebSocket market/fill plane.

This spec records decisions from the brainstorm. Do not implement until this file is approved.

## Goal

Replace the manual loop (open KCEX, look, buy/sell BTC, come back) with an autonomous bot:

1. Watch BTC/USDT continuously.
2. Every 15 minutes (or on a large move), one LLM decides `BUY` / `SELL` / `HOLD`.
3. Deterministic code sizes the order, attaches an ATR stop on the exchange, and either simulates (paper) or sends a market order (live).
4. Swap models via `.env` (OpenRouter). Same brain for paper and live.

Not a goal of v1: multi-pair, second LLM judge, grid, futures, copying the existing 0.00064 BTC stop position, or feeding the LLM every WebSocket tick.

## Locked decisions

| Topic | Choice |
| --- | --- |
| Venue | KCEX spot, session token (`KCEX_TOKEN`), CLI login in Chrome |
| Pair | BTC/USDT only |
| Cycle | 15 minutes, `CYCLE_MINUTES` configurable |
| Wake-up | Also run a cycle if price moved ~0.4% since last LLM call (`WAKE_MOVE_PCT`) |
| LLM | OpenRouter, one model at a time (`LLM_MODEL`) |
| Judge | Code only. No second model |
| Autonomy | Live sends orders without human confirm. Collar is the safety |
| First live collar | Max **20 USDT** per order, ~**5%** of free USDT, **1** bot position |
| Existing BTC | Do not cancel or modify the user’s current trigger sell (0.00064 BTC @ 75722) |
| Entry | Market (`POST /spot/api/spot/v4/order/place`) |
| Stop | Code computes ATR multiple, places KCEX stop-market trigger (`orderType` 103) |
| Modes | `MODE=paper` default; `MODE=live` explicit |
| Market data | WebSocket primary; REST fallback |
| LLM input | Compact numeric snapshot, never the WS firehose |

## Architecture

Four boxes. Each has one job and a testable interface.

```text
┌─────────────┐     local state      ┌─────────────┐
│  Eye        │ ───────────────────▶ │  Brain      │
│  WS + REST  │                      │  OpenRouter │
└─────────────┘                      └──────┬──────┘
                                            │ TradeIntent JSON
                                     ┌──────▼──────┐
                                     │  Collar     │
                                     │  risk gate  │
                                     └──────┬──────┘
                                            │ Approved | Rejected
                          ┌─────────────────┴─────────────┐
                          ▼                               ▼
                   ┌─────────────┐                 ┌─────────────┐
                   │ PaperExec   │                 │ LiveExec    │
                   │ ledger      │                 │ KCEX REST   │
                   └─────────────┘                 └─────────────┘
                          └─────────────┬─────────────┘
                                        ▼
                                   Audit log
```

### Eye

- Connect using `GET /uc/user_api/ws_token` then the private/public WS URL discovered in implementation (not fully mapped yet; first engineering task).
- Maintain: last price, top of book, last N trades, last fill, connection health.
- If WS dies: reconnect with backoff; meanwhile poll REST ticker/depth/deals (already reverse-engineered).
- Expose `snapshot()` for the brain and `on_move(threshold)` for wake-up.
- Does not call the LLM. Does not place orders.

### Brain

- HTTP to OpenRouter (`https://openrouter.ai/api/v1/chat/completions`).
- System prompt cached; user payload is the snapshot + last decision outcome.
- Output must parse as `TradeIntent` (schema below). Invalid JSON → skip cycle, log, no order.
- Temperature 0. Temperature and model come from env.
- Daily LLM budget (`LLM_DAILY_BUDGET_USD`) stops new calls until next UTC day.

### Collar

Hard rules, not prompt text. Order of checks:

1. Symbol is BTC/USDT.
2. `MODE` is paper or live.
3. Session valid (`user_info` not 401). Else halt and run `login` path.
4. No second bot position (ignore the user’s pre-existing trigger).
5. Intent action is `HOLD` → stop.
6. Notional ≤ `MAX_ORDER_USDT` (20).
7. Notional ≤ `MAX_PORTFOLIO_PCT` of free USDT (0.05).
8. Day realized+unrealized loss of **bot** trades ≤ `MAX_DAY_LOSS_USDT` (default 20). Else halt until next UTC day.
9. Stop price must be computable (ATR available). Else reject BUY/SELL.
10. Live only if `MODE=live`. Paper never hits KCEX place/cancel for bot orders.

Rejected intents are logged with the rule name. The LLM cannot override.

### Hands

**Paper:** mark to live mid/last from the Eye; apply a small slippage model (e.g. half spread or `PAPER_SLIPPAGE_BPS`). Simulate fill + simulated stop. Persist ledger in SQLite.

**Live:** `place_market` then immediately `place_trigger` stop-market (sell if long) with ATR price. Reconcile via WS fills and REST open-orders. If market fills and trigger fails, flatten or retry trigger (must not leave an unprotected long).

Do not send cancel for orders whose ids are not in the bot’s ledger (protects the 75722 stop).

## TradeIntent schema

LLM may only emit this JSON object. Extra keys ignored. Missing required keys → reject.

```json
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0,
  "reason": "short string, max 240 chars",
  "regime": "trend" | "range" | "shock" | "unknown"
}
```

The model does **not** send quantity, notional, stop price, or limit price. Collar + ATR own those numbers.

Sizing: `notional = min(MAX_ORDER_USDT, MAX_PORTFOLIO_PCT * free_usdt)` then qty = notional / last_price, rounded to KCEX qty scale 5.

Stop (long): `stop = entry - ATR_MULT * atr`. `ATR_PERIOD` default 14 on 15m bars. `ATR_MULT` default 2.0. Clamp stop distance to `[MIN_STOP_PCT, MAX_STOP_PCT]` (defaults 0.4% and 4%) so a dead ATR cannot place a 0.01% or 20% stop.

SELL while flat: HOLD. SELL while long: close the bot position (market) and cancel the bot’s trigger.

## Cycle

1. Eye updates continuously on WS.
2. Scheduler: `now - last_llm_ts >= CYCLE_MINUTES` **or** `abs(price / price_at_last_llm - 1) >= WAKE_MOVE_PCT`.
3. Build snapshot: last, bid, ask, spread, 15m OHLCV window, ATR, free USDT, bot position, last intent + PnL, WS health.
4. Brain → TradeIntent.
5. Collar → Approved | Rejected.
6. Hands execute if approved.
7. Append audit row.

Default: `CYCLE_MINUTES=15`, `WAKE_MOVE_PCT=0.004`.

## Auth

Reuse existing `kcex.login` / `KCEX_TOKEN`. Private REST and WS use the same session header. On 401: stop trading, log, optionally open `python -m kcex.cli login`. No silent password/2FA automation.

`wsToken` is separate and shorter-lived; refresh via `/uc/user_api/ws_token` on WS auth failure.

## Config (`.env`)

Required for paper: `OPENROUTER_API_KEY`, `LLM_MODEL`.  
Required for live: those plus valid `KCEX_TOKEN`.

| Key | Default |
| --- | --- |
| `MODE` | `paper` |
| `SYMBOL` | `BTC_USDT` |
| `CYCLE_MINUTES` | `15` |
| `WAKE_MOVE_PCT` | `0.004` |
| `MAX_ORDER_USDT` | `20` |
| `MAX_PORTFOLIO_PCT` | `0.05` |
| `MAX_DAY_LOSS_USDT` | `20` |
| `ATR_PERIOD` | `14` |
| `ATR_MULT` | `2.0` |
| `LLM_DAILY_BUDGET_USD` | `2` |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` |

Never commit `.env`. Existing `KCEX_EMAIL` / `KCEX_PASSWORD` only for the login window.

## File layout (v1)

Keep the current client. Add modules; do not replace KCEX auth with CCXT.

```text
kcex/           # existing REST + login
bot/
  eye.py        # WS + REST fallback + snapshot
  brain.py      # OpenRouter + schema parse
  collar.py     # pure functions, unit-tested
  hands.py      # paper ledger + live place/trigger
  cycle.py      # scheduler + orchestration
  store.py      # sqlite audit + paper fills
  cli.py        # python -m bot run
docs/kcex-spot-api.md
docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md
```

## Error handling

| Event | Behavior |
| --- | --- |
| WS disconnect | Reconnect; REST poll; do not place if both eyes stale > 30s |
| LLM timeout / 5xx | Skip cycle |
| Bad JSON | Skip cycle |
| Collar reject | Log, no order |
| Market fill, trigger fail | Retry trigger; if still fail, market-sell the bot qty |
| 401 | Halt live; request login |
| Day loss cap | Halt new entries until UTC day rolls |
| LLM budget | Skip brain; still manage existing stop via exchange |

## Testing

- `collar.py`: table tests (oversize, second position, HOLD, 401, wrong symbol, ATR missing).
- Paper exec: fake ticks, fill + stop simulation, ledger balances.
- Live exec: mocked `KcexClient` — assert market then trigger; assert never cancel unknown order ids.
- Eye: parse recorded WS frames once captured; REST fallback if socket closed.
- No live orders in CI. First live is a manual `MODE=live` after paper review.

## Implementation order

1. Map KCEX WS (public ticker/deals/depth + private fills) using the logged-in Chrome session; document in `docs/kcex-spot-api.md`.
2. Eye + snapshot + REST fallback.
3. Collar unit tests then implementation.
4. Brain (OpenRouter) + audit log.
5. Paper executor + `python -m bot run`.
6. Live executor (market + ATR trigger) behind `MODE=live`.
7. Wake-on-move using WS last price.

## Out of scope (v1)

Second LLM, ETH or more pairs, limit/IOC entries, Telegram (nice later), training DSPy, CCXT as the KCEX transport, disabling 2FA, Geetest solvers.

## Self-review

- No placeholder APIs except the WS URL, which is an explicit first implementation task.
- Paper and live share Brain + Collar; only Hands differ.
- LLM never sets size or stop — matches locked decisions.
- Existing user stop is excluded by “only cancel bot ledger ids”.
- WebSocket is the eye, not the brain — matches “best bot” discussion.

## Amendment — 2026-09-04 safety revision

Recorded after a full code read and public probes of the venue. Where this section
disagrees with the text above, this section wins.

| Topic | Spec said | Now |
| --- | --- | --- |
| Cycle | 15 minutes | `CYCLE_MINUTES` (ops 5; code default 15) |
| Market data | "WebSocket primary, URL to be discovered" | Verified: `wss://wbs.kcex.com/ws`, MEXC v3 protocol (deals + bookTicker). REST is a fallback every `POLL_SECONDS`, never a 1-second poll |
| Collar rule 8 | day loss = realized + unrealized | implemented (was realized only) |
| Sizing | `QTY_SCALE` fixed at 5 | scales and minimum read from the venue's symbol rules |
| Stop | computed on `last` | recomputed on the real fill price |
| Live entry | market then trigger | market → **persist PENDING** → confirm fill by balance → trigger; unfilled entry is cancelled |
| Trigger failure | "retry; if still fail, market-sell" | retry 2×, flatten once, else state `UNPROTECTED` + process exit 2 (never a silent flat) |
| SELL | market sell | cancel stop → confirm cancel → sell; failed sell restores the stop; unconfirmed sell is `CLOSING` until reconcile |
| Reconciliation | "reconcile via WS fills and REST open-orders" | `reconcile()` on boot and every LLM cycle against balances + open orders |
| LLM budget | daily USD cap | charged with `usage.cost` from OpenRouter; fallback flat cost only when absent |
| LLM failures | "skip cycle" | every failure has a name in the audit (`llm_budget`, `llm_timeout`, `llm_http_<n>`, `llm_parse`, ...) |
| Audit | intent + gate | + snapshot (last/bid/ask/atr), LLM reason/cost, order ids, position state, eye health |
| Paper | mid/ask + bps, cash implicit | cash is a persisted ledger; stop pays slippage; fills carry prices |
| Process | none | instance lock, file log, backoff on errors, exit codes 1/2/3/4 |
| Secrets | `.env` | `.env` written with mode 600; `KCEX_TOKEN_AT` recorded, warning from day 6 |

Still open: the anti-bot signature on order POSTs (no live order has been sent), the
private deals payload shape (entry price is an estimate until captured), and whether
market buys need `amount` instead of `quantity`.
