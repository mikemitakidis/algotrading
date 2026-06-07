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
    * actual first bar > request.start AND
       gap > _BOUNDARY_TOLERANCE_DAYS OR
       coverage not clean / missing_count > 0   → truncated start
    * actual last  bar < request.end AND
       gap > _BOUNDARY_TOLERANCE_DAYS OR
       coverage not clean / missing_count > 0   → truncated end

  WARNINGS (recorded in result.warnings, never block):
    * quality_status == 'warn'                  → soft quality issue
    * freshness_status != 'fresh'               → data is stale
    * actual first bar > request.start by
       <= _BOUNDARY_TOLERANCE_DAYS, coverage
       clean, missing_count == 0                → boundary_non_trading_start
    * actual last  bar < request.end by
       <= _BOUNDARY_TOLERANCE_DAYS, coverage
       clean, missing_count == 0                → boundary_non_trading_end

Note: M17.A V1 is strict on REAL truncation but tolerates small
non-trading-day boundary gaps (e.g. a request starting 2024-01-01
that returns a first bar at 2024-01-02 because Jan 1 is a US market
holiday). Tolerance ONLY applies when coverage quality is clean.

Returned DataFrame is sorted ascending by ts_utc, UTC-aware, with
columns: ts_utc, open, high, low, close, volume, quality_flags.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# The ONE module that bot.backtesting is allowed to import from
# bot.historical. G10 AST scan asserts no other bot/backtesting/*.py
# imports bot.historical or any submodule. Re-exposing
# M16_SCHEMA_VERSION here lets output.py read the schema version
# without violating that invariant.
from bot.historical import store as _m16_store
from bot.historical import schema as _m16_schema

# Public re-export of the M16 historical-store schema version. The
# manifest writer reads this from bot.backtesting.data_loader; it
# never imports bot.historical directly.
M16_SCHEMA_VERSION: int = _m16_schema.SCHEMA_VERSION

# How many calendar days of gap between requested-start and
# actual-first-bar (or requested-end and actual-last-bar) we tolerate
# as a non-trading-day boundary rather than a real truncation. 7 days
# covers any single US-market holiday cluster (e.g. a 4-day
# Thanksgiving weekend) without quietly accepting >1-week truncated
# datasets. Only applies when coverage quality is 'clean' AND
# missing_count == 0 — see load_backtest_bars().
_BOUNDARY_TOLERANCE_DAYS: int = 7

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
                     f"Schema invariant violated."),
            op="force-rebuild",
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
            op="force-rebuild",
        ))

    # Range coverage check at the BAR level (the M16 coverage row may
    # be stale or out of sync with the actual parquet store, so we
    # verify the returned bars themselves cover the requested period).
    #
    # M17.A V1 is STRICT on real truncation but tolerates SMALL boundary
    # gaps that come from non-trading days at the requested start/end.
    # Example: a request for 2024-01-01..2024-12-31 with AAPL 1D will
    # legitimately return a first bar at 2024-01-02 (Jan 1 is a US market
    # holiday). Without this tolerance the loader would reject every
    # holiday-aligned request.
    #
    # Boundary tolerance only applies when:
    #   * gap_days <= _BOUNDARY_TOLERANCE_DAYS                (small)
    #   * coverage['quality_status'] == 'clean'              (trustworthy)
    #   * coverage.get('missing_count', 0) == 0              (no known gaps)
    # If a gap qualifies, a BacktestWarning is recorded
    # ('boundary_non_trading_start' or 'boundary_non_trading_end') and
    # the loader continues. Otherwise the gap is treated as truncation
    # and raised as MissingDataError.
    actual_first_ts = pd.Timestamp(df.iloc[0]["ts_utc"])
    if actual_first_ts.tz is None:
        actual_first_ts = actual_first_ts.tz_localize("UTC")
    else:
        actual_first_ts = actual_first_ts.tz_convert("UTC")
    actual_last_ts = pd.Timestamp(df.iloc[-1]["ts_utc"])
    if actual_last_ts.tz is None:
        actual_last_ts = actual_last_ts.tz_localize("UTC")
    else:
        actual_last_ts = actual_last_ts.tz_convert("UTC")

    cov_clean    = coverage.get("quality_status") == "clean"
    cov_no_gaps  = (coverage.get("missing_count") or 0) == 0
    boundary_ok  = cov_clean and cov_no_gaps

    if actual_first_ts.date() > cfg.request.start:
        gap_days = (actual_first_ts.date() - cfg.request.start).days
        if boundary_ok and gap_days <= _BOUNDARY_TOLERANCE_DAYS:
            warnings.append(BacktestWarning(
                code="boundary_non_trading_start",
                message=(f"Requested start {cfg.request.start} is "
                          f"{gap_days} day(s) before first available bar "
                          f"{actual_first_ts.date()} for "
                          f"{cfg.request.symbol} {cfg.request.timeframe}; "
                          f"treated as a non-trading-day boundary "
                          f"(tolerance: {_BOUNDARY_TOLERANCE_DAYS} days)."),
                ts_utc=actual_first_ts.to_pydatetime(),
                extras={"gap_days": gap_days,
                          "tolerance_days": _BOUNDARY_TOLERANCE_DAYS,
                          "actual_first_date": actual_first_ts.date().isoformat(),
                          "requested_start":   cfg.request.start.isoformat()},
            ))
        else:
            raise MissingDataError(_refresh_message(
                cfg,
                reason=(f"M16 returned bars for {cfg.request.symbol} "
                         f"{cfg.request.timeframe} starting at "
                         f"{actual_first_ts.date()}, "
                         f"{gap_days} day(s) after requested start "
                         f"{cfg.request.start} "
                         f"(exceeds boundary tolerance of "
                         f"{_BOUNDARY_TOLERANCE_DAYS} days OR coverage "
                         f"quality/missing_count not clean). Loaded "
                         f"bars do not cover the requested period — "
                         f"coverage row may be stale or out of sync."),
                op="backfill",
            ))

    if actual_last_ts.date() < cfg.request.end:
        gap_days = (cfg.request.end - actual_last_ts.date()).days
        if boundary_ok and gap_days <= _BOUNDARY_TOLERANCE_DAYS:
            warnings.append(BacktestWarning(
                code="boundary_non_trading_end",
                message=(f"Requested end {cfg.request.end} is "
                          f"{gap_days} day(s) after last available bar "
                          f"{actual_last_ts.date()} for "
                          f"{cfg.request.symbol} {cfg.request.timeframe}; "
                          f"treated as a non-trading-day boundary "
                          f"(tolerance: {_BOUNDARY_TOLERANCE_DAYS} days)."),
                ts_utc=actual_last_ts.to_pydatetime(),
                extras={"gap_days": gap_days,
                          "tolerance_days": _BOUNDARY_TOLERANCE_DAYS,
                          "actual_last_date": actual_last_ts.date().isoformat(),
                          "requested_end":    cfg.request.end.isoformat()},
            ))
        else:
            raise MissingDataError(_refresh_message(
                cfg,
                reason=(f"M16 returned bars for {cfg.request.symbol} "
                         f"{cfg.request.timeframe} ending at "
                         f"{actual_last_ts.date()}, "
                         f"{gap_days} day(s) before requested end "
                         f"{cfg.request.end} "
                         f"(exceeds boundary tolerance of "
                         f"{_BOUNDARY_TOLERANCE_DAYS} days OR coverage "
                         f"quality/missing_count not clean). Loaded "
                         f"bars do not cover the requested period — "
                         f"coverage row may be stale or out of sync."),
                op="backfill",
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
                     f"Bars present but incomplete; gaps need filling."),
            op="repair",
        ))

    qstatus = cov.get("quality_status")
    if qstatus == "error":
        raise MissingDataError(_refresh_message(
            cfg,
            reason=(f"M16 coverage reports quality_status='error' for "
                     f"{cfg.request.symbol} {cfg.request.timeframe}. "
                     f"Data is corrupt."),
            op="force-rebuild",
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


def _refresh_message(cfg: BacktestConfig, *, reason: str,
                       op: str = "backfill") -> str:
    """Build the MissingDataError message body with a VALID M16 CLI
    command for the operator.

    `op` selects the M16 subcommand based on the failure mode:
      * 'backfill'      — no coverage, or coverage range too narrow,
                          or empty bars (data missing entirely)
      * 'repair'        — known gaps (missing_count > 0); the bars
                          are present but incomplete
      * 'force-rebuild' — corrupt data (quality_status='error',
                          duplicate timestamps, NaN OHLC); delete
                          and re-ingest

    M16 CLI flag invariants (`python -m bot.historical.cli <op> -h`):
      backfill:      --symbols (plural), --timeframes (plural),
                      --lookback (optional)
      repair:        --symbols (plural), --timeframes (plural)
      force-rebuild: --symbol  (SINGULAR, required),
                      --timeframe (SINGULAR, required),
                      --lookback (optional)

    There is NO --start / --end flag on any M16 subcommand. The
    operator constrains the window via --lookback when relevant.
    """
    if op not in ("backfill", "repair", "force-rebuild"):
        # Defensive — caller bug, but don't drop the message.
        op = "backfill"

    if op == "force-rebuild":
        # SINGULAR flags — verified against `python -m bot.historical.cli
        # force-rebuild --help` which shows
        # `--symbol SYMBOL --timeframe TIMEFRAME` (no plural form
        # supported).
        cmd = (f"  python -m bot.historical.cli {op} "
                f"--symbol {cfg.request.symbol} "
                f"--timeframe {cfg.request.timeframe}")
    else:
        # backfill and repair both use plural flags.
        cmd = (f"  python -m bot.historical.cli {op} "
                f"--symbols {cfg.request.symbol} "
                f"--timeframes {cfg.request.timeframe}")
        if op == "backfill":
            # --lookback is optional; M16 defaults to provider max.
            # Suggest it so the operator can constrain the window
            # if they want, but don't require it.
            cmd += "\n  (optionally: --lookback 730d to constrain the window)"
    return (
        f"{reason}\n"
        f"Run this first:\n"
        f"{cmd}"
    )


__all__ = ["load_backtest_bars", "validate_coverage",
            "M16_SCHEMA_VERSION",
            "load_multi_tf_bars", "MultiTfBars",
            "_BOUNDARY_TOLERANCE_DAYS"]


# ─────────────────────────────────────────────────────────────────────
# M17.B.2 — Multi-timeframe loader (strict-per-TF by default)
# ─────────────────────────────────────────────────────────────────────

from dataclasses import dataclass as _dataclass
from dataclasses import field as _field
from dataclasses import replace as _dc_replace


@_dataclass(frozen=True)
class MultiTfBars:
    """Result of load_multi_tf_bars().

    For each requested timeframe, holds the loaded bars + coverage
    metadata OR (in PARTIAL mode) a None placeholder with a warning.

    STRICT mode (allow_partial_tfs=False, default per Sharpened Rule
    #3): any per-TF MissingDataError is re-raised immediately; this
    object is never constructed. PARTIAL mode: failing TFs are recorded
    with bars=None and an explicit 'partial_tf_unavailable' warning is
    appended.
    """
    symbol:               str
    requested_timeframes: Tuple[str, ...]
    per_tf_bars:          Dict[str, Optional[pd.DataFrame]]
    per_tf_coverage:      Dict[str, Optional[Dict[str, Any]]]
    warnings:             List[BacktestWarning]
    allow_partial_tfs:    bool

    @property
    def loaded_timeframes(self) -> Tuple[str, ...]:
        """TFs for which bars were actually loaded (PARTIAL mode skips
        unavailable ones)."""
        return tuple(
            tf for tf in self.requested_timeframes
            if self.per_tf_bars.get(tf) is not None
        )


def load_multi_tf_bars(
    cfg: BacktestConfig,
    timeframes: List[str],
    *,
    allow_partial_tfs: bool = False,
) -> MultiTfBars:
    """Load M16 bars for one symbol across N timeframes.

    Wraps load_backtest_bars per-TF via dataclasses.replace so every
    M17.A integrity gate (coverage row check, NaN/dup-ts/empty checks,
    bar-level range check with non-trading-day boundary tolerance)
    fires identically on each TF. No M17.A validation logic is
    duplicated here.

    Args:
        cfg:               the full BacktestConfig. Used as a template:
                           request.symbol, request.start/end, data.adjusted,
                           data.provider are all preserved per-TF;
                           request.timeframe is replaced per call.
                           cfg.request.timeframe is NOT itself loaded
                           unless it appears in `timeframes`.
        timeframes:        list of TF labels (e.g. ['1D','4H','1H','15m']).
                           Order is preserved in the result.
        allow_partial_tfs: STRICT default (False). When True, per-TF
                           MissingDataError is caught and recorded as
                           a 'partial_tf_unavailable' warning instead
                           of raising. Per Sharpened Rule #3 partial
                           mode is OPT-IN and never silent.

    Returns:
        MultiTfBars

    Raises (STRICT mode only):
        MissingDataError if ANY requested TF fails to load. The error
        message is the M16 refresh command for that TF.
    """
    if not timeframes:
        raise ValueError("timeframes must contain at least one TF label")
    # Preserve duplicate-free ordering
    seen = []
    for tf in timeframes:
        if tf not in seen:
            seen.append(tf)
    timeframes = seen

    per_tf_bars:     Dict[str, Optional[pd.DataFrame]]    = {}
    per_tf_coverage: Dict[str, Optional[Dict[str, Any]]] = {}
    warnings:        List[BacktestWarning]                = []

    for tf in timeframes:
        per_tf_cfg = _dc_replace(
            cfg, request=_dc_replace(cfg.request, timeframe=tf))
        try:
            bars, coverage, tf_warnings = load_backtest_bars(per_tf_cfg)
        except MissingDataError as e:
            if not allow_partial_tfs:
                # STRICT: propagate. Wrap the per-TF message with the
                # request context so the operator sees WHICH TF failed
                # without losing the M16 refresh command.
                raise MissingDataError(
                    f"Multi-TF load failed at timeframe {tf!r} "
                    f"(strict mode; pass allow_partial_tfs=True to "
                    f"continue with the remaining TFs):\n{e}"
                ) from e
            # PARTIAL: record placeholder + warning
            per_tf_bars[tf]     = None
            per_tf_coverage[tf] = None
            warnings.append(BacktestWarning(
                code="partial_tf_unavailable",
                message=(f"Timeframe {tf} unavailable for "
                          f"{cfg.request.symbol}; multi-TF run "
                          f"continues with reduced TF set "
                          f"(allow_partial_tfs=True). Original "
                          f"failure: {e}"),
                extras={"timeframe": tf,
                          "symbol":   cfg.request.symbol,
                          "underlying_error": str(e)[:500]},
            ))
            continue
        per_tf_bars[tf]     = bars
        per_tf_coverage[tf] = coverage
        # Re-tag per-TF warnings with their timeframe so the caller
        # can tell which TF generated which boundary/quality warning.
        for w in tf_warnings:
            extras = dict(w.extras) if w.extras else {}
            extras.setdefault("timeframe", tf)
            warnings.append(BacktestWarning(
                code=w.code,
                message=f"[{tf}] {w.message}",
                ts_utc=w.ts_utc,
                extras=extras,
            ))

    return MultiTfBars(
        symbol=cfg.request.symbol,
        requested_timeframes=tuple(timeframes),
        per_tf_bars=per_tf_bars,
        per_tf_coverage=per_tf_coverage,
        warnings=warnings,
        allow_partial_tfs=allow_partial_tfs,
    )
