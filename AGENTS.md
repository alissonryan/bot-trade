# AGENTS.md — bot-trade

Instructions for coding agents (Claude Code, Cursor, Codex, Grok, etc.). **Read this before changing anything.** Claude Code also has [CLAUDE.md](CLAUDE.md) — keep both in sync.

## What this repo is

Autonomous **BTC/USDT spot** bot on **KCEX**. An LLM (via OpenRouter) proposes `BUY` / `SELL` / `HOLD`. **Code** sizes the order, attaches an ATR stop, and either simulates (paper) or sends a market order (live).

KCEX has **no official HMAC OpenAPI** we found (`/user/openapi` 404) and **no paper/demo account**. Paper is a local SQLite ledger on **live public prices**. Live uses a **web session token** (`KCEX_TOKEN`), not an exchange API key.

Owner currently trades manually and wants the bot to replace that loop. First live collar is tiny on purpose.

## Read first

| File | Why |
| --- | --- |
| [README.md](README.md) | How to run |
| [docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md](docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md) | Product decisions (some defaults drifted — see below) |
| [docs/kcex-spot-api.md](docs/kcex-spot-api.md) | Reverse-engineered REST and **WS gaps** |
| [docs/superpowers/plans/2026-09-04-kcex-llm-spot-bot.md](docs/superpowers/plans/2026-09-04-kcex-llm-spot-bot.md) | Implementation plan (**already executed**, 11 tasks) |

Spec vs code: the spec still says “15 min cycle” and “WS primary”. **Ops and `.env.example` use 5 minutes.** **v1 eye is REST poll.** Follow this file + code, not the stale spec lines.

## Architecture (do not collapse)

```
Eye (REST poll 1s, optional WS later) → Snapshot
Brain (OpenRouter, 1 model) → TradeIntent JSON
Collar (pure rules) → GateResult
Hands: PaperHands (SQLite ledger) | LiveHands (KCEX REST)
Store: data/bot.db audit + fills + bot order ids + position
```

| Path | Role |
| --- | --- |
| `kcex/` | Venue client + Chrome login. **Do not replace with CCXT.** |
| `kcex/client.py` | Reverse-engineered REST |
| `kcex/login.py` | Playwright session capture → `KCEX_TOKEN` |
| `bot/eye.py` | REST poll (`poll_quotes` every loop; `poll_heavy` when LLM due). `apply_frame` exists for future WS. |
| `bot/brain.py` | OpenRouter. Output `{action, confidence, reason, regime}` only |
| `bot/collar.py` | Risk gate: size, ATR stop, one position, day-loss |
| `bot/hands.py` | `PaperHands` / `LiveHands` |
| `bot/cycle.py` | Timer + wake-on-move; `SessionDead` on live 401 |
| `bot/store.py` | SQLite |
| `bot/atr.py` | True range including gap-to-previous-close |
| `bot/settings.py` | Env → `Settings` (`from_env`) |
| `data/bot.db` | Runtime DB (gitignored via `data/`) |
| `.env` | Secrets — **never commit, never print** |

## Locked decisions (v1)

- Pair: **BTC/USDT only** (`SYMBOL=BTC_USDT`)
- Entry: **market** `POST /spot/api/spot/v4/order/place`
- Stop: **code** computes ATR, places KCEX **stop-market** trigger `orderType` 103, `triggerType` `LE`. Live: retry trigger **twice**, then **flatten**.
- Judge: **code only** (no second LLM)
- LLM: OpenRouter, **one model** in `LLM_MODEL`. Temperature 0. Invalid JSON → HOLD, no order.
- Default `MODE=paper`. Live only if `MODE=live` **and** valid `KCEX_TOKEN`
- Collar: max **20 USDT** / order, **5%** of free USDT, **1** bot position. Day-loss halt blocks **new buys**; **SELL still allowed**
- Stale quotes: block **entries**; **SELL/exits still allowed**
- Cycle: `CYCLE_MINUTES` (ops **5**; code default if env missing is **15**). Also wake if price moves `WAKE_MOVE_PCT` (0.4%)
- **Never cancel** order ids that are not in `bot_orders` (protects the user’s existing BTC trigger: historically **0.00064 BTC @ 75722**)
- Do not feed the LLM the WS firehose. Snapshot only.
- No Geetest solver, no disabling 2FA, no silent password login
- No live orders in tests / CI. Mock `KcexClient` for live hands.

## Auth

- Login: `PYTHONPATH=. python -m kcex.cli login` (Playwright Chrome profile `.kcex-profile/`). Human does captcha + Google Authenticator. Token `WEB…` written to `.env` as `KCEX_TOKEN`. ~7 days if “stay logged in” was checked.
- `GET /uc/user_api/user_info` **requires** the `authorization` header. Cookie alone → 401. Login/validation does **not** rotate the token.
- Paper: OpenRouter key required for decisions. KCEX login **optional**. If balances fail or are zero, paper uses `PAPER_STARTING_USDT` (default **450**).
- Live: `require_live_token()`. On 401 the loop **halts** (`SessionDead`). Tell the human to re-run login.
- Optional `KCEX_EMAIL` / `KCEX_PASSWORD` only prefill the form.
- Never commit `.env`. Never print tokens or API keys.

## Eye (market data)

- v1 production path: **REST poll**. `poll_quotes()` (ticker+depth) every loop second; `poll_heavy()` (kline+balances) when the LLM is due.
- `KCEX_WS_URL` is **unset**. `Eye.connect_ws` is a no-op without it. **Do not invent a `wss://` host.** Capture from a logged-in Chrome session, then document in `docs/kcex-spot-api.md`.
- `GET /uc/user_api/ws_token` exists (short-lived, sockets only). REST trading does not use it.
- KuCoin/OKX/LLM-Auto-Trader prefer WS; we poll until KCEX WS is mapped.

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

Audit: `sqlite3 data/bot.db "select ts, action, rule from audit order by id desc limit 10;"`

## Tests

- `PYTHONPATH=. .venv/bin/python -m pytest tests`
- No live orders in CI.
- TDD for new collar/risk behavior.
- Known regression tests to keep: ATR gap-to-prev-close; day-loss still allows SELL.

## Known live risks (do not ignore)

- Place-order JS used `needDolos` / `content-sign`. First real order may be rejected. Probe with **20 USDT** only after paper looks sane.
- Live position is persisted in SQLite; reconcile with REST open-orders if you touch execution.
- Paper fills are optimistic (mid/ask + bps slip).
- First paper LLM run (2026-09-04) returned **HOLD** (range ~79.5k). That is expected, not a crash.

## Out of scope until the owner asks

Second LLM, ETH or more pairs, limit/IOC entries, Telegram, DSPy training, CCXT as transport, futures, automating Geetest/2FA.

## Session snapshot (2026-09-04)

Use this to resume. Update this section when ops change.

- Git: branch `main`, HEAD `c8f0100` (`fix: give paper mode virtual USDT when KCEX balance is zero`). Feature branch `feat/kcex-llm-spot-bot` was merged and deleted.
- All 11 plan tasks implemented. Review blockers (frozen REST snapshot, RAM-only live position, no live PnL, LLM budget not charged, 401 not halt, trigger fail flatten-immediately) were fixed in `fe4e56d` plus follow-ups.
- Tests: **36 passed** at merge.
- `.env` (local, not in git): `MODE=paper`, `CYCLE_MINUTES=5`, OpenRouter key set, `LLM_MODEL=deepseek/deepseek-v4-flash-0731`, **`KCEX_TOKEN` empty** — live login **not** done.
- Paper loop was started with `PYTHONPATH=. .venv/bin/python -m bot run`. Check if still running before starting another.
- Next human-facing work (not started unless asked):
  1. Keep paper running / inspect `data/bot.db` for BUY vs HOLD.
  2. Capture KCEX WS URL from Chrome (do not invent).
  3. `python -m kcex.cli login` then a **tiny** live probe (20 USDT) — only if the owner explicitly asks.
  4. Do **not** cancel/modify the user’s existing 0.00064 BTC stop at 75722.
