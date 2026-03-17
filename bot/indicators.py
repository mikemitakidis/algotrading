"""
bot/indicators.py
Computes all technical indicators from an OHLCV DataFrame.
Single function: compute(df) -> dict | None
No side effects. No logging. No data fetching.
"""
import numpy as np
import pandas as pd


def compute(df: pd.DataFrame) -> dict | None:
    """
    Compute all technical indicators from OHLCV DataFrame.

    Args:
        df: DataFrame with lowercase columns: open, high, low, close, volume

    Returns:
        dict with 13 indicator values, or None if:
        - insufficient bars (< 30)
        - any computed value is NaN or Inf
    """
    if len(df) < 30:
        return None

    try:
        c = df['close'].astype(float)
        v = df['volume'].astype(float)
        h = df['high'].astype(float)
        l = df['low'].astype(float)

        # ── RSI (14) ──────────────────────────────────────────────────────────
        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = 100 - (100 / (1 + gain / (loss + 1e-9)))

        # ── MACD (12/26/9) ───────────────────────────────────────────────────
        ema12      = c.ewm(span=12, adjust=False).mean()
        ema26      = c.ewm(span=26, adjust=False).mean()
        macd_line  = ema12 - ema26
        macd_sig   = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist  = macd_line - macd_sig

        # ── EMA 20 / 50 ──────────────────────────────────────────────────────
        ema20 = c.ewm(span=20, adjust=False).mean()
        ema50 = c.ewm(span=50, adjust=False).mean()

        # ── Bollinger Bands (20, 2) ───────────────────────────────────────────
        sma20  = c.rolling(20).mean()
        std20  = c.rolling(20).std()
        bb_up  = sma20 + 2 * std20
        bb_lo  = sma20 - 2 * std20
        bb_rng = bb_up.iloc[-1] - bb_lo.iloc[-1]
        bb_pos = float(
            (c.iloc[-1] - bb_lo.iloc[-1]) / (bb_rng + 1e-9)
            if bb_rng > 0 else 0.5
        )
        bb_width = float(bb_rng / (sma20.iloc[-1] + 1e-9))

        # ── VWAP deviation ────────────────────────────────────────────────────
        vwap     = (c * v).cumsum() / (v.cumsum() + 1e-9)
        vwap_dev = float((c.iloc[-1] - vwap.iloc[-1]) / (vwap.iloc[-1] + 1e-9))

        # ── OBV 5-bar slope ───────────────────────────────────────────────────
        obv       = (np.sign(c.diff()) * v).cumsum()
        obv_slope = float(
            (obv.iloc[-1] - obv.iloc[-5]) / (abs(obv.iloc[-5]) + 1e-9)
            if len(obv) > 5 else 0.0
        )

        # ── ATR (14) ──────────────────────────────────────────────────────────
        tr  = pd.concat(
            [h - l, (h - c.shift()).abs(), (l - c.shift()).abs()],
            axis=1
        ).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])

        # ── Volume ratio vs 20-bar average ────────────────────────────────────
        vol_ma    = v.rolling(20).mean()
        vol_ratio = float(v.iloc[-1] / (vol_ma.iloc[-1] + 1e-9))

        # ── 20-bar price change ───────────────────────────────────────────────
        lb    = min(20, len(c) - 1)
        pchg  = float((c.iloc[-1] - c.iloc[-lb]) / (c.iloc[-lb] + 1e-9))

        result = {
            'rsi':       float(rsi.iloc[-1]),
            'macd_hist': float(macd_hist.iloc[-1]),
            'ema20':     float(ema20.iloc[-1]),
            'ema50':     float(ema50.iloc[-1]),
            'bb_pos':    bb_pos,
            'bb_width':  bb_width,
            'vwap_dev':  vwap_dev,
            'obv_slope': obv_slope,
            'atr':       atr,
            'vol_ratio': vol_ratio,
            'price':     float(c.iloc[-1]),
            'pchg':      pchg,
        }

        # Reject any NaN or Inf
        if any(not np.isfinite(v) for v in result.values()):
            return None

        return result

    except Exception:
        return None
