"""bot.ml.registry.registry — file-based model registry.

Public surface:

  Registry(root='data/ml')
    .register_candidate(train_outputs, evaluation_report,
                          assembler_result)
        → RegistryEntry
        Writes artifacts + entry. Status inferred from gates. Sets
        approved_for_live=False (always, in M18.A.8).
        NEVER promotes to 'current'.

    .promote_to_current(model_id, *, force=False,
                          override_gates=(), reason=None, actor=None)
        → RegistryEntry (the promoted entry)
        STRICT enforcement (Q17):
          * Integrity reasons REJECT regardless of force.
          * Judgment reasons covered by `override_gates` allow
            promotion with status='forced_promoted' and audit
            recorded.
          * Without force, any blocking reason → reject.
        Demotes the previous current (if any) to 'demoted'.
        Appends a transition record to current_history.jsonl.

    .demote_current(scope_key, *, reason=None, actor=None)
        → RegistryEntry (the demoted entry)

    .get_current(scope_key)
        → Optional[RegistryEntry]

    .get_entry(model_id)
        → RegistryEntry

    .list_entries()
        → List[RegistryEntry]

    .current_history()
        → List[Dict[str, Any]]  (raw JSONL records)

Key invariants enforced here:
  * register_candidate NEVER auto-promotes
  * fixture_only entries CANNOT be promoted (integrity)
  * coverage_degraded entries CANNOT be promoted (integrity)
  * adversarial_validation_failed CANNOT be promoted (integrity)
  * drift_check_failed CANNOT be promoted (integrity)
  * --force can override sample_count/baseline_beat ONLY
  * approved_for_live ALWAYS False in M18.A.8
  * all writes are file-based; signals.db is never touched
"""
from __future__ import annotations

import datetime as _dt
import platform
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np
import pandas as pd

from bot.ml.errors import (
    M18ConfigError, PromotionBlockedError, ForceOverrideRequired,
)
from bot.ml.schemas import ALLOWED_REGISTRY_STATUSES
from bot.ml.registry import storage as _store
from bot.ml.registry.entry import (
    RegistryEntry, REGISTRY_ENTRY_SCHEMA_VERSION,
    ALWAYS_FALSE_APPROVED_FOR_LIVE,
    compute_model_id, infer_initial_status,
)
from bot.ml.registry.gates import (
    classify_reason, is_integrity_gate, is_judgment_gate,
    matches_override_gate, split_reasons,
    JUDGMENT_GATE_NAMES,
)


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds")


# ─────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────

class Registry:
    """File-based model registry rooted at `data/ml/`."""

    def __init__(self, root: str = _store.DATA_ML_ROOT_DEFAULT):
        self.root = Path(root)

    # ── Registration ──────────────────────────────────────────────────

    def register_candidate(
        self,
        train_outputs,
        evaluation_report,
        assembler_result,
        *,
        feature_columns: Optional[List[str]] = None,
    ) -> RegistryEntry:
        """Register a freshly-trained model as a candidate.

        Writes all artifacts under data/ml/artifacts/{model_id}/ and
        the RegistryEntry under data/ml/registry/{model_id}.json.
        NEVER sets status to 'current'.

        Re-registering the same (deterministic) model_id is allowed:
        it overwrites the previous record (status and audit history
        are recomputed from current inputs; current_history.jsonl
        is preserved untouched).
        """
        # Provenance sanity — assembler_result must match train_outputs
        if (train_outputs.dataset_hash_sha256
                != assembler_result.manifest.dataset_hash_sha256):
            raise M18ConfigError(
                f"dataset_hash mismatch: train_outputs has "
                f"{train_outputs.dataset_hash_sha256[:8]}…, "
                f"assembler_result has "
                f"{assembler_result.manifest.dataset_hash_sha256[:8]}…")

        model_id = compute_model_id(train_outputs)
        status   = infer_initial_status(train_outputs, evaluation_report)
        if status not in ALLOWED_REGISTRY_STATUSES:
            raise M18ConfigError(
                f"infer_initial_status returned {status!r} which is "
                f"not in ALLOWED_REGISTRY_STATUSES — this is a bug "
                f"in entry.py")

        # ── Persist artifacts ─────────────────────────────────────
        art_dir = _store.artifact_dir(self.root, model_id)

        # train_outputs.json
        _store.atomic_write_json(
            _store.artifact_path(self.root, model_id,
                                   _store.ARTIFACT_TRAIN_OUTPUTS),
            train_outputs.to_dict())

        # evaluation_report.json
        _store.atomic_write_json(
            _store.artifact_path(self.root, model_id,
                                   _store.ARTIFACT_EVAL_REPORT),
            evaluation_report.to_dict())

        # Training feature matrix + target for deterministic refit
        # at predict time. Resolve feature_columns if not supplied.
        if feature_columns is None:
            from bot.ml.models.base import select_feature_columns
            feature_columns = select_feature_columns(
                list(assembler_result.dataset.columns))

        dataset = assembler_result.dataset
        split   = assembler_result.split
        if split is None:
            raise M18ConfigError(
                "assembler_result.split is None; cannot persist "
                "training X/y for predict-time refit")

        train_idx = split.train_anchor_indices
        X_train_df = (dataset.iloc[train_idx][feature_columns]
                        .reset_index(drop=True))
        y_train_df = pd.DataFrame({
            train_outputs.target_label_id:
                dataset.iloc[train_idx][train_outputs.target_label_id]
                       .to_numpy(),
        }).reset_index(drop=True)

        _store.atomic_write_parquet(
            _store.artifact_path(self.root, model_id,
                                   _store.ARTIFACT_X_TRAIN),
            X_train_df)
        _store.atomic_write_parquet(
            _store.artifact_path(self.root, model_id,
                                   _store.ARTIFACT_Y_TRAIN),
            y_train_df)

        # training_feature_summary.json: per-feature stats used at
        # predict time to compute extrapolation flags.
        # Q20 LOCK: the EXTRAPOLATION ENVELOPE is [q01, q99] (1st and
        # 99th percentiles). min/max are also recorded for context
        # / debugging but are NOT the envelope.
        summary: Dict[str, Dict[str, float]] = {}
        for c in feature_columns:
            vals = X_train_df[c].to_numpy(dtype=np.float64)
            finite = vals[np.isfinite(vals)]
            if len(finite) == 0:
                summary[c] = {
                    "min":      float("nan"), "max": float("nan"),
                    "q01":      float("nan"), "q99": float("nan"),
                    "mean":     float("nan"), "std": float("nan"),
                    "n_finite": 0,
                }
            else:
                q01, q99 = np.quantile(finite, [0.01, 0.99])
                summary[c] = {
                    "min":      float(finite.min()),
                    "max":      float(finite.max()),
                    "q01":      float(q01),     # ← Q20 envelope lower
                    "q99":      float(q99),     # ← Q20 envelope upper
                    "mean":     float(finite.mean()),
                    "std":      float(finite.std(ddof=0)),
                    "n_finite": int(len(finite)),
                }
        _store.atomic_write_json(
            _store.artifact_path(self.root, model_id,
                                   _store.ARTIFACT_FEATURE_SUMMARY),
            summary)

        # training_metadata.json
        meta = {
            "feature_columns":    list(feature_columns),
            "target_label_id":    train_outputs.target_label_id,
            "target_label_class": train_outputs.target_label_class,
            "model_type":         train_outputs.model_type,
            "train_mode":         train_outputs.train_mode,
            "n_train":            int(train_outputs.n_train),
            "n_features":         int(train_outputs.n_features),
            "seed":               int(train_outputs.seed),
            "library_versions":   dict(train_outputs.library_versions),
        }
        _store.atomic_write_json(
            _store.artifact_path(self.root, model_id,
                                   _store.ARTIFACT_TRAINING_META),
            meta)

        # ── Build the RegistryEntry ────────────────────────────────
        now = _utc_now_iso()
        rel = lambda fn: str(Path(_store.ARTIFACTS_SUBDIR) / model_id / fn)
        entry = RegistryEntry(
            schema_version=REGISTRY_ENTRY_SCHEMA_VERSION,
            model_id=model_id,
            model_type=train_outputs.model_type,
            train_mode=train_outputs.train_mode,
            target_label_id=train_outputs.target_label_id,
            target_label_class=train_outputs.target_label_class,
            dataset_id=train_outputs.dataset_id,
            dataset_hash_sha256=train_outputs.dataset_hash_sha256,
            dataset_anchor_set=train_outputs.dataset_anchor_set,
            status=status,
            approved_for_live=ALWAYS_FALSE_APPROVED_FOR_LIVE,
            fixture_only=bool(train_outputs.fixture_only),
            promotion_eligible=bool(train_outputs.promotion_eligible),
            promotion_blocked_reasons=list(
                train_outputs.promotion_blocked_reasons),
            force_override_used=False,
            force_override_gates=[],
            force_override_reasons=[],
            force_override_actor=None,
            train_outputs_path=rel(_store.ARTIFACT_TRAIN_OUTPUTS),
            evaluation_report_path=rel(_store.ARTIFACT_EVAL_REPORT),
            training_feature_summary_path=rel(_store.ARTIFACT_FEATURE_SUMMARY),
            training_X_path=rel(_store.ARTIFACT_X_TRAIN),
            training_y_path=rel(_store.ARTIFACT_Y_TRAIN),
            training_metadata_path=rel(_store.ARTIFACT_TRAINING_META),
            created_at_utc=now,
            last_updated_utc=now,
            seed=int(train_outputs.seed),
            library_versions=dict(train_outputs.library_versions),
        )

        # Persist the entry
        _store.atomic_write_json(
            _store.entry_path(self.root, model_id), entry.to_dict())

        return entry

    # ── Read ──────────────────────────────────────────────────────────

    def get_entry(self, model_id: str) -> RegistryEntry:
        p = _store.entry_path(self.root, model_id)
        if not p.exists():
            raise M18ConfigError(
                f"no registry entry for model_id={model_id!r} "
                f"(expected at {p})")
        return RegistryEntry.from_dict(_store.read_json(p))

    def list_entries(self) -> List[RegistryEntry]:
        return [RegistryEntry.from_dict(_store.read_json(p))
                 for p in _store.iter_entry_paths(self.root)]

    def get_current(self, scope_key: str) -> Optional[RegistryEntry]:
        p = _store.current_pointer_path(self.root, scope_key)
        if not p.exists():
            return None
        ptr = _store.read_json(p)
        return self.get_entry(ptr["model_id"])

    def current_history(self) -> List[Dict[str, Any]]:
        return _store.read_current_history(self.root)

    # ── Promotion ────────────────────────────────────────────────────

    def promote_to_current(
        self,
        model_id: str,
        *,
        force: bool                  = False,
        override_gates: Tuple[str, ...] = (),
        reason: Optional[str]        = None,
        actor: Optional[str]         = None,
    ) -> RegistryEntry:
        """Promote a candidate to 'current'. STRICT Q17 enforcement.

        Without `force`:
          Any non-empty `promotion_blocked_reasons` → PromotionBlockedError.

        With `force=True`:
          `override_gates` must be a non-empty subset of the locked
          judgment gate names (baseline_beat, sample_count).
          `reason` must be a non-empty string (recorded in audit).
          Each blocking reason in the entry must EITHER be in
          INTEGRITY_GATE_REASONS (→ reject) OR be covered by one of
          the named override_gates (→ allowed).
          On success: status='forced_promoted', audit recorded.

        Without --force, success → status='current'.

        Demotes the previous current (if any) to 'demoted' and
        appends a transition record to current_history.jsonl.
        """
        entry = self.get_entry(model_id)

        # ── Static guard 1: fixture_only is NEVER overridable ──────
        if entry.fixture_only or entry.status == "fixture_only":
            raise PromotionBlockedError(
                "fixture_only", "integrity",
                f"model_id={model_id!r} is fixture_only — fixture "
                f"models can NEVER become current (Q16 lock); "
                f"--force does not apply")

        # ── Static guard 2: drift_check / coverage / AV failures ─
        if entry.status in {"coverage_degraded",
                             "failed_adversarial_validation",
                             "failed_drift_check"}:
            raise PromotionBlockedError(
                entry.status, "integrity",
                f"model_id={model_id!r} has integrity status "
                f"{entry.status!r}; --force cannot promote integrity-"
                f"gated entries (Q17)")

        # ── Reason-level integrity check (covers anything that
        #    composed reasons into promotion_blocked_reasons) ──────
        integrity, judgment, unknown = split_reasons(
            entry.promotion_blocked_reasons)

        if integrity:
            raise PromotionBlockedError(
                integrity[0], "integrity",
                f"model_id={model_id!r} has INTEGRITY reasons that "
                f"cannot be overridden: {integrity}. Q17 forbids "
                f"--force override of integrity gates.")

        # 'unknown' reasons (not classifiable) → fail-closed
        if unknown:
            raise PromotionBlockedError(
                unknown[0], "integrity",
                f"model_id={model_id!r} has unclassifiable reasons: "
                f"{unknown}. Refusing to promote; fail-closed.")

        # ── Promotion path: no blocking reasons → straight promote
        if not judgment:
            # Sanity: also enforce promotion_eligible to be True
            if not entry.promotion_eligible:
                raise PromotionBlockedError(
                    "promotion_eligible_false", "integrity",
                    f"model_id={model_id!r} has no blocking reasons "
                    f"but promotion_eligible=False — refusing "
                    f"promotion (probable inconsistency between "
                    f"reasons list and the boolean gate)")
            return self._do_promote(
                entry, status="current",
                force_used=False, override_gates=(),
                reason=None, actor=actor)

        # ── Judgment reasons remain — require --force + overrides ─
        if not force:
            raise PromotionBlockedError(
                judgment[0], "judgment",
                f"model_id={model_id!r} has judgment-gate reasons "
                f"{judgment}. Use --force --override-gate ... "
                f"--reason ... to override. Allowed override gates: "
                f"{sorted(JUDGMENT_GATE_NAMES)}")
        if not override_gates:
            raise ForceOverrideRequired(
                f"--force was set but no --override-gate was "
                f"specified. Allowed: {sorted(JUDGMENT_GATE_NAMES)}. "
                f"Q17 requires explicit naming of the gates being "
                f"overridden.")
        if reason is None or not str(reason).strip():
            raise ForceOverrideRequired(
                "--force was set but --reason was empty. Q17 "
                "requires a non-empty reason recorded in audit.")
        # Reject any override_gate that names an integrity gate or
        # something not in JUDGMENT_GATE_NAMES
        for og in override_gates:
            if og not in JUDGMENT_GATE_NAMES:
                raise ForceOverrideRequired(
                    f"--override-gate={og!r} is not a recognised "
                    f"judgment gate. Allowed: "
                    f"{sorted(JUDGMENT_GATE_NAMES)}.")
        # Every judgment reason must be covered by at least one
        # override_gate. If any uncovered judgment reason remains,
        # reject — the operator hasn't named the right gate.
        uncovered = [
            r for r in judgment
            if not any(matches_override_gate(r, og)
                         for og in override_gates)
        ]
        if uncovered:
            raise ForceOverrideRequired(
                f"--override-gate(s) {list(override_gates)} do not "
                f"cover all judgment reasons: uncovered={uncovered}. "
                f"Name the matching gate explicitly.")

        return self._do_promote(
            entry, status="forced_promoted",
            force_used=True,
            override_gates=tuple(override_gates),
            reason=str(reason), actor=actor)

    def _do_promote(
        self,
        entry: RegistryEntry,
        *,
        status: str,
        force_used: bool,
        override_gates: Tuple[str, ...],
        reason: Optional[str],
        actor: Optional[str],
    ) -> RegistryEntry:
        """Internal: execute a vetted promotion. Updates the entry,
        demotes the previous current (if any), and writes the
        transition to current_history.jsonl."""
        scope_key = _store.make_scope_key(
            dataset_anchor_set=entry.dataset_anchor_set,
            train_mode=entry.train_mode,
            target_label_id=entry.target_label_id,
            model_type=entry.model_type)
        prev_current = self.get_current(scope_key)
        now = _utc_now_iso()

        # Demote previous current (if any)
        if prev_current is not None and prev_current.model_id != entry.model_id:
            demoted = replace(
                prev_current, status="demoted",
                last_updated_utc=now)
            _store.atomic_write_json(
                _store.entry_path(self.root, demoted.model_id),
                demoted.to_dict())

        # Update the promoted entry
        promoted = replace(
            entry,
            status=status,
            approved_for_live=ALWAYS_FALSE_APPROVED_FOR_LIVE,  # invariant
            force_override_used=bool(force_used),
            force_override_gates=list(override_gates),
            force_override_reasons=([] if reason is None else [reason]),
            force_override_actor=actor,
            last_updated_utc=now)
        _store.atomic_write_json(
            _store.entry_path(self.root, promoted.model_id),
            promoted.to_dict())

        # Update the current pointer
        _store.atomic_write_json(
            _store.current_pointer_path(self.root, scope_key),
            {"model_id": promoted.model_id, "promoted_at_utc": now,
             "scope_key": scope_key, "status": status})

        # Append to current_history.jsonl
        _store.atomic_append_jsonl(
            _store.current_history_path(self.root),
            {
                "event":               "promote",
                "ts_utc":              now,
                "scope_key":           scope_key,
                "model_id":            promoted.model_id,
                "previous_model_id":   (prev_current.model_id
                                          if prev_current else None),
                "new_status":          status,
                "force_used":          bool(force_used),
                "override_gates":      list(override_gates),
                "reason":              reason,
                "actor":               actor,
                "approved_for_live":   ALWAYS_FALSE_APPROVED_FOR_LIVE,
            })

        return promoted

    def demote_current(
        self,
        scope_key: str,
        *,
        reason: Optional[str] = None,
        actor: Optional[str]  = None,
    ) -> RegistryEntry:
        """Demote whatever is current for `scope_key` to 'demoted'.
        No-op replacement of the pointer (pointer file is removed).
        Appends a 'demote' record to current_history.jsonl."""
        cur = self.get_current(scope_key)
        if cur is None:
            raise M18ConfigError(
                f"no current model for scope_key={scope_key!r}; "
                f"nothing to demote")
        now = _utc_now_iso()
        demoted = replace(
            cur, status="demoted", last_updated_utc=now)
        _store.atomic_write_json(
            _store.entry_path(self.root, demoted.model_id),
            demoted.to_dict())
        # Remove pointer atomically via unlink
        ptr_path = _store.current_pointer_path(self.root, scope_key)
        if ptr_path.exists():
            ptr_path.unlink()
        _store.atomic_append_jsonl(
            _store.current_history_path(self.root),
            {
                "event":          "demote",
                "ts_utc":         now,
                "scope_key":      scope_key,
                "model_id":       demoted.model_id,
                "reason":         reason,
                "actor":          actor,
            })
        return demoted
