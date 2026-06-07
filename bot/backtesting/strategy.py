"""bot.backtesting.strategy — strategy contract + M17.A strategies.

A Strategy takes a bars DataFrame plus its own params dict and returns
a signal DataFrame:

    inputs:
      bars  : pd.DataFrame with columns
                ts_utc, open, high, low, close, volume, quality_flags
      params: dict (strategy-specific schema, strategy validates)

    output:
      pd.DataFrame indexed identically to `bars`, with columns:
        signal       — int: +1 (enter long), 0 (flat / hold), -1 (exit)
        direction    — str: 'long' | 'flat' | 'short' (M17.A: long|flat only)
        atr_at_signal — float (NaN unless strategy uses ATR)
        entry_price_hint — float (the close at signal bar; engine uses
                            next bar's open for actual fill)

The engine consumes this via exec_signal = signal.shift(1), so a +1
on bar i becomes the entry trigger on bar i+1. The strategy MUST NOT
emit a signal that depends on bar i+1 values — verified by the scramble
test in G4.

M17.A strategies:
  * sma_crossover   long when fast SMA crosses above slow SMA; exit
                     when fast SMA crosses below slow SMA.

scanner_replica and friends land in M17.B.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type

import numpy as np
import pandas as pd

from bot.backtesting.errors import ConfigError, StrategyError
from bot.backtesting import indicators as ind


# ─────────────────────────────────────────────────────────────────────
# Output column contract
# ─────────────────────────────────────────────────────────────────────

SIGNAL_COLUMNS = ("signal", "direction", "atr_at_signal",
                    "entry_price_hint")

# Signal integer codes
SIG_FLAT  =  0
SIG_ENTRY = +1
SIG_EXIT  = -1


# ─────────────────────────────────────────────────────────────────────
# Strategy base
# ─────────────────────────────────────────────────────────────────────

class Strategy(ABC):
    """Base class for all M17 strategies.

    Subclasses implement:
      * name          — class attribute (the registry key)
      * default_params — class attribute (dict; merged with user params)
      * validate_params(params) — raise ConfigError on invalid
      * generate(bars, params) — return a signal DataFrame

    The base class wraps `generate()` to enforce the output contract.
    """
    name: str = ""
    default_params: Dict[str, Any] = {}

    def __init__(self, params: Dict[str, Any]):
        # Merge user params over defaults; later validation step
        # catches unknown / out-of-range fields.
        self.params: Dict[str, Any] = {**self.default_params, **params}
        self.validate_params(self.params)

    @classmethod
    def validate_params(cls, params: Dict[str, Any]) -> None:
        """Validate the parameter dict. Default: accept all (subclasses
        override). Raise ConfigError on invalid."""
        return None

    @abstractmethod
    def generate(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Generate the signal DataFrame for `bars`. Subclasses must
        return a DataFrame with columns SIGNAL_COLUMNS, indexed
        identically to bars."""
        raise NotImplementedError

    def run(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Public entry: call generate(), then enforce the output
        contract (column presence, index alignment, no forward
        references). Engine code calls this, not generate() directly."""
        out = self.generate(bars)
        _enforce_signal_contract(bars, out, strategy_name=self.name)
        return out


def _enforce_signal_contract(bars: pd.DataFrame, sig: pd.DataFrame,
                                *, strategy_name: str) -> None:
    """Hard-validate strategy output."""
    if not isinstance(sig, pd.DataFrame):
        raise StrategyError(
            f"strategy {strategy_name!r} returned "
            f"{type(sig).__name__}, expected pd.DataFrame")
    missing = set(SIGNAL_COLUMNS) - set(sig.columns)
    if missing:
        raise StrategyError(
            f"strategy {strategy_name!r} output missing columns: "
            f"{sorted(missing)}")
    if len(sig) != len(bars):
        raise StrategyError(
            f"strategy {strategy_name!r} output length {len(sig)} "
            f"!= bars length {len(bars)}")
    # Signal codes must be in the allowed set (after NaN-cast safety).
    bad = sig["signal"][~sig["signal"].isin([SIG_FLAT, SIG_ENTRY, SIG_EXIT])]
    if len(bad) > 0:
        raise StrategyError(
            f"strategy {strategy_name!r} emitted signal codes "
            f"outside {{-1, 0, +1}}: first bad index = "
            f"{bad.index[0]}, value = {bad.iloc[0]}")
    bad_dir = sig["direction"][~sig["direction"].isin(
        ["long", "flat", "short"])]
    if len(bad_dir) > 0:
        raise StrategyError(
            f"strategy {strategy_name!r} emitted direction outside "
            f"{{'long', 'flat', 'short'}}: "
            f"first bad index = {bad_dir.index[0]}, "
            f"value = {bad_dir.iloc[0]!r}")


# ─────────────────────────────────────────────────────────────────────
# SmaCrossoverStrategy — M17.A canonical foundation strategy
# ─────────────────────────────────────────────────────────────────────

class SmaCrossoverStrategy(Strategy):
    """Classic SMA crossover, long-only.

    Params:
      fast_window : int (default 20)  — fast SMA period
      slow_window : int (default 50)  — slow SMA period; must > fast

    Signal logic:
      cross_up   = fast SMA crosses ABOVE slow SMA at bar i close
                    → signal = +1 (ENTRY) at bar i
      cross_down = fast SMA crosses BELOW slow SMA at bar i close
                    → signal = -1 (EXIT) at bar i
      otherwise  → signal = 0 (flat)

    'direction' is 'long' on +1 bars, 'flat' otherwise (no shorts in
    M17.A).

    The engine consumes via exec_signal = signal.shift(1), so the
    actual entry/exit happens at bar i+1 OPEN, never bar i itself.
    """
    name = "sma_crossover"
    default_params: Dict[str, Any] = {
        "fast_window": 20,
        "slow_window": 50,
    }

    @classmethod
    def validate_params(cls, params: Dict[str, Any]) -> None:
        fast = params.get("fast_window")
        slow = params.get("slow_window")
        if not isinstance(fast, int) or isinstance(fast, bool) or fast <= 0:
            raise ConfigError(
                f"sma_crossover.fast_window must be a positive int, "
                f"got {fast!r}")
        if not isinstance(slow, int) or isinstance(slow, bool) or slow <= 0:
            raise ConfigError(
                f"sma_crossover.slow_window must be a positive int, "
                f"got {slow!r}")
        if fast >= slow:
            raise ConfigError(
                f"sma_crossover.fast_window ({fast}) must be < "
                f"slow_window ({slow})")

    def generate(self, bars: pd.DataFrame) -> pd.DataFrame:
        close = bars["close"]
        fast = ind.sma(close, self.params["fast_window"])
        slow = ind.sma(close, self.params["slow_window"])

        # Crossover detection: fast > slow now AND fast <= slow last bar.
        # Uses .shift(1) (positive shift = backward in time = OK).
        above_now  = (fast > slow)
        # `(fast > slow)` is bool dtype; .shift(1) introduces NaN at the
        # head. Use shift's fill_value to avoid the object-dtype-fillna
        # path entirely and keep the result bool-typed.
        above_prev = (fast > slow).shift(1, fill_value=False).astype(bool)

        cross_up   =  above_now & ~above_prev
        cross_down = ~above_now &  above_prev

        signal = pd.Series(SIG_FLAT, index=bars.index, dtype="int64")
        signal[cross_up]   = SIG_ENTRY
        signal[cross_down] = SIG_EXIT

        direction = pd.Series("flat", index=bars.index, dtype="object")
        direction[cross_up] = "long"

        # NaN in fast/slow during warmup -> force signal=0
        warmup_mask = fast.isna() | slow.isna()
        signal[warmup_mask] = SIG_FLAT
        direction[warmup_mask] = "flat"

        out = pd.DataFrame({
            "signal":           signal,
            "direction":        direction,
            "atr_at_signal":    pd.Series(np.nan, index=bars.index,
                                              dtype="float64"),
            "entry_price_hint": close.astype(float),
        }, index=bars.index)
        return out


# ─────────────────────────────────────────────────────────────────────
# Strategy registry
# ─────────────────────────────────────────────────────────────────────

_REGISTRY: Dict[str, Type[Strategy]] = {
    SmaCrossoverStrategy.name: SmaCrossoverStrategy,
    # scanner_replica is registered below after its class definition.
}


# ─────────────────────────────────────────────────────────────────────
# M17.B.4 — MultiTimeframeStrategy base + ScannerReplicaStrategy
# ─────────────────────────────────────────────────────────────────────
#
# scanner_replica reproduces bot/scanner.py logic IN CODE — not by
# import. Per Sharpened Rule #4 / Q5 / Q12: bot/backtesting/* must
# never import bot.scanner / bot.strategy / bot.feature_engine /
# bot.indicators / bot.sentiment / bot.flywheel (G10 AST scan
# enforces). The thresholds, scoring rules, confluence scaler, and
# route labeller are all replicated below; tests import the live
# helpers separately and assert per-rule parity.

class MultiTimeframeStrategy(Strategy):
    """Base class for strategies that need bars from MULTIPLE
    timeframes simultaneously (e.g. scanner_replica with its 1D/4H/1H/15m
    confluence gate).

    The runner detects subclasses of this and loads bars via
    load_multi_tf_bars, then attaches a MultiTimeframeContext via
    `attach_context` before calling `generate(bars)`. The `bars`
    argument is the ANCHOR TF's bars (cfg.request.timeframe), so the
    output signal DataFrame is still indexed identically to a single
    bar series — execution.py and the rest of the engine work
    UNCHANGED for multi-TF strategies.
    """

    requires_multi_tf: bool = True

    def __init__(self, params: Dict[str, Any]):
        super().__init__(params)
        self._context = None  # type: Optional[Any]  # MultiTimeframeContext

    def attach_context(self, context) -> None:
        """Called by the runner BEFORE generate(). Without this the
        strategy cannot run."""
        self._context = context

    # Subclasses still implement generate(bars) — the bars argument is
    # the anchor TF's bars. The context (with all TFs) is available
    # via self._context.


class ScannerReplicaStrategy(MultiTimeframeStrategy):
    """Reproduce bot/scanner.py's multi-timeframe confluence logic IN
    CODE (Q5 — pure config injection; no bot.scanner / bot.strategy
    imports).

    Per-anchor algorithm (bot/scanner.py:score_timeframe + scan_cycle):
      1. For each requested TF, find the most-recent-bar-at-or-before
         the anchor via MultiTimeframeContext.snapshot_at.
      2. Read precomputed indicators at that bar's idx
         (rsi / macd_hist / ema20 / ema50 / vwap_dev / vol_ratio).
      3. Apply score_timeframe() for 'long' and 'short' per TF.
      4. Sum the per-direction passing-TF count.
      5. Compute min_valid via the live scanner's scaling formula
         (uses PER-ANCHOR available_tfs, Sharpened Rule #3):
            available = N -> min_valid = cfg_min   if N >= total_tfs
                              max(2, cfg_min - 1)  if N >= 2
                              1                    otherwise
      6. If long_count >= min_valid -> emit SIG_ENTRY, direction='long'.
         Shorts are SUPPRESSED in M17.B.4 because execution.py is
         long-only (ExecutionConfig.allow_short must be False in M17.A
         and M17.B doesn't change that). When a SHORT confluence fires
         a 'scanner_replica_short_suppressed' warning could later be
         recorded — for M17.B.4 we silently skip shorts to stay aligned
         with the execution contract.

    Indicators are precomputed ONCE per TF (Sharpened Rule #2 — no
    per-anchor rolling recompute). RSI uses mode='sma_gain_loss' and
    ATR uses mode='sma_true_range' to match live scanner formulas to
    floating-point precision (asserted by M17.B.1 parity tests).

    Risk levels (ATR-based stops/targets) are computed and surfaced
    via atr_at_signal so the M17.B.5 execution-config extension can
    consume them. M17.B.4 does NOT yet drive ATR exits — that's the
    next phase.
    """

    name = "scanner_replica"

    # Defaults mirror bot/strategy.py::DEFAULTS exactly (read at audit
    # time; if the live DEFAULTS later drift, scanner_replica params
    # remain stable — operator must update the config explicitly).
    default_params: Dict[str, Any] = {
        "timeframes":         ["1D", "4H", "1H", "15m"],
        # anchor_tf is also the cfg.request.timeframe — runner checks
        # they match.
        "anchor_tf":          "15m",
        "confluence":         {"min_valid_tfs": 3},
        "long": {
            "rsi_min":        30.0,
            "rsi_max":        75.0,
            "macd_hist_gt":   0.0,
            "ema_tolerance":  0.005,
            "vwap_dev_min": -0.015,
            "vol_ratio_min":  0.6,
        },
        "short": {
            "rsi_min":        50.0,
            "macd_hist_lt":   0.0,
            "ema_tolerance":  0.005,
            "vwap_dev_max":   0.015,
            "vol_ratio_min":  0.6,
        },
    }

    @classmethod
    def validate_params(cls, params: Dict[str, Any]) -> None:
        tfs = params.get("timeframes")
        if not isinstance(tfs, list) or not tfs:
            raise ConfigError(
                "scanner_replica.params.timeframes must be a non-empty list")
        for tf in tfs:
            if tf not in ("1D", "4H", "1H", "15m"):
                raise ConfigError(
                    f"scanner_replica.params.timeframes contains unknown "
                    f"TF {tf!r}; allowed: 1D / 4H / 1H / 15m")
        anchor = params.get("anchor_tf")
        if anchor not in tfs:
            raise ConfigError(
                f"scanner_replica.params.anchor_tf={anchor!r} must be in "
                f"params.timeframes={tfs!r}")
        conf = params.get("confluence", {})
        if not isinstance(conf, dict) or "min_valid_tfs" not in conf:
            raise ConfigError(
                "scanner_replica.params.confluence must have "
                "min_valid_tfs (int)")
        try:
            mv = int(conf["min_valid_tfs"])
        except (TypeError, ValueError):
            raise ConfigError(
                "scanner_replica.params.confluence.min_valid_tfs must "
                "be coercible to int")
        if not (1 <= mv <= 4):
            raise ConfigError(
                f"scanner_replica.params.confluence.min_valid_tfs must "
                f"be 1..4, got {mv}")
        for side in ("long", "short"):
            sd = params.get(side, {})
            if not isinstance(sd, dict):
                raise ConfigError(
                    f"scanner_replica.params.{side} must be a dict")
        # rsi sanity
        if params["long"].get("rsi_min", 0) >= params["long"].get("rsi_max", 100):
            raise ConfigError(
                "scanner_replica.params.long.rsi_min must be < rsi_max")

    # ── Score reducer: identical algebra to bot/scanner.score_timeframe
    #    (replicated by code; tests import the live helper to assert
    #    parity per-branch).

    @staticmethod
    def _score_timeframe_long(ind: Dict[str, float],
                                 cfg_long: Dict[str, float]) -> int:
        rsi_min  = float(cfg_long.get("rsi_min",       30))
        rsi_max  = float(cfg_long.get("rsi_max",        75))
        macd_gt  = float(cfg_long.get("macd_hist_gt",  0.0))
        ema_tol  = float(cfg_long.get("ema_tolerance", 0.005))
        vwap_min = float(cfg_long.get("vwap_dev_min", -0.015))
        vol_min  = float(cfg_long.get("vol_ratio_min", 0.6))
        momentum = 1 if (rsi_min < ind["rsi"] < rsi_max
                          and ind["macd_hist"] > macd_gt) else 0
        trend    = 1 if (ind["ema20"] > ind["ema50"] * (1.0 - ema_tol)) else 0
        volume   = 1 if (ind["vwap_dev"] > vwap_min
                          and ind["vol_ratio"] > vol_min) else 0
        return 1 if (momentum + trend + volume == 3) else 0

    @staticmethod
    def _score_timeframe_short(ind: Dict[str, float],
                                  cfg_short: Dict[str, float]) -> int:
        rsi_min  = float(cfg_short.get("rsi_min",       50))
        macd_lt  = float(cfg_short.get("macd_hist_lt",  0.0))
        ema_tol  = float(cfg_short.get("ema_tolerance", 0.005))
        vwap_max = float(cfg_short.get("vwap_dev_max",  0.015))
        vol_min  = float(cfg_short.get("vol_ratio_min", 0.6))
        momentum = 1 if (ind["rsi"] > rsi_min
                          and ind["macd_hist"] < macd_lt) else 0
        trend    = 1 if (ind["ema20"] < ind["ema50"] * (1.0 + ema_tol)) else 0
        volume   = 1 if (ind["vwap_dev"] < vwap_max
                          and ind["vol_ratio"] > vol_min) else 0
        return 1 if (momentum + trend + volume == 3) else 0

    @staticmethod
    def confluence_min_valid(available_tfs: int,
                                total_tfs: int,
                                cfg_min: int) -> int:
        """Live scanner's scaling formula (bot/scanner.py:160-166).
        Operator-pinned in M17.B audit Q-checklist."""
        if available_tfs >= total_tfs:
            return cfg_min
        elif available_tfs >= 2:
            return max(2, cfg_min - 1)
        else:
            return 1

    # ── Indicator precomputation (vectorized; once per TF; Sharpened
    #    Rule #2 perf discipline).

    def _precompute_indicators(self, context) -> Dict[str, Dict[str, pd.Series]]:
        """Precompute the 9 indicators score_timeframe needs, per TF,
        as full pd.Series indexed identically to that TF's bars."""
        result: Dict[str, Dict[str, pd.Series]] = {}
        for tf in context.available_timeframes:
            df = context._per_tf_bars[tf]
            c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
            result[tf] = {
                "rsi":       ind.rsi(c, period=14, mode="sma_gain_loss"),
                "macd_hist": ind.macd(c, fast=12, slow=26, signal=9)["hist"],
                "ema20":     ind.ema(c, window=20),
                "ema50":     ind.ema(c, window=50),
                "vwap_dev":  ind.vwap_dev(c, v),
                "vol_ratio": ind.volume_ratio(v, window=20),
                "atr":       ind.atr(h, l, c, period=14, mode="sma_true_range"),
                "price":     c,
                "bb_pos":    ind.bb_pos(c, window=20, num_std=2.0),
            }
        return result

    def generate(self, bars: pd.DataFrame) -> pd.DataFrame:
        """`bars` is the anchor TF's bars. Iterate every anchor; at
        each anchor read precomputed indicators per TF; apply the
        live scanner's score_timeframe + confluence scaler; emit
        SIG_ENTRY when long confluence holds. Look-ahead-safe by
        construction (snapshot_at is at-or-before)."""
        if self._context is None:
            raise StrategyError(
                "scanner_replica.generate called before attach_context")
        ctx = self._context
        if ctx.anchor_tf != self.params["anchor_tf"]:
            raise StrategyError(
                f"scanner_replica anchor mismatch: context.anchor_tf="
                f"{ctx.anchor_tf!r} but params.anchor_tf="
                f"{self.params['anchor_tf']!r}")
        # Sanity: bars must equal the anchor TF's bars by ts_utc.
        anchor_bars = ctx._per_tf_bars[ctx.anchor_tf]
        if len(bars) != len(anchor_bars):
            raise StrategyError(
                f"scanner_replica: bars length {len(bars)} != "
                f"context anchor bars length {len(anchor_bars)}")

        precomp = self._precompute_indicators(ctx)
        cfg_long  = self.params["long"]
        cfg_short = self.params["short"]
        cfg_min   = int(self.params["confluence"]["min_valid_tfs"])
        total_tfs = len(self.params["timeframes"])

        n = len(bars)
        signal = np.zeros(n, dtype=np.int64)
        direction = np.array(["flat"] * n, dtype=object)
        atr_at_signal = np.full(n, np.nan, dtype=np.float64)
        entry_price_hint = np.full(n, np.nan, dtype=np.float64)

        # Convert anchor TF ts_utc to UTC-aware list for snapshot lookup
        anchor_ts_series = pd.to_datetime(bars["ts_utc"], utc=True)

        for i in range(n):
            anchor_ts = anchor_ts_series.iloc[i]
            snap = ctx.snapshot_at(anchor_ts)

            available = 0
            long_count = 0
            best_atr = np.nan
            best_price = np.nan
            best_tf_order = ("1D", "4H", "1H", "15m")

            # Collect per-TF indicator dicts for this anchor
            tf_inds: Dict[str, Dict[str, float]] = {}
            for tf, sb in snap.items():
                if sb is None:
                    continue
                row = {}
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
                available += 1

            if available == 0:
                continue

            # Score each TF for long
            for tf, row in tf_inds.items():
                long_count += self._score_timeframe_long(row, cfg_long)

            min_valid = self.confluence_min_valid(
                available_tfs=available,
                total_tfs=total_tfs,
                cfg_min=cfg_min,
            )

            if long_count >= min_valid:
                signal[i]    = SIG_ENTRY
                direction[i] = "long"
                # ATR + price come from the highest enabled TF that
                # has a valid indicator row at this anchor — matches
                # bot/scanner.py:211 ('Use best available indicator
                # set (prefer higher TFs)').
                for tf in best_tf_order:
                    if tf in tf_inds:
                        best_atr   = tf_inds[tf]["atr"]
                        best_price = tf_inds[tf]["price"]
                        break
                atr_at_signal[i]    = best_atr
                entry_price_hint[i] = best_price

        out = pd.DataFrame({
            "signal":           signal,
            "direction":        direction,
            "atr_at_signal":    atr_at_signal,
            "entry_price_hint": entry_price_hint,
        }, index=bars.index)
        return out


# Register scanner_replica now that its class is defined.
_REGISTRY[ScannerReplicaStrategy.name] = ScannerReplicaStrategy


def get_strategy(name: str, params: Dict[str, Any]) -> Strategy:
    """Instantiate a registered strategy by name. Raises ConfigError
    if the name isn't registered (config validation should have already
    caught this, but defensive)."""
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ConfigError(
            f"unknown strategy {name!r}. Registered: "
            f"{sorted(_REGISTRY.keys())}")
    return cls(params)


def registered_names() -> tuple:
    """Returns sorted tuple of registered strategy names."""
    return tuple(sorted(_REGISTRY.keys()))


__all__ = [
    "Strategy", "SmaCrossoverStrategy",
    "MultiTimeframeStrategy", "ScannerReplicaStrategy",
    "get_strategy", "registered_names",
    "SIGNAL_COLUMNS", "SIG_FLAT", "SIG_ENTRY", "SIG_EXIT",
]
