"""bot.ml.models.random_forest_trainer — M_random_forest trainer.

M18.B.1: sklearn RandomForestClassifier as a tree-based model that does
NOT require LightGBM. sklearn is the only ML dependency M18 relies on
and is already used elsewhere (B2_logistic, calibration, permutation
importance), so this adds NO new runtime dependency.

This trainer is NOT a silent automatic fallback. It trains only when
`train_config.model_type == "M_random_forest"` is explicitly requested;
it never replaces M_lightgbm on its own.

Determinism
-----------
RandomForest is deterministic given a fixed `random_state` and
single-threaded execution. The trainer pins:

  random_state = train_config.seed   (caller-supplied; cannot be overridden)
  n_jobs       = 1                   (cannot be overridden)
  bootstrap    = True with the fixed random_state seeding the RNG

so two runs with the same data + seed produce byte-identical
predict_proba output. Multi-threading (n_jobs != 1) reorders
floating-point reductions and is therefore forbidden.

Default hyperparameters (M18.B.1 recommended set)
-------------------------------------------------
  n_estimators      = 300
  max_depth         = 10
  min_samples_leaf  = 30
  class_weight      = "balanced_subsample"
  random_state      = seed
  n_jobs            = 1

Safe overrides (allowed): n_estimators, max_depth, min_samples_leaf,
class_weight. Anything else — and in particular n_jobs, random_state,
bootstrap, or any non-deterministic knob — raises M18ConfigError.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np

from bot.ml.errors import M18ConfigError


# Hyperparameters a caller may override (all determinism-preserving).
_RF_SAFE_OVERRIDE_KEYS = frozenset({
    "n_estimators",
    "max_depth",
    "min_samples_leaf",
    "class_weight",
})

# Default hyperparameters (the M18.B.1 recommended set).
_RF_DEFAULTS: Dict[str, Any] = {
    "n_estimators":     300,
    "max_depth":        10,
    "min_samples_leaf": 30,
    "class_weight":     "balanced_subsample",
}


class RandomForestTrainer:
    """M_random_forest — sklearn RandomForest, binary classification.

    Deterministic on the same input + seed (random_state=seed, n_jobs=1).
    NaN-tolerant: feature NaNs were imputed to 0 by extract_xy_for_split()
    upstream. sklearn-only; never imports lightgbm.
    """
    model_type = "M_random_forest"

    def __init__(self):
        # Lazy import so this module's mere existence doesn't pull
        # sklearn at package-load time; _require_sklearn() in fit().
        self._classifier = None
        self._label_class: Optional[str] = None
        self._sklearn_version_: Optional[str] = None

    @staticmethod
    def _require_sklearn():
        try:
            from sklearn.ensemble import RandomForestClassifier
            import sklearn
        except ImportError as e:
            raise M18ConfigError(
                "M_random_forest requires scikit-learn. sklearn is the "
                "only ML dependency M18 relies on and is already used "
                "elsewhere in the project; it adds no new requirement."
            ) from e
        return RandomForestClassifier, sklearn

    @staticmethod
    def _resolve_hyperparameters(
        hyperparameters: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Merge caller overrides onto the defaults, rejecting any
        unsafe / non-deterministic key. Returns the safe param dict
        (without random_state / n_jobs, which are pinned in fit())."""
        params = dict(_RF_DEFAULTS)
        if hyperparameters:
            bad = [k for k in hyperparameters
                   if k not in _RF_SAFE_OVERRIDE_KEYS]
            if bad:
                raise M18ConfigError(
                    f"M_random_forest hyperparameters may override only "
                    f"{sorted(_RF_SAFE_OVERRIDE_KEYS)}; unsupported / "
                    f"unsafe keys attempted: {sorted(bad)}. In "
                    f"particular n_jobs, random_state, and bootstrap "
                    f"are pinned for determinism and cannot be set.")
            params.update(hyperparameters)
        return params

    def fit(self, X_train: np.ndarray, y_train: np.ndarray, *,
              label_class: str, seed: int = 42,
              hyperparameters: Optional[Dict[str, Any]] = None,
              ) -> None:
        if label_class != "binary":
            raise M18ConfigError(
                f"M_random_forest in M18.B.1 supports binary targets "
                f"only; got label_class={label_class!r}")
        if X_train.shape[0] == 0:
            raise M18ConfigError(
                "M_random_forest requires at least 1 train row; got an "
                "empty train set")
        # One-class train set: RandomForest would learn a degenerate
        # always-one-class model and predict_proba would emit a single
        # column. Fail clearly rather than silently emit misleading
        # probabilities.
        uniq = np.unique(y_train)
        if uniq.shape[0] < 2:
            raise M18ConfigError(
                f"M_random_forest requires both classes present in the "
                f"train set; got a single-class train target "
                f"(class={uniq.tolist()}). Cannot fit a binary model on "
                f"one class.")
        if not np.all(np.isfinite(X_train)):
            raise M18ConfigError(
                "M_random_forest requires finite numeric features; "
                "train matrix contains non-finite values (NaN/inf). "
                "extract_xy_for_split() should have imputed feature "
                "NaNs upstream.")

        RF, sk = self._require_sklearn()
        self._label_class = label_class
        self._sklearn_version_ = sk.__version__

        params = self._resolve_hyperparameters(hyperparameters)
        self._classifier = RF(
            n_estimators=int(params["n_estimators"]),
            max_depth=(None if params["max_depth"] is None
                       else int(params["max_depth"])),
            min_samples_leaf=int(params["min_samples_leaf"]),
            class_weight=params["class_weight"],
            random_state=int(seed),   # pinned: caller seed, not override
            n_jobs=1,                 # pinned: single-threaded determinism
            bootstrap=True,           # seeded by random_state
        )
        self._classifier.fit(X_train, y_train)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self._classifier is None:
            raise M18ConfigError("M_random_forest not fitted")
        if X.shape[0] == 0:
            return np.empty((0,), dtype=np.float64)
        proba = self._classifier.predict_proba(X)
        # classes_ is sorted ascending; locate the column for class==1.0
        classes = self._classifier.classes_
        pos_idx = int(np.where(classes == 1.0)[0][0]) if (
            1.0 in classes) else 1
        out = proba[:, pos_idx].astype(np.float64)
        return out

    def library_versions(self) -> Dict[str, str]:
        out = {"numpy": np.__version__}
        if self._sklearn_version_:
            out["sklearn"] = self._sklearn_version_
        return out
