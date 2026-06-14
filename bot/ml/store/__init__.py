"""bot.ml.store — content-addressed feature & label stores (M18.B.7).

Storage infrastructure for caching/reusing computed features and labels
across dataset builds. Identity = content hash of (schema hashes, M16
bars digest, missingness policy, symbol/timeframe/anchor/config), so
any change to those inputs automatically invalidates the cache (the
old artifact is simply no longer addressed). Never touches signals.db,
brokers, dashboards, or live trading; artifacts are never committed.
"""
from bot.ml.store.metadata import (
    StoreKey, StoreMetadata, STORE_SCHEMA_VERSION, STORE_HASH_SCHEME,
)
from bot.ml.store.feature_store import (
    FeatureStore, CacheResult, make_feature_key,
)
from bot.ml.store.label_store import LabelStore, make_label_key

__all__ = [
    "StoreKey", "StoreMetadata", "STORE_SCHEMA_VERSION",
    "STORE_HASH_SCHEME", "FeatureStore", "LabelStore", "CacheResult",
    "make_feature_key", "make_label_key",
]
