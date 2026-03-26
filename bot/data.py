"""
bot/data.py
Market data access layer — delegates to the active data provider.

Public API (signatures unchanged so scanner.py needs no edits):
    fetch_bars(symbols, period, interval) -> dict
    resample_to_4h(df) -> DataFrame

The actual fetch/cache logic lives in bot/providers/yfinance_provider.py.
Switch providers by setting DATA_PROVIDER in .env (default: yfinance).

Also exposes:
    save_focus_cache / load_focus_cache  — unrelated to provider, stay here
"""
import json
import logging
import time
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

_BASE    = Path(__file__).resolve().parent.parent
_FOCUS_F = _BASE / 'data' / 'focus_cache.json'

# ── Provider delegation ───────────────────────────────────────────────────────

def fetch_bars(symbols: list, period: str, interval: str) -> dict:
    """
    Fetch OHLCV bars for multiple symbols via the active provider.
    Delegates entirely to bot.providers.get_provider().fetch_bars().
    """
    from bot.providers import get_provider
    return get_provider().fetch_bars(symbols, period, interval)


def resample_to_4h(df: pd.DataFrame) -> pd.DataFrame:
    """Resample a UTC-indexed OHLCV DataFrame to 4-hour bars."""
    if not isinstance(df.index, pd.DatetimeIndex):
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True)
    return df.resample('4h').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
    ).dropna()


# ── Focus cache (not provider-specific) ──────────────────────────────────────

def save_focus_cache(symbols: list) -> None:
    try:
        (_BASE / 'data').mkdir(parents=True, exist_ok=True)
        _FOCUS_F.write_text(json.dumps({'symbols': symbols, 'ts': time.time()}))
        log.info('[CACHE] Focus saved: %d symbols', len(symbols))
    except Exception as e:
        log.warning('[CACHE] Save failed: %s', e)


def load_focus_cache(max_age_secs: int = 21600) -> list:
    try:
        if not _FOCUS_F.exists():
            return []
        d   = json.loads(_FOCUS_F.read_text())
        age = time.time() - d.get('ts', 0)
        if age > max_age_secs:
            log.info('[CACHE] Focus stale (%.1fh)', age / 3600)
            return []
        syms = d.get('symbols', [])
        log.info('[CACHE] Focus loaded: %d symbols (%.1fh old)', len(syms), age / 3600)
        return syms
    except Exception as e:
        log.warning('[CACHE] Load failed: %s', e)
        return []
