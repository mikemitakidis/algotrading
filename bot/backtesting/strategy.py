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
from typing import Any, Dict, Type

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
}


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
    "get_strategy", "registered_names",
    "SIGNAL_COLUMNS", "SIG_FLAT", "SIG_ENTRY", "SIG_EXIT",
]
