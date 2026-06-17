# M19 — Gated Anchored Composite Signal Scoring Engine — Final Status

Static hand-written reference. Not imported by any module. Not generated at
runtime. Reflects the final M19 contract as of phase M19.H.

## 1. M19 final purpose

M19 turns an upstream trade idea (scanner signal or flywheel candidate snapshot,
optionally enriched with an ML prediction and readiness advisories) into a
single, deterministic, explainable **scored candidate**: a hard-gate decision, a
composite quality score on a 0–100 scale, a decision bucket, a confidence
bucket, and fully embedded sub-results (gates, components, penalties,
multipliers, provenance).

M19 is a **pure scoring and explainability layer**. It does not fetch data, does
not persist to the production database, and does not execute, route, or place
any order. It produces information for a future consumer (M20) to act on.

## 2. Package inventory (`bot/signal_scoring/`)

12 modules:

- `__init__.py` — public API surface and phase marker.
- `schema.py` — enums and dataclass contracts (input, output, gate/component/
  penalty/multiplier results) with to_dict/from_dict round-trips.
- `config.py` — `SignalScoringConfig` (frozen), weights, thresholds, penalty/
  multiplier/scanner/risk/volatility/liquidity/ml blocks, `validate()`.
- `keys.py` — context block key constants and value coercers.
- `provenance.py` — canonical JSON, digests, deterministic identity ids.
- `gates.py` — hard-gate evaluation (`evaluate_hard_gates`).
- `components.py` — 11 pure component scorers.
- `penalties.py` — penalty and multiplier evaluation.
- `composite.py` — `score_candidate` / `assemble_score` (composition + buckets).
- `adapters.py` — pure upstream → `SignalCandidateInput` converters.
- `io.py` — optional explicit JSONL output (the only module allowed to write).
- `audit.py` — pure audit-record / summary projections.

## 3. Public API inventory

The public surface is frozen at exactly 44 symbols (locked by
`M19HFinalAudit.test_public_api_exact_lock`):

Enums / contracts: `ScoringProfile`, `SignalSide`, `DecisionBucket`,
`ConfidenceBucket`, `PenaltySeverity`, `GateOutcome`, `SignalCandidateInput`,
`ScoredSignalCandidate`, `GateFailure`, `GateResult`, `ComponentScore`,
`PenaltyItem`, `PenaltyResult`, `MultiplierItem`, `MultiplierResult`,
`SignalScoringConfig`.

Functions: `make_component_score`, `evaluate_hard_gates`, `score_component`,
`score_all_components`, `evaluate_penalties`, `evaluate_multipliers`,
`score_candidate`, `assemble_score`, `adapter_from_scanner_signal`,
`adapter_from_candidate_snapshot`, `merge_ml_prediction`,
`merge_readiness_advisories`, `scored_candidate_to_jsonl_line`,
`is_write_safe_path`, `write_scored_candidates_jsonl`,
`build_scoring_audit_record`, `build_scoring_audit_summary`, `default_config`.

Constants / modules: `SCHEMA_VERSION_INPUT`, `SCHEMA_VERSION_OUTPUT`,
`DEFAULT_PROFILE`, `COMPONENT_NAMES`, `COMPONENT_SCORERS`, `PENALTY_NAMES`,
`MULTIPLIER_NAMES`, `GATE_ORDER`, `keys`, `provenance`.

## 4. Phase inventory M19.A–M19.G

- **M19.A** — contracts, config, provenance, safety guards.
- **M19.B** — hard gates (ordered, fail-safe, BLOCK precedence).
- **M19.C** — 11 pure component scorers (neutral/conservative fallback; honest
  ML calibration handling).
- **M19.D** — penalties (capped at 30) and multipliers (floor 0.70).
- **M19.E** — composite scoring, decision/confidence buckets, caps.
- **M19.F** — pure adapters (input only; malformed pass-through).
- **M19.G** — optional explicit JSONL output and pure audit helpers.

## 5. Final H reconciliation

The original M19 master contract states that M19 does not execute and must not
expose execution permission. M19.E originally computed
`execution_eligible=True` for a LONG candidate that passed all gates and scored
into `ELIGIBLE` / `HIGH_CONVICTION`, intended as a future paper-routing hint.

For final acceptance this was judged semantically unsafe: a field literally
named `execution_eligible=True` in a persisted artifact can be misread as
execution authorization. M19.H reconciles this to the literal contract:

    M19 scoring may still produce ELIGIBLE / HIGH_CONVICTION buckets,
    but execution_eligible must remain False because M19 does not execute.

This is the only behaviour change in M19.H. Everything else is audit/locking.

## 6. Safety statement

    M19 never executes trades.
    M19 never calls brokers.
    M19 never routes orders.
    M19 never writes signals.db.
    M19 execution_eligible is always False.

## 7. Buckets are quality only, not execution permission

`DecisionBucket.ELIGIBLE` and `DecisionBucket.HIGH_CONVICTION` describe **signal
quality** — how strongly the composite score and gates favour the candidate.
They are **not** an instruction or permission to execute. `execution_eligible`
is always `False` in M19; the quality signal lives entirely in the bucket.

## 8. M20 hand-off contract

M20 (runtime wiring / consumption) builds on M19 as a pure, read-only library.
M20 owns any execution or routing decision; M19 only scores. If M20 needs a
routing-eligibility concept, it must introduce its own clearly-named field
(e.g. `paper_candidate_eligible` / `paper_routing_eligible`) — it must not
overload `execution_eligible`.

## 9. What M20 may consume

- The frozen 44-symbol public API.
- Build inputs via `adapter_from_scanner_signal` /
  `adapter_from_candidate_snapshot`, enrich via `merge_ml_prediction` /
  `merge_readiness_advisories`.
- Call `score_candidate(input, config)` to obtain a `ScoredSignalCandidate`.
- Read any field of the scored candidate (decision_bucket, confidence_bucket,
  final_score, component/penalty/multiplier sub-results, provenance).
- Optionally persist via `write_scored_candidates_jsonl(...)` to an explicit
  system-temp path.
- Build audit records via `build_scoring_audit_record` /
  `build_scoring_audit_summary`.

All of the above are pure / read-only with respect to M19.

## 10. What M20 must not assume

- Must not treat `execution_eligible` or any decision bucket as execution
  authorization.
- Must not mutate `bot/risk.py`, `main.py`, `bot/scanner.py`,
  `bot/strategy.py`, or the dashboard through the scoring package.
- Must not write `signals.db`, `data/ml`, or `data/m19` through M19's io.
- Must not bypass the hard gates.
- Must not reinterpret a raw ML probability as a calibrated win probability.

## 11. Known limitations

- Predict-time calibration is surfaced honestly, but the ML model is not
  live-ready; dataset quality is the bottleneck and the flywheel is the path
  forward.
- Historical signals are predominantly LONG / IBKR-routed.
- Regime, liquidity, and data-quality context are caller-supplied (M19 is
  fetch-free); absent context degrades to neutral/conservative by design, not
  to a fabricated clean value.
- `atr_pct` and reward:risk are derived only when the caller supplies the
  needed inputs; otherwise omitted (neutral) or marked invalid where the inputs
  are present but malformed.

## 12. Deferred work

- **M20** — runtime wiring, real consumption of scored candidates, and any
  paper-routing eligibility field.
- **M21** — cross-fold feature-importance stability and speed/timing
  thresholds.
- **M22 / M23** — per the post-M18 roadmap.
