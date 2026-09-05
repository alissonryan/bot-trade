# AGENTS.md — bot-trade

Instructions for coding agents (Claude Code, Cursor, Codex, Grok, etc.). **Read this before changing anything.** Claude Code also has [CLAUDE.md](CLAUDE.md) — keep both in sync.

## What this repo is

Autonomous **BTC/USDT spot** bot on **KCEX**. An LLM (via OpenRouter) proposes `BUY` / `SELL` / `HOLD`. **Code** sizes the order, attaches an ATR stop, and either simulates (paper) or sends a market order (live).

KCEX has **no documented HMAC OpenAPI** (`/user/openapi` 404, `api.kcex.com` 403) and **no paper/demo account**. Paper is a local SQLite ledger on **live public prices**. Live uses a **web session token** (`KCEX_TOKEN`), not an exchange API key.

Owner currently trades manually and wants the bot to replace that loop. First live size is the venue minimum, on purpose.

## Read first

| File | Why |
| --- | --- |
| [README.md](README.md) | How to run, what the live path does step by step, exit codes |
| [docs/kcex-spot-api.md](docs/kcex-spot-api.md) | Reverse-engineered REST **and the verified WebSocket** |
| [docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md](docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md) | Product decisions; the amendment at the end lists what changed on 2026-09-04 |
| [docs/superpowers/plans/2026-09-04-kcex-llm-spot-bot.md](docs/superpowers/plans/2026-09-04-kcex-llm-spot-bot.md) | Original implementation plan (executed; code has moved on since) |

## Architecture (do not collapse)

```
Eye (WebSocket deals+bookTicker; REST fallback every POLL_SECONDS) → Snapshot
Brain (OpenRouter, 1 model) → ThinkResult(intent | None, reason, cost)
Collar (pure rules, symbol rules from the venue) → GateResult
Hands: PaperHands (SQLite ledger) | LiveHands (KCEX REST, balance-confirmed, reconciled)
Store: data/bot.db audit (with snapshot) + fills (with prices) + bot order ids + position (with state) + kv
```

| Path | Role |
| --- | --- |
| `kcex/` | Venue client + Chrome login. **Do not replace with CCXT.** |
| `kcex/client.py` | Reverse-engineered REST. GET retries; **POST/DELETE never retry** (a retried order is a duplicate order). Browser User-Agent. |
| `kcex/login.py` | Playwright session capture → `KCEX_TOKEN` + `KCEX_TOKEN_AT` |
| `bot/ws.py` | Public socket (MEXC v3 protocol): parser + reconnecting thread |
| `bot/eye.py` | Socket first, REST fallback that never raises; klines (forming bar dropped), balances, symbol rules |
| `bot/brain.py` | OpenRouter. Output `{action, confidence, reason, regime}` only; every failure has a named reason |
| `bot/collar.py` | Risk gate: size, ATR stop, one position, day-loss (realized + unrealized), min confidence, venue minimum |
| `bot/hands.py` | `PaperHands` / `LiveHands` — see **Live invariants** |
| `bot/cycle.py` | One loop step; audit row with snapshot, LLM outcome, order ids, position state |
| `bot/cli.py` | Loop with backoff, instance lock, file log, exit codes |
| `bot/store.py` | SQLite with forward migrations (ALTER TABLE) |
| `bot/atr.py` | Simple mean of the last N true ranges (not Wilder); gap-to-previous-close included |
| `data/` | `bot.db`, `bot.log`, `bot.lock` (gitignored) |
| `.env` | Secrets — **never commit, never print**; written with mode 600 |

## Locked decisions (v1, revised 2026-09-04)

- Pair: **BTC/USDT only** (`SYMBOL=BTC_USDT`)
- Entry: **market** `POST /spot/api/spot/v4/order/place`
- Stop: **code** computes ATR, places KCEX **stop-market** trigger `orderType` 103, `triggerType` `LE`. Recomputed on the real fill price.
- Judge: **code only** (no second LLM)
- LLM: OpenRouter, **one model** in `LLM_MODEL`. Temperature 0, `max_tokens`. Invalid JSON → HOLD with reason `llm_parse`, no order.
- Default `MODE=paper`. Live only if `MODE=live` **and** valid `KCEX_TOKEN`
- Collar: max **20 USDT** / order, **5%** of free USDT, **1** bot position, venue minimum respected. Day-loss halt (realized + unrealized) blocks **new buys**; **SELL still allowed**
- Stale quotes: block **entries**; **SELL/exits still allowed**
- Cycle: `CYCLE_MINUTES` (ops **5**; code default if env missing is **15**). Also wake if price moves `WAKE_MOVE_PCT` (0.4%)
- Market data: **WebSocket** `wss://wbs.kcex.com/ws` (verified). REST is a fallback at `POLL_SECONDS`, never a 1-second poll.
- **Never cancel** order ids that are not in `bot_orders` (protects the user's own orders)
- Do not feed the LLM the socket firehose. Snapshot only.
- No Geetest solver, no disabling 2FA, no silent password login
- No live orders in tests / CI. Mock `KcexClient` for live hands.
- Every audit row carries the snapshot (last/bid/ask/atr) and the LLM reason/cost. Without that the paper run proves nothing.

## Live invariants (bot/hands.py) — keep them, test them

1. The position row is persisted as `PENDING` the moment the entry is accepted, **before** the stop is tried.
2. Fills are confirmed by **BTC balance delta**, not by the order id. Partial fills protect only what was bought; an unfilled entry is cancelled and the bot stays flat.
3. A position never stays quietly unprotected: stop fails → flatten; flatten fails → state `UNPROTECTED`, `UnprotectedPosition` raised, process exits 2.
4. SELL: cancel the resident stop, confirm the cancel on open orders, then sell; a failed sell puts the stop back; an unconfirmed sell is `CLOSING` until `reconcile()` settles it.
5. `reconcile()` runs at boot and every LLM cycle: position gone from the exchange → booked as a fill; stop missing → re-placed; unrestorable → `UnprotectedPosition`.
6. Only bot-created order ids are ever cancelled.

Exit codes: `1` session dead, `2` unprotected position, `3` already running, `4` `--once` cycle failed.

## Auth (already built — do not re-reverse-engineer login)

```bash
PYTHONPATH=. python -m kcex.cli login
```

1. Opens a persistent Chrome profile at `.kcex-profile/` (Playwright, headed).
2. Optionally prefills `KCEX_EMAIL` / `KCEX_PASSWORD`. **Captcha (Geetest) and Google Authenticator stay human.**
3. Watches cookies for `Authorization=WEB…`, checks `user_info`, writes `KCEX_TOKEN` and `KCEX_TOKEN_AT` into `.env` (mode 600).
4. Token lasts ~**7 days** ("stay logged in"). It is **not** a JWT and does **not** refresh silently. The bot warns from day 6.

After that, every private REST call is `Authorization: WEB…`. On 401 the loop **halts** (`SessionDead`, exit 1). Tell the human to re-run the login command.

- Paper: KCEX login **optional**; balances fall back to the paper ledger.
- Do **not** solve Geetest, scrape the login form, or invent a new token flow. Never commit `.env` / `.kcex-profile/`. Never print the full token.

## Eye (market data)

- Socket: `wss://wbs.kcex.com/ws`, channels `spot@public.deals.v3.api@BTCUSDT` (last) and `spot@public.bookTicker.v3.api@BTCUSDT` (bid/ask), `{"method":"PING"}` every 15 s. Real frames in `tests/fixtures/kcex_ws_frames.jsonl`. Symbol has **no underscore** on the socket.
- REST fallback: `poll_quotes()` only when the socket is stale, at most once per `POLL_SECONDS`, and it never raises.
- `poll_heavy()` (klines + balances) runs when the LLM is due. The forming 15-minute bar is dropped before ATR. In live mode a balance read failure raises `EyeError` (the loop backs off) instead of trading on `free_usdt = 0`.
- `GET /uc/user_api/ws_token` exists for the private socket; not used yet.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chrome

PYTHONPATH=. python -m pytest tests -q
PYTHONPATH=. python -m kcex.cli login
PYTHONPATH=. python -m kcex.cli ticker BTC_USDT
PYTHONPATH=. python -m kcex.cli balances
MODE=paper PYTHONPATH=. python -m bot run          # loop
MODE=paper PYTHONPATH=. python -m bot run --once
```

Audit: `sqlite3 data/bot.db "select ts, action, rule, json_extract(payload,'$.llm.reason') from audit order by id desc limit 10;"`

## Tests

- `PYTHONPATH=. .venv/bin/python -m pytest tests -q`
- No live orders in CI. The socket is never opened in tests (`connect` is injected).
- TDD for new collar/risk/hands behavior. Regression tests to keep: ATR gap-to-prev-close; day-loss still allows SELL; stale still allows SELL; every live invariant above; schema migration from the previous `bot.db`.

## Known live risks (do not ignore)

- Place-order JS used `needDolos` / `content-sign`. **No live order has ever been sent through this client.** First real order may be rejected. Probe at the venue minimum only after paper looks sane.
- The private deals payload shape was not captured: entry price falls back to an estimate (`entry_source = "estimated"`) until `avg_fill_from_deals` sees a matching shape.
- Market buy is sent with `quantity`; the web form may use `amount` (quote mode). Untested.
- The WAF rejects curl's User-Agent (406). The client sends a browser UA; the venue can change policy.
- The token is the whole account. Keep the bot on an account with only the capital it should touch.

## Out of scope until the owner asks

Second LLM, ETH or more pairs, limit/IOC entries, Telegram, DSPy training, CCXT as transport, futures (a public futures API exists, see the API doc; not used), automating Geetest/2FA.

## Session snapshot (2026-09-04, safety revision)

- Branch `fix/live-safety-observability` on top of `main` @ `cecbe69`.
- What changed: see README "Histórico" and the spec amendment. Tests: 100+ passing.
- `.env` (local, not in git): `MODE=paper`, `CYCLE_MINUTES=5`, OpenRouter key set, **`KCEX_TOKEN` empty** — live login **not** done.
- Next human-facing work (not started unless asked):
  1. Run paper; read the audit with the LLM reason; measure decisions once there are enough (README "Medir o modelo").
  2. `python -m kcex.cli login` then a live probe at the venue minimum — only if the owner explicitly asks.
  3. Do **not** cancel/modify the user's existing orders.
