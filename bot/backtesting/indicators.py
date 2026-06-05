"""bot.backtesting.indicators — vectorized technical indicators.

Pure functions: `pd.Series -> pd.Series` (or `-> pd.DataFrame` where the
indicator naturally produces multiple outputs like MACD).

Hard rules:
  * No `center=True` rolling windows (no centered means = no look-ahead).
  * No `shift(-N)` or any other forward indexing.
  * Inputs are not mutated (each function operates on a copy if needed).
  * NaN at the warmup boundary is allowed and documented; downstream
    strategy code is responsible for filtering it.

Parity with bot.indicators.compute() (the live scanner's last-bar
indicator engine) is DEFERRED to M17.B when scanner_replica lands.
M17.A indicators are standalone and verified against hand-computed
reference values only.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────
# Trend / moving averages
# ─────────────────────────────────────────────────────────────────────

def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average over `window` periods.

    Returns a Series with NaN for the first `window-1` positions.
    """
    _check_positive_int(window, "window")
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, window: int) -> pd.Series:
    """Exponential moving average. Uses `adjust=False` to match the
    standard / live-scanner convention (recursive form, not the
    full-window weighting variant)."""
    _check_positive_int(window, "window")
    # min_periods=window ensures the first window-1 values are NaN,
    # matching SMA's warmup convention. This is conservative for
    # backtesting (no early-bar trades from partial-EMA values).
    return series.ewm(span=window, adjust=False,
                        min_periods=window).mean()


# ─────────────────────────────────────────────────────────────────────
# Momentum / oscillators
# ─────────────────────────────────────────────────────────────────────

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI over `period` bars.

    Standard definition:
        gain = max(close.diff(), 0)
        loss = max(-close.diff(), 0)
        avg_gain = SMA(gain, period)  for the first value;
                   then  EMA-like recursive smoothing (Wilder)
        avg_loss = same
        rs  = avg_gain / avg_loss
        rsi = 100 - 100 / (1 + rs)

    Returns NaN for the first `period` values (one bar of diff +
    `period-1` for the SMA seed).
    """
    _check_positive_int(period, "period")
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    # Wilder's smoothing == EMA with alpha = 1/period (i.e. com = period-1)
    # NOT span = period. Use ewm(alpha=1/period) for exact Wilder semantics.
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False,
                          min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False,
                          min_periods=period).mean()

    # Avoid divide-by-zero: where avg_loss is 0, RSI is 100 (all gains).
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))

    # Where avg_loss == 0 (NaN above) and avg_gain > 0: RSI = 100.
    # Where both are 0: RSI is undefined (NaN — the series is flat).
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)

    return out


def macd(series: pd.Series, fast: int = 12, slow: int = 26,
          signal: int = 9) -> pd.DataFrame:
    """MACD: returns DataFrame with columns macd, signal, hist.

    macd   = EMA(close, fast) - EMA(close, slow)
    signal = EMA(macd, signal)
    hist   = macd - signal

    All NaN at the warmup boundary (slow + signal - 1 bars).
    """
    _check_positive_int(fast,   "fast")
    _check_positive_int(slow,   "slow")
    _check_positive_int(signal, "signal")
    if fast >= slow:
        raise ValueError(f"fast ({fast}) must be < slow ({slow})")

    ema_fast = series.ewm(span=fast, adjust=False,
                            min_periods=fast).mean()
    ema_slow = series.ewm(span=slow, adjust=False,
                            min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False,
                                  min_periods=signal).mean()
    hist = macd_line - signal_line

    out = pd.DataFrame({
        "macd":   macd_line,
        "signal": signal_line,
        "hist":   hist,
    }, index=series.index)
    return out


# ─────────────────────────────────────────────────────────────────────
# Volatility / range
# ─────────────────────────────────────────────────────────────────────

def atr(high: pd.Series, low: pd.Series, close: pd.Series,
         period: int = 14) -> pd.Series:
    """Average True Range (Wilder).

    TR = max(high - low,
              |high - prev_close|,
              |low  - prev_close|)
    ATR = Wilder-smoothed EMA of TR over `period` bars.
    """
    _check_positive_int(period, "period")
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low  - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # Wilder smoothing == EMA with alpha = 1/period
    return tr.ewm(alpha=1.0 / period, adjust=False,
                    min_periods=period).mean()


def bollinger(series: pd.Series, window: int = 20,
                num_std: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands: middle (SMA), upper, lower, pct_b, width.

    pct_b = (price - lower) / (upper - lower) — Bollinger %B
    width = (upper - lower) / middle           — band width as fraction
    """
    _check_positive_int(window, "window")
    if num_std <= 0:
        raise ValueError(f"num_std must be > 0, got {num_std}")
    middle = series.rolling(window=window, min_periods=window).mean()
    std    = series.rolling(window=window, min_periods=window).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    rng   = upper - lower
    pct_b = (series - lower) / rng.where(rng > 0, np.nan)
    width = rng / middle.where(middle != 0, np.nan)
    return pd.DataFrame({
        "middle": middle,
        "upper":  upper,
        "lower":  lower,
        "pct_b":  pct_b,
        "width":  width,
    }, index=series.index)


# ─────────────────────────────────────────────────────────────────────
# Volume
# ─────────────────────────────────────────────────────────────────────

def volume_avg(volume: pd.Series, window: int = 20) -> pd.Series:
    """Trailing average volume over `window` bars (SMA on volume).

    Returns NaN for the first `window-1` positions.
    """
    return sma(volume, window)


def volume_ratio(volume: pd.Series, window: int = 20) -> pd.Series:
    """Ratio of current bar's volume to its trailing average.

    > 1.0 means above-average volume; < 1.0 means below-average.
    NaN at warmup boundary.
    """
    avg = volume_avg(volume, window)
    return volume / avg.where(avg > 0, np.nan)


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────

def _check_positive_int(v, name: str) -> None:
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise ValueError(f"{name} must be a positive int, got {v!r}")


__all__ = [
    "sma", "ema", "rsi", "macd", "atr",
    "bollinger", "volume_avg", "volume_ratio",
]
