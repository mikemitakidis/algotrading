"""research.mllab.uncertainty — distribution-free uncertainty and abstention.

* ``SplitConformal`` — class-conditional (Mondrian) split conformal
  prediction sets for a binary classifier. A set {1} means "confident win",
  {0} "confident loss", {0, 1} "don't know" -> abstain. Coverage holds
  per class at 1 - alpha for exchangeable data (Vovk et al.; Angelopoulos &
  Bates, 2021). Markets are not exchangeable, so coverage is MEASURED out of
  sample and reported, never assumed.
* ``AdaptiveConformal`` — Gibbs & Candès (2021) online update of the
  miscoverage level so long-run coverage tracks the target under drift.
* ``ensemble_dispersion`` — std of member probabilities (epistemic signal).
* ``risk_coverage`` — selective-prediction curve: performance of the most
  confident fraction of decisions, and its area (AURC-style summary).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np


def _q(scores: np.ndarray, alpha: float) -> float:
    n = len(scores)
    if n == 0:
        return 1.0
    k = int(np.ceil((n + 1) * (1 - alpha)))
    if k > n:
        return 1.0
    return float(np.sort(scores)[k - 1])


@dataclass
class SplitConformal:
    alpha: float = 0.2

    def fit(self, p_cal: np.ndarray, y_cal: np.ndarray) -> "SplitConformal":
        p_cal, y_cal = np.asarray(p_cal, float), np.asarray(y_cal).astype(int)
        # Nonconformity of the true class: 1 - P(true class).
        s1 = 1.0 - p_cal[y_cal == 1]
        s0 = p_cal[y_cal == 0]
        self.q1_, self.q0_ = _q(s1, self.alpha), _q(s0, self.alpha)
        self.n_cal_ = len(y_cal)
        return self

    def sets(self, p: np.ndarray) -> np.ndarray:
        """(n, 2) boolean: column 0 = 'loss' in set, column 1 = 'win' in set."""
        p = np.asarray(p, float)
        return np.column_stack([p <= self.q0_, 1.0 - p <= self.q1_])

    def decision(self, p: np.ndarray) -> np.ndarray:
        """+1 confident win (set == {1}), 0 abstain / confident loss."""
        s = self.sets(p)
        return (s[:, 1] & ~s[:, 0]).astype(int)

    def coverage(self, p: np.ndarray, y: np.ndarray) -> Dict[str, float]:
        s = self.sets(p)
        y = np.asarray(y).astype(int)
        cov = s[np.arange(len(y)), y]
        single = s.sum(axis=1) == 1
        return {"coverage": float(cov.mean()) if len(y) else float("nan"),
                "coverage_win": float(cov[y == 1].mean()) if (y == 1).any() else float("nan"),
                "coverage_loss": float(cov[y == 0].mean()) if (y == 0).any() else float("nan"),
                "share_singleton": float(single.mean()) if len(y) else float("nan"),
                "share_empty": float((s.sum(axis=1) == 0).mean()) if len(y) else float("nan")}


class AdaptiveConformal:
    """Online miscoverage tracking (ACI): alpha_{t+1} = alpha_t + gamma * (alpha - err_t)."""

    def __init__(self, alpha: float = 0.2, gamma: float = 0.01, window: int = 500):
        self.target, self.gamma, self.window = alpha, gamma, window
        self.alpha_t = alpha
        self.scores: List[float] = []
        self.errs: List[int] = []

    def interval_q(self) -> float:
        a = float(np.clip(self.alpha_t, 1e-3, 0.999))
        return _q(np.asarray(self.scores[-self.window:]), a) if self.scores else 1.0

    def update(self, score: float) -> bool:
        """Feed the realised nonconformity score of one prediction; returns whether it was covered."""
        covered = score <= self.interval_q()
        err = 0 if covered else 1
        self.errs.append(err)
        self.alpha_t += self.gamma * (self.target - err)
        self.scores.append(float(score))
        return covered


def ensemble_dispersion(member_p: np.ndarray) -> np.ndarray:
    return np.asarray(member_p, float).std(axis=1)


def risk_coverage(confidence: np.ndarray, value: np.ndarray, grid=(0.1, 0.2, 0.3, 0.5, 0.7, 1.0)) -> Dict:
    """Mean `value` (e.g. net R or correctness) of the top-`c` most confident rows."""
    order = np.argsort(-np.asarray(confidence, float), kind="mergesort")
    v = np.asarray(value, float)[order]
    out = []
    for c in grid:
        k = max(1, int(round(c * len(v))))
        out.append((float(c), float(v[:k].mean()), k))
    csum = np.cumsum(v) / np.arange(1, len(v) + 1)
    return {"curve": out, "area": float(csum.mean()) if len(v) else float("nan")}


__all__ = ["SplitConformal", "AdaptiveConformal", "ensemble_dispersion", "risk_coverage"]
