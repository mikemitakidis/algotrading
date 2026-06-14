"""bot.ml.store.label_store — content-addressed label cache (M18.B.7).

Same content-addressed mechanics as FeatureStore, but for computed
label DataFrames. Label identity depends on the LABEL schema (and the
bars digest), and parquet round-trips the `<group>.is_pending` columns
so pending status is preserved exactly.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from bot.ml.store.feature_store import _ContentAddressedStore
from bot.ml.store.metadata import StoreKey


class LabelStore(_ContentAddressedStore):
    KIND = "label"


def make_label_key(
    *, symbol: str, anchor_tf: str, anchor_set: str, timeframes,
    label_specs_hash: str, m16_bars_digest: Dict[str, Any],
    extra: Optional[Dict] = None,
) -> StoreKey:
    """Build a StoreKey for a LABEL artifact. feature_specs_hash and the
    missingness policy are NOT part of label identity (labels are
    computed from bars + the label schema, independent of features /
    feature-missingness fill)."""
    return StoreKey(
        kind="label", symbol=symbol, anchor_tf=anchor_tf,
        anchor_set=anchor_set, timeframes=list(timeframes),
        feature_specs_hash="",        # not relevant to labels
        label_specs_hash=label_specs_hash,
        m16_bars_digest=dict(m16_bars_digest),
        missingness_policy_hash="",   # not relevant to labels
        extra=dict(extra or {}),
    )
