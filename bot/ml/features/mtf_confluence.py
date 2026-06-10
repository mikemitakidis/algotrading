"""bot.ml.features.mtf_confluence — multi-timeframe confluence features.

All features here are leak_class="safe" — multi-TF snapshots from
MultiTimeframeContext are look-ahead-safe by construction (every
snapshot is the most-recent-bar-AT-OR-BEFORE the anchor).

Features (5):
  available_tf_count    int8   number of TFs with valid bars at the
                                 anchor (1..N where N = total TFs).
  tf_15m_present        int8   1 if the 15m TF has a snapshot bar at
                                 the anchor else 0.
  tf_1h_present         int8   same for 1H.
  tf_4h_present         int8   same for 4H.
  tf_1d_present         int8   same for 1D.

This group intentionally produces NO scoring or signal logic — those
features belong to scanner_replica, which uses the same context.
mtf_confluence is the LOWER-LEVEL group that exposes the raw
multi-TF availability picture so ML can learn:
    "signals fire ONLY when 1D and 4H are both present"
    "models trained on these anchors might be unfair if 1D is missing"
etc., without baking scanner_replica's specific score formulas into
the feature surface.

DEPENDENCY: bot.backtesting.mtf_context (M17.B). This is allowed by
the AST guard (not on the forbidden prefix list — only bot.backtesting
.execution / portfolio / runner are forbidden).
"""
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

# M17.B surface — allowed per the AST guard (no forbidden prefix
# matches 'bot.backtesting.mtf_context').
from bot.backtesting.mtf_context import MultiTimeframeContext

from bot.ml.schemas import FeatureSpec
from bot.ml.features.base import align_to_bars


GROUP_NAME = "mtf_confluence"
GROUP_VERSION = 1

# Canonical order matches the live scanner's preference list.
_TF_ORDER: Tuple[str, ...] = ("1D", "4H", "1H", "15m")


def _spec(name: str, *, dtype: str = "int8",
           desc: str, value_range=None) -> FeatureSpec:
    return FeatureSpec(
        feature_id=f"{GROUP_NAME}.{name}",
        feature_group=GROUP_NAME,
        feature_group_version=GROUP_VERSION,
        dtype=dtype,
        leak_class="safe",
        lookback_bars=0,
        lookback_unit="bars_at_this_tf",
        computed_from=("__multi_tf_bars__",),
        description=desc,
        value_range=value_range,
        live_compatible=False,
        live_compatible_with=None,
        tested_in="test_m18_ml.py::G2_MTFConfluence",
    )


SPECS: tuple = (
    _spec("available_tf_count",
            desc="count of TFs (0..N) with a snapshot bar at-or-before "
                  "the anchor",
            value_range=(0.0, 4.0)),
    _spec("tf_15m_present",
            desc="1 if the 15m TF has a bar at-or-before the anchor",
            value_range=(0.0, 1.0)),
    _spec("tf_1h_present",
            desc="1 if the 1H TF has a bar at-or-before the anchor",
            value_range=(0.0, 1.0)),
    _spec("tf_4h_present",
            desc="1 if the 4H TF has a bar at-or-before the anchor",
            value_range=(0.0, 1.0)),
    _spec("tf_1d_present",
            desc="1 if the 1D TF has a bar at-or-before the anchor",
            value_range=(0.0, 1.0)),
)


def compute(bars: pd.DataFrame, *,
              per_tf_bars: Dict[str, pd.DataFrame],
              anchor_tf: str = "15m") -> pd.DataFrame:
    """Compute mtf_confluence features for `bars` (the anchor TF bars).

    Parameters
    ----------
    bars         the anchor TF's bars (typically 15m); used for index
                   alignment and as the cadence driver.
    per_tf_bars  dict TF_label -> bars DataFrame; passed to
                   MultiTimeframeContext. Same contract as M17.B:
                   None or empty entries are tolerated (silently
                   dropped). Must contain `anchor_tf`.
    anchor_tf    label of the anchor TF (default '15m').

    Returns
    -------
    pd.DataFrame indexed identically to `bars` with the 5 features.
    """
    ctx = MultiTimeframeContext(per_tf_bars=per_tf_bars,
                                 anchor_tf=anchor_tf)

    # For every anchor in `bars`, ask the context which TFs have a
    # valid snapshot at-or-before. This is the look-ahead-safe
    # availability picture by construction.
    n = len(bars)
    available_count = np.zeros(n, dtype=np.int8)
    present_15m = np.zeros(n, dtype=np.int8)
    present_1h  = np.zeros(n, dtype=np.int8)
    present_4h  = np.zeros(n, dtype=np.int8)
    present_1d  = np.zeros(n, dtype=np.int8)

    anchor_ts_series = pd.to_datetime(bars["ts_utc"], utc=True)

    for i in range(n):
        anchor_ts = anchor_ts_series.iloc[i]
        snap = ctx.snapshot_at(anchor_ts)
        cnt = 0
        for tf in _TF_ORDER:
            sb = snap.get(tf)
            if sb is None:
                continue
            cnt += 1
            if tf == "15m":
                present_15m[i] = 1
            elif tf == "1H":
                present_1h[i] = 1
            elif tf == "4H":
                present_4h[i] = 1
            elif tf == "1D":
                present_1d[i] = 1
        available_count[i] = cnt

    out = pd.DataFrame(index=bars.index)
    out[f"{GROUP_NAME}.available_tf_count"] = available_count
    out[f"{GROUP_NAME}.tf_15m_present"]     = present_15m
    out[f"{GROUP_NAME}.tf_1h_present"]      = present_1h
    out[f"{GROUP_NAME}.tf_4h_present"]      = present_4h
    out[f"{GROUP_NAME}.tf_1d_present"]      = present_1d
    return align_to_bars(out, bars, group_name=GROUP_NAME)
