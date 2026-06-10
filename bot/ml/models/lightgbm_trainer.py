"""bot.ml.models.lightgbm_trainer — M_lightgbm trainer (import-gated).

Per M18.A.6 directive: LightGBM is used ONLY IF ALREADY INSTALLED in
the venv. M18.A.6 must NOT add lightgbm to requirements.txt.

If lightgbm is not importable at runtime and the operator requests
M_lightgbm, the trainer raises M18ConfigError immediately with a
clear message — it does NOT silently fall back to another model.

LightGBM determinism (when used):
  deterministic=True
  num_threads=1
  force_col_wise=True
  random_state=seed
  verbosity=-1

These are the documented LightGBM determinism flags. The trainer is
otherwise a thin wrapper.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np

from bot.ml.errors import M18ConfigError


def is_lightgbm_available() -> bool:
    """Return True iff `import lightgbm` succeeds."""
    try:
        import lightgbm  # noqa: F401
        return True
    except ImportError:
        return False


class LightGBMTrainer:
    """M_lightgbm — LightGBM gradient boosting, binary classification.

    Raises M18ConfigError if lightgbm is unavailable. Tests for this
    trainer use unittest.skipUnless(is_lightgbm_available()).
    """
    model_type = "M_lightgbm"

    def __init__(self):
        self._classifier  = None
        self._lgb_version = None

    @staticmethod
    def _require_lightgbm():
        try:
            import lightgbm as lgb
        except ImportError as e:
            raise M18ConfigError(
                "M_lightgbm requested but lightgbm is not installed in "
                "the venv. M18.A.6 does NOT add lightgbm to "
                "requirements.txt by design. Either install lightgbm "
                "(`pip install lightgbm`) before training this model, "
                "or choose a different model_type (e.g. B2_logistic)."
            ) from e
        return lgb

    def fit(self, X_train: np.ndarray, y_train: np.ndarray, *,
              label_class: str, seed: int = 42,
              hyperparameters: Optional[Dict[str, Any]] = None,
              ) -> None:
        if label_class != "binary":
            raise M18ConfigError(
                f"M_lightgbm in M18.A.6 supports binary targets only; "
                f"got label_class={label_class!r}")
        lgb = self._require_lightgbm()
        self._lgb_version = lgb.__version__

        defaults = dict(
            objective="binary",
            deterministic=True,
            num_threads=1,
            force_col_wise=True,
            random_state=int(seed),
            verbosity=-1,
            n_estimators=100,
            learning_rate=0.05,
            num_leaves=31,
            min_data_in_leaf=20,
        )
        if hyperparameters:
            # Allow overrides for n_estimators / learning_rate / etc.
            # but FORBID overriding the determinism flags.
            forbidden = {"deterministic", "num_threads", "force_col_wise",
                          "random_state"}
            bad = [k for k in hyperparameters if k in forbidden]
            if bad:
                raise M18ConfigError(
                    f"M_lightgbm hyperparameters cannot override the "
                    f"determinism flags {sorted(forbidden)}; "
                    f"attempted: {sorted(bad)}")
            defaults.update(hyperparameters)

        self._classifier = lgb.LGBMClassifier(**defaults)
        self._classifier.fit(X_train, y_train)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self._classifier is None:
            raise M18ConfigError("M_lightgbm not fitted")
        if X.shape[0] == 0:
            return np.empty((0,), dtype=np.float64)
        proba = self._classifier.predict_proba(X)
        classes = self._classifier.classes_
        pos_idx = int(np.where(classes == 1.0)[0][0]) if (
            1.0 in classes) else 1
        return proba[:, pos_idx].astype(np.float64)

    def library_versions(self) -> Dict[str, str]:
        return {
            "numpy":    np.__version__,
            "lightgbm": self._lgb_version or "not_imported",
        }
