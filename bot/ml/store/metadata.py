"""bot.ml.store.metadata — content-addressed store identity (M18.B.7).

A StoreKey captures everything that determines whether a cached
feature/label artifact is still valid. If ANY identity input changes
(feature schema, label schema, M16 bars digest, missingness policy,
or the symbol/timeframe/anchor/config that shape the dataset), the
content hash changes and the old artifact is no longer addressed —
i.e. safe invalidation is automatic because identity is the address.

This module is pure metadata + hashing. No I/O, no pandas.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List

from bot.ml.hashing import canonical_json, sha256_hex


STORE_SCHEMA_VERSION = 1

# Bumped if the store layout / hashing scheme itself changes (separate
# from the content it addresses). Folded into the hash so a scheme
# change invalidates every prior artifact.
STORE_HASH_SCHEME = "sha256:c14n-json:m18.store.v1"


@dataclass
class StoreKey:
    """The full identity of a feature/label artifact.

    kind                 "feature" | "label"
    symbol               e.g. "AAPL"
    anchor_tf            anchor timeframe, e.g. "15m"
    anchor_set           anchor-set name
    timeframes           sorted list of timeframes used
    feature_specs_hash   compute_feature_specs_hash(...)
    label_specs_hash     compute_label_specs_hash(...)
    m16_bars_digest      _bars_digest(...) dict
    missingness_policy_hash  missingness_policy_hash()
    extra                any additional config that shapes the artifact
                         (embargo, fractions, fixture flag, ...)
    """
    kind:               str
    symbol:             str
    anchor_tf:          str
    anchor_set:         str
    timeframes:         List[str]
    feature_specs_hash: str
    label_specs_hash:   str
    m16_bars_digest:    Dict[str, Any]
    missingness_policy_hash: str = ""
    extra:              Dict[str, Any] = field(default_factory=dict)

    def canonical_object(self) -> Dict[str, Any]:
        """Deterministic, sorted, JSON-safe identity object."""
        return {
            "store_schema_version": STORE_SCHEMA_VERSION,
            "store_hash_scheme":    STORE_HASH_SCHEME,
            "kind":                 self.kind,
            "symbol":               self.symbol,
            "anchor_tf":            self.anchor_tf,
            "anchor_set":           self.anchor_set,
            "timeframes":           sorted(self.timeframes),
            "feature_specs_hash":   self.feature_specs_hash,
            "label_specs_hash":     self.label_specs_hash,
            "m16_bars_digest":      self.m16_bars_digest,
            "missingness_policy_hash": self.missingness_policy_hash,
            "extra":                dict(sorted(self.extra.items())),
        }

    def content_hash(self) -> str:
        """SHA-256 of the canonical identity. The content address."""
        return sha256_hex(canonical_json(self.canonical_object()))

    def partition_path(self) -> str:
        """Human-readable, collision-free relative path:
        <kind>/<symbol>/<anchor_tf>/<anchor_set>/<hash>. The hash makes
        it unique; the prefix makes the store browsable / partitioned
        by symbol+timeframe."""
        h = self.content_hash()
        # sanitize path components (no separators/whitespace)
        def _safe(s: str) -> str:
            return "".join(c if (c.isalnum() or c in "._-") else "_"
                           for c in str(s))
        return (f"{_safe(self.kind)}/{_safe(self.symbol)}/"
                f"{_safe(self.anchor_tf)}/{_safe(self.anchor_set)}/{h}")


@dataclass
class StoreMetadata:
    """JSON-safe sidecar persisted next to each cached artifact. Used to
    detect corruption / identity mismatch on read (fail-closed)."""
    store_schema_version: int
    kind:                 str
    content_hash:         str
    key_canonical:        Dict[str, Any]
    artifact_filename:    str
    n_rows:               int
    n_columns:            int
    columns:              List[str]
    created_note:         str = "m18.store.v1"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StoreMetadata":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
