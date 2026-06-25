"""M20.UE — active symbol selection from the universe registry.

Pure, offline helper that returns the runtime symbol list from the registry's
`scan_ready=true` records, in the SAME bare-ticker format the runtime currently
expects from bot.focus.FOCUS_SYMBOLS (e.g. "AAPL", "SPY").

This module does NOT change runtime behaviour by itself. It is consumed by
main.py behind a config flag (use_registry_universe, default false). When the
flag is off, the runtime keeps using FOCUS_SYMBOLS exactly as before.

No network, no broker, no live, no paper. Reads only the committed universe
JSON files via UniverseRegistry.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Union

from bot.universe.registry import UniverseRegistry

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_PATHS = [
    _REPO / "configs" / "universe" / "us_seed.json",
    _REPO / "configs" / "universe" / "us_expanded.json",
]


def _bare_ticker(record) -> Optional[str]:
    """Return the runtime-compatible bare ticker for a SymbolRecord.

    Prefer provider_symbols['yfinance'] (the runtime data provider). Fall back
    to the segment after ':' in internal_symbol only if no yfinance provider
    symbol is present. Returns None if nothing usable.
    """
    psyms = getattr(record, "provider_symbols", {}) or {}
    yf = psyms.get("yfinance")
    if yf:
        return yf
    internal = getattr(record, "internal_symbol", "") or ""
    if ":" in internal:
        return internal.split(":", 1)[1] or None
    return internal or None


def get_scan_ready_symbols(
    paths: Optional[Sequence[Union[str, Path]]] = None,
) -> List[str]:
    """Return bare tickers for all `scan_ready=true` records in the registry.

    Deterministic: sorted, de-duplicated, runtime-compatible bare-ticker format.
    Pure/offline — no network, no broker. Raises only on unreadable/invalid
    universe files (same failure mode as UniverseRegistry.load).
    """
    use = list(paths) if paths is not None else list(_DEFAULT_PATHS)
    reg = UniverseRegistry.load(use)
    out: List[str] = []
    seen = set()
    for rec in reg.scan_ready_symbols():
        t = _bare_ticker(rec)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return sorted(out)
