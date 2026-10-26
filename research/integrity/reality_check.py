"""research.integrity.reality_check — data-snooping tests for best-of-N searches.

* White (2000) Reality Check: H0 "no strategy beats the benchmark".
* Hansen (2005) Superior Predictive Ability, consistent version (SPA_c):
  studentised and recentred, so poor strategies do not dilute power.

Input is a (T, K) matrix of performance differentials versus the benchmark
(e.g. strategy return minus benchmark return, or per-period net R when the
benchmark is "no trade" = 0). Both use the stationary bootstrap.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from research.integrity.stats import stationary_bootstrap_indices


@dataclass(frozen=True)
class SnoopingResult:
    test: str
    statistic: float
    p_value: float
    n_strategies: int
    n_obs: int
    n_boot: int
    best_index: int


def _prep(d) -> np.ndarray:
    a = np.asarray(d, dtype=float)
    if a.ndim == 1:
        a = a[:, None]
    if a.ndim != 2 or a.shape[0] < 10:
        raise ValueError("need a (T>=10, K) differential matrix")
    if not np.isfinite(a).all():
        raise ValueError("differentials contain non-finite values")
    return a


def white_reality_check(d, rng: np.random.Generator, n_boot: int = 2000,
                        mean_block: float = 5.0) -> SnoopingResult:
    a = _prep(d)
    T, K = a.shape
    fbar = a.mean(axis=0)
    v = math.sqrt(T) * fbar.max()
    idx = stationary_bootstrap_indices(T, mean_block, rng, n_boot)
    boot = np.empty(n_boot)
    for b in range(n_boot):
        boot[b] = math.sqrt(T) * (a[idx[b]].mean(axis=0) - fbar).max()
    return SnoopingResult("white_rc", float(v), float(np.mean(boot >= v)), K, T, n_boot, int(np.argmax(fbar)))


def hansen_spa(d, rng: np.random.Generator, n_boot: int = 2000,
               mean_block: float = 5.0, variant: str = "c") -> SnoopingResult:
    """variant: "c" consistent (default), "u" upper (no recentring, least
    favourable configuration, conservative), "l" lower (liberal)."""
    if variant not in ("c", "u", "l"):
        raise ValueError("variant must be 'c', 'u' or 'l'")
    a = _prep(d)
    T, K = a.shape
    fbar = a.mean(axis=0)
    idx = stationary_bootstrap_indices(T, mean_block, rng, n_boot)
    bmeans = np.stack([a[i].mean(axis=0) for i in idx])          # (n_boot, K)
    omega = np.sqrt(T) * bmeans.std(axis=0, ddof=1)
    omega = np.where(omega > 0, omega, np.inf)
    t_spa = max(float(np.max(math.sqrt(T) * fbar / omega)), 0.0)
    thresh = -np.sqrt((omega ** 2 / T) * 2.0 * math.log(max(math.log(T), 1.0 + 1e-12)))
    if variant == "c":
        g = np.where(fbar >= thresh, fbar, 0.0)      # Hansen's g_c: recentre only non-poor models
    elif variant == "u":
        g = fbar
    else:
        g = np.maximum(fbar, 0.0)
    z = math.sqrt(T) * (bmeans - g) / omega
    boot = np.maximum(z.max(axis=1), 0.0)
    return SnoopingResult("hansen_spa_" + variant, t_spa, float(np.mean(boot >= t_spa)) if t_spa > 0 else 1.0,
                          K, T, n_boot, int(np.argmax(fbar / omega)))


__all__ = ["SnoopingResult", "white_reality_check", "hansen_spa"]
