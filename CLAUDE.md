# CLAUDE.md

This file is loaded by **Claude Code**. Canonical rules for every agent: **[AGENTS.md](AGENTS.md)**. Read AGENTS.md first; this file is the short operating contract.

## Project in one paragraph

Python bot: OpenRouter LLM decides **BTC/USDT spot on KCEX**; a **code collar** must approve every order. Paper is a local SQLite ledger on **live KCEX prices** (the exchange has no demo account). Live uses web session `KCEX_TOKEN` (~7 days), not HMAC OpenAPI. Owner: Alisson. First live size: the venue minimum.

## Do

- Keep `kcex/` as the **only** exchange client (no CCXT). GET may retry; **POST/DELETE never retry**.
- Keep LLM output as `{action, confidence, reason, regime}` only. Size and stop live in `bot/collar.py`, keyed off `executable_entry_price()` (best ask, or `last` when ask is missing/invalid, marked up by slippage) — the same price `PaperHands` actually fills at.
- Keep the six live invariants in `bot/hands.py` (persist before stop, confirm by balance, never quietly unprotected, cancel-confirm-sell, reconcile, only own ids). Add a test for any change there. Invariant 5's balance-delta attribution is corroboration, not identification — an offsetting owner deposit/withdrawal or a lagging balance read can fool it; keep it (it is the only autonomous-stop-fill detector) but do not present it as sound (see AGENTS.md § Live invariants, point 5).
- Never `cancel_order` unless `store.is_bot_order(id)`.
- Default paper. Live is an explicit `MODE=live` **plus** `python -m kcex.cli login`. Do not flip live unless the human asks.
- **Do not re-implement login.** `python -m kcex.cli login` already opens Chrome, waits for captcha+2FA, and writes `KCEX_TOKEN` + `KCEX_TOKEN_AT` to `.env`.
- On live 401: halt (`SessionDead`, exit 1). On `UnprotectedPosition`: halt (exit 2) and tell the human to fix the exchange by hand.
- Every audit row keeps the snapshot, the LLM reason/cost, and the exact sanitized request body a decision actually used. Do not remove that.
- Follow [docs/kcex-spot-api.md](docs/kcex-spot-api.md) for endpoints and the socket. If a path was not captured, say so — do not guess.

## Do not

- Invent any *other* `wss://` host. The public spot WS is confirmed (`wss://wbs.kcex.com/ws?platform=web`, `kcex.ws.DEFAULT_WS_URL`) and is the default when `KCEX_WS_URL` is unset. A different endpoint must be captured from Chrome first, then documented.
- Bind the local chart (`--chart`) off loopback. `bot/chart_server.py` hard-rejects anything but `127.0.0.1`/`localhost`/`::1`.
- Poll REST every second again; the socket is the price source, REST is the fallback at `POLL_SECONDS`.
- Solve Geetest or automate Google 2FA.
- Add a second "judge" model, extra pairs, Telegram, or **live** futures unless asked. Futures **paper** lives in `fut/` (spec `docs/superpowers/specs/2026-09-17-kcex-futures-paper-jev-llm-design.md`): public `/fapi` reads only, never a private route or `KCEX_TOKEN`.
- Commit `.env`, `.kcex-profile/`, `data/`, or print secrets.
- Place live orders from tests or CI.
- Cancel or modify orders the bot did not create.

## Layout

| Path | Job |
| --- | --- |
| `bot/eye.py` | Socket first (via `bot/hub.py::Hub`), REST fallback, klines, balances (live only), symbol rules |
| `bot/hub.py` | Shared in-process holder of the latest WS tick |
| `kcex/ws.py` | Public WS client — parses frames, ping, `DEFAULT_WS_URL` (the only WS client) |
| `bot/chart_server.py` | Loopback-only local chart HTTP+WS server (`--chart`) |
| `bot/brain.py` | OpenRouter, one model, named failure reasons, real cost |
| `bot/collar.py` | Risk gate (20 USDT, 5%, ATR, 1 position, venue minimum, day-loss incl. unrealized); sizes/stops off the executable entry price, not raw `last` |
| `bot/hands.py` | PaperHands / LiveHands (live invariants) |
| `bot/cycle.py` | One loop step + audit row |
| `bot/cli.py` | Loop, lock, log, exit codes |
| `bot/store.py` | SQLite `data/bot.db` with migrations |
| `kcex/client.py` | Reverse-engineered REST |
| `kcex/login.py` | Playwright session capture |
| `docs/kcex-spot-api.md` | Endpoint notes + confirmed public WS |
| `docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md` | Spec (cycle line may be stale; AGENTS.md wins) |
| `kcex/fws.py`, `kcex/fapi.py` | Public futures WS (incremental book) and REST — GET only, no auth |
| `fut/` | Futures paper: Jev trigger → LLM decision → collar → paper ledger, shadow baselines, report |
| `docs/kcex-futures-api.md` | Captured public futures endpoints and frames |

## Run

```bash
./scripts/test                                  # the whole suite, under the project venv
PYTHONPATH=. python -m bot run --once          # paper, one LLM cycle
PYTHONPATH=. python -m bot run                 # paper loop
PYTHONPATH=. python -m bot run --chart         # paper loop + local chart at http://127.0.0.1:8765/
PYTHONPATH=. python -m kcex.cli login          # human captcha + 2FA
PYTHONPATH=. python -m fut run                  # futures paper loop (Jev every 2 s, LLM on wake)
PYTHONPATH=. python -m fut report [--since-ms N] # edge criterion vs flat/jev_only/random baselines
PYTHONPATH=. python -m fut panel                # read-only local panel at http://127.0.0.1:8766/
```

Paper without KCEX login uses `PAPER_STARTING_USDT` (default 450) once, then its own ledger. Prices come from the confirmed public KCEX WS by default, with REST as fallback when WS is down or stale (`KCEX_WS_URL=-` forces REST-only). `--chart` serves a read-only local candlestick chart; it never binds off loopback and never opens a second KCEX connection.

## P1 barriers

TDD is mandatory for collar/hands changes. See AGENTS.md § P1 for the canonical contract: opt-in `TP_ATR_MULT`/`TIME_LIMIT_MINUTES`, target recomputed on fills, persistent entry age, shared pre-LLM tick hook, and **one resident LE stop only** (GE exists but OCO is unproven). TP/TTL are local; synchronous LLM/HTTP calls and downtime leave gaps, so checks are not guaranteed every second. P0 samples local exits only at Min15 opens; do not infer local TP fills from candle highs. Trailing is deferred to P6 with coordinator approval.

## Paper/live isolation

Each mode gets its own database (`bot/cli.py::db_path_for_mode`): paper keeps `data/bot.db`, live gets `data/bot-live.db`. `Store(path, mode=...)` stamps the owning mode and raises `StoreIdentityMismatch` on a mismatched open; `LiveHands` additionally refuses any position row with `paper` provenance. An unstamped legacy database may be adopted by paper, never by live. **Account switching is not covered** — the token rotates weekly and no account id is captured, so treat one live database as belonging to one account by hand. `bot/cli.py::main` holds `data/bot.lock` (shared by both modes) BEFORE constructing anything with a side effect, including file logging; a platform with no `fcntl` fails closed instead of running unlocked. `DATA_DIR`/`DB_PATH`/`LOCK_PATH`/`LOG_PATH` are fixed to this checkout's own location, not the process cwd. See AGENTS.md § Paper/live isolation.

## P2 cooldown

`COOLDOWN_MINUTES=0` is opt-out. Positive durations gate BUY only, armed by the last persisted **losing** SELL fill (`pnl < 0`) and wall-clock decision time. A profitable exit does not arm it — blocking a continuation after a win only cancels profit, which is why Rafael Vargas retired the post-any-exit form (Apex Brief v17, Rule 3). SELL never queries cooldown history, and LLM scheduling is unchanged. No hands/live order changes. See AGENTS.md § P2 for restart, reconciliation, legacy timestamps, conservative intrabar replay timing, and the tiny-sample/no-evidence measurement limitation.

## P4 journal

`JOURNAL_ENABLED=0` is opt-out. See AGENTS.md § P4: persistent BUY/fill linkage, null-PnL non-execution, independent outcome/reflection knowledge timestamps and mandatory final prompt `as_of` filter. At most five lessons/400-char reflections; one 160-token reflection after decision AND execution shares the daily Budget with an estimated next-decision reserve. The daily Budget is durable per mode/database: `Store.reserve_budget()` commits atomically BEFORE the HTTP dispatch and `Store.settle_budget()` trues it up after, so a same-day restart resumes spend and a corrupt/unreadable row blocks new spend instead of resetting to zero; a timeout/network failure also reserves the fallback cost instead of charging $0. Not an account identity or provider-wide cap. No reflection is a judge or order controller. P0 offline skips reflection explicitly; fixed cached intents cannot measure learning, and changed lesson payloads must miss old cache keys. Anti-look-ahead, migration, budget-priority and isolated-process opt-out tests are required.

## Futures paper (fut/)

Paper only, BTC_USDT perpetual, leverage 1x default and 3x hard cap, isolated margin, market orders at book ± slippage with the venue taker fee. Jev (`typesafe-sdk`, `FUT_JEV_MODEL`, `TYPESAFE_API_KEY`) evaluates every 2 s; `fut/questions.py` holds every question and threshold. A wake calls the LLM immediately, one call at a time, 10 s cooldown after HOLD or an LLM failure/non-decision; late or price-moved LONG/SHORT are discarded, CLOSE never is. Stop, 5 min max hold and liquidation (by `fairPrice`) are enforced every step; opt-in `FUT_MIN_HOLD_SECONDS` (default 0) only suppresses Jev exit/reversal wakes right after entry. Own DB `data/futures-paper.db` (mode `futures-paper`), own lock `data/futures.lock`. Exit codes: 3 already running, 8 unmonitored position, 9 DB of another mode. Sessions with the mock Jev never count toward the edge criterion; passing the criterion only allows writing a live spec.
FUT_WAKE_THRESHOLD is a first guess to be tuned from paper data, never from the edge-criterion window. The opt-in `FUT_MAX_SPREAD_BPS`, `FUT_MIN_MOVE_MULT`, `FUT_MAX_ENTRIES_PER_HOUR`, `FUT_WAKE_STREAK`, and `FUT_WAKE_REGIMES` settings apply to the main and shadow wake/collar paths, and `python -m fut wakegrid [--since-ms N]` is a read-only, in-sample replay only. Run the fixed edge criterion with `python -m fut report --since-ms N` at the start of a pre-registered window with fixed settings; tuning windows never count toward the criterion.

`python -m fut panel` serves a read-only page (plain Portuguese: price, position, narrated decisions, day result, trades, shadow scoreboard) for whoever is watching. It is a separate process: `mode=ro` SQLite connections opened and closed per read, never `FutStore`, no `futures.lock`, no `.env`, no KCEX/Jev/LLM call, GET only, and it hard-rejects any host but `127.0.0.1`/`localhost`/`::1`. The price is the snapshot the bot already logs (~2.5 s), not a second exchange connection; the position card is an estimate, the ledger fills are the record.

## Resume (2026-09-04, safety revision)

- `main` @ `d6ae9c9` (public WS + local chart) merged with PR #1 `fix/live-safety-observability` (live invariants, fill-by-balance, reconcile, observability). Paper works; live login **not** in `.env`.
- Ops: `CYCLE_MINUTES=5`, model `deepseek/deepseek-v4-flash-0731`.
- Next: run paper, read the audit (`json_extract(payload,'$.llm.reason')`), measure decisions, live only if asked. WS mapping is **done** — see `docs/kcex-spot-api.md`.
- Full snapshot: [AGENTS.md](AGENTS.md) § Session snapshot.
