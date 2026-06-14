"""bot.ml.registry.entry — RegistryEntry dataclass + helpers.

A RegistryEntry is the on-disk record describing one trained model
in the M18 registry. It is created via `make_initial_entry()` at
registration time and updated only through controlled paths in
`Registry`. Promotions, demotions, and force-overrides are recorded
via append-only `current_history.jsonl` (see storage.py).

Status inference rules (initial registration):

  fixture_only                  → from dataset.fixture_only OR
                                    train_config.fixture_mode
  coverage_degraded             → from dataset.coverage_degraded
  failed_adversarial_validation → reasons contains
                                    'adversarial_validation_failed'
                                    or 'adversarial_validation_not_run'
  failed_drift_check            → evaluation_report.drift block has
                                    any drift_warning=True
  failed_sample_count           → train_outputs.thinness_status.passed
                                    is False
  candidate                     → default when no blocking reason

NOTE: 'failed_baseline_beat' is NEVER an INITIAL status — it's
determined at PROMOTION TIME by comparing against baselines in the
same cohort. The registration step does not have access to the
sibling baseline reports.

'current' is set only by Registry.promote_to_current().
'demoted' is set only by Registry.demote_current().
'forced_promoted' is set only when promote_to_current() was called
with force=True for a judgment-gate override.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional


REGISTRY_ENTRY_SCHEMA_VERSION = 1

# In M18.A.8 we NEVER set this to True. Live approval is a separate
# downstream gate that lives outside the M18 registry.
ALWAYS_FALSE_APPROVED_FOR_LIVE: bool = False


@dataclass
class RegistryEntry:
    """One trained model's registry record.

    Status drift is impossible because every status transition goes
    through Registry methods which validate and append to
    current_history.jsonl.
    """
    schema_version: int

    # Deterministic identity — derived from train_outputs + manifest
    model_id: str

    # Identity from TrainOutputs
    model_type: str
    train_mode: str
    target_label_id: str
    target_label_class: str
    dataset_id: str
    dataset_hash_sha256: str
    dataset_anchor_set: str

    # Current status — must be in ALLOWED_REGISTRY_STATUSES
    status: str

    # Live-approval flag — M18.A.8 ALWAYS False (live approval lives
    # outside the M18 registry).
    approved_for_live: bool

    # Gate state echoed verbatim from TrainOutputs / manifest
    fixture_only: bool
    promotion_eligible: bool
    promotion_blocked_reasons: List[str]

    # Force-override audit (Q17)
    force_override_used: bool
    force_override_gates: List[str]          # which gate names were overridden
    force_override_reasons: List[str]         # operator-supplied reasons
    force_override_actor: Optional[str]       # caller identity if available

    # Artifact paths (relative to data/ml/)
    train_outputs_path: str
    evaluation_report_path: str
    training_feature_summary_path: str
    training_X_path: str
    training_y_path: str
    training_metadata_path: str

    # Timestamps
    created_at_utc: str
    last_updated_utc: str

    # Free-form provenance — captured from train_outputs
    seed: int
    library_versions: Dict[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RegistryEntry":
        return cls(**d)


# ─────────────────────────────────────────────────────────────────────
# Deterministic model_id
# ─────────────────────────────────────────────────────────────────────

def compute_model_id(train_outputs) -> str:
    """Compute a deterministic 16-char hex model_id from the
    train_outputs' identifying fields.

    Same dataset + same model_type + same train_mode + same target
    + same hyperparameters + same seed → same model_id.

    Different hyperparameters or seed → different model_id. This is
    the registry's primary key.
    """
    # Canonical JSON of the identifying subset
    canonical = {
        "dataset_hash_sha256":  train_outputs.dataset_hash_sha256,
        "dataset_anchor_set":   train_outputs.dataset_anchor_set,
        "model_type":           train_outputs.model_type,
        "train_mode":           train_outputs.train_mode,
        "target_label_id":      train_outputs.target_label_id,
        "target_label_class":   train_outputs.target_label_class,
        "seed":                 int(train_outputs.seed),
        "train_config":         dict(train_outputs.train_config),
    }
    blob = json.dumps(canonical, sort_keys=True,
                        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────
# Initial status inference
# ─────────────────────────────────────────────────────────────────────

def infer_initial_status(
    train_outputs, evaluation_report,
) -> str:
    """Return the initial registry status for a freshly-trained
    model at registration time.

    Precedence (highest priority first):
      1. fixture_only          (Q16)
      2. coverage_degraded     (Q19)
      3. failed_adversarial_validation
      4. failed_drift_check    (any train→val or train→test drift_warning)
      5. failed_sample_count   (thinness failed)
      6. candidate             (default)

    'current', 'demoted', 'forced_promoted', 'failed_baseline_beat',
    'candidate_inspection_only' are NEVER returned here — they are
    set only by Registry methods at promote/demote/baseline-compare
    time.
    """
    reasons = list(train_outputs.promotion_blocked_reasons)

    def _has(r): return r in reasons

    # 1. fixture_only — Q16
    if (train_outputs.fixture_only
            or _has("fixture_only")
            or _has("dataset:fixture_only")):
        return "fixture_only"

    # 2. coverage_degraded — Q19
    if _has("coverage_degraded") or _has("dataset:coverage_degraded"):
        return "coverage_degraded"

    # 3. adversarial validation
    if (_has("adversarial_validation_failed")
            or _has("dataset:adversarial_validation_failed")):
        return "failed_adversarial_validation"

    # 4. drift check
    drift = getattr(evaluation_report, "drift", None) or {}
    for split_pair, drift_block in drift.items():
        if isinstance(drift_block, dict) and drift_block.get(
                "drift_warning"):
            return "failed_drift_check"

    # 5. sample count / thinness — includes the strict production
    #    thinness gates (M18.B.4). A production-blocked model is not a
    #    clean 'candidate'; map it to the existing safe non-candidate
    #    status so the registry status is not misleading. (Promotion is
    #    independently blocked by the integrity gate regardless.)
    if any(r.startswith("thinness:") for r in reasons):
        return "failed_sample_count"
    if any(r.startswith("production:") for r in reasons):
        return "failed_sample_count"

    # Default
    return "candidate"
