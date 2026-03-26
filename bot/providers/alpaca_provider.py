"""
bot/providers/alpaca_provider.py
Alpaca Markets data provider — PLACEHOLDER for Milestone 11+.

This provider is NOT implemented. It exists to:
  1. Prove the interface is provider-agnostic (set DATA_PROVIDER=alpaca → honest error)
  2. Reserve the implementation slot for Milestone 11 (IBKR paper trading uses Alpaca data)
  3. Document the capability contract for future implementors

To activate when ready:
  pip install alpaca-py
  set ALPACA_KEY, ALPACA_SECRET in .env
  set DATA_PROVIDER=alpaca in .env
"""
from datetime import date
from typing import Dict, Optional, Tuple
import pandas as pd

from bot.providers.base import DataProvider


class AlpacaProvider(DataProvider):
    """
    Alpaca Markets data provider.
    NOT IMPLEMENTED — raises NotImplementedError on all data calls.
    Planned for Milestone 11 (IBKR paper trading).
    """

    @property
    def name(self) -> str:
        return 'Alpaca Markets (not implemented)'

    @property
    def capabilities(self) -> dict:
        return {
            'supported_timeframes': ['1d', '1h', '15m', '5m', '1m'],
            'max_history_days':     {'1d': 3650, '1h': 3650, '15m': 3650},
            'intraday':             True,
            'benchmark':            True,
            'real_time':            True,
            'notes': (
                'NOT IMPLEMENTED. Requires ALPACA_KEY and ALPACA_SECRET env vars. '
                'Planned for Milestone 11. Set DATA_PROVIDER=yfinance to proceed.'
            ),
        }

    def fetch_bars(self, symbols: list, period: str, interval: str) -> Dict[str, pd.DataFrame]:
        raise NotImplementedError(
            'AlpacaProvider.fetch_bars() is not implemented. '
            'Set DATA_PROVIDER=yfinance or implement this provider in Milestone 11.'
        )

    def fetch_bars_range(
        self,
        sym: str,
        interval: str,
        start: date,
        end: date,
    ) -> Tuple[Optional[pd.DataFrame], str]:
        return None, 'not_implemented'
