# Futures Paper Cost Gates and Wake Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add opt-in cost gates, persistent Jev entry wakes, and a read-only `wakegrid` calibration replay to the futures paper loop while preserving all default behavior.

**Architecture:** Pure helpers will hold cost and wake qualification rules. `FutLoop` owns only the live streak counter; `FutStore` supplies per-book open counts for the collar. Shadows call the same collar and entry qualification path. `wakegrid` will use a direct SQLite URI connection with `mode=ro`, selecting only `fut_decisions` rows and never constructing `FutStore`.

**Tech Stack:** Python dataclasses, SQLite, argparse, pytest via `./scripts/test`.

---

### Task 1: Add RED tests for settings, cost gate, and open counts

**Files:**
- Modify: `tests/fut/test_fut_settings.py`
- Modify: `tests/fut/test_fut_collar.py`
- Modify: `tests/fut/test_fut_store.py` (create if absent)

- [ ] Write tests for default-off settings, env parsing, finite/non-negative validation, wake streak minimum, regime parsing/validation, and `count_opens(book, since_ms)` including its inclusive boundary and ignoring closes/other books.
- [ ] Write cost-gate tests for spread threshold, expected move versus round-trip cost, invalid ATR deferring to the existing `atr` rule, and entry-rate behavior while CLOSE/HOLD remain unaffected.
- [ ] Run the focused tests with `./scripts/test tests/fut/test_fut_settings.py tests/fut/test_fut_collar.py tests/fut/test_fut_store.py`; confirm RED failures caused by missing settings/functions/methods.
- [ ] Implement the minimal settings, collar, and store changes; rerun the same command GREEN and commit `feat(fut): add opt-in cost gates and entry counts`.

### Task 2: Add RED tests for wake persistence and shadow parity

**Files:**
- Modify: `tests/fut/test_fut_jev.py`
- Modify: `tests/fut/test_fut_loop.py`
- Modify: `tests/fut/test_fut_shadow.py`

- [ ] Test `entry_qualifies` for valid sides, threshold, errors, and regime filters; test `should_wake` streak gating while keeping open-position exits/reversals independent.
- [ ] Test loop streak increments for the same side, resets on non-qualification/error/side change/open position/stale flat snapshot, logs `streak`, and does not dispatch or charge the LLM when the flat entry cost gate blocks the wake.
- [ ] Test main and shadow books pass their own `count_opens` to the collar and that a shadow entry is blocked by the same configured gate/rate rule.
- [ ] Run only those tests with `./scripts/test ...`; confirm RED failures.
- [ ] Implement the pure wake helpers, loop state/dispatch behavior, and shadow count wiring; rerun GREEN and commit `feat(fut): persist Jev entry wakes across evaluations`.

### Task 3: Add RED tests for read-only wakegrid replay and CLI

**Files:**
- Create: `tests/fut/test_fut_wakegrid.py`
- Modify: `tests/fut/test_fut_cli.py`

- [ ] Build a temporary SQLite database directly with only `fut_decisions`, record ordered Jev payloads/snapshots, and test long/short entry prices, one-position-at-a-time exits at the first row at least 60 seconds later, costs, wins, and spread/move gate variants.
- [ ] Test null/error answers are ignored, `--since-ms` filtering, deterministic sorting by sum net bps, and the explicit in-sample warning.
- [ ] Test `cli.main(["wakegrid", ...])` opens a valid database read-only and does not create/migrate/write a file; use a database lacking `bot_meta`/mode stamp to prove no `FutStore` construction.
- [ ] Run `./scripts/test tests/fut/test_fut_wakegrid.py tests/fut/test_fut_cli.py`; confirm RED failures.
- [ ] Implement `fut/wakegrid.py` and CLI parsing/printing with `sqlite3.connect("file:<path>?mode=ro", uri=True)`; rerun GREEN and commit `feat(fut): add read-only wakegrid replay`.

### Task 4: Wire environment documentation and run the full required suite

**Files:**
- Modify: `tests/conftest.py`
- Modify: `.env.example`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`

- [ ] Add every new env var to the cleared test environment list and `.env.example` with one-line comments; document defaults as opt-in/off.
- [ ] Add one short paragraph to each futures-paper documentation section covering cost gates, shared shadows/wake streak/regimes, and `python -m fut wakegrid` as in-sample only.
- [ ] Run the full suite exclusively with `./scripts/test`, inspect the complete output and count tests.
- [ ] Review `git diff --stat`, `git diff -w`, changed paths, and `git log --oneline main..HEAD`; commit documentation and final integration as `docs(fut): document cost gates and wakegrid`.

### Task 5: Final verification

- [ ] Rerun `./scripts/test` after the last change.
- [ ] Confirm `.env`, `data/`, `kcex/`, and `bot/` are unchanged and no network test path was added.
- [ ] Print final test count, `git log --oneline main..HEAD`, and `CODEX_DONE`.
