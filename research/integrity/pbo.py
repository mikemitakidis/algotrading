"""research.integrity.pbo — Probability of Backtest Overfitting via CSCV.

Bailey, Borwein, López de Prado & Zhu (2014/2017). Given a T x N matrix of
per-period returns for N strategy configurations, split the rows into S
contiguous blocks; for every choice of S/2 blocks as in-sample, select the
in-sample best configuration and record its out-of-sample relative rank.
PBO is the share of splits where the in-sample winner ranks at or below the
out-of-sample median (logit <= 0).
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Callable, Tuple

import numpy as np


def _sharpe_cols(m: np.ndarray) -> np.ndarray:
    sd = m.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(sd > 0, m.mean(axis=0) / sd, -np.inf)
    return s


@dataclass(frozen=True)
class PBOResult:
    pbo: float
    n_splits: int
    n_strategies: int
    logits: Tuple[float, ...]
    is_best_oos_perf: Tuple[float, ...]     # OOS performance of the IS winner per split
    degradation_slope: float                 # OLS slope of OOS on IS performance of winners
    prob_oos_loss: float                     # share of splits where the IS winner loses OOS


def pbo(returns_matrix, n_blocks: int = 16,
        metric: Callable[[np.ndarray], np.ndarray] = _sharpe_cols) -> PBOResult:
    m = np.asarray(returns_matrix, dtype=float)
    if m.ndim != 2 or m.shape[1] < 2:
        raise ValueError("returns_matrix must be (T, N) with N >= 2")
    if n_blocks < 2 or n_blocks % 2:
        raise ValueError("n_blocks must be an even number >= 2")
    if not np.isfinite(m).all():
        raise ValueError("returns_matrix contains non-finite values")
    T, N = m.shape
    rows = (T // n_blocks) * n_blocks
    if rows < 2 * n_blocks:
        raise ValueError("not enough rows for %d blocks" % n_blocks)
    blocks = np.split(m[:rows], n_blocks)
    logits, oos_perf, is_perf = [], [], []
    for is_set in combinations(range(n_blocks), n_blocks // 2):
        is_mask = np.zeros(n_blocks, dtype=bool)
        is_mask[list(is_set)] = True
        ins = np.vstack([blocks[i] for i in range(n_blocks) if is_mask[i]])
        oos = np.vstack([blocks[i] for i in range(n_blocks) if not is_mask[i]])
        r_is, r_oos = metric(ins), metric(oos)
        best = int(np.argmax(r_is))
        rank = 1 + np.sum(r_oos < r_oos[best]) + 0.5 * (np.sum(r_oos == r_oos[best]) - 1)
        w = rank / (N + 1.0)
        logits.append(float(np.log(w / (1.0 - w))))
        oos_perf.append(float(r_oos[best]))
        is_perf.append(float(r_is[best]))
    lg = np.asarray(logits)
    x, y = np.asarray(is_perf), np.asarray(oos_perf)
    slope = float(np.polyfit(x, y, 1)[0]) if np.ptp(x) > 0 else float("nan")
    return PBOResult(pbo=float(np.mean(lg <= 0.0)), n_splits=len(lg), n_strategies=N,
                     logits=tuple(logits), is_best_oos_perf=tuple(oos_perf),
                     degradation_slope=slope, prob_oos_loss=float(np.mean(y <= 0.0)))


__all__ = ["pbo", "PBOResult"]
