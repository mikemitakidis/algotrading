"""bot.ml.registry.gates — Q17 enforcement: integrity vs judgment gates.

Q17 locked semantic (M18.A.1):
  `--force` may override JUDGMENT gates only.
  `--force` MAY NEVER override INTEGRITY gates.

This module is the SINGLE SOURCE OF TRUTH for which gate names fall
into which class. Both the CLI argument parser and the registry's
promote() function consult these sets.

Integrity gates (NEVER overridable):
  Anything that, if overridden, would let a structurally broken model
  become 'current'. Includes dataset-level integrity (fixture_only,
  coverage_degraded, adversarial_validation_*), pipeline-level
  integrity (schema_mismatch, point_in_time_violation, leakage_
  detected, hash_mismatch), and drift_check failures.

Judgment gates (overridable with --force --override-gate --reason):
  Gates that reflect a HUMAN judgment about quality (e.g. didn't
  beat B0, sample count too low). An operator may decide a model is
  still worth promoting despite the failure, but must do so
  EXPLICITLY with --override-gate naming the specific judgment gate
  and a --reason recorded in current_history.

Reason-string conventions in this codebase:
  Dataset-level reasons may appear with or without 'dataset:' prefix
  depending on whether they came from the manifest or were composed
  by the trainer. Both forms are recognised.
  Thinness reasons have the form 'thinness:<check_name>' (e.g.
  'thinness:minority_class_count').
"""
from __future__ import annotations

from typing import FrozenSet, List, Tuple


# ─────────────────────────────────────────────────────────────────────
# Integrity gates — NEVER overridable
# ─────────────────────────────────────────────────────────────────────

# Bare and prefixed forms are both recognised because different code
# paths emit different conventions:
#   bot.ml.dataset.assembler emits bare reasons ("fixture_only", etc.)
#   bot.ml.models.trainer composes them with "dataset:" prefix
INTEGRITY_GATE_REASONS: FrozenSet[str] = frozenset({
    # Dataset-level
    "fixture_only",                       # Q16
    "dataset:fixture_only",
    "coverage_degraded",                  # Q19 / M16
    "dataset:coverage_degraded",
    "adversarial_validation_failed",
    "dataset:adversarial_validation_failed",
    "adversarial_validation_not_run",
    "dataset:adversarial_validation_not_run",
    # Pipeline integrity
    "schema_mismatch",
    "point_in_time_violation",
    "leakage_detected",
    "hash_mismatch",
    "feature_schema_mismatch",
    # Drift integrity (from EvaluationReport.drift)
    "drift_check_failed",
    "drift_warning",
})

# ─────────────────────────────────────────────────────────────────────
# Judgment gates — overridable with --force + --override-gate + --reason
# ─────────────────────────────────────────────────────────────────────

# Bare names only — these match the CLI's allowed --override-gate
# values verbatim per M18.A.1's locked surface.
JUDGMENT_GATE_NAMES: FrozenSet[str] = frozenset({
    "baseline_beat",
    "sample_count",
})

# Reason-string prefixes that map to judgment gates
JUDGMENT_GATE_REASON_PREFIXES: Tuple[str, ...] = (
    "thinness:",          # thinness:<check>  → judgment (sample_count)
    "baseline_beat:",     # baseline_beat:<spec> → judgment
)


def is_integrity_gate(reason: str) -> bool:
    """True iff `reason` names an integrity gate.

    Integrity gates are NEVER overridable. Catches both the
    bare and 'dataset:'-prefixed forms.
    """
    if reason in INTEGRITY_GATE_REASONS:
        return True
    # 'dataset:<anything else>' is treated as integrity by default —
    # the manifest only emits integrity-class reasons under the
    # 'dataset:' prefix.
    if reason.startswith("dataset:"):
        return True
    return False


def is_judgment_gate(reason: str) -> bool:
    """True iff `reason` names a judgment gate that --force can
    override (with --override-gate naming it explicitly).
    """
    if reason in JUDGMENT_GATE_NAMES:
        return True
    for prefix in JUDGMENT_GATE_REASON_PREFIXES:
        if reason.startswith(prefix):
            return True
    return False


def classify_reason(reason: str) -> str:
    """Classify a promotion-blocked reason as 'integrity', 'judgment',
    or 'unknown'. The 'unknown' bucket is treated as integrity by
    callers — fail-closed when uncertain."""
    if is_integrity_gate(reason):
        return "integrity"
    if is_judgment_gate(reason):
        return "judgment"
    return "unknown"


def matches_override_gate(reason: str, override_gate: str) -> bool:
    """True iff `override_gate` (a judgment gate NAME like
    'baseline_beat' or 'sample_count') covers `reason`.

    Mapping:
      override_gate='sample_count'  matches any 'thinness:*' reason
                                     and the bare 'sample_count'
      override_gate='baseline_beat' matches any 'baseline_beat:*'
                                     reason and bare 'baseline_beat'
    Integrity reasons are NEVER matched (returns False even if the
    operator names them — they cannot be overridden by definition).
    """
    if is_integrity_gate(reason):
        return False
    if override_gate not in JUDGMENT_GATE_NAMES:
        return False
    if override_gate == "sample_count":
        return reason == "sample_count" or reason.startswith("thinness:")
    if override_gate == "baseline_beat":
        return reason == "baseline_beat" or reason.startswith("baseline_beat:")
    return False


def split_reasons(
    reasons: List[str],
) -> Tuple[List[str], List[str], List[str]]:
    """Split `reasons` into (integrity_reasons, judgment_reasons,
    unknown_reasons) lists, preserving order within each bucket."""
    integrity:  List[str] = []
    judgment:   List[str] = []
    unknown:    List[str] = []
    for r in reasons:
        kind = classify_reason(r)
        if kind == "integrity":
            integrity.append(r)
        elif kind == "judgment":
            judgment.append(r)
        else:
            unknown.append(r)
    return integrity, judgment, unknown
