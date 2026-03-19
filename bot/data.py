"""
bot/data.py
Yahoo Finance data fetcher with aggressive cache-first behavior.

Rate limit strategy:
  - Try to fetch fresh data per symbol with browser session
  - After 3 consecutive rate limits on a timeframe: STOP fresh fetching,
    return whatever is in disk cache immediately
  - This prevents multi-hour backoff spirals
  - Cache TTL: 23h daily, 4h hourly, 1h 15m
"""
import json
import logging
import random
import time
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

log = logging.getLogger(__name__)

DELAY_MIN       = 8.0
DELAY_MAX       = 12.0
MIN_BARS        = 20
MAX_CONSEC_RL   = 3    # give up fresh fetching after this many consecutive rate limits

_BASE      = Path(__file__).resolve().parent.parent
_CACHE_DIR = _BASE / 'data' / 'bar_cache'
_FOCUS_F   = _BASE / 'data' / 'focus_cache.json'

BAR_TTL = {'1d': 23*3600, '1h': 4*3600, '15m': 60*60}


def _browser_session():
    s = requests.Session()
    s.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/122.0.0.0 Safari/537.36'
        ),
        'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
    })
    return s

_SES = _browser_session()


class RateLimitError(Exception):
    pass


def _cache_path(sym, interval):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f'{sym}_{interval}.json'


def _load_cached(sym, interval):
    """Load from disk cache regardless of TTL — used as fallback."""
    p = _cache_path(sym, interval)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        df = pd.DataFrame.from_dict(d['rows'], orient='index')
        df.index = pd.to_datetime(df.index, utc=True)
        if len(df) >= MIN_BARS:
            return df
    except Exception:
        pass
    return None


def _load_fresh_cached(sym, interval):
    """Load from disk cache only if within TTL."""
    p = _cache_path(sym, interval)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        age = time.time() - d.get('ts', 0)
        if age > BAR_TTL.get(interval, 3600):
            return None
        df = pd.DataFrame.from_dict(d['rows'], orient='index')
        df.index = pd.to_datetime(df.index, utc=True)
        return df if len(df) >= MIN_BARS else None
    except Exception:
        return None


def _save_cached(sym, interval, df):
    try:
        rows = {str(k): v for k, v in df.to_dict(orient='index').items()}
        _cache_path(sym, interval).write_text(
            json.dumps({'ts': time.time(), 'rows': rows})
        )
    except Exception:
        pass


def _fetch_one(sym, period, interval):
    """Fetch a single symbol with browser session. Raises RateLimitError if throttled."""
    try:
        df = yf.Ticker(sym, session=_SES).history(
            period=period, interval=interval, auto_adjust=True
        )
        if df is None or df.empty or len(df) < MIN_BARS:
            return None
        df.columns = [c.lower() for c in df.columns]
        keep = [c for c in ('open', 'high', 'low', 'close', 'volume') if c in df.columns]
        return df[keep]
    except Exception as e:
        err = str(e)
        if any(k in err for k in ('Rate', 'Too Many', '429', 'rate limit', 'TooMany')):
            raise RateLimitError(err)
        return None


def fetch_bars(symbols, period, interval):
    """
    Fetch bars with cache-first, rate-limit-safe behavior.

    Strategy:
    1. If fresh cache exists: use it (no network call)
    2. If no fresh cache: try to fetch from Yahoo
    3. After MAX_CONSEC_RL consecutive rate limits: stop fresh fetching,
       fall back to stale cache for remaining symbols
    4. Return all available data — partial is fine

    Logs: cache hits, cache misses, rate limit events, fallback mode
    """
    result      = {}
    from_cache  = 0
    fetched     = 0
    skipped     = 0
    fallback    = 0
    consec_rl   = 0
    stop_fresh  = False

    for i, sym in enumerate(symbols):
        # Step 1: fresh cache
        cached = _load_fresh_cached(sym, interval)
        if cached is not None:
            result[sym] = cached
            from_cache += 1
            consec_rl = 0
            continue

        # Step 2: if too many consecutive rate limits, fall back to stale cache
        if stop_fresh:
            stale = _load_cached(sym, interval)
            if stale is not None:
                result[sym] = stale
                fallback += 1
            else:
                skipped += 1
            continue

        # Step 3: try fresh fetch
        try:
            df = _fetch_one(sym, period, interval)
            if df is not None:
                result[sym] = df
                _save_cached(sym, interval, df)
                fetched += 1
                consec_rl = 0
            else:
                # Try stale cache before giving up on this symbol
                stale = _load_cached(sym, interval)
                if stale is not None:
                    result[sym] = stale
                    fallback += 1
                else:
                    skipped += 1

        except RateLimitError:
            consec_rl += 1
            log.warning('[DATA] Rate limited: %s (%d/%d) — consecutive: %d/%d',
                        sym, i+1, len(symbols), consec_rl, MAX_CONSEC_RL)

            # Fall back to stale cache for this symbol
            stale = _load_cached(sym, interval)
            if stale is not None:
                result[sym] = stale
                fallback += 1
            else:
                skipped += 1

            if consec_rl >= MAX_CONSEC_RL:
                stop_fresh = True
                log.warning('[DATA] %d consecutive rate limits on %s — '
                            'switching to cache-only mode for remaining %d symbols',
                            MAX_CONSEC_RL, interval, len(symbols) - i - 1)

        time.sleep(DELAY_MIN + random.random() * (DELAY_MAX - DELAY_MIN))

    mode = 'CACHE-ONLY' if stop_fresh else 'NORMAL'
    log.info('[DATA] %s %s [%s]: %d total | %d fresh | %d cache | %d stale | %d skipped',
             interval, period, mode, len(result), fetched, from_cache, fallback, skipped)
    return result


def resample_to_4h(df):
    # Ensure DatetimeIndex before resampling (cache loads may have plain Index)
    if not isinstance(df.index, pd.DatetimeIndex):
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True)
    return df.resample('4h').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
    ).dropna()


def save_focus_cache(symbols):
    try:
        (_BASE / 'data').mkdir(parents=True, exist_ok=True)
        _FOCUS_F.write_text(json.dumps({'symbols': symbols, 'ts': time.time()}))
        log.info('[CACHE] Focus saved: %d symbols', len(symbols))
    except Exception as e:
        log.warning('[CACHE] Save failed: %s', e)


def load_focus_cache(max_age_secs=21600):
    try:
        if not _FOCUS_F.exists():
            return []
        d = json.loads(_FOCUS_F.read_text())
        age = time.time() - d.get('ts', 0)
        if age > max_age_secs:
            log.info('[CACHE] Focus stale (%.1fh)', age/3600)
            return []
        syms = d.get('symbols', [])
        log.info('[CACHE] Focus loaded: %d symbols (%.1fh old)', len(syms), age/3600)
        return syms
    except Exception as e:
        log.warning('[CACHE] Load failed: %s', e)
        return []
