# M18 Recovery Manifest — FINAL

**Recovery branch:** `m18-recovery-from-transcripts`
**Base `main`:** `a8d8ca4` (M17.B closeout)
**Latest functional recovery checkpoint:** `baedf9f` (Checkpoint 4E — G10 hygiene)
**Final audit documentation checkpoint:** `4fe264b`
**Metadata correction checkpoint:** `519878d`
**Current branch tip:** see `git log` on `m18-recovery-from-transcripts`
**Ahead-of-`main` count:**
- Implementation recovery chain through `baedf9f`: 23 commits
- After final audit documentation commit `4fe264b`: 24 commits ahead of `main`
- After metadata correction commit `519878d`: 25 commits ahead of `main`
- Current exact ahead count: verify with `git rev-list --count main..HEAD`

> This is the maximum evidence-supported recovery of the lost M18
> local-only commit chain. **This is NOT a byte-identical restoration.**
> M18 was recovered to **428 OK / skipped=3, M18 G10 10 OK**, with
> **24 original G2–G5 test methods unrecoverable** from available
> evidence.

---

## 1. Original accepted local-only target

| Item | Value |
|---|---|
| Final local HEAD (lost) | `a06fcfe3116a801d0c86d19d68927f119d257114` |
| `test_m18_ml` | 452 OK, skipped=2 |
| M18 `G10_Hygiene` | 10 OK |

The original 11-commit chain (`c76e4f1 → … → a06fcfe`) was never pushed;
a container reset destroyed the working tree before the operator
authorised the push. The git objects are lost. Implementation source
was recovered from `/mnt/transcripts/*m18*.txt` (pre-phase → A.8) and
from the recovery chat sessions (A.9, A.10, and all checkpoint work).

---

## 2. Current recovered branch

| Item | Value |
|---|---|
| Branch | `m18-recovery-from-transcripts` |
| Latest functional checkpoint | `baedf9f` (Checkpoint 4E — G10 hygiene) |
| Final audit documentation checkpoint | `4fe264b` |
| Metadata correction checkpoint | `519878d` |
| Current branch tip | see `git log` on `m18-recovery-from-transcripts` |
| `test_m18_ml` | **428 OK, skipped=3** |
| M18 `G10_Hygiene` | **10 OK** |
| M17.B safety gate | **200 OK, skipped=2** |
| Protected files touched | **0** (all 20 protected files unchanged) |
| `bot/data.py` sha256 | `35a7ff9f88500d4b27444d171268631202ad0eca9809b113e157192ed2538440` (unchanged) |
| `requirements.txt` | unchanged |
| `data/ml/` artifacts | none |

`skipped=3` = three LightGBM-conditional tests (run only when the
optional `lightgbm` dependency is installed). The original target had
`skipped=2`; the extra skip is one additional LightGBM-conditional case
counted differently in the recovered suite, not a deferred test.

---

## 3. Remaining gap

| Item | Value |
|---|---|
| Gap to 452 | **24 tests** |
| Nature | G2–G5 method-level tests within existing (complete) classes |
| Full final `test_m18_ml.py` recovered? | **No** — no create_file writes >200 tests in any source |
| Unapplied G2–G5 test bodies found? | **No** — in transcripts, str_replace ledgers (35 ops), or patches |
| Tests fabricated? | **No** — per the no-blind-invention rule |

The 24 residual are **NOT missing classes** — the M18 G2–G5 class set is
confirmed complete against all evidence. (An earlier "missing classes"
finding was a false positive: M17 backtesting classes share the same
G-numbering scheme.) For every G2–G5 class, the current test count is
≥ the maximum count found in any evidence block. The 24 are method-level
additions whose exact bodies were never captured in any recoverable
artifact.

### Final residual search (run at audit time)

`recovery_audit/final_commit_audit/final_residual_24_search.txt`
captures the last grep across `/mnt/transcripts`, `recovery_extracted`,
and `/tmp`. Conclusion, restated:

- No full final `test_m18_ml.py` was recovered.
- No unapplied G2–G5 test bodies were found.
- The remaining 24 tests are evidence-unrecoverable and were not
  fabricated.

---

## 4. Classification table (byte-faithful vs contract-faithful vs unrecoverable)

| Area | Files | Status | Evidence | Notes |
|---|---|---|---|---|
| G2–G9 test blocks | `test_m18_ml.py` | byte-faithful where source existed | transcript create_file blocks / str_replace new_str | except the documented contract repairs below |
| G1 | `test_m18_ml.py` | contract-faithful / not byte-identical | A.5 class inventory + production contracts | original G1 bodies unavailable; 5 of 9 classes had no recoverable test names |
| G1_DatasetConfig | `test_m18_ml.py` | contract-faithful / not byte-identical | final schema from deep-scan displayed source | rebuilt 4 → 15 tests against the corrected schema |
| DatasetConfig | `bot/ml/schemas.py` + `configs/ml/*.json` | contract-faithful / not byte-identical | deep-scan transcript displayed source (full from_dict/to_dict) | final schema restored: symbols/labels/start_date/end_date/train_pct/val_pct/test_pct/embargo_trading_days/require_intraday/fixture_mode |
| G10 | `test_m18_ml.py` | mixed | A.5 body fragments + M17.B byte-faithful analogs + contract | 3 byte-faithful + 4 contract-faithful (see §5) |
| CLI | `bot/ml/cli.py` | contract-faithful / not byte-identical | G9 byte-faithful tests | output contract aligned (command/n_entries/entry keys; --override-gate append; stub tags; "does not exist") |
| Evaluation | `bot/ml/evaluation/*` | contract-faithful / not byte-identical | G6/G7/G8 byte-faithful tests | v2 kwargs + package export surface drift fixed |
| Manifest/trainer fixes | `bot/ml/dataset/manifest.py`, `bot/ml/models/trainer.py` | contract-faithful / not byte-identical | G4/G5/G6 tests exposed drift | canonical_json import; IMPLEMENTED_MODEL_TYPES guard; coverage_degraded migration |
| 24 residual tests | G2–G5 methods | **unrecoverable** | final residual search | documented here; NOT fabricated |

### Production files corrected during recovery (all contract-faithful / NOT byte-identical)

These were changed only because a byte-faithful test proved a real
reconstruction defect; none has a byte-faithful production source in any
transcript:

- `bot/ml/schemas.py` — DatasetConfig final schema; ALLOWED_LABEL_CLASSES
- `bot/ml/labels/__init__.py`
- `bot/ml/dataset/manifest.py` — canonical_json/sha256_hex import fix
- `bot/ml/models/trainer.py` — IMPLEMENTED_MODEL_TYPES guard; coverage rename
- `bot/ml/evaluation/{__init__,evaluator,report,trading_metrics}.py` —
  v2 export surface; evaluate_model v2 kwargs (cost_per_trade_log_return,
  permutation_n_repeats/n_top, breakdowns_min_samples,
  drift_warning_threshold); equity_curve None keys
- `bot/ml/cli.py` — A.9 API alignment; G9 output contract; --override-gate
  append; accepted-A.9-surface wording

---

## 5. G10 hygiene test provenance (10 total)

| Test | Status |
|---|---|
| `test_all_bot_ml_files_compile` | byte-faithful |
| `test_no_socket_at_import_time` | byte-faithful |
| `test_only_m16_loader_imports_bot_historical` | byte-faithful |
| `test_no_forbidden_imports_in_bot_ml` | byte-faithful (A.5 fragment) |
| `test_no_unexpected_files_added` | byte-faithful (A.5 fragment) |
| `test_data_ml_gitignored` | byte-faithful (A.5 fragment) |
| `test_no_network_libs_imported` | contract-faithful / not byte-identical |
| `test_m17b_forbidden_baseline_preserved` | contract-faithful / not byte-identical |
| `test_bot_historical_only_in_m16_loader` | contract-faithful / not byte-identical |
| `test_m18_new_forbidden_additions_present` | contract-faithful / not byte-identical |

**G10 forbidden-import decision (corrected):** `bot.backtesting` is NOT
forbidden — M18 features legitimately reuse
`bot.backtesting.indicators / .mtf_context / .strategy` for
scanner-replica parity (verified against production). The M18-specific
forbidden additions are the executor/order surfaces `bot.main` and
`bot.recovery_executor` (a read-only/shadow-only milestone must never
import the live order executor). The G10 docstrings were corrected to
match this decision.

---

## 6. Commit audit: 23 implementation/recovery commits through `baedf9f`, plus final audit/documentation commit `4fe264b`

Full per-commit detail: `recovery_audit/final_commit_audit/commit_audit.txt`.

The final audit commit `4fe264b` was separately reviewed for
documentation-only changes and did not alter production logic or test
behaviour (the `test_m18_ml.py` changes are two G10 docstrings; the
`bot/ml/cli.py` changes are module-docstring/comment text only).

Every commit touched only M18-scope paths: `bot/ml/*`, `configs/ml/*`,
`test_m18_ml.py`, `test_m17_backtesting.py` (M18 whitelist filter only —
no M17 production/behaviour change), the M18 docs/manifest, and
`.gitignore`. Safety checks at audit time:

- `recovery_audit/final_commit_audit/protected_or_risky_changes.txt` — **EMPTY**
- `recovery_audit/final_commit_audit/data_ml_files.txt` — **EMPTY**
- `git diff a8d8ca4..HEAD -- requirements.txt` — **EMPTY**
- `git diff a8d8ca4..HEAD -- bot/data.py` — **EMPTY**
- `git diff a8d8ca4..HEAD -- main.py` — **EMPTY**
- `git diff a8d8ca4..HEAD -- bot/risk.py` — **EMPTY**

No live/broker/scanner/order/dashboard path was changed.

### Outdated commit-message claims (corrected by this audit)

- Early commits (`85380ab`, `3f8f647`, `8b6473d`) and the A.9 CLI
  header said the CLI surface was "UNCHANGED from M18.A.1." That is
  superseded: A.9 wired the safe partial surface (predict + registry
  list/show/promote live; four documented stubs). The CLI wording is
  now corrected to "the accepted M18.A.9 safe partial surface only."
- The interim Checkpoint 4E commit reasoning briefly forbade
  `bot.backtesting`; that was corrected in the same checkpoint to the
  executor surfaces, and the docstrings are now consistent.

---

## 7. Honest final status

M18 recovered to the maximum evidence-supported state:
**428 OK / skipped=3, M18 G10 10 OK**, with **24 original G2–G5 test
methods unrecoverable** from available evidence. The original
**452 OK / skipped=2** target was **not** byte-identically recoverable.

### Not accepted as final M18 (decision recorded)

This recovered branch is accepted as a **safe baseline**, NOT as final M18.
An original-plan-vs-code audit found that the *accepted* M18 had itself
narrowed several items from the original "M18 Final Architecture v2" plan.
Now that the target is the full original plan implemented and improved, the
following are missing or materially reduced and must be completed before final
acceptance: RandomForest fallback; repro_hash_v2 (full SR-8); real isotonic
calibration; strict production thinness gates; NaN/missingness policy;
adversarial-validation failure-reason persistence; content-addressed
feature_store/label_store; dataset/model artifact persistence; full CLI
(build/train/evaluate/demote); original artifact / model-card layout.

The full corrected audit, the advanced M18+ requirements, the M18.B phase plan,
per-phase tests, the push-safe workflow, and final acceptance criteria are in
**`docs/M18_COMPLETION_PLAN.md`**.

**M18.B progress:** M18.B.1 (RandomForest fallback + permutation-importance
integration), M18.B.2 (repro_hash_v2 full SR-8 composition, with
`m16_bars_digest` persisted in `DatasetManifest` and `TrainOutputs.repro_hash_v2`
populated), and M18.B.3 (real isotonic calibration — `fit_isotonic_calibration`
fits on val only and applies to test; JSON-safe artifact;
`EvaluationReport.isotonic_calibration` with pre/post Brier/ECE/MCE) are DONE on
the branch, plus a B1–B3 audit-hardening pass (RF rejects non-finite + non-0/1
targets; repro_hash_v2 is fail-closed in the trainer; isotonic validates
val/test shapes + non-binary labels, is strict-JSON safe, and
`apply_isotonic_artifact` validates the artifact). M18.B.4 (strict production
thinness gates — a separate `ProductionThinnessThresholds` profile of
2000/500/100/50 evaluated for every model, attached to
`TrainOutputs.production_thinness_status`, emitted as integrity-class
`production:*` blocked reasons that `--force` cannot override; the profile is
non-bypassable — a non-locked (relaxed) profile emits
`production_threshold_profile_not_locked` so relaxed thresholds can never create
a promotable model; trainability gates unchanged so fixtures still train) is DONE. Suite at 534 OK / skipped=3

M18.B.5 (explicit NaN/missingness policy) is DONE: central
`bot/ml/features/missingness.py` (`m18_missingness_v1`) covers all 10 feature
groups with deterministic neutral fill (0.0) + per-column `<feature>__was_missing`
indicators; `expect_no_missing` groups (scanner_replica, symbol_meta) surface
unexpected missingness in the report. The prior silent `X[np.isnan(X)] = 0.0`
in `models/base.py` is replaced by `apply_missingness_fill()` +
`assert_finite_matrix()` (raises `M18DataError` on remaining NaN/inf/object
before `.fit()`). A JSON-safe `missingness_report` + `missingness_policy_hash`
are persisted in `DatasetManifest` (filtered `from_dict` keeps old manifests
round-tripping) and surfaced on `TrainOutputs`; the policy hash is folded into
`compute_dataset_hash`, so the dataset hash and `repro_hash_v2` change when the
policy changes. Known limitation: indicator columns are NOT materialised as
persisted dataset columns — policy-change detection is via the policy hash in
the dataset hash, not via schema columns.
with these added.
The 428-OK figures above are the pre-M18.B recovery baseline.

Remaining tasks: execute the M18.B completion phases (push-safe, one phase per
commit); recover the 24 residual tests only if further evidence surfaces (never
fabricate); M13.4A (Dashboard Broker Allocation) remains deferred.
