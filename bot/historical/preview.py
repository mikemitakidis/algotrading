"""bot/data/preview.py — M16.B local-data read CAPABILITY PROOF.

This module exists for ONE purpose: prove that a downstream module can
read bars from the local historical store via bot.historical.store.get_bars()
without making any provider/network calls.

It is NOT:
  * a strategy
  * a signal scoring engine
  * a backtester
  * an ML feature pipeline
  * an integration with the live scanner

It IS:
  * a tiny SMA computation that takes (symbol, timeframe, periods, lookback)
  * reads via get_bars()
  * returns last N SMA values
  * proves the local read path is wired

When the test mocks the provider to raise on any call and runs this
function against seeded Parquet data, the call MUST succeed and the
provider mock MUST never be invoked. That's the entire M16.B acceptance
criterion.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from bot.historical import store as _store


log = logging.getLogger(__name__)


def compute_recent_sma(
    symbol: str,
    timeframe: str,
    *,
    periods: int = 20,
    lookback: int = 60,
    provider: str = "yfinance",
    adjusted: bool = True,
    parquet_root: Optional[Path] = None,
) -> pd.Series:
    """Compute SMA(periods) on the most recent `lookback + periods` bars.

    Returns a pd.Series of the last `lookback` SMA values, indexed by ts_utc.
    Returns an empty Series if not enough local data exists.

    Reads via bot.historical.store.get_bars only — no provider calls.
    """
    df = _store.get_bars(symbol, timeframe, provider=provider,
                            adjusted=adjusted, parquet_root=parquet_root)
    if df is None or len(df) < periods:
        return pd.Series(dtype="float64", name=f"sma{periods}")

    df = df.sort_values("ts_utc").reset_index(drop=True)
    sma = df["close"].rolling(periods).mean()
    out = sma.tail(lookback)
    out.index = df["ts_utc"].tail(lookback)
    out.name = f"sma{periods}"
    return out
