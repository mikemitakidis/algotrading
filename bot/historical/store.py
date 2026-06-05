"""bot/historical/store.py — M16 public read façade.

The ONLY public API the rest of the bot uses to access historical bars:

  get_bars(symbol, timeframe, start_utc, end_utc, *, provider='yfinance',
            adjusted=True)
  get_coverage(symbol, timeframe=None, *, provider='yfinance')
  list_symbols(*, asset_class=None, only_active=True)
  list_quality_events(*, symbol=None, timeframe=None, severity=None,
                          since_utc=None, limit=100)

Internal storage helpers (used by refresh.py only — NOT for callers):

  _read_parquet_raw(path)
  _write_parquet_atomic(path, df)
  _parquet_path(provider, timeframe, symbol, root=None)

Hard invariants:
  * Reads are pure: NO provider/network calls, NO writes.
  * Tz-aware UTC throughout.
  * Returns DataFrame copies, never views.
  * Empty DataFrame on unknown symbol — never raises.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from bot.historical import schema as _schema
from bot.historical.timeframes import ensure_utc


log = logging.getLogger(__name__)


# Parquet schema spec — written and validated at every write.
_PARQUET_COLUMNS = (
    "ts_utc", "open", "high", "low", "close", "volume",
    "adj_close", "adjustment_ratio", "is_adjusted",
    "provider", "ingested_at_utc", "quality_flags",
)


def _parquet_path(provider: str, timeframe: str, symbol: str,
                    *, root: Optional[Path] = None) -> Path:
    """Canonical path: data/historical/<provider>/<timeframe>/<symbol>.parquet

    Provider-in-path per Correction 1: future providers don't mix with
    yfinance bars. Symbol is upper-cased so 'aapl' and 'AAPL' resolve
    to the same file.
    """
    if root is None:
        root = _schema.default_parquet_root()
    sym = symbol.upper()
    return Path(root) / provider / timeframe / f"{sym}.parquet"


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------

def get_bars(
    symbol: str,
    timeframe: str,
    start_utc: Union[str, datetime, pd.Timestamp, None] = None,
    end_utc: Union[str, datetime, pd.Timestamp, None] = None,
    *,
    provider: str = "yfinance",
    adjusted: bool = True,
    parquet_root: Optional[Path] = None,
) -> pd.DataFrame:
    """Return historical bars from the local Parquet store.

    NEVER calls a provider. NEVER writes anything. Pure read.

    Parameters:
      symbol      e.g. 'AAPL' (case-insensitive; normalised to upper)
      timeframe   one of '1D', '4H', '1H', '15m'
      start_utc   lower bound INCLUSIVE; if None, no lower bound
      end_utc     upper bound EXCLUSIVE; if None, no upper bound
      provider    'yfinance' (V1). Future providers select their own files.
      adjusted    True  -> open/high/low computed via adjustment_ratio,
                            close from stored adj_close.
                  False -> raw open/high/low/close as the provider returned.
      parquet_root  Optional override (tests use this).

    Returns:
      A pd.DataFrame with columns:
        ts_utc (tz-aware UTC), open, high, low, close, volume, quality_flags
      Empty DataFrame (with the same columns) if no data exists.
    """
    if timeframe not in _schema.ALLOWED_TIMEFRAMES:
        raise ValueError(f"unsupported timeframe {timeframe!r}; "
                          f"must be one of {_schema.ALLOWED_TIMEFRAMES}")

    path = _parquet_path(provider, timeframe, symbol, root=parquet_root)
    if not path.exists():
        return _empty_read_frame()

    df = _read_parquet_raw(path)
    if df.empty:
        return _empty_read_frame()

    # Predicate filter on ts_utc (inclusive lower, exclusive upper).
    if start_utc is not None:
        start_ts = ensure_utc(start_utc)
        df = df[df["ts_utc"] >= start_ts]
    if end_utc is not None:
        end_ts = ensure_utc(end_utc)
        df = df[df["ts_utc"] < end_ts]

    if df.empty:
        return _empty_read_frame()

    df = df.sort_values("ts_utc").reset_index(drop=True)

    if adjusted:
        # Adjusted view: open/high/low scaled via adjustment_ratio,
        # close = stored adj_close. Documented approximation:
        # yfinance only exposes Adj Close, not adjusted O/H/L
        # separately; we use a uniform per-bar ratio.
        ratio = df["adjustment_ratio"]
        if ratio.isna().all():
            # No adjustment data available — fall through to raw.
            adjusted = False

    out = pd.DataFrame()
    out["ts_utc"] = df["ts_utc"]
    if adjusted:
        out["open"] = df["open"] * df["adjustment_ratio"]
        out["high"] = df["high"] * df["adjustment_ratio"]
        out["low"]  = df["low"]  * df["adjustment_ratio"]
        out["close"] = df["adj_close"]
    else:
        out["open"] = df["open"]
        out["high"] = df["high"]
        out["low"]  = df["low"]
        out["close"] = df["close"]
    out["volume"] = df["volume"]
    out["quality_flags"] = df["quality_flags"] if "quality_flags" in df.columns else 0

    return out.reset_index(drop=True).copy()


def get_coverage(
    symbol: str,
    timeframe: Optional[str] = None,
    *,
    provider: str = "yfinance",
    db_path: Optional[Path] = None,
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """Return coverage row(s) for a symbol.

    If timeframe is given, returns a single dict (or None if no row).
    Otherwise returns a list of dicts (one per timeframe present).
    """
    db = db_path or _schema.default_db_path()
    if not Path(db).exists():
        return None if timeframe is not None else []

    conn = _schema.open_db(db)
    try:
        sym = symbol.upper()
        cols = ("symbol, timeframe, provider, first_ts_utc, last_ts_utc, "
                 "bar_count, missing_count, duplicate_count, "
                 "quality_status, freshness_status, last_refresh_at_utc, "
                 "last_refresh_id, provider_limit_note, "
                 "source_timeframe, derivation_method, resample_rule_version")
        if timeframe is not None:
            row = conn.execute(
                f"SELECT {cols} FROM historical_coverage "
                "WHERE symbol = ? AND timeframe = ? AND provider = ?",
                (sym, timeframe, provider),
            ).fetchone()
            if row is None:
                return None
            return _coverage_row_to_dict(row, cols)
        rows = conn.execute(
            f"SELECT {cols} FROM historical_coverage "
            "WHERE symbol = ? AND provider = ? "
            "ORDER BY timeframe",
            (sym, provider),
        ).fetchall()
        return [_coverage_row_to_dict(r, cols) for r in rows]
    finally:
        conn.close()


def list_symbols(*, asset_class: Optional[str] = None,
                    only_active: bool = True,
                    db_path: Optional[Path] = None) -> List[str]:
    """Return the symbol universe."""
    db = db_path or _schema.default_db_path()
    if not Path(db).exists():
        return []
    conn = _schema.open_db(db)
    try:
        sql = "SELECT symbol FROM historical_symbols WHERE 1=1"
        params: List[Any] = []
        if asset_class is not None:
            sql += " AND asset_class = ?"
            params.append(asset_class)
        if only_active:
            sql += " AND is_active = 1"
        sql += " ORDER BY symbol"
        return [r[0] for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def list_quality_events(*, symbol: Optional[str] = None,
                          timeframe: Optional[str] = None,
                          severity: Optional[str] = None,
                          since_utc: Optional[str] = None,
                          limit: int = 100,
                          db_path: Optional[Path] = None,
                          ) -> List[Dict[str, Any]]:
    """Return recent quality events. Newest first."""
    db = db_path or _schema.default_db_path()
    if not Path(db).exists():
        return []
    conn = _schema.open_db(db)
    try:
        sql = ("SELECT id, run_id, symbol, timeframe, provider, ts_utc, "
                "severity, kind, message, details_json, created_at_utc "
                "FROM historical_quality_events WHERE 1=1")
        params: List[Any] = []
        if symbol is not None:
            sql += " AND symbol = ?"
            params.append(symbol.upper())
        if timeframe is not None:
            sql += " AND timeframe = ?"
            params.append(timeframe)
        if severity is not None:
            sql += " AND severity = ?"
            params.append(severity)
        if since_utc is not None:
            sql += " AND created_at_utc >= ?"
            params.append(since_utc)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        keys = ("id", "run_id", "symbol", "timeframe", "provider", "ts_utc",
                  "severity", "kind", "message", "details_json", "created_at_utc")
        return [dict(zip(keys, r)) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal Parquet I/O (refresh.py only)
# ---------------------------------------------------------------------------

def _read_parquet_raw(path: Path) -> pd.DataFrame:
    """Read a Parquet file as a DataFrame. Ts column always tz-aware UTC."""
    try:
        table = pq.read_table(str(path))
    except Exception as e:  # noqa: BLE001
        log.warning("failed to read Parquet at %s: %s", path, e)
        return pd.DataFrame(columns=_PARQUET_COLUMNS)
    df = table.to_pandas()
    if "ts_utc" in df.columns and len(df) > 0:
        # Ensure tz-aware UTC. pyarrow preserves tz on round-trip.
        if df["ts_utc"].dt.tz is None:
            df["ts_utc"] = df["ts_utc"].dt.tz_localize("UTC")
        else:
            df["ts_utc"] = df["ts_utc"].dt.tz_convert("UTC")
    return df


def _write_parquet_atomic(path: Path, df: pd.DataFrame) -> None:
    """Atomically write df to Parquet at `path`.

    Protocol:
      1. mkdir -p parent
      2. write to <path>.tmp.<uuid>
      3. re-open the temp + schema/sort/uniqueness validate
      4. os.replace(tmp, path) — POSIX-atomic rename
    Caller is responsible for invoking validation BEFORE this. This
    function performs only structural post-write sanity checks.
    """
    if df is None or len(df) == 0:
        raise ValueError("refusing to write empty DataFrame")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Required-columns check.
    missing = [c for c in _PARQUET_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")

    # Sort + uniqueness pre-check.
    df_out = df[list(_PARQUET_COLUMNS)].copy()
    df_out = df_out.sort_values("ts_utc").reset_index(drop=True)
    dup = df_out["ts_utc"].duplicated()
    if dup.any():
        raise ValueError(
            f"refusing to write Parquet with duplicate ts_utc "
            f"(n={int(dup.sum())} duplicates). The caller should have "
            f"deduped via quality.validate_batch first."
        )

    # Coerce types defensively.
    df_out["ts_utc"] = pd.to_datetime(df_out["ts_utc"], utc=True)
    df_out["ingested_at_utc"] = pd.to_datetime(
        df_out["ingested_at_utc"], utc=True)
    for col in ("open", "high", "low", "close", "adj_close",
                  "adjustment_ratio"):
        df_out[col] = pd.to_numeric(df_out[col], errors="coerce")
    df_out["volume"] = pd.to_numeric(df_out["volume"],
                                       errors="coerce").astype("Int64")
    df_out["quality_flags"] = pd.to_numeric(
        df_out["quality_flags"], errors="coerce").fillna(0).astype("int32")
    df_out["is_adjusted"] = df_out["is_adjusted"].astype(bool)
    df_out["provider"] = df_out["provider"].astype(str)

    # Write to a uniquely-named sibling temp.
    tmp_name = f"{path.name}.tmp.{uuid.uuid4().hex}"
    tmp_path = path.parent / tmp_name
    try:
        table = pa.Table.from_pandas(df_out, preserve_index=False)
        pq.write_table(table, str(tmp_path), compression="snappy")

        # Post-write validation: re-open and inspect.
        reread = pq.read_table(str(tmp_path)).to_pandas()
        if len(reread) != len(df_out):
            raise RuntimeError(
                f"post-write count mismatch: wrote {len(df_out)}, "
                f"re-read {len(reread)}")
        for c in _PARQUET_COLUMNS:
            if c not in reread.columns:
                raise RuntimeError(f"post-write missing column {c!r}")

        os.replace(str(tmp_path), str(path))
    except Exception:
        # Discard the temp file on any failure.
        try:
            if tmp_path.exists():
                os.unlink(str(tmp_path))
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_read_frame() -> pd.DataFrame:
    """Return an empty DataFrame with the public read shape."""
    df = pd.DataFrame(columns=("ts_utc", "open", "high", "low", "close",
                                 "volume", "quality_flags"))
    # Make ts_utc a tz-aware empty column so .dt is callable.
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    return df


def _coverage_row_to_dict(row, cols_str):
    keys = [c.strip() for c in cols_str.split(",")]
    return dict(zip(keys, row))
