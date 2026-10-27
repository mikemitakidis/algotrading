"""research.mllab.audits — independent checks of the lab's own machinery.

* ``parity_with_m18`` — the research barrier label against the production
  M18 triple-barrier label (``bot.ml.labels.triple_barrier``) at the M18
  parameters (TP 3 ATR, SL 2 ATR, 50 bars). On rows both sides resolve,
  without a same-bar tie or an open gap, outcome and resolution bar must be
  identical. The documented differences are counted, not hidden:
  same-bar ties (M18 books a loss, research flags ambiguous), gaps through a
  barrier at the open (research fills at the open), and tail rows M18 leaves
  pending although a barrier was already hit.
* ``leaky_symbol_features`` — a deliberately leaking feature function (the
  next bar's return); the as-of audit must catch it.
* ``harness_leakage`` — brute-force overlap/embargo check of every walk-forward
  fold the harness uses (``research.integrity.splits.leakage_report``).
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from bot.ml.labels import triple_barrier as TB
from research.integrity.splits import leakage_report, walk_forward
from research.mllab.datasets import ATR_COL, EventDataset, symbol_features
from research.mllab.labels import BarrierSpec, barrier_outcomes

M18_SPEC = BarrierSpec(tp_mult=TB.TP_MULT, sl_mult=TB.SL_MULT, horizon=TB.TIMEOUT_BARS)


def parity_with_m18(bars: pd.DataFrame) -> Dict[str, int]:
    atr = symbol_features(bars)[ATR_COL]
    m18 = TB.compute(bars, atr_series=atr)
    lab3 = m18[TB.LABEL_ID_3WAY].to_numpy(dtype=float)
    won = m18[TB.LABEL_ID_WON].to_numpy(dtype=float)
    pend = m18["%s.is_pending" % TB.LABEL_ID_3WAY].to_numpy() == 1
    m18_idx = np.arange(len(bars)) + m18["%s.bars_to_resolution" % TB.LABEL_ID_3WAY].to_numpy().astype(np.int64)
    out = barrier_outcomes(bars["open"], bars["high"], bars["low"], bars["close"], atr, M18_SPEC)
    y, res, amb, gap = out["y"], out["resolve_idx"], out["ambiguous"], out["gap"]
    both = ~pend & (np.isfinite(y) | amb)
    cmp = ~pend & np.isfinite(y) & ~amb & ~gap
    return {
        "rows": int(len(bars)),
        "compared": int(cmp.sum()),
        "outcome_mismatch": int((y[cmp] != won[cmp]).sum()),
        "resolution_bar_mismatch": int((res[cmp] != m18_idx[cmp]).sum()),
        "ambiguous": int((both & amb).sum()),
        "ambiguous_booked_as_stop_by_m18": int((both & amb & (lab3 == -1)).sum()),
        "gap_rows": int((~pend & gap).sum()),
        "gap_rows_same_resolution_bar": int((~pend & gap & (res == m18_idx)).sum()),
        "gap_rows_outcome_differs": int((~pend & gap & np.isfinite(y) & (y != won)).sum()),
        "research_resolved_m18_pending": int((pend & np.isfinite(y)).sum()),
        "m18_resolved_research_unlabelled": int((~pend & ~np.isfinite(y) & ~amb).sum()),
    }


def leaky_symbol_features(bars: pd.DataFrame) -> pd.DataFrame:
    """symbol_features plus a column that peeks at the NEXT bar (negative control)."""
    f = symbol_features(bars)
    c = bars["close"].astype(float)
    f["leak.next_ret"] = np.log(c.shift(-1) / c).to_numpy()
    return f


def harness_leakage(ds: EventDataset, *, n_folds: int, embargo_ns: int, min_train_groups: int = 2) -> Dict:
    folds = walk_forward(ds.start, ds.end, n_folds=n_folds, embargo=embargo_ns, expanding=True,
                         min_train_groups=min_train_groups)
    rows = []
    for j, f in enumerate(folds):
        rep = leakage_report(f.train, f.test, ds.start, ds.end, embargo=embargo_ns)
        rows.append({"fold": j, "train": int(len(f.train)), "test": int(len(f.test)), "purged": int(f.purged),
                     "embargoed": int(f.embargoed), "overlapping_pairs": rep.overlapping_pairs,
                     "embargo_violations": rep.embargo_violations, "shared_indices": rep.shared_indices,
                     "train_resolves_before_test": bool(ds.end[f.train].max() < ds.start[f.test].min())})
    return {"folds": rows, "clean": all(r["overlapping_pairs"] == 0 and r["shared_indices"] == 0 and
                                        r["embargo_violations"] == 0 and r["train_resolves_before_test"]
                                        for r in rows)}


__all__ = ["M18_SPEC", "parity_with_m18", "leaky_symbol_features", "harness_leakage"]
