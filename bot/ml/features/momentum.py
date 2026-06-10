"""bot.ml.features.momentum — momentum / oscillator features.

All features here are leak_class="safe". NaN at warmup boundary.

Features (5):
  rsi_14_sma_gain_loss     RSI(14) in live-scanner-parity mode
                             (sma_gain_loss; matches
                             bot.feature_engine.compute_features
                             via bot.backtesting.indicators.rsi).
  macd_line                MACD(12, 26)
  macd_signal              EMA(MACD, 9)
  macd_hist                MACD line - signal
  roc_10                   10-bar rate of change: (c - c.shift(10)) / c.shift(10)
  momentum_acceleration    log_ret_5 - log_ret_5.shift(5)
                             (change in 5-bar momentum over 5 bars)

stoch_k is intentionally NOT in M18.A.2. It can be added in a later
phase if the eval shows it adds incremental signal.

INDEPENDENT IMPLEMENTATION:
  These computations DO NOT import bot.backtesting.indicators (SR-7
  in spirit: parity by code, not by import). G2 parity tests in
  test_m18_ml.py assert bit-identical agreement at rtol=1e-9.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from bot.ml.schemas import FeatureSpec
from bot.ml.features.base import align_to_bars, compute_log_return


GROUP_NAME = "momentum"
GROUP_VERSION = 1


def _rsi_sma_gain_loss(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI in 'sma_gain_loss' mode — matches live scanner.

    Reproduces bot.backtesting.indicators.rsi(mode='sma_gain_loss')
    EXACTLY by independent implementation:
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta).clip(lower=0).rolling(period).mean()
        rs    = gain / (loss + 1e-9)
        rsi   = 100 - (100 / (1 + rs))

    The +1e-9 epsilon mirrors bot.feature_engine.compute_features
    exactly so per-bar values agree to floating-point.

    G2 parity test asserts == bot.backtesting.indicators.rsi(
        ..., mode='sma_gain_loss') at rtol=1e-9.
    """
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(window=period, min_periods=period).mean()
    loss  = (-delta).clip(lower=0).rolling(window=period, min_periods=period).mean()
    rs    = gain / (loss + 1e-9)
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(s: pd.Series, w: int) -> pd.Series:
    return s.ewm(span=w, adjust=False, min_periods=w).mean()


def _macd_triple(close: pd.Series, fast: int = 12, slow: int = 26,
                  signal: int = 9):
    """Returns (macd_line, signal_line, hist). Mirrors
    bot.backtesting.indicators.macd."""
    ema_f = _ema(close, fast)
    ema_s = _ema(close, slow)
    macd  = ema_f - ema_s
    sig   = _ema(macd, signal)
    return macd, sig, macd - sig


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
        tested_in="test_m18_ml.py::G2_Momentum",
    )


SPECS: tuple = (
    _spec("rsi_14_sma_gain_loss", lookback=14,
            desc="RSI(14) sma_gain_loss mode — live-scanner-parity",
            value_range=(0.0, 100.0),
            live_compatible_with="bot.backtesting.indicators.rsi"
                                   " (mode='sma_gain_loss')"),
    _spec("macd_line",   lookback=26,
            desc="EMA(close, 12) - EMA(close, 26)",
            live_compatible_with="bot.backtesting.indicators.macd"),
    _spec("macd_signal", lookback=34,
            desc="EMA(macd_line, 9)",
            live_compatible_with="bot.backtesting.indicators.macd"),
    _spec("macd_hist",   lookback=34,
            desc="macd_line - macd_signal",
            live_compatible_with="bot.backtesting.indicators.macd"),
    _spec("roc_10",      lookback=10,
            desc="(close_t / close_{t-10}) - 1"),
    _spec("momentum_acceleration", lookback=10,
            desc="log_ret_5_t - log_ret_5_{t-5} — "
                  "change in 5-bar log-return over 5 bars"),
)


def compute(bars: pd.DataFrame) -> pd.DataFrame:
    """Compute all momentum features for `bars`."""
    c = bars["close"].astype(float)

    out = pd.DataFrame(index=bars.index)
    out[f"{GROUP_NAME}.rsi_14_sma_gain_loss"] = _rsi_sma_gain_loss(c, 14)

    macd, sig, hist = _macd_triple(c)
    out[f"{GROUP_NAME}.macd_line"]   = macd
    out[f"{GROUP_NAME}.macd_signal"] = sig
    out[f"{GROUP_NAME}.macd_hist"]   = hist

    prev_10 = c.shift(10)
    out[f"{GROUP_NAME}.roc_10"] = (c / prev_10.where(prev_10 != 0)) - 1.0

    lr5 = compute_log_return(c, 5)
    out[f"{GROUP_NAME}.momentum_acceleration"] = lr5 - lr5.shift(5)

    return align_to_bars(out, bars, group_name=GROUP_NAME)
