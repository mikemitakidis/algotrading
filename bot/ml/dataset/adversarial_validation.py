"""bot.ml.dataset.adversarial_validation — classifier-based adversarial
validation gate.

Adversarial validation is the canonical test for "are train and
holdout actually drawn from the same distribution?" Per the locked
SR-Q17 plan:

  * Label each TRAIN row with target=0 and each HOLDOUT row with
    target=1.
  * Train a SIMPLE classifier (LogisticRegression here — choice is
    determinism + interpretability) to predict the binary target.
  * Report ROC AUC estimated via cross-validation on the COMBINED
    set (not training AUC — that would just measure model fit).
  * Gate: PASS iff mean CV AUC <= threshold (default 0.55). A high
    AUC means train and holdout are easily distinguishable, which
    indicates drift or leakage and blocks promotion.

This module is DISTINCT from a distribution-distance / PSI proxy —
those are useful as diagnostics but cannot replace an adversarial
classifier. A separate function distribution_shift_proxy_psi() is
provided for diagnostic use; it is NEVER reported as
"adversarial_validation" in the manifest.

DEPENDENCY: sklearn (already a project dependency via ml_train.py;
requirements.txt is protected so it isn't modified). The functions
here raise a clear AdversarialValidationError if sklearn is missing
at runtime rather than letting an opaque ImportError bubble up.

DETERMINISM: random_state=42 throughout. cv_folds default to 5. LR
solver='liblinear' (deterministic on small/medium datasets).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bot.ml.errors import M18Error


# ─── Explicit AV status / reason vocabulary (M18.B.6) ────────────────
# Before B.6 a failed/unrunnable AV collapsed to av_result=None, which
# conflated "exception", "no split", and "fixture skip" and lost the
# reason. These give every AV outcome an explicit, JSON-safe status +
# stable reason string that the manifest persists and gating consumes.
AV_STATUS_PASSED                 = "passed"
AV_STATUS_FAILED                 = "failed"
AV_STATUS_SKIPPED_NOT_ENOUGH_DATA = "skipped_not_enough_data"
AV_STATUS_UNAVAILABLE_ERROR      = "unavailable_error"
AV_STATUS_DISABLED_FIXTURE_MODE  = "disabled_fixture_mode"
AV_STATUS_SKIPPED_NO_SPLIT       = "skipped_no_split"

AV_REASON_PASSED          = "av_passed"
AV_REASON_FAILED          = "av_failed"
AV_REASON_NOT_ENOUGH_ROWS = "av_not_enough_rows"
AV_REASON_NOT_ENOUGH_CLASSES = "av_not_enough_classes"
AV_REASON_NO_USABLE_FEATURES = "av_no_usable_features"
AV_REASON_EXCEPTION       = "av_exception"
AV_REASON_FIXTURE_MODE    = "av_fixture_mode"
AV_REASON_MISSING_SPLIT   = "av_missing_split"


def classify_av_error_reason(exc: Exception) -> str:
    """Map an AdversarialValidationError (or other exception) raised by
    run_adversarial_validation to a STABLE reason string. Inspects the
    message text for the known not-enough-data conditions; anything
    unrecognised is the generic av_exception."""
    msg = str(exc).lower()
    if "needs >= cv_folds" in msg or "rows per side" in msg:
        return AV_REASON_NOT_ENOUGH_ROWS
    if "non-empty inputs" in msg:
        return AV_REASON_NOT_ENOUGH_ROWS
    if "no usable features" in msg:
        return AV_REASON_NO_USABLE_FEATURES
    if "no usable folds" in msg or "class balance" in msg:
        return AV_REASON_NOT_ENOUGH_CLASSES
    return AV_REASON_EXCEPTION


def av_reason_is_not_enough_data(reason: str) -> bool:
    """True iff the reason is a 'not enough data' condition (vs a true
    error). Determines status skipped_not_enough_data vs
    unavailable_error."""
    return reason in (
        AV_REASON_NOT_ENOUGH_ROWS,
        AV_REASON_NOT_ENOUGH_CLASSES,
        AV_REASON_NO_USABLE_FEATURES,
    )


class AdversarialValidationError(M18Error):
    """Raised when adversarial validation cannot run (e.g. sklearn
    missing, empty inputs, no usable features)."""


@dataclass
class AdversarialValidationResult:
    """One AV run's report. Manifest-serializable via to_dict()."""
    auc_mean:          float
    auc_per_fold:      List[float]
    threshold:         float
    passed:            bool
    classifier:        str       # "logistic_regression"
    n_train_rows:      int       # rows used (post-NaN-drop)
    n_holdout_rows:    int       # rows used (post-NaN-drop)
    n_train_rows_dropped:   int  # dropped due to NaN
    n_holdout_rows_dropped: int
    feature_count_used: int
    dropped_features:  List[str]  # all-NaN or constant in either set
    cv_folds:          int
    random_state:      int
    classifier_params: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _require_sklearn():
    """Single point of import. Raises AdversarialValidationError if
    sklearn is missing (not the opaque ImportError)."""
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import StratifiedKFold
        from sklearn.preprocessing import StandardScaler
    except ImportError as e:
        raise AdversarialValidationError(
            "scikit-learn is required for adversarial validation but "
            "is not importable. Adversarial validation cannot be "
            "downgraded to a distribution-distance proxy (see "
            "distribution_shift_proxy_psi for that separate "
            "diagnostic)."
        ) from e
    return LogisticRegression, roc_auc_score, StratifiedKFold, StandardScaler


def _drop_unusable_features(
    X_train: pd.DataFrame, X_holdout: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    """Drop columns that are all-NaN in either set OR constant in
    either set (no variance → cannot distinguish train from holdout
    on that feature). Returns (X_train_kept, X_holdout_kept, dropped_ids)."""
    dropped: List[str] = []
    keep: List[str] = []
    for c in X_train.columns:
        if c not in X_holdout.columns:
            dropped.append(c)
            continue
        tr = X_train[c]
        ho = X_holdout[c]
        if tr.isna().all() or ho.isna().all():
            dropped.append(c)
            continue
        # If non-NaN values are constant in either set, drop.
        tr_nonan = tr.dropna()
        ho_nonan = ho.dropna()
        if tr_nonan.nunique() <= 1 or ho_nonan.nunique() <= 1:
            dropped.append(c)
            continue
        keep.append(c)
    return X_train[keep], X_holdout[keep], dropped


def run_adversarial_validation(
    X_train: pd.DataFrame,
    X_holdout: pd.DataFrame,
    *,
    threshold: float = 0.55,
    cv_folds: int = 5,
    random_state: int = 42,
    max_iter: int = 1000,
    only_numeric_dtypes: bool = True,
) -> AdversarialValidationResult:
    """Run adversarial validation: train a LogisticRegression to
    distinguish X_train rows (target=0) from X_holdout rows
    (target=1), report cross-validated AUC, gate at `threshold`.

    Parameters
    ----------
    X_train, X_holdout : pd.DataFrame
        Feature frames (NO labels). Same columns required; columns
        missing from either side are dropped from the run.
    threshold : float
        Gate threshold. PASS iff mean CV AUC <= threshold. Default
        0.55 per the locked plan.
    cv_folds : int
        Number of stratified folds for CV-AUC estimation. Default 5.
    random_state : int
        Seed for LR + CV split. Default 42 (deterministic).
    max_iter : int
        LogisticRegression max_iter. Default 1000.
    only_numeric_dtypes : bool
        If True (default), restricts to float/int dtype columns —
        we don't one-hot-encode categoricals in AV (this is a
        sanity gate, not a full model).

    Returns
    -------
    AdversarialValidationResult

    Raises
    ------
    AdversarialValidationError if sklearn is missing, inputs are
    empty, or no usable features remain after filtering.
    """
    LogisticRegression, roc_auc_score, StratifiedKFold, StandardScaler = \
        _require_sklearn()

    if len(X_train) == 0 or len(X_holdout) == 0:
        raise AdversarialValidationError(
            f"adversarial validation needs non-empty inputs; got "
            f"X_train rows={len(X_train)}, X_holdout rows={len(X_holdout)}")

    if only_numeric_dtypes:
        num_train = X_train.select_dtypes(
            include=[np.number]).copy()
        num_holdout = X_holdout.select_dtypes(
            include=[np.number]).copy()
    else:
        num_train = X_train.copy()
        num_holdout = X_holdout.copy()

    # Drop unusable features
    X_train_k, X_holdout_k, dropped_feats = _drop_unusable_features(
        num_train, num_holdout)

    if X_train_k.shape[1] == 0:
        raise AdversarialValidationError(
            f"adversarial validation has no usable features after "
            f"NaN/constant filtering; dropped {len(dropped_feats)} "
            f"features; cannot fit a classifier")

    # Drop rows with any remaining NaN — LR can't take NaN inputs.
    n_train_pre = len(X_train_k)
    n_holdout_pre = len(X_holdout_k)
    X_train_k = X_train_k.dropna(axis=0, how="any")
    X_holdout_k = X_holdout_k.dropna(axis=0, how="any")
    n_train_dropped   = n_train_pre   - len(X_train_k)
    n_holdout_dropped = n_holdout_pre - len(X_holdout_k)

    if len(X_train_k) < cv_folds or len(X_holdout_k) < cv_folds:
        raise AdversarialValidationError(
            f"adversarial validation needs >= cv_folds={cv_folds} "
            f"rows per side after NaN drop; got X_train={len(X_train_k)} "
            f"X_holdout={len(X_holdout_k)}")

    # Build the combined design matrix + binary target.
    X_combined = pd.concat([X_train_k, X_holdout_k], axis=0,
                             ignore_index=True)
    y = np.concatenate([
        np.zeros(len(X_train_k), dtype=np.int8),
        np.ones (len(X_holdout_k), dtype=np.int8),
    ])

    # Standardize per-column (LR likes it; deterministic).
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_combined.to_numpy(dtype=float))

    # Cross-validated AUC. Use StratifiedKFold to balance the
    # binary target across folds (we have ~50/50 typically; small
    # imbalances should still split cleanly).
    classifier_params = {
        "solver":       "liblinear",
        "C":            1.0,
        "max_iter":     int(max_iter),
        "random_state": int(random_state),
    }
    skf = StratifiedKFold(n_splits=int(cv_folds), shuffle=True,
                            random_state=int(random_state))

    auc_per_fold: List[float] = []
    for train_idx, test_idx in skf.split(X_scaled, y):
        clf = LogisticRegression(**classifier_params)
        clf.fit(X_scaled[train_idx], y[train_idx])
        proba = clf.predict_proba(X_scaled[test_idx])[:, 1]
        # If a fold somehow ends up with one class only on the
        # test side, roc_auc_score raises — that should never happen
        # with StratifiedKFold but guard anyway.
        if len(np.unique(y[test_idx])) < 2:
            continue
        auc_per_fold.append(float(roc_auc_score(y[test_idx], proba)))

    if not auc_per_fold:
        raise AdversarialValidationError(
            "adversarial validation produced no usable folds; "
            "check class balance")

    auc_mean = float(np.mean(auc_per_fold))
    passed = bool(auc_mean <= float(threshold))

    return AdversarialValidationResult(
        auc_mean=auc_mean,
        auc_per_fold=auc_per_fold,
        threshold=float(threshold),
        passed=passed,
        classifier="logistic_regression",
        n_train_rows=int(len(X_train_k)),
        n_holdout_rows=int(len(X_holdout_k)),
        n_train_rows_dropped=int(n_train_dropped),
        n_holdout_rows_dropped=int(n_holdout_dropped),
        feature_count_used=int(X_train_k.shape[1]),
        dropped_features=sorted(dropped_feats),
        cv_folds=int(cv_folds),
        random_state=int(random_state),
        classifier_params=classifier_params,
    )


# ─────────────────────────────────────────────────────────────────────
# Separate diagnostic — NOT a substitute for adversarial validation
# ─────────────────────────────────────────────────────────────────────

def distribution_shift_proxy_psi(
    X_train: pd.DataFrame,
    X_holdout: pd.DataFrame,
    *,
    n_bins: int = 10,
) -> Dict[str, float]:
    """Population Stability Index PER FEATURE — a distribution-shift
    DIAGNOSTIC. This is intentionally a separate function with a
    distinct name so it is NEVER reported as 'adversarial_validation'
    in the manifest.

    Returns dict feature_name -> PSI scalar. Higher PSI = more shift.
    Rule-of-thumb thresholds: < 0.1 stable; 0.1-0.25 moderate; >= 0.25
    significant. Use this for FEATURE-LEVEL inspection when AV fails
    — it's much more interpretable than 'AUC > 0.55'.
    """
    numeric_cols = X_train.select_dtypes(include=[np.number]).columns
    out: Dict[str, float] = {}
    for c in numeric_cols:
        if c not in X_holdout.columns:
            continue
        tr = X_train[c].dropna().to_numpy(dtype=float)
        ho = X_holdout[c].dropna().to_numpy(dtype=float)
        if len(tr) < 10 or len(ho) < 10:
            continue
        # Use quantile bins from the training distribution.
        try:
            bin_edges = np.unique(
                np.quantile(tr, np.linspace(0, 1, n_bins + 1)))
        except Exception:
            continue
        if len(bin_edges) < 3:
            continue
        # Bound by ±inf so edges-of-distribution samples count.
        bin_edges = np.concatenate([[-np.inf], bin_edges[1:-1],
                                      [np.inf]])
        tr_counts, _ = np.histogram(tr, bins=bin_edges)
        ho_counts, _ = np.histogram(ho, bins=bin_edges)
        tr_pct = (tr_counts + 1) / (tr_counts.sum() + len(tr_counts))
        ho_pct = (ho_counts + 1) / (ho_counts.sum() + len(ho_counts))
        psi = float(np.sum((ho_pct - tr_pct)
                              * np.log(ho_pct / tr_pct)))
        out[c] = psi
    return out
