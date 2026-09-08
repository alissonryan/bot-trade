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
| [docs/kcex-spot-api.md](docs/kcex-spot-api.md) | Reverse-engineered REST **and the confirmed public WS** |
| [docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md](docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md) | Product decisions; the amendment at the end lists what changed on 2026-09-04 |
| [docs/superpowers/plans/2026-09-04-kcex-llm-spot-bot.md](docs/superpowers/plans/2026-09-04-kcex-llm-spot-bot.md) | Original implementation plan (executed; code has moved on since) |

Spec vs code: the spec still says “15 min cycle”. **Ops and `.env.example` use 5 minutes.** **v1 eye is public WS + REST fallback** (WS primary, confirmed and wired — see below). Follow this file + code, not the stale spec lines.

## Architecture (do not collapse)

```
Eye (WebSocket miniTicker+deals+bookTicker via Hub; REST fallback every POLL_SECONDS) → Snapshot
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
| `kcex/ws.py` | **The only WS client.** Parses `miniTicker`/`deals`/`bookTicker` frames, ping loop, `DEFAULT_WS_URL` |
| `bot/hub.py` | In-process holder of the latest WS tick (`last`/`bid`/`ask`/`ts_ms`/`depth_ts_ms`/`ws_ok`), shared by `Eye` and `ChartServer` |
| `bot/eye.py` | Socket first (via `Hub`), REST fallback that never raises; klines (forming bar dropped), balances, symbol rules |
| `bot/chart_server.py` | Loopback-only local HTTP+WS chart server (`--chart`) — REST kline + live WS ticks, read-only |
| `bot/brain.py` | OpenRouter. Output `{action, confidence, reason, regime}` only; every failure has a named reason |
| `bot/collar.py` | Risk gate: size, ATR stop, one position, day-loss (realized + unrealized), min confidence, venue minimum |
| `bot/hands.py` | `PaperHands` / `LiveHands` — see **Live invariants** |
| `bot/cycle.py` | One loop step; audit row with snapshot, LLM outcome, order ids, position state |
| `bot/cli.py` | Loop with backoff, instance lock, file log, exit codes |
| `bot/store.py` | SQLite with forward migrations (ALTER TABLE) |
| `bot/atr.py` | Simple mean of the last N true ranges (not Wilder); gap-to-previous-close included |
| `bot/settings.py` | Env → `Settings` (`from_env`) |
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
2. Fills are confirmed by **BTC balance delta**, not by the order id. Partial fills protect only what was bought.
   **The row is deleted only on proof the entry did not fill** — it was still resting on the book and we cancelled it. A balances outage, or an entry that already left the book, is *ambiguous*: the row stays `PENDING` and `reconcile()` settles it. `_watch_balance` returns `None` for "not one reading succeeded" precisely so ignorance cannot be mistaken for "nothing filled".
3. A position never stays quietly unprotected: stop fails → flatten; flatten fails → state `UNPROTECTED`, `UnprotectedPosition` raised, process exits 2.
4. SELL: cancel the resident stop, confirm the cancel on open orders, then sell; an unconfirmed sell is `CLOSING` until `reconcile()` settles it.
   A **failed** sell re-reads the balance *before* putting the stop back — the POST is never retried, so the sell may in fact have executed, and a stop for BTC we no longer own would sit on the owner's own coins.
   If the resident stop is not a recorded bot order, `cancel_if_ours` can never cancel it: that raises `PositionStuck` (exit 5) instead of aborting the exit silently on every future cycle.
5. `reconcile()` runs at boot and every LLM cycle. It never trusts the local row alone:
   - **Flat locally** → it still queries the exchange. `foreign_btc` (kv) is the BTC that is not ours, seeded on the first flat reconcile and re-baselined downwards while flat. An *increase* while flat is an entry that filled without being recorded → `UnprotectedPosition`.
   - **Open locally** → the account is judged by how much BTC is *missing* against `btc_before + qty`. Missing ≈ our own size → our exit, booked. Missing = some other amount → the **owner** moved their coins (during the 2026-09 capture the account held their own 0.00064 stop; see the account-state note below): the position is treated as still open and the foreign baseline is re-set. Guessing the other way orphans a real position and lets the next BUY stack a second one.
   - A `PENDING` row whose entry never filled is dropped **without** booking a fill — inventing a SELL there writes a fabricated PnL into the ledger.
   - **Honest scoping (2026-09 M1 design review, sharpened by the 2026-09 re-review):** "missing ≈ our own size" is *corroboration*, not *identification* — but code does not read the difference off the page. In `reconcile()` this heuristic alone still **authorises action**: booking a SELL fill, clearing the position row, and (separately) restoring a stop, with no other confirmation gate in between. Calling it "corroboration" in prose while the code treats it as sufficient proof does not make it one; an owner deposit/withdrawal that happens to equal the bot's own quantity produces the exact same observable delta as our own exit, and a lagging balance read can do the same — no threshold and no extra read recovers the missing causal information. A concrete cost of leaving it live: an owner withdrawal that happens to equal the bot's own size, with the resident stop still open on the exchange, is read as "our stop fired" and the row is deleted anyway — contradicting evidence (a live stop) does not stop it. Closing this gap needs the same captured order-history/deals terminal evidence the M1 exit latch (`bot/hands.py::EXIT_LATCH_KEY`, `TerminalEvidenceUnavailable`) is waiting on. The safe direction, when this heuristic and reality disagree, is to **stop on uncertainty** — halt and ask a human — not to keep resolving it silently in the direction that lets the loop keep running, and not to remove the detector either: it is still the only autonomous-stop-fill detector, and dropping it would leave a real exchange-side fill undetected, which is worse. It is kept anyway, deliberately, as a documented residual risk, not a live bug being exploited today — but "corroboration" must not be read as "this can no longer authorise a write on its own", because today it still does.
6. Only bot-created order ids are ever cancelled.

Exit codes: `1` session dead, `2` unprotected position, `3` already running, `4` `--once` cycle failed, `5` stuck position (protected, but the bot cannot exit it — the resident stop is not a bot order; square it by hand), `6` terminal evidence unavailable (`TerminalEvidenceUnavailable`: a live discretionary exit — LLM SELL, local take-profit, time limit — refused before any write, because a cancel of the resident stop cannot yet be proven safe; this attempt changed no orders, but that alone does not prove a resident stop is still protecting the position — confirming protection requires inspecting the exchange directly; capture the missing order-history/deals evidence, or inspect and exit by hand), `7` exit latch blocked (`ExitLatchBlocked`: a durable EXIT-latch record — a foreign/future writer, or one of ours mid-resolution — is present, the position is a legacy `CLOSING` row with no latch, the latch is corrupt/mismatched, or the guard itself could not read storage; every write path fences until a human resolves it — this is not auto-resumable and no timeout manufactures the missing proof), `8` write storm halt (`WriteStormHalt`, § P5: `KILL_WRITES_PER_HOUR` writes seen in the last rolling hour; raised at the cycle barrier before any write in the cycle and before the LLM, never mid-write; not auto-resumable in the sense that the process always exits and a human must look, but the underlying count IS a rolling window, not a latch — it self-clears roughly an hour after the last recorded write, so a restart made well after the burst can succeed on its own; a restart made immediately will very likely re-halt on the same count, and — because `bot/cli.py::_loop`'s boot `reconcile()` can itself issue a real `place_trigger` repair before the barrier is ever reached — each such restart still costs exactly one more write, so a supervisor that restarts immediately on every exit 8 bleeds one write per restart rather than stopping; read `order_writes` and confirm protection on the exchange by hand before restarting). `5`, `6`, `7` and `8` are deliberately distinct, one code per operator action: `5` means a *foreign* stop can never be cancelled by this bot; `6` means the bot has not started an exit and refuses to, because it cannot yet prove a cancel of *any* stop would succeed rather than execute a moment before/after the read; `7` means an exit attempt (or a state indistinguishable from one) is already mid-flight or ambiguous, and the thing needing inspection is that in-flight state itself, not just the resident stop; `8` means the process itself has been writing too often and stops making any more writes until a human looks, independent of whether any single write was wrong.

**Exit 6's operational contract, corrected (2026-09 third-round re-review):** an earlier version of this document implied a genuinely missing stop would simply be repaired on the next LLM cycle. That was false, and is not how this halt behaves. `bot/cycle.py::run_once` runs the barrier tick (the code path that can raise `TerminalEvidenceUnavailable`) **before** `poll_heavy()` and the LLM section of the *same* cycle; when it raises, `_loop` returns exit 6 and the process **exits**. There is no next cycle — not the next LLM cycle, not another `reconcile()` — to fix a missing stop after that; only a human restart, after manual inspection, resumes anything. Do **not** reintroduce the unsafe POST to shrink this window — refusing with no proof of safety is the correct call — but do not describe the wait as bounded either: if the resident stop is in fact gone, the position can sit on the exchange completely unprotected for as long as it takes a human to notice, with no timer anywhere shortening that. `LiveHands.last_stop_observation` carries the one cheap, honest fact `reconcile()` actually saw the last time it looked (stop present in the open-order book, stop absent, or `"unknown"` when no observation was made, e.g. a pending stop replacement) into the halt message — never inferred when there is no observation.

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

- v1 production path: **public WS + REST fallback**. `KCEX_WS_URL` defaults (when unset) to the confirmed `wss://wbs.kcex.com/ws?platform=web` (`kcex.ws.DEFAULT_WS_URL`, also reachable at `wbs.kcex.io`). `Eye.start_ws_thread()` spawns one daemon thread owning the process's single `PublicSpotWs` connection, feeding ticker/deal/depth events into the shared `bot/hub.py::Hub`, reconnecting with a 2s backoff on error.
- `Eye.poll_quotes()` calls `sync_hub()` first and only falls back to REST ticker+depth when WS is down or **stale** (no fresh frame within `STALE_MS`, default 30000ms — this covers both a hard socket error and a WS thread that silently stops producing frames). `poll_heavy()` (kline+balances) is unchanged and always REST.
- **Do not invent any *other* `wss://` host.** This one was captured from Chrome and confirmed working; if you need a different endpoint, capture it from a logged-in Chrome session first, then document in `docs/kcex-spot-api.md`.
- `KCEX_WS_URL=-` forces REST-only (empties `ws_url`, `start_ws_thread` no-ops). Chart candles are still REST kline — no kline-over-WS.
- `GET /uc/user_api/ws_token` exists (short-lived, private/authenticated socket use case). **Not used** by the public WS — the public channels need no auth at all.
- `python -m bot run --chart` starts a **loopback-only** local chart server (`bot/chart_server.py`) at `http://127.0.0.1:8765/` (host/port from `CHART_HOST`/`CHART_PORT`) — a read-only price/candlestick view (REST `/kline` seed + WS `/ws` live ticks re-encoded as our own JSON, never the raw KCEX frame). No buy/sell controls. It never opens a second KCEX WS connection — it only reads the shared `Hub`. **Do not bind it off loopback** (`require_loopback` hard-rejects anything but `127.0.0.1`/`localhost`/`::1`); if the port is already bound, `--chart` prints the error and exits 1 rather than falling back to another port.
- Channels (`kcex.ws.subscribe_message`): `spot@public.miniTicker@BTC_USDT@UTC+0` and `spot@public.aggre.deals@BTC_USDT` for `last`, plus `spot@public.bookTicker.v3.api@BTCUSDT` for bid/ask. KCEX accepts both the legacy and the MEXC-v3 naming on the same socket (verified live, all five channels ack and stream); bookTicker wins for top of book because it is a ~90-byte frame with the exact best bid/ask instead of the whole ladder rounded to 0.01. The v3 channel takes the symbol with **no underscore**; `_unws_symbol()` restores it on parse.
- REST fallback: `poll_quotes()` only when the socket is stale, at most once per `POLL_SECONDS`, and it **never raises**. Depth staleness is tracked separately from ticker staleness, so bid/ask cannot freeze behind a healthy ticker feed; a failed depth top-up degrades rather than killing the loop.
- `poll_heavy()` (klines + balances) runs when the LLM is due and is always REST. The forming 15-minute bar is dropped before ATR. In live mode a balance read failure raises `EyeError` (the loop backs off) instead of trading on `free_usdt = 0`.
- `GET /uc/user_api/ws_token` exists (short-lived, private/authenticated socket use case). **Not used** — the public channels need no auth at all.

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

## P1 — local take-profit and time limit (opt-in)

- `TP_ATR_MULT=0` and `TIME_LIMIT_MINUTES=0` default to disabled. New entry targets are `fill + clamp(ATR * TP_ATR_MULT, fill * MIN_TP_PCT, fill * MAX_TP_PCT)`; percentage defaults are 0.006/0.06. Target precision is truncated, and confirmed live fills recompute it, including partial fills. Existing targets stay fixed; `TP_ATR_MULT=0` suspends even a persisted TP target. With both settings zero, legacy paper stop scheduling and idle-live behavior are preserved.
- When enabled, `PaperHands.mark` and `LiveHands.mark` run before the LLM timer on **every loop iteration**, including when the LLM is not due. TP compares a fresh executable bid with the persisted target; TTL compares wall-clock time with persisted `opened_ts`, allowing a stale but valid price for an exit. Missing/invalid quotes never fabricate a zero-price exit. PENDING/CLOSING positions do not issue a second local exit.
- **Only one resident order, the LE stop.** KCEX exposes trigger 103 with `GE`, but two independent triggers on the same BTC have **no proven OCO semantics**. Do not add a resident GE target alongside LE: an outage/whipsaw could leave the other order selling the owner's coins. Local TP/TTL reconcile first, then use the existing owned-stop cancel → confirmation → market SELL path; foreign stop is `PositionStuck`, failed sell plus failed stop restoration is `UNPROTECTED`/exit 2.
- **Local gap/availability risk:** a target cross-and-return between observations can be missed. Roughly one-second checks apply only while the loop is idle: synchronous LLM/HTTP calls, backoff and process downtime delay both paper and live local barriers. The resident live SL remains active during those delays; the simulated paper SL does not. No guarantee of one-second execution or TP fill at the target.
- `opened_ts` is preserved on position updates and restart, not reset on stop placement/reconcile. Legacy timestamps already overwritten by earlier versions cannot be reconstructed; rows with no timestamp have no provable TTL age. Targets are not retrofitted to legacy positions without an entry ATR.
- Barrier attempts carry snapshot + `llm.reason=not_called_barrier` in audit; execution errors preserve fatal exceptions even if audit fails. `fills.source` identifies local TP/TTL; live fill prices can still be estimates (especially delayed reconcile), not proof of an exchange-reported execution price.
- P0 measurement uses the same local exit predicate at **Min15 opens only**, not an assumed fill on an intrabar high. Resident SL uses lows/gaps; a dual-touch candle does not manufacture a TP fill. TTL exits at the first sampled eligible open. Fixed intents are an execution ablation, not a new counterfactual LLM trajectory or tick-accurate reproduction.
- Trailing is deliberately deferred to P6 (`task_97a946246e88`), approved by the coordinator: no cancel/recreate trailing window was introduced. TDD remains mandatory for collar/hands changes.

## P2 — post-loss cooldown (opt-in)

- `COOLDOWN_MINUTES=0` defaults to disabled. A positive finite duration blocks **BUY only**, returning `cooldown`; SELL is evaluated first and never queries cooldown history. The LLM timer/wake policy is unchanged, so this does not save LLM calls.
- **Only a losing exit arms it.** A profitable exit leaves the next entry free: re-entering the same direction while the move continues is riding it, not revenge, and `already_long` plus the fresh-signal requirement already prevent stacking. Breakeven (`pnl >= 0`) is not a loss, and a later win never resets an armed clock. This is the v17 form of Rafael Vargas's Rule 3 — he shipped the post-*any*-exit version first and retired it after measuring the cost on his own book (an ETH bear cascade worth +$2,300 over four trades that the old rule would have cut after the first). `Store.last_exit_ms()` is kept for any-exit callers; the collar takes `last_loss_exit_ms`.
- Age comes from the last recorded `fills.side='SELL'` row **with `pnl < 0`** in the existing bot ledger, across UTC days and process restarts, not account-wide trades or in-memory state. All confirmed exit paths already write SELL fills, including stop, local TP/TTL, flatten and reconcile. Ambiguous pending exits do not fabricate a fill; delayed reconciliation starts the clock at observation. Legacy rows without a provable SELL timestamp cannot establish an exit age.
- The collar receives wall-clock milliseconds at decision time, not quote time; BUY is allowed exactly at expiry. A backward clock jump keeps BUY blocked. No new resident order or live execution change.
- P0 uses the same collar with simulated time. Intrabar stops have unknown execution time, so cooldown age begins conservatively at candle end; gap stops and sampled voluntary/local exits use their observed open. Existing trade timestamps are unchanged. A sub-Min15 cooldown cannot be evaluated faithfully here.
- The pre-registered 30-minute uniform / 60-minute stop-only research hypothesis has only two completed trades and no stops in the frozen 104-decision sample: **no evidence**, not calibration. Keep one production duration and opt-out default; see local `data/backtest/p2-report.md` and `p2-optout-equivalence.json` for measured results and full isolated-process equivalence.

## P4 — decision journal and deferred reflection (opt-in)

- `JOURNAL_ENABLED=0` is opt-out: no new decision journal rows, prompt lessons or reflection calls. Forward migration still creates the journal schema. A previously tracked open position may finish its fill linkage while disabled; a new BUY detaches any older ambiguous entry so it cannot inherit another trade's outcome.
- Before execution, an approved BUY owns a persistent journal id; `fills.journal_id` binds its fills across restarts. Partial SELLs resolve only after the recorded BUY quantity has exited. Outcomes use the existing ledger PnL, with explicit provenance that live prices may be estimates, not exchange fill proof. Missing BUY-fill evidence stays pending rather than inventing an entry, return or duration. Proven cancelled/unfilled entries resolve `not_executed` with null PnL; exception/uncertainty stays pending.
- HOLD is logged as a non-executed observation, **not** a hypothetical next-window return. SELL intent is logged separately without duplicating the owning BUY's PnL. Only completed BUY outcomes are prompt/reflection candidates; non-executed records do not crowd them out. No old audit/fills are retrofitted into fictional theses.
- **Two point-in-time clocks:** `outcome_known_ms` gates outcome visibility and `reflection_known_ms` separately gates generated text. Both Store lookup and the final brain payload enforce `as_of`; a reflection generated after a historical decision cannot leak even if the trade was already closed then. Runtime uses the decision wall clock, replay the sampled open. Intrabar outcomes become known at candle end, never the preceding open.
- At most 5 latest completed outcomes enter the prompt, original reason <=240 chars and reflection <=400 chars. Reflection is one same-model call at most per LLM cycle, **after decision and execution**, max 160 output tokens, 10s timeout, 2–4 prose sentences. It has no order authority. Invalid/truncated prose is discarded with a named `reflection_*` reason; successful-response charges including malformed responses use the same Budget and provider usage/fallback cost.
- Reflect only with at least two `LLM_FALLBACK_COST_USD` estimates remaining (this call + next decision). This is a reservation estimate, not a hard provider billing guarantee or a promise of funding all future decisions; the existing decision budget guard still applies. The existing in-memory daily Budget still resets at process restart. Reflections are best-effort one-shot per resolved record, including named budget/config/offline skips; outcome-only lessons remain usable. Reflection/audit failure cannot replace a fatal hands exception, and journal settlement uses a savepoint so a metadata failure does not lose a real fill.
- Reflection is synchronous and can further delay local paper/TP/TTL monitoring, like existing LLM/HTTP work; it does not change the resident stop. See P1 availability caveats.
- P0 shares recording, resolution, lookup and deferred-reflection code; its default reflection callback is **offline**, recording `reflection_offline` without invented prose. Enabled prompts change cache identity; do not reuse an old no-memory response as a cache hit. Fixed intents only test plumbing/as_of, not learning. The frozen 104-decision sample has n=2 trades and mechanically identical PnL with/without journal: **no evidence about learning until a funded paired run**. Paid smoke was not used; injected-HTTP parser smoke is explicitly synthetic. Local proof/report: `data/backtest/p4-optout-equivalence.json`, `p4-report.md`.

## P5 — order-write rate limit and storm halt

- `MAX_WRITES_PER_HOUR=0`, `MAX_ENTRIES_PER_DAY=0`, `KILL_WRITES_PER_HOUR=0` disable each knob independently; all three at `0` reproduce pre-P5 behaviour exactly (see `data/backtest/p5-optout-equivalence.json`). Unlike P1/P2/P4 this ships **on**, at 30 / 20 / 90: a guard-rail only counts if it is mounted before the accident. A nonzero `KILL_WRITES_PER_HOUR` at or below `MAX_WRITES_PER_HOUR` raises at `Settings` construction (F5: equality included, not only strictly below) — the barrier runs before the collar every cycle, so a kill ceiling at or below the soft one halts on the very count at which the soft gate would first refuse, making the soft gate unreachable either way.
- Every venue write (`place_market`, `place_limit`, `place_trigger`, `cancel_order` — the full `KcexClient` write surface, not only the ones `bot/hands.py` calls today) is recorded in `order_writes` **before** the POST/DELETE, in the mode's own database. POST/DELETE are never retried, so a write that timed out may still have executed; counting after the fact would undercount in exactly the situation that matters.
- Both windows are rolling, not calendar: 1 hour for `MAX_WRITES_PER_HOUR`/`KILL_WRITES_PER_HOUR`, 24 hours for `MAX_ENTRIES_PER_DAY`. A UTC-day counter would hand a loop a fresh allowance at midnight. Rows are pruned past 48 hours of retention on every `record()`, well past either window, so the table cannot grow unbounded while still covering both counts.
- **The counting window is bounded above too, but with a tolerance (F1).** `Store.count_writes(until_ms=...)` and `WriteMeter.counts`/`check_storm` cap the window at `now_ms + MAX_CLOCK_SKEW_MS` (`bot/ratelimit.py`, one hour), not a bare `now_ms`. Without any upper bound, a row stamped while the system clock was ahead (VM resume, a container with no RTC, an NTP step) counted as "in the last hour" until real time caught up to it — reproduced as 95 rows stamped `now + 30 days` making `check_storm` raise exit 8 at the first barrier of every boot for thirty days, with no documented escape but `KILL_WRITES_PER_HOUR=0` or deleting rows by hand. The bound must stay *tolerant*, not strict, because the pre-existing safety property for the OPPOSITE clock direction depends on it: before F1, a **backward** clock jump could only ever widen the window (no upper bound existed), so the write count could rise but never silently drop — refusing a BUY it should refuse (see `test_a_spent_budget_refuses_buy_and_still_allows_sell` in `test_collar.py` and the two clock-direction tests in `test_ratelimit.py`). A strict `until_ms=now_ms` cutoff would have broken that: a modest backward step would exclude rows written moments before it, undercounting and letting a BUY through it shouldn't. `record()`'s own prune (`Store.prune_writes(after_ms=...)`) uses the same tolerance to actively drop rows implausibly far in the future once a normal-time write happens again — optional per the original finding, kept here because it also clears the stale rows out of the table instead of leaving them until real time catches up to their fake stamp.
- **Only entries are refused.** Stop, stop replacement, cancel, flatten and SELL are counted and always allowed. A limiter that can refuse an exit is a limiter that creates the unprotected position of invariant 3. The collar's `rate_limit` gate (`bot/collar.py::decide`) sits after `day_loss` and before `confidence`, and is unreachable from the SELL branch, which returns earlier and never reads `write_counts`.
- The hard ceiling (`WriteMeter.check_storm`) raises `WriteStormHalt` at the `cycle.run_once` barrier — the first statement in the barrier's `try`, before any write **in the cycle** and before the LLM section of that cycle — and `bot/cli.py` returns **exit 8**. "Before any write in the cycle" is exact, not "before any write, period": `bot/cli.py::_loop` calls `hands.reconcile()` once at boot, before the loop that runs this barrier, and that call can itself issue a real `place_trigger` repair. Blocking the boot reconcile would be the wrong fix — that would be the meter refusing a stop placement, which this design forbids — so under a supervisor that restarts the process immediately on every exit 8, each restart still costs exactly one boot-reconcile write before re-halting; the write bleed becomes one write per restart, not zero. Never mid-write otherwise. The halt message carries `LiveHands.last_stop_observation` verbatim (`stop_present` / `stop_absent` / `unknown`) and never infers one; there is no automatic *resume* (the process always exits and a human must look), but the count itself is a rolling window, not a latch — it self-clears roughly an hour after the last recorded write, so a restart delayed until then needs no manual row surgery. A restart made immediately after the halt will very likely re-halt on the same count; `bot/cli.py`'s halt log line says so.
- Paper pays a close approximation of live's toll, not an inflated one. `PaperHands` records the same two writes a live entry costs (entry market, resident stop). On exit it distinguishes the two shapes a live exit can take: a **voluntary** exit (LLM SELL, local TP, TTL, flatten) really does cancel the resident stop first and then send an exit market order — two writes, matching `LiveHands._sell`. A **stop-fired** exit (the resident stop executing at the exchange) costs the live bot *zero* venue writes — the venue executes it and `reconcile()` books it from a balance read (`LiveHands._settle_closed_on_exchange` makes no client call) — so paper bills exactly one write for that shape (the exit itself), not the cancel+exit pair, and deliberately not zero either: recording strictly less than any real exit could ever cost would make paper's calibration more permissive than live, which this design does not do on ambiguity. `bot/backtest.py::replay` bills the same asymmetry (`close(..., "stop")` records one write; every other reason records two), so the P0 replay stays faithful to what each exit type actually costs live, and `rate_limit` can be exercised in paper/replay without either over- or under-billing a stop-out round trip.
- **Scope, honestly:** this bounds repetition, not correctness. One wrong order is still one wrong order, and 30 writes an hour is ample rope. It also cannot see writes made by anything other than this process — the owner's own manual orders are invisible to it by design. The defaults are argued from the cycle arithmetic (~4 writes per trade, one position at a time, ~12 decisions/h at `CYCLE_MINUTES=5`), **not** calibrated against a measured distribution of real write bursts: no such sample exists, because no live order has ever been sent through this client. The frozen "queda" replay sample that exercises this code makes two entries roughly eleven hours apart (`data/backtest/p5-defaults.json`); both are isolated by hours, so neither the soft nor the hard ceiling was ever under real pressure in that sample (`rate_limit_rejections: 0` in `p5-optout-equivalence.json`). `p5-verify.py` asserts the two arms' full replay output — equity, trades, metrics, decisions, gates, snapshots, LLM audit — is identical field-for-field, which proves the two arms behave the same when nothing is stressed; it says nothing about what either arm does under an actual write burst, because the sample never produces one.

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
  2. ~~Capture KCEX WS URL from Chrome~~ — **done**: public WS (`wss://wbs.kcex.com/ws?platform=web`) is confirmed, wired, and now the v1 default. See [Eye (market data)](#eye-market-data) above and `docs/kcex-spot-api.md`. `python -m bot run --chart` gives a local read-only chart at `http://127.0.0.1:8765/`.
  3. `python -m kcex.cli login` then a live probe at the venue minimum (20 USDT) — only if the owner explicitly asks.
  4. Do **not** cancel/modify the user's existing orders. (The 0.00064 BTC stop at 75722 that this line used to name is gone — see the account-state note below — but the rule stands for whatever is there next.)

## Account state — captured 2026-09-07, read-only

`order_history` and `balances` were read with the live session token; no order was created, cancelled or modified.

- **BTC total on the account: 0.0. Open orders: none.** The owner's 0.00064 position was sold manually at 79803.99, and the 0.00064 stop at 75722 ended `state=4` with `dealQuantity=0.00000` — it never fired.
- Docs elsewhere in this repo used to assert that stop as a *present* fact. It is **history**, not current state. Anything reasoning from "the owner has coins in this account right now" is reasoning from a stale premise.
- The `foreign_btc` machinery and its tests stay exactly as they are. They are adversarial scenarios about what happens when someone else's BTC shares the account — that risk returns the moment the owner holds anything again, and an empty account today is not a reason to weaken them. **Do not clear the database or delete those tests on the strength of this note.**
- Terminal-evidence fields now captured (`state`, `dealQuantity`, `avgPrice`, `fee`, `triggerType`, `triggerPrice`, `boundTriggerState`): see `/private/tmp/claude-501/capture/order-history-capture.md`. What they do **not** yet establish is in that file's "não está provado" section — most importantly whether `order_history` lags a DELETE, which is what M1 still turns on.

## Paper/live isolation (security fix)

- **A live run can never reach paper state.** `bot/cli.py::db_path_for_mode` gives each mode its own database: paper keeps `data/bot.db` and its history, live gets `data/bot-live.db`. The bug this closes: one `Store(DB_PATH)` with no mode identity was handed to `LiveHands`, so following the README — run paper, then set `MODE=live` — let a simulated position be adopted as real, and `reconcile()` would place a resident stop-market SELL sized for BTC the bot never bought, on an account where the owner keeps their own coins.
- **Three layers, because one flag in the wrong place should not be enough.** (1) separate files; (2) `Store(path, mode=...)` stamps `store_mode` in `kv` and raises `StoreIdentityMismatch` on a mismatched open; (3) `LiveHands` refuses at construction any position row whose `entry_source`, `entry_order_id` or `stop_order_id` starts with `paper`.
- **An unstamped database is adopted by paper, never by live.** A legacy file carries no proof of who wrote it. Being wrong in paper costs a simulated number; being wrong in live sends a real order sized from a fiction. Live on an unstamped file that already has rows fails closed and asks for a human.
- **Account switching is NOT covered.** `Store` accepts a mode, not an account. Hashing `KCEX_TOKEN` would be useless — it rotates roughly weekly for the same account — and the `user_info` response shape is not captured (`docs/kcex-spot-api.md` records only that it needs the header and 401 means dead), so there is no captured account id to fingerprint. Pointing live at a different KCEX account while reusing `bot-live.db` would still adopt the old account's position. Treat one live database as belonging to one account, by hand, until an account id is captured.
- The instance lock stays shared on purpose: paper and live must never run at once.
