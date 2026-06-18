"""M20.UA symbol-registry contract.

Frozen, standard-library dataclass. The full SymbolRecord contract (26 fields)
is frozen now; liquidity/quality fields are Optional and stay null in the seed
(populated later in M20.UC). Records validate exchange/suffix/country/currency/
timezone/calendar consistency against bot.universe.suffixes. No I/O, no network.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.universe import suffixes

SCHEMA_VERSION = "m20_universe_v1"


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    ADR = "ADR"


class DataQualityStatus(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"


def _require_utc(ts: Any, field_name: str) -> str:
    if not isinstance(ts, str) or not ts.strip():
        raise ValueError(f"{field_name} must be a non-empty ISO UTC string")
    s = ts.strip()
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"{field_name} invalid ISO timestamp: {ts!r} ({e})")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC: {ts!r}")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC (+00:00/Z): {ts!r}")
    return s


def _require_date(d: Any, field_name: str) -> str:
    """Validate a YYYY-MM-DD date string."""
    if not isinstance(d, str) or not d.strip():
        raise ValueError(f"{field_name} must be a non-empty YYYY-MM-DD string")
    try:
        datetime.strptime(d.strip(), "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"{field_name} invalid date: {d!r} ({e})")
    return d.strip()


def _opt_non_negative(value: Any, field_name: str) -> None:
    """When present, reject bool, non-numeric, NaN/inf, and negatives."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a non-negative number or null, "
                         f"got {value!r}")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{field_name} must be finite, got {value!r}")
    if value < 0:
        raise ValueError(f"{field_name} must be >= 0, got {value!r}")


def _coerce_enum(value, enum_cls, field_name):
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise ValueError(f"{field_name}: unknown {enum_cls.__name__} "
                             f"{value!r}")
    raise ValueError(f"{field_name}: unknown {enum_cls.__name__} {value!r}")


@dataclass(frozen=True)
class SymbolRecord:
    # ── identity / required ──
    internal_symbol: str                      # EXCHANGE:TICKER
    provider_symbols: Dict[str, str]          # {"yfinance": "AAPL"}
    asset_class: AssetClass
    name: str
    exchange: str
    country: str
    region: str
    currency: str
    timezone: str
    trading_calendar: str
    universe_tags: List[str]
    active: bool
    scan_ready: bool
    source: str
    as_of_date: str
    first_seen_utc: str
    # ── optional / nullable (populated later, null in seed) ──
    session_open: Optional[str] = None
    session_close: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    data_quality_status: DataQualityStatus = DataQualityStatus.UNVERIFIED
    min_liquidity_tier: Optional[str] = None
    avg_volume_20d: Optional[float] = None
    avg_dollar_volume_20d: Optional[float] = None
    median_spread_bps: Optional[float] = None
    last_verified_utc: Optional[str] = None
    notes: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "asset_class",
                           _coerce_enum(self.asset_class, AssetClass,
                                        "asset_class"))
        object.__setattr__(self, "data_quality_status",
                           _coerce_enum(self.data_quality_status,
                                        DataQualityStatus,
                                        "data_quality_status"))
        # internal symbol + exchange consistency
        ex, _ticker = suffixes.split_internal_symbol(self.internal_symbol)
        if ex != self.exchange:
            raise ValueError(
                f"exchange {self.exchange!r} does not match internal_symbol "
                f"prefix {ex!r}")
        info = suffixes.exchange_info(self.exchange)  # raises on unknown
        # country / currency / timezone / calendar / region consistency
        if self.country != info.country:
            raise ValueError(f"country {self.country!r} != expected "
                             f"{info.country!r} for {self.exchange}")
        if self.currency != info.currency:
            raise ValueError(f"currency {self.currency!r} != expected "
                             f"{info.currency!r} for {self.exchange}")
        if self.trading_calendar != info.trading_calendar:
            raise ValueError(f"trading_calendar {self.trading_calendar!r} != "
                             f"expected {info.trading_calendar!r}")
        if self.region != info.region:
            raise ValueError(f"region {self.region!r} != expected "
                             f"{info.region!r} for {self.exchange}")
        if self.timezone != info.timezone:
            raise ValueError(f"timezone {self.timezone!r} != expected "
                             f"{info.timezone!r} for {self.exchange}")
        # timezone must be a real IANA zone
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"invalid IANA timezone: {self.timezone!r}")
        # provider symbols
        if not isinstance(self.provider_symbols, dict) or \
                not self.provider_symbols:
            raise ValueError("provider_symbols must be a non-empty dict")
        yf = self.provider_symbols.get("yfinance")
        if yf is not None and yf != suffixes.to_yfinance_symbol(
                self.internal_symbol):
            raise ValueError(
                f"yfinance provider symbol {yf!r} != expected "
                f"{suffixes.to_yfinance_symbol(self.internal_symbol)!r}")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("name must be a non-empty string")
        if not isinstance(self.universe_tags, list):
            raise ValueError("universe_tags must be a list")
        if not isinstance(self.active, bool) or \
                not isinstance(self.scan_ready, bool):
            raise ValueError("active and scan_ready must be booleans")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("source must be a non-empty string")
        _require_date(self.as_of_date, "as_of_date")
        _require_utc(self.first_seen_utc, "first_seen_utc")
        if self.last_verified_utc is not None:
            _require_utc(self.last_verified_utc, "last_verified_utc")
        # nullable numerics
        _opt_non_negative(self.avg_volume_20d, "avg_volume_20d")
        _opt_non_negative(self.avg_dollar_volume_20d, "avg_dollar_volume_20d")
        _opt_non_negative(self.median_spread_bps, "median_spread_bps")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["asset_class"] = self.asset_class.value
        d["data_quality_status"] = self.data_quality_status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SymbolRecord":
        allowed = set(cls.__dataclass_fields__)
        unknown = set(d) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in allowed})
