"""bot.ml.dataset.m16_loader — the SOLE importer of bot.historical
in production bot/ml/*.

This invariant is enforced by the G10 AST guard in test_m18_ml.py
(see G10_Hygiene.test_no_forbidden_imports_in_bot_ml AND a
companion test asserting bot.historical appears in NO other
bot/ml/*.py file). Every other M18 module that needs bars MUST
import from here.

Design:
  * Thin pass-through over bot.historical.store.get_bars().
  * Raises M16CoverageError (NOT empty-DataFrame, NOT silent NaN)
    when the M16 store has no bars for the requested window.
  * Provides validate_lookback_coverage() so feature group code can
    sanity-check coverage BEFORE computing a windowed indicator
    (rather than discovering NaN at the end of a 200-bar EMA).
  * NEVER writes. NEVER triggers refresh. NEVER touches yfinance.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Union

import pandas as pd

# This is the only bot.historical import allowed in production bot/ml/*.
# The G10 AST guard explicitly whitelists THIS file.
from bot.historical import store as _m16_store

from bot.ml.errors import M16CoverageError


_TimestampLike = Union[str, datetime, pd.Timestamp, None]

# Required output columns from bot.historical.store.get_bars (verified
# at runtime — if M16's contract ever changes, M18 fails loud).
_REQUIRED_BAR_COLUMNS = ("ts_utc", "open", "high", "low", "close", "volume")


def load_bars(
    symbol: str,
    timeframe: str,
    start_utc: _TimestampLike = None,
    end_utc: _TimestampLike = None,
    *,
    adjusted: bool = True,
    min_rows: int = 1,
    provider: str = "yfinance",
) -> pd.DataFrame:
    """Load OHLCV bars from M16's Parquet store.

    Parameters
    ----------
    symbol      e.g. "AAPL" (case-insensitive; passed straight to M16)
    timeframe   one of "1D" | "4H" | "1H" | "15m"
    start_utc   lower bound INCLUSIVE; None = no lower bound
    end_utc     upper bound EXCLUSIVE; None = no upper bound
    adjusted    True = corporate-action-adjusted O/H/L/C (default).
                  Mirrors bot.historical.store.get_bars semantics.
    min_rows    Raise M16CoverageError if fewer than this many rows
                  are returned. Defaults to 1 (any data at all).
    provider    Reserved; M16 V1 only supports "yfinance".

    Returns
    -------
    pd.DataFrame with columns: ts_utc, open, high, low, close, volume,
        quality_flags. Sorted ascending by ts_utc. Reset index.

    Raises
    ------
    M16CoverageError   if M16 has < min_rows for the requested window.
    ValueError          if timeframe is not in M16's allowed set
                          (propagated from bot.historical.store).
    """
    df = _m16_store.get_bars(
        symbol=symbol,
        timeframe=timeframe,
        start_utc=start_utc,
        end_utc=end_utc,
        provider=provider,
        adjusted=adjusted,
    )
    # Contract check: M16's get_bars must return the documented columns,
    # even on empty result. If this ever fails, M16 has broken its
    # contract and M18 should fail loud, not silently propagate.
    missing = [c for c in _REQUIRED_BAR_COLUMNS if c not in df.columns]
    if missing:
        raise M16CoverageError(
            f"M16 get_bars({symbol!r}, {timeframe!r}) returned a "
            f"DataFrame missing required columns: {missing}. "
            f"Got columns: {list(df.columns)}. This is a contract "
            f"violation in bot.historical.store — investigate before "
            f"running M18 feature/dataset code."
        )

    n = len(df)
    if n < min_rows:
        msg = (
            f"M16 has only {n} bars for {symbol!r} @ {timeframe!r}"
        )
        if start_utc is not None or end_utc is not None:
            msg += f" in window [{start_utc}, {end_utc})"
        # Use the canonical M16-CLI helper so this error string can
        # never drift from the actual CLI surface (verified by
        # G4_M16Backfill.test_command_matches_actual_m16_cli).
        from bot.ml.dataset._m16_backfill import format_backfill_command
        msg += (
            f"; need at least {min_rows}. "
            f"To backfill, run on the VPS:\n"
            f"{format_backfill_command(symbol, timeframe)}"
        )
        raise M16CoverageError(msg)

    # Guarantee: ts_utc is tz-aware UTC and strictly increasing.
    # bot.historical.store already sorts; we double-check defensively
    # because feature code assumes this.
    if not df["ts_utc"].is_monotonic_increasing:
        df = df.sort_values("ts_utc", kind="mergesort").reset_index(
            drop=True)

    return df


def validate_lookback_coverage(
    bars: pd.DataFrame,
    *,
    lookback_bars: int,
    feature_name: str = "<unspecified>",
) -> None:
    """Check that `bars` is long enough to compute a feature whose
    rolling lookback is `lookback_bars`.

    A feature with lookback=N needs at least N+1 bars to produce one
    non-NaN value at the last bar. We require N+1 here (some safety
    margin would just hide bugs).

    Raises M16CoverageError on failure with an explicit message naming
    the feature so the user can see WHICH feature group blew up.

    Use this in feature group code immediately before invoking the
    windowed computation, so the error surface is the feature group,
    not a downstream NaN-only column.
    """
    if not isinstance(lookback_bars, int) or lookback_bars < 0:
        raise ValueError(
            f"lookback_bars must be a non-negative int, "
            f"got {lookback_bars!r}")
    required = lookback_bars + 1
    n = len(bars)
    if n < required:
        raise M16CoverageError(
            f"Feature {feature_name!r} needs {required} bars "
            f"(lookback {lookback_bars} + 1 anchor) but only {n} "
            f"were provided. Backfill M16 or shrink the lookback."
        )


def assert_utc_index(bars: pd.DataFrame) -> None:
    """Defensive: assert the bars frame has a tz-aware UTC ts_utc
    column. Catches malformed inputs before they cause subtle bugs
    in feature compute."""
    if "ts_utc" not in bars.columns:
        raise M16CoverageError(
            "bars frame is missing the ts_utc column — likely not "
            "produced by bot.ml.dataset.m16_loader.load_bars")
    ts = bars["ts_utc"]
    if ts.dt.tz is None:
        raise M16CoverageError(
            "ts_utc is tz-naive; M16 contract is tz-aware UTC. "
            "Did upstream code drop the timezone?")
    if str(ts.dt.tz) not in ("UTC", "tzutc()"):
        # Different repr depending on pandas/python version; accept both.
        try:
            ts_offset = ts.dt.tz.utcoffset(datetime.now(timezone.utc))
        except Exception:
            ts_offset = None
        if ts_offset is None or ts_offset.total_seconds() != 0:
            raise M16CoverageError(
                f"ts_utc timezone is {ts.dt.tz!r}, expected UTC")
