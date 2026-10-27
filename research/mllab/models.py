"""research.mllab.models — the model zoo: baselines, boosted trees, ensembles,
calibration and regime-conditioned challengers.

Every model has the same contract:
    fit(X, y, *, start, end)   start/end: int64 decision / resolution times,
                               used for purged internal validation splits
    predict_proba(X) -> P(y=1) as a 1-D array

Simple baselines come first on purpose: ``prior`` (base rate) and
``logistic`` are the bars every challenger must clear out of sample.

Challengers:
* rf, extra_trees, hgb (sklearn), xgb (XGBoost), lgbm (LightGBM, optional),
  catboost (optional), mlp (small neural net)
* Calibrated(base, "isotonic" | "sigmoid"): calibrator fitted on the
  chronologically LAST part of the training data, with the base model fitted
  on the earlier part (purged), never on the same rows
* MeanEnsemble: average of calibrated members (diversity reported separately)
* StackedEnsemble: members' out-of-fold predictions from a purged walk-forward
  INSIDE the training window feed a logistic meta-learner
* RegimeModel: separate models per causal regime state (a feature column),
  with a global fallback where a regime is thin
* BaggedModel: seed/bootstrap bag of one learner; its member dispersion is an
  epistemic-uncertainty signal

All randomness is seeded through research.common.rng.child_seed, and every
model is bit-for-bit reproducible for a given seed: random forests run
single-threaded (threaded probability accumulation changes the last bits)
and LightGBM runs in deterministic mode.
"""
from __future__ import annotations

import importlib.util
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from research.common.rng import child_seed
from research.integrity.splits import walk_forward

EPS = 1e-6
CAL_CLIP = 0.02    # isotonic steps can reach 0/1; no trade outcome is that certain


def _logit(p):
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


def available(name: str) -> bool:
    mod = {"lgbm": "lightgbm", "catboost": "catboost", "xgb": "xgboost"}.get(name)
    return True if mod is None else importlib.util.find_spec(mod) is not None


class Prior:
    name = "prior"

    def fit(self, X, y, **kw):
        self.p_ = float(np.clip(np.mean(y), EPS, 1 - EPS))
        return self

    def predict_proba(self, X):
        return np.full(len(X), self.p_)


class SK:
    """Adapter for an sklearn-style classifier; degenerates to the prior on single-class data."""

    def __init__(self, name: str, make: Callable[[], object]):
        self.name, self._make = name, make

    def fit(self, X, y, **kw):
        y = np.asarray(y)
        if len(np.unique(y)) < 2:
            self.est_ = None
            self.p_ = float(np.clip(y.mean(), EPS, 1 - EPS)) if len(y) else 0.5
            return self
        self.est_ = self._make()
        self.est_.fit(X, y)
        return self

    def predict_proba(self, X):
        if self.est_ is None:
            return np.full(len(X), self.p_)
        return self.est_.predict_proba(X)[:, 1]


def make(name: str, seed: int = 0) -> object:
    """Factory for named base models (deterministic in `seed`)."""
    s = child_seed(seed, "model", name)
    if name == "prior":
        return Prior()
    if name == "logistic":
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        return SK(name, lambda: make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=2000)))
    if name == "rf":
        from sklearn.ensemble import RandomForestClassifier
        return SK(name, lambda: RandomForestClassifier(n_estimators=150, max_depth=8, min_samples_leaf=40,
                                                       max_features=0.3, n_jobs=1, random_state=s))
    if name == "extra_trees":
        from sklearn.ensemble import ExtraTreesClassifier
        return SK(name, lambda: ExtraTreesClassifier(n_estimators=150, max_depth=8, min_samples_leaf=40,
                                                     max_features=0.3, n_jobs=1, random_state=s))
    if name == "hgb":
        from sklearn.ensemble import HistGradientBoostingClassifier
        return SK(name, lambda: HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_depth=4,
                                                               min_samples_leaf=60, l2_regularization=1.0,
                                                               random_state=s))
    if name == "xgb":
        import xgboost as xgb
        return SK(name, lambda: xgb.XGBClassifier(n_estimators=200, max_depth=4, learning_rate=0.05,
                                                  subsample=0.8, colsample_bytree=0.6, min_child_weight=20,
                                                  reg_lambda=2.0, tree_method="hist", n_jobs=2, random_state=s,
                                                  eval_metric="logloss", verbosity=0))
    if name == "lgbm":
        import lightgbm as lgb
        return SK(name, lambda: lgb.LGBMClassifier(n_estimators=200, num_leaves=15, learning_rate=0.05,
                                                   min_child_samples=60, subsample=0.8, subsample_freq=1,
                                                   colsample_bytree=0.6, reg_lambda=2.0, random_state=s,
                                                   n_jobs=2, verbose=-1, deterministic=True,
                                                   force_row_wise=True))
    if name == "catboost":
        from catboost import CatBoostClassifier
        return SK(name, lambda: CatBoostClassifier(iterations=200, depth=4, learning_rate=0.05,
                                                   random_seed=s, verbose=False, thread_count=2,
                                                   allow_writing_files=False))
    if name == "mlp":
        from sklearn.neural_network import MLPClassifier
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        return SK(name, lambda: make_pipeline(StandardScaler(), MLPClassifier(hidden_layer_sizes=(32, 16), alpha=1e-2,
                                                                              max_iter=300, early_stopping=True,
                                                                              random_state=s)))
    raise ValueError("unknown model %r" % name)


def _time_split(start, end, frac: float):
    """Chronological split of a training window into (fit, holdout) with the
    holdout = last `frac` of events and fit rows purged of overlap."""
    n = len(start)
    cut = int(n * (1 - frac))
    if cut < 10 or n - cut < 10:
        return np.arange(n), np.arange(0)
    hold = np.arange(cut, n)
    fit = np.arange(cut)
    fit = fit[np.asarray(end)[fit] < np.asarray(start)[hold].min()]
    return fit, hold


class Calibrated:
    def __init__(self, base, method: str = "isotonic", frac: float = 0.25):
        if method not in ("isotonic", "sigmoid"):
            raise ValueError("method must be isotonic or sigmoid")
        self.base, self.method, self.frac = base, method, frac
        self.name = "%s+%s" % (getattr(base, "name", "model"), "iso" if method == "isotonic" else "platt")

    def fit(self, X, y, *, start, end):
        fit_i, hold_i = _time_split(start, end, self.frac)
        y = np.asarray(y)
        self.base.fit(X[fit_i], y[fit_i], start=np.asarray(start)[fit_i], end=np.asarray(end)[fit_i])
        if len(hold_i) < 30 or len(np.unique(y[hold_i])) < 2:
            self.cal_ = None
            return self
        p = self.base.predict_proba(X[hold_i])
        if self.method == "isotonic":
            from sklearn.isotonic import IsotonicRegression
            self.cal_ = IsotonicRegression(y_min=CAL_CLIP, y_max=1 - CAL_CLIP, out_of_bounds="clip").fit(p, y[hold_i])
        else:
            from sklearn.linear_model import LogisticRegression
            self.cal_ = LogisticRegression(C=1e6, max_iter=1000).fit(_logit(p)[:, None], y[hold_i])
        return self

    def member_proba(self, X):
        """Uncalibrated member probabilities of an ensemble/bagged base (epistemic signal)."""
        if not hasattr(self.base, "member_proba"):
            raise AttributeError("%s has no ensemble members" % self.name)
        return self.base.member_proba(X)

    def predict_proba(self, X):
        p = self.base.predict_proba(X)
        if self.cal_ is None:
            return p
        if self.method == "isotonic":
            return np.clip(self.cal_.predict(p), CAL_CLIP, 1 - CAL_CLIP)
        return self.cal_.predict_proba(_logit(p)[:, None])[:, 1]


class MeanEnsemble:
    def __init__(self, members: Sequence, name: str = "mean_ensemble"):
        self.members, self.name = list(members), name

    def fit(self, X, y, *, start, end):
        for m in self.members:
            m.fit(X, y, start=start, end=end)
        return self

    def member_proba(self, X) -> np.ndarray:
        return np.column_stack([m.predict_proba(X) for m in self.members])

    def predict_proba(self, X):
        return self.member_proba(X).mean(axis=1)


class StackedEnsemble:
    """Logistic meta-learner on purged out-of-fold member predictions."""

    def __init__(self, member_factories: Sequence[Callable[[], object]], inner_folds: int = 3,
                 name: str = "stacked"):
        self.factories, self.inner_folds, self.name = list(member_factories), inner_folds, name

    def fit(self, X, y, *, start, end):
        from sklearn.linear_model import LogisticRegression
        y = np.asarray(y)
        start, end = np.asarray(start), np.asarray(end)
        folds = walk_forward(start, end, n_folds=self.inner_folds, min_train_groups=1)
        oof_rows, oof_pred = [], []
        for f in folds:
            if len(f.train) < 50 or len(np.unique(y[f.train])) < 2:
                continue
            preds = []
            for fac in self.factories:
                m = fac()
                m.fit(X[f.train], y[f.train], start=start[f.train], end=end[f.train])
                preds.append(m.predict_proba(X[f.test]))
            oof_rows.append(f.test)
            oof_pred.append(np.column_stack(preds))
        self.members_ = [fac() for fac in self.factories]
        for m in self.members_:
            m.fit(X, y, start=start, end=end)
        if oof_rows and len(np.unique(y[np.concatenate(oof_rows)])) == 2:
            Z = _logit(np.vstack(oof_pred))
            self.meta_ = LogisticRegression(C=1.0, max_iter=1000).fit(Z, y[np.concatenate(oof_rows)])
        else:
            self.meta_ = None
        return self

    def member_proba(self, X):
        return np.column_stack([m.predict_proba(X) for m in self.members_])

    def predict_proba(self, X):
        P = self.member_proba(X)
        if self.meta_ is None:
            return P.mean(axis=1)
        return self.meta_.predict_proba(_logit(P))[:, 1]


class RegimeModel:
    """Separate models per regime of a causal feature (column index `col`,
    regime = value > threshold), global model as fallback."""

    def __init__(self, factory: Callable[[], object], col: int, threshold: float, min_rows: int = 300,
                 name: str = "regime"):
        self.factory, self.col, self.threshold, self.min_rows, self.name = factory, col, threshold, min_rows, name

    def fit(self, X, y, *, start, end):
        y = np.asarray(y)
        self.global_ = self.factory().fit(X, y, start=start, end=end)
        reg = X[:, self.col] > self.threshold
        self.by_ = {}
        for r in (False, True):
            m = reg == r
            if m.sum() >= self.min_rows and len(np.unique(y[m])) == 2:
                self.by_[r] = self.factory().fit(X[m], y[m], start=np.asarray(start)[m], end=np.asarray(end)[m])
        return self

    def predict_proba(self, X):
        p = self.global_.predict_proba(X)
        reg = X[:, self.col] > self.threshold
        for r, m in self.by_.items():
            sel = reg == r
            if sel.any():
                p[sel] = m.predict_proba(X[sel])
        return p


class BaggedModel:
    """Bootstrap bag of one learner (moving-block bootstrap over time order)."""

    def __init__(self, name: str, n: int = 5, seed: int = 0, block: int = 50):
        self.base_name, self.n, self.seed, self.block = name, n, seed, block
        self.name = "bag_%s" % name

    def fit(self, X, y, *, start, end):
        from research.common.rng import stream
        rng = stream(self.seed, "bag", self.base_name)
        n = len(y)
        self.members_ = []
        for b in range(self.n):
            starts = rng.integers(0, max(1, n - self.block), size=int(np.ceil(n / self.block)))
            idx = np.sort(np.concatenate([np.arange(s, min(s + self.block, n)) for s in starts]))[:n]
            m = make(self.base_name, seed=child_seed(self.seed, "bag", str(b)))
            m.fit(X[idx], np.asarray(y)[idx], start=np.asarray(start)[idx], end=np.asarray(end)[idx])
            self.members_.append(m)
        return self

    def member_proba(self, X):
        return np.column_stack([m.predict_proba(X) for m in self.members_])

    def predict_proba(self, X):
        return self.member_proba(X).mean(axis=1)


class ColumnSubset:
    """Restrict a model to a subset of feature columns (e.g. regime-blind variants)."""

    def __init__(self, base, keep: Sequence[int], name: str):
        self.base, self.keep, self.name = base, np.asarray(keep, dtype=int), name

    def fit(self, X, y, **kw):
        self.base.fit(X[:, self.keep], y, **kw)
        return self

    def predict_proba(self, X):
        return self.base.predict_proba(X[:, self.keep])


def factories(names: Sequence[str], seed: int, *, calibrate: Optional[str] = "isotonic",
              regime_col: Optional[int] = None, context_cols: Sequence[int] = (),
              n_features: Optional[int] = None) -> Dict[str, Callable[[], object]]:
    """Named model factories for the harness, skipping unavailable optional libraries.

    Special names: 'stack' (stacked hgb+xgb+logistic, calibrated members),
    'mean_ens' (mean of calibrated hgb, xgb, logistic), 'regime_hgb'
    (RegimeModel over calibrated hgb), 'bag_hgb' (bagged hgb), and any
    '<name>_noctx' (the same model without the market-context columns).
    """
    out: Dict[str, Callable[[], object]] = {}
    cal = (lambda m: Calibrated(m, calibrate)) if calibrate else (lambda m: m)
    for n in names:
        if n.endswith("_noctx"):
            if n_features is None or not len(context_cols):
                raise ValueError("%s needs n_features and context_cols" % n)
            inner = factories([n[:-6]], seed, calibrate=calibrate, regime_col=regime_col)
            if not inner:
                continue
            keep = [i for i in range(n_features) if i not in set(context_cols)]
            fac = next(iter(inner.values()))
            out[n] = (lambda fac=fac, n=n, keep=keep: ColumnSubset(fac(), keep, n))
        elif n in ("prior", "logistic"):
            out[n] = (lambda n=n: make(n, seed))
        elif n == "stack":
            out[n] = lambda: StackedEnsemble([lambda: cal(make("hgb", seed)), lambda: cal(make("xgb", seed)),
                                              lambda: make("logistic", seed)])
        elif n == "mean_ens":
            out[n] = lambda: MeanEnsemble([cal(make("hgb", seed)), cal(make("xgb", seed)),
                                           cal(make("logistic", seed))])
        elif n == "regime_hgb":
            if regime_col is None:
                raise ValueError("regime_hgb needs regime_col")
            out[n] = lambda: RegimeModel(lambda: cal(make("hgb", seed)), regime_col, 0.7, name="regime_hgb")
        elif n == "bag_hgb":
            out[n] = lambda: cal(BaggedModel("hgb", n=5, seed=seed))
        elif available(n):
            out[n] = (lambda n=n: cal(make(n, seed)))
    return out


__all__ = ["available", "Prior", "SK", "make", "Calibrated", "MeanEnsemble", "StackedEnsemble", "RegimeModel",
           "BaggedModel", "ColumnSubset", "factories"]
