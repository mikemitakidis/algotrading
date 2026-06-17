"""M20.A paper provenance — self-contained canonical JSON + deterministic
paper-ID builders.

Isolation rule (M20 contract): this module does NOT import M19 provenance
internals. M19 is an input dependency only at the public schema/enum level.

Determinism rules:
  * canonical JSON: sorted keys, compact separators, finite floats only.
  * paper IDs are sha256 over a canonical identity payload, prefixed:
        PPR-  paper order
        PFL-  paper fill
        PPS-  paper position
        PEV-  paper event
  * NO wall-clock, NO RNG, NO network. Identical identity -> identical id.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

PPR_PREFIX = "PPR-"
PFL_PREFIX = "PFL-"
PPS_PREFIX = "PPS-"
PEV_PREFIX = "PEV-"

# how many hex chars of the digest to keep after the prefix
_ID_HEX_LEN = 32


def _reject_nonfinite(obj: Any) -> Any:
    """Recursively reject NaN/inf so serialization is deterministic and safe."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"non-finite float not allowed: {obj!r}")
        return obj
    if isinstance(obj, dict):
        return {k: _reject_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_reject_nonfinite(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """Deterministic JSON string: sorted keys, compact separators, finite
    floats only. Identical inputs always produce identical strings."""
    safe = _reject_nonfinite(obj)
    return json.dumps(
        safe,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def sha256_hex(obj: Any) -> str:
    """SHA-256 hex digest over the canonical JSON of `obj`."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _make_id(prefix: str, identity: Any) -> str:
    return prefix + sha256_hex(identity)[:_ID_HEX_LEN]


def paper_order_id(identity: Any) -> str:
    """Deterministic PPR- id from a canonical order-identity payload."""
    return _make_id(PPR_PREFIX, identity)


def paper_fill_id(identity: Any) -> str:
    """Deterministic PFL- id from a canonical fill-identity payload."""
    return _make_id(PFL_PREFIX, identity)


def paper_position_id(identity: Any) -> str:
    """Deterministic PPS- id from a canonical position-identity payload."""
    return _make_id(PPS_PREFIX, identity)


def paper_event_id(identity: Any) -> str:
    """Deterministic PEV- id from a canonical event-identity payload."""
    return _make_id(PEV_PREFIX, identity)
