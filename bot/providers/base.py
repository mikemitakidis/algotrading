"""
bot/providers/base.py
Abstract DataProvider interface for Milestone 6.

Any data source (yfinance, Alpaca, Polygon, IBKR …) must subclass DataProvider
and implement fetch_bars() and fetch_bars_range().
Strategy, scanner, and backtest code never imports a provider directly — they
go through get_provider() from bot.providers.
"""
from abc import ABC, abstractmethod
from datetime import date
from typing import Dict, Optional, Tuple
import pandas as pd


class DataProvider(ABC):
    """
    Abstract base class for all market-data providers.

    DataFrame contract (all methods):
      - columns: open, high, low, close, volume (lowercase)
      - index: UTC-aware DatetimeIndex, ascending
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Short human-readable name, e.g. 'yfinance (Yahoo Finance)'."""

    @property
    def capabilities(self) -> dict:
        """
        Provider capability metadata used by dashboard and backtest to
        display limits and warn users honestly.

        Keys (all optional with safe defaults):
            supported_timeframes : list[str]   intervals this provider supports
            max_history_days     : dict         {interval: max_days_lookback}
            intraday             : bool         supports sub-daily intervals
            benchmark            : bool         can fetch benchmark (SPY etc.)
            real_time            : bool         provides live/streaming quotes
            notes                : str          free-text caveats
        """
        return {
            'supported_timeframes': ['1d'],
            'max_history_days':     {'1d': 365},
            'intraday':             False,
            'benchmark':            False,
            'real_time':            False,
            'notes':                '',
        }

    # ── Live / scanner fetch ──────────────────────────────────────────────────

    @abstractmethod
    def fetch_bars(self, symbols: list, period: str, interval: str) -> Dict[str, pd.DataFrame]:
        """
        Fetch recent OHLCV bars for multiple symbols using a lookback period.
        Used by the live scanner.

        Parameters
        ----------
        symbols  : list[str]  ticker strings
        period   : str        yfinance-style lookback ('3mo', '1mo', '5d', …)
        interval : str        bar size ('1d', '1h', '15m', …)

        Returns
        -------
        dict  {symbol -> DataFrame}   missing symbols simply absent
        """

    # ── Backtest / date-range fetch ───────────────────────────────────────────

    @abstractmethod
    def fetch_bars_range(
        self,
        sym: str,
        interval: str,
        start: date,
        end: date,
    ) -> Tuple[Optional[pd.DataFrame], str]:
        """
        Fetch OHLCV bars for a single symbol over an explicit date range.
        Used by the backtest engine.

        Returns
        -------
        (df, status) where status is one of:
            'ok'                   — data returned
            'empty_response'       — Yahoo returned nothing
            'rate_limited'         — throttled after retries
            'too_few_bars_N'       — data present but < MIN_BARS
            'unsupported_timeframe'— provider can't supply this interval
            'network_error'        — connection/proxy failure
            'not_implemented'      — placeholder provider
            'error:<msg>'          — other exception
        """

    # ── Shared utilities ──────────────────────────────────────────────────────

    def resample_to_4h(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Resample a UTC-indexed OHLCV DataFrame to 4-hour bars.
        Default works for any provider with standard bar data.
        Override only if the provider supplies pre-aggregated 4H data.
        """
        if not isinstance(df.index, pd.DatetimeIndex):
            df = df.copy()
            df.index = pd.to_datetime(df.index, utc=True)
        return df.resample('4h').agg(
            {'open': 'first', 'high': 'max', 'low': 'min',
             'close': 'last', 'volume': 'sum'}
        ).dropna()
