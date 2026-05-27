"""
M13.3 — eToro symbol → instrumentId resolver.

In-memory cache. Pluggable: tests can preload it; production callers
can attach an EtoroReadAdapter (which uses GET-only). NEVER persists to
disk in M13.3 (per M13.1 design decision: bootstrap-from-scratch on
restart is conservative).
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Mapping, Optional

log = logging.getLogger(__name__)


class InstrumentCache:
    """Symbol → instrumentId lookup with optional adapter fallback.

    All operations are thread-safe (cheap RLock; the cache is small).
    """

    def __init__(self, read_adapter=None):
        self._read_adapter = read_adapter
        self._cache: Dict[str, int] = {}
        self._lock = threading.RLock()

    def preload(self, mapping: Mapping[str, int]) -> None:
        """Bulk-load entries. Used by tests to avoid network. Production
        callers may also preload from any external source."""
        with self._lock:
            for sym, iid in mapping.items():
                if iid is None:
                    continue
                self._cache[sym.upper()] = int(iid)

    def has(self, symbol: str) -> bool:
        with self._lock:
            return symbol.upper() in self._cache

    def resolve(self, symbol: str) -> Optional[int]:
        """Returns the instrumentId for `symbol`, or None if unresolvable.

        Resolution order:
          1. cache hit -> return
          2. cache miss + read_adapter present -> ONE search call,
             cache the result, return
          3. cache miss + no adapter -> None (never raises)

        The read_adapter call uses EtoroReadAdapter.search_instrument
        which goes through EtoroClient.get (GET-only).
        """
        key = symbol.upper()
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        if self._read_adapter is None:
            return None
        try:
            matches = self._read_adapter.search_instrument(symbol)
        except Exception as e:
            log.warning('[INSTR_CACHE] search failed for %s: %s', symbol, e)
            return None
        # Pick the first match whose ticker text looks like the symbol;
        # fall back to first match if no obvious hit.
        chosen = None
        for m in matches or []:
            raw = getattr(m, 'raw', {}) or {}
            blob = ' '.join(str(v) for v in raw.values()).upper()
            if key in blob:
                chosen = m
                break
        if chosen is None and matches:
            chosen = matches[0]
        if chosen is None or chosen.instrument_id is None:
            return None
        iid = int(chosen.instrument_id)
        with self._lock:
            self._cache[key] = iid
        return iid

    def snapshot(self) -> Dict[str, int]:
        """Return a copy of current cache contents (for tests / inspection)."""
        with self._lock:
            return dict(self._cache)
