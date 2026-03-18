"""
bot/data.py
Yahoo Finance data fetcher with rate-limit-safe pacing.

Key design decisions:
- Batch size: 20 symbols (not 100 — yfinance free tier throttles large batches)
- Delay between batches: 2-4s with jitter to avoid burst patterns
- On rate limit: exponential backoff, then skip batch and continue
- Focus set cached to disk — survives restarts, reduces re-download frequency
- Never crashes the bot — returns whatever data was obtained
"""
import json
import logging
import os
import random
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# Conservative settings for yfinance free tier
BATCH_SIZE   = 20      # small batches to avoid rate limits
MIN_DELAY    = 2.0     # seconds between batches (minimum)
MAX_DELAY    = 4.0     # seconds between batches (maximum, adds jitter)
MIN_BARS     = 30      # minimum bars required for indicators
MAX_RETRIES  = 2       # retries per batch on rate limit
RETRY_WAITS  = [30, 90]  # seconds to wait between retries

# Cache location for focus set
_CACHE_DIR   = Path(__file__).resolve().parent.parent / 'data'
_FOCUS_CACHE = _CACHE_DIR / 'focus_cache.json'


def _jitter_delay():
    """Sleep for a random duration between MIN_DELAY and MAX_DELAY."""
    time.sleep(MIN_DELAY + random.random() * (MAX_DELAY - MIN_DELAY))


def fetch_bars(symbols: list, period: str, interval: str) -> dict:
    """
    Fetch OHLCV bars for a list of symbols.
    Returns {symbol: DataFrame} for all symbols that returned data.
    Skips rate-limited batches after backoff — never crashes.

    Logs:
    - batch progress at DEBUG level
    - rate limit warnings with backoff time
    - final count at INFO level
    """
    result       = {}
    total        = len(symbols)
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    skipped      = 0

    for i in range(0, total, BATCH_SIZE):
        batch     = symbols[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        success = False
        for attempt in range(MAX_RETRIES + 1):
            try:
                raw = yf.download(
                    tickers=batch,
                    period=period,
                    interval=interval,
                    group_by='ticker',
                    auto_adjust=True,
                    progress=False,
                    threads=False,   # serial download — less likely to trigger limits
                )

                if raw is None or raw.empty:
                    log.debug('[DATA] Batch %d/%d (%s): empty', batch_num, total_batches, interval)
                    success = True
                    break

                if isinstance(raw.columns, pd.MultiIndex):
                    got = 0
                    for sym in batch:
                        try:
                            df = raw[sym].dropna()
                            df.columns = [c.lower() for c in df.columns]
                            if len(df) >= MIN_BARS:
                                result[sym] = df
                                got += 1
                        except (KeyError, AttributeError):
                            pass
                    log.debug('[DATA] Batch %d/%d (%s): %d/%d symbols',
                              batch_num, total_batches, interval, got, len(batch))
                else:
                    if len(batch) == 1:
                        raw.columns = [c.lower() for c in raw.columns]
                        df = raw.dropna()
                        if len(df) >= MIN_BARS:
                            result[batch[0]] = df

                success = True
                break

            except Exception as e:
                err = str(e)
                is_rate_limit = any(k in err for k in ('Rate', 'Too Many', 'rate limit', '429'))

                if is_rate_limit and attempt < MAX_RETRIES:
                    wait = RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]
                    log.warning('[DATA] Rate limit on batch %d/%d. Waiting %ds (attempt %d/%d)...',
                                batch_num, total_batches, wait, attempt + 1, MAX_RETRIES)
                    time.sleep(wait)
                else:
                    if is_rate_limit:
                        log.warning('[DATA] Batch %d/%d: rate limit persists — skipping batch',
                                    batch_num, total_batches)
                    else:
                        log.warning('[DATA] Batch %d/%d (%s): %s — skipping',
                                    batch_num, total_batches, interval, err[:80])
                    skipped += 1
                    break

        if success or skipped:
            pass  # continue to next batch

        _jitter_delay()

    log.info('[DATA] %s %s: %d/%d symbols, %d batches skipped',
             interval, period, len(result), total, skipped)
    return result


def resample_to_4h(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 1H DataFrame to 4H bars."""
    return df.resample('4h').agg({
        'open':   'first',
        'high':   'max',
        'low':    'min',
        'close':  'last',
        'volume': 'sum',
    }).dropna()


# ── Focus set cache ───────────────────────────────────────────────────────────

def save_focus_cache(symbols: list, scored: dict):
    """Save focus set and scores to disk for reuse after restart."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            'symbols':   symbols,
            'scored':    scored,
            'timestamp': time.time(),
        }
        with open(_FOCUS_CACHE, 'w') as f:
            json.dump(data, f)
        log.info('[CACHE] Focus set saved (%d symbols)', len(symbols))
    except Exception as e:
        log.warning('[CACHE] Could not save focus cache: %s', e)


def load_focus_cache(max_age_secs: int = 21600) -> list:
    """
    Load cached focus set if it exists and is not stale.
    Returns list of symbols, or empty list if cache is missing/stale.
    """
    try:
        if not _FOCUS_CACHE.exists():
            return []
        with open(_FOCUS_CACHE) as f:
            data = json.load(f)
        age = time.time() - data.get('timestamp', 0)
        if age > max_age_secs:
            log.info('[CACHE] Focus cache stale (%.1fh old) — will re-rank', age / 3600)
            return []
        symbols = data.get('symbols', [])
        log.info('[CACHE] Loaded focus cache: %d symbols (%.1fh old)', len(symbols), age / 3600)
        return symbols
    except Exception as e:
        log.warning('[CACHE] Could not load focus cache: %s', e)
        return []
