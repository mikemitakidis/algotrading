"""bot.ml.store.feature_store — content-addressed feature cache (M18.B.7).

Caches computed feature DataFrames keyed by StoreKey.content_hash().
Same identity -> same path -> cache hit (no recompute). Any identity
input change -> different hash -> cache miss -> recompute + write.

Storage layout under `root`:
    <kind>/<symbol>/<anchor_tf>/<anchor_set>/<hash>.parquet   (artifact)
    <kind>/<symbol>/<anchor_tf>/<anchor_set>/<hash>.json      (metadata)

Fail-closed reads: if the metadata is missing/corrupt or its
content_hash does not match the key, the read is treated as a MISS
(safe recompute) rather than returning stale/wrong data.

This is storage infrastructure only — never touches signals.db, brokers,
dashboards, or live trading. Artifacts are written under a caller-
supplied root (e.g. a tmp dir in tests, data/ml/store in prod) and are
NEVER committed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from bot.ml.store.metadata import StoreKey, StoreMetadata


@dataclass
class CacheResult:
    """Outcome of a store lookup. JSON-safe via to_dict()."""
    hit:           bool
    kind:          str
    content_hash:  str
    reason:        str         # "hit" | "miss_no_artifact" |
                               # "miss_no_metadata" | "miss_corrupt_metadata" |
                               # "miss_hash_mismatch"
    artifact_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hit":           bool(self.hit),
            "kind":          self.kind,
            "content_hash":  self.content_hash,
            "reason":        self.reason,
            "artifact_path": self.artifact_path,
        }


class _ContentAddressedStore:
    """Shared read/write logic for feature and label stores."""

    KIND = "artifact"

    def __init__(self, root: str):
        self.root = Path(root)

    # ── paths ────────────────────────────────────────────────────────
    def _base(self, key: StoreKey) -> Path:
        return self.root / key.partition_path()

    def artifact_path(self, key: StoreKey) -> Path:
        return self._base(key).with_suffix(".parquet")

    def metadata_path(self, key: StoreKey) -> Path:
        return self._base(key).with_suffix(".json")

    # ── lookup ─────────────────────────────────────────────────────────
    def lookup(self, key: StoreKey) -> CacheResult:
        """Check the cache WITHOUT loading the dataframe. Fail-closed:
        any inconsistency is reported as a miss (safe recompute)."""
        ch = key.content_hash()
        art = self.artifact_path(key)
        meta = self.metadata_path(key)
        if not art.exists():
            return CacheResult(False, self.KIND, ch, "miss_no_artifact")
        if not meta.exists():
            return CacheResult(False, self.KIND, ch, "miss_no_metadata")
        try:
            md = StoreMetadata.from_dict(
                json.loads(meta.read_text()))
        except Exception:
            return CacheResult(False, self.KIND, ch,
                               "miss_corrupt_metadata")
        if md.content_hash != ch or md.kind != self.KIND:
            return CacheResult(False, self.KIND, ch,
                               "miss_hash_mismatch")
        return CacheResult(True, self.KIND, ch, "hit", str(art))

    # ── write ──────────────────────────────────────────────────────────
    def write(self, key: StoreKey, df: pd.DataFrame) -> CacheResult:
        """Write the artifact + JSON-safe metadata. Returns a hit
        CacheResult for the freshly-written entry."""
        ch = key.content_hash()
        art = self.artifact_path(key)
        meta = self.metadata_path(key)
        art.parent.mkdir(parents=True, exist_ok=True)
        # reset_index so positional round-trip is exact
        df_out = df.reset_index(drop=True)
        df_out.to_parquet(art, index=False)
        md = StoreMetadata(
            store_schema_version=1,
            kind=self.KIND,
            content_hash=ch,
            key_canonical=key.canonical_object(),
            artifact_filename=art.name,
            n_rows=int(df_out.shape[0]),
            n_columns=int(df_out.shape[1]),
            columns=[str(c) for c in df_out.columns],
        )
        # allow_nan=False would reject NaN; metadata has no floats that
        # can be NaN, but be explicit about JSON-safety.
        meta.write_text(json.dumps(md.to_dict(), allow_nan=False,
                                   sort_keys=True))
        return CacheResult(True, self.KIND, ch, "hit", str(art))

    # ── read ───────────────────────────────────────────────────────────
    def read(self, key: StoreKey) -> Optional[pd.DataFrame]:
        """Load the cached dataframe iff lookup() is a hit, else None."""
        res = self.lookup(key)
        if not res.hit:
            return None
        return pd.read_parquet(self.artifact_path(key))

    def get_or_compute(
        self, key: StoreKey, compute_fn
    ) -> Tuple[pd.DataFrame, CacheResult]:
        """Return (df, cache_result). On hit, loads from cache (no
        recompute). On miss, calls compute_fn() -> DataFrame, writes it,
        and returns it. compute_fn is only called on a miss."""
        res = self.lookup(key)
        if res.hit:
            return pd.read_parquet(self.artifact_path(key)), res
        df = compute_fn()
        self.write(key, df)
        # report the original miss reason (so callers can see why it
        # recomputed) but flag that it is now cached.
        return df, CacheResult(False, self.KIND, res.content_hash,
                               res.reason, str(self.artifact_path(key)))


class FeatureStore(_ContentAddressedStore):
    KIND = "feature"


def make_feature_key(
    *, symbol: str, anchor_tf: str, anchor_set: str, timeframes,
    feature_specs_hash: str, m16_bars_digest: Dict[str, Any],
    missingness_policy_hash: str = "", extra: Optional[Dict] = None,
) -> StoreKey:
    """Build a StoreKey for a FEATURE artifact. label_specs_hash is not
    part of feature identity (features don't depend on labels)."""
    return StoreKey(
        kind="feature", symbol=symbol, anchor_tf=anchor_tf,
        anchor_set=anchor_set, timeframes=list(timeframes),
        feature_specs_hash=feature_specs_hash,
        label_specs_hash="",          # not relevant to features
        m16_bars_digest=dict(m16_bars_digest),
        missingness_policy_hash=missingness_policy_hash,
        extra=dict(extra or {}),
    )
