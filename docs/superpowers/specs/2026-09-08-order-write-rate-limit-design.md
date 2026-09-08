# Order-write rate limit and write-storm kill-switch — design

Date: 2026-09-08
Status: draft for user review
Scope: P5. One new module (`bot/ratelimit.py`), one new collar reason, one new exit code. BTC/USDT spot, paper and live. No change to what the LLM sees, no change to sizing, no new resident order.

This spec records the approved brainstorm. Do not implement until this file is approved.

## Why

`bot/collar.py` gates **position risk**: notional per order, 5% of free USDT, one position, ATR stop, day loss, venue minimum. Nothing anywhere gates **submission rate**. The concern is borrowed from VeighNa's (vnpy) `RiskManager` app, which caps orders per second, active orders, and daily traded volume — the one idea in that framework that maps onto this bot. Nothing else was taken: vnpy has no crypto gateway, needs Qt/TA-Lib/pandas, and its `MainEngine` would want to own the loop.

The exposure is real and specific here:

- The venue session is a browser token, not an HMAC key. A burst of writes is the shape of traffic that gets a WAF policy tightened or a session invalidated (406/401 are already handled as fatal).
- `kcex/client.py` never retries POST/DELETE, so a duplicate order can only come from *our* control flow — a loop, not the transport.
- `bot/cli.py` restarts with backoff after a failed cycle. A crash after a successful write, repeated, is a write loop that no in-process counter would ever see.

## Non-goals

Not in this work: throttling GET/market data, per-second smoothing or sleeping to pace orders, cancelling anything, limiting the LLM, a second judge, changing sizing, or any new resident exchange order. The limiter never *delays* a write; it either allows it or refuses the entry outright.

## Locked decisions

| Topic | Choice |
| --- | --- |
| Counted | Every write to the venue: `place_market`, `place_trigger`, `cancel_order`. Nothing else. |
| Storage | New SQLite table `order_writes`, in the mode's own database (`db_path_for_mode`). Survives restart. |
| Recorded | **Before** the POST is issued, never after. |
| Blocked | BUY entries only. |
| Never blocked | Stop placement, stop replacement, cancel, flatten, SELL — under any counter state. |
| Soft trip | `collar.decide()` returns `GateResult(False, "rate_limit", "BUY")`. |
| Hard trip | `WriteStormHalt` raised at the `cycle.run_once` barrier, before any write and before the LLM. Exit code **8**. |
| Default | On. `0` on any knob disables that knob. |
| Clock | Wall-clock ms passed in by the caller, as `cooldown_minutes` already does. |

## Settings

| Env | Default | Effect | Disabled by |
| --- | --- | --- | --- |
| `MAX_WRITES_PER_HOUR` | 30 | refuse BUY | `0` |
| `MAX_ENTRIES_PER_DAY` | 20 | refuse BUY | `0` |
| `KILL_WRITES_PER_HOUR` | 90 | halt, exit 8 | `0` |

`Settings.__post_init__` rejects negative or non-finite values, matching the `cooldown_minutes` guard. A `KILL_WRITES_PER_HOUR` that is nonzero and below `MAX_WRITES_PER_HOUR` is a configuration error and raises: it would halt the process before the soft gate could ever refuse anything.

Headroom for the defaults: a complete trade costs four writes (entry market, stop trigger, cancel of the stop, exit market). `CYCLE_MINUTES=5` allows at most ~12 decisions per hour and `already_long` allows one position at a time, so a healthy hour tops out around 8 writes. 30/h is roughly four times the realistic ceiling; 90/h is not a busy day, it is a loop.

## Architecture

```text
cycle.run_once
  ├─ barrier: meter.check_storm(now_ms)  ── WriteStormHalt ──> exit 8
  ├─ collar.decide(..., write_counts=meter.counts(now_ms))  ── "rate_limit" ──> no order
  └─ hands (LiveHands holds meter.wrap(client))
        └─ every place_market / place_trigger / cancel_order
              └─ meter.record(kind)  ──(writes the row)──>  then the POST
```

New module `bot/ratelimit.py`:

- `WriteKind` — `ENTRY` for the buy `place_market`; `PROTECTIVE` for everything else. The kind is decided by the caller at the wrap site, not guessed from arguments.
- `WriteCounts` — a frozen dataclass, `writes_1h` and `entries_24h`. This is the only thing the collar sees; the collar stays pure and does no I/O, exactly as it does today with `day_pnl_usdt` and `last_loss_exit_ms`.
- `WriteMeter(store, settings)` —
  - `record(kind, now_ms)` inserts one row and prunes rows older than 48h.
  - `counts(now_ms)` returns `WriteCounts`.
  - `check_storm(now_ms)` raises `WriteStormHalt` when `KILL_WRITES_PER_HOUR` is nonzero and reached.
  - `wrap(client)` returns a proxy exposing the full `KcexClient` surface, with `place_market`, `place_trigger` and `cancel_order` calling `record` first and then delegating. Everything else is passed straight through by `__getattr__`.

The proxy is the choke point. Recording at each of the six call sites in `bot/hands.py` by hand would work today and silently miss the seventh write added next month.

### Why the transport does not block

Blocking inside `KcexClient` was rejected. It would make `kcex/` depend on `bot/store.py`, inverting the layering that AGENTS.md fixes; and a transport that can refuse a write can refuse the `place_trigger` that protects a fresh position or the `place_market` that flattens it. That is live invariant 3 — a position never stays quietly unprotected — broken by the safety feature. The meter therefore *observes* every write and *refuses* only at the two points where refusing is safe: the collar's BUY branch, and the barrier before a cycle begins.

## Collar change

One new check in the BUY branch of `decide()`, placed immediately after `day_loss` and before `confidence`:

```python
if write_counts is not None and _rate_limited(write_counts, settings):
    return GateResult(False, "rate_limit", "BUY")
```

`write_counts` is a new keyword argument defaulting to `None`. `None` means "no limiter wired", which is what every existing caller and test gets until it passes one — so no existing behaviour changes by accident.

The SELL branch returns before this line and never reads `write_counts`, the same discipline `cooldown` follows. A backward clock jump shrinks the elapsed window and therefore raises the count, which fails **closed** for BUY and leaves SELL untouched.

## Kill-switch and exit 8

`check_storm` is called in the `cycle.run_once` barrier section, alongside the existing `TerminalEvidenceUnavailable` path, **before** `poll_heavy()` and before the LLM. It never fires in the middle of a write: aborting a half-finished exit is precisely how a position ends up unprotected.

`WriteStormHalt` propagates like `SessionDead` and `UnprotectedPosition`. `bot/cli.py` maps it to exit **8**, distinct from 1/2/5/6/7 because it names a distinct operator action: *the bot is issuing writes in a pattern nobody designed; read `data/bot.log` and the `order_writes` table before restarting it.* The message carries `LiveHands.last_stop_observation` — `stop_present`, `stop_absent`, or `unknown` — verbatim, exactly as exit 6 does, and never infers a value that was not observed.

Halting with an open position is deliberate and consistent with exits 2, 6 and 7: stopping on uncertainty is this codebase's safe direction. The resident stop stays on the exchange while the process is down; the halt message says whether it was last seen there.

## Storage

```sql
CREATE TABLE IF NOT EXISTS order_writes (
  id INTEGER PRIMARY KEY,
  ts_ms INTEGER NOT NULL,
  kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_writes_ts ON order_writes(ts_ms);
```

Forward migration only, in the existing `Store._migrate` style. Counting is `SELECT COUNT(*) WHERE ts_ms > ?`; pruning is `DELETE WHERE ts_ms < now - 48h`, done inside `record` so no separate job is needed. Paper and live already use separate databases and are stamped by mode, so paper traffic can never arm a live halt.

Rows are audit evidence, not just a counter: after an exit 8 the operator can read exactly which writes happened and when.

## Paper and P0

`PaperHands` gets the same meter and records the same rows. Paper writes nothing to a venue, but recording keeps the collar's `rate_limit` reason reachable in paper and keeps the P0 replay faithful. With the shipped defaults the limiter must never trip in the frozen 104-decision sample.

That claim is not assumed, it is measured: the plan produces `data/backtest/p5-optout-equivalence.json` in the shape of the existing `p2-optout-equivalence.json`, comparing an isolated-process run with the limiter at defaults against one with all three knobs at `0`, and asserting identical decisions and PnL. If they differ, the defaults are wrong, not the sample.

## Testing

TDD is mandatory: this touches `bot/collar.py` and `bot/hands.py`.

1. A recorded write survives a process restart — counts read back from a reopened `Store`.
2. With `writes_1h` over the limit, BUY is refused with `rate_limit` and SELL of an open position is still approved.
3. `_place_stop`, `_flatten`, `_sell` and `cancel_if_ours` all still execute with every counter far past every limit.
4. `record` happens before the delegated call: a client whose `place_market` raises still leaves the row behind.
5. `check_storm` raises before `poll_heavy` and before any write; `bot/cli.py` returns 8.
6. `0` on each knob disables that knob individually, and all three at `0` reproduce today's behaviour exactly.
7. Pruning deletes nothing inside the window and does delete a 49h-old row.
8. A backward clock jump keeps BUY refused and leaves SELL approved.
9. The proxy covers exactly `place_market`, `place_trigger`, `cancel_order` — a guard test enumerating the `KcexClient` methods that issue POST/DELETE and failing if one is not wrapped, so a future write cannot be added uncounted.
10. `KILL_WRITES_PER_HOUR` below a nonzero `MAX_WRITES_PER_HOUR` raises at settings construction.
11. P0 opt-out equivalence artifact, as above.

No test places a live order; `KcexClient` stays mocked, per AGENTS.md.

## What this does not protect against

A single wrong order is still a single wrong order: the limiter is about repetition, not correctness, and 30 writes an hour is plenty of rope to lose money slowly. It also cannot see writes issued by anything other than this process — the owner's own manual orders are invisible to it, by design.
