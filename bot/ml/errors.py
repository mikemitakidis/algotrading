"""bot.ml.errors — M18 exception hierarchy.

Every M18 module raises subclasses of M18Error. Callers can catch
M18Error to handle ANY M18 failure, or catch a specific category:

  M18ConfigError    — invalid configuration values, bad CLI args,
                      malformed YAML/JSON.
  M18SchemaError    — feature/label/manifest schema violations.
  M18DataError      — bar/flywheel data unavailable, coverage gaps,
                      point-in-time violations.
  M18LeakageError   — adversarial validation failure, drift checks.
  M18RegistryError  — model registry promotion/demotion failures.

Adding a new error: subclass M18Error or one of the category bases.
Tests in test_m18_ml.G10_Hygiene enforce that every module raises
an M18Error subclass for known failure modes (never raw Exception).
"""
from __future__ import annotations


class M18Error(Exception):
    """Base for every M18 failure mode."""


# ─── Configuration / schema ──────────────────────────────────────────

class M18ConfigError(M18Error):
    """Invalid or missing M18 configuration (CLI args, YAML, JSON
    bodies, environment values)."""


class M18SchemaError(M18Error):
    """Base for FeatureSpec / LabelSpec / Manifest schema violations."""


class FeatureSchemaError(M18SchemaError):
    """A FeatureSpec is missing required fields or has invalid values."""


class LabelSchemaError(M18SchemaError):
    """A LabelSpec is missing required fields or has invalid values."""


class FeatureSchemaMismatchError(M18SchemaError):
    """A persisted dataset's feature_specs_hash does not match the
    current FeatureSpec set — the feature schema has drifted under
    a dataset that was already built. Force a rebuild."""


# ─── Data / coverage ─────────────────────────────────────────────────

class M18DataError(M18Error):
    """Base for missing/incomplete bar or flywheel data."""


class PointInTimeViolationError(M18DataError):
    """A feature touched bars at or after the anchor (`bar_index >=
    anchor_index`). Look-ahead leak."""


class InsufficientDataError(M18DataError):
    """Not enough bars after lookback/embargo to compute a feature or
    a label."""


class M16CoverageError(M18DataError):
    """M16 parquet bars don't cover the requested symbol/timeframe/
    window. The loader couldn't satisfy the request (distinct from
    InsufficientIntradayCoverageError). This error means: data is
    not on disk for the request, the assembler must not silently
    fabricate or skip."""


class InsufficientIntradayCoverageError(M18DataError):
    """M16 coverage exists but at a coarser timeframe than requested.
    The dataset assembler downgrades the anchor timeframe and emits
    coverage_degraded=True in the manifest."""


# ─── Leakage / drift ─────────────────────────────────────────────────

class M18LeakageError(M18Error):
    """Base for adversarial validation / drift failures."""


class AdversarialValidationFailedError(M18LeakageError):
    """Train vs test rows are too easily distinguishable. ROC-AUC of
    the AV classifier exceeded the configured threshold — the
    dataset has a leak path or a structural train/test difference
    that would let a model "cheat"."""


class DriftCheckFailedError(M18LeakageError):
    """Feature-level PSI between train and val/test exceeded the
    configured drift_warning_threshold."""


# ─── Registry ────────────────────────────────────────────────────────

class M18RegistryError(M18Error):
    """Base for model-registry failures."""


class PromotionBlockedError(M18RegistryError):
    """A model failed a promotion gate. The specific failed gate is
    recorded in the exception's args for the registry audit trail.

    Whether this is a judgment gate (overridable with --force +
    --override-gate + reason) or an integrity gate (never overridable)
    depends on which gate failed. The exception carries a
    `gate_category` argument: 'judgment' or 'integrity'."""

    def __init__(self, gate: str, gate_category: str, message: str):
        super().__init__(message)
        self.gate = gate
        self.gate_category = gate_category


class ForceOverrideRequired(M18RegistryError):
    """Raised when --force was used but --override-gate was missing or
    named a forbidden integrity gate (Q17)."""


__all__ = [
    "M18Error",
    "M18ConfigError",
    "M18SchemaError",
    "FeatureSchemaError",
    "LabelSchemaError",
    "FeatureSchemaMismatchError",
    "M18DataError",
    "PointInTimeViolationError",
    "InsufficientDataError",
    "M16CoverageError",
    "InsufficientIntradayCoverageError",
    "M18LeakageError",
    "AdversarialValidationFailedError",
    "DriftCheckFailedError",
    "M18RegistryError",
    "PromotionBlockedError",
    "ForceOverrideRequired",
]
