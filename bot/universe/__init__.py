"""bot.universe — M20.U symbol registry (M20.UA infrastructure + 89-symbol seed).

Isolated, read-only universe registry. Consumes nothing from bot.paper, brokers,
live, risk, main, or dashboard, and performs no network access. Additive only:
the runtime still uses bot.focus.FOCUS_SYMBOLS until a later migration milestone.
"""
from __future__ import annotations

M20U_PHASE = "M20.UA"

from bot.universe.schema import (  # noqa: F401,E402
    SCHEMA_VERSION,
    SymbolRecord,
    AssetClass,
    DataQualityStatus,
)
from bot.universe import suffixes  # noqa: F401,E402
from bot.universe.registry import UniverseRegistry  # noqa: F401,E402

__all__ = [
    "M20U_PHASE",
    "SCHEMA_VERSION",
    "SymbolRecord",
    "AssetClass",
    "DataQualityStatus",
    "UniverseRegistry",
    "suffixes",
]
