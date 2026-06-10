"""bot.ml.features.price_return — price and return features.

All features here are leak_class="safe": they use only data at or
before each bar's anchor_ts. NaN at warmup boundaries.

Features (12):
  close                          raw close (passthrough)
  log_ret_1, log_ret_5, log_ret_20    log returns
  gap_pct                        (open - prev_close) / prev_close
  body_pct                       (close - open) / open
  hl_range_pct                   (high - low) / open
  upper_wick_pct                 (high - max(open, close)) / open
  lower_wick_pct                 (min(open, close) - low) / open
  dist_from_rolling_high_20      (close - max(high, 20)) / max(high, 20)
  dist_from_rolling_high_50      same with window=50
  dist_from_rolling_low_20       (close - min(low, 20)) / min(low, 20)
  dist_from_rolling_low_50       same with window=50
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from bot.ml.schemas import FeatureSpec
from bot.ml.features.base import align_to_bars, compute_log_return


GROUP_NAME = "price_return"
GROUP_VERSION = 1


def _spec(name: str, *, lookback: int, dtype: str = "float64",
           desc: str, value_range=None,
           computed_from=("close",)) -> FeatureSpec:
    return FeatureSpec(
        feature_id=f"{GROUP_NAME}.{name}",
        feature_group=GROUP_NAME,
        feature_group_version=GROUP_VERSION,
        dtype=dtype,
        leak_class="safe",
        lookback_bars=lookback,
        lookback_unit="bars_at_this_tf",
        computed_from=tuple(computed_from),
        description=desc,
        value_range=value_range,
        live_compatible=False,
        live_compatible_with=None,
        tested_in="test_m18_ml.py::G2_PriceReturn",
    )


SPECS: tuple = (
    _spec("close",        lookback=0,
            desc="raw close (adjusted) passthrough"),
    _spec("log_ret_1",    lookback=1,
            desc="log(close_t / close_{t-1})"),
    _spec("log_ret_5",    lookback=5,
            desc="log(close_t / close_{t-5})"),
    _spec("log_ret_20",   lookback=20,
            desc="log(close_t / close_{t-20})"),
    _spec("gap_pct",      lookback=1,
            desc="(open_t - close_{t-1}) / close_{t-1}",
            computed_from=("open", "close")),
    _spec("body_pct",     lookback=0,
            desc="(close - open) / open",
            computed_from=("open", "close")),
    _spec("hl_range_pct", lookback=0,
            desc="(high - low) / open",
            computed_from=("open", "high", "low")),
    _spec("upper_wick_pct", lookback=0,
            desc="(high - max(open, close)) / open",
            computed_from=("open", "high", "close")),
    _spec("lower_wick_pct", lookback=0,
            desc="(min(open, close) - low) / open",
            computed_from=("open", "low", "close")),
    _spec("dist_from_rolling_high_20", lookback=20,
            desc="(close - rolling_max(high, 20)) / rolling_max(high, 20)",
            computed_from=("high", "close")),
    _spec("dist_from_rolling_high_50", lookback=50,
            desc="(close - rolling_max(high, 50)) / rolling_max(high, 50)",
            computed_from=("high", "close")),
    _spec("dist_from_rolling_low_20", lookback=20,
            desc="(close - rolling_min(low, 20)) / rolling_min(low, 20)",
            computed_from=("low", "close")),
    _spec("dist_from_rolling_low_50", lookback=50,
            desc="(close - rolling_min(low, 50)) / rolling_min(low, 50)",
            computed_from=("low", "close")),
)


def compute(bars: pd.DataFrame) -> pd.DataFrame:
    """Compute all price_return features for `bars`.

    Returns a DataFrame indexed identically to `bars`, with one
    column per feature in SPECS (column name = feature_id).
    """
    o = bars["open"].astype(float)
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)

    # Safe divisor: open == 0 should never happen on real bars but we
    # guard so a corrupted bar doesn't poison the whole symbol.
    safe_o = o.where(o > 0)

    out = pd.DataFrame(index=bars.index)
    out[f"{GROUP_NAME}.close"]        = c
    out[f"{GROUP_NAME}.log_ret_1"]    = compute_log_return(c, 1)
    out[f"{GROUP_NAME}.log_ret_5"]    = compute_log_return(c, 5)
    out[f"{GROUP_NAME}.log_ret_20"]   = compute_log_return(c, 20)

    prev_c = c.shift(1)
    safe_prev_c = prev_c.where(prev_c > 0)
    out[f"{GROUP_NAME}.gap_pct"]      = (o - prev_c) / safe_prev_c

    out[f"{GROUP_NAME}.body_pct"]     = (c - o) / safe_o
    out[f"{GROUP_NAME}.hl_range_pct"] = (h - l) / safe_o

    upper_body = pd.concat([o, c], axis=1).max(axis=1)
    lower_body = pd.concat([o, c], axis=1).min(axis=1)
    out[f"{GROUP_NAME}.upper_wick_pct"] = (h - upper_body) / safe_o
    out[f"{GROUP_NAME}.lower_wick_pct"] = (lower_body - l) / safe_o

    for w in (20, 50):
        rh = h.rolling(window=w, min_periods=w).max()
        rl = l.rolling(window=w, min_periods=w).min()
        safe_rh = rh.where(rh > 0)
        safe_rl = rl.where(rl > 0)
        out[f"{GROUP_NAME}.dist_from_rolling_high_{w}"] = (c - rh) / safe_rh
        out[f"{GROUP_NAME}.dist_from_rolling_low_{w}"]  = (c - rl) / safe_rl

    return align_to_bars(out, bars, group_name=GROUP_NAME)
