"""bot.backtesting.indicators — vectorized technical indicators.

Pure functions: `pd.Series -> pd.Series` (or `-> pd.DataFrame` where the
indicator naturally produces multiple outputs like MACD).

Hard rules:
  * No `center=True` rolling windows (no centered means = no look-ahead).
  * No `shift(-N)` or any other forward indexing.
  * Inputs are not mutated (each function operates on a copy if needed).
  * NaN at the warmup boundary is allowed and documented; downstream
    strategy code is responsible for filtering it.

Live-scanner parity (M17.B addition):
  rsi() and atr() accept a `mode` parameter. Default ('wilder' for RSI,
  'wilder' for ATR) preserves M17.A semantics EXACTLY — no behaviour
  change for SmaCrossoverStrategy. scanner_replica selects the
  live-compatible modes ('sma_gain_loss' for RSI, 'sma_true_range' for
  ATR) to match bot/feature_engine.compute_features.

  vwap_dev() and bb_pos() are M17.B additions that mirror the live
  scanner's VWAP-deviation and Bollinger-position formulas
  (cumulative VWAP, +1e-9 epsilons, 0.5 fallback when band collapses).
  Their parity against bot.feature_engine is asserted by G3 tests.
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

def rsi(series: pd.Series, period: int = 14, *,
         mode: str = "wilder") -> pd.Series:
    """RSI over `period` bars.

    Two modes are supported (Sharpened Rule #2):

    * mode='wilder' (default — M17.A semantics, unchanged):
        avg_gain / avg_loss smoothed via Wilder's EMA
        (`ewm(alpha=1/period, adjust=False, min_periods=period)`).
        This is the textbook RSI and what M17.A's SmaCrossoverStrategy
        relies on. M17.A reproducibility hashes depend on this value.

    * mode='sma_gain_loss' (M17.B addition — live scanner parity):
        avg_gain / avg_loss as a simple rolling mean (`rolling(period)`).
        This matches bot/feature_engine.compute_features and the
        live scanner's RSI. Used by scanner_replica.

    Both modes:
        rs  = avg_gain / avg_loss
        rsi = 100 - 100 / (1 + rs)
    NaN for the first `period` positions (one bar of diff + warmup).
    """
    _check_positive_int(period, "period")
    if mode not in ("wilder", "sma_gain_loss"):
        raise ValueError(
            f"rsi mode must be 'wilder' or 'sma_gain_loss', got {mode!r}")

    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    if mode == "wilder":
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
    else:  # sma_gain_loss
        # Live-scanner-compatible: bot/feature_engine.compute_features
        # computes:
        #   gain  = delta.clip(lower=0).rolling(period).mean()
        #   loss  = (-delta).clip(upper=0).rolling(period).mean()  # negated
        #   rsi   = 100 - (100 / (1 + gain / (loss + 1e-9)))
        # Note the +1e-9 epsilon goes inside the inverse — we replicate
        # that exactly so per-bar values match to floating-point.
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / (avg_loss + 1e-9)
        out = 100 - (100 / (1 + rs))

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
         period: int = 14, *, mode: str = "wilder") -> pd.Series:
    """Average True Range.

    TR = max(high - low,
              |high - prev_close|,
              |low  - prev_close|)

    Two smoothing modes (Sharpened Rule #2):

    * mode='wilder' (default — M17.A semantics, unchanged):
        Wilder's EMA of TR (`ewm(alpha=1/period, adjust=False)`).
        M17.A SmaCrossoverStrategy relies on this. M17.A reproducibility
        hashes depend on this value.

    * mode='sma_true_range' (M17.B addition — live scanner parity):
        Simple rolling mean of TR (`tr.rolling(period).mean()`).
        Matches bot/feature_engine.compute_features and the live
        scanner's ATR. Used by scanner_replica.
    """
    _check_positive_int(period, "period")
    if mode not in ("wilder", "sma_true_range"):
        raise ValueError(
            f"atr mode must be 'wilder' or 'sma_true_range', got {mode!r}")
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low  - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    if mode == "wilder":
        return tr.ewm(alpha=1.0 / period, adjust=False,
                        min_periods=period).mean()
    else:  # sma_true_range
        return tr.rolling(window=period, min_periods=period).mean()


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
# Volume-weighted / Bollinger derived (M17.B additions for scanner parity)
# ─────────────────────────────────────────────────────────────────────

def vwap_dev(close: pd.Series, volume: pd.Series) -> pd.Series:
    """Cumulative VWAP deviation — fractional gap between close and VWAP.

    Matches bot/feature_engine.compute_features:
        vwap     = cumsum(c * v) / cumsum(v)
        vwap_dev = (c - vwap) / vwap
    NOT a rolling-window VWAP — the live scanner uses a CUMULATIVE
    VWAP since the start of the loaded bars (i.e., session-level
    arithmetic on whatever bars are passed in). Reproduced exactly
    here so scanner_replica gets identical values.

    The +1e-9 epsilons in the live scanner protect against zero-volume
    bars and zero VWAP; we mirror them so per-bar values match within
    floating-point.

    Returns a pd.Series, NaN where division would be invalid.
    """
    if not isinstance(close, pd.Series):
        raise TypeError(f"close must be pd.Series, got {type(close).__name__}")
    if not isinstance(volume, pd.Series):
        raise TypeError(f"volume must be pd.Series, got {type(volume).__name__}")
    if len(close) != len(volume):
        raise ValueError(
            f"close ({len(close)}) and volume ({len(volume)}) lengths differ")
    cum_pv = (close * volume).cumsum()
    cum_v  = volume.cumsum()
    vwap   = cum_pv / (cum_v + 1e-9)
    return (close - vwap) / (vwap + 1e-9)


def bb_pos(series: pd.Series, window: int = 20,
            num_std: float = 2.0) -> pd.Series:
    """Position of `series` inside its Bollinger Band, as a fraction
    [0.0 = at lower band, 1.0 = at upper band].

    Matches bot/feature_engine.compute_features:
        sma  = c.rolling(window).mean()
        std  = c.rolling(window).std()
        up   = sma + num_std * std
        lo   = sma - num_std * std
        rng  = up.iloc[-1] - lo.iloc[-1]
        pos  = (c.iloc[-1] - lo.iloc[-1]) / (rng + 1e-9)
                  if rng > 0 else 0.5

    The live scanner returns 0.5 when the band collapsed (rng <= 0);
    we mirror that. Per-bar version (returns a Series, not scalar) so
    scanner_replica can read the value at any anchor.

    NaN at the warmup boundary.
    """
    _check_positive_int(window, "window")
    if num_std <= 0:
        raise ValueError(f"num_std must be > 0, got {num_std}")
    middle = series.rolling(window=window, min_periods=window).mean()
    std    = series.rolling(window=window, min_periods=window).std()
    upper  = middle + num_std * std
    lower  = middle - num_std * std
    rng    = upper - lower
    # Mirror the live scanner's +1e-9 epsilon AND its
    # "rng > 0 -> compute; else -> 0.5" fallback.
    out = (series - lower) / (rng + 1e-9)
    out = out.where(rng > 0, 0.5)
    # Preserve NaN at warmup (middle is NaN there, so rng is NaN, so
    # 'rng > 0' is False — the .where call would coerce to 0.5).
    # Restore NaN by reapplying the warmup mask.
    out = out.where(middle.notna(), np.nan)
    return out


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

    Live-scanner parity note: bot/feature_engine.compute_features uses
    `v.iloc[-1] / (vol_ma.iloc[-1] + 1e-9)` — a tiny epsilon to avoid
    division by zero when the trailing window had zero volume. Here
    we instead return NaN when avg == 0, which is mathematically
    cleaner. For non-zero volumes (the only ones that ever drive
    signals) the two formulas agree to floating-point precision.
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
    "bollinger", "bb_pos", "vwap_dev",
    "volume_avg", "volume_ratio",
]
