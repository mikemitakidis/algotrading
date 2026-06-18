"""M20.UA universe registry — pure load / filter / query API.

Loads SymbolRecords from one or more JSON files (caller supplies the path; no
default path is baked in). Read-only: no network, no file writes, no module-
level path. Rejects duplicate internal_symbol and duplicate provider symbol.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from bot.universe.schema import SymbolRecord


class UniverseRegistry:
    def __init__(self, records: Sequence[SymbolRecord]):
        by_internal: Dict[str, SymbolRecord] = {}
        seen_provider: Dict[str, str] = {}  # "provider:symbol" -> internal
        for rec in records:
            if rec.internal_symbol in by_internal:
                raise ValueError(
                    f"duplicate internal_symbol: {rec.internal_symbol}")
            by_internal[rec.internal_symbol] = rec
            for provider, psym in rec.provider_symbols.items():
                key = f"{provider}:{psym}"
                if key in seen_provider:
                    raise ValueError(
                        f"duplicate provider symbol {key} "
                        f"({seen_provider[key]} and {rec.internal_symbol})")
                seen_provider[key] = rec.internal_symbol
        # preserve insertion order for determinism
        self._records: List[SymbolRecord] = list(records)
        self._by_internal = by_internal

    # ── loading ──
    @classmethod
    def load(cls, paths: Union[str, Path, Sequence[Union[str, Path]]]
             ) -> "UniverseRegistry":
        if isinstance(paths, (str, Path)):
            paths = [paths]
        records: List[SymbolRecord] = []
        for p in paths:
            with open(p, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            symbols = payload["symbols"] if isinstance(payload, dict) \
                and "symbols" in payload else payload
            for entry in symbols:
                records.append(SymbolRecord.from_dict(entry))
        return cls(records)

    # ── queries ──
    def all_symbols(self) -> List[SymbolRecord]:
        return list(self._records)

    def get(self, internal_symbol: str) -> Optional[SymbolRecord]:
        return self._by_internal.get(internal_symbol)

    def active_symbols(self) -> List[SymbolRecord]:
        return [r for r in self._records if r.active]

    def scan_ready_symbols(self) -> List[SymbolRecord]:
        return [r for r in self._records if r.scan_ready]

    def symbols_by_tag(self, tag: str) -> List[SymbolRecord]:
        return [r for r in self._records if tag in r.universe_tags]

    def provider_symbol(self, internal_symbol: str,
                        provider: str = "yfinance") -> Optional[str]:
        rec = self._by_internal.get(internal_symbol)
        if rec is None:
            raise KeyError(f"unknown internal_symbol: {internal_symbol}")
        return rec.provider_symbols.get(provider)

    def __len__(self) -> int:
        return len(self._records)
