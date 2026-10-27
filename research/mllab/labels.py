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

Observability (comprehensive audit, PR #7, P1/P2). ``barrier_outcomes`` works
on ROW positions. If an expected bar is missing (a vendor gap, or a bar the
data-quality filter removed) the next row is silently treated as the next
bar, and a stop or target hit in the missing bar is never seen. So
``path_observability`` marks, per row, an invalid observation (non-finite or
inconsistent OHLC, a blocking or unknown quality bit) and a gap step (one or
more expected bars missing before the row). ``build_dataset`` drops every
label whose path (entry bar .. resolution bar) touches either, and every
anchor that is itself invalid. Without an exchange calendar the expectation
is every business day (1D) or contiguous same-day bars (intraday), so a
holiday is treated as a gap: conservative, and stated. Pass the calendar to
distinguish genuine closures.
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


def path_observability(ts, *, flags=None, ohlc=None, timeframe: str = "1D", tf_minutes: int = 390,
                       calendar=None) -> Dict[str, np.ndarray]:
    """Per-row ``bad_bar`` (invalid or blocked observation) and ``gap_before`` (expected bars missing between
    row k-1 and row k). Only the observation itself and the expected calendar are used, so the mask is
    knowable at each bar."""
    import pandas as pd
    from bot.historical.quality import QF_DUPLICATE_KEPT, QF_OUTLIER_WARNED, QF_ZERO_VOLUME
    t = pd.DatetimeIndex(pd.to_datetime(ts, utc=True))
    n = len(t)
    bad = np.zeros(n, dtype=bool)
    if ohlc is not None:
        o, h, l, c = (np.asarray(x, dtype=float) for x in ohlc)
        with np.errstate(invalid="ignore"):
            ok = (np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & (o > 0) & (l > 0)
                  & (h >= np.maximum(o, c)) & (l <= np.minimum(o, c)))
        bad |= ~ok
    if flags is not None:
        warn = np.int64(QF_ZERO_VOLUME | QF_OUTLIER_WARNED | QF_DUPLICATE_KEPT)
        f = np.nan_to_num(np.asarray(flags, dtype=float), nan=0.0).astype(np.int64)
        bad |= (f & ~warn) != 0
    gap = np.zeros(n, dtype=bool)
    if n > 1:
        if calendar is not None:
            cal = pd.DatetimeIndex(pd.to_datetime(calendar, utc=True))
            pos = cal.get_indexer(t)
            gap[1:] = (np.diff(pos) != 1) | (pos[1:] < 0)
            bad |= pos < 0
        else:
            d = t.tz_convert("UTC").normalize().tz_localize(None).values.astype("datetime64[D]")
            skipped = np.busday_count(d[:-1] + np.timedelta64(1, "D"), d[1:])
            if timeframe == "1D":
                gap[1:] = skipped > 0
            else:
                same = d[1:] == d[:-1]
                step = np.diff(t.asi8) / 6e10
                gap[1:] = (same & (step > tf_minutes)) | (~same & (skipped > 0))
    return {"bad_bar": bad, "gap_before": gap}


def unobserved_paths(out: Dict[str, np.ndarray], obs: Dict[str, np.ndarray]) -> np.ndarray:
    """True for anchor i when the anchor bar is invalid or its path i+1 .. resolve_idx[i] holds an invalid bar
    or a missing expected bar. Rows without a resolution are left to the pending / NaN rules."""
    bad = obs["bad_bar"] | obs["gap_before"]
    cb = np.concatenate([[0], np.cumsum(bad)])
    res = np.asarray(out["resolve_idx"])
    n = len(res)
    i = np.arange(n)
    j = np.where(res >= 0, res, i)
    window_bad = (cb[j + 1] - cb[np.minimum(i + 1, n)]) > 0
    return obs["bad_bar"] | ((res >= 0) & window_bad)


__all__ = ["BarrierSpec", "barrier_outcomes", "path_observability", "unobserved_paths"]
