"""
bot/providers/alpaca_provider.py
Alpaca Markets data provider — historical data only (M9 unblock).

Scope: read-only historical OHLCV for backtest use.
       No order execution. No live trading. Data only.

Requires:
  pip install alpaca-py
  ALPACA_KEY=<key>     in .env
  ALPACA_SECRET=<secret> in .env
  DATA_PROVIDER=alpaca   in .env  (or per-run via env var)

Timeframe mapping (Alpaca → internal):
  '1d'  → TimeFrame.Day
  '1h'  → TimeFrame.Hour
  '15m' → TimeFrame(15, TimeFrameUnit.Minute)

Free tier supports full daily history (5+ years).
Intraday history available depending on subscription.
"""
import logging
import os
from datetime import date, timedelta
from typing import Dict, Optional, Tuple

import pandas as pd

from bot.providers.base import DataProvider

log = logging.getLogger(__name__)


def _get_keys() -> tuple[str, str]:
    key    = os.getenv('ALPACA_KEY', '').strip()
    secret = os.getenv('ALPACA_SECRET', '').strip()
    return key, secret


def _interval_to_timeframe(interval: str):
    """Convert internal interval string to Alpaca TimeFrame."""
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    mapping = {
        '1d':  TimeFrame.Day,
        '1h':  TimeFrame.Hour,
        '15m': TimeFrame(15, TimeFrameUnit.Minute),
        '5m':  TimeFrame(5,  TimeFrameUnit.Minute),
    }
    if interval not in mapping:
        raise ValueError(f"Unsupported interval: {interval}")
    return mapping[interval]


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize Alpaca response to standard lowercase OHLCV DataFrame."""
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    keep = [c for c in ('open', 'high', 'low', 'close', 'volume') if c in df.columns]
    df = df[keep]
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize('UTC')
    else:
        df.index = df.index.tz_convert('UTC')
    return df.sort_index().dropna()


class AlpacaProvider(DataProvider):
    """
    Alpaca Markets historical data provider.
    Read-only. No order execution.
    Uses alpaca-py StockHistoricalDataClient.
    """

    @property
    def name(self) -> str:
        return 'Alpaca Markets (historical data)'

    @property
    def capabilities(self) -> dict:
        return {
            'supported_timeframes': ['1d', '1h', '15m', '5m'],
            'max_history_days':     {'1d': 3650, '1h': 730, '15m': 30},
            'intraday':             True,
            'benchmark':            True,
            'real_time':            False,
            'notes': (
                'Historical data only. No order execution. '
                'Requires ALPACA_KEY and ALPACA_SECRET in .env. '
                'Free tier: full daily history. Intraday varies by plan.'
            ),
        }

    def _client(self):
        from alpaca.data.historical import StockHistoricalDataClient
        key, secret = _get_keys()
        if not key or not secret:
            raise ValueError(
                'ALPACA_KEY and ALPACA_SECRET must be set in .env '
                'to use DATA_PROVIDER=alpaca'
            )
        return StockHistoricalDataClient(key, secret)

    # ── Live scanner path (period-based, multi-symbol) ────────────────────────

    def fetch_bars(self, symbols: list, period: str, interval: str) -> Dict[str, pd.DataFrame]:
        """
        Fetch recent bars for live scanner. Converts period string to date range.
        """
        from alpaca.data.requests import StockBarsRequest
        # Convert period string to days
        period_days = {
            '1d': 1, '5d': 5, '1mo': 30, '3mo': 90,
            '6mo': 180, '1y': 365, '2y': 730,
        }
        days  = period_days.get(period, 90)
        end   = date.today()
        start = end - timedelta(days=days)

        result = {}
        try:
            client    = self._client()
            timeframe = _interval_to_timeframe(interval)
            req = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=timeframe,
                start=start.isoformat(),
                end=end.isoformat(),
            )
            bars = client.get_stock_bars(req).df
            if bars.empty:
                return result
            # Multi-symbol response has (symbol, timestamp) MultiIndex
            if isinstance(bars.index, pd.MultiIndex):
                for sym in bars.index.get_level_values(0).unique():
                    df = bars.xs(sym, level=0)
                    df = _normalize(df)
                    if len(df) >= 20:
                        result[sym] = df
            else:
                df = _normalize(bars)
                if len(df) >= 20:
                    result[symbols[0]] = df
        except Exception as e:
            log.warning('[ALPACA] fetch_bars error: %s', str(e)[:120])
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
        Fetch historical bars for backtest. Returns (df, status).
        """
        from alpaca.data.requests import StockBarsRequest
        try:
            client    = self._client()
            timeframe = _interval_to_timeframe(interval)
            req = StockBarsRequest(
                symbol_or_symbols=sym,
                timeframe=timeframe,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
            )
            bars = client.get_stock_bars(req).df
            if bars.empty:
                return None, 'empty_response'

            # Single symbol: may have symbol level in index
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(sym, level=0)

            df = _normalize(bars)
            if len(df) < 20:
                return None, f'too_few_bars_{len(df)}'

            log.info('[ALPACA] %s %s: %d bars (%s→%s)',
                     sym, interval, len(df),
                     df.index[0].date(), df.index[-1].date())
            return df, 'ok'

        except ValueError as e:
            return None, f'unsupported_timeframe:{e}'
        except Exception as e:
            err = str(e)[:120]
            log.warning('[ALPACA] %s %s error: %s', sym, interval, err)
            if 'credential' in err.lower() or 'unauthorized' in err.lower() or '403' in err:
                return None, 'auth_error'
            if 'forbidden' in err.lower():
                return None, 'subscription_required'
            return None, f'error:{err}'
