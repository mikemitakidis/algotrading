"""M20.UA exchange <-> provider-suffix <-> country/currency/timezone/calendar
mapping table.

Static and explicit: suffixes are NEVER guessed dynamically. Every supported
exchange has one canonical yfinance suffix and a consistent country/currency/
timezone/calendar tuple. US venues (NASDAQ/NYSE/ARCA) use no suffix. Non-US
venues are defined here for later phases (M20.UB/UD); the M20.UA seed only
exercises the US rows.

Pure module: no I/O, no network, no imports beyond the standard library.
"""
from __future__ import annotations

from typing import Dict, NamedTuple, Optional


class ExchangeInfo(NamedTuple):
    yfinance_suffix: str        # appended to the local ticker for yfinance
    country: str                # ISO-3166 alpha-2
    currency: str               # ISO-4217
    timezone: str               # IANA tz
    trading_calendar: str       # MIC-style calendar id
    region: str                 # coarse region bucket


# Keyed by internal EXCHANGE prefix (the part before the ':').
EXCHANGES: Dict[str, ExchangeInfo] = {
    # ── United States (no yfinance suffix) ──
    "NASDAQ": ExchangeInfo("", "US", "USD", "America/New_York", "XNAS", "US"),
    "NYSE":   ExchangeInfo("", "US", "USD", "America/New_York", "XNYS", "US"),
    "ARCA":   ExchangeInfo("", "US", "USD", "America/New_York", "XNYS", "US"),
    # ── Defined for later phases (not exercised by the M20.UA US seed) ──
    "LSE":    ExchangeInfo(".L",  "GB", "GBP", "Europe/London",    "XLON", "UK"),
    "TSE":    ExchangeInfo(".T",  "JP", "JPY", "Asia/Tokyo",       "XTKS", "JP"),
    "HKEX":   ExchangeInfo(".HK", "HK", "HKD", "Asia/Hong_Kong",   "XHKG", "HK"),
    "XETRA":  ExchangeInfo(".DE", "DE", "EUR", "Europe/Berlin",    "XETR", "EU"),
    "EPA":    ExchangeInfo(".PA", "FR", "EUR", "Europe/Paris",     "XPAR", "EU"),
    "AEX":    ExchangeInfo(".AS", "NL", "EUR", "Europe/Amsterdam", "XAMS", "EU"),
    "BME":    ExchangeInfo(".MC", "ES", "EUR", "Europe/Madrid",    "XMAD", "EU"),
    "SIX":    ExchangeInfo(".SW", "CH", "CHF", "Europe/Zurich",    "XSWX", "EU"),
}

KNOWN_EXCHANGES = frozenset(EXCHANGES)
KNOWN_CURRENCIES = frozenset(e.currency for e in EXCHANGES.values())
KNOWN_REGIONS = frozenset(e.region for e in EXCHANGES.values())


def split_internal_symbol(internal_symbol: str) -> tuple:
    """Split 'EXCHANGE:TICKER' -> (exchange, ticker). Raise ValueError on a
    malformed internal symbol."""
    if not isinstance(internal_symbol, str) or internal_symbol.count(":") != 1:
        raise ValueError(
            f"internal_symbol must be 'EXCHANGE:TICKER', got "
            f"{internal_symbol!r}")
    exchange, ticker = internal_symbol.split(":")
    if not exchange or not ticker:
        raise ValueError(
            f"internal_symbol must be 'EXCHANGE:TICKER', got "
            f"{internal_symbol!r}")
    return exchange, ticker


def exchange_info(exchange: str) -> ExchangeInfo:
    if exchange not in EXCHANGES:
        raise ValueError(f"unknown exchange: {exchange!r}")
    return EXCHANGES[exchange]


def to_yfinance_symbol(internal_symbol: str) -> str:
    """Map an internal 'EXCHANGE:TICKER' to its canonical yfinance symbol via
    the static suffix table. Hong Kong codes are zero-padded to 4 digits."""
    exchange, ticker = split_internal_symbol(internal_symbol)
    info = exchange_info(exchange)
    if exchange == "HKEX":
        ticker = ticker.zfill(4)
    return f"{ticker}{info.yfinance_suffix}"


def expected_consistency(exchange: str) -> Optional[ExchangeInfo]:
    """Return the expected (country, currency, timezone, calendar, region) for
    an exchange, or None if unknown."""
    return EXCHANGES.get(exchange)
