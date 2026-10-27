"""research.mllab.labels — parametric barrier labels for model training.

Same path semantics as the M18 triple-barrier label (entry at the next bar's
open, barriers in ATR multiples from the entry, scan from the entry bar,
time stop after `horizon` bars) with three honesty upgrades that lanes A/C
share in spirit:

* barrier, horizon and cost are parameters (M18 fixes 3/2/50);
* a gap through a barrier at a bar's OPEN resolves at the open;
* a bar containing BOTH barriers is flagged ``ambiguous`` instead of being
  silently booked as a loss. Ambiguous and pending rows are excluded from
  training and evaluation by default.

Parity with ``bot.ml.labels.triple_barrier`` is tested on unambiguous rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import numpy as np


@dataclass(frozen=True)
class BarrierSpec:
    tp_mult: float = 1.0
    sl_mult: float = 1.0
    horizon: int = 5

    def __post_init__(self):
        if not (self.tp_mult > 0 and self.sl_mult > 0 and self.horizon >= 1):
            raise ValueError("tp_mult, sl_mult must be > 0 and horizon >= 1")


def barrier_outcomes(o, h, l, c, atr, spec: BarrierSpec) -> Dict[str, np.ndarray]:
    """Long-side outcome for every anchor bar i (decision at the close of i).

    Returns arrays (length n): y (1 target first, 0 stop or timeout, NaN if not
    labelled), r (gross R multiple; risk = sl_mult * atr), resolve_idx,
    ambiguous (bool), pending (bool), gap (bool).
    """
    o, h, l, c, atr = (np.asarray(x, dtype=float) for x in (o, h, l, c, atr))
    n = len(c)
    y = np.full(n, np.nan)
    r = np.full(n, np.nan)
    res = np.full(n, -1, dtype=np.int64)
    amb = np.zeros(n, dtype=bool)
    pend = np.zeros(n, dtype=bool)
    gap = np.zeros(n, dtype=bool)
    H = spec.horizon
    for i in range(n):
        if i + 1 >= n or not (np.isfinite(atr[i]) and atr[i] > 0):
            pend[i] = i + 1 >= n
            continue
        e = o[i + 1]
        if not (np.isfinite(e) and e > 0):
            continue
        risk = spec.sl_mult * atr[i]
        up, dn = e + spec.tp_mult * atr[i], e - risk
        j1 = min(i + H, n - 1)
        oo, hh, ll = o[i + 1:j1 + 1], h[i + 1:j1 + 1], l[i + 1:j1 + 1]
        gs, gt = oo <= dn, oo >= up
        gs[0] = gt[0] = False                     # the entry fills at the first open
        hs, ht = ll <= dn, hh >= up
        hit = gs | gt | hs | ht
        if hit.any():
            k = int(np.argmax(hit))
            j = i + 1 + k
            res[i] = j
            if gs[k]:
                y[i], r[i], gap[i] = 0.0, (oo[k] - e) / risk, True
            elif gt[k]:
                y[i], r[i], gap[i] = 1.0, (oo[k] - e) / risk, True
            elif hs[k] and ht[k]:
                amb[i] = True
            elif hs[k]:
                y[i], r[i] = 0.0, -1.0
            else:
                y[i], r[i] = 1.0, spec.tp_mult / spec.sl_mult
        elif i + H <= n - 1:
            res[i] = i + H
            y[i], r[i] = 0.0, (c[i + H] - e) / risk
        else:
            pend[i] = True
    return {"y": y, "r": r, "resolve_idx": res, "ambiguous": amb, "pending": pend, "gap": gap}


__all__ = ["BarrierSpec", "barrier_outcomes"]
