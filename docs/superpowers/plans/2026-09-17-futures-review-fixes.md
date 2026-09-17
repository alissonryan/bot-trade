# Futures Paper Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and superpowers:verification-before-completion. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the confirmed A1–A4, M1–M6, and B1–B6 findings in the futures paper while keeping paper-only execution, no network tests, and the existing public interfaces where possible.

**Architecture:** Keep the single-threaded paper loop and independent shadow ledgers, but make stale market data explicitly REST-priced, evaluate the Jev baseline from its own position/streak, and make replay/report reads isolated and windowed. Documentation-only limitations are recorded beside the futures-paper operating contract and spec.

**Tech Stack:** Python, sqlite3, pytest via `./scripts/test` only.

---

### Task 1: A3 stale WS accounting

**Files:**
- Modify: `fut/market.py`
- Modify: `tests/fut/test_fut_market.py`, `tests/fut/test_fut_loop.py`

- [ ] Write a failing market/loop regression with a synced `100.0/100.1` book, a stale WS clock, and REST `97.95/98.05`; assert snapshot bid/ask and a time-limit/LLM-close exit use REST prices, while stale depth is empty and imbalance neutral.
- [ ] Run `./scripts/test tests/fut/test_fut_market.py tests/fut/test_fut_loop.py -q`; observe the frozen-book assertion fail.
- [ ] Make `bid()`, `ask()`, and depth selection ignore the book whenever WS age exceeds `stale_market_s`; keep the ticker as the price source until a fresh WS book frame is applied.
- [ ] Re-run the focused command and then the complete suite.
- [ ] Commit `fix: use REST prices after stale futures websocket`.

### Task 2: A1 independent Jev-only baseline

**Files:**
- Modify: `fut/loop.py`, `fut/shadow.py`
- Modify: `tests/fut/test_fut_loop.py`, `tests/fut/test_fut_shadow.py`

- [ ] Add failing tests for Jev-only reversal while main is flat, no false close when main is opposite but Jev still supports Jev-only, minimum hold from Jev-only `opened_ms`, and Jev-only entry while main is open.
- [ ] Run `./scripts/test tests/fut/test_fut_loop.py tests/fut/test_fut_shadow.py -q`; observe failures against shared main state.
- [ ] Give Jev-only its own `should_wake` call, entry side/streak reset rules, cost gate, and position-aware signal; use reversal-by-direction for Jev-only exits when no `exit_now` is carried for that book. Keep random behavior separate.
- [ ] Re-run focused tests and the complete suite.
- [ ] Update the AGENTS futures section to state that Jev-only uses its own position/streak and treats reversal direction as its exit signal.
- [ ] Commit `fix: make jev-only shadow independent from main position`.

### Task 3: A2 wakegrid replay semantics and settings

**Files:**
- Modify: `fut/wakegrid.py`, `fut/cli.py`
- Modify: `tests/fut/test_fut_wakegrid.py`, `tests/fut/test_fut_cli.py`

- [ ] Add failing replay tests for `up, flat, up` with streak 2 producing zero entries, and for a flat row at/after 60 seconds closing an open trade at its bid/ask; assert the positive gross result.
- [ ] Run `./scripts/test tests/fut/test_fut_wakegrid.py tests/fut/test_fut_cli.py -q`; observe the replay overcount/late-exit failures.
- [ ] Process every valid snapshot row for an existing replay position before entry filtering; reset streak on flat, error, invalid, and nonqualifying rows; skip entry rows recorded while main was occupied when the payload exposes that state, otherwise document the limitation. Use `FutSettings.from_env()` values for fee, slippage, and gate calculations where available.
- [ ] Extend the warning with in-sample scope, fixed 60-second hold/no stop, gap/restart/stale caveats, and `wakes` meaning entries; expose the existing `--since-ms` in the output context if needed.
- [ ] Re-run focused tests and the complete suite.
- [ ] Commit `fix: make wakegrid replay exits and streaks faithful`.

### Task 4: A4/M1 read-only windowed report

**Files:**
- Modify: `fut/report.py`, `fut/cli.py`
- Modify: `tests/fut/test_fut_report.py`, `tests/fut/test_fut_cli.py`

- [ ] Add failing tests for `--since-ms`, report output naming the window, read-only report behavior, and two real Jev rows 15 UTC days apart failing `min_days` when only two distinct UTC dates are present.
- [ ] Run `./scripts/test tests/fut/test_fut_report.py tests/fut/test_fut_cli.py -q`; observe current CLI/report and day-span failures.
- [ ] Read the DB through a `sqlite3` `file:...?mode=ro` connection (without `FutStore`, migration, or commit), filter decisions/fills to the requested window, and compute days as distinct UTC dates with real Jev rows.
- [ ] Re-run focused tests and the complete suite.
- [ ] Document in CLAUDE.md and AGENTS.md that report criteria must be run with `--since-ms` at the beginning of a pre-registered fixed-settings window and tuning windows never count.
- [ ] Commit `fix: make futures reports windowed and read only`.

### Task 5: M4 restart-stable random baseline

**Files:**
- Modify: `fut/loop.py`, `fut/shadow.py`
- Modify: `tests/fut/test_fut_shadow.py`, `tests/fut/test_fut_loop.py`

- [ ] Add failing tests showing a restarted loop derives random entry probability from persisted real/main Jev data and does not replay the same random sequence; assert the effective seed is persisted or logged.
- [ ] Run `./scripts/test tests/fut/test_fut_shadow.py tests/fut/test_fut_loop.py -q`; observe process-local rate/seed behavior.
- [ ] Derive the main entry rate from persisted real Jev rows/open counts, seed RNG from configured seed plus process start time, and record the effective seed in a startup decision or Jev payload.
- [ ] Re-run focused tests and the complete suite.
- [ ] Commit `fix: persist random shadow rate across restarts`.

### Task 6: M5 isolated report criterion tests

**Files:**
- Modify: `tests/fut/test_fut_report.py`

- [ ] Add independent synthetic cases where `beats_jev_only` alone fails and where net is positive but the bootstrap CI lower bound is non-positive; add coverage for Jev-only cost deduction.
- [ ] Run `./scripts/test tests/fut/test_fut_report.py -q`; observe the new tests fail before any production change.
- [ ] Adjust only test fixtures/helpers or report logic strictly needed to make each named criterion independently observable; do not weaken assertions.
- [ ] Re-run the focused report suite and the complete suite.
- [ ] Commit `test: isolate futures edge criterion failures`.

### Task 7: B2 ATR gate before wake

**Files:**
- Modify: `fut/loop.py`
- Modify: `tests/fut/test_fut_loop.py`

- [ ] Add a failing test with a qualifying Jev entry and missing/invalid ATR; assert no LLM dispatch and audit gate `atr`.
- [ ] Run `./scripts/test tests/fut/test_fut_loop.py -q`; observe the paid wake occurs.
- [ ] Add the ATR validity check to the pre-dispatch wake gate, preserving collar validation after an LLM decision.
- [ ] Re-run focused tests and the complete suite.
- [ ] Commit `fix: block futures wakes without ATR`.

### Task 8: B3 null insufficient returns

**Files:**
- Modify: `fut/market.py`
- Modify: `tests/fut/test_fut_market.py`, `tests/fut/test_fut_jev.py`

- [ ] Add a failing test asserting missing history returns are `None` in the snapshot/state rather than `0.0`.
- [ ] Run `./scripts/test tests/fut/test_fut_market.py tests/fut/test_fut_jev.py -q`; observe zero placeholders.
- [ ] Emit nullable return values and preserve them through `jev_state` rounding/serialization.
- [ ] Re-run focused tests and the complete suite.
- [ ] Commit `fix: preserve null futures returns without history`.

### Task 9: B4 mock Jev regime coverage

**Files:**
- Modify: `fut/jev.py`
- Modify: `tests/fut/test_fut_jev.py`

- [ ] Add a failing test proving mock Jev can produce an allowed `trend` or `volatile` regime under `FUT_WAKE_REGIMES`.
- [ ] Run `./scripts/test tests/fut/test_fut_jev.py -q`; observe the fixed `range` regime.
- [ ] Derive/rotate the deterministic mock regime from signal magnitude/flow while preserving deterministic behavior.
- [ ] Re-run focused tests and the complete suite.
- [ ] Commit `fix: let mock Jev exercise configured regimes`.

### Task 10: M3/M2/M6/B1/B6 documentation and collar/spec wording

**Files:**
- Modify: `AGENTS.md`, `CLAUDE.md`, `docs/superpowers/specs/2026-09-17-kcex-futures-paper-jev-llm-design.md`
- Modify: `tests/fut/test_fut_collar.py`, `tests/fut/test_fut_dispatch.py` only if needed to pin existing behavior

- [ ] Add a failing documentation/behavior assertion only where existing wording is contradicted by executable behavior, especially LLM failure arming cooldown.
- [ ] Run the focused test first and observe the mismatch.
- [ ] Document fixed model cost allocation versus per-trade reporting, synchronous Jev/REST stop-blind windows up to about 2 seconds, the funding capture edge case, collar order including cost gate and entry-rate gate, and that LLM failures also arm cooldown. Update the spec collar order to match code.
- [ ] Re-run focused tests and the complete suite.
- [ ] Commit `docs: record futures paper review limitations and gate order`.

### Task 11: Final verification and handoff

**Files:**
- No production files beyond the tasks above.

- [ ] Run `./scripts/test` after the last change and read the complete exit status and test count.
- [ ] Check `git diff --check`, prohibited paths, and `git log --oneline main..HEAD`.
- [ ] Report one line per A1–A4, M1–M6, B1–B6 as fixed/documented/skipped with reason, then print `CODEX_DONE`.
