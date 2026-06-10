"""bot.ml.models.baselines — three baseline trainers.

The three baselines are SPECIFIED in bot.ml.schemas.ALLOWED_MODEL_TYPES:

  B0_majority           DummyClassifier-style: predict the train-set
                          majority class always, returning that class's
                          train-frequency as the probability for every
                          row. Sets the trivial floor for binary
                          classifiers.

  B1_scanner_replica    Passthrough of the scanner_replica.signal_fires
                          feature column. NO fitting — this baseline
                          represents "what if we accept the live
                          scanner's binary decision as the model
                          output?" For Model A cohorts (anchors WHERE
                          scanner fires), every row has fires=1 so the
                          baseline always predicts 1. For Model B
                          cohorts (1H ∪ scanner candidates), this
                          predicts 1 only at the scanner-candidate
                          anchors and 0 at the 1H-only anchors.

  B2_logistic           sklearn LogisticRegression with StandardScaler.
                          The simplest LEARNED baseline. Deterministic
                          via random_state (=seed). NO hyperparameter
                          tuning in M18.A.6 — that lands in a later
                          phase if needed.

Every trainer has:
  * model_type     str  matches ALLOWED_MODEL_TYPES
  * fit(...)       returns None; idempotent
  * predict_proba(...)  returns shape (n,) array of class-1 probabilities
                          (binary) or yhat (regression — only B0 + B2
                          for now; B1 is binary-only)

Trainers operate on plain numpy arrays — column slicing and pandas
boilerplate live in the orchestrator (trainer.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

from bot.ml.errors import M18ConfigError


# ─────────────────────────────────────────────────────────────────────
# B0_majority
# ─────────────────────────────────────────────────────────────────────

class MajorityClassTrainer:
    """B0_majority — predicts the train-set majority class always.

    For binary classification, predict_proba returns the train-set
    frequency of the predicted majority class (a scalar broadcast
    across n rows).

    For regression, predict_proba returns the train-set mean of y.
    """
    model_type = "B0_majority"

    def __init__(self):
        self.is_classification_: Optional[bool]    = None
        self.majority_class_:     Optional[float]  = None
        self.prediction_constant_: Optional[float] = None

    def fit(self, y_train: np.ndarray, *, label_class: str,
              seed: int = 42) -> None:
        if len(y_train) == 0:
            raise M18ConfigError(
                "B0_majority requires at least 1 train row")
        if label_class in ("binary", "classification_3way"):
            self.is_classification_ = True
            unique, counts = np.unique(y_train, return_counts=True)
            # Deterministic tiebreak: smallest label wins
            order = np.lexsort((unique, -counts))
            self.majority_class_ = float(unique[order[0]])
            # predict_proba semantics: probability the row IS class 1
            # (calibrated prior; sklearn DummyClassifier strategy=
            # 'prior'). When the majority class is 0 this is < 0.5
            # so predictions binarise to 0 (= majority); when the
            # majority class is 1 this is > 0.5 so predictions
            # binarise to 1 (= majority). Either way, the 0.5-
            # threshold prediction equals the majority class, but
            # the probability stays informative for Brier / AUC.
            self.prediction_constant_ = float(
                np.mean(y_train == 1.0))
        else:
            # Regression: constant = train mean
            self.is_classification_ = False
            self.majority_class_ = None
            self.prediction_constant_ = float(np.mean(y_train))

    def predict_proba(self, n_rows: int) -> np.ndarray:
        if self.prediction_constant_ is None:
            raise M18ConfigError("B0_majority not fitted")
        return np.full(n_rows, self.prediction_constant_,
                        dtype=np.float64)

    def library_versions(self) -> Dict[str, str]:
        return {"numpy": np.__version__}


# ─────────────────────────────────────────────────────────────────────
# B1_scanner_replica
# ─────────────────────────────────────────────────────────────────────

class ScannerReplicaTrainer:
    """B1_scanner_replica — passthrough of scanner_replica.signal_fires.

    Not a fitted model. Records the train-set positive rate for
    diagnostics, then returns the signal_fires column directly as the
    "probability" on each split.

    Only meaningful for the binary triple-barrier-won target — this
    baseline says "trust the live scanner" and asks whether learned
    models can do better than that.
    """
    model_type = "B1_scanner_replica"

    def __init__(self):
        self.train_positive_rate_: Optional[float] = None

    def fit(self, signal_fires_train: np.ndarray, *,
              seed: int = 42) -> None:
        if len(signal_fires_train) == 0:
            raise M18ConfigError(
                "B1_scanner_replica requires at least 1 train row")
        self.train_positive_rate_ = float(
            np.mean(signal_fires_train.astype(float)))

    def predict_proba(self,
                        signal_fires: np.ndarray) -> np.ndarray:
        """Identity passthrough — interpret signal_fires as the
        probability of the positive class."""
        if self.train_positive_rate_ is None:
            raise M18ConfigError("B1_scanner_replica not fitted")
        return signal_fires.astype(np.float64).copy()

    def library_versions(self) -> Dict[str, str]:
        return {"numpy": np.__version__}


# ─────────────────────────────────────────────────────────────────────
# B2_logistic
# ─────────────────────────────────────────────────────────────────────

class LogisticRegressionTrainer:
    """B2_logistic — sklearn LR + StandardScaler.

    Deterministic on the same input + seed (solver='liblinear',
    random_state=seed). NaN-tolerant: feature NaNs were imputed to 0
    by extract_xy_for_split() upstream.

    For class_imbalance: uses class_weight='balanced' by default.
    Hyperparameter tuning is deliberately omitted in M18.A.6 — the
    baseline is intended to be a no-frills floor.
    """
    model_type = "B2_logistic"

    def __init__(self):
        # Lazy imports so this module's mere existence doesn't pull
        # sklearn at package-load time. _require_sklearn() in fit().
        self._scaler        = None
        self._classifier    = None
        self._label_class:  Optional[str] = None

    @staticmethod
    def _require_sklearn():
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler
            import sklearn
        except ImportError as e:
            raise M18ConfigError(
                "B2_logistic requires scikit-learn. sklearn is the "
                "only ML dependency M18 relies on and is already used "
                "elsewhere in the project."
            ) from e
        return LogisticRegression, StandardScaler, sklearn

    def fit(self, X_train: np.ndarray, y_train: np.ndarray, *,
              label_class: str, seed: int = 42) -> None:
        if label_class != "binary":
            raise M18ConfigError(
                f"B2_logistic in M18.A.6 supports binary targets only; "
                f"got label_class={label_class!r}")
        LR, SS, sk = self._require_sklearn()
        self._label_class = label_class
        self._sklearn_version_ = sk.__version__
        self._scaler = SS()
        Xs = self._scaler.fit_transform(X_train)
        self._classifier = LR(
            solver="liblinear",
            C=1.0,
            max_iter=1000,
            random_state=int(seed),
            class_weight="balanced",
        )
        self._classifier.fit(Xs, y_train)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self._classifier is None:
            raise M18ConfigError("B2_logistic not fitted")
        if X.shape[0] == 0:
            return np.empty((0,), dtype=np.float64)
        Xs = self._scaler.transform(X)
        # LR.classes_ is sorted ascending; class 1 is the second column.
        proba = self._classifier.predict_proba(Xs)
        # Locate the column corresponding to class==1.0
        classes = self._classifier.classes_
        pos_idx = int(np.where(classes == 1.0)[0][0]) if (
            1.0 in classes) else 1
        return proba[:, pos_idx].astype(np.float64)

    def library_versions(self) -> Dict[str, str]:
        out = {"numpy": np.__version__}
        if getattr(self, "_sklearn_version_", None):
            out["sklearn"] = self._sklearn_version_
        return out
