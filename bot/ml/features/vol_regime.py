"""bot.ml.features.vol_regime — volatility and regime features.

All features here are leak_class="safe". NaN at warmup boundary.

Features (7):
  atr_14_sma_true_range     ATR(14) in live-scanner-parity mode
                              (sma_true_range; matches bot.feature_engine
                              via bot.backtesting.indicators.atr).
  atr_over_close            atr_14 / close (volatility normalized to
                              price level — comparable across symbols)
  atr_percentile_60         rank of atr_14 within the trailing 60-bar
                              window (0.0 = lowest, 1.0 = highest)
  realized_vol_20           rolling std of log_ret_1 over 20 bars
                              (annualization NOT applied — raw daily
                              vol; consumer can scale as needed)
  bb_width                  Bollinger band width / middle band
                              (matches bot.backtesting.indicators.bollinger.width)
  bb_pos                    Bollinger position [0=lower, 1=upper]
                              (matches bot.backtesting.indicators.bb_pos)
  vol_regime_flag           ordinal 0-3: 0=lowvol, 1=mid, 2=high,
                              3=extreme; based on atr_percentile_60
                              quartile buckets

regime_flag is encoded as int8 — quartile bucket on atr_percentile_60.
Boundary semantics: [0, 0.25) -> 0; [0.25, 0.5) -> 1; [0.5, 0.75) -> 2;
[0.75, 1.0] -> 3. NaN at warmup encoded as 0 (conservative default).

INDEPENDENT IMPLEMENTATION:
  Mirrors bot.backtesting.indicators.atr(mode='sma_true_range') and
  bb_pos() exactly. G2 parity tests assert bit-identical agreement
  at rtol=1e-9.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from bot.ml.schemas import FeatureSpec
from bot.ml.features.base import align_to_bars, compute_log_return


GROUP_NAME = "vol_regime"
GROUP_VERSION = 1


def _atr_sma_true_range(high: pd.Series, low: pd.Series,
                         close: pd.Series, period: int = 14) -> pd.Series:
    """ATR in 'sma_true_range' mode — matches live scanner.

    Reproduces bot.backtesting.indicators.atr(mode='sma_true_range'):
        prev_close = close.shift(1)
        TR  = max(high - low, |high - prev_close|, |low - prev_close|)
        ATR = TR.rolling(period).mean()
    """
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")
    prev_c = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_c).abs()
    tr3 = (low  - prev_c).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()


def _bb_pos(close: pd.Series, window: int = 20,
             num_std: float = 2.0) -> pd.Series:
    """Bollinger position [0=lower, 1=upper] — live-scanner-parity.

    Reproduces bot.backtesting.indicators.bb_pos exactly:
        middle = c.rolling(window).mean()
        std    = c.rolling(window).std()
        upper  = middle + num_std * std
        lower  = middle - num_std * std
        rng    = upper - lower
        pos    = (c - lower) / (rng + 1e-9)
        # where rng <= 0: 0.5 (band collapsed fallback)
        # NaN at warmup
    """
    if window <= 0:
        raise ValueError(f"window must be positive, got {window}")
    if num_std <= 0:
        raise ValueError(f"num_std must be > 0, got {num_std}")
    middle = close.rolling(window=window, min_periods=window).mean()
    std    = close.rolling(window=window, min_periods=window).std()
    upper  = middle + num_std * std
    lower  = middle - num_std * std
    rng    = upper - lower
    out    = (close - lower) / (rng + 1e-9)
    out    = out.where(rng > 0, 0.5)
    # Restore NaN at warmup (middle is NaN there)
    out    = out.where(middle.notna(), np.nan)
    return out


def _bb_width(close: pd.Series, window: int = 20,
               num_std: float = 2.0) -> pd.Series:
    """Bollinger width / middle. Reproduces the 'width' column from
    bot.backtesting.indicators.bollinger()."""
    middle = close.rolling(window=window, min_periods=window).mean()
    std    = close.rolling(window=window, min_periods=window).std()
    upper  = middle + num_std * std
    lower  = middle - num_std * std
    rng    = upper - lower
    return rng / middle.where(middle != 0, np.nan)


def _rolling_percentile_rank(s: pd.Series, window: int) -> pd.Series:
    """For each bar, compute the percentile rank of s[t] within the
    trailing window of size `window` (inclusive of s[t]).

    Result in [0.0, 1.0]. NaN at warmup boundary.

    Implementation: for each rolling window, the percentile rank is
    rank_of_last / window. We use pd.Series.rank semantics with
    method='max' so ties get the highest rank, which is the convention
    bot.feature_engine implicitly uses for ATR percentile.
    """
    def _rank_last(arr):
        # arr is a numpy array of length `window`; rank of last element
        last = arr[-1]
        # average rank to handle ties symmetrically
        return float(np.mean(arr <= last))
    return s.rolling(window=window, min_periods=window).apply(
        _rank_last, raw=True)


def _spec(name: str, *, lookback: int, dtype: str = "float64",
           desc: str, value_range=None,
           computed_from=("high", "low", "close"),
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
        tested_in="test_m18_ml.py::G2_VolRegime",
    )


SPECS: tuple = (
    _spec("atr_14_sma_true_range", lookback=14,
            desc="ATR(14) sma_true_range mode — live-scanner-parity",
            live_compatible_with="bot.backtesting.indicators.atr"
                                   " (mode='sma_true_range')"),
    _spec("atr_over_close", lookback=14,
            desc="ATR(14) / close — symbol-comparable volatility"),
    _spec("atr_percentile_60", lookback=74,
            desc="trailing-60 percentile rank of ATR(14)",
            value_range=(0.0, 1.0)),
    _spec("realized_vol_20", lookback=21,
            desc="rolling std of log_ret_1 over 20 bars",
            computed_from=("close",)),
    _spec("bb_width", lookback=20,
            desc="(BB upper - BB lower) / middle — band width",
            computed_from=("close",),
            live_compatible_with="bot.backtesting.indicators.bollinger"
                                   ".width"),
    _spec("bb_pos", lookback=20,
            desc="(close - BB lower) / (BB upper - BB lower + 1e-9)"
                  " — live-scanner-parity",
            value_range=(0.0, 1.0),
            computed_from=("close",),
            live_compatible_with="bot.backtesting.indicators.bb_pos"),
    _spec("vol_regime_flag", lookback=74, dtype="int8",
            desc="quartile bucket on atr_percentile_60: "
                  "0=lowvol [0,0.25), 1=mid [0.25,0.5), "
                  "2=high [0.5,0.75), 3=extreme [0.75,1.0]; "
                  "NaN warmup -> 0",
            value_range=(0.0, 3.0)),
)


def compute(bars: pd.DataFrame) -> pd.DataFrame:
    """Compute all vol_regime features for `bars`."""
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)

    atr14 = _atr_sma_true_range(h, l, c, 14)

    out = pd.DataFrame(index=bars.index)
    out[f"{GROUP_NAME}.atr_14_sma_true_range"] = atr14
    out[f"{GROUP_NAME}.atr_over_close"] = atr14 / c.where(c != 0, np.nan)
    out[f"{GROUP_NAME}.atr_percentile_60"] = _rolling_percentile_rank(
        atr14, 60)

    lr1 = compute_log_return(c, 1)
    out[f"{GROUP_NAME}.realized_vol_20"] = lr1.rolling(
        window=20, min_periods=20).std()

    out[f"{GROUP_NAME}.bb_width"] = _bb_width(c, 20, 2.0)
    out[f"{GROUP_NAME}.bb_pos"]   = _bb_pos(c, 20, 2.0)

    # Regime flag: quartile bucket on atr_percentile_60.
    pct = out[f"{GROUP_NAME}.atr_percentile_60"]
    bucket = pd.Series(0, index=bars.index, dtype="int8")
    bucket = bucket.where(~(pct >= 0.25), 1)
    bucket = bucket.where(~(pct >= 0.50), 2)
    bucket = bucket.where(~(pct >= 0.75), 3)
    # NaN warmup → 0 (already the default since bucket starts at 0
    # and `where` with NaN comparison is False).
    out[f"{GROUP_NAME}.vol_regime_flag"] = bucket.astype("int8")

    return align_to_bars(out, bars, group_name=GROUP_NAME)
