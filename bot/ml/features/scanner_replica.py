"""bot.ml.features.scanner_replica — live-scanner-parity signal features.

All features here are leak_class="safe" — every value depends only
on indicators evaluated at-or-before the anchor (M17.B's
ScannerReplicaStrategy is look-ahead-safe by construction).

Features (8):
  signal_fires              int8     1 if scanner would emit SIG_ENTRY
                                       (long) at this anchor else 0.
  long_count                int8     number of TFs (0..4) whose
                                       per-TF score is 1 for long.
  available_tf_count        int8     number of TFs (0..4) with a
                                       valid indicator row at this
                                       anchor. Matches mtf_confluence
                                       .available_tf_count but ALSO
                                       checks that all 8 indicator
                                       values are finite at the
                                       snapshot bar (a TF can be
                                       "present" in mtf_confluence
                                       but "unavailable" here if it
                                       has not yet warmed up RSI/MACD
                                       etc.).
  confluence_min_valid      int8     the threshold this anchor used
                                       (depends on available count;
                                       1..N per the live formula).
  pass_15m_long             int8     per-TF long score for 15m.
  pass_1h_long              int8     per-TF long score for 1H.
  pass_4h_long              int8     per-TF long score for 4H.
  pass_1d_long              int8     per-TF long score for 1D.

(Shorts are intentionally not surfaced — M17.B's executor is long-only
and the scanner_replica strategy silently skips short confluences in
backtests; ML training data for shorts therefore would not match how
the live system behaves and could mis-train the model.)

DESIGN CHOICE — REUSE OVER REIMPLEMENT:
  This module uses bot.backtesting.strategy.ScannerReplicaStrategy
  for the canonical signal output AND uses the strategy's static
  _score_timeframe_long classmethod for the granular per-TF flags.
  The per-anchor loop here mirrors the strategy's own generate()
  loop so the granular outputs agree exactly with the binary signal.

  Indicators are precomputed via bot.backtesting.indicators (the
  live-parity helpers M17.B tested at rtol=1e-9). NEITHER
  bot.backtesting.execution / portfolio / runner is imported.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

# M17.B surfaces — allowed by the AST guard.
from bot.backtesting import indicators as _ind
from bot.backtesting.mtf_context import MultiTimeframeContext
from bot.backtesting.strategy import ScannerReplicaStrategy

from bot.ml.schemas import FeatureSpec
from bot.ml.features.base import align_to_bars


GROUP_NAME = "scanner_replica"
GROUP_VERSION = 1


def _spec(name: str, *, dtype: str = "int8",
           desc: str, value_range=None) -> FeatureSpec:
    return FeatureSpec(
        feature_id=f"{GROUP_NAME}.{name}",
        feature_group=GROUP_NAME,
        feature_group_version=GROUP_VERSION,
        dtype=dtype,
        leak_class="safe",
        lookback_bars=0,             # depends on underlying TFs;
                                       # validated upstream
        lookback_unit="bars_at_this_tf",
        computed_from=("__multi_tf_bars__",),
        description=desc,
        value_range=value_range,
        live_compatible=True,
        live_compatible_with="bot.scanner.score_timeframe + scan_cycle"
                              " via bot.backtesting.strategy"
                              ".ScannerReplicaStrategy",
        tested_in="test_m18_ml.py::G2_ScannerReplica",
    )


SPECS: tuple = (
    _spec("signal_fires",
            desc="1 if scanner_replica would emit SIG_ENTRY (long)",
            value_range=(0.0, 1.0)),
    _spec("long_count",
            desc="count of TFs (0..4) whose per-TF score is 1 for long",
            value_range=(0.0, 4.0)),
    _spec("available_tf_count",
            desc="count of TFs (0..4) with finite indicator rows at"
                  " the anchor",
            value_range=(0.0, 4.0)),
    _spec("confluence_min_valid",
            desc="confluence threshold used at this anchor (1..N)",
            value_range=(1.0, 4.0)),
    _spec("pass_15m_long",
            desc="per-TF long score for 15m (1 or 0)",
            value_range=(0.0, 1.0)),
    _spec("pass_1h_long",
            desc="per-TF long score for 1H (1 or 0)",
            value_range=(0.0, 1.0)),
    _spec("pass_4h_long",
            desc="per-TF long score for 4H (1 or 0)",
            value_range=(0.0, 1.0)),
    _spec("pass_1d_long",
            desc="per-TF long score for 1D (1 or 0)",
            value_range=(0.0, 1.0)),
)


def _precompute_indicators(ctx: MultiTimeframeContext
                             ) -> Dict[str, Dict[str, pd.Series]]:
    """Per-TF indicator dict — mirrors ScannerReplicaStrategy's own
    _precompute_indicators (which is a method, so we redo the same
    work here to avoid touching a private API)."""
    out: Dict[str, Dict[str, pd.Series]] = {}
    for tf in ctx.available_timeframes:
        df = ctx._per_tf_bars[tf]
        c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
        out[tf] = {
            "rsi":       _ind.rsi(c, period=14, mode="sma_gain_loss"),
            "macd_hist": _ind.macd(c, fast=12, slow=26,
                                    signal=9)["hist"],
            "ema20":     _ind.ema(c, window=20),
            "ema50":     _ind.ema(c, window=50),
            "vwap_dev":  _ind.vwap_dev(c, v),
            "vol_ratio": _ind.volume_ratio(v, window=20),
            "atr":       _ind.atr(h, l, c, period=14,
                                   mode="sma_true_range"),
            "price":     c,
        }
    return out


def compute(bars: pd.DataFrame, *,
              per_tf_bars: Dict[str, pd.DataFrame],
              anchor_tf: str = "15m",
              strategy_params: Optional[Dict[str, Any]] = None,
              ) -> pd.DataFrame:
    """Compute scanner_replica features for `bars` (anchor TF bars).

    Parameters
    ----------
    bars             anchor TF bars; index defines the output rows.
    per_tf_bars      dict TF_label -> bars; same contract as M17.B
                       MultiTimeframeContext.
    anchor_tf        anchor TF label (default '15m').
    strategy_params  override params for ScannerReplicaStrategy;
                       defaults to ScannerReplicaStrategy.default_params
                       (the live scanner's canonical config).

    Returns
    -------
    pd.DataFrame indexed identically to `bars` with the 8 features.
    """
    if strategy_params is None:
        strategy_params = dict(ScannerReplicaStrategy.default_params)
    ctx = MultiTimeframeContext(per_tf_bars=per_tf_bars,
                                 anchor_tf=anchor_tf)
    precomp = _precompute_indicators(ctx)

    cfg_long  = strategy_params["long"]
    cfg_min   = int(strategy_params["confluence"]["min_valid_tfs"])
    total_tfs = len(strategy_params["timeframes"])

    n = len(bars)
    signal_fires      = np.zeros(n, dtype=np.int8)
    long_count        = np.zeros(n, dtype=np.int8)
    available_count   = np.zeros(n, dtype=np.int8)
    confluence_min    = np.zeros(n, dtype=np.int8)
    pass_15m          = np.zeros(n, dtype=np.int8)
    pass_1h           = np.zeros(n, dtype=np.int8)
    pass_4h           = np.zeros(n, dtype=np.int8)
    pass_1d           = np.zeros(n, dtype=np.int8)

    anchor_ts_series = pd.to_datetime(bars["ts_utc"], utc=True)

    for i in range(n):
        anchor_ts = anchor_ts_series.iloc[i]
        snap = ctx.snapshot_at(anchor_ts)

        tf_inds: Dict[str, Dict[str, float]] = {}
        # Build the per-TF indicator dicts using the strategy's own
        # "all finite or skip this TF" rule.
        for tf, sb in snap.items():
            if sb is None:
                continue
            row: Dict[str, float] = {}
            bad = False
            for key in ("rsi", "macd_hist", "ema20", "ema50",
                          "vwap_dev", "vol_ratio", "atr", "price"):
                v = precomp[tf][key].iloc[sb.idx]
                if not np.isfinite(v):
                    bad = True
                    break
                row[key] = float(v)
            if bad:
                continue
            tf_inds[tf] = row

        avail = len(tf_inds)
        available_count[i] = avail
        if avail == 0:
            confluence_min[i] = 1   # min_valid degenerate case
            continue

        # Per-TF long score using the strategy's static method
        lc = 0
        for tf, row in tf_inds.items():
            score = ScannerReplicaStrategy._score_timeframe_long(
                row, cfg_long)
            if tf == "15m":
                pass_15m[i] = score
            elif tf == "1H":
                pass_1h[i] = score
            elif tf == "4H":
                pass_4h[i] = score
            elif tf == "1D":
                pass_1d[i] = score
            lc += score
        long_count[i] = lc

        mv = ScannerReplicaStrategy.confluence_min_valid(
            available_tfs=avail, total_tfs=total_tfs, cfg_min=cfg_min)
        confluence_min[i] = mv
        if lc >= mv:
            signal_fires[i] = 1

    out = pd.DataFrame(index=bars.index)
    out[f"{GROUP_NAME}.signal_fires"]         = signal_fires
    out[f"{GROUP_NAME}.long_count"]           = long_count
    out[f"{GROUP_NAME}.available_tf_count"]   = available_count
    out[f"{GROUP_NAME}.confluence_min_valid"] = confluence_min
    out[f"{GROUP_NAME}.pass_15m_long"]        = pass_15m
    out[f"{GROUP_NAME}.pass_1h_long"]         = pass_1h
    out[f"{GROUP_NAME}.pass_4h_long"]         = pass_4h
    out[f"{GROUP_NAME}.pass_1d_long"]         = pass_1d
    return align_to_bars(out, bars, group_name=GROUP_NAME)


def parity_check_with_strategy(
    bars: pd.DataFrame, *,
    per_tf_bars: Dict[str, pd.DataFrame],
    anchor_tf: str = "15m",
) -> pd.DataFrame:
    """Helper for tests: invoke ScannerReplicaStrategy.generate
    directly and return its DataFrame for comparison. NOT used in
    production assembly — kept here so test_m18_ml's parity test can
    compare the M18 feature output against the M17.B strategy output
    without duplicating setup boilerplate.
    """
    ctx = MultiTimeframeContext(per_tf_bars=per_tf_bars,
                                 anchor_tf=anchor_tf)
    strat = ScannerReplicaStrategy(
        dict(ScannerReplicaStrategy.default_params))
    strat.attach_context(ctx)
    return strat.generate(bars)
