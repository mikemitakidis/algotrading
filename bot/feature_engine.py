"""
bot/feature_engine.py
Centralised feature calculation layer (Milestone 7).

Single public function:
    compute_features(df) -> FeatureSet | None

Returns a FeatureSet with two clearly separated groups:

  decision  — features used by the live strategy RIGHT NOW
              (RSI, MACD hist, EMA20, EMA50, VWAP dev, vol_ratio, ATR, price)
              These power score_timeframe() and must not change without
              a strategy version bump.

  ml        — features logged for future ML / analytics ONLY
              Not used in any live signal decision today.
              Safe to add/remove without touching strategy logic.

FEATURE REGISTRY
================
Decision features (used in live strategy):
  rsi         RSI-14. Range ~0–100. Long: 30–75. Short: >50.
  macd_hist   MACD(12,26,9) histogram. Long: >0. Short: <0.
  ema20       20-period EMA. Used in EMA20/EMA50 trend condition.
  ema50       50-period EMA. Used in EMA20/EMA50 trend condition.
  vwap_dev    (price - VWAP) / VWAP. Long: >-1.5%. Short: <+1.5%.
  vol_ratio   current_vol / 20-bar avg vol. Long/Short: >0.6.
  atr         ATR-14. Used for stop/target sizing.
  price       Last close. Used for entry/stop/target calc.
  bb_pos      Position within Bollinger Bands (0=lower, 1=upper).
              Used by backtest_v2 trade record and scanner signal dict.

ML-only features (logged, not used in decisions):
  bb_pos      Position within Bollinger Bands (0=lower, 1=upper).
  bb_width    Band width normalised by SMA20. Measures volatility regime.
  obv_slope   5-bar OBV slope / |OBV[-5]|. Volume trend direction.
  pchg_20     20-bar price change. Medium-term momentum.
  pchg_1      1-bar return. Very short-term momentum.
  pchg_3      3-bar return. Short-term momentum.
  pchg_5      5-bar return. Short-term momentum.
  adx         ADX-14. Trend strength. >25 = trending, <20 = ranging.
  di_plus     +DI-14. Directional indicator — bullish pressure.
  di_minus    -DI-14. Directional indicator — bearish pressure.
  stoch_k     Stochastic %K(14,3). Momentum oscillator 0–100.
  stoch_d     Stochastic %D (3-bar SMA of %K). Signal line.
  roc_10      Rate of Change over 10 bars (%). Momentum.
  cci_20      CCI-20. Mean deviation from typical price. >100 overbought.
  mfi_14      Money Flow Index 14. Volume-weighted RSI. >80 overbought.
  atr_pct     ATR / price * 100. Normalised volatility measure.
  ema20_dist  (price - EMA20) / EMA20 * 100. Distance from short trend.
  ema50_dist  (price - EMA50) / EMA50 * 100. Distance from long trend.
  vol_zscore  (vol - vol_20mean) / vol_20std. Normalised volume spike.
  body_pct    |close-open| / (high-low+ε). Candle body ratio 0–1.
  upper_wick  (high - max(open,close)) / (high-low+ε). Upper shadow ratio.
  lower_wick  (min(open,close) - low) / (high-low+ε). Lower shadow ratio.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Optional
import numpy as np
import pandas as pd


# ── Return type ───────────────────────────────────────────────────────────────

@dataclass
class FeatureSet:
    """
    Clean container separating live-decision features from ML-only features.
    Both groups are plain dicts for easy dict-unpacking into signals.
    """
    decision: dict   # used in live strategy scoring NOW
    ml:       dict   # logged for ML/analytics, NOT used in live decisions

    def all_features(self) -> dict:
        """Flat merge of decision + ml features. ml keys get ml_ prefix."""
        out = dict(self.decision)
        for k, v in self.ml.items():
            out[f'ml_{k}'] = v
        return out


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(x) -> float:
    """Return float or None; never NaN/Inf."""
    try:
        f = float(x)
        return f if np.isfinite(f) else None
    except Exception:
        return None


def _rolling_window(series: pd.Series, window: int) -> Optional[float]:
    if len(series) < window:
        return None
    return _safe(series.iloc[-window:])


# ── Main entry point ──────────────────────────────────────────────────────────

def compute_features(df: pd.DataFrame) -> Optional[FeatureSet]:
    """
    Compute all features from an OHLCV DataFrame.

    Args:
        df: DataFrame with lowercase columns: open, high, low, close, volume
            Minimum 60 rows for full ML feature coverage.
            Minimum 30 rows for decision features only.

    Returns:
        FeatureSet or None if insufficient data or all decision features are bad.
    """
    if df is None or len(df) < 30:
        return None

    try:
        c = df['close'].astype(float)
        v = df['volume'].astype(float)
        h = df['high'].astype(float)
        lo = df['low'].astype(float)
        o = df['open'].astype(float)
        n = len(c)

        # ── DECISION FEATURES ─────────────────────────────────────────────────
        # These are the ONLY features used by score_timeframe(). Do not change
        # their names or calculation without bumping strategy version.

        # RSI-14
        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi_s = 100 - (100 / (1 + gain / (loss + 1e-9)))
        rsi   = _safe(rsi_s.iloc[-1])

        # MACD(12,26,9)
        ema12     = c.ewm(span=12, adjust=False).mean()
        ema26     = c.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        macd_sig  = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = _safe((macd_line - macd_sig).iloc[-1])

        # EMA 20 / 50
        ema20_s = c.ewm(span=20, adjust=False).mean()
        ema50_s = c.ewm(span=50, adjust=False).mean()
        ema20   = _safe(ema20_s.iloc[-1])
        ema50   = _safe(ema50_s.iloc[-1])

        # VWAP deviation
        vwap     = (c * v).cumsum() / (v.cumsum() + 1e-9)
        vwap_dev = _safe((c.iloc[-1] - vwap.iloc[-1]) / (vwap.iloc[-1] + 1e-9))

        # Volume ratio vs 20-bar average
        vol_ma    = v.rolling(20).mean()
        vol_ratio = _safe(v.iloc[-1] / (vol_ma.iloc[-1] + 1e-9))

        # ATR-14
        tr  = pd.concat(
            [h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1
        ).max(axis=1)
        atr = _safe(tr.rolling(14).mean().iloc[-1])

        # Price (last close)
        price = _safe(c.iloc[-1])

        # Bollinger Band position — needed by backtest_v2 trade record
        # and by scanner signal dict (**best_ind unpacking).
        # Computed here so it is available in the decision group.
        sma20_d = c.rolling(20).mean()
        std20_d = c.rolling(20).std()
        bb_up_d = sma20_d + 2 * std20_d
        bb_lo_d = sma20_d - 2 * std20_d
        bb_rng_d = _safe(bb_up_d.iloc[-1] - bb_lo_d.iloc[-1])
        bb_pos_d = _safe(
            (c.iloc[-1] - bb_lo_d.iloc[-1]) / (bb_rng_d + 1e-9)
            if bb_rng_d and bb_rng_d > 0 else 0.5
        )

        decision = {
            'rsi':       rsi,
            'macd_hist': macd_hist,
            'ema20':     ema20,
            'ema50':     ema50,
            'vwap_dev':  vwap_dev,
            'vol_ratio': vol_ratio,
            'atr':       atr,
            'price':     price,
            'bb_pos':    bb_pos_d,   # used by backtest_v2 trade record
        }

        # Reject if any decision feature is bad — can't make a live decision
        if any(v is None for v in decision.values()):
            return None

        # ── ML-ONLY FEATURES ──────────────────────────────────────────────────
        # Not used in any live signal decision. Logged for future ML work.
        # None values are acceptable — stored as NULL in DB.

        # Bollinger Bands
        sma20   = c.rolling(20).mean()
        std20   = c.rolling(20).std()
        bb_up   = sma20 + 2 * std20
        bb_lo   = sma20 - 2 * std20
        bb_rng  = _safe(bb_up.iloc[-1] - bb_lo.iloc[-1])
        bb_pos  = _safe(
            (c.iloc[-1] - bb_lo.iloc[-1]) / (bb_rng + 1e-9)
            if bb_rng and bb_rng > 0 else 0.5
        )
        bb_width = _safe(
            (bb_rng / (sma20.iloc[-1] + 1e-9)) if bb_rng else None
        )

        # OBV 5-bar slope
        obv = (np.sign(c.diff()) * v).cumsum()
        obv_slope = _safe(
            (obv.iloc[-1] - obv.iloc[-5]) / (abs(obv.iloc[-5]) + 1e-9)
            if len(obv) > 5 else 0.0
        )

        # Price change features
        def _pchg(bars):
            if n < bars + 1:
                return None
            prev = c.iloc[-(bars+1)]
            return _safe((c.iloc[-1] - prev) / (prev + 1e-9))

        pchg_1  = _pchg(1)
        pchg_3  = _pchg(3)
        pchg_5  = _pchg(5)
        pchg_20 = _pchg(20)

        # ADX / +DI / -DI (14)
        adx_val = di_plus_val = di_minus_val = None
        if n >= 28:
            up_move   = h.diff()
            down_move = -lo.diff()
            plus_dm   = pd.Series(
                np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=c.index
            )
            minus_dm  = pd.Series(
                np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=c.index
            )
            atr14     = tr.rolling(14).mean()
            di_plus_s  = 100 * plus_dm.rolling(14).mean()  / (atr14 + 1e-9)
            di_minus_s = 100 * minus_dm.rolling(14).mean() / (atr14 + 1e-9)
            dx         = (100 * (di_plus_s - di_minus_s).abs()
                          / (di_plus_s + di_minus_s + 1e-9))
            adx_s      = dx.rolling(14).mean()
            adx_val      = _safe(adx_s.iloc[-1])
            di_plus_val  = _safe(di_plus_s.iloc[-1])
            di_minus_val = _safe(di_minus_s.iloc[-1])

        # Stochastic %K/%D (14,3)
        stoch_k_val = stoch_d_val = None
        if n >= 17:
            lo14  = lo.rolling(14).min()
            hi14  = h.rolling(14).max()
            k_raw = 100 * (c - lo14) / (hi14 - lo14 + 1e-9)
            k_s   = k_raw.rolling(3).mean()    # smoothed %K
            d_s   = k_s.rolling(3).mean()      # %D
            stoch_k_val = _safe(k_s.iloc[-1])
            stoch_d_val = _safe(d_s.iloc[-1])

        # ROC-10
        roc_10 = _safe(
            (c.iloc[-1] - c.iloc[-11]) / (c.iloc[-11] + 1e-9) * 100
            if n >= 11 else None
        )

        # CCI-20
        cci_val = None
        if n >= 20:
            typical  = (h + lo + c) / 3.0
            sma_typ  = typical.rolling(20).mean()
            mean_dev = typical.rolling(20).apply(
                lambda x: np.mean(np.abs(x - x.mean())), raw=True
            )
            cci_s   = (typical - sma_typ) / (0.015 * mean_dev + 1e-9)
            cci_val = _safe(cci_s.iloc[-1])

        # MFI-14
        mfi_val = None
        if n >= 15:
            typical  = (h + lo + c) / 3.0
            raw_mf   = typical * v
            pos_mf   = raw_mf.where(typical > typical.shift(), 0.0)
            neg_mf   = raw_mf.where(typical < typical.shift(), 0.0)
            mf_ratio = pos_mf.rolling(14).sum() / (neg_mf.rolling(14).sum() + 1e-9)
            mfi_s    = 100 - (100 / (1 + mf_ratio))
            mfi_val  = _safe(mfi_s.iloc[-1])

        # ATR%
        atr_pct = _safe(atr / (price + 1e-9) * 100) if atr and price else None

        # EMA distance
        ema20_dist = _safe(
            (price - ema20) / (ema20 + 1e-9) * 100
        ) if price and ema20 else None
        ema50_dist = _safe(
            (price - ema50) / (ema50 + 1e-9) * 100
        ) if price and ema50 else None

        # Volume z-score
        vol_zscore = None
        if n >= 20:
            vol_mean  = v.rolling(20).mean().iloc[-1]
            vol_std   = v.rolling(20).std().iloc[-1]
            vol_zscore = _safe(
                (v.iloc[-1] - vol_mean) / (vol_std + 1e-9)
                if vol_std and vol_std > 0 else 0.0
            )

        # Candle structure
        candle_range = float(h.iloc[-1] - lo.iloc[-1])
        if candle_range > 0:
            body_pct    = _safe(abs(c.iloc[-1] - o.iloc[-1]) / candle_range)
            upper_wick  = _safe(
                (h.iloc[-1] - max(o.iloc[-1], c.iloc[-1])) / candle_range
            )
            lower_wick  = _safe(
                (min(o.iloc[-1], c.iloc[-1]) - lo.iloc[-1]) / candle_range
            )
        else:
            body_pct = upper_wick = lower_wick = 0.0

        ml = {
            'bb_pos':      bb_pos,
            'bb_width':    bb_width,
            'obv_slope':   obv_slope,
            'pchg_20':     pchg_20,
            'pchg_1':      pchg_1,
            'pchg_3':      pchg_3,
            'pchg_5':      pchg_5,
            'adx':         adx_val,
            'di_plus':     di_plus_val,
            'di_minus':    di_minus_val,
            'stoch_k':     stoch_k_val,
            'stoch_d':     stoch_d_val,
            'roc_10':      roc_10,
            'cci_20':      cci_val,
            'mfi_14':      mfi_val,
            'atr_pct':     atr_pct,
            'ema20_dist':  ema20_dist,
            'ema50_dist':  ema50_dist,
            'vol_zscore':  vol_zscore,
            'body_pct':    body_pct,
            'upper_wick':  upper_wick,
            'lower_wick':  lower_wick,
        }

        return FeatureSet(decision=decision, ml=ml)

    except Exception:
        return None


# ── Backwards-compat shim for existing compute() callers ─────────────────────

def compute(df: pd.DataFrame) -> Optional[dict]:
    """
    Drop-in replacement for bot.indicators.compute().
    Returns the flat decision dict (same keys, same values).
    Used by scanner.py, backtest_v2.py, strategy scoring — callers unchanged.
    """
    fs = compute_features(df)
    if fs is None:
        return None
    return fs.decision
