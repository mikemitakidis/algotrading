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
    versions used, and the git HEAD SHA.

    NOTE: this is the M18.A.1 v1 hash, kept for backward compatibility.
    The full SR-8 reproducibility surface is `repro_hash_v2` below.
    """
    payload = {
        "config":            dict(config),
        "library_versions":  dict(library_versions),
        "git_head":          git_sha,
    }
    return hash_canonical(payload)


# ─── repro_hash_v2 — full SR-8 reproducibility composition (M18.B.2) ──
#
# v1 (above) hashed only {config, library_versions, git_head}. SR-8
# requires the COMPLETE reproducibility surface, with each component
# independently hashed so an audit can see exactly what changed:
#
#   feature_schema_hash   label_schema_hash   train_config_hash
#   dataset_manifest_hash m16_bars_hash       git_head
#   library_versions (+ its own hash)
#
# repro_hash_v2_payload() assembles the canonical payload (no raw
# OHLCV — only the bars DIGEST). repro_hash_v2() returns the single
# top-level hash. repro_hash_v2_component_hashes() returns the
# per-component map for audit/diffing.

REPRO_HASH_V1_VERSION = 1
REPRO_HASH_V2_VERSION = 2
REPRO_HASH_V2_ALGORITHM = "sha256:c14n-json:m18.repro.v2"

# train_config keys SR-8 requires to be present.
_REPRO_V2_REQUIRED_TRAIN_CONFIG_KEYS = (
    "model_type", "train_mode", "target_label_id",
    "hyperparameters", "seed", "fixture_mode", "dataset_id",
)
# dataset_manifest keys SR-8 requires to be present.
_REPRO_V2_REQUIRED_MANIFEST_KEYS = (
    "dataset_id", "dataset_hash_sha256", "feature_specs_hash",
    "label_specs_hash", "anchor_set", "anchor_count_train",
    "anchor_count_val", "anchor_count_test", "coverage_degraded",
    "fixture_only", "promotion_eligible", "promotion_blocked_reasons",
)


def _resolve_schema_hash(
    raw_schema: Any,
    precomputed_hash: Any,
    name: str,
) -> "tuple":
    """Return (hash, source) for a feature/label schema component.

    - raw schema supplied  → hash_canonical(raw), source='raw_schema'
    - only precomputed hash → that hash,         source='precomputed_hash'
    - both, and they conflict → ValueError
    - neither → ValueError
    """
    if raw_schema is not None and precomputed_hash is not None:
        derived = hash_canonical(raw_schema)
        if derived != precomputed_hash:
            raise ValueError(
                f"repro_hash_v2: {name} raw schema hashes to "
                f"{derived!r} but a conflicting {name}_hash "
                f"{precomputed_hash!r} was supplied")
        return derived, "raw_schema"
    if raw_schema is not None:
        return hash_canonical(raw_schema), "raw_schema"
    if precomputed_hash is not None:
        return str(precomputed_hash), "precomputed_hash"
    raise ValueError(
        f"repro_hash_v2: must supply either {name} or {name}_hash")


def repro_hash_v2_payload(
    *,
    train_config: Mapping[str, Any],
    dataset_manifest: Mapping[str, Any],
    feature_schema: Any = None,
    feature_schema_hash: Any = None,
    label_schema: Any = None,
    label_schema_hash: Any = None,
    m16_bars_digest: Any = None,
    m16_bars_sha: Any = None,
    library_versions: Any = None,
    git_sha: Any = None,
) -> Dict[str, Any]:
    """Assemble the canonical v2 reproducibility payload.

    Does NOT mutate any input. Raises ValueError on missing required
    train_config / manifest keys or conflicting schema hash inputs.
    Never embeds raw OHLCV — only the bars digest/hash.
    """
    # ── feature / label schema ──────────────────────────────────
    feature_schema_hash_v, feature_schema_source = _resolve_schema_hash(
        feature_schema, feature_schema_hash, "feature_schema")
    label_schema_hash_v, label_schema_source = _resolve_schema_hash(
        label_schema, label_schema_hash, "label_schema")

    # ── train config (validate required keys) ───────────────────
    missing_tc = [k for k in _REPRO_V2_REQUIRED_TRAIN_CONFIG_KEYS
                  if k not in train_config]
    if missing_tc:
        raise ValueError(
            f"repro_hash_v2: train_config missing required keys "
            f"{missing_tc}")
    train_config_canon = {k: train_config[k] for k in
                           sorted(train_config.keys())}
    train_config_hash = hash_canonical(train_config_canon)

    # ── dataset manifest (validate required keys) ───────────────
    missing_mf = [k for k in _REPRO_V2_REQUIRED_MANIFEST_KEYS
                  if k not in dataset_manifest]
    if missing_mf:
        raise ValueError(
            f"repro_hash_v2: dataset_manifest missing required keys "
            f"{missing_mf}")
    manifest_canon = {k: dataset_manifest[k] for k in
                       sorted(dataset_manifest.keys())}
    dataset_manifest_hash = hash_canonical(manifest_canon)

    # ── M16 bars digest/sha (data fingerprint, NEVER raw OHLCV) ──
    if m16_bars_digest is not None and m16_bars_sha is not None:
        derived_bars = hash_canonical(m16_bars_digest)
        if derived_bars != m16_bars_sha:
            raise ValueError(
                f"repro_hash_v2: m16_bars_digest hashes to "
                f"{derived_bars!r} but conflicting m16_bars_sha "
                f"{m16_bars_sha!r} was supplied")
        m16_bars_hash = derived_bars
        m16_bars_source = "digest"
    elif m16_bars_digest is not None:
        m16_bars_hash = hash_canonical(m16_bars_digest)
        m16_bars_source = "digest"
    elif m16_bars_sha is not None:
        m16_bars_hash = str(m16_bars_sha)
        m16_bars_source = "precomputed_sha"
    else:
        raise ValueError(
            "repro_hash_v2: must supply m16_bars_digest or "
            "m16_bars_sha (the data fingerprint is mandatory under "
            "SR-8)")

    # ── library versions + git ──────────────────────────────────
    libs = dict(library_versions) if library_versions is not None \
        else lib_versions()
    for required in ("python", "numpy", "pandas", "sklearn", "lightgbm"):
        libs.setdefault(required, "absent")
    library_versions_hash = hash_canonical(libs)
    git_head = git_sha if git_sha is not None else git_head_sha()

    component_hashes = {
        "feature_schema_hash":   feature_schema_hash_v,
        "label_schema_hash":     label_schema_hash_v,
        "train_config_hash":     train_config_hash,
        "dataset_manifest_hash": dataset_manifest_hash,
        "m16_bars_hash":         m16_bars_hash,
        "library_versions_hash": library_versions_hash,
        "git_head":              git_head,
    }

    payload = {
        "schema_version":        REPRO_HASH_V2_VERSION,
        "algorithm":             REPRO_HASH_V2_ALGORITHM,
        "feature_schema_hash":   feature_schema_hash_v,
        "feature_schema_source": feature_schema_source,
        "label_schema_hash":     label_schema_hash_v,
        "label_schema_source":   label_schema_source,
        "train_config_hash":     train_config_hash,
        "dataset_manifest_hash": dataset_manifest_hash,
        "m16_bars_hash":         m16_bars_hash,
        "m16_bars_source":       m16_bars_source,
        "git_head":              git_head,
        "library_versions":      libs,
        "library_versions_hash": library_versions_hash,
        "component_hashes":      component_hashes,
    }
    return payload


def repro_hash_v2(
    *,
    train_config: Mapping[str, Any],
    dataset_manifest: Mapping[str, Any],
    feature_schema: Any = None,
    feature_schema_hash: Any = None,
    label_schema: Any = None,
    label_schema_hash: Any = None,
    m16_bars_digest: Any = None,
    m16_bars_sha: Any = None,
    library_versions: Any = None,
    git_sha: Any = None,
) -> str:
    """Top-level SR-8 reproducibility hash. Deterministic over the
    full canonical v2 payload (see repro_hash_v2_payload)."""
    payload = repro_hash_v2_payload(
        train_config=train_config,
        dataset_manifest=dataset_manifest,
        feature_schema=feature_schema,
        feature_schema_hash=feature_schema_hash,
        label_schema=label_schema,
        label_schema_hash=label_schema_hash,
        m16_bars_digest=m16_bars_digest,
        m16_bars_sha=m16_bars_sha,
        library_versions=library_versions,
        git_sha=git_sha,
    )
    return hash_canonical(payload)


def repro_hash_v2_component_hashes(
    payload: Mapping[str, Any],
) -> Dict[str, str]:
    """Return the per-component hash map from a v2 payload (the same
    dict stored under 'component_hashes')."""
    ch = payload.get("component_hashes")
    if not isinstance(ch, Mapping):
        raise ValueError(
            "repro_hash_v2_component_hashes: payload has no "
            "'component_hashes' mapping")
    return dict(ch)


__all__ = [
    "canonical_json",
    "sha256_hex",
    "hash_canonical",
    "lib_versions",
    "git_head_sha",
    "repro_hash",
    "REPRO_HASH_V1_VERSION",
    "REPRO_HASH_V2_VERSION",
    "REPRO_HASH_V2_ALGORITHM",
    "repro_hash_v2",
    "repro_hash_v2_payload",
    "repro_hash_v2_component_hashes",
]
