# M18.A — ML Pipeline / Closed-Loop ML — Status

**Milestone:** M18.A (ML Pipeline / Closed-Loop ML)
**Recovered branch:** `m18-recovery-from-transcripts`
**Latest functional recovery checkpoint:** `baedf9f` (Checkpoint 4E — G10 hygiene)
**Final audit documentation checkpoint:** `4fe264b`
**Metadata correction checkpoint:** `519878d`
**Current branch tip:** see `git log` on `m18-recovery-from-transcripts`
**Ahead-of-`main` count:**
- Implementation recovery chain through `baedf9f`: 23 commits
- After final audit documentation commit `4fe264b`: 24 commits ahead of `main`
- After metadata correction commit `519878d`: 25 commits ahead of `main`
- Current exact ahead count: verify with `git rev-list --count main..HEAD`

**Mode throughout:** read-only / shadow-only — no live promotion.

### Current recovered state

| Metric | Value |
|---|---|
| `test_m18_ml` | **428 OK, skipped=3** |
| M18 `G10_Hygiene` | **10 OK** |
| M17.B safety gate | **200 OK, skipped=2** |
| Original local-only target | 452 OK, skipped=2 |
| Remaining gap | **24 unrecoverable G2–G5 method-level tests** |

M18 was recovered to the maximum evidence-supported state:
**428 OK / skipped=3, M18 G10 10 OK**, with 24 original G2–G5 test
methods unrecoverable from available evidence. The original 452 OK
target was **not** byte-identically recoverable — see
`RECOVERY_M18_MANIFEST.md` for the full byte-faithful vs
contract-faithful vs unrecoverable breakdown. This is **not** a claim
of byte-identical restoration.

> **Not accepted as final M18.** This branch is a safe baseline. An
> original-plan-vs-code audit found items missing or materially reduced
> versus "M18 Final Architecture v2" (RandomForest fallback,
> repro_hash_v2, real isotonic calibration, strict production thinness
> gates, NaN/missingness policy, AV failure-reason persistence,
> feature_store/label_store, artifact persistence, full CLI). The
> completion roadmap, advanced M18+ requirements, M18.B phases, and
> final acceptance criteria are in **`docs/M18_COMPLETION_PLAN.md`**.

---

## Hard invariants (asserted by G10)

- `signals.db` is NEVER written by ML code. The read-only enforcement
  applies to every module under `bot/ml/`; only `bot/ml/dataset/m16_loader.py`
  may import `bot.historical` (SR-7).
- `data/ml/` is gitignored. No model artifact is committed.
- `ALWAYS_FALSE_APPROVED_FOR_LIVE = False` on every registry entry.
- ML remains read-only / shadow-only throughout M18 — no live
  promotion is ever attempted from this milestone.
- The registry's `predictions.py` is read-only — its import surface
  is AST-checked against any write path (open in 'w', signals.db,
  broker calls, etc.).

---

## Sub-milestones

| Sub | Hash (original) | Deliverable |
|---|---|---|
| pre-phase | `c76e4f1` | M17.B G10 whitelist extension to allow M18 paths |
| A.1 | `5ed45e4` | Package skeleton + schemas + AST guard expansion |
| A.2 | `be6c0bf` | M16 loader + 5 safe feature groups |
| A.3 | `7c8f3db` | 5 extended features (mtf_confluence, scanner_replica, market_context, signal_history, symbol_meta) |
| A.4 | `cf5b4b7` | Triple-barrier + 10 locked secondary labels |
| A.5 | `156f94b` | Dataset assembler + walk-forward + adversarial validation |
| A.6 | `23c376d` | Baselines (B0/B1/B2) + dual-cohort trainer + LightGBM gate |
| A.7 | `2ca0b46` | EvaluationReport v2 + metrics/drift/permutation/breakdowns |
| A.8 | `ae7ca4b` | File-based registry + read-only predictions + Q17/Q20 fixes |
| A.9 | `cd4ce36` | Safe partial CLI wiring (predict + registry list/show/promote live; rest documented stubs) |
| A.10 | `a06fcfe` | Final hardening + evidence prep |

---

## Locked Q-decisions (do not silently widen)

- **Q16** `fixture_mode` is a permanent tag — once a registry entry
  is recorded as `fixture_only`, it stays that way.
- **Q17** Promotion gates are classified — **integrity** gates
  (`fixture_only`, `coverage_degraded`, `failed_adversarial_validation`,
  `failed_drift_check`) are NEVER overridable; **judgment** gates
  (`failed_baseline_beat`, `failed_sample_count`) are overridable
  with `--force --override-gate <gate> --reason <text>`.
- **Q18** `train_mode` ↔ `anchor_set` is a strict 1:1 mapping
  (`model_a_meta_label` ↔ `model_a_scanner_replica`;
  `model_b_candidate_quality` ↔ `model_b_1h_union_candidates`).
  Mismatch raises `M18ConfigError` at training time. A ⊆ B is
  proven structurally by the assembler.
- **Q19** Anchor timeframe is strict — the assembler does not silently
  downgrade; coverage gaps surface as `coverage_degraded=True` plus
  a `degradation_warning` string in the manifest.
- **Q20** Prediction-row schema is locked:
  `{prediction, predicted_class, model_id,
    feature_extrapolation_flags, feature_extrapolation_count}` plus
  back-compat aliases. Envelope is `[q01, q99]` for both predicted-class
  boundaries.
- **Q22** LightGBM determinism flags (`deterministic=True`,
  `force_col_wise=True`) are required when the optional dep is
  installed.

---

## Acceptance tests (G1–G10)

| Block | Coverage |
|---|---|
| G1_CLI | Subcommand surface, stub-message literals, exit codes |
| G2 | M16 loader + 5 safe feature groups (point-in-time, lookback bounds, leak_class) |
| G3 | Extended features + 10 labels (locked target_values, leak_class=future_label_only) |
| G4 | Dataset assembler (anchor set, walk-forward embargo/purge, coverage_degraded) |
| G5 | Adversarial validation (ROC-AUC envelope, train/test separability) |
| G6 | Baselines + dual-cohort trainer (A ⊆ B proof, B0/B1/B2 parity, thinness gates) |
| G7 | EvaluationReport v2 (PR-AUC, threshold table, drift PSI, permutation, breakdowns) |
| G8 | Registry (status inference, promotion gates, Q17 force-override matrix, Q20 envelope + schema) |
| G9 | M18.A.9 CLI surface: predict, registry list/show/promote, documented stubs (exit 2), no repo pollution |
| G10_Hygiene | AST no-write-to-signals.db, no-socket-at-import, no forbidden/network/executor imports, bot.historical sole-importer, data/ml gitignore, M17.B baseline preservation, file-scope drift guard |

---

## Recovery note (this branch)

The 11 local-only commits originally landed at:

    c76e4f1 → 5ed45e4 → be6c0bf → 7c8f3db → cf5b4b7 → 156f94b →
    23c376d → 2ca0b46 → ae7ca4b → cd4ce36 → a06fcfe

were never pushed; the working tree was wiped by a container reset
before the operator authorised the push. The git objects are lost,
but the implementation source is recoverable from
`/mnt/transcripts/*m18*.txt` for sub-milestones pre-phase through A.8.
A.9 and A.10 were reconstructed from this chat session's history.

Per the recovery directive, every recovered file is tagged in its
landing commit with either `byte-faithful` or
`RECONSTRUCTED_FROM_TRANSCRIPT_NOT_BYTE_IDENTICAL`. The recovery
manifest at `RECOVERY_M18_MANIFEST.md` (sections §1–§11) is the
definitive record of evidence and gaps.

Files deliberately NOT added because they have zero transcript
evidence (likely never existed):

- `bot/ml/features/benchmark.py`
- `bot/ml/evaluation/baseline_compare.py`
- `bot/ml/evaluation/binary_metrics_extended.py`

If any of these surface as `ImportError` during test runs they will be
reconstructed at that point.
