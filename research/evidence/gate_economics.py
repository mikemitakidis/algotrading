"""research.evidence.gate_economics — what each gate avoided and what it forfeited.

For every gate / Risk Authority reason code, compare the counterfactual net R
of candidates the gate BLOCKED with the candidates that PASSED it, on
definitive labels only, with confidence intervals from a cluster bootstrap
over decision dates (candidates on the same day are not independent).

This is OBSERVATIONAL accounting (canonical B04). Blocked and passed
candidates can differ in many ways besides the gate, so a negative mean for
blocked candidates does not prove the gate causes better results. Ambiguous,
unobserved and unfillable shares are reported per gate so a gate that mostly
blocks unobservable cases is visible.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from research.common.rng import stream

CAVEAT = ("Observational: blocked and passed candidates may differ in ways other than the gate. "
          "Counterfactual R uses modelled fills and is not P&L.")


@dataclass(frozen=True)
class GateRow:
    gate: str
    n_blocked: int
    n_blocked_definitive: int
    share_ambiguous: float
    share_unobserved_or_unfillable: float
    mean_r_blocked: float
    mean_r_passed: float
    diff_passed_minus_blocked: float
    diff_ci_lo: float
    diff_ci_hi: float
    avoided_loss_r: float        # sum of -R over blocked definitive losers
    forfeited_gain_r: float      # sum of R over blocked definitive winners


def _cluster_boot_diff(rb: np.ndarray, db: np.ndarray, rp: np.ndarray, dp: np.ndarray,
                       rng: np.random.Generator, n_boot: int) -> np.ndarray:
    days = np.unique(np.concatenate([db, dp]))
    out = np.empty(n_boot)
    b_by = {d: rb[db == d] for d in days}
    p_by = {d: rp[dp == d] for d in days}
    for i in range(n_boot):
        pick = rng.choice(days, size=len(days), replace=True)
        b = np.concatenate([b_by[d] for d in pick])
        p = np.concatenate([p_by[d] for d in pick])
        out[i] = (p.mean() if len(p) else np.nan) - (b.mean() if len(b) else np.nan)
    return out


def gate_table(labels: pd.DataFrame, *, r_col: str = "r_net", seed: int = 0, n_boot: int = 1000,
               blocked_stages: Sequence[str] = ("blocked_gate", "blocked_risk", "skipped_capacity"),
               passed_stages: Sequence[str] = ("executed", "eligible")) -> List[GateRow]:
    """Rows per reason code. Candidates must carry `reasons` ('|'-joined) and `stage`."""
    lab = labels.copy()
    lab["day"] = pd.to_datetime(lab["decision_ts_utc"], utc=True).dt.floor("D")
    passed = lab[lab["stage"].isin(passed_stages) & (lab["status"] == "definitive")]
    blocked = lab[lab["stage"].isin(blocked_stages)]
    codes = sorted({c for r in blocked["reasons"] for c in str(r).split("|") if c})
    rows = []
    for code in codes:
        sel = blocked[blocked["reasons"].map(lambda r: code in str(r).split("|"))]
        d = sel[sel["status"] == "definitive"]
        rb, rp = d[r_col].to_numpy(float), passed[r_col].to_numpy(float)
        if len(rb) >= 2 and len(rp) >= 2:
            boots = _cluster_boot_diff(rb, d["day"].to_numpy(), rp, passed["day"].to_numpy(),
                                       stream(seed, "gate_economics", code), n_boot)
            lo, hi = np.nanquantile(boots, [0.025, 0.975])
        else:
            lo = hi = float("nan")
        rows.append(GateRow(
            gate=code, n_blocked=int(len(sel)), n_blocked_definitive=int(len(d)),
            share_ambiguous=float((sel["status"] == "ambiguous").mean()) if len(sel) else float("nan"),
            share_unobserved_or_unfillable=float(sel["status"].isin(["unobserved", "unfillable"]).mean()) if len(sel) else float("nan"),
            mean_r_blocked=float(rb.mean()) if len(rb) else float("nan"),
            mean_r_passed=float(rp.mean()) if len(rp) else float("nan"),
            diff_passed_minus_blocked=float(rp.mean() - rb.mean()) if len(rb) and len(rp) else float("nan"),
            diff_ci_lo=float(lo), diff_ci_hi=float(hi),
            avoided_loss_r=float(-rb[rb < 0].sum()), forfeited_gain_r=float(rb[rb > 0].sum())))
    return rows


def to_markdown(rows: List[GateRow]) -> str:
    L = ["| gate | blocked (definitive) | ambiguous | unobserved/unfillable | mean R blocked | mean R passed | "
         "passed − blocked (95 % CI) | avoided loss R | forfeited gain R |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append("| %s | %d (%d) | %.0f %% | %.0f %% | %.3f | %.3f | %.3f (%.3f to %.3f) | %.1f | %.1f |" % (
            r.gate, r.n_blocked, r.n_blocked_definitive, 100 * r.share_ambiguous,
            100 * r.share_unobserved_or_unfillable, r.mean_r_blocked, r.mean_r_passed,
            r.diff_passed_minus_blocked, r.diff_ci_lo, r.diff_ci_hi, r.avoided_loss_r, r.forfeited_gain_r))
    return "\n".join(L) + "\n\n_" + CAVEAT + "_\n"


__all__ = ["CAVEAT", "GateRow", "gate_table", "to_markdown"]
