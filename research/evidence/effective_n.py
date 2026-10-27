"""research.evidence.effective_n — how many INDEPENDENT observations a label set holds.

Raw candidate counts overstate evidence (Issue #2 critique #1): labels
overlap in time within a symbol, and candidates on the same day share market
moves. This module reports, side by side:

* ``n_uniqueness``  — sum of average uniqueness (López de Prado): an event
  that shares every bar of its window with three others counts 1/4;
* ``n_cluster_<dim>`` — the effective N implied by a cluster-robust variance
  of the mean, clustering by decision date, symbol or sector:
  n_eff = s^2 / Var_cluster(mean);
* ``n_twoway``      — two-way clustering by date and symbol
  (Cameron–Gelbach–Miller);
* ``n_effective``   — the most conservative of the above (the headline).

All are capped at the raw N.

FAIL-CLOSED (PR #6 review, P1): a clustering that cannot be estimated
(fewer than two clusters, e.g. one decision day or one sector), a zero or
non-finite cluster variance, or a constant outcome gives NaN — never the raw
N — and ``status`` becomes ``insufficient_evidence`` with the reasons. A NaN
effective N must block any downstream claim or promotion.
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence

import numpy as np
import pandas as pd


def average_uniqueness(symbols: Sequence[str], entry_idx: Sequence[int], exit_idx: Sequence[int]) -> np.ndarray:
    """Per-event average uniqueness within its own symbol's bar timeline."""
    sym = np.asarray(symbols)
    e0 = np.asarray(entry_idx, dtype=np.int64)
    e1 = np.asarray(exit_idx, dtype=np.int64)
    if (e1 < e0).any():
        raise ValueError("exit before entry")
    u = np.zeros(len(sym))
    for s in np.unique(sym):
        m = np.flatnonzero(sym == s)
        lo, hi = int(e0[m].min()), int(e1[m].max())
        conc = np.zeros(hi - lo + 2, dtype=np.int64)
        np.add.at(conc, e0[m] - lo, 1)
        np.add.at(conc, e1[m] - lo + 1, -1)
        conc = np.cumsum(conc)[:-1]
        inv = 1.0 / np.maximum(conc, 1)
        cs = np.concatenate([[0.0], np.cumsum(inv)])
        u[m] = (cs[e1[m] - lo + 1] - cs[e0[m] - lo]) / (e1[m] - e0[m] + 1)
    return u


def cluster_variance_of_mean(x: np.ndarray, groups: np.ndarray) -> float:
    """Cluster-robust variance of the sample mean with the G/(G-1) correction."""
    e = x - x.mean()
    df = pd.DataFrame({"g": groups, "e": e})
    sums = df.groupby("g", sort=False)["e"].sum().to_numpy()
    G = len(sums)
    if G < 2:
        return float("nan")
    return float((G / (G - 1.0)) * np.sum(sums ** 2) / len(x) ** 2)


def _n_from_var(x: np.ndarray, v: float) -> float:
    """Effective N implied by a variance of the mean; NaN when not estimable."""
    s2 = x.var(ddof=1)
    if not (np.isfinite(v) and v > 0 and np.isfinite(s2) and s2 > 0):
        return float("nan")
    return float(np.clip(s2 / v, 1.0, len(x)))


def effective_n(values, *, dates=None, symbols=None, sectors=None, entry_idx=None, exit_idx=None) -> Dict:
    x = np.asarray(values, dtype=float)
    ok = np.isfinite(x)
    x = x[ok]
    n = len(x)
    out: Dict = {"n_raw": float(n), "status": "ok", "reasons": []}
    if n < 3:
        out.update(n_effective=float("nan"), status="insufficient_evidence", reasons=["fewer than 3 observations"])
        return out
    if not np.ptp(x) > 0:
        out.update(n_effective=float("nan"), status="insufficient_evidence", reasons=["constant outcome"])
        return out
    cands = []
    if symbols is not None and entry_idx is not None and exit_idx is not None:
        u = average_uniqueness(np.asarray(symbols)[ok], np.asarray(entry_idx)[ok], np.asarray(exit_idx)[ok])
        out["n_uniqueness"] = float(u.sum())
        cands.append(out["n_uniqueness"])
    for name, g in (("date", dates), ("symbol", symbols), ("sector", sectors)):
        if g is not None:
            gg = np.asarray(g)[ok]
            v = _n_from_var(x, cluster_variance_of_mean(x, gg))
            out["n_cluster_" + name] = v
            if not np.isfinite(v):
                out["reasons"].append("cannot estimate %s clustering (%d cluster(s))" % (name, len(np.unique(gg))))
            cands.append(v)
    if dates is not None and symbols is not None:
        d, s = np.asarray(dates)[ok], np.asarray(symbols)[ok]
        inter = np.array(["%s|%s" % (a, b) for a, b in zip(d, s)])
        v = (cluster_variance_of_mean(x, d) + cluster_variance_of_mean(x, s)
             - cluster_variance_of_mean(x, inter))
        out["n_twoway"] = _n_from_var(x, v)
        if not np.isfinite(out["n_twoway"]):
            out["reasons"].append("cannot estimate two-way clustering")
        cands.append(out["n_twoway"])
    if not cands:
        out.update(n_effective=float("nan"), status="insufficient_evidence",
                   reasons=["no clustering or overlap information supplied"])
    elif not all(np.isfinite(c) for c in cands):
        out.update(n_effective=float("nan"), status="insufficient_evidence")
    else:
        out["n_effective"] = float(min(cands))
    return out


__all__ = ["average_uniqueness", "cluster_variance_of_mean", "effective_n"]
