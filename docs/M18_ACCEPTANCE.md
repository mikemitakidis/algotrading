# M18 Acceptance Report

**Milestone:** M18 — ML strategy/criteria foundation (read-only / shadow-only)
**Branch:** `m18-recovery-from-transcripts`
**Status:** foundation complete on the branch; **NOT merged to main**; awaiting
explicit operator approval to merge.
**Mode throughout:** read-only / shadow-only. No live trading, no broker
execution, no dashboard mutation, no scanner production-path mutation, no
`signals.db` writes from the M18 workflow.

This document is the single consolidated acceptance summary for the M18.B
hardening/completion phases. It does not claim M18 is live, promoted, or
M19 scoring — it is the ML *foundation*.

---

## 1. What M18 is (and is not)

**Is:** a reproducible, gated, auditable ML foundation — feature engineering,
label generation, dataset assembly, walk-forward splits, adversarial
validation, baseline + RandomForest models, reproducibility hashing, isotonic
calibration (stored), missingness policy, production thinness gates, a model
registry with artifact-consistency verification, a read-only prediction path,
a workflow CLI, a read-only audit runner, and an advisory readiness reporter.

**Is not:** live trading; automatic execution; a promotion of any model to
live; M19 signal scoring; optimisation; news/sentiment/macro; a self-learning
loop. Readiness is advisory only and is **not** a promotion gate.

---

## 2. Cleared phases

| Phase | Summary | Key commit(s) |
|---|---|---|
| **B8** | Dataset/model artifact persistence + registry artifact-consistency verification; `verify_artifact_consistency` reads & validates every persisted artifact; promotion fails closed (integrity, `--force` cannot override) on any mismatch | `949e92c`, `52b5b34`, `fd8bd56` |
| **B9.A** | CLI hardening: global `--json` strict envelope `{ok,command,status,result,warnings,errors}`, `--debug`, `registry demote` wired via `--scope-key`, `--dry-run` for promote/demote (rejects `--dry-run --force` cleanly); legacy default output preserved | `88756ac`, `d242f4e` |
| **B9.B-1** | `build-dataset` / `train` / `evaluate` wired in-process through existing real interfaces (load_bars → assembler → trainer → evaluator → register_candidate; evaluate = strict registry/artifact inspection); explicit flags only | `cfdb11d` |
| **B10** | Read-only audit/safety runner `bot/ml/audit.py` + `cli audit` (static / hygiene / full modes); invokes existing G10 hygiene tests rather than reimplementing them | `8bc15e2` |
| **B11** | Read-only **advisory** model-readiness reporter `bot/ml/readiness.py` + `cli readiness`; overfit gap, stored-calibration verdict, baseline verdict, regime coverage, thinness; advisory only | `bab58c1` |
| **B12** | Final docs / acceptance / merge-prep (this document + status-doc reconciliation); no code change | this commit |

Earlier B-phases on the branch: B1 (RandomForest fallback), B2 (`repro_hash_v2`,
fail-closed), B3 (isotonic calibration — stored), B4 (strict production thinness
gates, integrity-class), B5 (explicit missingness policy + indicators), B6
(adversarial-validation status/reason persistence), B7 (content-addressed
feature/label stores).

---

## 3. Proof table

| Requirement | Evidence | Verification | Limitation |
|---|---|---|---|
| Persisted artifacts agree with the model/data path that produced them | B8 `Registry.verify_artifact_consistency`; reads train_outputs/eval_report/feature_summary/metadata/X/y and cross-checks identity, widths, split counts | `G8_ArtifactConsistency` 33 OK; promotion fails closed on injected mismatch | "model artifact" = deterministic-refit source, not a frozen-weights blob |
| Promotion cannot be forced past integrity problems | B8 integrity gate; `--force`/`--override-gate` cannot override | `G8_IntegrityCannotBeForced`, `G9_CliSafety` | judgment gates (non-integrity) remain force-overridable by design |
| CLI emits machine-parseable results | B9.A `--json` envelope, valid on success+failure | `G9_CliJsonEnvelope`, `G9_CliSafety` | envelope is opt-in; legacy default output preserved |
| `registry demote` wired safely | B9.A `demote_current(--scope-key)`; dry-run no mutation | `G9_CliDemote`, `G9_CliDryRun` | demotion is by scope, not model_id |
| `--dry-run --force` not misleading | B9.A rejects the combination cleanly (exit 1, valid envelope) | `G9_CliDryRun` | forced-promotion dry-run simulation intentionally unsupported |
| build/train/evaluate run the real pipeline | B9.B-1 in-process wiring; train registers real B8 artifacts | `G9_CliBuildDataset`/`Train`/`Evaluate`/`Workflow` | train assembles its own dataset (no build→train handoff yet — B9.C) |
| One-command safety audit | B10 `bot/ml/audit.py` + `cli audit`; read-only | `G10_AuditRunner` 16 OK; real `cli audit` smoke | heavy suites opt-in via `--mode full` |
| Advisory model-readiness summary | B11 `assess_readiness`; consumes stored eval report | `G11_Readiness` 13 OK; real `cli readiness` smoke | advisory only; not a promotion gate |
| Full suite green | 668 OK (skipped=3) | batched run reconciles to loader count 668 | — |
| Safety invariants hold | protected files unchanged vs origin/main; requirements unchanged; no `data/ml` committed; no live/broker/dashboard/scanner/signals.db writes | `G10_Hygiene` (M18 10, M17.B 9); `cli audit`; protected-path diff = 0 | — |

---

## 4. Final branch / main state

- `origin/main` = `a8d8ca4` — **M17.B.closeout**.
- M1–M17 are on main; M17.B full regression is 200 OK (skipped=2).
- M18 lives entirely on `m18-recovery-from-transcripts`.
- Branch is **ahead 50, behind 0** of `origin/main`.
- **M18 is not on main** (`git ls-tree -r origin/main | grep -c bot/ml/` = 0).

---

## 5. Known limitations (recorded honestly)

- **B9.C deferred:** true `AssemblerResult` persistence/reload for a real
  build-dataset → train handoff (new dataset-layer architecture). `train`
  currently assembles its own dataset in-process.
- **B3 gap:** stored isotonic calibration exists and can be assessed, but
  calibration is **not applied at predict time**. Predict/readiness both flag
  `predict_time_calibration_applied: false`; no output implies live predictions
  are calibrated.
- **B11.x / M21 deferred:** cross-fold feature-importance stability (needs
  walk-forward refits) and any speed pass/fail thresholds.
- **Duplicate G7 test classes** (`G7_PermutationImportance`, `G7_ThresholdTable`
  defined twice) remain a latent **test-organisation** trap — Python keeps the
  last definition, so the earlier ones are shadowed. This is **not** a current
  suite-correctness blocker: the suite as executed passes and the batched
  counts reconcile to the loader count. Recorded as a future cleanup item; not
  deduplicated in B12 because removing/merging test classes during final
  acceptance risks silently dropping coverage.
- M18 is **not** live trading and **not** M19 signal scoring.
- Readiness is **advisory**, not a promotion gate (promotion stays owned by the
  B4 thinness gates + B8 consistency + registry rules).

---

## 6. Main-merge plan (NOT executed in B12)

Fast-forward only, after explicit operator approval, on a clean verified
checkout, after reviewing the full `git diff origin/main..HEAD` (all changes
under `bot/ml/`, `test_m18_ml.py`, and `docs/` — zero protected-file changes):

```
git checkout main
git pull --ff-only origin main
git merge --ff-only m18-recovery-from-transcripts
git push origin main
```

No squash. No merge commit. No force push. The branch is ahead 50 / behind 0,
so a fast-forward is possible and preserves the audited per-phase history.
**This is not executed during B12.**
