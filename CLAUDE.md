# CLAUDE.md

This file is loaded by **Claude Code**. Canonical rules for every agent: **[AGENTS.md](AGENTS.md)**. Read AGENTS.md first; this file is the short operating contract.

## Project in one paragraph

Python bot: OpenRouter LLM decides **BTC/USDT spot on KCEX**; a **code collar** must approve every order. Paper is a local SQLite ledger on **live KCEX prices** (the exchange has no demo account). Live uses web session `KCEX_TOKEN` (~7 days), not HMAC OpenAPI. Owner: Alisson. First live size: the venue minimum.

## Do

- Keep `kcex/` as the **only** exchange client (no CCXT). GET may retry; **POST/DELETE never retry**.
- Keep LLM output as `{action, confidence, reason, regime}` only. Size and stop live in `bot/collar.py`.
- Keep the six live invariants in `bot/hands.py` (persist before stop, confirm by balance, never quietly unprotected, cancel-confirm-sell, reconcile, only own ids). Add a test for any change there.
- Never `cancel_order` unless `store.is_bot_order(id)`.
- Default paper. Live is an explicit `MODE=live` **plus** `python -m kcex.cli login`. Do not flip live unless the human asks.
- **Do not re-implement login.** `python -m kcex.cli login` already opens Chrome, waits for captcha+2FA, and writes `KCEX_TOKEN` + `KCEX_TOKEN_AT` to `.env`.
- On live 401: halt (`SessionDead`, exit 1). On `UnprotectedPosition`: halt (exit 2) and tell the human to fix the exchange by hand.
- Every audit row keeps the snapshot and the LLM reason/cost. Do not remove that.
- Follow [docs/kcex-spot-api.md](docs/kcex-spot-api.md) for endpoints and the socket. If a path was not captured, say so — do not guess.

## Do not

- Poll REST every second again; the socket is the price source, REST is the fallback at `POLL_SECONDS`.
- Solve Geetest or automate Google 2FA.
- Add a second "judge" model, extra pairs, Telegram, or futures unless asked.
- Commit `.env`, `.kcex-profile/`, `data/`, or print secrets.
- Place live orders from tests or CI.
- Cancel or modify orders the bot did not create.

## Layout

| Path | Job |
| --- | --- |
| `bot/eye.py` | Socket first, REST fallback, klines, balances, symbol rules |
| `bot/ws.py` | KCEX public socket (MEXC v3 protocol) |
| `bot/brain.py` | OpenRouter, one model, named failure reasons, real cost |
| `bot/collar.py` | Risk gate (20 USDT, 5%, ATR, 1 position, venue minimum, day-loss incl. unrealized) |
| `bot/hands.py` | PaperHands / LiveHands (live invariants) |
| `bot/cycle.py` | One loop step + audit row |
| `bot/cli.py` | Loop, lock, log, exit codes |
| `bot/store.py` | SQLite `data/bot.db` with migrations |
| `kcex/client.py` | Reverse-engineered REST |
| `kcex/login.py` | Playwright session capture |
| `docs/kcex-spot-api.md` | Endpoint + socket notes |

## Run

```bash
PYTHONPATH=. python -m pytest tests -q
PYTHONPATH=. python -m bot run --once          # paper, one LLM cycle
PYTHONPATH=. python -m bot run                 # paper loop
PYTHONPATH=. python -m kcex.cli login          # human captcha + 2FA
```

Paper without KCEX login uses `PAPER_STARTING_USDT` (default 450) once, then its own ledger. Prices come from the KCEX public socket.

## Resume (2026-09-04, safety revision)

- Branch `fix/live-safety-observability` over `main` @ `cecbe69`. Live login **not** in `.env`.
- Ops: `CYCLE_MINUTES=5`, model `deepseek/deepseek-v4-flash-0731`.
- Next: run paper, read the audit (`json_extract(payload,'$.llm.reason')`), measure decisions, live only if asked.
- Full snapshot: [AGENTS.md](AGENTS.md) § Session snapshot.
