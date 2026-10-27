"""research.common.hashing — canonical digests for research artifacts.

Reuses the M19 canonical JSON encoder (sorted keys, compact separators,
finite floats only) so research records hash exactly like scoring records.
Adds helpers for numpy scalars/arrays, dataclasses and pandas frames.
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
from typing import Any

import numpy as np
import pandas as pd

from bot.signal_scoring.provenance import canonical_json as _m19_canonical_json

HASH_FORMAT = "m21r-sha256-v1"


def to_plain(obj: Any) -> Any:
    """Recursively convert numpy / pandas / dataclass values to JSON-plain types.

    Non-finite floats become the strings "nan", "inf", "-inf" so that records
    containing them still hash deterministically (the M19 encoder rejects
    non-finite floats by design).
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(to_plain(v) for v in obj)
    if isinstance(obj, np.ndarray):
        return [to_plain(v) for v in obj.tolist()]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        if math.isnan(f):
            return "nan"
        if math.isinf(f):
            return "inf" if f > 0 else "-inf"
        return f
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    if hasattr(obj, "isoformat") and callable(obj.isoformat):
        return obj.isoformat()
    return obj


def canonical_json(obj: Any) -> str:
    """Deterministic JSON text for `obj` (after `to_plain`)."""
    return _m19_canonical_json(to_plain(obj))


def digest(obj: Any) -> str:
    """SHA-256 hex digest of the canonical JSON of `obj`."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def short(d: str, n: int = 12) -> str:
    return d[:n]


def frame_digest(df: pd.DataFrame) -> str:
    """Content digest of a DataFrame: column names, dtypes and cell values.

    Independent of the index labels, sensitive to row order, so callers should
    sort first when order is not meaningful.
    """
    h = hashlib.sha256()
    h.update(canonical_json({"columns": [str(c) for c in df.columns],
                             "dtypes": [str(t) for t in df.dtypes],
                             "rows": int(len(df))}).encode("utf-8"))
    if len(df):
        h.update(pd.util.hash_pandas_object(df, index=False).to_numpy().tobytes())
    return h.hexdigest()


def array_digest(a: np.ndarray) -> str:
    a = np.ascontiguousarray(a)
    h = hashlib.sha256()
    h.update(("%s|%s|" % (a.dtype.str, a.shape)).encode("ascii"))
    h.update(a.tobytes())
    return h.hexdigest()


__all__ = ["HASH_FORMAT", "to_plain", "canonical_json", "digest", "short",
           "frame_digest", "array_digest"]
