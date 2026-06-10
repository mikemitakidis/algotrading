"""bot.ml.features.trend — trend features.

All features here are leak_class="safe". NaN at warmup boundary.

Features (8):
  sma_distance_50         (close - SMA50) / SMA50
  sma_distance_200        (close - SMA200) / SMA200
  ema_distance_20         (close - EMA20) / EMA20
  ema_distance_50         (close - EMA50) / EMA50
  ema20_slope             (EMA20_t - EMA20_{t-5}) / EMA20_{t-5}
  ema50_slope             (EMA50_t - EMA50_{t-5}) / EMA50_{t-5}
  ema20_gt_ema50          0/1 flag (EMA20 > EMA50)
  close_gt_sma200         0/1 flag (close > SMA200)

mtf_trend_alignment is intentionally deferred to M18.A.3 (requires
multi-timeframe context, which lands with the MTF feature group).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from bot.ml.schemas import FeatureSpec
from bot.ml.features.base import align_to_bars


GROUP_NAME = "trend"
GROUP_VERSION = 1


def _sma(s: pd.Series, w: int) -> pd.Series:
    """SMA reimpl matching bot.backtesting.indicators.sma exactly."""
    return s.rolling(window=w, min_periods=w).mean()


def _ema(s: pd.Series, w: int) -> pd.Series:
    """EMA reimpl matching bot.backtesting.indicators.ema exactly:
    adjust=False, min_periods=w."""
    return s.ewm(span=w, adjust=False, min_periods=w).mean()


def _spec(name: str, *, lookback: int, dtype: str = "float64",
           desc: str, value_range=None,
           computed_from=("close",),
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
        tested_in="test_m18_ml.py::G2_Trend",
    )


SPECS: tuple = (
    _spec("sma_distance_50",  lookback=50,
            desc="(close - SMA50) / SMA50",
            live_compatible_with="bot.backtesting.indicators.sma"),
    _spec("sma_distance_200", lookback=200,
            desc="(close - SMA200) / SMA200",
            live_compatible_with="bot.backtesting.indicators.sma"),
    _spec("ema_distance_20",  lookback=20,
            desc="(close - EMA20) / EMA20",
            live_compatible_with="bot.backtesting.indicators.ema"),
    _spec("ema_distance_50",  lookback=50,
            desc="(close - EMA50) / EMA50",
            live_compatible_with="bot.backtesting.indicators.ema"),
    _spec("ema20_slope",      lookback=25,
            desc="(EMA20_t - EMA20_{t-5}) / EMA20_{t-5} — "
                  "fractional 5-bar slope of EMA20"),
    _spec("ema50_slope",      lookback=55,
            desc="(EMA50_t - EMA50_{t-5}) / EMA50_{t-5} — "
                  "fractional 5-bar slope of EMA50"),
    _spec("ema20_gt_ema50",   lookback=50, dtype="int8",
            desc="1 if EMA20 > EMA50 else 0; NaN warmup encoded as 0",
            value_range=(0.0, 1.0)),
    _spec("close_gt_sma200",  lookback=200, dtype="int8",
            desc="1 if close > SMA200 else 0; NaN warmup encoded as 0",
            value_range=(0.0, 1.0)),
)


def compute(bars: pd.DataFrame) -> pd.DataFrame:
    """Compute all trend features for `bars`."""
    c = bars["close"].astype(float)

    sma50  = _sma(c, 50)
    sma200 = _sma(c, 200)
    ema20  = _ema(c, 20)
    ema50  = _ema(c, 50)

    out = pd.DataFrame(index=bars.index)
    out[f"{GROUP_NAME}.sma_distance_50"]  = (c - sma50)  / sma50.where(sma50 != 0)
    out[f"{GROUP_NAME}.sma_distance_200"] = (c - sma200) / sma200.where(sma200 != 0)
    out[f"{GROUP_NAME}.ema_distance_20"]  = (c - ema20)  / ema20.where(ema20 != 0)
    out[f"{GROUP_NAME}.ema_distance_50"]  = (c - ema50)  / ema50.where(ema50 != 0)

    ema20_prev5 = ema20.shift(5)
    ema50_prev5 = ema50.shift(5)
    out[f"{GROUP_NAME}.ema20_slope"] = (ema20 - ema20_prev5) / ema20_prev5.where(
        ema20_prev5 != 0)
    out[f"{GROUP_NAME}.ema50_slope"] = (ema50 - ema50_prev5) / ema50_prev5.where(
        ema50_prev5 != 0)

    # Flag features: 0/1 with NaN warmup mapped to 0 (intentional —
    # binary flags can't be NaN without violating dtype="int8";
    # warmup → unknown → conservative "no" → 0).
    ema20_gt = (ema20 > ema50).astype("int8")
    # When either side is NaN, comparison is False, so int conversion
    # gives 0 — which is correct conservative behaviour. But we DO
    # want the spec dtype to be int8 for storage efficiency.
    out[f"{GROUP_NAME}.ema20_gt_ema50"] = ema20_gt

    close_gt_sma200 = (c > sma200).astype("int8")
    out[f"{GROUP_NAME}.close_gt_sma200"] = close_gt_sma200

    return align_to_bars(out, bars, group_name=GROUP_NAME)
