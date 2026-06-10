"""bot.ml.dataset.flywheel_reader — read-only SQLite access to the
live signals.db `signal_outcomes` table.

This module exists so the signal_history feature group can pull
past-only resolved-signal stats without importing bot.flywheel (which
is on the AST forbidden list — it writes/owns the schema). We use the
sqlite3 stdlib directly and pin the expected schema by name.

Hard guarantees:
  * Read-only. Connection opened with `mode=ro` URI; any write
    attempt raises sqlite3.OperationalError.
  * NO bot.flywheel / bot.db / live-DB-writing imports.
  * NO yfinance / requests / urllib / http.client / broker / dashboard.
  * Missing DB file or missing `signal_outcomes` table is NOT an
    error — the reader yields an empty result so signal_history can
    fall back to all-NaN features (used for fixture mode + early-life
    training when no history exists yet).

Point-in-time contract:
  closed_outcomes_for_symbol(symbol, before_ts) returns only rows
  where outcome IN ('WIN','LOSS','TIMEOUT') AND resolved_at IS NOT
  NULL AND resolved_at < before_ts. Open / unresolved signals are
  excluded — their final outcome is in the future from the anchor's
  perspective, and using them would leak future information.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional, Union

import pandas as pd


# Expected schema fingerprint (the columns this reader queries).
# If the live DB ever changes, the reader fails loud with a clear
# message rather than silently mis-aligning columns.
_REQUIRED_OUTCOME_COLUMNS = frozenset({
    "symbol", "outcome", "return_pct", "resolved_at",
})

_CLOSED_OUTCOMES = ("WIN", "LOSS", "TIMEOUT")


class FlywheelReader:
    """Read-only flywheel-DB accessor for signal_history features.

    Usage
    -----
        reader = FlywheelReader("/path/to/signals.db")
        df = reader.closed_outcomes_for_symbol(
            "AAPL",
            before_ts=pd.Timestamp("2024-06-01", tz="UTC"),
            lookback_days=90,
        )

    The reader is stateless — it opens a new read-only connection per
    query (sqlite3 connections are cheap and this avoids cross-thread
    issues with the dataset assembler). If the DB or table is missing,
    every query returns an empty DataFrame with the expected columns
    rather than raising — see signal_history.compute() for the
    "no-history → all-NaN-features" semantics.
    """

    def __init__(self, db_path: Union[str, Path]):
        self._db_path = Path(db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def is_available(self) -> bool:
        """True iff the DB file exists AND the signal_outcomes table
        exists AND has the expected columns."""
        if not self._db_path.exists():
            return False
        try:
            with closing(self._open_ro()) as conn:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='signal_outcomes'")
                if cur.fetchone() is None:
                    return False
                cur = conn.execute(
                    "PRAGMA table_info(signal_outcomes)")
                cols = {row[1] for row in cur.fetchall()}
                return _REQUIRED_OUTCOME_COLUMNS.issubset(cols)
        except sqlite3.DatabaseError:
            return False

    def _open_ro(self) -> sqlite3.Connection:
        """Open the DB strictly read-only. Any write attempted via
        this connection raises sqlite3.OperationalError."""
        uri = f"file:{self._db_path}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=5.0)

    def closed_outcomes_for_symbol(
        self,
        symbol: str,
        *,
        before_ts: pd.Timestamp,
        lookback_days: int = 90,
    ) -> pd.DataFrame:
        """Return resolved outcomes for `symbol` where
        resolved_at < before_ts AND resolved_at >= before_ts -
        lookback_days. Only rows with outcome IN
        ('WIN','LOSS','TIMEOUT') are returned (open / null
        outcomes excluded — they leak future info).

        Returns an EMPTY frame (with the expected columns) when the
        DB or table is missing, or when no rows match. NEVER raises
        for "absent" — only for malformed schema / unreachable DB.

        The `before_ts` and `lookback_days` arguments are formatted
        into ISO-8601 strings for the SQL filter. resolved_at in the
        live schema is a TEXT ISO timestamp.
        """
        if not isinstance(before_ts, pd.Timestamp) or before_ts.tz is None:
            raise ValueError(
                "before_ts must be a tz-aware pd.Timestamp (UTC)")
        if not isinstance(lookback_days, int) or lookback_days <= 0:
            raise ValueError(
                f"lookback_days must be a positive int, got "
                f"{lookback_days!r}")

        empty = pd.DataFrame(
            {c: pd.Series(dtype=object)
              for c in sorted(_REQUIRED_OUTCOME_COLUMNS)})
        if not self.is_available():
            return empty

        upper = before_ts.tz_convert("UTC").isoformat()
        lower = (before_ts.tz_convert("UTC")
                  - pd.Timedelta(days=lookback_days)).isoformat()

        try:
            with closing(self._open_ro()) as conn:
                rows = conn.execute(
                    "SELECT symbol, outcome, return_pct, resolved_at "
                    "FROM signal_outcomes "
                    "WHERE symbol = ? "
                    "  AND outcome IN ('WIN','LOSS','TIMEOUT') "
                    "  AND resolved_at IS NOT NULL "
                    "  AND resolved_at < ? "
                    "  AND resolved_at >= ? "
                    "ORDER BY resolved_at ASC",
                    (symbol, upper, lower),
                ).fetchall()
        except sqlite3.DatabaseError:
            # Corrupt DB / locked / etc. — degrade to empty.
            return empty

        if not rows:
            return empty
        df = pd.DataFrame(rows, columns=[
            "symbol", "outcome", "return_pct", "resolved_at",
        ])
        return df
