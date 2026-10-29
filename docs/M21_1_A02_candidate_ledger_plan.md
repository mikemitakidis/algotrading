# M21.1 A02: candidate-decision ledger, implementation plan

**Status:** offline implementation on draft PR (branch `m21.1/a02-candidate-ledger`, stacked on A01 PR #12).

**Not done in this PR, each needing operator approval:**
- no migration deployed to any production database;
- no writer enabled;
- no runtime wiring (`main.py` is sha-pinned and not touched).

## 1. Purpose

Carry **one stable candidate id** through every stage:

**scanner candidate → M19 score → risk decision → execution intent → broker result → outcome.**

This gives:
- **complete stage accounting:** every candidate ends in exactly one terminal kind or is legitimately awaiting an outcome;
- **traceability:** joins to the existing tables;
- **safe handling of skipped and rejected candidates:** the "what we did not trade" side of M21.2–M21.7.

## 2. Stable identifiers (`bot/candidate_ledger/ids.py`)

| id | formula | notes |
|---|---|---|
| `candidate_id` | `"C-" + sha256(canonical_json({"s": symbol, "t": decision_ts_iso_utc, "d": direction, "k": strategy_id}))[:16]` | Identical to lane A `research/evidence/candidates.make_candidate_id` (parity tested). It uses the M19 canonical JSON encoder. Naive timestamps are refused. |
| `strategy_id` | `"scanner:v<strategy_version>"` | For scanner candidates. |
| `decision_ts_utc` | the decision bar's close, under completed-candle evaluation (C01 §11). Until then: the scan's signal timestamp. | The legacy replay uses `signals.timestamp` for final signals and the snapshot timestamp for other candidates. |
| `event_id` | `"E-" + sha256(canonical_json({"c": candidate_id, "s": stage, "a": attempt}))[:24]` | One per (candidate, stage, attempt). |

Ids are deterministic, so re-running the same decisions or replays writes nothing new. That is what makes the writer idempotent.

## 3. Schema (`bot/candidate_ledger/schema.py`, `SCHEMA_VERSION = "1"`)

Three **new** tables. No existing table is altered, dropped or written.

```sql
CREATE TABLE IF NOT EXISTS candidate_ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  -- schema_version = '1', created_at_utc

CREATE TABLE IF NOT EXISTS candidate_ledger (
  candidate_id     TEXT PRIMARY KEY CHECK (candidate_id LIKE 'C-%'),
  cycle_key        TEXT NOT NULL,        -- scan timestamp (UTC ISO)
  cycle_id         INTEGER,              -- main.py cycle counter (informational; resets on restart)
  symbol           TEXT NOT NULL,
  direction        TEXT NOT NULL CHECK (direction IN ('long','short')),
  strategy_id      TEXT NOT NULL,
  decision_ts_utc  TEXT NOT NULL,
  scanner_stage    TEXT NOT NULL,        -- partial_confluence | watch_only | final_signal | ...
  bar_asof_json    TEXT,                 -- A01 per-timeframe bar record
  first_seen_utc   TEXT NOT NULL,
  signal_id        INTEGER,              -- -> signals.id
  intent_id        INTEGER,              -- -> execution_intents.id
  risk_decision_id TEXT,                 -- -> risk_decisions.decision_id (M14, when one exists)
  outcome_id       INTEGER,              -- -> signal_outcomes.id
  terminal_kind    TEXT CHECK (terminal_kind IS NULL OR terminal_kind IN (<TERMINAL_KINDS>)),
  terminal_at_utc  TEXT,
  created_by       TEXT NOT NULL,        -- writer name (scanner | legacy_replay | ...)
  CHECK ((terminal_kind IS NULL) = (terminal_at_utc IS NULL))
);

CREATE TABLE IF NOT EXISTS candidate_ledger_events (
  event_id      TEXT PRIMARY KEY CHECK (event_id LIKE 'E-%'),
  candidate_id  TEXT NOT NULL REFERENCES candidate_ledger(candidate_id),
  stage         TEXT NOT NULL CHECK (stage IN (<STAGES>)),
  attempt       INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  at_utc        TEXT NOT NULL,
  reason_codes  TEXT NOT NULL DEFAULT '[]',
  payload_json  TEXT NOT NULL DEFAULT '{}',   -- bounded, deterministic JSON
  writer        TEXT NOT NULL,
  UNIQUE (candidate_id, stage, attempt)
);
-- indexes: ledger(cycle_key), ledger(signal_id), ledger(intent_id), ledger(terminal_kind), events(candidate_id)
```

## 4. Event stages and terminal kinds (`bot/candidate_ledger/stages.py`)

| stage | requires | payload (bounded) |
|---|---|---|
| `scanned` | — | scanner stage, valid_count, tfs_passing, available_tfs, indicators, legacy snapshot id |
| `scored` (optional) | `scanned` | from A01 `TruthfulScore`: M19 bucket, final score, research_rank_score, hard-gate failures, `execution_eligible` (always false), UNKNOWN inputs, observation states, Risk Authority state, verified decision id, corporate-action state |
| `risk_evaluated` | `scanned` | verdict pass/reject, reason, selected check fields only (never the raw broker reconcile) |
| `intent_logged` | `risk_evaluated` | `execution_intents.id`, status, broker |
| `broker_result` | `intent_logged` | status, broker order id, rejection reason |
| `outcome_resolved` | `broker_result` | `signal_outcomes.id`, WIN/LOSS/TIMEOUT, return, method |
| `closed` | `scanned` | terminal kind |

**Terminal kinds** (exactly one per closed candidate):
- `not_selected`
- `not_persisted`
- `scoring_blocked`
- `risk_rejected`
- `broker_unready`
- `broker_not_accepted`
- `cancelled`
- `outcome_resolved`
- `untracked_after_signal` (a legacy data gap; reported, never invented)

**In flight:** a candidate without `closed`. This is legitimate only after an accepted broker result (`accepted`, `filled`, `paper_logged`) while it awaits its outcome.

## 5. Outcome joins

| ledger column | joins | integrity checked by `account()` |
|---|---|---|
| `signal_id` | `signals.id` (= `candidate_snapshots.signal_id`) | row exists |
| `intent_id` | `execution_intents.id` | same `signal_id`, `symbol`, `direction` |
| `outcome_id` | `signal_outcomes.id` | same `signal_id` |
| `risk_decision_id` | `risk_decisions.decision_id` | row exists. Populated once the scanner path makes M14 decisions (C01). A01 records verified decision ids in the `scored` payload. |

## 6. Migration safety

- **Explicit only.** `schema.migrate(conn, now_utc=...)` is never called at import or by the writer, and the writer never creates tables. Deploying to any non-fixture DB is an operator decision.
- **Additive and idempotent.** `CREATE TABLE/INDEX IF NOT EXISTS` and a version sentinel. Re-running changes nothing; `created_at_utc` keeps its first value.
- **All-or-nothing.** The migration runs inside a `SAVEPOINT`; any failure rolls back every ledger table.
- **A version mismatch is refused** (`LedgerSchemaError`); an unknown ledger version is never upgraded silently.
- **Existing tables are untouched.** The test fingerprints every pre-existing table, its DDL and its rows, before and after migrating a legacy fixture DB. A static check confirms the schema module has no `ALTER`, `DROP`, `DELETE` or `UPDATE`.

## 7. Flag-gated writer (`bot/candidate_ledger/writer.py`)

- **OFF by default.** `CandidateLedgerWriter(conn_factory, enabled=False)`. Even with `enabled=True` it writes only if the schema is already present at the expected version. `ledger_enabled_from_env()` is True only for `CANDIDATE_LEDGER_ENABLED=1` exactly.
- **Own connection per call.** It never commits or rolls back the caller's transaction (tested with an uncommitted caller write).
- **Idempotent.** Identical repeats are no-ops. Different content for an existing (candidate, stage, attempt) is **not** overwritten and is counted as a conflict. Links and terminal kinds are set once. A re-evaluation uses an explicit `attempt`.
- **Order-checked.** A stage is accepted only after the stages it requires; nothing new is accepted after `closed`; unknown candidates are rejected.
- **Never raises.** Every failure is counted in `stats`. `open_candidate` still returns the deterministic id, so callers can carry it even when the ledger is off.

## 8. Accounting (`bot/candidate_ledger/accounting.py`)

`account(conn, complete_before=...)` is read-only. It checks the invariants below and returns counts per terminal kind, the in-flight counts, and every violation:
- exactly one `scanned` event;
- predecessor stages present;
- `closed` ↔ `terminal_kind` consistent;
- the terminal kind consistent with the stages;
- no unexplained in-flight candidates in completed cycles;
- join integrity.

## 9. Offline legacy replay (`tools/candidate_ledger/replay_legacy.py`)

- **Rebuilds the ledger** from `candidate_snapshots`, `signals`, `execution_intents` and `signal_outcomes`.
- **Read-only source:** SQLite `mode=ro` plus `query_only`.
- **Output:** must be under `/tmp/`, outside the repository, in an existing directory, and different from the source.
- **Idempotent.**
- **Reports legacy gaps** instead of inventing data.
- **Running it on a copy of production data needs approval.**

## 10. Tests (`test_m21_1_a02_candidate_ledger.py`)

- **Ids:** lane-A parity; determinism; validation.
- **Migration:** additive, idempotent, rollback, version refusal, static check.
- **Writer:** off by default; never creates schema; full pipeline twice with no duplicates; conflicts never overwrite; order and unknown-candidate rejection; never raises; never touches the caller's transaction.
- **Accounting:** clean paths, and every violation class detected.
- **Legacy replay** on a fixture built with the production writers:
  - every candidate path reconstructed and accounted for: not_selected ×2, risk_rejected, broker_unready, outcome_resolved, broker_not_accepted, cancelled, untracked_after_signal, awaiting_outcome, incomplete;
  - the source is byte-identical afterwards;
  - idempotent re-run;
  - path refusals;
  - read-only source.
- **A01 score payload:** from a real `TruthfulScore`, never records an execution grant.
- **Static:** no runtime module imports the ledger, and the package's imports are allowlisted.

## 11. Rollout (each step needs operator approval)

1. **Review** this PR together with A01 (PR #12).
2. **Offline replay** on an approved, read-only *copy* of production `signals.db`. Review the accounting report and its legacy gaps.
3. **Wiring PR:** `main.py` calls the writer after each stage, behind `CANDIDATE_LEDGER_ENABLED`, default off. Writer failures can never affect trading. This needs a reviewed `main.py` sha-pin transition.
4. **Migration deployment** to production `signals.db` (backup first), then enable the flag on paper.
5. **Use by M21.2–M21.7:** skipped/rejected candidate analysis, counterfactual labels (lane A), gate economics (B04).
