# M21.1extra-D2 — Supervised Paper Lifecycle (reuse B2b + C)

Branch `m21-1extra-d2-lifecycle-supervised`, off main `f46df56`.

This step wires the first real **paper** lifecycle behind the D0 + D1 gates,
reusing the existing proven primitives with **no new broker path**.

## Orchestration (no double-flatten)

`bot/paper_loop_d2/lifecycle.py` — `run_supervised_cycle(...)`:

1. select a valid candidate (same deterministic, fail-closed selector as dry-run)
2. D0 gate — must be allowed
3. D1 gate — must return `market_open`
4. call B2b `run_lifecycle(symbol, confirm=True, entry_price, stop_loss,
   target_price, kill_switch_active, broker)` **exactly once**
5. enrich the returned B2b dict with D0/D1 provenance + market-clock fields
6. call C `persist_lifecycle(enriched, db_path=)` **once**

B2b already flattens internally via `broker.flatten_paper_position(symbol,
confirm=True)`, so D2 does **not** call a separate flatten on the happy path —
no double-flatten, no `run_flatten` wrapper, no B2flat duplication. `run_dry_cycle`
is untouched; supervised mode is a separate entry point.

Emergency cleanup: only if B2b **raises** after the broker may have been
touched, a narrow flatten is attempted via the same injected broker's
`flatten_paper_position`. If flatness cannot be affirmatively confirmed, the
cycle fails closed **loudly** (`EMERGENCY_FLATTEN_UNCONFIRMED`) and never claims
flatness.

**Important (broker=None):** the real proof runner passes `broker=None`, so B2b
builds its **own internal** paper broker that D2 cannot reach. If B2b raises
after touching that internal broker, D2 **cannot** emergency-flatten it — the
result carries `emergency_flatten_unavailable_b2b_internal_broker` plus
`EMERGENCY_FLATTEN_UNCONFIRMED`, and a **human must manually verify flatness** on
the account. D2 never implies a guaranteed flatten when `broker=None`.

## Core price validation

`run_supervised_cycle` validates prices itself, before any B2b call (the future
scheduler calls it directly, so runner-only validation is insufficient): all
prices > 0 and `stop_loss < entry_price < target_price`. Invalid prices →
`PRECHECK_FAILED` with **no B2b call, no flatten, no persist** — never
`PERSISTED`. The proof runner keeps its own validation too.

## State transitions added

```
CANDIDATE_SELECTED -> D0 gate -> D1 gate ->
   D0_BLOCKED | D1_BLOCKED            (unchanged, no B2b call)
   both affirmative -> B2b once ->
     PRECHECK_FAILED   (B2b originated no entry: paper-mode/account/port/size/pre-existing)
     PERSISTED         (entry originated + lifecycle_confirmed + persisted) — success
     FAILED_CLOSED     (entry not confirmed / B2b raised / persist failed / ambiguity)
```

`ORDER_SUBMITTED`, `POSITION_OBSERVED`, `FLATTEN_CONFIRMED`, `PERSISTED` are
added to the enum; the supervised success terminal is `PERSISTED`.

## D0/D1 reasons carried into the record

The enriched dict passed to C includes `d0_allowed`, `d0_reason`,
`d1_may_submit`, `d1_reason`, `market_clock_checked=True`,
`market_clock_reason=<D1 reason>`, `market_calendar_id="US_EQ"`. The D0 reason
also rides in C's `raw_json` (C stores the whole dict). No fake P&L; C keeps
`record_kind='mechanical_paper_lifecycle'`, `is_edge_outcome=0`.

## Minimal C change

`persist_lifecycle` previously hardcoded `market_clock_checked=0` and
`market_clock_reason="not_checked_in_C_deferred_to_D"`. It now honors a
caller-supplied `market_clock_checked` / `market_clock_reason` when present, and
**preserves the old defaults when absent** — so pre-D2 records are unchanged
(existing C tests still pass) while a D2-enriched record persists
`market_clock_checked=1` and the real D1 reason. No schema expansion, no fake
market-open data.

## Fail-closed matrix (mocked broker/B2b/C; no live DB/network)

```
gates blocked                 -> no B2b call, no persist
D0 allowed + D1 open          -> B2b called once
B2b happy + confirmed         -> persisted once -> PERSISTED (no double-flatten)
B2b no entry originated       -> PRECHECK_FAILED, persisted truthfully
B2b lifecycle_confirmed=false -> FAILED_CLOSED, persisted truthfully
B2b raises                    -> emergency flatten; FAILED_CLOSED
emergency flatten unconfirmed -> FAILED_CLOSED loudly (no false flatness)
C persist fails               -> FAILED_CLOSED
```

## Proof runner (supervised, operator-driven)

`tools/paper_loop/m21_1extra_d2_lifecycle_proof.py` requires `--symbol`,
`--entry-price`, `--stop-loss`, `--target-price`, and the ugly confirm flag
`yes-run-one-real-paper-lifecycle`. It acquires a `CycleLock` (default
`/opt/algo-trader/data/d2_loop.lock`, `--lock-path` overridable; tests use temp
paths) — a lock held by a live process refuses with RC=1 and **no broker
action**. It refuses (RC=1, no broker touched) unless all prices are > 0 and
`stop_loss < entry_price < target_price`; B2b enforces qty/notional caps, paper
mode, account `DUP623346`, and port 4002. DB paths are **split**:
`--policy-db-path` is passed only to the D0 gate, `--lifecycle-db-path` only to
C — lifecycle rows never land in the policy DB and D0 never reads from the
lifecycle DB. There is no live price fetch. RC=0 only if the lifecycle reaches
`PERSISTED`. The lock is released on normal completion and on exception. The
runner is invoked on the VPS during real US market open, D0 enabled, D1
`market_open`, IBKR paper only, human watching, with signals.db backed up and
restore-verified.

## Scope

```
Paper only. No live account, no live port. No new broker path.
Reuses B2b run_lifecycle + C persist_lifecycle only. No B2flat call on happy path.
No unattended scheduler, no cron/systemd. No D3. No M21.2.
Tests use a mocked broker and a temp DB — no live DB/network.
```
