"""bot.ml.dataset.manifest — dataset manifest schema + deterministic hash.

The manifest is the auditable record of one dataset build. Every
field is part of the dataset's identity for downstream comparison
(M18.A.6 training reproducibility, M18.A.8 registry promotion).

Identity:
    dataset_id              human-readable string
    dataset_hash_sha256     deterministic from contents

Promotion gate fields:
    is_full_coverage_dataset    Q19 — False blocks scanner_replica
                                  meta-label promotion
    degraded_reason             populated iff is_full=False
    fixture_mode_invocation     Q16 — tagging
    anchor_set                  "model_a_scanner_replica" or
                                  "model_b_1h_union_candidates"

Walk-forward subblock:
    walk_forward                dict with split sizes, embargo,
                                  purge counts

Adversarial validation subblock (populated AFTER AV runs):
    adversarial_validation      dict with auc, threshold, passed, etc.

The deterministic hash MUST be reproducible: same inputs → same hash.
We hash a canonical-JSON of the IDENTITY-relevant fields only —
NOT the timestamps, NOT the AV results (those come after the dataset
is built; they're a property of the trained AV model on this
dataset, not of the dataset itself).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from bot.ml.hashing import canonical_json, sha256_hex


MANIFEST_SCHEMA_VERSION = 1


@dataclass
class DatasetManifest:
    """Record of one dataset build (Q19-aligned schema).

    Q19-mandated fields (all explicit):

      requested_timeframes   What the AssemblerConfig asked for.
      available_timeframes   TFs actually supplied with >= 1 bar.
      missing_timeframes     Required TFs not supplied (or empty).
      requested_anchor_tf    config.anchor_tf
      actual_anchor_tf       The TF actually used. EQUAL to
                               requested_anchor_tf in M18.A.5 (TF
                               substitution is a future M18.A.6+
                               feature reserved by this field).
      coverage_degraded      Boolean Q19 flag.
      degradation_warning    Human-readable reason; None when full.

    Promotion gate (derived in the assembler):

      fixture_only           True iff fixture_mode OR skip_adversarial.
                               When True, this dataset can NEVER be
                               registered as a full scanner_replica
                               meta-label model (M18.A.8 enforces).
      promotion_eligible     True iff ALL OF:
                                 not coverage_degraded
                                 not fixture_only
                                 adversarial_validation is not None
                                 adversarial_validation.passed
      promotion_blocked_reasons
                             Explicit list of WHY promotion_eligible
                               is False (empty when eligible).

    Adversarial validation sub-block (populated AFTER AV runs):
      adversarial_validation   Dict serialised from
                                 AdversarialValidationResult, or None
                                 if AV did not run.
    """

    # Identity
    schema_version: int
    dataset_id: str
    dataset_hash_sha256: str
    created_at_utc: str

    # Source
    symbol: str
    requested_timeframes: List[str]
    available_timeframes: List[str]
    missing_timeframes:   List[str]
    requested_anchor_tf:  str
    actual_anchor_tf:     str
    bar_window_start_utc: str
    bar_window_end_utc:   str
    bars_per_tf: Dict[str, int]

    # Q19 coverage flags
    coverage_degraded:    bool
    degradation_warning:  Optional[str]

    # Anchors
    anchor_set: str   # "model_a_scanner_replica" | "model_b_1h_union_candidates"
    anchor_count_raw: int        # before any exclusion
    anchor_count_pending_excluded: int
    anchor_count_total: int      # raw - pending
    anchor_count_train: int
    anchor_count_val:   int
    anchor_count_test:  int
    anchor_count_purged: int
    anchor_count_embargoed: int

    # Schema fingerprints
    feature_specs_hash: str
    label_specs_hash:   str
    feature_count: int
    label_count:   int

    # Walk-forward sub-block
    walk_forward: Dict[str, Any]

    # Tags (Q19/Q16)
    fixture_mode_invocation: bool
    fixture_only:            bool

    # Promotion gate (Q19 + Q17)
    promotion_eligible:           bool
    promotion_blocked_reasons:    List[str]

    # Adversarial validation (set AFTER AV runs; None until then)
    adversarial_validation: Optional[Dict[str, Any]] = None
    # M18.B.6 — explicit AV status + stable reason string, set for EVERY
    # outcome (passed / failed / skipped_not_enough_data /
    # unavailable_error / disabled_fixture_mode / skipped_no_split) so
    # the reason never collapses to an ambiguous bare None. Backward-
    # compatible defaults; from_dict filters unknown keys for old
    # manifests. "" default distinguishes a pre-B6 manifest.
    adversarial_validation_status: str = ""
    adversarial_validation_reason: str = ""

    # M16 input-bars digest (M18.B.2 / SR-8). The compact per-timeframe
    # fingerprint (n_bars / first_ts / last_ts / close sums) that already
    # feeds dataset_hash_sha256 via compute_dataset_hash — now PERSISTED
    # so repro_hash_v2 can fingerprint the source data without storing
    # raw OHLCV. Added LAST + default_factory so older manifests that
    # predate this field still round-trip (from_dict tolerates absence).
    m16_bars_digest: Dict[str, Any] = field(default_factory=dict)

    # M18.B.5 — explicit missingness policy provenance. Backward-
    # compatible (default_factory / default ""), filtered by from_dict
    # so older manifests round-trip. The policy hash also feeds the
    # dataset hash + repro_hash_v2 so a policy change is detectable.
    missingness_policy_hash: str = ""
    missingness_report: Dict[str, Any] = field(default_factory=dict)

    # F2 / ISSUE-016 + ISSUE-017 — price-adjustment provenance + PIT-leakage
    # gate. Backward-compatible defaults (raw mode) so older manifests
    # round-trip via from_dict. When adjusted prices are used, the adjusted
    # O/H/L are SYNTHETIC (uniform per-bar adjustment_ratio; only adj_close is
    # real — see bot.historical.store) and the values embed corporate actions
    # known after each historical bar, i.e. a point-in-time look-ahead risk.
    # ML readiness/promotion blocks adjusted mode unless
    # allow_adjusted_prices_for_ml is explicitly True.
    price_adjustment_mode:   str  = "raw"   # "raw" | "adjusted"
    adjusted_ohlc_synthetic: bool = False
    adjusted_ohlc_method:    str  = "none"  # "uniform_ratio" when adjusted
    adjustment_ratio_source: str  = "none"  # "yfinance_adj_close" when adjusted
    adjusted_close_real:     bool = False
    allow_adjusted_prices_for_ml: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DatasetManifest":
        if int(d.get("schema_version", 0)) != MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"DatasetManifest schema_version must be "
                f"{MANIFEST_SCHEMA_VERSION}, got "
                f"{d.get('schema_version')!r}")
        # Backward compatibility: m16_bars_digest was added in M18.B.2.
        # Older manifests won't have it — default to {} rather than
        # failing the round-trip. We only pass keys the dataclass knows.
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


def _bars_digest(per_tf_bars: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    """Compact digest of the input bars — enough to detect the SAME
    input deterministically, but not the raw OHLCV (which would
    be huge and bind the manifest to specific data files)."""
    digest = {}
    for tf in sorted(per_tf_bars.keys()):
        df = per_tf_bars[tf]
        if df is None or len(df) == 0:
            digest[tf] = {"n_bars": 0,
                          "first_ts": None, "last_ts": None,
                          "close_sum_str": "0",
                          "close_sum_sq_str": "0"}
            continue
        n = int(len(df))
        first_ts = str(pd.to_datetime(
            df["ts_utc"].iloc[0], utc=True))
        last_ts  = str(pd.to_datetime(
            df["ts_utc"].iloc[-1], utc=True))
        # Sum-of-closes is a coarse-but-deterministic fingerprint
        # that catches "same bars" without exposing the raw data.
        # We round to avoid float-formatting nondeterminism.
        close = df["close"].astype(float).to_numpy()
        digest[tf] = {
            "n_bars": n,
            "first_ts": first_ts,
            "last_ts":  last_ts,
            "close_sum_str":   f"{close.sum():.12f}",
            "close_sum_sq_str": f"{(close * close).sum():.12f}",
        }
    return digest


def compute_dataset_hash(
    *,
    symbol: str,
    timeframes: List[str],
    anchor_tf: str,
    anchor_set: str,
    bars_digest: Dict[str, Any],
    feature_specs_hash: str,
    label_specs_hash: str,
    train_frac: float,
    val_frac: float,
    test_frac: float,
    embargo_bars: int,
    fixture_mode_invocation: bool,
    missingness_policy_hash: str = "",
    price_adjustment_mode: str = "raw",
) -> str:
    """Deterministic SHA-256 over the dataset's identity-relevant
    fields. Uses canonical_json (sorted keys, fixed separators) to
    avoid JSON-encoding nondeterminism."""
    payload = {
        "symbol":                 symbol,
        "timeframes":             sorted(timeframes),
        "anchor_tf":              anchor_tf,
        "anchor_set":             anchor_set,
        "bars_digest":            bars_digest,
        "feature_specs_hash":     feature_specs_hash,
        "label_specs_hash":       label_specs_hash,
        "train_frac":             round(float(train_frac), 6),
        "val_frac":               round(float(val_frac), 6),
        "test_frac":              round(float(test_frac), 6),
        "embargo_bars":           int(embargo_bars),
        "fixture_mode_invocation": bool(fixture_mode_invocation),
        "missingness_policy_hash": str(missingness_policy_hash),
        # F2 / ISSUE-017: price-adjustment mode is identity-relevant — a
        # dataset built on adjusted vs raw prices is a different dataset.
        "price_adjustment_mode":  str(price_adjustment_mode),
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
    }
    return sha256_hex(canonical_json(payload))


def compute_feature_specs_hash(feature_groups: Dict[str, Any]) -> str:
    """Hash over every FeatureSpec across every group, sorted by
    feature_id, including dtype + lookback + leak_class + version.
    Drift in any of these changes the hash."""
    items = []
    for grp_name in sorted(feature_groups.keys()):
        for s in feature_groups[grp_name].SPECS:
            items.append({
                "feature_id":            s.feature_id,
                "feature_group":         s.feature_group,
                "feature_group_version": int(s.feature_group_version),
                "dtype":                 s.dtype,
                "leak_class":            s.leak_class,
                "lookback_bars":         int(s.lookback_bars),
                "lookback_unit":         s.lookback_unit,
            })
    items.sort(key=lambda r: r["feature_id"])
    return sha256_hex(canonical_json(items))


def compute_label_specs_hash(label_groups: Dict[str, Any]) -> str:
    """Hash over every LabelSpec across every group."""
    items = []
    for grp_name in sorted(label_groups.keys()):
        for s in label_groups[grp_name].SPECS:
            items.append({
                "label_id":             s.label_id,
                "label_schema_version": int(s.label_schema_version),
                "label_class":          s.label_class,
                "horizon_bars":         int(s.horizon_bars),
                "horizon_unit":         s.horizon_unit,
                "leak_class":           s.leak_class,
                "cost_model_applied":   bool(s.cost_model_applied),
                "tp_mult":              s.tp_mult,
                "sl_mult":              s.sl_mult,
                "tie_breaker":          s.tie_breaker,
            })
    items.sort(key=lambda r: r["label_id"])
    return sha256_hex(canonical_json(items))


def current_utc_iso() -> str:
    """Return the current UTC time as an ISO-8601 string. Wrapped in
    a function so tests can monkeypatch if needed."""
    return datetime.now(timezone.utc).isoformat()
