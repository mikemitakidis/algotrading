"""M21.1 A01 — conservative corporate-action / price-basis truth (pure).

The causal split detector is incomplete: it cannot see splits that a provider
has already adjusted away, splits below its jump threshold, or splits hidden
by missing bars. So NOT detecting a split never proves the data is safe.
Only a verified corporate-action record covering the decision's lookback
window can establish VERIFIED_CLEAN for raw prices.

States
  VERIFIED_CLEAN  raw prices + a verified record shows no corporate action in
                  the lookback window + the detector (if run) found nothing
  ADJUSTED        the provider retroactively adjusts prices (e.g. yfinance
                  ``auto_adjust=True``); under M19 policy adjusted prices are a
                  point-in-time risk unless explicitly allowed (never here)
  SUSPECTED       a split-like jump was detected, or a verified record shows a
                  corporate action inside the window
  UNVERIFIED      anything else (raw or unknown basis without a verified
                  record) — treated as UNKNOWN, never as safe
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Sequence, Tuple

from bot.scoring_truth.observations import parse_utc


class PriceBasis(str, enum.Enum):
    RAW = "raw"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN_ADJUSTED = "total_return_adjusted"
    UNKNOWN = "unknown"


ADJUSTED_BASES = (PriceBasis.SPLIT_ADJUSTED, PriceBasis.TOTAL_RETURN_ADJUSTED)

# Price basis per data provider, from the request code in bot/providers/.
# yfinance: auto_adjust=True (split + dividend adjusted, retroactively).
# alpaca:   the request sends no adjustment parameter; the effective basis is
#           whatever the API defaults to, which this code cannot verify
#           offline, so it stays UNKNOWN.
PROVIDER_PRICE_BASIS = {
    "yfinance": PriceBasis.TOTAL_RETURN_ADJUSTED,
    "alpaca": PriceBasis.UNKNOWN,
}


def provider_price_basis(provider: Optional[str]) -> PriceBasis:
    return PROVIDER_PRICE_BASIS.get(str(provider or "").strip().lower(),
                                    PriceBasis.UNKNOWN)


class CorporateActionState(str, enum.Enum):
    VERIFIED_CLEAN = "VERIFIED_CLEAN"
    ADJUSTED = "ADJUSTED"
    SUSPECTED = "SUSPECTED"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class CorporateActionRecord:
    """A corporate-action lookup for one symbol over a window. ``verified``
    must only be True for a record from a validated source."""

    symbol: str
    window_start_utc: Any
    window_end_utc: Any
    events_utc: Tuple[Any, ...] = ()   # effective times of price-scale actions
    source: str = ""
    verified: bool = False


@dataclass(frozen=True)
class CorporateActionTruth:
    state: CorporateActionState
    price_basis: PriceBasis
    reason: str


# Smallest consecutive close ratio treated as split-like (5:4 = 1.25).
SPLIT_JUMP_RATIO = 1.24


def suspect_split(closes: Sequence[Any], *,
                  min_ratio: float = SPLIT_JUMP_RATIO) -> Optional[bool]:
    """Causal, incomplete split heuristic over consecutive closes. Returns
    None when it cannot run (fewer than 2 points, or any non-finite /
    non-positive close), True on any jump of at least ``min_ratio`` in either
    direction, else False. False is NOT evidence of safety."""
    vals = []
    for c in closes:
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            return None
        if not math.isfinite(c) or c <= 0:
            return None
        vals.append(float(c))
    if len(vals) < 2:
        return None
    for a, b in zip(vals, vals[1:]):
        r = b / a
        if max(r, 1.0 / r) >= min_ratio:
            return True
    return False


def classify_corporate_action(symbol: str, basis: Any, *,
                              detector_suspected: Optional[bool],
                              record: Optional[CorporateActionRecord],
                              lookback_start: Any,
                              evaluated_at: datetime) -> CorporateActionTruth:
    try:
        basis = PriceBasis(basis) if basis is not None else PriceBasis.UNKNOWN
    except ValueError:
        basis = PriceBasis.UNKNOWN
    if detector_suspected is True:
        return CorporateActionTruth(CorporateActionState.SUSPECTED, basis,
                                    "split_like_jump_detected")
    if basis in ADJUSTED_BASES:
        return CorporateActionTruth(CorporateActionState.ADJUSTED, basis,
                                    "provider_adjusted_series_not_point_in_time")
    if basis != PriceBasis.RAW:
        return CorporateActionTruth(CorporateActionState.UNVERIFIED, basis,
                                    "price_basis_unknown")
    if record is None:
        return CorporateActionTruth(CorporateActionState.UNVERIFIED, basis,
                                    "no_corporate_action_record"
                                    + ("" if detector_suspected is False
                                       else "; detector_not_run"))
    if not isinstance(record, CorporateActionRecord) or record.verified is not True:
        return CorporateActionTruth(CorporateActionState.UNVERIFIED, basis,
                                    "corporate_action_record_not_verified")
    if str(record.symbol).strip().upper() != str(symbol).strip().upper():
        return CorporateActionTruth(CorporateActionState.UNVERIFIED, basis,
                                    "corporate_action_record_other_symbol")
    try:
        ws, we = parse_utc(record.window_start_utc), parse_utc(record.window_end_utc)
        lb, ev = parse_utc(lookback_start), parse_utc(evaluated_at)
        events = [parse_utc(e) for e in record.events_utc]
    except ValueError as exc:
        return CorporateActionTruth(CorporateActionState.UNVERIFIED, basis,
                                    "corporate_action_times_invalid: %s" % exc)
    if not (ws <= lb and we >= ev):
        return CorporateActionTruth(CorporateActionState.UNVERIFIED, basis,
                                    "corporate_action_record_does_not_cover_lookback")
    if any(lb <= e <= ev for e in events):
        return CorporateActionTruth(CorporateActionState.SUSPECTED, basis,
                                    "corporate_action_inside_lookback")
    return CorporateActionTruth(CorporateActionState.VERIFIED_CLEAN, basis,
                                "verified_record_no_action_in_lookback")
