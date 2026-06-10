"""bot.ml.features — feature computation subpackage.

Every group is a pure function: `bars: DataFrame -> features: DataFrame`,
where the output has THE SAME ts_utc index as the input. Features that
need warmup return NaN at the warmup boundary (never imputed forward).

Hard rules (enforced by tests in test_m18_ml.py G2):
  * leak_class="safe" for every feature shipped in M18.A.2
  * No center=True rolling; no shift(-N); no future-bar access
  * Output identity: same ts_utc, same row count
  * Bit-identical parity with bot.backtesting.indicators where a
    live_compatible_with counterpart exists (rtol=1e-9, atol=1e-8)
  * No bot.historical/bot.scanner/bot.strategy/bot.feature_engine/
    bot.indicators imports anywhere in this subpackage (G10 AST guard)
  * No I/O, no side effects, deterministic on same input

Groups shipped in M18.A.2:
  price_return     close, log returns, gaps, body/wick percents,
                     distance-from-rolling-high/low
  trend            sma/ema distances, slopes, alignment flags
  momentum         rsi (live-parity), macd line/signal/hist, roc,
                     momentum acceleration
  vol_regime       atr (live-parity), atr/close, atr percentile,
                     realized vol, bollinger width/position, regime flag
  volume_liquidity volume ratio, z-score, dollar volume, vol shock,
                     liquidity bucket

NOT shipped in M18.A.2 (per phase plan):
  mtf_confluence   scanner_replica multi-TF score — M18.A.3
  scanner_replica  full per-anchor scanner_replica state — M18.A.3
  market_context   SPY/QQQ-relative features — M18.A.3
  symbol_meta      static metadata — M18.A.3
  signal_history   past-only signal stats — M18.A.3
"""
from __future__ import annotations

from bot.ml.features import base  # noqa: F401
from bot.ml.features import price_return as _price_return
from bot.ml.features import trend as _trend
from bot.ml.features import momentum as _momentum
from bot.ml.features import vol_regime as _vol_regime
from bot.ml.features import volume_liquidity as _volume_liquidity

# Group registry: maps group_name -> compute callable + spec list
# Used by the dataset assembler (M18.A.5) to enumerate available groups.
SAFE_FEATURE_GROUPS_V2 = {
    "price_return":     _price_return,
    "trend":            _trend,
    "momentum":         _momentum,
    "vol_regime":       _vol_regime,
    "volume_liquidity": _volume_liquidity,
}

__all__ = ["base", "SAFE_FEATURE_GROUPS_V2"]
