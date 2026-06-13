"""bot.ml.features.missingness — explicit NaN / missingness policy (M18.B.5).

The problem this solves
-----------------------
NaNs appear in M18 features from rolling-indicator warmup, multi-timeframe
joins, market-context gaps, and signal-history gaps. Before B.5 the trainer
silently did `X[np.isnan(X)] = 0.0` with no indicators, no report, and no
audit trail — expected missingness and bad missingness were indistinguishable.

This module makes missingness EXPLICIT, DETERMINISTIC, and AUDITABLE:

  * a per-feature-group policy (fill strategy + whether an indicator is
    required + the expected reason),
  * a deterministic NEUTRAL fill (0.0) applied at the model boundary,
  * per-column missingness INDICATOR arrays (no future/cross-row/cross-symbol
    information — purely "was this cell NaN?"),
  * a JSON-safe missingness REPORT computed at dataset-assembly time,
  * a POLICY HASH so repro_hash_v2 / dataset hash change if the policy changes,
  * a finite-matrix GUARD so NaN/inf can never reach .fit().

Leakage
-------
The fill value is a fixed constant (0.0) — it is NOT learned from data, so
there is no train/val/test leakage and no future-aware fill. Indicators are
computed per-cell from the cell's own NaN-ness only. (Per-group learned
imputation, if ever wanted, is deferred — it would have to be fit on train
only; B.5 deliberately uses constant neutral fill.)
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np

from bot.ml.hashing import canonical_json, sha256_hex


MISSINGNESS_POLICY_VERSION = "m18_missingness_v1"
MISSINGNESS_REPORT_SCHEMA_VERSION = 1

# Neutral fill value applied to any remaining feature NaN. Constant (not
# data-derived) so there is no leakage.
NEUTRAL_FILL_VALUE = 0.0

# Fill strategies (only neutral_fill_with_indicator is used in v1; the
# enum documents intent and leaves room for future strategies).
STRATEGY_NEUTRAL_FILL_WITH_INDICATOR = "neutral_fill_with_indicator"
STRATEGY_EXPECTED_NONE = "expected_no_missingness_detect_only"

# Stable failure-reason strings for the finite-matrix guard.
MISSINGNESS_REMAINING_NAN = "missingness_remaining_nan"
MISSINGNESS_REMAINING_INF = "missingness_remaining_inf"
MISSINGNESS_UNEXPECTED_OBJECT_DTYPE = "missingness_unexpected_object_dtype"
MISSINGNESS_POLICY_UNKNOWN_GROUP = "missingness_policy_unknown_group"

# The 10 locked feature groups and their explicit missingness policy.
#   strategy            : how remaining NaN is handled
#   indicator_required  : whether a per-column "__was_missing" indicator
#                          is emitted for columns in this group
#   expected_reason     : human-readable expected source of missingness
#   expect_no_missing   : True for groups that should be fully populated;
#                          missingness there is UNEXPECTED and must be
#                          surfaced in the report (never hidden)
FEATURE_GROUP_POLICY: Dict[str, Dict[str, Any]] = {
    "price_return": {
        "strategy": STRATEGY_NEUTRAL_FILL_WITH_INDICATOR,
        "indicator_required": True,
        "expected_reason": "first-row / lagged-return warmup",
        "expect_no_missing": False,
    },
    "trend": {
        "strategy": STRATEGY_NEUTRAL_FILL_WITH_INDICATOR,
        "indicator_required": True,
        "expected_reason": "rolling-window warmup / insufficient history",
        "expect_no_missing": False,
    },
    "momentum": {
        "strategy": STRATEGY_NEUTRAL_FILL_WITH_INDICATOR,
        "indicator_required": True,
        "expected_reason": "rolling-window warmup / insufficient history",
        "expect_no_missing": False,
    },
    "vol_regime": {
        "strategy": STRATEGY_NEUTRAL_FILL_WITH_INDICATOR,
        "indicator_required": True,
        "expected_reason": "rolling-window warmup / insufficient history",
        "expect_no_missing": False,
    },
    "volume_liquidity": {
        "strategy": STRATEGY_NEUTRAL_FILL_WITH_INDICATOR,
        "indicator_required": True,
        "expected_reason": "missing / zero volume edge cases",
        "expect_no_missing": False,
    },
    "mtf_confluence": {
        "strategy": STRATEGY_NEUTRAL_FILL_WITH_INDICATOR,
        "indicator_required": True,
        "expected_reason": "higher-timeframe value not available at early "
                            "anchors (no lookahead fill)",
        "expect_no_missing": False,
    },
    "scanner_replica": {
        "strategy": STRATEGY_NEUTRAL_FILL_WITH_INDICATOR,
        "indicator_required": True,
        "expected_reason": "normally deterministic; any NaN is unexpected "
                            "and is surfaced, never hidden",
        "expect_no_missing": True,
    },
    "market_context": {
        "strategy": STRATEGY_NEUTRAL_FILL_WITH_INDICATOR,
        "indicator_required": True,
        "expected_reason": "external/context feature not available for "
                            "some anchors",
        "expect_no_missing": False,
    },
    "symbol_meta": {
        "strategy": STRATEGY_NEUTRAL_FILL_WITH_INDICATOR,
        "indicator_required": True,
        "expected_reason": "normally present; missing symbol metadata is "
                            "unexpected and is surfaced",
        "expect_no_missing": True,
    },
    "signal_history": {
        "strategy": STRATEGY_NEUTRAL_FILL_WITH_INDICATOR,
        "indicator_required": True,
        "expected_reason": "no previous signal yet / empty history window "
                            "(intentional missingness)",
        "expect_no_missing": False,
    },
}

LOCKED_FEATURE_GROUPS = frozenset(FEATURE_GROUP_POLICY.keys())


def policy_canonical_object() -> Dict[str, Any]:
    """The canonical, hashable representation of the missingness policy."""
    return {
        "policy_version": MISSINGNESS_POLICY_VERSION,
        "neutral_fill_value": NEUTRAL_FILL_VALUE,
        "groups": {
            g: {
                "strategy": p["strategy"],
                "indicator_required": bool(p["indicator_required"]),
                "expect_no_missing": bool(p["expect_no_missing"]),
            }
            for g, p in sorted(FEATURE_GROUP_POLICY.items())
        },
    }


def missingness_policy_hash() -> str:
    """Deterministic SHA-256 of the canonical policy object. Changes iff
    the policy itself changes (version, fill value, or any group rule)."""
    return sha256_hex(canonical_json(policy_canonical_object()))


def _group_of(column: str) -> str:
    """Feature columns are namespaced '<group>.<feature>'."""
    return column.split(".", 1)[0]


def assert_known_groups(feature_columns: List[str]) -> None:
    """Raise if any feature column belongs to a group with no policy."""
    from bot.ml.errors import M18ConfigError
    unknown = sorted({_group_of(c) for c in feature_columns
                       if _group_of(c) not in FEATURE_GROUP_POLICY})
    if unknown:
        raise M18ConfigError(
            f"{MISSINGNESS_POLICY_UNKNOWN_GROUP}: feature columns belong "
            f"to group(s) with no missingness policy: {unknown}")


def apply_missingness_fill(
    X: np.ndarray,
    feature_columns: List[str],
) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """Apply the deterministic neutral-fill policy to a feature matrix.

    Returns
    -------
    (X_filled, indicators, indicator_names)
      X_filled         copy of X with NaN replaced by NEUTRAL_FILL_VALUE
      indicators       (n_rows, n_indicator_cols) 0/1 float array marking
                        which original cells were NaN, for columns whose
                        group requires an indicator
      indicator_names  ['<feature>__was_missing', ...] aligned to columns
                        of `indicators`

    Does NOT mutate X. inf is left in place here (the finite guard
    catches it) so that "remaining inf" is distinguishable from NaN.
    """
    assert_known_groups(feature_columns)
    X = np.asarray(X, dtype=np.float64)
    Xf = X.copy()
    nan_mask = np.isnan(Xf)

    indicator_cols: List[np.ndarray] = []
    indicator_names: List[str] = []
    for j, col in enumerate(feature_columns):
        grp = _group_of(col)
        policy = FEATURE_GROUP_POLICY[grp]
        if policy["indicator_required"]:
            indicator_cols.append(nan_mask[:, j].astype(np.float64))
            indicator_names.append(f"{col}__was_missing")

    Xf[nan_mask] = NEUTRAL_FILL_VALUE

    if indicator_cols:
        indicators = np.column_stack(indicator_cols)
    else:
        indicators = np.empty((Xf.shape[0], 0), dtype=np.float64)
    return Xf, indicators, indicator_names


def assert_finite_matrix(X: np.ndarray, *, name: str) -> None:
    """Guard before .fit(): raise M18DataError on NaN / inf / object."""
    from bot.ml.errors import M18DataError
    arr = np.asarray(X)
    if arr.dtype == object:
        raise M18DataError(
            f"{MISSINGNESS_UNEXPECTED_OBJECT_DTYPE}: {name} has object "
            f"dtype after the missingness policy — features must be "
            f"numeric")
    if np.isnan(arr).any():
        raise M18DataError(
            f"{MISSINGNESS_REMAINING_NAN}: {name} still contains "
            f"{int(np.isnan(arr).sum())} NaN value(s) after the "
            f"missingness policy")
    if np.isinf(arr).any():
        raise M18DataError(
            f"{MISSINGNESS_REMAINING_INF}: {name} contains "
            f"{int(np.isinf(arr).sum())} infinite value(s); the "
            f"missingness policy does not fill inf (it indicates a "
            f"feature-computation bug, not warmup missingness)")


def build_missingness_report(
    feature_values: "Any",
    feature_columns: List[str],
) -> Dict[str, Any]:
    """Compute a JSON-safe missingness report over an assembled feature
    table (a pandas DataFrame or 2-D array aligned to feature_columns).

    Reports, per group and overall: NaN counts before the policy, NaN
    counts after (0 by construction of neutral fill), inf counts,
    indicator columns that would be added, the per-group policy, and any
    unexpected-missingness flags for groups marked expect_no_missing.
    """
    assert_known_groups(feature_columns)
    # Accept a DataFrame or an ndarray.
    if hasattr(feature_values, "to_numpy"):
        arr = feature_values[feature_columns].to_numpy(
            dtype=np.float64, copy=True)
    else:
        arr = np.asarray(feature_values, dtype=np.float64)

    n_rows = int(arr.shape[0])
    nan_mask = np.isnan(arr)
    inf_mask = np.isinf(arr)

    groups: Dict[str, Any] = {}
    indicators_added_total: List[str] = []
    unexpected_flags: List[str] = []
    for grp in sorted(LOCKED_FEATURE_GROUPS):
        cols = [j for j, c in enumerate(feature_columns)
                if _group_of(c) == grp]
        if not cols:
            continue
        policy = FEATURE_GROUP_POLICY[grp]
        nan_before = int(nan_mask[:, cols].sum())
        inf_before = int(inf_mask[:, cols].sum())
        inds = ([f"{feature_columns[j]}__was_missing" for j in cols]
                if policy["indicator_required"] else [])
        indicators_added_total.extend(inds)
        if policy["expect_no_missing"] and nan_before > 0:
            unexpected_flags.append(
                f"unexpected_missingness_in_{grp}")
        groups[grp] = {
            "features": len(cols),
            "nan_before": nan_before,
            "nan_after": 0,            # neutral fill removes all NaN
            "inf_before": inf_before,
            "inf_after": inf_before,   # policy does NOT fill inf
            "indicators_added": inds,
            "rows_dropped": 0,
            "policy": policy["strategy"],
            "expected_reason": policy["expected_reason"],
            "expect_no_missing": bool(policy["expect_no_missing"]),
        }

    total_nan_before = int(nan_mask.sum())
    total_inf = int(inf_mask.sum())
    feature_count_before = len(feature_columns)
    return {
        "schema_version": MISSINGNESS_REPORT_SCHEMA_VERSION,
        "policy_version": MISSINGNESS_POLICY_VERSION,
        "policy_hash": missingness_policy_hash(),
        "rows_before_missingness_policy": n_rows,
        "rows_after_missingness_policy": n_rows,   # neutral fill drops 0
        "rows_dropped": 0,
        "feature_count_before_indicators": feature_count_before,
        "feature_count_after_indicators": (
            feature_count_before + len(indicators_added_total)),
        "nan_count_before": total_nan_before,
        "nan_count_after": 0,
        "inf_count_before": total_inf,
        "inf_count_after": total_inf,
        "indicators_added": indicators_added_total,
        "groups": groups,
        "unexpected_missingness_flags": unexpected_flags,
        "blocked_reasons": [],
    }
