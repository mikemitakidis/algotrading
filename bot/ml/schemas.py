"""bot.ml.schemas — locked allowlists and dataclass schemas (M18.A.1).

RECONSTRUCTED_FROM_TRANSCRIPT_NOT_BYTE_IDENTICAL.

The M18.A.1 originating commit (5ed45e4) was in a session whose
transcript was not captured in /mnt/transcripts/. This file is
reconstructed from:

  - All ALLOWED_* constant contents captured in bash outputs.
  - Class declaration line offsets (FeatureSpec @ 113, FeatureGroupSchema
    @ 291, LabelSpec @ 349, DatasetConfig @ 536, TrainConfig @ 666).
  - Full FeatureSpec dataclass body + from_dict partial (bash cat).
  - Full LabelSpec dataclass body + from_dict partial (bash cat).
  - Full TrainConfig dataclass body + from_dict + to_dict
    (view tool_result for lines [660, 760] in transcript #7).
  - The locked __all__ export list (same view).
  - Every `from bot.ml.schemas import X` across the 47 recovered files.

Anything not preserved verbatim from those sources is a best-effort
reconstruction that satisfies the importer contract and the locked
allowlist semantics described in the M18 design doc (Q-checklist).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import (
    Any, Dict, FrozenSet, List, Mapping, Optional, Tuple,
)

from bot.ml.errors import (
    FeatureSchemaError,
    LabelSchemaError,
    M18ConfigError,
    M18SchemaError,
)


# ═════════════════════════════════════════════════════════════════════
# Locked allowlists (M18.A.1 — never silently widened)
# ═════════════════════════════════════════════════════════════════════

# ─── Leak classes ────────────────────────────────────────────────────

LEAK_CLASS_SAFE                    = "safe"
LEAK_CLASS_REQUIRES_PAST_FLYWHEEL  = "requires_past_flywheel_only"
LEAK_CLASS_FUTURE_LABEL            = "future_label_only"
LEAK_CLASS_FORBIDDEN               = "forbidden_as_feature"

ALLOWED_LEAK_CLASSES: FrozenSet[str] = frozenset({
    LEAK_CLASS_SAFE,
    LEAK_CLASS_REQUIRES_PAST_FLYWHEEL,
    LEAK_CLASS_FUTURE_LABEL,
    LEAK_CLASS_FORBIDDEN,
})

# Features may only carry leak_class safe or requires_past_flywheel_only.
ALLOWED_FEATURE_LEAK_CLASSES: FrozenSet[str] = frozenset({
    LEAK_CLASS_SAFE,
    LEAK_CLASS_REQUIRES_PAST_FLYWHEEL,
})

# Labels must be future_label_only — they look at data after the anchor.
ALLOWED_LABEL_LEAK_CLASSES: FrozenSet[str] = frozenset({
    LEAK_CLASS_FUTURE_LABEL,
})

# ─── Feature dtypes ──────────────────────────────────────────────────
ALLOWED_DTYPES: FrozenSet[str] = frozenset({
    "float64", "float32", "int64", "int32", "bool",
})

# ─── Label classes ───────────────────────────────────────────────────
ALLOWED_LABEL_CLASSES: FrozenSet[str] = frozenset({
    "classification_3way", "binary", "regression", "ranking",
})

# ─── Anchor timeframes ───────────────────────────────────────────────
ALLOWED_ANCHOR_TFS: FrozenSet[str] = frozenset({
    "1m", "5m", "15m", "30m", "1h", "4h", "1d",
})

# ─── Model types (B0/B1/B2 baselines + M_ main models, Q22 LightGBM) ─
ALLOWED_MODEL_TYPES: FrozenSet[str] = frozenset({
    "B0_majority",
    "B1_scanner_replica",
    "B2_logistic",
    "M_lightgbm",
    "M_random_forest",
})

# ─── Train modes (dual cohort per SR-6 / Q18) ────────────────────────
ALLOWED_TRAIN_MODES: FrozenSet[str] = frozenset({
    "model_a_meta_label",
    "model_b_candidate_quality",
})

# ─── Registry statuses (M18.A.8) ─────────────────────────────────────
ALLOWED_REGISTRY_STATUSES: FrozenSet[str] = frozenset({
    "candidate",
    "candidate_inspection_only",
    "coverage_degraded",
    "current",
    "demoted",
    "failed_adversarial_validation",
    "failed_baseline_beat",
    "failed_drift_check",
    "failed_sample_count",
    "fixture_only",
    "forced_promoted",
})


# ═════════════════════════════════════════════════════════════════════
# FeatureSpec (M18.A.1 — line 113 in original)
# ═════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FeatureSpec:
    """Spec for a single feature.

    Identity:
      feature_id            globally unique, format "group.name"
      feature_group         group this feature belongs to
      feature_group_version bump on any semantic change to the group

    Semantics:
      dtype                 element type, ALLOWED_DTYPES
      leak_class            ALLOWED_FEATURE_LEAK_CLASSES (safe |
                              requires_past_flywheel_only)
      lookback_bars         bars at lookback_unit needed before this
                              feature is computable; 0 = no lookback
      lookback_unit         "bars_at_this_tf"
      computed_from         upstream column/feature names
      description           human-readable

    Optional:
      value_range           (min, max) sanity bounds; None = unbounded
      live_compatible       True if value matches a live counterpart
                              to floating-point precision
      live_compatible_with  name of the live counterpart
      tested_in             name of the asserting G2 test
    """
    feature_id: str
    feature_group: str
    feature_group_version: int
    dtype: str
    leak_class: str
    lookback_bars: int
    lookback_unit: str
    computed_from: Tuple[str, ...]
    description: str
    value_range: Optional[Tuple[float, float]] = None
    live_compatible: bool = False
    live_compatible_with: Optional[str] = None
    tested_in: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "FeatureSpec":
        if not isinstance(d, Mapping):
            raise FeatureSchemaError(
                f"FeatureSpec.from_dict expects a Mapping, "
                f"got {type(d).__name__}")
        required = ("feature_id", "feature_group", "feature_group_version",
                     "dtype", "leak_class", "lookback_bars",
                     "lookback_unit", "computed_from", "description")
        for k in required:
            if k not in d:
                raise FeatureSchemaError(
                    f"FeatureSpec missing required key {k!r}; "
                    f"have {sorted(d.keys())}")
        fid = d["feature_id"]
        if not isinstance(fid, str) or "." not in fid or fid.startswith(".") \
              or fid.endswith("."):
            raise FeatureSchemaError(
                f"feature_id must be a string of form 'group.name', "
                f"got {fid!r}")
        group = d["feature_group"]
        if not isinstance(group, str) or not group:
            raise FeatureSchemaError(
                f"feature_id={fid!r}: feature_group must be a non-empty "
                f"string, got {group!r}")
        if not fid.startswith(group + "."):
            raise FeatureSchemaError(
                f"feature_id={fid!r} must start with feature_group + '.' "
                f"(feature_group={group!r})")
        if d["dtype"] not in ALLOWED_DTYPES:
            raise FeatureSchemaError(
                f"feature_id={fid!r}: unknown dtype {d['dtype']!r}; "
                f"allowed: {sorted(ALLOWED_DTYPES)}")
        if d["leak_class"] not in ALLOWED_FEATURE_LEAK_CLASSES:
            raise FeatureSchemaError(
                f"feature_id={fid!r}: leak_class {d['leak_class']!r} "
                f"not allowed for features. Features may only be "
                f"{sorted(ALLOWED_FEATURE_LEAK_CLASSES)}.")
        lb = d["lookback_bars"]
        if not isinstance(lb, int) or isinstance(lb, bool) or lb < 0:
            raise FeatureSchemaError(
                f"feature_id={fid!r}: lookback_bars must be int >= 0, "
                f"got {lb!r}")
        cf = d["computed_from"]
        if not isinstance(cf, (list, tuple)):
            raise FeatureSchemaError(
                f"feature_id={fid!r}: computed_from must be a list/tuple "
                f"of strings, got {type(cf).__name__}")
        cf_tuple = tuple(cf)
        if any(not isinstance(x, str) for x in cf_tuple):
            raise FeatureSchemaError(
                f"feature_id={fid!r}: computed_from entries must be "
                f"strings, got {cf_tuple!r}")
        vr = d.get("value_range")
        if vr is not None:
            if (not isinstance(vr, (list, tuple))) or len(vr) != 2:
                raise FeatureSchemaError(
                    f"feature_id={fid!r}: value_range must be a "
                    f"(min, max) pair or None, got {vr!r}")
            vr = (float(vr[0]), float(vr[1]))
        return cls(
            feature_id=fid,
            feature_group=group,
            feature_group_version=int(d["feature_group_version"]),
            dtype=d["dtype"],
            leak_class=d["leak_class"],
            lookback_bars=int(lb),
            lookback_unit=str(d["lookback_unit"]),
            computed_from=cf_tuple,
            description=str(d["description"]),
            value_range=vr,
            live_compatible=bool(d.get("live_compatible", False)),
            live_compatible_with=d.get("live_compatible_with"),
            tested_in=d.get("tested_in"),
        )

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["computed_from"] = list(self.computed_from)
        if self.value_range is not None:
            out["value_range"] = list(self.value_range)
        return out


# ═════════════════════════════════════════════════════════════════════
# FeatureGroupSchema (M18.A.1 — line 291 in original)
# ═════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FeatureGroupSchema:
    """Aggregates the FeatureSpecs that make up one feature group.

    A feature group is the unit at which lookback, dtype, and leak_class
    are usually homogeneous. The group is identified by `group_name` and
    versioned by `group_version` — bump the version on any semantic
    change to any feature in the group.
    """
    group_name: str
    group_version: int
    feature_specs: Tuple[FeatureSpec, ...]
    description: str = ""

    @classmethod
    def from_specs(
        cls,
        group_name: str,
        group_version: int,
        specs: List[FeatureSpec],
        description: str = "",
    ) -> "FeatureGroupSchema":
        for s in specs:
            if s.feature_group != group_name:
                raise FeatureSchemaError(
                    f"FeatureGroupSchema {group_name!r}: spec "
                    f"{s.feature_id!r} declares feature_group="
                    f"{s.feature_group!r}")
            if s.feature_group_version != group_version:
                raise FeatureSchemaError(
                    f"FeatureGroupSchema {group_name!r}: spec "
                    f"{s.feature_id!r} declares feature_group_version="
                    f"{s.feature_group_version}, group expects "
                    f"{group_version}")
        return cls(
            group_name=group_name,
            group_version=group_version,
            feature_specs=tuple(specs),
            description=description,
        )


# ═════════════════════════════════════════════════════════════════════
# LabelSpec (M18.A.1 — line 349 in original)
# ═════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class LabelSpec:
    """Spec for a single label.

    Identity:
      label_id              globally unique
      label_schema_version  bump on any semantic change

    Semantics:
      label_class           ALLOWED_LABEL_CLASSES
      horizon_bars          max bars after anchor used to resolve label
      horizon_unit          "bars_at_anchor_tf"
      leak_class            MUST be 'future_label_only' (asserted)
      computed_from         column names used to compute the label
      description           human-readable
      cost_model_applied    True if label includes fees/slippage

    Triple-barrier-specific (optional, used by triple_barrier_*):
      target_values         {"+1": "target_hit", ...}
      tp_mult / sl_mult     ATR multipliers
      atr_source            feature_id used as ATR source
      entry_price_source    "next_bar_open_after_anchor" etc.
      tie_breaker           "pessimistic_stop_first" etc.

    Optional:
      tested_in             name of the G3 test that asserts this label
    """
    label_id: str
    label_schema_version: int
    label_class: str
    horizon_bars: int
    horizon_unit: str
    leak_class: str
    computed_from: Tuple[str, ...]
    description: str
    cost_model_applied: bool = False
    target_values: Optional[Dict[str, str]] = None
    tp_mult: Optional[float] = None
    sl_mult: Optional[float] = None
    atr_source: Optional[str] = None
    entry_price_source: Optional[str] = None
    tie_breaker: Optional[str] = None
    tested_in: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "LabelSpec":
        if not isinstance(d, Mapping):
            raise LabelSchemaError(
                f"LabelSpec.from_dict expects a Mapping, "
                f"got {type(d).__name__}")
        required = ("label_id", "label_schema_version", "label_class",
                     "horizon_bars", "horizon_unit", "leak_class",
                     "computed_from", "description")
        for k in required:
            if k not in d:
                raise LabelSchemaError(
                    f"LabelSpec missing required key {k!r}; "
                    f"have {sorted(d.keys())}")
        lid = d["label_id"]
        if not isinstance(lid, str) or not lid:
            raise LabelSchemaError(
                f"label_id must be a non-empty string, got {lid!r}")
        lc = d["label_class"]
        if lc not in ALLOWED_LABEL_CLASSES:
            raise LabelSchemaError(
                f"label_id={lid!r}: label_class {lc!r} not in "
                f"{sorted(ALLOWED_LABEL_CLASSES)}")
        hb = d["horizon_bars"]
        if not isinstance(hb, int) or isinstance(hb, bool) or hb <= 0:
            raise LabelSchemaError(
                f"label_id={lid!r}: horizon_bars must be int > 0, "
                f"got {hb!r}")
        if d["leak_class"] not in ALLOWED_LABEL_LEAK_CLASSES:
            raise LabelSchemaError(
                f"label_id={lid!r}: leak_class must be "
                f"{sorted(ALLOWED_LABEL_LEAK_CLASSES)}, got "
                f"{d['leak_class']!r}")
        cf = d["computed_from"]
        if not isinstance(cf, (list, tuple)):
            raise LabelSchemaError(
                f"label_id={lid!r}: computed_from must be list/tuple")
        cf_tuple = tuple(cf)
        if any(not isinstance(x, str) for x in cf_tuple):
            raise LabelSchemaError(
                f"label_id={lid!r}: computed_from entries must be strings")
        tv = d.get("target_values")
        if tv is not None and not isinstance(tv, Mapping):
            raise LabelSchemaError(
                f"label_id={lid!r}: target_values must be a mapping or None")
        return cls(
            label_id=lid,
            label_schema_version=int(d["label_schema_version"]),
            label_class=lc,
            horizon_bars=int(hb),
            horizon_unit=str(d["horizon_unit"]),
            leak_class=d["leak_class"],
            computed_from=cf_tuple,
            description=str(d["description"]),
            cost_model_applied=bool(d.get("cost_model_applied", False)),
            target_values=dict(tv) if tv is not None else None,
            tp_mult=float(d["tp_mult"]) if d.get("tp_mult") is not None else None,
            sl_mult=float(d["sl_mult"]) if d.get("sl_mult") is not None else None,
            atr_source=d.get("atr_source"),
            entry_price_source=d.get("entry_price_source"),
            tie_breaker=d.get("tie_breaker"),
            tested_in=d.get("tested_in"),
        )

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["computed_from"] = list(self.computed_from)
        if self.target_values is not None:
            out["target_values"] = dict(self.target_values)
        return out


# ═════════════════════════════════════════════════════════════════════
# DatasetConfig (M18.A.1 — line 536 in original)
# ═════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class DatasetConfig:
    """User-facing config for assembling a single dataset (M18.A.1).

    symbols                tickers to build the dataset for (>=1)
    anchor_tf              chosen anchor timeframe (must be in
                             ALLOWED_ANCHOR_TFS)
    start_date             ISO date string 'YYYY-MM-DD', inclusive
    end_date               ISO date string 'YYYY-MM-DD', exclusive;
                             must be > start_date
    feature_groups         feature group names to include (>=1)
    labels                 label_ids to compute (>=1)
    train_pct              fraction for train split (default 0.6)
    val_pct                fraction for val split (default 0.2)
    test_pct               fraction for test split (default 0.2);
                             train+val+test must sum to 1.0, all > 0
    embargo_trading_days   purge/embargo width in trading days
                             (int >= 0, default 5)
    require_intraday       if True, intraday TFs must be present
                             (default False)
    fixture_mode           Q16 / Amendment 2: bypass thinness gates;
                             permanently tags artifacts as fixture_only
    """
    symbols: Tuple[str, ...]
    anchor_tf: str
    start_date: str
    end_date: str
    feature_groups: Tuple[str, ...]
    labels: Tuple[str, ...]
    train_pct: float = 0.6
    val_pct: float = 0.2
    test_pct: float = 0.2
    embargo_trading_days: int = 5
    require_intraday: bool = False
    fixture_mode: bool = False

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "DatasetConfig":
        if not isinstance(d, Mapping):
            raise M18ConfigError(
                f"DatasetConfig.from_dict expects a Mapping")
        required = ("symbols", "anchor_tf", "start_date", "end_date",
                     "feature_groups", "labels")
        for k in required:
            if k not in d:
                raise M18ConfigError(
                    f"DatasetConfig missing required key {k!r}")
        syms = d["symbols"]
        if not isinstance(syms, (list, tuple)) or not syms:
            raise M18ConfigError(
                f"DatasetConfig.symbols must be non-empty list/tuple")
        for s in syms:
            if not isinstance(s, str) or not s:
                raise M18ConfigError(
                    f"DatasetConfig.symbols entries must be non-empty "
                    f"strings; got {s!r}")
        anchor_tf = d["anchor_tf"]
        if anchor_tf not in ALLOWED_ANCHOR_TFS:
            raise M18ConfigError(
                f"DatasetConfig.anchor_tf must be in "
                f"{sorted(ALLOWED_ANCHOR_TFS)}; got {anchor_tf!r}")
        sd = d["start_date"]
        ed = d["end_date"]
        if not isinstance(sd, str) or not isinstance(ed, str):
            raise M18ConfigError(
                f"DatasetConfig.start_date and end_date must be ISO "
                f"date strings 'YYYY-MM-DD'")
        if sd >= ed:
            raise M18ConfigError(
                f"DatasetConfig.start_date={sd!r} must be < "
                f"end_date={ed!r}")
        fgs = d["feature_groups"]
        if not isinstance(fgs, (list, tuple)) or not fgs:
            raise M18ConfigError(
                f"DatasetConfig.feature_groups must be non-empty "
                f"list/tuple")
        labels = d["labels"]
        if not isinstance(labels, (list, tuple)) or not labels:
            raise M18ConfigError(
                f"DatasetConfig.labels must be non-empty list/tuple")
        train_pct = float(d.get("train_pct", 0.6))
        val_pct   = float(d.get("val_pct",   0.2))
        test_pct  = float(d.get("test_pct",  0.2))
        total = train_pct + val_pct + test_pct
        if abs(total - 1.0) > 1e-9:
            raise M18ConfigError(
                f"DatasetConfig: train+val+test must sum to 1.0, "
                f"got {total}")
        if min(train_pct, val_pct, test_pct) <= 0:
            raise M18ConfigError(
                f"DatasetConfig: all of train/val/test must be > 0")
        emb = d.get("embargo_trading_days", 5)
        if not isinstance(emb, int) or isinstance(emb, bool) or emb < 0:
            raise M18ConfigError(
                f"DatasetConfig.embargo_trading_days must be int >= 0")
        ri = bool(d.get("require_intraday", False))
        fm = bool(d.get("fixture_mode", False))
        return cls(
            symbols=tuple(syms),
            anchor_tf=anchor_tf,
            start_date=sd,
            end_date=ed,
            feature_groups=tuple(fgs),
            labels=tuple(labels),
            train_pct=train_pct,
            val_pct=val_pct,
            test_pct=test_pct,
            embargo_trading_days=emb,
            require_intraday=ri,
            fixture_mode=fm,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbols":              list(self.symbols),
            "anchor_tf":            self.anchor_tf,
            "start_date":           self.start_date,
            "end_date":             self.end_date,
            "feature_groups":       list(self.feature_groups),
            "labels":               list(self.labels),
            "train_pct":            self.train_pct,
            "val_pct":              self.val_pct,
            "test_pct":             self.test_pct,
            "embargo_trading_days": self.embargo_trading_days,
            "require_intraday":     self.require_intraday,
            "fixture_mode":         self.fixture_mode,
        }


# ═════════════════════════════════════════════════════════════════════
# TrainConfig (M18.A.1 — line 666 in original; full body recovered
# from view tool_result for lines [660, 760] in transcript #7)
# ═════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TrainConfig:
    """Config for a single training run (M18.A.6).

    dataset_id           the assembled dataset to train on
    model_type           ALLOWED_MODEL_TYPES
    train_mode           ALLOWED_TRAIN_MODES (Model A or Model B per SR-6)
    target_label_id      which label to predict
    hyperparameters      model-specific dict
    seed                 fixed seed for determinism (SR-4)
    fixture_mode         Q16 / Amendment 2: bypass thinness gates;
                          permanently tags artifacts as fixture_only;
                          can never be --force promoted
    """
    dataset_id: str
    model_type: str
    train_mode: str
    target_label_id: str
    hyperparameters: Dict[str, Any]
    seed: int = 42
    fixture_mode: bool = False

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TrainConfig":
        if not isinstance(d, Mapping):
            raise M18ConfigError(
                f"TrainConfig.from_dict expects a Mapping")
        required = ("dataset_id", "model_type", "train_mode",
                     "target_label_id", "hyperparameters")
        for k in required:
            if k not in d:
                raise M18ConfigError(
                    f"TrainConfig missing required key {k!r}")
        ds_id = d["dataset_id"]
        if not isinstance(ds_id, str) or not ds_id:
            raise M18ConfigError(
                f"TrainConfig.dataset_id must be non-empty string")
        mt = d["model_type"]
        if mt not in ALLOWED_MODEL_TYPES:
            raise M18ConfigError(
                f"TrainConfig.model_type {mt!r} not in "
                f"{sorted(ALLOWED_MODEL_TYPES)}")
        tm = d["train_mode"]
        if tm not in ALLOWED_TRAIN_MODES:
            raise M18ConfigError(
                f"TrainConfig.train_mode {tm!r} not in "
                f"{sorted(ALLOWED_TRAIN_MODES)}")
        tlid = d["target_label_id"]
        if not isinstance(tlid, str) or not tlid:
            raise M18ConfigError(
                f"TrainConfig.target_label_id must be non-empty string")
        hps = d["hyperparameters"]
        if not isinstance(hps, Mapping):
            raise M18ConfigError(
                f"TrainConfig.hyperparameters must be a Mapping")
        seed = d.get("seed", 42)
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise M18ConfigError(
                f"TrainConfig.seed must be int; got {type(seed).__name__}")
        fm = bool(d.get("fixture_mode", False))
        return cls(
            dataset_id=ds_id,
            model_type=mt,
            train_mode=tm,
            target_label_id=tlid,
            hyperparameters=dict(hps),
            seed=seed,
            fixture_mode=fm,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id":      self.dataset_id,
            "model_type":      self.model_type,
            "train_mode":      self.train_mode,
            "target_label_id": self.target_label_id,
            "hyperparameters": dict(self.hyperparameters),
            "seed":            self.seed,
            "fixture_mode":    self.fixture_mode,
        }


__all__ = [
    # Allowed-value sets
    "LEAK_CLASS_SAFE", "LEAK_CLASS_REQUIRES_PAST_FLYWHEEL",
    "LEAK_CLASS_FUTURE_LABEL", "LEAK_CLASS_FORBIDDEN",
    "ALLOWED_LEAK_CLASSES", "ALLOWED_FEATURE_LEAK_CLASSES",
    "ALLOWED_LABEL_LEAK_CLASSES",
    "ALLOWED_DTYPES", "ALLOWED_LABEL_CLASSES", "ALLOWED_ANCHOR_TFS",
    "ALLOWED_MODEL_TYPES", "ALLOWED_TRAIN_MODES",
    "ALLOWED_REGISTRY_STATUSES",
    # Schema dataclasses
    "FeatureSpec", "FeatureGroupSchema",
    "LabelSpec",
    "DatasetConfig", "TrainConfig",
]
