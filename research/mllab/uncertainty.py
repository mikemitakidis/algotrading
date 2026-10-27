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
* ``online_coverage`` — static split conformal (calibrated once on a warm-up
  block) versus ACI on the same stream, CAUSALLY: a label enters calibration
  only once it has resolved strictly before the decision being made
  (second review, P1: barrier labels resolve days after the decision, and
  feeding them back at decision time leaks future outcomes).
* ``dispersion_report`` — does ensemble disagreement carry information?
  Rank correlation with per-row log loss, log loss by dispersion tercile, and
  the net R of taken trades kept at low dispersion.
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

    def resolve(self, score: float, q_at_decision: float) -> bool:
        """Delayed feedback: the label of a past prediction has just become known. Its error is judged against
        the quantile that was actually used at ITS decision time."""
        covered = score <= q_at_decision
        err = 0 if covered else 1
        self.errs.append(err)
        self.alpha_t += self.gamma * (self.target - err)
        self.scores.append(float(score))
        return covered

    def update(self, score: float) -> bool:
        """Label-INSTANT feedback (the label is known before the next decision). For overlapping or delayed
        labels use ``resolve`` via ``online_coverage``; calling this at decision time leaks the future."""
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


def _binary_scores(p, y):
    p, y = np.asarray(p, float), np.asarray(y).astype(int)
    return np.where(y == 1, 1.0 - p, p)


def _set_size(p, q):
    p = np.asarray(p, float)
    return (1.0 - p <= q).astype(int) + (p <= q).astype(int)


def online_coverage(p, y, block, decision_ts, resolve_ts, *, alpha: float = 0.2, gamma: float = 0.01,
                    window: int = 500, warmup_block: int = 0, return_q: bool = False) -> Dict:
    """Causal static-vs-ACI coverage. Rows are processed in decision order. A row's label (and so its
    nonconformity score) becomes usable only when ``resolve_ts < decision_ts`` of the decision being made.

    * Static split conformal is calibrated once, on warm-up rows whose labels resolved strictly before the
      first evaluated decision.
    * ACI starts from the same resolved scores; afterwards each pending label is released when it resolves,
      and its coverage error is judged against the quantile used at its own decision time.
    * Coverage is scored after the fact for every evaluated row (evaluation only, never fed back early).
    Score = 1 - P(true class) (binary)."""
    p, y, block = np.asarray(p, float), np.asarray(y).astype(int), np.asarray(block)
    dts, rts = np.asarray(decision_ts, dtype=np.int64), np.asarray(resolve_ts, dtype=np.int64)
    if not (len(p) == len(y) == len(block) == len(dts) == len(rts)):
        raise ValueError("p, y, block, decision_ts and resolve_ts must have equal length")
    if (rts < dts).any():
        raise ValueError("a label cannot resolve before its decision")
    order = np.argsort(dts, kind="mergesort")
    p, y, block, dts, rts = p[order], y[order], block[order], dts[order], rts[order]
    s = _binary_scores(p, y)
    warm = np.flatnonzero(block == warmup_block)
    later = np.flatnonzero(block > warmup_block)
    if not len(warm) or not len(later):
        raise ValueError("need warm-up rows and later rows")
    t0 = dts[later[0]]
    known = warm[rts[warm] < t0]
    if not len(known):
        raise ValueError("no warm-up label resolved before the first evaluated decision")
    q_static = _q(s[known], alpha)
    aci = AdaptiveConformal(alpha=alpha, gamma=gamma, window=window)
    aci.scores = list(s[known])
    pending = [(int(rts[j]), 0, int(j), float("nan")) for j in warm if rts[j] >= t0]   # warm: score only
    import heapq
    heapq.heapify(pending)
    q_used = np.full(len(p), np.nan)
    lags = rts[later] - dts[later]
    for i in later:
        while pending and pending[0][0] < dts[i]:
            _, kind, j, qj = heapq.heappop(pending)
            if kind == 0:
                aci.scores.append(float(s[j]))
            else:
                aci.resolve(float(s[j]), qj)
        q_used[i] = aci.interval_q()
        heapq.heappush(pending, (int(rts[i]), 1, int(i), float(q_used[i])))
    cov_a = s[later] <= q_used[later]
    size_a = _set_size(p[later], q_used[later])
    cov_s = s[later] <= q_static
    size_s = _set_size(p[later], q_static)
    per = []
    for b in np.unique(block[later]):
        m = block[later] == b
        per.append({"block": int(b), "n": int(m.sum()), "static": float(cov_s[m].mean()),
                    "aci": float(cov_a[m].mean())})
    out = {"target": 1.0 - alpha, "n": int(len(later)), "static_coverage": float(cov_s.mean()),
           "aci_coverage": float(cov_a.mean()), "static_mean_set_size": float(size_s.mean()),
           "aci_mean_set_size": float(size_a.mean()), "static_q": float(q_static),
           "aci_final_alpha": float(aci.alpha_t), "warmup_labels_resolved": int(len(known)),
           "warmup_labels_pending": int(len(warm) - len(known)), "median_label_lag_ns": float(np.median(lags)),
           "feedback": "causal: labels used only after resolve_ts < decision_ts", "per_block": per}
    if return_q:
        inv = np.empty_like(order)
        inv[order] = np.arange(len(order))
        out["q_used"] = q_used[inv]                      # in the caller's original row order
    return out


def dispersion_report(member_p, p, y, r_net, take) -> Dict:
    """Is ensemble disagreement informative? Rows with any NaN member are dropped."""
    from scipy.stats import spearmanr
    M = np.asarray(member_p, float)
    ok = np.isfinite(M).all(axis=1) & np.isfinite(np.asarray(p, float))
    M, p, y = M[ok], np.asarray(p, float)[ok], np.asarray(y, float)[ok]
    r_net, take = np.asarray(r_net, float)[ok], np.asarray(take, bool)[ok]
    d = ensemble_dispersion(M)
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    ll = -(y * np.log(pc) + (1 - y) * np.log(1 - pc))
    rho, pval = spearmanr(d, ll) if np.ptp(d) > 0 else (float("nan"), float("nan"))
    terc = np.quantile(d, [1 / 3, 2 / 3]) if len(d) else np.array([np.nan, np.nan])
    groups = [d <= terc[0], (d > terc[0]) & (d <= terc[1]), d > terc[1]]
    by_t = [{"tercile": k + 1, "n": int(g.sum()), "mean_dispersion": float(d[g].mean()) if g.any() else float("nan"),
             "log_loss": float(ll[g].mean()) if g.any() else float("nan")} for k, g in enumerate(groups)]
    rc = risk_coverage(-d[take], r_net[take], grid=(0.2, 0.5, 1.0)) if take.any() else {"curve": [], "area": float("nan")}
    return {"n": int(len(d)), "members": int(M.shape[1]) if M.ndim == 2 else 0,
            "mean_dispersion": float(d.mean()) if len(d) else float("nan"),
            "spearman_dispersion_vs_logloss": float(rho), "spearman_p": float(pval),
            "by_tercile": by_t, "taken_trades_low_dispersion_first": rc["curve"]}


__all__ = ["SplitConformal", "AdaptiveConformal", "ensemble_dispersion", "risk_coverage", "online_coverage",
           "dispersion_report"]
