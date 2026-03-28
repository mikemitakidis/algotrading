"""
bot/providers/__init__.py
Data provider factory for Milestone 6.

Usage:
    from bot.providers import get_provider
    provider = get_provider()
    bars = provider.fetch_bars(symbols, period, interval)

Active provider selected by DATA_PROVIDER env var (default: yfinance).
Supported values: yfinance, alpaca (placeholder — not implemented).
"""
import os
from bot.providers.base import DataProvider

_SUPPORTED = ('yfinance', 'alpaca')


def get_provider() -> DataProvider:
    """Return the configured data provider. Raises ValueError for unknown names."""
    name = os.getenv('DATA_PROVIDER', 'yfinance').lower().strip()
    if name == 'yfinance':
        from bot.providers.yfinance_provider import YFinanceProvider
        return YFinanceProvider()
    if name == 'alpaca':
        from bot.providers.alpaca_provider import AlpacaProvider
        return AlpacaProvider()
    raise ValueError(
        f"Unknown DATA_PROVIDER='{name}'. Supported: {', '.join(_SUPPORTED)}"
    )


def get_provider_name() -> str:
    """Return the active provider name string without instantiating it."""
    name = os.getenv('DATA_PROVIDER', 'yfinance').lower().strip()
    if name == 'yfinance':
        return 'yfinance (Yahoo Finance)'
    if name == 'alpaca':
        return 'Alpaca Markets (historical data)'
    return f'unknown:{name}'


__all__ = ['DataProvider', 'get_provider', 'get_provider_name']
