"""research.common.bars — OHLCV conventions, validation and resampling.

Conventions used by every research lane:

* Columns: ``ts_utc, open, high, low, close, volume, quality_flags`` (the M17
  bar contract; extra columns are allowed and ignored).
* ``ts_utc`` is tz-aware UTC and marks the bar's CLOSE (end of interval).
  This matches the M17 multi-timeframe context, which treats a bar as usable
  at an anchor only when ``ts_utc <= anchor``.
* Synthetic US sessions run 14:30–21:00 UTC (09:30–16:00 ET, daylight saving
  ignored). Daily bars are stamped at the session close.

CALENDAR SCOPE: the session helpers here (``session_key``, ``resample``,
``multi_timeframe``) describe the SYNTHETIC calendar only — no daylight saving,
no exchange holidays, no half days. ``resample`` refuses bars that do not sit
on that grid, so real market data (e.g. US bars at 13:30 UTC in summer) cannot
silently be bucketed with synthetic assumptions. Real-market adapters must
use an actual exchange calendar and the source's own bar start/end times.
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd

BAR_COLUMNS = ("ts_utc", "open", "high", "low", "close", "volume", "quality_flags")
TS_CONVENTION = "bar_close_utc"
TF_MINUTES: Dict[str, int] = {"15m": 15, "1H": 60, "4H": 240, "1D": 390}
SESSION_OPEN_UTC = (14, 30)
SESSION_MINUTES = 390
CALENDAR_SCOPE = "synthetic_only"


def validate_ohlcv(df: pd.DataFrame, *, name: str = "bars") -> List[str]:
    """Return a list of contract violations (empty list = valid)."""
    issues: List[str] = []
    missing = [c for c in BAR_COLUMNS if c not in df.columns]
    if missing:
        return ["%s: missing columns %s" % (name, missing)]
    if len(df) == 0:
        return issues
    ts = df["ts_utc"]
    if not isinstance(ts.dtype, pd.DatetimeTZDtype) or str(ts.dt.tz) != "UTC":
        issues.append("%s: ts_utc must be tz-aware UTC" % name)
    elif not ts.is_monotonic_increasing or ts.duplicated().any():
        issues.append("%s: ts_utc must be strictly increasing" % name)
    o, h, l, c, v = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume"))
    if not (np.isfinite(o).all() and np.isfinite(h).all() and np.isfinite(l).all() and np.isfinite(c).all()):
        issues.append("%s: non-finite OHLC" % name)
    elif (np.minimum.reduce([o, h, l, c]) <= 0).any():
        issues.append("%s: non-positive price" % name)
    else:
        tol = 1e-9 * np.maximum(1.0, np.abs(h))
        if (l > np.minimum(o, c) + tol).any() or (h < np.maximum(o, c) - tol).any() or (l > h + tol).any():
            issues.append("%s: high/low do not bracket open/close" % name)
    if not np.isfinite(v).all() or (v < 0).any():
        issues.append("%s: volume must be finite and >= 0" % name)
    return issues


def session_key(ts: pd.Series) -> pd.Series:
    """Session date (UTC calendar date of the bar close) for synthetic US sessions."""
    return ts.dt.tz_convert("UTC").dt.floor("D")


def resample(bars: pd.DataFrame, target_tf: str, *, source_tf: str = "15m") -> pd.DataFrame:
    """Aggregate intraday bars into a coarser timeframe, session-aware.

    Buckets are anchored at the session open (14:30 UTC), so 1H gives seven
    bars per session (the last one 30 minutes long) and 4H gives two. Each
    output bar is stamped with the close time of its last input bar.
    """
    if target_tf not in TF_MINUTES or source_tf not in TF_MINUTES:
        raise ValueError("unknown timeframe %r -> %r" % (source_tf, target_tf))
    if TF_MINUTES[target_tf] < TF_MINUTES[source_tf]:
        raise ValueError("cannot resample %s down to %s" % (source_tf, target_tf))
    if len(bars) == 0:
        return bars.loc[:, list(BAR_COLUMNS)].copy()
    ts = pd.to_datetime(bars["ts_utc"], utc=True)
    day = session_key(ts)
    open_utc = day + pd.Timedelta(SESSION_OPEN_UTC[0] * 60 + SESSION_OPEN_UTC[1], unit="m")
    start_min = ((ts - open_utc).dt.total_seconds() // 60).astype(int) - TF_MINUTES[source_tf]
    off_grid = (start_min < 0) | (start_min + TF_MINUTES[source_tf] > SESSION_MINUTES) \
        | (start_min % TF_MINUTES[source_tf] != 0)
    if off_grid.any():
        raise ValueError("bars are not on the synthetic 14:30-21:00 UTC %s grid (first offender %s); "
                         "real market data needs an exchange-calendar resampler" % (
                             source_tf, ts[off_grid].iloc[0]))
    if target_tf == "1D":
        bucket = pd.Series(0, index=bars.index)
    else:
        bucket = start_min // TF_MINUTES[target_tf]
    g = bars.assign(_day=day.values, _bucket=bucket.values).groupby(["_day", "_bucket"], sort=True)
    out = pd.DataFrame({
        "ts_utc": g["ts_utc"].max(),
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
        "quality_flags": g["quality_flags"].max() if "quality_flags" in bars else 0,
    }).reset_index(drop=True)
    out["ts_utc"] = pd.to_datetime(out["ts_utc"], utc=True)
    return out


def multi_timeframe(bars_15m: pd.DataFrame, timeframes=("1D", "4H", "1H", "15m")) -> Dict[str, pd.DataFrame]:
    """Build the per-timeframe bar dict used by M17 multi-timeframe strategies."""
    out: Dict[str, pd.DataFrame] = {}
    for tf in timeframes:
        out[tf] = bars_15m.loc[:, list(BAR_COLUMNS)].reset_index(drop=True) if tf == "15m" \
            else resample(bars_15m, tf, source_tf="15m")
    return out


__all__ = ["BAR_COLUMNS", "TS_CONVENTION", "TF_MINUTES", "SESSION_OPEN_UTC",
           "SESSION_MINUTES", "CALENDAR_SCOPE", "validate_ohlcv", "session_key", "resample",
           "multi_timeframe"]
