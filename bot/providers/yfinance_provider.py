"""
bot/providers/yfinance_provider.py
Yahoo Finance data provider — the active default for all environments.

Implements both:
  fetch_bars()       used by live scanner (period-based, multi-symbol)
  fetch_bars_range() used by backtest engine (date-range, single symbol)

Cache strategy (live scanner path):
  1. Fresh disk cache within TTL → return immediately
  2. Network fetch via browser-spoofed session
  3. After MAX_CONSEC_RL consecutive rate limits → stale cache fallback

Pacing: 8–12s between live fetches to avoid rate limiting.
Backtest path uses faster pacing (1.5s) since it runs interactively.
"""
import json
import logging
import random
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf

from bot.providers.base import DataProvider

log = logging.getLogger(__name__)

BASE_DIR      = Path(__file__).resolve().parent.parent.parent
_CACHE_DIR    = BASE_DIR / 'data' / 'bar_cache'

# Production TTL values — match the live bot's caching behaviour
MIN_BARS      = 20
MAX_CONSEC_RL = 3
DELAY_MIN     = 8.0    # live scan pacing
DELAY_MAX     = 12.0
BAR_TTL       = {'1d': 23 * 3600, '1h': 4 * 3600, '15m': 3600}


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _browser_session() -> requests.Session:
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


def _cache_path(sym: str, interval: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f'{sym}_{interval}.json'


def _load_cached(sym: str, interval: str) -> Optional[pd.DataFrame]:
    """Stale-ok load — used as fallback when rate limited."""
    p = _cache_path(sym, interval)
    if not p.exists():
        return None
    try:
        d  = json.loads(p.read_text())
        df = pd.DataFrame.from_dict(d['rows'], orient='index')
        df.index = pd.to_datetime(df.index, utc=True)
        if len(df) >= MIN_BARS:
            return df
    except Exception:
        pass
    return None


def _load_fresh_cached(sym: str, interval: str) -> Optional[pd.DataFrame]:
    """TTL-checked load."""
    p = _cache_path(sym, interval)
    if not p.exists():
        return None
    try:
        d   = json.loads(p.read_text())
        age = time.time() - d.get('ts', 0)
        if age > BAR_TTL.get(interval, 3600):
            return None
        df = pd.DataFrame.from_dict(d['rows'], orient='index')
        df.index = pd.to_datetime(df.index, utc=True)
        return df if len(df) >= MIN_BARS else None
    except Exception:
        return None


def _save_cached(sym: str, interval: str, df: pd.DataFrame) -> None:
    try:
        rows = {str(k): v for k, v in df.to_dict(orient='index').items()}
        _cache_path(sym, interval).write_text(
            json.dumps({'ts': time.time(), 'rows': rows})
        )
    except Exception:
        pass


class RateLimitError(Exception):
    pass


# ── Rate-limit detection helpers (audit-P1-data-rate-limit-fix) ───────────────
# Mirror the M16 pattern in bot/historical/providers_yfinance.py.
#
# Background: yfinance >= 0.2 catches per-symbol exceptions inside its
# fetch routines (including YFRateLimitError) and stashes them in
# `yf.shared._ERRORS[symbol]` while returning an empty DataFrame.
# Inspecting that registry is the ONLY reliable way to distinguish a
# rate-limited-empty response from a genuinely-no-data-empty response
# without relying on str(exc) heuristics.
#
# Helpers are kept module-level (not class methods) so bot/backtest.py
# can import them too — single source of truth across both old call
# sites, with no inheritance / cross-import coupling to the M16 tree.

# Substrings (lower-cased) that mean "rate limited" in either an
# exception message or a yf.shared._ERRORS entry. Belt-and-braces for
# upstream signal drift; isinstance and type-name checks come FIRST.
_RATE_LIMIT_TOKENS = (
    "yfratelimiterror",     # repr of yfinance.exceptions.YFRateLimitError
    "rate limit",
    "rate-limit",
    "rate limited",
    "too many requests",
    "429",
)


def _is_rate_limit_signal(text) -> bool:
    """True iff `text` (str or any stringifiable) contains a rate-limit
    token. Tolerant of None / non-string input."""
    if not text:
        return False
    try:
        lower = str(text).lower()
    except Exception:
        return False
    return any(tok in lower for tok in _RATE_LIMIT_TOKENS)


def _yf_rate_limit_exc_class():
    """Return yfinance.exceptions.YFRateLimitError if importable, else
    None. yfinance < 0.2 didn't have it; defensive import keeps the
    provider importable in any supported version."""
    try:
        from yfinance.exceptions import YFRateLimitError
        return YFRateLimitError
    except Exception:
        return None


def _clear_yf_errors(yf_mod) -> None:
    """Best-effort clear of `yf.shared._ERRORS`. Idempotent and
    defensive: a yfinance build without `shared._ERRORS` is treated as
    a no-op rather than an error."""
    try:
        shared = getattr(yf_mod, "shared", None)
        if shared is None:
            return
        errors = getattr(shared, "_ERRORS", None)
        if errors is None:
            return
        try:
            errors.clear()
        except Exception:
            pass
    except Exception:
        pass


def _scan_yf_errors_for_rate_limit(yf_mod, symbol: str):
    """Inspect `yf.shared._ERRORS` for a rate-limit signal.

    Returns the rate-limit message string if found, else None.

    Looks at the entry keyed by `symbol` first, then any other entries
    (the registry is global; we cleared it before THIS call so any
    entries present at scan time belong to this call). This mirrors
    `bot/historical/providers_yfinance.py::_scan_yf_errors_for_rate_limit`.
    """
    try:
        shared = getattr(yf_mod, "shared", None)
        if shared is None:
            return None
        errors = getattr(shared, "_ERRORS", None)
        if not errors:
            return None
    except Exception:
        return None

    # Prefer this symbol's entry; fall back to any other entry.
    candidates = []
    try:
        if symbol in errors:
            candidates.append(errors[symbol])
        for k, v in errors.items():
            if k != symbol:
                candidates.append(v)
    except Exception:
        return None

    for msg in candidates:
        if _is_rate_limit_signal(msg):
            return str(msg)
    return None


def _scan_yf_errors_for_other_error(yf_mod, symbol: str):
    """Return non-rate-limit error message for `symbol` if present.
    Called after `_scan_yf_errors_for_rate_limit` returned None; any
    remaining entry for this symbol is worth surfacing at WARNING."""
    try:
        shared = getattr(yf_mod, "shared", None)
        if shared is None:
            return None
        errors = getattr(shared, "_ERRORS", None)
        if not errors:
            return None
        own = errors.get(symbol)
        if own and not _is_rate_limit_signal(own):
            return str(own)
    except Exception:
        return None
    return None


def _is_rate_limit_exception(exc: BaseException) -> bool:
    """Layered detection — isinstance, then type-name, then substring.
    Returns True iff `exc` represents a yfinance rate-limit signal."""
    rl_cls = _yf_rate_limit_exc_class()
    if rl_cls is not None and isinstance(exc, rl_cls):
        return True
    if type(exc).__name__ == "YFRateLimitError":
        return True
    return _is_rate_limit_signal(str(exc))


# ── Provider ──────────────────────────────────────────────────────────────────

class YFinanceProvider(DataProvider):
    """
    Yahoo Finance data provider.
    Single shared browser session per process lifetime.
    """

    _session: Optional[requests.Session] = None

    @property
    def name(self) -> str:
        return 'yfinance (Yahoo Finance)'

    @property
    def capabilities(self) -> dict:
        return {
            'supported_timeframes': ['1d', '1h', '15m'],
            'max_history_days':     {'1d': 730, '1h': 730, '15m': 60},
            'intraday':             True,
            'benchmark':            True,
            'real_time':            False,
            'notes': (
                '15m data limited to last 60 days. '
                '1H data limited to last 730 days. '
                'May be rate-limited on shared IPs.'
            ),
        }

    # ── Session ───────────────────────────────────────────────────────────────

    def _get_session(self) -> requests.Session:
        if YFinanceProvider._session is None:
            YFinanceProvider._session = _browser_session()
        return YFinanceProvider._session

    # ── Live scanner path (period-based, multi-symbol) ────────────────────────

    def _fetch_one(self, sym: str, period: str, interval: str) -> Optional[pd.DataFrame]:
        # audit-P1-data-rate-limit-fix (2026-06-05): clear yfinance's
        # per-symbol error registry BEFORE the call so any entry
        # present at scan time can be attributed to this call. Without
        # this, yfinance silently swallows rate-limit exceptions into
        # _ERRORS while returning an empty DataFrame, and the empty df
        # gets misclassified here as "no_data".
        _clear_yf_errors(yf)

        try:
            df = yf.Ticker(sym, session=self._get_session()).history(
                period=period, interval=interval, auto_adjust=True
            )
        except Exception as e:
            # Layered detection: isinstance(YFRateLimitError),
            # type-name match, then substring fallback. Strictly more
            # reliable than the pre-fix str(e)-only heuristic.
            if _is_rate_limit_exception(e):
                raise RateLimitError(str(e))
            return None

        if df is None or df.empty:
            # Empty df can mean either "no data" OR "rate limited and
            # yfinance swallowed it". Disambiguate via _ERRORS.
            rl_msg = _scan_yf_errors_for_rate_limit(yf, sym)
            if rl_msg is not None:
                raise RateLimitError(rl_msg)
            # Non-rate-limit error for this symbol — surface at
            # WARNING (operator visibility), then treat as no_data.
            other_msg = _scan_yf_errors_for_other_error(yf, sym)
            if other_msg is not None:
                log.warning('[DATA] %s %s: yfinance error (non-RL): %s',
                              sym, interval, str(other_msg)[:120])
            return None

        if len(df) < MIN_BARS:
            return None

        df.columns = [c.lower() for c in df.columns]
        keep = [c for c in ('open', 'high', 'low', 'close', 'volume') if c in df.columns]
        return df[keep]

    def fetch_bars(self, symbols: list, period: str, interval: str) -> Dict[str, pd.DataFrame]:
        """
        Cache-first, rate-limit-safe multi-symbol fetch for the live scanner.
        """
        result     = {}
        from_cache = fetched = skipped = fallback = consec_rl = 0
        stop_fresh = False

        for i, sym in enumerate(symbols):
            cached = _load_fresh_cached(sym, interval)
            if cached is not None:
                result[sym] = cached
                from_cache += 1
                consec_rl = 0
                continue

            if stop_fresh:
                stale = _load_cached(sym, interval)
                if stale is not None:
                    result[sym] = stale; fallback += 1
                else:
                    skipped += 1
                continue

            try:
                df = self._fetch_one(sym, period, interval)
                if df is not None:
                    result[sym] = df
                    _save_cached(sym, interval, df)
                    fetched += 1; consec_rl = 0
                else:
                    stale = _load_cached(sym, interval)
                    if stale is not None:
                        result[sym] = stale; fallback += 1
                    else:
                        skipped += 1

            except RateLimitError:
                consec_rl += 1
                log.warning('[DATA] Rate limited: %s (%d/%d) consec:%d/%d',
                            sym, i+1, len(symbols), consec_rl, MAX_CONSEC_RL)
                stale = _load_cached(sym, interval)
                if stale is not None:
                    result[sym] = stale; fallback += 1
                else:
                    skipped += 1
                if consec_rl >= MAX_CONSEC_RL:
                    stop_fresh = True
                    log.warning('[DATA] switching to cache-only for remaining %d symbols',
                                len(symbols) - i - 1)

            time.sleep(DELAY_MIN + random.random() * (DELAY_MAX - DELAY_MIN))

        mode = 'CACHE-ONLY' if stop_fresh else 'NORMAL'
        log.info('[DATA] %s %s [%s]: total=%d fresh=%d cache=%d stale=%d skip=%d',
                 interval, period, mode, len(result), fetched, from_cache, fallback, skipped)
        return result

    # ── Backtest path (date-range, single symbol) ─────────────────────────────

    def fetch_bars_range(
        self,
        sym: str,
        interval: str,
        start: date,
        end: date,
    ) -> Tuple[Optional[pd.DataFrame], str]:
        """
        Fetch bars for a single symbol over a date range.
        3 attempts with 12/24s backoff on rate limiting.
        Returns (df_or_None, status_str).
        """
        # Check 15m availability
        if interval == '15m':
            cap = self.capabilities['max_history_days'].get('15m', 60)
            from datetime import date as date_cls
            if (date_cls.today() - start).days > cap:
                log.warning('[PROV] %s 15m: requested start %s > %d day limit', sym, start, cap)

        for attempt in range(3):
            if attempt > 0:
                wait = attempt * 12
                log.warning('[PROV] %s %s: rate limited — waiting %ds (attempt %d/3)',
                            sym, interval, wait, attempt + 1)
                time.sleep(wait)

            # audit-P1-data-rate-limit-fix (2026-06-05): clear yfinance's
            # per-symbol error registry BEFORE each attempt. With
            # raise_errors=False below, yfinance swallows rate-limit
            # exceptions into _ERRORS and returns an empty DataFrame;
            # without inspecting _ERRORS we cannot distinguish a
            # rate-limited-empty response from a genuinely-no-data-
            # empty response, so the pre-fix code silently
            # misclassified rate-limits as 'empty_response' and never
            # retried.
            _clear_yf_errors(yf)

            try:
                ses = self._get_session()
                df  = yf.Ticker(sym, session=ses).history(
                    start        = start.strftime('%Y-%m-%d'),
                    end          = (end + timedelta(days=2)).strftime('%Y-%m-%d'),
                    interval     = interval,
                    auto_adjust  = True,
                    actions      = False,
                    raise_errors = False,
                )

                if df is None or df.empty:
                    # Disambiguate: swallowed rate-limit vs genuinely
                    # empty. The retry-with-backoff path is gated on
                    # the same flag (`is_rl`) the except branch uses
                    # so both code paths behave identically.
                    rl_msg = _scan_yf_errors_for_rate_limit(yf, sym)
                    if rl_msg is not None:
                        log.warning('[PROV] %s %s: swallowed rate-limit '
                                      'detected via _ERRORS (attempt %d/3)',
                                      sym, interval, attempt + 1)
                        if attempt < 2:
                            continue
                        return None, 'rate_limited'
                    # Non-rate-limit error worth surfacing.
                    other_msg = _scan_yf_errors_for_other_error(yf, sym)
                    if other_msg is not None:
                        log.warning('[PROV] %s %s: yfinance error '
                                      '(non-RL): %s',
                                      sym, interval,
                                      str(other_msg)[:120])
                    return None, 'empty_response'

                df.columns = [c.lower() for c in df.columns]
                keep = [c for c in ('open', 'high', 'low', 'close', 'volume') if c in df.columns]
                if not keep:
                    return None, 'empty_response'

                df = df[keep].dropna()

                if not isinstance(df.index, pd.DatetimeIndex):
                    df.index = pd.to_datetime(df.index, utc=True)
                elif df.index.tz is None:
                    df.index = df.index.tz_localize('UTC')
                else:
                    df.index = df.index.tz_convert('UTC')

                if len(df) < MIN_BARS:
                    return None, f'too_few_bars_{len(df)}'

                time.sleep(1.5)
                return df, 'ok'

            except Exception as e:
                # Layered detection: isinstance(YFRateLimitError),
                # type-name, then substring fallback.
                is_rl = _is_rate_limit_exception(e)
                if is_rl and attempt < 2:
                    continue
                if is_rl:
                    return None, 'rate_limited'
                err = str(e)
                if any(k in err for k in ('403', 'Forbidden', 'proxy', 'tunnel')):
                    return None, 'network_error'
                log.warning('[PROV] %s %s error: %s', sym, interval, err[:80])
                return None, f'error:{err[:80]}'

        return None, 'rate_limited'
