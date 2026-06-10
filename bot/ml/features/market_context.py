"""bot.ml.features.market_context — SPY/QQQ benchmark-relative features.

All features here are leak_class="safe" — every value depends only
on benchmark bars at-or-before the anchor.

Features (6):
  spy_above_ema200_1d       int8     1 if SPY close > SPY EMA(200)
                                       at the most recent 1D bar
                                       at-or-before the anchor.
  spy_drawdown_pct_60d      float64  (SPY close - rolling-max SPY
                                       close over 60 bars) /
                                       rolling-max. 0 at peak,
                                       negative below.
  spy_log_ret_1d_at_anchor  float64  most recent 1D log-return of SPY
                                       at-or-before the anchor.
  qqq_above_ema200_1d       int8     same as spy_above_ema200_1d for QQQ.
  qqq_log_ret_1d_at_anchor  float64  same as spy_log_ret_1d for QQQ.
  benchmark_data_available  int8     1 if both SPY and QQQ benchmarks
                                       are present and have valid rows
                                       at-or-before the anchor; 0
                                       otherwise. (Lets the model
                                       learn that 'no benchmark data'
                                       is its own regime.)

The benchmark bars are passed in as a dict and must be at the SAME
TIMEFRAME as the calling group's anchor (typically '1D' even when
the anchor is 15m — we use the daily benchmark snapshot via
MultiTimeframeContext-style lookup).
"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from bot.ml.schemas import FeatureSpec
from bot.ml.features.base import align_to_bars


GROUP_NAME = "market_context"
GROUP_VERSION = 1


def _spec(name: str, *, dtype: str, desc: str,
           value_range=None) -> FeatureSpec:
    return FeatureSpec(
        feature_id=f"{GROUP_NAME}.{name}",
        feature_group=GROUP_NAME,
        feature_group_version=GROUP_VERSION,
        dtype=dtype,
        leak_class="safe",
        lookback_bars=200,    # EMA(200) is the deepest dependency
        lookback_unit="bars_at_this_tf",
        computed_from=("__benchmark_bars__",),
        description=desc,
        value_range=value_range,
        live_compatible=False,
        live_compatible_with=None,
        tested_in="test_m18_ml.py::G2_MarketContext",
    )


SPECS: tuple = (
    _spec("spy_above_ema200_1d", dtype="int8",
            desc="1 if SPY close > EMA(200) at-or-before anchor",
            value_range=(0.0, 1.0)),
    _spec("spy_drawdown_pct_60d", dtype="float64",
            desc="SPY drawdown vs rolling-60 peak, fractional"
                  " (0 at peak, negative below)"),
    _spec("spy_log_ret_1d_at_anchor", dtype="float64",
            desc="most recent 1D log-return of SPY at-or-before anchor"),
    _spec("qqq_above_ema200_1d", dtype="int8",
            desc="1 if QQQ close > EMA(200) at-or-before anchor",
            value_range=(0.0, 1.0)),
    _spec("qqq_log_ret_1d_at_anchor", dtype="float64",
            desc="most recent 1D log-return of QQQ at-or-before anchor"),
    _spec("benchmark_data_available", dtype="int8",
            desc="1 if both SPY and QQQ benchmark snapshots are valid",
            value_range=(0.0, 1.0)),
)


def _ema(s: pd.Series, w: int) -> pd.Series:
    return s.ewm(span=w, adjust=False, min_periods=w).mean()


def _log_return_1(close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    safe = close.where(close > 0)
    safe_prev = prev.where(prev > 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return pd.Series(np.log(
            (safe / safe_prev).to_numpy(dtype=float)),
            index=close.index)


def _snapshot_lookup(benchmark_ts: pd.Series, anchor_ts: pd.Timestamp,
                       ) -> int:
    """Most-recent-bar-at-or-before via searchsorted.
    Returns -1 if no bar exists at-or-before anchor_ts.

    benchmark_ts must be a tz-aware datetime64[ns, UTC] series sorted
    ascending.
    """
    arr = benchmark_ts.values   # numpy datetime64[ns]
    # searchsorted with side='right' gives insertion index AFTER
    # equal values, so subtracting 1 yields the at-or-before bar.
    pos = int(np.searchsorted(arr, np.datetime64(
        anchor_ts.tz_convert("UTC").to_datetime64()
        if anchor_ts.tz is not None else anchor_ts.to_datetime64()),
        side="right")) - 1
    return pos


def compute(bars: pd.DataFrame, *,
              benchmark_bars: Dict[str, Optional[pd.DataFrame]],
              ) -> pd.DataFrame:
    """Compute market_context features for `bars`.

    Parameters
    ----------
    bars            anchor-TF bars; provides the anchor timestamps.
    benchmark_bars  dict with optional "SPY" and "QQQ" keys, each
                      mapping to a bars DataFrame (with ts_utc / close
                      columns, ascending by ts_utc). None or missing
                      entries are tolerated: the corresponding
                      features come out as NaN/0 and
                      benchmark_data_available=0.

    Returns
    -------
    pd.DataFrame indexed identically to `bars`.
    """
    n = len(bars)
    out = pd.DataFrame(index=bars.index)

    spy = benchmark_bars.get("SPY")
    qqq = benchmark_bars.get("QQQ")

    # Precompute SPY-side series
    if spy is not None and len(spy) > 0:
        spy_close = spy["close"].astype(float).reset_index(drop=True)
        spy_ts = pd.to_datetime(spy["ts_utc"],
                                  utc=True).reset_index(drop=True)
        spy_ema200 = _ema(spy_close, 200)
        # rolling-max over 60 bars (inclusive of current)
        spy_roll_max = spy_close.rolling(window=60,
                                            min_periods=60).max()
        spy_drawdown = (spy_close - spy_roll_max) / spy_roll_max.where(
            spy_roll_max > 0, np.nan)
        spy_log_ret = _log_return_1(spy_close)
    else:
        spy_close = spy_ts = spy_ema200 = spy_drawdown = spy_log_ret = None

    # Precompute QQQ-side series
    if qqq is not None and len(qqq) > 0:
        qqq_close = qqq["close"].astype(float).reset_index(drop=True)
        qqq_ts = pd.to_datetime(qqq["ts_utc"],
                                  utc=True).reset_index(drop=True)
        qqq_ema200 = _ema(qqq_close, 200)
        qqq_log_ret = _log_return_1(qqq_close)
    else:
        qqq_close = qqq_ts = qqq_ema200 = qqq_log_ret = None

    spy_above   = np.zeros(n, dtype=np.int8)
    spy_dd      = np.full(n, np.nan, dtype=np.float64)
    spy_ret     = np.full(n, np.nan, dtype=np.float64)
    qqq_above   = np.zeros(n, dtype=np.int8)
    qqq_ret     = np.full(n, np.nan, dtype=np.float64)
    bench_avail = np.zeros(n, dtype=np.int8)

    anchor_ts_series = pd.to_datetime(bars["ts_utc"], utc=True)

    for i in range(n):
        anchor_ts = anchor_ts_series.iloc[i]
        spy_ok = False
        qqq_ok = False
        if spy_close is not None:
            pos = _snapshot_lookup(spy_ts, anchor_ts)
            if pos >= 0:
                v_close = spy_close.iloc[pos]
                v_ema   = spy_ema200.iloc[pos]
                v_dd    = spy_drawdown.iloc[pos]
                v_ret   = spy_log_ret.iloc[pos]
                if np.isfinite(v_close) and np.isfinite(v_ema):
                    spy_above[i] = 1 if v_close > v_ema else 0
                    spy_ok = True
                if np.isfinite(v_dd):
                    spy_dd[i] = float(v_dd)
                if np.isfinite(v_ret):
                    spy_ret[i] = float(v_ret)
        if qqq_close is not None:
            pos = _snapshot_lookup(qqq_ts, anchor_ts)
            if pos >= 0:
                v_close = qqq_close.iloc[pos]
                v_ema   = qqq_ema200.iloc[pos]
                v_ret   = qqq_log_ret.iloc[pos]
                if np.isfinite(v_close) and np.isfinite(v_ema):
                    qqq_above[i] = 1 if v_close > v_ema else 0
                    qqq_ok = True
                if np.isfinite(v_ret):
                    qqq_ret[i] = float(v_ret)
        bench_avail[i] = 1 if (spy_ok and qqq_ok) else 0

    out[f"{GROUP_NAME}.spy_above_ema200_1d"]      = spy_above
    out[f"{GROUP_NAME}.spy_drawdown_pct_60d"]     = spy_dd
    out[f"{GROUP_NAME}.spy_log_ret_1d_at_anchor"] = spy_ret
    out[f"{GROUP_NAME}.qqq_above_ema200_1d"]      = qqq_above
    out[f"{GROUP_NAME}.qqq_log_ret_1d_at_anchor"] = qqq_ret
    out[f"{GROUP_NAME}.benchmark_data_available"] = bench_avail
    return align_to_bars(out, bars, group_name=GROUP_NAME)
