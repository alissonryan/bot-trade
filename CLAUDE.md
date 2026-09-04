# CLAUDE.md

This file is loaded by **Claude Code**. Canonical rules for every agent: **[AGENTS.md](AGENTS.md)**. Read AGENTS.md first; this file is the short operating contract.

## Project in one paragraph

Python bot: OpenRouter LLM decides **BTC/USDT spot on KCEX**; a **code collar** must approve every order. Paper is a local SQLite ledger on **live KCEX prices** (the exchange has no demo account). Live uses web session `KCEX_TOKEN` (~7 days), not HMAC OpenAPI. Owner: Alisson. First live size: **20 USDT**.

## Do

- Keep `kcex/` as the **only** exchange client (no CCXT).
- Keep LLM output as `{action, confidence, reason, regime}` only. Size and stop live in `bot/collar.py`.
- Never `cancel_order` unless `store.is_bot_order(id)`. Protect the user’s existing stop (0.00064 BTC @ 75722).
- Default paper. Live is an explicit `MODE=live` **plus** `python -m kcex.cli login`. Do not flip live unless the human asks.
- **Do not re-implement login.** `python -m kcex.cli login` already opens Chrome, waits for captcha+2FA, and writes `KCEX_TOKEN` to `.env`. HTTP internals: `docs/kcex-spot-api.md`.
- On live 401: halt (`SessionDead`) and tell them to re-run that same command. Do not retry forever.
- Add tests under `tests/bot/` for collar / hands / cycle changes. TDD for risk.
- Follow [docs/kcex-spot-api.md](docs/kcex-spot-api.md) for endpoints. If a path was not captured, say so — do not guess.

## Do not

- Invent `KCEX_WS_URL` / `wss://` hosts. Capture from Chrome, then document.
- Solve Geetest or automate Google 2FA.
- Add a second “judge” model, extra pairs, Telegram, or futures unless asked.
- Commit `.env`, `.kcex-profile/`, `data/`, or print secrets.
- Place live orders from tests or CI.
- Cancel or modify orders the bot did not create.

## Layout

| Path | Job |
| --- | --- |
| `bot/eye.py` | REST poll (+ `apply_frame` for future WS) |
| `bot/brain.py` | OpenRouter, one model |
| `bot/collar.py` | Risk gate (20 USDT, 5%, ATR, 1 position) |
| `bot/hands.py` | PaperHands / LiveHands |
| `bot/cycle.py` | 5-min timer (ops) + 0.4% wake |
| `bot/store.py` | SQLite `data/bot.db` |
| `kcex/client.py` | Reverse-engineered REST |
| `kcex/login.py` | Playwright session capture |
| `docs/kcex-spot-api.md` | Endpoint notes + WS gap |
| `docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md` | Spec (cycle/WS lines may be stale; AGENTS.md wins) |

## Run

```bash
PYTHONPATH=. python -m pytest tests -q
PYTHONPATH=. python -m bot run --once          # paper, one LLM cycle
PYTHONPATH=. python -m bot run                 # paper loop
PYTHONPATH=. python -m kcex.cli login          # human captcha + 2FA
```

Paper without KCEX login uses `PAPER_STARTING_USDT` (default 450). Prices still come from KCEX public REST.

## Resume (2026-09-04)

- `main` @ `c8f0100`. Plan fully implemented. Paper works; live login **not** in `.env`.
- Ops: `CYCLE_MINUTES=5`, model `deepseek/deepseek-v4-flash-0731`.
- Next: inspect paper audit, map WS from Chrome, live only if asked.
- Full snapshot: [AGENTS.md](AGENTS.md) § Session snapshot.
