"""bot/risk_authority/authority.py — M14.E authority ladder.

Five levels in strict total order, lowest = safest:

    OFF < SIGNAL_ONLY < PAPER_ONLY < ONE_SHOT_MANUAL < AUTO_ALLOWED

Rules (M14.A §5 / governor invariants — non-negotiable):
  * Downgrades happen automatically on breach (monotone safety move).
  * Upgrades MUST require an explicit human action: any audit row with
    `source='manual_reset'` is the only legitimate upgrade carrier.
  * Property-tested in test_m14_e_engine.py: across 1000 random input
    sequences, no autonomous (source='auto') transition produces an
    upgrade.
"""
from __future__ import annotations

from enum import IntEnum


class Authority(IntEnum):
    """Authority ladder. IntEnum to make comparisons (`<`, `>=`) cheap
    and to enforce strict total order at the type level."""
    OFF              = 0
    SIGNAL_ONLY      = 1
    PAPER_ONLY       = 2
    ONE_SHOT_MANUAL  = 3
    AUTO_ALLOWED     = 4

    @classmethod
    def from_string(cls, name: str) -> "Authority":
        try:
            return cls[name]
        except KeyError:
            raise ValueError(
                f"unknown authority level {name!r}; "
                f"allowed={[a.name for a in cls]}"
            )

    def as_label(self) -> str:
        return self.name


# Required minimum authority for each requested action. Used by gate #9.
REQUIRED_AUTHORITY = {
    "query_authority":  Authority.OFF,           # always allowed to ask
    "trade_close":      Authority.PAPER_ONLY,    # closing existing exposure is safer than opening
    "trade_open":       Authority.ONE_SHOT_MANUAL,
}


def is_monotone_safe(before: Authority, after: Authority,
                     source: str) -> bool:
    """True iff the transition from `before` to `after` honours the
    downgrade-only invariant under the given audit `source`.

    * Equal levels are always allowed (no-op transitions).
    * Downgrades (`after < before`) are always allowed.
    * Upgrades (`after > before`) are only allowed when
      `source='manual_reset'` — an explicit human action.

    Returns False if the transition is an autonomous upgrade.
    """
    if not isinstance(before, Authority) or not isinstance(after, Authority):
        raise TypeError("before/after must be Authority instances")
    if after <= before:
        return True
    return source == "manual_reset"


__all__ = ["Authority", "REQUIRED_AUTHORITY", "is_monotone_safe"]
