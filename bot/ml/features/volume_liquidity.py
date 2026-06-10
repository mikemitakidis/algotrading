"""bot.ml.features.volume_liquidity — volume and liquidity features.

All features here are leak_class="safe". NaN at warmup boundary.

Features (5):
  vol_ratio_20         volume / SMA(volume, 20) — live-scanner-parity
                         (mirrors bot.backtesting.indicators.volume_ratio
                         which itself matches bot.feature_engine)
  vol_zscore_60        (volume - SMA(vol, 60)) / std(vol, 60)
  dollar_vol_20        SMA(close * volume, 20) — trailing dollar volume
  vol_shock            volume / SMA(vol, 60) - 1
                         (rolling-60 baseline; "shock" relative to a
                         longer-window normal)
  liquidity_bucket     int8 ordinal: 0=micro, 1=small, 2=mid, 3=large,
                         4=mega; based on percentile of dollar_vol_20
                         within the symbol's own trailing 252-bar
                         window (NOT cross-sectional — uses only
                         past data of THIS symbol).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from bot.ml.schemas import FeatureSpec
from bot.ml.features.base import align_to_bars


GROUP_NAME = "volume_liquidity"
GROUP_VERSION = 1


def _spec(name: str, *, lookback: int, dtype: str = "float64",
           desc: str, value_range=None,
           computed_from=("volume",),
           live_compatible_with=None) -> FeatureSpec:
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
        live_compatible=bool(live_compatible_with),
        live_compatible_with=live_compatible_with,
        tested_in="test_m18_ml.py::G2_VolumeLiquidity",
    )


SPECS: tuple = (
    _spec("vol_ratio_20", lookback=20,
            desc="volume / SMA(volume, 20) — live-scanner-parity",
            live_compatible_with="bot.backtesting.indicators.volume_ratio"),
    _spec("vol_zscore_60", lookback=60,
            desc="(volume - SMA(vol, 60)) / std(vol, 60)"),
    _spec("dollar_vol_20", lookback=20,
            desc="SMA(close * volume, 20) — trailing dollar volume",
            computed_from=("close", "volume")),
    _spec("vol_shock", lookback=60,
            desc="volume / SMA(vol, 60) - 1 — long-window vol shock"),
    _spec("liquidity_bucket", lookback=252, dtype="int8",
            desc="symbol-self-relative liquidity bucket on dollar_vol_20: "
                  "0=micro [0,0.2), 1=small [0.2,0.4), 2=mid [0.4,0.6), "
                  "3=large [0.6,0.8), 4=mega [0.8,1.0]; NaN warmup -> 0",
            value_range=(0.0, 4.0),
            computed_from=("close", "volume")),
)


def _rolling_percentile_rank(s: pd.Series, window: int) -> pd.Series:
    """Trailing percentile rank — same impl as vol_regime."""
    def _rank_last(arr):
        last = arr[-1]
        return float(np.mean(arr <= last))
    return s.rolling(window=window, min_periods=window).apply(
        _rank_last, raw=True)


def compute(bars: pd.DataFrame) -> pd.DataFrame:
    """Compute all volume_liquidity features for `bars`."""
    c = bars["close"].astype(float)
    v = bars["volume"].astype(float)

    vol_sma_20 = v.rolling(window=20, min_periods=20).mean()
    vol_sma_60 = v.rolling(window=60, min_periods=60).mean()
    vol_std_60 = v.rolling(window=60, min_periods=60).std()

    out = pd.DataFrame(index=bars.index)
    # vol_ratio_20: NaN where SMA is 0 (mathematically cleaner than
    # epsilon; the live scanner uses +1e-9 but for non-zero volume
    # the values agree to floating-point — parity test confirms this).
    out[f"{GROUP_NAME}.vol_ratio_20"] = v / vol_sma_20.where(
        vol_sma_20 > 0, np.nan)

    out[f"{GROUP_NAME}.vol_zscore_60"] = (v - vol_sma_60) / vol_std_60.where(
        vol_std_60 > 0, np.nan)

    dv = c * v
    dollar_vol_20 = dv.rolling(window=20, min_periods=20).mean()
    out[f"{GROUP_NAME}.dollar_vol_20"] = dollar_vol_20

    out[f"{GROUP_NAME}.vol_shock"] = (v / vol_sma_60.where(
        vol_sma_60 > 0, np.nan)) - 1.0

    # liquidity_bucket: percentile of dollar_vol_20 over trailing 252.
    dv_pct = _rolling_percentile_rank(dollar_vol_20, 252)
    bucket = pd.Series(0, index=bars.index, dtype="int8")
    bucket = bucket.where(~(dv_pct >= 0.2), 1)
    bucket = bucket.where(~(dv_pct >= 0.4), 2)
    bucket = bucket.where(~(dv_pct >= 0.6), 3)
    bucket = bucket.where(~(dv_pct >= 0.8), 4)
    out[f"{GROUP_NAME}.liquidity_bucket"] = bucket.astype("int8")

    return align_to_bars(out, bars, group_name=GROUP_NAME)
