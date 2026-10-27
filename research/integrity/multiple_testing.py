"""research.integrity.multiple_testing — family-wise and false-discovery control.

Adjusted p-values for Bonferroni, Holm (FWER), Benjamini–Hochberg and
Benjamini–Yekutieli (FDR, the latter valid under arbitrary dependence, as
recommended for factor/strategy searches by Harvey, Liu & Zhu, 2016).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np

METHODS = ("bonferroni", "holm", "bh", "by")


def _p(pvals) -> np.ndarray:
    p = np.asarray(pvals, dtype=float).ravel()
    if p.size == 0:
        raise ValueError("no p-values")
    if not np.isfinite(p).all() or (p < 0).any() or (p > 1).any():
        raise ValueError("p-values must be finite and in [0, 1]")
    return p


def adjust(pvals, method: str = "holm") -> np.ndarray:
    """Adjusted p-values (same order as input)."""
    p = _p(pvals)
    m = p.size
    if method == "bonferroni":
        return np.minimum(p * m, 1.0)
    order = np.argsort(p, kind="mergesort")
    ps = p[order]
    if method == "holm":
        adj = np.maximum.accumulate((m - np.arange(m)) * ps)
    elif method in ("bh", "by"):
        c = 1.0 if method == "bh" else float(np.sum(1.0 / np.arange(1, m + 1)))
        adj = np.minimum.accumulate((c * m / np.arange(1, m + 1) * ps)[::-1])[::-1]
    else:
        raise ValueError("method must be one of %s" % (METHODS,))
    out = np.empty(m)
    out[order] = np.minimum(adj, 1.0)
    return out


@dataclass(frozen=True)
class MultipleTestResult:
    method: str
    alpha: float
    adjusted: Tuple[float, ...]
    rejected: Tuple[bool, ...]

    @property
    def n_rejected(self) -> int:
        return int(sum(self.rejected))


def test(pvals, method: str = "holm", alpha: float = 0.05) -> MultipleTestResult:
    adj = adjust(pvals, method)
    return MultipleTestResult(method=method, alpha=alpha, adjusted=tuple(float(a) for a in adj),
                              rejected=tuple(bool(a <= alpha) for a in adj))


__all__ = ["METHODS", "adjust", "test", "MultipleTestResult"]
