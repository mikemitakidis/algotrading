"""M19.A provenance — deterministic, dependency-free hashing helpers.

All helpers are pure and deterministic:
  * stable key ordering (sorted keys),
  * fixed separators (no incidental whitespace),
  * stable float handling (canonical repr via round-trippable formatting),
  * NO wall-clock, NO random UUIDs, NO RNG anywhere.

These back the M19 provenance contract: same config -> same config_hash;
same input -> same input_digest; same candidate fields -> same candidate_id.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

# Bump only when the canonicalisation rules themselves change.
CANONICAL_FORMAT_VERSION = "m19_canon_v1"


def _canonicalize(obj: Any) -> Any:
    """Recursively normalise a value into a canonical, JSON-stable form.

    - dicts: keys coerced to str, recursively canonicalised, emitted sorted
      (json.dumps(sort_keys=True) handles ordering).
    - lists/tuples: canonicalised element-wise, order preserved (order is
      semantically meaningful for sequences).
    - floats: NaN/inf rejected (non-deterministic / not JSON-stable);
      finite floats normalised so that e.g. 1.0 and 1 do not collide-by-accident
      yet equal floats always render identically. We keep float type but route
      through repr() at dump time via a custom encoder.
    - other scalars (int, str, bool, None): returned as-is.
    """
    if isinstance(obj, dict):
        return {str(k): _canonicalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValueError("non-finite float is not canonicalisable")
        return obj
    return obj


class _CanonicalFloatEncoder(json.JSONEncoder):
    """Encoder that renders floats via repr() for stable round-trippable
    output (Python's repr gives the shortest string that round-trips)."""

    def default(self, o):  # pragma: no cover - only for unknown types
        if isinstance(o, (set, frozenset)):
            return sorted(o)
        raise TypeError(
            f"Object of type {type(o).__name__} is not canonicalisable")


def canonical_json(obj: Any) -> str:
    """Deterministic JSON string: sorted keys, compact separators, finite
    floats only. Identical inputs always produce identical strings."""
    canon = _canonicalize(obj)
    return json.dumps(
        canon,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        cls=_CanonicalFloatEncoder,
    )


def sha256_digest(obj: Any) -> str:
    """SHA-256 hex digest over the canonical JSON of `obj`."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def config_hash(config_dict: dict) -> str:
    """Deterministic hash of a config dict. Same config -> same hash."""
    return sha256_digest({"canon": CANONICAL_FORMAT_VERSION,
                          "config": config_dict})


def input_digest(input_dict: dict) -> str:
    """Deterministic digest of a SignalCandidateInput dict."""
    return sha256_digest({"canon": CANONICAL_FORMAT_VERSION,
                          "input": input_dict})


def candidate_id(
    *,
    schema_version: str,
    symbol: str,
    side: str,
    signal_timestamp_utc: str,
    input_digest_hex: str,
    config_hash_hex: str,
) -> str:
    """Deterministic candidate id from identity-relevant fields only.

    NO wall-clock, NO RNG. Same (schema_version, symbol, side, timestamp,
    input_digest, config_hash) -> same candidate_id."""
    return sha256_digest({
        "canon":                CANONICAL_FORMAT_VERSION,
        "schema_version":       schema_version,
        "symbol":               symbol,
        "side":                 side,
        "signal_timestamp_utc": signal_timestamp_utc,
        "input_digest":         input_digest_hex,
        "config_hash":          config_hash_hex,
    })


def candidate_digest(candidate_dict: dict) -> str:
    """Deterministic digest over a full scored-candidate dict (for audit /
    replay integrity). Distinct from candidate_id, which is identity-only."""
    return sha256_digest({"canon": CANONICAL_FORMAT_VERSION,
                          "candidate": candidate_dict})
