"""bot.ml.dataset.assembler — single-symbol dataset assembler.

End-to-end pipeline:

    bars (per TF) ──┐
                     ├─► compute all feature groups (M18.A.2/A.3)
                     ├─► compute all label groups   (M18.A.4)
                     └─► join on anchor TF index, exclude pending
                          ▼
                       restrict to anchor set (Model A or Model B per Q18)
                          ▼
                       coverage assessment (Q19 — degraded flag)
                          ▼
                       walk-forward split with embargo + label-overlap purge
                          ▼
                       adversarial validation (sklearn LR + CV AUC + 0.55 gate)
                          ▼
                       emit DatasetManifest + AssemblerResult

Scope:
  * Single-symbol (multi-symbol cross-section is a later phase).
  * Caller provides per_tf_bars, optional symbol_metadata_path,
    optional flywheel_reader, optional benchmark_bars. No internal
    I/O — the m16_loader can supply bars, but the assembler doesn't
    call it directly (keeps the assembler testable on synthetic data).
  * No CLI yet (M18.A.9 owns CLI consolidation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd

from bot.ml.dataset.anchors import (
    ANCHOR_SET_MODEL_A_SCANNER_REPLICA,
    ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
    ALLOWED_ANCHOR_SETS,
    enumerate_anchors,
)
from bot.ml.dataset.coverage import (
    assess_intraday_coverage,
    CoverageReport,
    DEFAULT_MIN_BARS_PER_TF,
)
from bot.ml.dataset.manifest import (
    DatasetManifest,
    MANIFEST_SCHEMA_VERSION,
    _bars_digest,
    compute_dataset_hash,
    compute_feature_specs_hash,
    compute_label_specs_hash,
    current_utc_iso,
)
from bot.ml.dataset.walk_forward import (
    WalkForwardSplit,
    default_embargo_bars,
    make_walk_forward_split,
)
from bot.ml.dataset.adversarial_validation import (
    AdversarialValidationResult,
    run_adversarial_validation,
)
from bot.ml.dataset.flywheel_reader import FlywheelReader

from bot.ml.features import (
    SAFE_FEATURE_GROUPS_V2,
    EXTENDED_FEATURE_GROUPS_V3,
    ALL_FEATURE_GROUPS,
)
from bot.ml.labels import ALL_LABEL_GROUPS


@dataclass
class AssemblerConfig:
    """Configuration for one dataset build."""
    symbol: str
    anchor_tf: str = "15m"
    timeframes: tuple = ("1D", "4H", "1H", "15m")
    anchor_set: str = ANCHOR_SET_MODEL_A_SCANNER_REPLICA
    train_frac: float = 0.6
    val_frac:   float = 0.2
    test_frac:  float = 0.2
    embargo_trading_days: int = 5
    embargo_bars_override: Optional[int] = None
    require_intraday: bool = True
    fixture_mode: bool = False
    min_bars_per_tf: int = DEFAULT_MIN_BARS_PER_TF
    adversarial_threshold: float = 0.55
    adversarial_cv_folds: int = 5
    adversarial_random_state: int = 42
    skip_adversarial: bool = False   # set True when sample sizes
                                       # are too small to be meaningful
                                       # (e.g. fixture_mode)

    def resolved_embargo_bars(self) -> int:
        if self.embargo_bars_override is not None:
            return int(self.embargo_bars_override)
        return default_embargo_bars(self.anchor_tf,
                                      self.embargo_trading_days)


@dataclass
class AssemblerResult:
    """End-to-end output of DatasetAssembler.build()."""
    dataset: pd.DataFrame
    manifest: DatasetManifest
    split: Optional[WalkForwardSplit]
    coverage_report: CoverageReport
    adversarial_validation: Optional[AdversarialValidationResult]


class DatasetAssembler:
    """Orchestrates one dataset build for one symbol."""

    def __init__(self, config: AssemblerConfig):
        if config.anchor_set not in ALLOWED_ANCHOR_SETS:
            raise ValueError(
                f"anchor_set must be one of {sorted(ALLOWED_ANCHOR_SETS)}, "
                f"got {config.anchor_set!r}")
        if config.anchor_tf not in config.timeframes:
            raise ValueError(
                f"anchor_tf={config.anchor_tf!r} must be in "
                f"timeframes={config.timeframes}")
        self.cfg = config

    # ── Feature compute ─────────────────────────────────────────────

    def _compute_features(
        self,
        anchor_bars: pd.DataFrame,
        per_tf_bars: Dict[str, pd.DataFrame],
        *,
        symbol_metadata_path: Optional[Union[str, Path]],
        flywheel_reader: Optional[FlywheelReader],
        benchmark_bars: Optional[Dict[str, pd.DataFrame]],
    ) -> pd.DataFrame:
        """Compute every registered feature group and concat the
        outputs column-wise. Returns a single DataFrame indexed
        identically to `anchor_bars`."""
        parts: List[pd.DataFrame] = []
        # Single-TF safe groups (M18.A.2)
        for name, mod in SAFE_FEATURE_GROUPS_V2.items():
            parts.append(mod.compute(anchor_bars))
        # Multi-TF / context groups (M18.A.3)
        parts.append(EXTENDED_FEATURE_GROUPS_V3["mtf_confluence"]
                       .compute(anchor_bars, per_tf_bars=per_tf_bars,
                                  anchor_tf=self.cfg.anchor_tf))
        parts.append(EXTENDED_FEATURE_GROUPS_V3["scanner_replica"]
                       .compute(anchor_bars, per_tf_bars=per_tf_bars,
                                  anchor_tf=self.cfg.anchor_tf))
        parts.append(EXTENDED_FEATURE_GROUPS_V3["market_context"]
                       .compute(anchor_bars,
                                  benchmark_bars=(benchmark_bars or {})))
        # symbol_meta REQUIRES either metadata dict or metadata_path;
        # if the caller supplied neither, inject the minimal valid
        # default (an empty symbols block with the encodings tables
        # the example file uses). Every symbol resolves to "unknown".
        if symbol_metadata_path is None:
            default_meta = {
                "schema_version": 1,
                "symbols": {},
                "encodings": {
                    "sector":            {"unknown": 99},
                    "market_cap_bucket": {"unknown": 99},
                    "asset_class":       {"unknown": 99},
                },
            }
            parts.append(EXTENDED_FEATURE_GROUPS_V3["symbol_meta"]
                           .compute(anchor_bars,
                                      symbol=self.cfg.symbol,
                                      metadata=default_meta))
        else:
            parts.append(EXTENDED_FEATURE_GROUPS_V3["symbol_meta"]
                           .compute(anchor_bars,
                                      symbol=self.cfg.symbol,
                                      metadata_path=symbol_metadata_path))
        parts.append(EXTENDED_FEATURE_GROUPS_V3["signal_history"]
                       .compute(anchor_bars,
                                  symbol=self.cfg.symbol,
                                  flywheel_reader=flywheel_reader))
        out = pd.concat(parts, axis=1)
        return out

    # ── Label compute ───────────────────────────────────────────────

    def _compute_labels(
        self,
        anchor_bars: pd.DataFrame,
        feature_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """Compute every registered label group. The triple_barrier,
        mfe_mae, and risk_adjusted groups depend on the ATR series
        from the vol_regime feature group — we read it out of
        feature_df rather than recomputing."""
        atr_series = feature_df["vol_regime.atr_14_sma_true_range"]
        parts: List[pd.DataFrame] = []
        parts.append(ALL_LABEL_GROUPS["triple_barrier"]
                       .compute(anchor_bars, atr_series=atr_series))
        parts.append(ALL_LABEL_GROUPS["forward_returns"]
                       .compute(anchor_bars))
        parts.append(ALL_LABEL_GROUPS["mfe_mae"]
                       .compute(anchor_bars, atr_series=atr_series))
        parts.append(ALL_LABEL_GROUPS["risk_adjusted"]
                       .compute(anchor_bars, atr_series=atr_series))
        return pd.concat(parts, axis=1)

    # ── Join + pending exclusion ────────────────────────────────────

    @staticmethod
    def _is_pending_columns(label_df: pd.DataFrame) -> List[str]:
        return [c for c in label_df.columns if c.endswith(".is_pending")]

    @staticmethod
    def _any_label_pending_mask(label_df: pd.DataFrame) -> pd.Series:
        """A row is "any-pending" if ANY label is_pending. We exclude
        these from train/val/test so every row is fully labeled."""
        pending_cols = DatasetAssembler._is_pending_columns(label_df)
        if not pending_cols:
            return pd.Series(False, index=label_df.index)
        # Each column is int8 {0, 1}; bitwise-or across columns.
        out = pd.Series(False, index=label_df.index)
        for c in pending_cols:
            out = out | (label_df[c] == 1)
        return out

    # ── Main build ─────────────────────────────────────────────────

    def build(
        self,
        per_tf_bars: Dict[str, pd.DataFrame],
        *,
        symbol_metadata_path: Optional[Union[str, Path]] = None,
        flywheel_reader: Optional[FlywheelReader] = None,
        benchmark_bars: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> AssemblerResult:
        """Build one dataset end-to-end."""
        # 0. UNCONDITIONAL anchor-TF presence check (Q19 strict rule).
        # The requested anchor TF MUST be present with at least one
        # bar. Without anchor bars there are no rows to compute
        # features/labels on, so this fails regardless of
        # require_intraday — require_intraday=False only permits
        # degraded coverage in the OTHER required TFs, not in the
        # anchor TF itself. M18.A.5 does NOT support automatic
        # anchor-TF substitution; the actual_anchor_tf manifest field
        # is reserved for a future substitution feature.
        anchor_tf = self.cfg.anchor_tf
        if (anchor_tf not in per_tf_bars
                or per_tf_bars[anchor_tf] is None
                or len(per_tf_bars[anchor_tf]) == 0):
            from bot.ml.errors import InsufficientIntradayCoverageError
            from bot.ml.dataset._m16_backfill import format_backfill_command
            cmd = format_backfill_command(self.cfg.symbol, anchor_tf)
            raise InsufficientIntradayCoverageError(
                f"Requested anchor timeframe {anchor_tf!r} is missing "
                f"or empty for symbol={self.cfg.symbol!r}; cannot build "
                f"any dataset (no anchor bars to compute features or "
                f"labels on).\n"
                f"M18.A.5 does NOT perform automatic anchor-TF "
                f"substitution — the requested anchor TF must be "
                f"backfilled before this dataset can be assembled:\n"
                f"{cmd}\n"
                f"This failure is unconditional and is NOT bypassable "
                f"by require_intraday=False, which only permits "
                f"degraded non-anchor TFs (the manifest will mark such "
                f"datasets coverage_degraded=True and "
                f"promotion_eligible=False)."
            )

        # 1. Q19 coverage assessment for non-anchor TFs.
        coverage = assess_intraday_coverage(
            per_tf_bars,
            min_bars_per_tf=self.cfg.min_bars_per_tf)
        if self.cfg.require_intraday:
            coverage.assert_promotable_or_raise(symbol=self.cfg.symbol)

        anchor_bars = per_tf_bars[anchor_tf].reset_index(drop=True)

        # 2. Compute features (all groups)
        feature_df = self._compute_features(
            anchor_bars=anchor_bars,
            per_tf_bars=per_tf_bars,
            symbol_metadata_path=symbol_metadata_path,
            flywheel_reader=flywheel_reader,
            benchmark_bars=benchmark_bars,
        )

        # 3. Compute labels (all groups)
        label_df = self._compute_labels(
            anchor_bars=anchor_bars,
            feature_df=feature_df,
        )

        # 4. Join
        dataset = pd.concat([
            anchor_bars[["ts_utc"]].reset_index(drop=True),
            feature_df.reset_index(drop=True),
            label_df.reset_index(drop=True),
        ], axis=1)

        # 5. Enumerate the anchor set
        scanner_fires = feature_df["scanner_replica.signal_fires"]
        one_hour_ts = (per_tf_bars.get("1H")["ts_utc"]
                        if (per_tf_bars.get("1H") is not None
                              and len(per_tf_bars["1H"]) > 0)
                        else pd.Series(
                            [], dtype="datetime64[ns, UTC]"))
        raw_anchor_idx = enumerate_anchors(
            anchor_set=self.cfg.anchor_set,
            anchor_ts=anchor_bars["ts_utc"],
            one_hour_ts=one_hour_ts,
            scanner_replica_fires=scanner_fires,
        )
        anchor_count_raw = int(len(raw_anchor_idx))

        # 6. Pending exclusion (any label pending → row dropped)
        any_pending = self._any_label_pending_mask(label_df)
        # Translate to positions: keep raw_anchor_idx rows where
        # any_pending == False at that position.
        if anchor_count_raw == 0:
            kept_anchor_idx = raw_anchor_idx
            anchor_count_pending_excluded = 0
        else:
            pending_at_anchors = any_pending.iloc[
                raw_anchor_idx].to_numpy()
            kept_anchor_idx = raw_anchor_idx[~pending_at_anchors]
            anchor_count_pending_excluded = int(
                anchor_count_raw - len(kept_anchor_idx))
        anchor_count_total = int(len(kept_anchor_idx))

        # 7. Walk-forward split (only if we have enough rows AND we
        #    aren't a tiny fixture run with no meaningful split)
        embargo_bars = self.cfg.resolved_embargo_bars()
        split: Optional[WalkForwardSplit] = None
        if anchor_count_total >= 10:   # below 10, splits are silly
            label_resolved_ts_map: Dict[str, pd.Series] = {
                c.replace(".resolved_ts", ""): label_df[c].iloc[
                    kept_anchor_idx].reset_index(drop=True)
                for c in label_df.columns
                if c.endswith(".resolved_ts")
            }
            anchor_ts_arr = pd.to_datetime(
                anchor_bars["ts_utc"].iloc[kept_anchor_idx],
                utc=True).to_numpy()
            try:
                split = make_walk_forward_split(
                    anchor_indices=kept_anchor_idx.astype(np.int64),
                    anchor_ts=anchor_ts_arr,
                    label_resolved_ts=label_resolved_ts_map,
                    train_frac=self.cfg.train_frac,
                    val_frac=self.cfg.val_frac,
                    test_frac=self.cfg.test_frac,
                    embargo_bars=embargo_bars,
                )
            except ValueError:
                # Anchor count too small for requested fractions.
                split = None

        # 8. Adversarial validation (TRAIN vs TEST cohort)
        av_result: Optional[AdversarialValidationResult] = None
        if split is not None and not self.cfg.skip_adversarial:
            feature_cols = list(feature_df.columns)
            X_train = dataset.iloc[
                split.train_anchor_indices][feature_cols]
            X_test  = dataset.iloc[
                split.test_anchor_indices][feature_cols]
            try:
                av_result = run_adversarial_validation(
                    X_train=X_train,
                    X_holdout=X_test,
                    threshold=self.cfg.adversarial_threshold,
                    cv_folds=self.cfg.adversarial_cv_folds,
                    random_state=self.cfg.adversarial_random_state,
                )
            except Exception:
                # Don't fail the whole build if AV can't run (e.g.
                # too few rows after NaN drop). Manifest records
                # av=None so the caller can choose to block.
                av_result = None

        # 9. Manifest
        bars_digest_dict = _bars_digest(per_tf_bars)
        feat_hash = compute_feature_specs_hash(ALL_FEATURE_GROUPS)
        lbl_hash  = compute_label_specs_hash(ALL_LABEL_GROUPS)
        dataset_hash = compute_dataset_hash(
            symbol=self.cfg.symbol,
            timeframes=list(self.cfg.timeframes),
            anchor_tf=self.cfg.anchor_tf,
            anchor_set=self.cfg.anchor_set,
            bars_digest=bars_digest_dict,
            feature_specs_hash=feat_hash,
            label_specs_hash=lbl_hash,
            train_frac=self.cfg.train_frac,
            val_frac=self.cfg.val_frac,
            test_frac=self.cfg.test_frac,
            embargo_bars=embargo_bars,
            fixture_mode_invocation=self.cfg.fixture_mode,
        )

        # 9. Manifest. Compute Q19 timeframe lists and the promotion
        #    gate explicitly so downstream M18.A.8 promotion can be
        #    a one-line boolean check on `promotion_eligible`.
        requested_tfs = sorted(self.cfg.timeframes)
        available_tfs_list = sorted([
            tf for tf in per_tf_bars
            if per_tf_bars[tf] is not None
              and len(per_tf_bars[tf]) > 0
        ])
        missing_tfs_list = sorted(
            set(requested_tfs) - set(available_tfs_list))

        # Q19/Q16: fixture mode + skip_adversarial both imply a
        # dataset that can NEVER be promoted (M18.A.8 contract).
        fixture_only = bool(self.cfg.fixture_mode
                              or self.cfg.skip_adversarial)

        # Promotion-eligibility gate. Build the reason list first so
        # we can report it; promotion_eligible is True iff empty.
        promotion_blocked_reasons: List[str] = []
        if coverage.coverage_degraded:
            promotion_blocked_reasons.append("coverage_degraded")
        if fixture_only:
            promotion_blocked_reasons.append("fixture_only")
        if av_result is None:
            promotion_blocked_reasons.append(
                "adversarial_validation_not_run")
        elif not av_result.passed:
            promotion_blocked_reasons.append(
                "adversarial_validation_failed")
        promotion_eligible = (len(promotion_blocked_reasons) == 0)

        manifest = DatasetManifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            dataset_id=(
                f"{self.cfg.symbol}_{self.cfg.anchor_tf}"
                f"_{self.cfg.anchor_set}_{dataset_hash[:8]}"),
            dataset_hash_sha256=dataset_hash,
            created_at_utc=current_utc_iso(),
            symbol=self.cfg.symbol,
            requested_timeframes=requested_tfs,
            available_timeframes=available_tfs_list,
            missing_timeframes=missing_tfs_list,
            requested_anchor_tf=self.cfg.anchor_tf,
            actual_anchor_tf=self.cfg.anchor_tf,
            bar_window_start_utc=str(
                pd.to_datetime(anchor_bars["ts_utc"].iloc[0], utc=True)),
            bar_window_end_utc=str(
                pd.to_datetime(anchor_bars["ts_utc"].iloc[-1], utc=True)),
            bars_per_tf={
                tf: int(len(per_tf_bars[tf]))
                for tf in per_tf_bars
                if per_tf_bars[tf] is not None
            },
            coverage_degraded=coverage.coverage_degraded,
            degradation_warning=coverage.degradation_warning,
            anchor_set=self.cfg.anchor_set,
            anchor_count_raw=anchor_count_raw,
            anchor_count_pending_excluded=anchor_count_pending_excluded,
            anchor_count_total=anchor_count_total,
            anchor_count_train=(int(len(split.train_anchor_indices))
                                  if split else 0),
            anchor_count_val=(int(len(split.val_anchor_indices))
                                if split else 0),
            anchor_count_test=(int(len(split.test_anchor_indices))
                                 if split else 0),
            anchor_count_purged=(split.purged_count if split else 0),
            anchor_count_embargoed=(split.embargoed_count
                                      if split else 0),
            feature_specs_hash=feat_hash,
            label_specs_hash=lbl_hash,
            feature_count=int(feature_df.shape[1]),
            label_count=sum(
                1 for g in ALL_LABEL_GROUPS.values() for _ in g.SPECS),
            walk_forward={
                "embargo_bars":                       embargo_bars,
                "embargo_trading_days":               int(
                    self.cfg.embargo_trading_days),
                "train_frac":                         float(
                    self.cfg.train_frac),
                "val_frac":                           float(
                    self.cfg.val_frac),
                "test_frac":                          float(
                    self.cfg.test_frac),
                "label_resolved_ts_purge_applied":    True,
                "split_built":                        split is not None,
            },
            fixture_mode_invocation=bool(self.cfg.fixture_mode),
            fixture_only=fixture_only,
            promotion_eligible=promotion_eligible,
            promotion_blocked_reasons=promotion_blocked_reasons,
            adversarial_validation=(av_result.to_dict()
                                      if av_result else None),
        )

        return AssemblerResult(
            dataset=dataset,
            manifest=manifest,
            split=split,
            coverage_report=coverage,
            adversarial_validation=av_result,
        )
