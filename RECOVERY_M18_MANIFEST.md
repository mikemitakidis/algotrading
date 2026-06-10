# M18 Recovery Manifest — from `/mnt/transcripts/*m18*.txt`

**Recovery branch:** `m18-recovery-from-transcripts`
**Base:** `origin/main` at `a8d8ca44` (M17.B.closeout, 2026-06-08 13:28:41).
**Scope:** reconstruct the lost M18 local-only commit chain (originally
`c76e4f1` → `a06fcfe`) as a single new branch, never touching `main`.

> **The git objects are lost, but the implementation source is
> recoverable from transcripts** for the phases listed below.
> For files not present in any transcript, the source must come
> from this conversation's history (current chat session).

---

## 0. Top-line summary

| Item | Value |
|---|---|
| Transcripts found | 9 `*m18*.txt` files, 7.5 MB total + `journal.txt` (16 KB) |
| Date range of transcripts | 2026-06-08 21:55 to 2026-06-09 23:34 UTC |
| Phases covered in transcripts (high confidence) | M18.A.pre-phase, M18.A.1, M18.A.2, M18.A.3, M18.A.4, M18.A.5, M18.A.6, M18.A.7 |
| Phases NOT covered in transcripts | M18.A.8 (registry), M18.A.9 (CLI), M18.A.10 (docs) — those sessions finished after the last transcript was finalised; their content must come from this chat session's history |
| Files reconstructable from transcripts | ~40 of the ~55 production files in `bot/ml/` |
| Files that MUST come from chat history (not in transcripts) | ~15 (registry package, evaluation extension modules, docs/M18_status.md, config examples) — see § 4 |

---

## 1. Transcript files found

| # | File | Size | Date | JSON arrays | Tool-call census |
|---|---|---|---|---|---|
| 1 | `recovery_inputs/m18_transcripts/2026-06-08-21-55-56-m18-planning-and-a1-start.txt` | 776 K | 2026-06-08 21:55 | 6 | 4 create_file, 34 str_replace, 94 bash, 46 view |
| 2 | `recovery_inputs/m18_transcripts/2026-06-08-22-07-41-m18-planning-and-a1-start.txt` | 1.2 M | 2026-06-08 22:07 | 9 | 6 create_file, 78 str_replace, 146 bash, 68 view |
| 3 | `recovery_inputs/m18_transcripts/2026-06-08-22-41-14-m18-a2-implementation.txt` | 218 K | 2026-06-08 22:41 | 3 | **0** create_file, **0** str_replace, 14 bash, 6 view |
| 4 | `recovery_inputs/m18_transcripts/2026-06-09-08-31-58-2026-06-09-m18a3-implementation.txt` | 129 K | 2026-06-09 08:31 | 2 | **0** create_file, **0** str_replace, 22 bash, 0 view |
| 5 | `recovery_inputs/m18_transcripts/2026-06-09-09-44-31-m18-a5-dataset-assembler.txt` | 1.3 M | 2026-06-09 09:44 | 6 | 50 create_file, 28 str_replace, 136 bash, 30 view |
| 6 | `recovery_inputs/m18_transcripts/2026-06-09-11-36-27-m18-a6-trainer-implementation.txt` | 1.4 M | 2026-06-09 11:36 | 6 | 56 create_file, 28 str_replace, 110 bash, 20 view |
| 7 | `recovery_inputs/m18_transcripts/2026-06-09-20-46-39-m18-a7-evaluation-amend.txt` | 1.2 M | 2026-06-09 20:46 | 6 | 34 create_file, 40 str_replace, 98 bash, 20 view |
| 8 | `recovery_inputs/m18_transcripts/2026-06-09-21-20-53-2026-06-09-m18-a8-registry-implementation.txt` | 505 K | 2026-06-09 21:20 | 2 | 16 create_file, 8 str_replace, 30 bash, 2 view |
| 9 | `recovery_inputs/m18_transcripts/2026-06-09-23-34-05-2026-06-09-m18-a10-final-hardening.txt` | 990 K | 2026-06-09 23:34 | 3 | 22 create_file, 4 str_replace, 74 bash, 14 view |
|   | `recovery_inputs/journal.txt` | 16 K | 2026-06-09 23:34 | — | catalog of all sessions with one-paragraph summaries |

**Important:** transcript filenames are NOT 1-to-1 with phase numbers. The
filename reflects the phase that was *about to start* when the session opened,
but the file content captures the work that was actually *delivered* in that
session — which is typically the PREVIOUS phase. See § 2.

**No separate transcript for M18.A.4** — per `journal.txt`, M18.A.4 work
(triple-barrier + 10 secondary labels) is folded into transcript #5
(the "m18-a5-dataset-assembler" file).

**No separate transcript for M18.A.9** — M18.A.9 work was begun and
completed inside transcript #9 ("m18-a10-final-hardening"), which also
covers M18.A.7 amend acceptance and the start of M18.A.10.

---

## 2. Phase → transcript mapping

| Phase | Hash (lost) | Transcript that contains the implementation | Recoverability |
|---|---|---|---|
| M18.A.pre-phase | `c76e4f1` | #1 + #2 (G10 whitelist extension in M17.B test) | str_replace sequence on `test_m17_backtesting.py` — recoverable |
| M18.A.1 | `5ed45e4` | #2 (skeleton + schemas + AST guard) | mostly create_file — recoverable, BUT `bot/ml/__init__.py`, `bot/ml/schemas.py`, `bot/ml/cli.py`, `bot/ml/errors.py` are NOT in any create_file inventory — they were built via bash heredocs in transcripts #3 and #4 |
| M18.A.2 | `be6c0bf` | #3 (M16 loader + 5 safe feature groups) | **HEREDOC-ONLY** — no create_file calls; content must be reconstructed from the `cat > path << EOF ... EOF` patterns inside bash_tool calls. Cross-referenced + amended in transcript #5 |
| M18.A.3 | `7c8f3db` | #4 (5 extended feature groups) | **HEREDOC-ONLY** — same pattern as A.2. Subsequent str_replace amendments visible in transcript #5 |
| M18.A.4 | `cf5b4b7` | #5 (triple-barrier + 10 secondary labels) | create_file blocks for label modules present in transcripts #5 and #6 |
| M18.A.5 | `156f94b` | #6 (dataset assembler + walk-forward + adversarial validation) | 12 create_file blocks for `bot/ml/dataset/{anchors,coverage,manifest,walk_forward,adversarial_validation,assembler,_m16_backfill}.py` (the last lands in transcript #7's A.5 follow-up) |
| M18.A.6 | `23c376d` | #7 (B0/B1/B2 baselines + import-gated LightGBM + thinness gates + trainer) | 7 create_file blocks in transcript #7 |
| M18.A.7 | `2ca0b46` | #8 + #9 (initial evaluation v1, then v2 amend) | create_file blocks for `bot/ml/evaluation/{__init__,calibration,evaluator,report,trading_metrics}.py` in transcript #8; v2 amend adds `ml_metrics.py` in transcript #9. **Files `drift.py`, `permutation_importance.py`, `threshold_metrics.py`, `breakdowns.py`, `baseline_compare.py`, `binary_metrics_extended.py` are MENTIONED in transcript #9 but NOT visible as create_file blocks in my extraction** — see § 4 |
| M18.A.8 | `ae7ca4b` | **NOT IN ANY TRANSCRIPT** — the M18.A.8 transcript file (#8) is misleadingly named; per its own contents and per journal.txt, M18.A.8's registry implementation was DONE in this conversation's current session (this chat). Source: chat history |
| M18.A.9 | `cd4ce36` | **NOT IN ANY TRANSCRIPT** — source: chat history (current session) |
| M18.A.10 | `a06fcfe` | **NOT IN ANY TRANSCRIPT** — source: chat history (current session) |

**Journal evidence backing this mapping** — `journal.txt` entry for transcript
#9 says verbatim: *"Contains M18.A.7 amend acceptance, M18.A.8
implementation with two Q20 corrections (envelope + schema), M18.A.9 CLI
wiring with documented stubs, all accepted locally. M18.A.10 work in
progress … Final accepted HEAD: cd4ce36."* But my JSON-walk of that
transcript's `create_file`/`str_replace` calls returns ONLY evaluation-module
files — not registry files, not CLI changes, not docs. The journal entry
describes what the SESSION discussed; the actual artifact-producing tool
calls happened in subsequent sessions whose transcripts were not yet
finalised when `/mnt/transcripts/` was snapshotted.

---

## 3. Files recoverable from transcripts (create_file or str_replace)

### Foundation (M18.A.1)

| File | Source | Recoverability |
|---|---|---|
| `bot/ml/__init__.py` | Heredoc in transcript #3 or #4 — needs careful extraction | `bash_heredoc` (medium confidence) |
| `bot/ml/errors.py` | str_replace ×2 in transcript #5; content built incrementally | `str_replace_sequence` — verify final state |
| `bot/ml/schemas.py` | Mentioned 19 times in transcript #5 + 8 in #6; **no create_file detected**. Likely written via bash heredoc in transcript #3/#4 | `bash_heredoc` (medium confidence) — search heredoc text directly |
| `bot/ml/cli.py` (M18.A.1 stub version) | Mentioned 3 times in transcript #5; same pattern | `bash_heredoc` (medium confidence) |
| `test_m18_ml.py` | Built incrementally via str_replace across every transcript — recoverable as a sequence | `str_replace_sequence` |

### M16 loader + safe feature groups (M18.A.2)

| File | Source | Recoverability |
|---|---|---|
| `bot/ml/dataset/__init__.py` | create_file in transcript #5 | `create_file_full` ✓ |
| `bot/ml/dataset/m16_loader.py` | create_file in transcript #5; str_replace ×1 in #7 | `create_file_full + str_replace` ✓ |
| `bot/ml/dataset/flywheel_reader.py` | create_file in transcript #5 | `create_file_full` ✓ |
| `bot/ml/features/__init__.py` | create_file in transcript #5; str_replace ×1 to add A.3 imports | `create_file_full + str_replace` ✓ |
| `bot/ml/features/base.py` | create_file in transcript #5; str_replace ×1 | `create_file_full + str_replace` ✓ |
| `bot/ml/features/price_return.py` | create_file in transcript #5 | `create_file_full` ✓ |
| `bot/ml/features/trend.py` | create_file in transcript #5 | `create_file_full` ✓ |
| `bot/ml/features/momentum.py` | create_file in transcript #5 | `create_file_full` ✓ |
| `bot/ml/features/vol_regime.py` | create_file in transcript #5 | `create_file_full` ✓ |
| `bot/ml/features/volume_liquidity.py` | create_file in transcript #5 | `create_file_full` ✓ |

### Extended feature groups (M18.A.3)

| File | Source | Recoverability |
|---|---|---|
| `bot/ml/features/mtf_confluence.py` | create_file in transcript #5 | `create_file_full` ✓ |
| `bot/ml/features/scanner_replica.py` | create_file in transcript #5 | `create_file_full` ✓ |
| `bot/ml/features/market_context.py` | create_file in transcript #5 | `create_file_full` ✓ |
| `bot/ml/features/signal_history.py` | create_file in transcript #5 | `create_file_full` ✓ |
| `bot/ml/features/symbol_meta.py` | create_file in transcript #5 | `create_file_full` ✓ |
| `bot/ml/features/benchmark.py` | **MENTIONED in conversation context but ZERO mentions in any transcript** | **MISSING from transcripts** — see § 4 |
| `configs/ml/symbol_metadata.example.json` | create_file in transcript #5 | `create_file_full` ✓ |

### Labels (M18.A.4)

| File | Source | Recoverability |
|---|---|---|
| `bot/ml/labels/__init__.py` | create_file in transcript #5 + re-create in #6 (label-id realignment) | `create_file_full` ✓ (use the latest) |
| `bot/ml/labels/base.py` | create_file in transcripts #5 and #6 | `create_file_full` ✓ |
| `bot/ml/labels/triple_barrier.py` | create_file in transcripts #5 and #6 | `create_file_full` ✓ |
| `bot/ml/labels/forward_returns.py` | create_file in transcripts #5 and #6 | `create_file_full` ✓ |
| `bot/ml/labels/mfe_mae.py` | create_file in transcripts #5 and #6 | `create_file_full` ✓ |
| `bot/ml/labels/risk_adjusted.py` | create_file in transcripts #5 and #6 | `create_file_full` ✓ |

### Dataset assembler (M18.A.5)

| File | Source | Recoverability |
|---|---|---|
| `bot/ml/dataset/anchors.py` | create_file in transcript #6 | `create_file_full` ✓ |
| `bot/ml/dataset/coverage.py` | create_file in transcript #6; str_replace ×1 in #7 | `create_file_full + str_replace` ✓ |
| `bot/ml/dataset/manifest.py` | create_file in transcript #6; str_replace ×1 in #6 | `create_file_full + str_replace` ✓ |
| `bot/ml/dataset/walk_forward.py` | create_file in transcript #6; str_replace ×1 in #6 | `create_file_full + str_replace` ✓ |
| `bot/ml/dataset/adversarial_validation.py` | create_file in transcript #6 | `create_file_full` ✓ |
| `bot/ml/dataset/assembler.py` | create_file in transcript #6; str_replace ×2 (in #6 and #7) | `create_file_full + str_replace_sequence` ✓ |
| `bot/ml/dataset/_m16_backfill.py` | create_file in transcript #7 | `create_file_full` ✓ |

### Models / trainers (M18.A.6)

| File | Source | Recoverability |
|---|---|---|
| `bot/ml/models/__init__.py` | create_file in transcript #7; str_replace ×1 | `create_file_full + str_replace` ✓ |
| `bot/ml/models/base.py` | create_file in transcript #7; str_replace ×1 | `create_file_full + str_replace` ✓ |
| `bot/ml/models/baselines.py` | create_file in transcript #7; str_replace ×1 | `create_file_full + str_replace` ✓ |
| `bot/ml/models/lightgbm_trainer.py` | create_file in transcript #7 | `create_file_full` ✓ |
| `bot/ml/models/thinness_gates.py` | create_file in transcript #7 | `create_file_full` ✓ |
| `bot/ml/models/trainer.py` | create_file in transcript #7; str_replace ×1 | `create_file_full + str_replace` ✓ |

### Evaluation (M18.A.7 v1 + v2 amend)

| File | Source | Recoverability |
|---|---|---|
| `bot/ml/evaluation/__init__.py` | create_file in transcripts #8 and #9 (v2 amend rewrites) | `create_file_full` ✓ (use the latest, from #9) |
| `bot/ml/evaluation/calibration.py` | create_file in #8 and #9 | `create_file_full` ✓ |
| `bot/ml/evaluation/evaluator.py` | create_file in #8 and #9 | `create_file_full` ✓ |
| `bot/ml/evaluation/report.py` | create_file in #8 and #9 | `create_file_full` ✓ |
| `bot/ml/evaluation/trading_metrics.py` | create_file in #8 and #9 | `create_file_full` ✓ |
| `bot/ml/evaluation/ml_metrics.py` | create_file in #9 (v2 amend new file) | `create_file_full` ✓ |
| `bot/ml/evaluation/drift.py` | 17 mentions in #9 but **not detected as create_file** — need second-pass scan | **UNCERTAIN** — likely heredoc or my detector missed |
| `bot/ml/evaluation/permutation_importance.py` | 17 mentions in #9 — same | **UNCERTAIN** |
| `bot/ml/evaluation/threshold_metrics.py` | 17 mentions in #9 — same | **UNCERTAIN** |
| `bot/ml/evaluation/breakdowns.py` | 17 mentions in #9 — same | **UNCERTAIN** |
| `bot/ml/evaluation/baseline_compare.py` | ZERO mentions in any transcript | **MISSING** — § 4 |
| `bot/ml/evaluation/binary_metrics_extended.py` | ZERO mentions in any transcript | **MISSING** — § 4 |

---

## 4. Files NOT in any transcript (must come from this chat session's history)

These files were referenced in the system prompt's M18 summary (which
describes work done in this very conversation), but appear in ZERO
transcripts under `/mnt/transcripts/`:

### M18.A.8 — registry package (6 files, all UNRECOVERABLE from transcripts)

| File | Source | Confidence |
|---|---|---|
| `bot/ml/registry/__init__.py` | Current chat session (M18.A.8 commit `ae7ca4b`) | High — built in this conversation |
| `bot/ml/registry/entry.py` | Current chat session | High |
| `bot/ml/registry/gates.py` | Current chat session | High |
| `bot/ml/registry/storage.py` | Current chat session | High |
| `bot/ml/registry/registry.py` | Current chat session | High |
| `bot/ml/registry/predictions.py` | Current chat session | High |

All 6 files were built in M18.A.8 with two Q20 amends in this conversation.
Reconstruction must come from the conversation's prior assistant turns that
contained the `create_file` calls for these files. Without the originals,
the rebuilt version **will not be byte-identical** to the lost `ae7ca4b`.

### M18.A.7 — evaluation extension modules (UNCERTAIN; need deeper scan)

| File | Possible source | Status |
|---|---|---|
| `bot/ml/evaluation/drift.py` | Transcript #9 (17 mentions) — likely create_file with different regex shape | **NEEDS RE-SCAN** |
| `bot/ml/evaluation/permutation_importance.py` | Transcript #9 (17 mentions) | **NEEDS RE-SCAN** |
| `bot/ml/evaluation/threshold_metrics.py` | Transcript #9 (17 mentions) | **NEEDS RE-SCAN** |
| `bot/ml/evaluation/breakdowns.py` | Transcript #9 (17 mentions) | **NEEDS RE-SCAN** |
| `bot/ml/evaluation/baseline_compare.py` | None | **UNRECOVERABLE** from transcripts — chat history only |
| `bot/ml/evaluation/binary_metrics_extended.py` | None | **UNRECOVERABLE** from transcripts — chat history only |

The "17 mentions" pattern suggests these four files were created in a
larger str_replace or via heredoc; my first-pass regex missed them. Step 4
will run a deeper scan before declaring them unrecoverable.

### M18.A.3 — `bot/ml/features/benchmark.py`

ZERO transcript mentions. The system prompt's M18 summary lists it as part
of M18.A.3 deliverables. Must come from current chat session.

### M18.A.9 — CLI rewrite + config examples

| File | Source | Status |
|---|---|---|
| `bot/ml/cli.py` (M18.A.9 rewrite — wired predict + registry list/show/promote) | Current chat session | Initial stub version may be in transcripts #3/#4 as heredoc; the M18.A.9 rewrite is current-session only |
| `configs/ml/dataset.example.json` | Current chat session | Unrecoverable from transcripts |
| `configs/ml/train.example.json` | Current chat session | Unrecoverable from transcripts |

### M18.A.10 — docs

| File | Source | Status |
|---|---|---|
| `docs/M18_status.md` | Current chat session | Unrecoverable from transcripts; the full content was rendered as an assistant message earlier in this conversation |
| `MILESTONE_STATUS.md` (M18 section addition) | Current chat session | The exact text was rendered as an assistant message; reconstruction will not be byte-identical |
| `test_m18_ml.py` + `test_m17_backtesting.py` (1-char regex fix in G10) | Current chat session | The regex change is documented in chat |

All "current chat session" files will be tagged
`RECONSTRUCTED_FROM_TRANSCRIPT_NOT_BYTE_IDENTICAL` in their reconstruction
commits.

---

## 5. Files outside `bot/ml/` that the transcripts will touch

For honest scope tracking — transcripts show these non-`bot/ml/` paths
being read or modified. Step 4 will only modify them if the transcript
shows an actual edit.

| Path | Modified? | Source phase |
|---|---|---|
| `test_m17_backtesting.py` | YES — str_replace ×1 (G10 whitelist extension, 1 line) | M18.A.pre-phase + M18.A.10 (regex alignment) |
| `MILESTONE_STATUS.md` | YES — str_replace in M18.A.pre-phase + new section in M18.A.10 | Pre-phase + A.10 |
| `ROADMAP.md` | YES — str_replace in M18.A.pre-phase | Pre-phase |
| `docs/NEXT_WORK_REGISTER.md` | YES — str_replace in M18.A.pre-phase | Pre-phase |
| `docs/M17_B_closeout.md` | NO modify, only view | — |

The remaining "create_file" entries that appeared in early planning
transcripts (`bot/backtesting/mtf_context.py`, `docs/M17_A_closeout.md`,
etc.) are M17.B-and-earlier work, NOT M18 scope, and will NOT be touched.

---

## 6. Reconstruction order (matches Step 5 checkpoint plan)

The directive says checkpoints can be reconstructed per-phase OR in larger
package chunks. The order below proceeds in dependency order, with each
group committed and pushed before the next begins.

1. **Foundation** — `bot/ml/__init__.py`, `bot/ml/errors.py`,
   `bot/ml/schemas.py`. Source: transcript #3 / #4 (bash heredoc) +
   transcript #5 (str_replace amendments). Test changes:
   minimal `test_m18_ml.py` skeleton.
2. **M18.A.pre-phase** — G10 whitelist extension on
   `test_m17_backtesting.py`. Source: transcript #1/#2 str_replace.
3. **M18.A.2** — `bot/ml/dataset/{__init__,m16_loader,flywheel_reader}.py`
   + `bot/ml/features/{__init__,base,price_return,trend,momentum,vol_regime,volume_liquidity}.py`.
   Source: transcript #5. Tests for these modules: str_replace on
   `test_m18_ml.py`.
4. **M18.A.3** — `bot/ml/features/{mtf_confluence,scanner_replica,market_context,signal_history,symbol_meta}.py`
   + `configs/ml/symbol_metadata.example.json`. Source: transcript #5.
   **`bot/ml/features/benchmark.py` must come from chat history if
   it actually existed** — flagged as `missing_uncertain`.
5. **M18.A.4** — `bot/ml/labels/{__init__,base,triple_barrier,forward_returns,mfe_mae,risk_adjusted}.py`.
   Source: transcripts #5 (initial) + #6 (label-id realignment).
6. **M18.A.5** — `bot/ml/dataset/{anchors,coverage,manifest,walk_forward,adversarial_validation,assembler,_m16_backfill}.py`.
   Source: transcript #6 + #7's A.5-followup edits.
7. **M18.A.6** — `bot/ml/models/*.py` (6 files).
   Source: transcript #7.
8. **M18.A.7** — `bot/ml/evaluation/*.py` (10 files).
   Source: transcripts #8 (v1) + #9 (v2 amend). Files with "17 mentions"
   in #9 need a deeper scan in Step 4 to determine if they're create_file
   or heredoc. `baseline_compare.py` and `binary_metrics_extended.py`
   come from chat history.
9. **M18.A.8** — `bot/ml/registry/*.py` (6 files) **from current chat
   session only**. Tag every commit `RECONSTRUCTED_FROM_TRANSCRIPT_NOT_BYTE_IDENTICAL`.
10. **M18.A.9** — `bot/ml/cli.py` rewrite + `configs/ml/{dataset,train}.example.json`
    **from current chat session only**. Same NOT_BYTE_IDENTICAL tag.
11. **M18.A.10** — `docs/M18_status.md` + `MILESTONE_STATUS.md` M18 section
    + the 1-char G10 regex alignment in both test files. Same
    NOT_BYTE_IDENTICAL tag.

---

## 7. Constraints carried over to Step 4

Per the directive:

- **No invention.** Module designs, field names, function signatures come
  verbatim from the transcripts. If transcripts show something different
  from my context summary, the transcript wins.
- **No new dependencies.** `requirements.txt` will not change.
- **No live/broker/dashboard/scanner/order paths.** No touches to
  `bot/main.py`, `bot/scanner.py`, `bot/strategy.py`, `bot/risk.py`,
  `bot/risk_authority.py`, `bot/feature_engine.py`, `bot/indicators.py`,
  `bot/data.py`, `bot/providers.py`, `bot/sentiment.py`,
  `bot/flywheel.py`, `bot/notify.py`, `bot/utils.py`, `bot/db.py`,
  `services/*`, `.env.example`, `configs/scanner.yaml`,
  `configs/risk.yaml`.
- **No protected-file touches** unless a transcript shows the edit
  explicitly. (The pre-phase G10 whitelist edit on
  `test_m17_backtesting.py` is shown and is in-scope.)
- **No reconstruction until this manifest is committed and pushed.**

Per-file recoverability tag legend used in § 3:
- `create_file_full` — full content recoverable from one or more
  `create_file` tool_use blocks in a transcript
- `create_file_full + str_replace` — `create_file_full` plus subsequent
  amendments via `str_replace` that must be applied in order
- `str_replace_sequence` — built up incrementally via multiple
  `str_replace` calls; reconstruction order matters
- `bash_heredoc` — written via `bash_tool` heredoc; recoverable but
  requires extracting the heredoc body from the `command` string
- `RECONSTRUCTED_FROM_TRANSCRIPT_NOT_BYTE_IDENTICAL` — source is this
  chat session's history, not a transcript; final SHA will differ from
  the original lost commit
- `missing_uncertain` — not visible in any transcript with my current
  scan; needs deeper search in Step 4

---

## 8. What this manifest does NOT yet decide

These open items will be resolved in Step 4, BEFORE writing any code for
the affected file:

1. **Deep re-scan for `drift.py`, `permutation_importance.py`,
   `threshold_metrics.py`, `breakdowns.py`** in transcript #9. Their
   17 mentions each strongly suggest they exist as create_file blocks
   that my first-pass regex missed.
2. **Heredoc extraction** for transcripts #3 and #4 (the M18.A.2 and
   M18.A.3 sessions). These have zero `create_file` calls but the source
   for `bot/ml/__init__.py`, `bot/ml/schemas.py`, `bot/ml/cli.py`,
   `bot/ml/errors.py` should be in their `bash_tool` heredoc blocks.
3. **Conversation-history extraction protocol** for files marked
   "current chat session only" — the source content is in this chat's
   prior assistant turns and will be lifted exactly from there, then
   tagged NOT_BYTE_IDENTICAL.

---

## 9. Push plan for this manifest

1. Empty branch already created locally: `m18-recovery-from-transcripts`
   at `a8d8ca44`.
2. Commit this manifest locally.
3. Need PAT to push both the empty branch (`git push -u origin
   m18-recovery-from-transcripts`) AND this manifest commit.
4. After push, await operator's go-ahead before starting Step 4.

No code will be reconstructed until this manifest is committed and pushed.
