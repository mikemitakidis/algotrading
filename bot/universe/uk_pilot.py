"""M21.UR — UK-only pilot accessor (explicit opt-in, NOT runtime-wired).

This module provides an EXPLICIT, opt-in way to load the UK pilot scan-ready set
from `configs/universe/uk_pilot.json`. It is deliberately NOT imported by any
runtime entrypoint (scanner / main / dashboard / broker). Nothing happens unless
a caller explicitly invokes get_uk_pilot_symbols().

Isolation guarantees:
  - The default runtime path (bot.universe.active_selection.get_scan_ready_symbols
    with no args) is UNCHANGED and still returns the US set (536). This module
    does not touch _DEFAULT_PATHS and does not edit active_selection.py.
  - scan_ready=true for the pilot symbols exists ONLY inside uk_pilot.json; the
    global universe (global_expanded.json) is untouched and all its records
    remain inactive / not scan_ready.
  - Loading is done through the SAME registry machinery via the public
    get_scan_ready_symbols(paths=[...]) argument, so the pilot uses the exact
    same validation/format as the US set.

Rollback: delete configs/universe/uk_pilot.json and this module; the default
path never referenced them, so get_scan_ready_symbols() stays 536 throughout.
"""
from pathlib import Path
from typing import List

from bot.universe.active_selection import get_scan_ready_symbols

_REPO = Path(__file__).resolve().parents[2]
UK_PILOT_PATH = _REPO / "configs" / "universe" / "uk_pilot.json"


def get_uk_pilot_symbols() -> List[str]:
    """Return the UK pilot scan-ready bare tickers (suffixed, e.g. 'AAF.L').

    Loads ONLY the UK pilot file, explicitly. Does not read the US default
    paths and does not alter them. Deterministic, sorted, de-duplicated.

    Note: the returned tickers are LSE-suffixed ('.L'), unlike the suffix-free
    US scan-ready set. Any future runtime/broker consumer must handle the
    suffix; this accessor only exposes the set, it does not wire it anywhere.
    """
    return get_scan_ready_symbols(paths=[UK_PILOT_PATH])
