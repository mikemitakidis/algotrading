"""
bot/data.py
Yahoo Finance data fetcher.

Root cause of rate limits on VPS:
  yf.download() is flagged as bot traffic. Fix: use yf.Ticker(sym).history()
  with a browser-like requests.Session — different code path, passes bot check.

Design:
  - One symbol at a time, 2-5s random delay
  - Browser User-Agent and headers
  - Disk cache per symbol (skip re-fetch if fresh)
  - 60s backoff on rate limit, then continue
  - Never crashes the bot
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

DELAY_MIN  = 2.0
DELAY_MAX  = 5.0
MIN_BARS   = 30

_BASE      = Path(__file__).resolve().parent.parent
_CACHE_DIR = _BASE / 'data' / 'bar_cache'
_FOCUS_F   = _BASE / 'data' / 'focus_cache.json'

BAR_TTL = {'1d': 6*3600, '1h': 2*3600, '15m': 30*60}


def _session():
    s = requests.Session()
    s.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/122.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    return s

_SES = _session()


class RateLimitError(Exception):
    pass


def _cache_path(sym, interval):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f'{sym}_{interval}.json'


def _load_cached(sym, interval):
    p = _cache_path(sym, interval)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        if time.time() - d.get('ts', 0) > BAR_TTL.get(interval, 3600):
            return None
        df = pd.DataFrame.from_dict(d['rows'], orient='index')
        df.index = pd.to_datetime(df.index)
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
    try:
        df = yf.Ticker(sym, session=_SES).history(
            period=period, interval=interval, auto_adjust=True
        )
        if df is None or df.empty or len(df) < MIN_BARS:
            return None
        df.columns = [c.lower() for c in df.columns]
        keep = [c for c in ('open','high','low','close','volume') if c in df.columns]
        return df[keep]
    except Exception as e:
        err = str(e)
        if any(k in err for k in ('Rate','Too Many','429','rate limit','TooMany')):
            raise RateLimitError(err)
        log.debug('[DATA] %s %s: %s', sym, interval, err[:60])
        return None


def fetch_bars(symbols, period, interval):
    result = {}
    from_cache = fetched = skipped = 0
    rl_wait = 0

    for i, sym in enumerate(symbols):
        cached = _load_cached(sym, interval)
        if cached is not None:
            result[sym] = cached
            from_cache += 1
            continue

        if rl_wait > 0:
            log.warning('[DATA] Rate limit backoff %ds...', rl_wait)
            time.sleep(rl_wait)
            rl_wait = 0

        try:
            df = _fetch_one(sym, period, interval)
            if df is not None:
                result[sym] = df
                _save_cached(sym, interval, df)
                fetched += 1
            else:
                skipped += 1
        except RateLimitError:
            rl_wait = 60
            skipped += 1
            log.warning('[DATA] Rate limited at %s (%d/%d) — next fresh fetch waits 60s',
                        sym, i+1, len(symbols))

        time.sleep(DELAY_MIN + random.random() * (DELAY_MAX - DELAY_MIN))

    log.info('[DATA] %s %s: %d symbols | %d fetched | %d cache | %d skipped',
             interval, period, len(result), fetched, from_cache, skipped)
    return result


def resample_to_4h(df):
    return df.resample('4h').agg(
        {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}
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
