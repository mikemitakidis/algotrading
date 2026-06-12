# M18 Completion & Advanced Architecture Plan

**Branch:** `m18-recovery-from-transcripts`
**Status of this document:** roadmap / checkpoint only — no production code or
tests are changed by the commit that introduces this file.
**Supersedes for planning purposes:** the "honest final status" in
`RECOVERY_M18_MANIFEST.md` §7 — the recovered branch is accepted as a
*safe baseline*, not as final M18.

---

## 1. Current recovered state

| Metric | Value |
|---|---|
| `test_m18_ml` | 428 OK, skipped=3 (lightgbm-conditional) |
| M18 `G10_Hygiene` | 10 OK |
| M17.B safety gate | 200 OK, skipped=2 |
| Protected files touched | 0 / 20 |
| `bot/data.py` sha256 | `35a7ff9f88500d4b27444d171268631202ad0eca9809b113e157192ed2538440` (unchanged) |
| `requirements.txt` | unchanged |
| `data/ml/` artifacts | none |
| Known residual | 24 unrecoverable G2–G5 method-level tests |

The recovered branch is **safe and useful** — full safety contract intact, all
G-blocks present, dual-cohort training, registry, read-only predictions, and
the `ALWAYS_FALSE_APPROVED_FOR_LIVE` invariant all working.

---

## 2. Why current M18 is not final

The recovery reconstructed the *accepted* M18, which had itself narrowed
several items from the original "M18 Final Architecture v2" plan. Now that the
target is **the full original plan, implemented and improved**, the branch is
an incomplete-versus-original recovery. Ten original-plan items are missing or
materially reduced (§4), and there are real modelling risks (§5).

---

## 3. Corrected original-plan audit summary

The full audit lives in this repo's history and in §4–§5 below. The key
corrections over the first audit pass:

- **Calibration** was mis-classified as implemented. Only diagnostics
  (reliability curve / ECE / MCE) exist; there is **no `IsotonicRegression`**
  fit-on-validation / applied-to-test calibrator anywhere in `bot/ml/`.
- **Thinness gates** are materially weaker than the plan
  (train≥200/val≥50/test≥50/minority≥20 vs total≥2000/train-positives≥500/
  holdout-positives≥100/per-symbol≥50) and count *total samples*, not
  *positive labels*.
- **Adversarial validation** is a hard *promotion/integrity* gate, not a hard
  *dataset-build* gate: `assembler.py` catches AV exceptions, sets
  `av_result=None`, and continues; promotion is blocked later by
  `adversarial_validation_not_run` / `_failed`.
- **NaN→0 imputation** in `models/base.py::extract_xy_for_split` was missed —
  a global `X[np.isnan(X)] = 0.0` with no missingness indicators.
- **Parquet feature_store / label_store** is absent (assembler computes
  in-memory); this is central to the original "fast self-training through
  caching" goal, so it is a missing implementation, not a nice-to-have.

---

## 4. Missing / reduced original-plan items

1. **RandomForest fallback** — **RESOLVED (M18.B.1).** `M_random_forest` is now
   implemented (`bot/ml/models/random_forest_trainer.py`, sklearn-only,
   deterministic) and in `IMPLEMENTED_MODEL_TYPES`; it trains only when
   explicitly requested and never silently replaces M_lightgbm.
2. **repro_hash_v2 (SR-8)** — current `repro_hash(config, library_versions,
   git_sha)` is a subset; the plan required feature/label/train_config/
   dataset_manifest canonical JSON + per-symbol M16 bar SHA + git head +
   python/numpy/pandas/sklearn/lightgbm versions as explicit components.
3. **Real isotonic calibration model** — fit-on-val, apply-to-test, persist;
   pre/post Brier/ECE/MCE in the eval report.
4. **Strict production thinness gates** — keep cold-start defaults for build,
   add a separate strict `production_promotion` profile.
5. **Explicit NaN/missingness policy** — per-group fill + indicator columns +
   tests that warmup/signal_history/market_context NaN behaviour is intentional.
6. **AV failure-reason persistence** — record exception class/message/cause
   (too-few-rows vs NaN vs one-class vs sklearn-missing) in the manifest.
7. **Content-addressed feature_store / label_store** —
   `bot/ml/store/{feature_store,label_store}.py`, partitions by
   symbol/timeframe/date/schema-hash, `schema.json`/`meta.json`, cache
   hit/miss, no committed artifacts.
8. **Dataset/model artifact persistence** — persisted dataset splits +
   manifest + model artifacts so commands can hand off between invocations.
9. **Full CLI** — build-dataset / train / evaluate / demote (currently
   documented stubs, blocked on item 8).
10. **Original artifact layout / model-card output** — the plan's per-model
    directory (`model.joblib`/`model_card.json`/`train_config.json`/
    `training_log.jsonl`/`eval_report.json`/`calibration.json`/
    `feature_importances.json`/`adversarial_validation.json`/
    `drift_report.json`/`predictions_holdout.parquet`/`repro_hash.txt`).
    The current design deliberately refits-on-demand from persisted
    `training_X/y.parquet` + seed (avoids joblib/sklearn version coupling);
    this divergence is defensible but must be a documented decision, and a
    model-card-style summary should still be emitted.

---

## 5. Bugs / modelling risks

| Risk | Location | Severity | Fix |
|---|---|---|---|
| Global NaN→0 imputation encodes missingness as a real value | `models/base.py::extract_xy_for_split` | Important (modelling) | per-group fill + missingness indicators + intentional-NaN tests |
| AV failure reason swallowed | `dataset/assembler.py` `except Exception: av_result=None` | Important | persist structured failure record |
| `M_random_forest` advertised but unimplemented | `models/trainer.py` | Important | **RESOLVED (M18.B.1)** — implemented as sklearn RandomForestTrainer |
| repro_hash weaker than SR-8 | `hashing.py` | Important | repro_hash_v2 (M18.B.2) |

No integrity-gate bypass, no mutable `approved_for_live`, no `signals.db`
writes — those remain correct.

---

## 6. Advanced M18+ Requirements

Operator-requested advanced improvements, classified by when they should land.

### 6.1 Model leaderboard / champion–challenger
- Track every trained model vs the current champion; never replace the champion
  unless gates pass; keep the previous champion recoverable; include B0/B1/B2
  deltas in the comparison.
- **Classification: SHOULD IMPLEMENT BEFORE FINAL M18.**

### 6.2 Experiment / run manifest for every train/eval attempt
- Record run_id, dataset_id, feature_schema_hash, label_schema_hash,
  train_config_hash, repro_hash_v2, git SHA, library versions, pass/fail gates,
  promotion eligibility, rejection reasons.
- **Classification: MUST IMPLEMENT BEFORE FINAL M18** (it is the audit spine for
  every other gate and depends on repro_hash_v2).

### 6.3 Fast-training cache stats
- feature/label cache hit/miss, partitions rebuilt/reused, elapsed time by
  stage, rows/sec. Depends on the store (M18.B.7).
- **Classification: SHOULD IMPLEMENT BEFORE FINAL M18.**

### 6.4 Parallel-safe cache design
- Atomic writes (temp file → rename), lock file or content-addressed immutable
  paths, never leave corrupted parquet partitions. Part of the store design.
- **Classification: MUST IMPLEMENT BEFORE FINAL M18** (correctness of the store).

### 6.5 Data-quality gate before training
- Missing/duplicate/non-monotonic timestamps, min/max sanity, impossible OHLC
  (high<low, close outside [low,high]), zero-volume/stale-bar, per-symbol
  coverage report.
- **Classification: MUST IMPLEMENT BEFORE FINAL M18** (cheap, high-value, guards
  every downstream stage).

### 6.6 Model confidence / calibration-quality gate
- Brier and ECE thresholds, probability-distribution sanity, calibrated vs
  uncalibrated comparison; reject overconfident-but-inaccurate models. Depends
  on isotonic calibration (M18.B.3).
- **Classification: SHOULD IMPLEMENT BEFORE FINAL M18.**

### 6.7 Shadow-prediction monitoring
- Prediction count/day, score-distribution drift, feature-extrapolation rate,
  confidence-bucket outcomes once labels resolve. Read-only, no live action.
- **Classification: SHOULD IMPLEMENT BEFORE FINAL M18.**

### 6.8 One-command M18 safety audit
- Runs test_m18_ml, M18 G10, M17.B G10, M17.B full, protected-file diff,
  requirements diff, data/ml artifact check, no-signals.db-write check,
  no-live/broker/order/dashboard-import check.
- **Classification: MUST IMPLEMENT BEFORE FINAL M18** (makes every later phase
  cheap to verify and prevents regressions).

### 6.9 Feature stability / selection diagnostics
- Permutation-importance stability across folds; remove unstable features from
  promotion eligibility; feature-leakage suspicion report.
- **Classification: SHOULD IMPLEMENT BEFORE FINAL M18.**

### 6.10 Regime-aware validation
- Evaluate separately in high/low-vol, trend/range, SPY up/down/flat (if
  market_context exists); require no catastrophic underperformance in any major
  regime as a promotion check.
- **Classification: SHOULD IMPLEMENT BEFORE FINAL M18.**

### 6.11 Future advanced model roadmap
- CPCV / combinatorial purged CV, ensemble/stacking, regime-specific models,
  hyperparameter search, online monitoring, live integration.
- **Classification: M18.C / M19 ROADMAP** (live integration also
  **REQUIRES OPERATOR APPROVAL** and is never in M18).

---

## 7. Claude's Additional Expert Recommendations

Beyond the original plan and the operator's advanced list, the following would
make M18 more professional, reliable, and faster. Classified MUST / SHOULD /
ROADMAP.

### MUST (before final M18)
- **Determinism CI assertion across two fresh processes.** The current
  determinism tests refit in-process. Add a test that trains the same config in
  two *separate* subprocesses and asserts byte-identical `predict_proba` output
  (catches BLAS thread-count and hash-seed nondeterminism the in-process test
  can miss). Cheap, and it is the real guarantee SR-4/SR-8 promise.
- **Label/feature schema-hash pinned into every prediction row.** Predictions
  should fail closed if the predict-time feature/label schema hash differs from
  the model's training schema hash. The plan flagged this as a hard fail
  (feature schema drift risk); make the read-only predictor enforce it, not just
  the trainer.
- **`fixture_only` taint propagation test at the registry boundary.** Add an
  explicit negative test that a `fixture_only` entry can never become `current`
  even via `--force --override-gate`, complementing the existing integrity-gate
  tests. Q16/Q17 are the load-bearing safety invariants; they deserve a
  belt-and-suspenders test.

### SHOULD (before final M18)
- **Time-decay / recency weighting option in training.** Markets are
  non-stationary; an optional sample-weight that down-weights older rows
  (config-gated, off by default for determinism reproducibility) is a standard
  professional upgrade that materially improves live relevance without changing
  the safety surface.
- **Purged-CV embargo unit on calendar days, not bar count, with DST/holiday
  awareness.** The embargo is "5 trading days"; verify it is computed on the
  trading calendar (not naive bar offsets) so 15m vs 1d anchors embargo the same
  wall-clock window. A subtle leakage source worth a targeted test.
- **Cost-model sensitivity in the eval report.** Report trading metrics at a
  small grid of cost assumptions (e.g. fee+slippage at 0.5×/1×/2×) so a model's
  edge is shown to survive realistic costs, not just one assumed number. Pure
  evaluation, no new risk.
- **Prediction-set immutability hash.** Each shadow-prediction file carries a
  content hash + the model_id + repro_hash_v2, so a later audit can prove which
  model produced which predictions and that they were not edited.
- **Feature-store schema-evolution policy.** When a feature group version bumps,
  the store should treat old partitions as a different content-address (already
  implied by the hash) AND emit a migration note, so a silent version bump can
  never serve stale cached features. Test for it.

### ROADMAP (M18.C / M19)
- **Conformal prediction intervals** for the candidate-quality model — gives
  calibrated abstention ("no confident call") which is exactly what a read-only
  advisory model should support before any live use.
- **Drift-triggered retrain scheduler** (offline, advisory only): when shadow
  PSI/score-drift crosses a threshold, emit a "retrain recommended" artifact —
  never auto-trains, never goes live.
- **Per-regime champion routing** (depends on regime-aware validation): keep a
  champion per market regime rather than one global champion. Powerful but adds
  selection complexity; defer until single-champion is proven.
- **Backtest-bridge replay harness**: a read-only harness that replays shadow
  predictions against the M17.B backtester's *labels* (not its executor) to
  produce an apples-to-apples "would this model have helped?" report. Strong
  evaluation upgrade; defer because it needs careful no-execution-coupling
  design.

---

## 7.A Advanced quality & speed requirements (operator-added)

Additional operator-requested requirements that extend §6 and §7. The theme is
quality assurance and training speed — the goal is not only to match the
original plan but to make M18 measurably faster to train and safer to promote.

1. **Model risk scorecard.** Every model gets a model-risk score across:
   leakage risk, drift risk, calibration risk, data-thinness risk,
   feature-instability risk, and regime-fragility risk. Surfaced in the eval
   report and the registry entry.
   **Classification: SHOULD before final M18.**

2. **Training speed benchmark.** Track elapsed time for each stage (feature
   build, label build, dataset assembly, training, evaluation, registry write).
   Store rows/sec and, once the feature_store/label_store lands, the cache-hit
   speed improvement. The whole point of the store is faster training, so this
   is how we prove it.
   **Classification: MUST before final M18** (the goal is faster training).

3. **Dataset quality report.** Persist a dataset-quality report per dataset:
   gaps, duplicates, stale bars, OHLC anomalies, zero-volume rows, missing
   features, missing labels.
   **Classification: MUST before final M18.**

4. **Model explainability report.** Top features, negative-impact features,
   unstable features, and the feature groups that contribute most to false
   positives.
   **Classification: SHOULD before final M18.**

5. **False-positive / false-negative error analysis.** Evaluation lists why
   rejected/accepted trades failed, broken down by feature group, regime,
   symbol, and scanner route.
   **Classification: SHOULD before final M18.**

6. **Training reproducibility bundle.** For every model/run, save a full audit
   bundle: config, schema hashes, dataset manifest, repro_hash_v2, git SHA,
   library versions, gate outcomes, and the reason for rejection/promotion.
   (Extends §6.2's run manifest into a self-contained, portable bundle.)
   **Classification: MUST before final M18.**

7. **Performance regression guard.** A new model cannot be promoted if it is
   slower than the baseline by a configured threshold, unless explicitly
   accepted (recorded like a forced judgment-gate override).
   **Classification: SHOULD before final M18.**

8. **Cache performance regression guard.** Once feature_store/label_store
   exists, add tests proving repeated builds reuse cached partitions and become
   faster (cache-hit path strictly faster than cold build on a fixture).
   **Classification: MUST before final M18.**

9. **Model rollback / previous-champion restoration.** The registry must allow
   restoring a previous champion without retraining (complements §6.1's
   "previous champion recoverable").
   **Classification: SHOULD before final M18.**

10. **Overfitting suspicion report.** Report train/val/test metric gaps,
    permutation-importance instability, and adversarial-validation signs of
    leakage/overfit; flag a model as overfit-suspect for operator review.
    **Classification: MUST before final M18.**

These map onto the phase plan as follows: items 2/3/8 attach to **M18.B.7**
(store + cache stats) and **M18.B.8** (artifact persistence); items 1/4/5/6/10
attach to **M18.B.10** (advanced monitoring/evaluation); items 7/9 attach to
**M18.B.10** registry/leaderboard work. Each carries its own tests per the §9
pattern (a passing fixture and a failing fixture that trips the guard).

---

## 8. M18.B implementation phases

| Phase | Scope | Depends on |
|---|---|---|
| **M18.B.0** | Save audit + completion roadmap (this commit) | — |
| **M18.B.1** | RandomForest fallback (sklearn, deterministic) — **DONE** (commit on branch) | — |
| **M18.B.2** | repro_hash_v2 (full SR-8 composition) | — |
| **M18.B.3** | Real isotonic calibration (fit-val / apply-test / persist) | — |
| **M18.B.4** | Strict production thinness gates (separate profile) | — |
| **M18.B.5** | NaN/missingness policy (per-group fill + indicators) | — |
| **M18.B.6** | AV failure-reason persistence | — |
| **M18.B.7** | Content-addressed feature_store / label_store (atomic, parallel-safe) | — |
| **M18.B.8** | Dataset/model artifact persistence + model-card output | B.7 |
| **M18.B.9** | Full CLI completion (build/train/evaluate/demote) | B.8 |
| **M18.B.10** | Advanced monitoring (leaderboard, run manifest, cache stats, data-quality gate, regime-aware validation, shadow monitoring) + one-command audit + final docs | B.1–B.9 |

Self-contained, low-risk phases (B.1, B.2, B.3, B.4, B.5, B.6) can land first in
any order; B.7→B.8→B.9 are the larger dependent chain; B.10 closes out.

---

## 9. Tests required per phase

- **B.1:** RF trains; two-subprocess byte-identical refit; used only when
  requested/`fallback` config; G5/G6 cohort integration.
- **B.2:** each SR-8 component independently changes the hash; identical inputs
  → identical hash; v1 back-compat preserved.
- **B.3:** calibrator fits on val only (assert train indices unused); applied to
  test; monotonic; pre/post Brier improves on a miscalibrated fixture;
  persist/reload round-trip.
- **B.4:** production profile blocks a thin dataset at promotion; cold-start
  still builds; fixture_mode bypass still tags `fixture_only`.
- **B.5:** warmup NaN → indicator column set + value imputed per policy;
  signal_history/market_context NaN intentional; no silent 0.0 leakage.
- **B.6:** each AV failure mode records a distinct structured reason.
- **B.7:** deterministic partition paths; rebuild-avoidance (cache hit when bar
  unchanged); atomic write (no corrupt partition on interrupt); no `data/ml/`
  committed; schema/meta round-trip.
- **B.8:** persisted dataset/model round-trips; model-card emitted; artifact set
  matches the documented layout.
- **B.9:** build-dataset→train→evaluate→predict end-to-end smoke; demote status
  transition; registry round-trip.
- **B.10:** leaderboard champion-challenger transitions; run-manifest fields
  present; data-quality gate catches each bad-data fixture; regime-aware check
  blocks catastrophic-regime models; one-command audit returns non-zero on any
  injected violation.

Every phase also re-runs the four standing gates (test_m18_ml, M18 G10, M17.B
G10, M17.B full) and the safety checks.

---

## 10. Push-safe workflow (no more local-only long sessions)

Every meaningful phase MUST:

1. implement a small, scoped change;
2. run the relevant tests + the four standing gates;
3. commit;
4. push to `m18-recovery-from-transcripts`;
5. report the commit hash;
6. wait for review before continuing.

Never push to `main`. Never fabricate the 24 residual tests. Never commit
`data/ml/` artifacts. Never change a protected file, `requirements.txt`,
`main.py`, `bot/data.py`, or `bot/risk.py` without explicit operator approval.

---

## 11. Final acceptance criteria for completed M18

- All current tests pass; all new M18.B tests pass.
- M18 G10 pass; M17.B G10 + full pass.
- `requirements.txt` unchanged unless explicitly approved.
- No `data/ml/` artifacts committed.
- No live/broker/order/dashboard changes; no `signals.db` writes.
- feature_store / label_store work (cache hit/miss, atomic, parallel-safe).
- Full CLI works (build/train/evaluate/predict/registry incl. demote).
- RandomForest fallback works (deterministic).
- Isotonic calibration works (fit-val / apply-test / persist; pre/post metrics).
- repro_hash_v2 works (all SR-8 components; per-component change tests).
- Strict production thinness gates work.
- NaN/missingness policy works.
- AV failure reasons persist.
- Advanced audit command works; run-manifest + leaderboard + data-quality gate +
  regime-aware validation present.
- The 24 residual tests are either recovered (if evidence surfaces) or remain
  explicitly documented as unrecoverable — never fabricated.
