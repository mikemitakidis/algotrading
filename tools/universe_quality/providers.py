"""M21.UQ provider interface + result normalization (read-only, offline-safe).

Defines the contract a price/volume provider must satisfy, plus a normalizer
that turns arbitrary provider payloads (list-of-dicts, yfinance-style rows) into
a clean list[ProviderBar]. Unit tests inject a FixtureProvider; no live network
is used in tests. A real yfinance adapter can be added later behind the same
ProviderProtocol without touching the evaluators.
"""
import math
from typing import Dict, List, Optional, Protocol

from tools.universe_quality.quality_model import ProviderBar


class ProviderProtocol(Protocol):
    """A provider returns OHLCV bars for a provider symbol, or None if the
    symbol is unknown/unfetchable. Implementations MUST NOT mutate inputs."""

    def fetch_ohlcv(self, provider_symbol: str) -> Optional[List[dict]]:
        ...


class FixtureProvider:
    """Offline provider backed by an in-memory dict for deterministic tests.

    mapping: {provider_symbol: list[dict] | None}. A symbol mapped to None (or
    absent) simulates an unfetchable/unknown symbol.
    """

    def __init__(self, mapping: Dict[str, Optional[List[dict]]]):
        self._mapping = dict(mapping)

    def fetch_ohlcv(self, provider_symbol: str) -> Optional[List[dict]]:
        return self._mapping.get(provider_symbol)


def _is_finite_number(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def normalize_bars(raw: Optional[List[dict]]) -> Optional[List[ProviderBar]]:
    """Normalize a raw provider payload into list[ProviderBar].

    Returns None if raw is None (unfetchable). Returns [] if raw is an empty
    list (fetched but no data). Rows missing required keys are skipped, NOT
    silently coerced; a row with a non-finite OHLC/volume is KEPT (so the
    non-finite check can flag it) but normalized to float where possible.
    Accepts keys: date/Date, open/Open, high/High, low/Low, close/Close,
    volume/Volume.
    """
    if raw is None:
        return None
    bars: List[ProviderBar] = []

    def g(row, *keys):
        for k in keys:
            if k in row:
                return row[k]
        return None

    for row in raw:
        if not isinstance(row, dict):
            continue
        date = g(row, "date", "Date")
        if date is None:
            continue
        o = g(row, "open", "Open")
        h = g(row, "high", "High")
        low = g(row, "low", "Low")
        c = g(row, "close", "Close")
        v = g(row, "volume", "Volume")

        def num(x):
            # keep NaN/inf as-is so non-finite checks can catch them; coerce
            # clean numerics to float; leave un-coercible as the raw value.
            try:
                return float(x)
            except (TypeError, ValueError):
                return x
        bars.append(ProviderBar(
            date=str(date), open=num(o), high=num(h), low=num(low),
            close=num(c), volume=num(v)))
    return bars


def bars_all_finite(bars: List[ProviderBar]) -> bool:
    for b in bars:
        for val in (b.open, b.high, b.low, b.close, b.volume):
            if not _is_finite_number(val):
                return False
    return True
