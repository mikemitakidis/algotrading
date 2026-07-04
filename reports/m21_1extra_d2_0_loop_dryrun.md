# M21.1extra-D2.0 — Dry-Run Loop Orchestration

Branch `m21-1extra-d2-0-loop-state-dryrun`, off main `686d996`.

## What D2.0 is

The **pure, dry-run** core of the unattended loop: a state machine that decides
what a real cycle **would** do, given injected gate decisions and candidates. It
places no orders and touches no broker, no gateway, no D0/D1 modules, no
network, and no database. The only "success" terminal is
`DRY_RUN_WOULD_SUBMIT` — meaning both gates were affirmative and a valid
candidate was selected, so a real cycle would submit here. There is
deliberately **no `ORDER_SUBMITTED` state** in D2.0.

## Scope statement

```
D2.0 proves dry-run loop orchestration only.
D2.0 does NOT place orders.
D2.0 does NOT prove unattended production scheduling.
D2.0 does NOT persist real lifecycle rows.
D2.0 does NOT prove the IBKR paper lifecycle.
D2.0 does NOT prove D2.1 / D2.2 / D2.3 or D3.
```

## State machine

```
IDLE -> SCAN_STARTED -> { NO_CANDIDATE | CANDIDATE_SELECTED }
CANDIDATE_SELECTED -> D0 gate -> D1 gate ->
    { D0_BLOCKED | D1_BLOCKED | PRECHECK_FAILED | DRY_RUN_WOULD_SUBMIT }
any step -> FAILED_CLOSED   (any exception / ambiguity)
```

Gate order is D0 then D1 (D0 is the cheaper master switch); both reasons are
always recorded. `would_submit=True` iff the terminal state is
`DRY_RUN_WOULD_SUBMIT` — enforced in the record's `__post_init__`.

## Injected dependencies (hermetic)

`run_dry_cycle(...)` imports no D0/D1/broker/network/DB. It takes:

- `d0_gate() -> (allowed, reason)` — mirrors `runtime_policy` (not-skip == allowed)
- `d1_gate() -> (may_submit, reason)` — mirrors `MarketClockDecision`
- `candidate_provider() -> [(symbol, score), ...]`
- `threshold: float`
- `clock()` and `cycle_id_factory()` — both injectable. Default `cycle_id` is
  **non-deterministic** (unique per cycle); tests inject fixed values for
  reproducibility. Candidate selection is deterministic regardless of the id.

## Candidate selection (deterministic)

Validate every candidate (a malformed one raises → **`FAILED_CLOSED`**, never
silently skipped). Among candidates with `score >= threshold`, pick the highest
score, breaking ties by lexicographic symbol. No qualifying candidate →
`NO_CANDIDATE`.

## Lock (overlap prevention)

`CycleLock(path)` — path injected (tests use temp paths; the real
`/opt/algo-trader/data/d2_loop.lock` is never touched in D2.0). Acquisition is
**atomic** via `os.open(O_CREAT | O_EXCL)`: winning the create holds the lock;
losing it (`FileExistsError`) reads the existing lock — a lock held by **any
live pid, including our own**, refuses acquisition (`LockHeld`), while a
**stale** lock (owning pid not alive) or a **corrupt** file is removed and the
atomic create retried. The lock is always released, on normal completion and on
exception, via the context manager (which does not suppress the exception), and
only a lock we actually acquired (still naming our pid) is removed. No daemon,
cron, or systemd.

## Fail-closed dependency handling

The injected `clock()` and `cycle_id_factory()` are called under fail-closed
handling: if the clock raises, returns a non-datetime, or returns a **naive**
datetime, or if the cycle-id factory raises or returns an empty/non-string id,
`run_dry_cycle` returns a `FAILED_CLOSED` record (with a safe fallback UTC
timestamp used only to build that record) rather than raising. A naive datetime
is never silently assumed to be UTC.

## Tests

- `test_m21_1extra_d2_0_dryrun.py` — selector (none/below/one/multiple/tie),
  malformed-fail-closed (incl. does-not-skip-to-next), provider exception,
  D0/D1 blocked and both-open, gate exceptions, injected clock + cycle id,
  determinism independent of cycle id, the record invariant, and the property
  that `would_submit` requires D0-allowed AND D1-open AND a valid candidate.
- `test_m21_1extra_d2_0_lock.py` — acquire/live-refuse/stale-reclaim/
  release-on-completion/release-on-exception/exception-not-suppressed/
  corrupt-reclaim/own-lock-only/re-acquire-same-pid/path-is-injected.

## What comes next (not in D2.0)

D2.1 swaps the injected mock gates for the real D0 (`runtime_policy`) and D1
(`evaluate_market_clock` + Alpaca) adapters behind the identical interface and
proves every blocked path — still with no orders. D2.2 introduces the first
supervised IBKR **paper** lifecycle by reusing B2b/B2flat + C persistence. D2.3
adds the unattended scheduler wrapper. Then D3 (dashboard).
