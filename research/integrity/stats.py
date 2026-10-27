"""research.integrity.stats — Sharpe inference under multiple testing.

Implements, with references:

* Probabilistic Sharpe Ratio (Bailey & López de Prado, 2012).
* Expected maximum Sharpe of N independent null trials and the Deflated
  Sharpe Ratio (Bailey & López de Prado, 2014).
* Effective number of independent trials from a trial-return matrix
  (eigenvalue participation ratio and correlation clustering).
* Minimum track-record length.
* Clopper–Pearson binomial intervals (exact).
* Newey–West (HAC) standard error of a mean for overlapping observations.
* Stationary bootstrap (Politis & Romano, 1994) indices and percentile CIs.

All Sharpe ratios here are PER OBSERVATION (not annualised) unless a
function says otherwise; `annualise` converts.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import numpy as np
from scipy import stats as _st

EULER_GAMMA = 0.5772156649015329


def _clean(x) -> np.ndarray:
    a = np.asarray(x, dtype=float).ravel()
    if not np.isfinite(a).all():
        raise ValueError("series contains non-finite values")
    return a


def sharpe(x, ddof: int = 1) -> float:
    a = _clean(x)
    if len(a) < 2:
        return float("nan")
    sd = a.std(ddof=ddof)
    return float(a.mean() / sd) if sd > 0 else float("nan")


def annualise(sr_per_obs: float, periods_per_year: float) -> float:
    return float(sr_per_obs * math.sqrt(periods_per_year))


def moments(x) -> Tuple[float, float]:
    """(skewness, kurtosis) with kurtosis NON-excess (normal = 3)."""
    a = _clean(x)
    return float(_st.skew(a, bias=False)), float(_st.kurtosis(a, fisher=False, bias=False))


def sr_variance(sr: float, n: int, skew: float = 0.0, kurt: float = 3.0) -> float:
    """Asymptotic variance of the per-observation Sharpe estimator."""
    if n < 2:
        raise ValueError("need n >= 2")
    return float((1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr) / (n - 1))


def probabilistic_sharpe_ratio(sr: float, n: int, sr_benchmark: float = 0.0,
                               skew: float = 0.0, kurt: float = 3.0) -> float:
    """P(true SR > sr_benchmark | observed sr over n observations)."""
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return float("nan")
    z = (sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom)
    return float(_st.norm.cdf(z))


def expected_max_sharpe(n_trials: float, sr_variance_across_trials: float) -> float:
    """E[max SR] of `n_trials` independent null trials whose SR estimates
    have the given variance (Bailey & López de Prado, 2014, eq. for SR0)."""
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if n_trials == 1:
        return 0.0
    z1 = _st.norm.ppf(1.0 - 1.0 / n_trials)
    z2 = _st.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(math.sqrt(sr_variance_across_trials) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2))


@dataclass(frozen=True)
class DSRResult:
    sr: float                 # observed per-observation Sharpe of the selected trial
    n_obs: int
    n_trials: float           # (effective) number of trials searched
    sr0: float                # expected max Sharpe under the null
    dsr: float                # P(true SR > sr0)
    psr0: float               # PSR against 0 (no multiplicity correction)
    skew: float
    kurt: float


def deflated_sharpe_ratio(x, n_trials: float, sr_variance_across_trials: Optional[float] = None) -> DSRResult:
    """Deflated Sharpe Ratio of the series `x` selected from `n_trials` trials.

    If the cross-trial Sharpe variance is unknown, the null sampling variance
    of a Sharpe estimate over len(x) observations (≈ 1/(n-1)) is used, which
    corresponds to independent null trials of the same length.
    """
    a = _clean(x)
    n = len(a)
    sr = sharpe(a)
    sk, ku = moments(a)
    v = sr_variance_across_trials if sr_variance_across_trials is not None else 1.0 / (n - 1)
    sr0 = expected_max_sharpe(n_trials, v)
    return DSRResult(sr=sr, n_obs=n, n_trials=float(n_trials), sr0=sr0,
                     dsr=probabilistic_sharpe_ratio(sr, n, sr0, sk, ku),
                     psr0=probabilistic_sharpe_ratio(sr, n, 0.0, sk, ku), skew=sk, kurt=ku)


def min_track_record_length(sr: float, sr_benchmark: float = 0.0, skew: float = 0.0,
                            kurt: float = 3.0, confidence: float = 0.95) -> float:
    """Observations needed for PSR(sr_benchmark) to reach `confidence`."""
    if sr <= sr_benchmark:
        return float("inf")
    z = _st.norm.ppf(confidence)
    return float(1.0 + (1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr) * (z / (sr - sr_benchmark)) ** 2)


# ── effective number of trials ─────────────────────────────────────────

def effective_trials_eigen(returns_matrix) -> float:
    """Participation ratio of the trial correlation spectrum: (Σλ)² / Σλ².

    1 for perfectly correlated trials, N for uncorrelated trials.
    """
    m = np.asarray(returns_matrix, dtype=float)
    if m.ndim != 2 or m.shape[1] < 1:
        raise ValueError("returns_matrix must be (n_obs, n_trials)")
    if m.shape[1] == 1:
        return 1.0
    c = np.corrcoef(m, rowvar=False)
    c = np.nan_to_num(c, nan=0.0)
    np.fill_diagonal(c, 1.0)
    lam = np.clip(np.linalg.eigvalsh(c), 0.0, None)
    return float(lam.sum() ** 2 / (lam ** 2).sum())


def effective_trials_cluster(returns_matrix, max_corr_distance: float = 0.5) -> int:
    """Number of clusters of trials under average-linkage clustering on the
    correlation distance sqrt((1 - rho) / 2); trials closer than the threshold
    count as one trial."""
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform
    m = np.asarray(returns_matrix, dtype=float)
    if m.shape[1] == 1:
        return 1
    c = np.nan_to_num(np.corrcoef(m, rowvar=False), nan=0.0)
    d = np.sqrt(np.clip((1.0 - c) / 2.0, 0.0, 1.0))
    np.fill_diagonal(d, 0.0)
    z = linkage(squareform(d, checks=False), method="average")
    return int(fcluster(z, t=max_corr_distance, criterion="distance").max())


# ── binomial, HAC, bootstrap ───────────────────────────────────────────

def clopper_pearson(k: int, n: int, alpha: float = 0.05, one_sided_upper: bool = False) -> Tuple[float, float]:
    """Exact binomial interval for k successes in n trials.

    one_sided_upper=True returns (0, upper) with P(p <= upper) >= 1 - alpha.
    """
    if n <= 0 or k < 0 or k > n:
        raise ValueError("need 0 <= k <= n and n > 0")
    if one_sided_upper:
        hi = 1.0 if k == n else float(_st.beta.ppf(1.0 - alpha, k + 1, n - k))
        return 0.0, hi
    lo = 0.0 if k == 0 else float(_st.beta.ppf(alpha / 2.0, k, n - k + 1))
    hi = 1.0 if k == n else float(_st.beta.ppf(1.0 - alpha / 2.0, k + 1, n - k))
    return lo, hi


def newey_west_se(x, lags: Optional[int] = None) -> float:
    """HAC standard error of the mean (Bartlett kernel)."""
    a = _clean(x)
    n = len(a)
    if n < 2:
        return float("nan")
    if lags is None:
        lags = int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    lags = max(0, min(int(lags), n - 1))
    e = a - a.mean()
    v = float(e @ e) / n
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)
        v += 2.0 * w * float(e[lag:] @ e[:-lag]) / n
    return float(math.sqrt(max(v, 0.0) / n))


def stationary_bootstrap_indices(n: int, mean_block: float, rng: np.random.Generator,
                                 n_boot: int = 1) -> np.ndarray:
    """(n_boot, n) index matrix of stationary-bootstrap resamples."""
    if n < 1 or mean_block < 1:
        raise ValueError("need n >= 1 and mean_block >= 1")
    p = 1.0 / mean_block
    idx = np.empty((n_boot, n), dtype=np.int64)
    idx[:, 0] = rng.integers(0, n, size=n_boot)
    jumps = rng.random((n_boot, n)) < p
    starts = rng.integers(0, n, size=(n_boot, n))
    for t in range(1, n):
        idx[:, t] = np.where(jumps[:, t], starts[:, t], (idx[:, t - 1] + 1) % n)
    return idx


def bootstrap_ci(x, stat: Callable[[np.ndarray], float], rng: np.random.Generator, *,
                 n_boot: int = 2000, mean_block: float = 1.0, level: float = 0.95) -> Tuple[float, float, float]:
    """(estimate, lower, upper) percentile CI; mean_block > 1 uses the
    stationary bootstrap (for autocorrelated / overlapping observations)."""
    a = _clean(x)
    est = float(stat(a))
    if mean_block <= 1.0:
        idx = rng.integers(0, len(a), size=(n_boot, len(a)))
    else:
        idx = stationary_bootstrap_indices(len(a), mean_block, rng, n_boot)
    vals = np.array([stat(a[i]) for i in idx], dtype=float)
    q = (1.0 - level) / 2.0
    return est, float(np.nanquantile(vals, q)), float(np.nanquantile(vals, 1.0 - q))


def mean_test(x, lags: Optional[int] = None, mu0: float = 0.0) -> Tuple[float, float, float]:
    """(mean, HAC t-stat, one-sided p-value for mean > mu0)."""
    a = _clean(x)
    se = newey_west_se(a, lags)
    t = (a.mean() - mu0) / se if se > 0 else float("nan")
    return float(a.mean()), float(t), float(_st.norm.sf(t)) if np.isfinite(t) else float("nan")


def max_drawdown(x) -> float:
    """Maximum drawdown of the cumulative sum of `x` (additive units, e.g. R)."""
    c = np.cumsum(_clean(x))
    peak = np.maximum.accumulate(np.concatenate([[0.0], c]))[1:]
    return float(np.max(peak - c)) if len(c) else 0.0


def expected_shortfall(x, level: float = 0.95) -> float:
    """Mean of the worst (1 - level) share of observations (a loss is negative)."""
    a = np.sort(_clean(x))
    k = max(1, int(math.ceil((1.0 - level) * len(a) - 1e-9)))
    return float(a[:k].mean())


__all__ = [
    "EULER_GAMMA", "sharpe", "annualise", "moments", "sr_variance",
    "probabilistic_sharpe_ratio", "expected_max_sharpe", "DSRResult",
    "deflated_sharpe_ratio", "min_track_record_length", "effective_trials_eigen",
    "effective_trials_cluster", "clopper_pearson", "newey_west_se",
    "stationary_bootstrap_indices", "bootstrap_ci", "mean_test", "max_drawdown",
    "expected_shortfall",
]
