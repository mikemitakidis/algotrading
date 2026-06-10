"""bot.ml.features — feature computation subpackage.

Every group is a pure function: `bars: DataFrame -> features: DataFrame`,
where the output has THE SAME ts_utc index as the input. Features that
need warmup return NaN at the warmup boundary (never imputed forward).

Hard rules (enforced by tests in test_m18_ml.py G2):
  * leak_class="safe" for every M18.A.2 + M18.A.3 group EXCEPT
    signal_history (leak_class="requires_past_flywheel_only").
  * No center=True rolling; no shift(-N); no future-bar access
  * Output identity: same ts_utc, same row count
  * Bit-identical parity with bot.backtesting.indicators where a
    live_compatible_with counterpart exists (rtol=1e-9, atol=1e-8)
  * Allowed M17.B surfaces inside bot/ml/features/* (M18.A.3 onwards):
    bot.backtesting.mtf_context, bot.backtesting.strategy
    (.ScannerReplicaStrategy), bot.backtesting.indicators.
    NOT allowed: bot.scanner / strategy / feature_engine / indicators
    / sentiment / flywheel; bot.backtesting.execution / portfolio /
    runner (executor surface).
  * Production bot/ml/* does NOT import bot.flywheel or bot.db;
    signal_history goes through bot.ml.dataset.flywheel_reader instead.
  * No I/O, no side effects, deterministic on same input.

Groups shipped:
  M18.A.2 (safe, single-symbol bars only):
    price_return     close, log returns, gaps, body/wick percents,
                       distance-from-rolling-high/low
    trend            sma/ema distances, slopes, alignment flags
    momentum         rsi (live-parity), macd line/signal/hist, roc,
                       momentum acceleration
    vol_regime       atr (live-parity), atr/close, atr percentile,
                       realized vol, bollinger width/position, regime flag
    volume_liquidity volume ratio, z-score, dollar volume, vol shock,
                       liquidity bucket

  M18.A.3 (multi-TF, benchmark, metadata, and flywheel history):
    mtf_confluence   per-anchor TF availability picture (uses M17.B
                       MultiTimeframeContext)
    scanner_replica  per-anchor scanner_replica signal + per-TF flags
                       (uses M17.B ScannerReplicaStrategy)
    market_context   SPY/QQQ regime features (above EMA200, drawdown)
    symbol_meta      static metadata from configs/ml/symbol_metadata*.json
    signal_history   past-only resolved-outcome stats from flywheel DB
                       (the only requires_past_flywheel_only group)
"""
from __future__ import annotations

from bot.ml.features import base  # noqa: F401

# M18.A.2 groups (single-symbol bars only)
from bot.ml.features import price_return as _price_return
from bot.ml.features import trend as _trend
from bot.ml.features import momentum as _momentum
from bot.ml.features import vol_regime as _vol_regime
from bot.ml.features import volume_liquidity as _volume_liquidity

# M18.A.3 groups (multi-TF, benchmark, metadata, flywheel)
from bot.ml.features import mtf_confluence as _mtf_confluence
from bot.ml.features import scanner_replica as _scanner_replica
from bot.ml.features import market_context as _market_context
from bot.ml.features import symbol_meta as _symbol_meta
from bot.ml.features import signal_history as _signal_history


# Group registry: maps group_name -> module providing SPECS + compute.
# Used by the dataset assembler (M18.A.5) to enumerate available
# groups. Single-symbol-bars groups have a compatible compute(bars)
# signature; multi-input groups have additional required kwargs
# documented in each module.
SAFE_FEATURE_GROUPS_V2 = {
    "price_return":     _price_return,
    "trend":            _trend,
    "momentum":         _momentum,
    "vol_regime":       _vol_regime,
    "volume_liquidity": _volume_liquidity,
}

# Groups needing multi-TF, benchmark, metadata, or flywheel inputs.
EXTENDED_FEATURE_GROUPS_V3 = {
    "mtf_confluence":   _mtf_confluence,
    "scanner_replica":  _scanner_replica,
    "market_context":   _market_context,
    "symbol_meta":      _symbol_meta,
    "signal_history":   _signal_history,
}

# Combined registry — every feature group available in M18.A.3.
ALL_FEATURE_GROUPS = {
    **SAFE_FEATURE_GROUPS_V2,
    **EXTENDED_FEATURE_GROUPS_V3,
}

__all__ = ["base", "SAFE_FEATURE_GROUPS_V2",
            "EXTENDED_FEATURE_GROUPS_V3", "ALL_FEATURE_GROUPS"]
