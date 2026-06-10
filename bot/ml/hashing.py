"""bot.ml.hashing — canonical hashing + reproducibility metadata (M18.A.1).

RECONSTRUCTED_FROM_TRANSCRIPT_NOT_BYTE_IDENTICAL.

Recovered from `grep ^def bot/ml/hashing.py` output captured in
transcript #6: the file exports canonical_json, sha256_hex,
hash_canonical, lib_versions, git_head_sha, and repro_hash. Bodies
are best-effort reconstructions that satisfy the importer contract
(bot/ml/dataset/manifest.py imports canonical_json and sha256_hex).

The point of canonical_json is to produce a byte-deterministic
serialization of any JSON-like Python object so that two datasets
with identical (semantic) content produce identical SHA-256 hashes.
The point of repro_hash is to summarise the environment + git head
for the audit trail attached to a manifest or registry entry.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from typing import Any, Dict, Mapping


# ─── Canonical JSON ──────────────────────────────────────────────────

def _canonical_default(o: Any) -> Any:
    """JSON encoder default: handle types json doesn't know natively.

    - tuple, set, frozenset → sorted list
    - dataclass-like objects with .to_dict() → that dict
    - anything else: raise TypeError so we don't silently embed junk.
    """
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, tuple):
        return list(o)
    if hasattr(o, "to_dict") and callable(o.to_dict):
        return o.to_dict()
    raise TypeError(
        f"canonical_json cannot serialize {type(o).__name__}")


def canonical_json(obj: Any) -> bytes:
    """UTF-8 bytes of a canonical JSON encoding of obj.

    Canonical means:
      - sort_keys=True (dict key order is stable)
      - separators=(',', ':') (no whitespace)
      - ensure_ascii=True (\\uXXXX escape any non-ASCII)
      - tuples / sets normalized via _canonical_default
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_canonical_default,
    ).encode("utf-8")


def sha256_hex(payload: bytes) -> str:
    """SHA-256 hex digest of payload bytes."""
    return hashlib.sha256(payload).hexdigest()


def hash_canonical(obj: Any) -> str:
    """Convenience: sha256_hex(canonical_json(obj))."""
    return sha256_hex(canonical_json(obj))


# ─── Reproducibility metadata ────────────────────────────────────────

def lib_versions() -> Dict[str, str]:
    """Return versions of the libraries we want pinned in manifests
    and registry entries. Missing libraries are reported as 'absent'
    rather than raising, since several (lightgbm, etc.) are optional.
    """
    out: Dict[str, str] = {}
    out["python"] = sys.version.split()[0]
    out["platform"] = platform.platform()
    for mod in ("numpy", "pandas", "sklearn", "lightgbm",
                 "scipy", "pyarrow"):
        try:
            m = __import__(mod)
            out[mod] = getattr(m, "__version__", "unknown")
        except Exception:
            out[mod] = "absent"
    return out


def git_head_sha() -> str:
    """The current repo's HEAD commit SHA, or 'unknown' if git fails
    (e.g., running outside a git checkout)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def repro_hash(
    config: Mapping[str, Any],
    library_versions: Mapping[str, str],
    git_sha: str,
) -> str:
    """Compose a single hash that identifies the run's reproducibility
    surface: the config (DatasetConfig or TrainConfig dict), the library
    versions used, and the git HEAD SHA."""
    payload = {
        "config":            dict(config),
        "library_versions":  dict(library_versions),
        "git_head":          git_sha,
    }
    return hash_canonical(payload)


__all__ = [
    "canonical_json",
    "sha256_hex",
    "hash_canonical",
    "lib_versions",
    "git_head_sha",
    "repro_hash",
]
