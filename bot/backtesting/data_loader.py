"""bot.backtesting.data_loader — load M16 bars for a backtest.

This is the ONLY module in bot.backtesting that touches bot.historical.
G2 / G10 AST scans assert this hard invariant.

Date semantics conversion
─────────────────────────
The CLI / JSON config uses INCLUSIVE dates on BOTH ends (operator-natural).
The M16 store (`bot.historical.store.get_bars`) uses INCLUSIVE start,
EXCLUSIVE end (Pythonic).

Conversion happens in this module:
    M16.start_utc = request.start           (00:00:00 UTC of that day)
    M16.end_utc   = request.end + 1 day     (00:00:00 UTC of NEXT day,
                                              exclusive — so all bars
                                              whose ts_utc falls on
                                              request.end are included)

This is the SOLE place where the conversion happens. Documented in
the function docstrings; tested at the boundary in G2.

Coverage gate
─────────────
Before reading bars, we check `bot.historical.store.get_coverage` and
enforce:

  HARD FAILURES (raise MissingDataError with refresh command):
    * no coverage row at all                    → never refreshed
    * first_ts_utc > request.start              → starts too late
    * last_ts_utc  < request.end                → ends too early
    * missing_count > 0                         → known gaps
    * quality_status == 'error'                 → corrupt data
    * loaded df is empty                        → nothing in window
    * loaded df contains NaN OHLC               → corrupt rows
    * loaded df has duplicate timestamps        → schema violation

  WARNINGS (recorded in result.warnings, never block):
    * quality_status == 'warn'                  → soft quality issue
    * freshness_status != 'fresh'               → data is stale
    * coverage row's last_ts_utc is significantly
       past request.end                          (informational only)

Returned DataFrame is sorted ascending by ts_utc, UTC-aware, with
columns: ts_utc, open, high, low, close, volume, quality_flags.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import pandas as pd

# The ONE import that this module is allowed to make from bot.historical.
# G2 / G10 AST scans assert nothing else imports from there.
from bot.historical import store as _m16_store

from bot.backtesting.config import BacktestConfig
from bot.backtesting.errors import MissingDataError
from bot.backtesting.models import BacktestWarning


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

def load_backtest_bars(
    cfg: BacktestConfig,
) -> Tuple[pd.DataFrame, Dict[str, Any], List[BacktestWarning]]:
    """Load bars from the M16 store for a backtest configuration.

    Returns a tuple:
      (bars_df, coverage_metadata, warnings)

    On any HARD FAILURE condition (see module docstring), raises
    MissingDataError with the exact `python -m bot.historical.cli
    backfill` command the operator needs to run.

    The bars DataFrame columns: ts_utc, open, high, low, close, volume,
    quality_flags. ts_utc is UTC-aware. Sorted ascending.
    """
    warnings: List[BacktestWarning] = []

    # ---- 1. coverage gate -------------------------------------------
    coverage = validate_coverage(cfg)

    # 'warn' quality status doesn't block, but goes in warnings.
    qstatus = coverage.get("quality_status")
    if qstatus == "warn":
        warnings.append(BacktestWarning(
            code="m16_quality_warn",
            message=(f"M16 coverage reports quality_status='warn' for "
                      f"{cfg.request.symbol} {cfg.request.timeframe}; "
                      f"backtest will continue but results may be "
                      f"affected."),
        ))

    fstatus = coverage.get("freshness_status")
    if fstatus and fstatus != "fresh":
        warnings.append(BacktestWarning(
            code="m16_freshness_warn",
            message=(f"M16 coverage reports freshness_status="
                      f"{fstatus!r} for {cfg.request.symbol} "
                      f"{cfg.request.timeframe}."),
        ))

    # ---- 2. load bars ------------------------------------------------
    # Conversion: CLI inclusive end -> M16 exclusive end (+1 day).
    m16_start = _cli_date_to_utc(cfg.request.start)
    m16_end   = _cli_date_to_utc(cfg.request.end + timedelta(days=1))

    df = _m16_store.get_bars(
        symbol=cfg.request.symbol,
        timeframe=cfg.request.timeframe,
        start_utc=m16_start,
        end_utc=m16_end,
        provider=cfg.data.provider,
        adjusted=cfg.data.adjusted,
    )

    # ---- 3. post-load validation ------------------------------------
    if df is None or df.empty:
        raise MissingDataError(_refresh_message(
            cfg,
            reason=(f"M16 returned 0 bars for {cfg.request.symbol} "
                     f"{cfg.request.timeframe} in "
                     f"{cfg.request.start}..{cfg.request.end}."),
        ))

    # Required columns must be present (defensive — M16 contract should
    # always provide them, but assert).
    for col in ("ts_utc", "open", "high", "low", "close", "volume"):
        if col not in df.columns:
            raise MissingDataError(_refresh_message(
                cfg,
                reason=(f"M16 returned bars without required column "
                         f"{col!r}; got columns: {list(df.columns)}."),
            ))

    # Sort + de-duplicate.
    df = df.sort_values("ts_utc").reset_index(drop=True)
    if df["ts_utc"].duplicated().any():
        raise MissingDataError(_refresh_message(
            cfg,
            reason=(f"M16 bars contain duplicate timestamps for "
                     f"{cfg.request.symbol} {cfg.request.timeframe}. "
                     f"Schema invariant violated — run M16 repair."),
        ))

    # NaN OHLC = fail.
    ohlc = df[["open", "high", "low", "close"]]
    if ohlc.isna().any().any():
        nan_rows = df[ohlc.isna().any(axis=1)]
        raise MissingDataError(_refresh_message(
            cfg,
            reason=(f"M16 bars contain NaN OHLC values "
                     f"({len(nan_rows)} bad rows) for "
                     f"{cfg.request.symbol} {cfg.request.timeframe}. "
                     f"First bad ts: {nan_rows.iloc[0]['ts_utc']}."),
        ))

    # Range coverage check at the bar level (in case coverage table is
    # stale but bars are correct, or vice versa).
    expected_start = pd.Timestamp(cfg.request.start, tz="UTC")
    if df.iloc[0]["ts_utc"] > expected_start + timedelta(days=7):
        # More than a week of warmup gap from requested start — warn,
        # don't fail. Operator may be intentionally backtesting from a
        # warmup-padded start.
        warnings.append(BacktestWarning(
            code="data_starts_late",
            message=(f"First available bar is "
                      f"{df.iloc[0]['ts_utc']}, more than 7 days "
                      f"after requested start {cfg.request.start}."),
        ))

    return df, coverage, warnings


def validate_coverage(cfg: BacktestConfig) -> Dict[str, Any]:
    """Check the M16 coverage row for the requested symbol/timeframe.

    Returns the coverage row dict on success. Raises MissingDataError
    on any HARD failure (see module docstring).
    """
    cov = _m16_store.get_coverage(
        symbol=cfg.request.symbol,
        timeframe=cfg.request.timeframe,
        provider=cfg.data.provider,
    )

    if cov is None:
        raise MissingDataError(_refresh_message(
            cfg,
            reason=(f"No M16 coverage row for {cfg.request.symbol} "
                     f"{cfg.request.timeframe}. The store has never been "
                     f"refreshed for this symbol+timeframe."),
        ))

    # ---- range gates -------------------------------------------------
    first_ts = cov.get("first_ts_utc")
    last_ts  = cov.get("last_ts_utc")
    if first_ts is None or last_ts is None:
        raise MissingDataError(_refresh_message(
            cfg,
            reason=(f"M16 coverage row for {cfg.request.symbol} "
                     f"{cfg.request.timeframe} has null first_ts_utc "
                     f"or last_ts_utc."),
        ))

    first_ts = pd.Timestamp(first_ts)
    last_ts  = pd.Timestamp(last_ts)
    if first_ts.tz is None:
        first_ts = first_ts.tz_localize("UTC")
    if last_ts.tz is None:
        last_ts = last_ts.tz_localize("UTC")

    req_start_ts = pd.Timestamp(cfg.request.start, tz="UTC")
    req_end_ts   = pd.Timestamp(cfg.request.end,   tz="UTC")

    if first_ts > req_start_ts:
        raise MissingDataError(_refresh_message(
            cfg,
            reason=(f"M16 coverage for {cfg.request.symbol} "
                     f"{cfg.request.timeframe} starts at "
                     f"{first_ts.date()}, after requested start "
                     f"{cfg.request.start}."),
        ))
    if last_ts < req_end_ts:
        raise MissingDataError(_refresh_message(
            cfg,
            reason=(f"M16 coverage for {cfg.request.symbol} "
                     f"{cfg.request.timeframe} ends at "
                     f"{last_ts.date()}, before requested end "
                     f"{cfg.request.end}."),
        ))

    # ---- integrity gates --------------------------------------------
    missing_count = cov.get("missing_count")
    if isinstance(missing_count, (int, float)) and missing_count > 0:
        raise MissingDataError(_refresh_message(
            cfg,
            reason=(f"M16 coverage reports missing_count="
                     f"{int(missing_count)} bars for "
                     f"{cfg.request.symbol} {cfg.request.timeframe}. "
                     f"Run M16 repair to fill gaps."),
        ))

    qstatus = cov.get("quality_status")
    if qstatus == "error":
        raise MissingDataError(_refresh_message(
            cfg,
            reason=(f"M16 coverage reports quality_status='error' for "
                     f"{cfg.request.symbol} {cfg.request.timeframe}. "
                     f"Data is corrupt; run M16 force-rebuild."),
        ))

    return cov


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _cli_date_to_utc(d: date) -> datetime:
    """Convert a date (INCLUSIVE bound from CLI / JSON config) to a
    UTC-aware datetime at 00:00:00 UTC of that day. M16's get_bars
    accepts datetime / Timestamp / str, but we pass an explicit
    UTC-aware datetime to be unambiguous."""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _refresh_message(cfg: BacktestConfig, *, reason: str) -> str:
    """Build the MissingDataError message body with the exact M16 CLI
    command the operator needs to run."""
    return (
        f"{reason}\n"
        f"Run this first:\n"
        f"  python -m bot.historical.cli backfill "
        f"--symbols {cfg.request.symbol} "
        f"--timeframes {cfg.request.timeframe} "
        f"--start {cfg.request.start.isoformat()} "
        f"--end {cfg.request.end.isoformat()}"
    )


__all__ = ["load_backtest_bars", "validate_coverage"]
