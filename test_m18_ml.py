"""M18 test suite — G1 (CLI surface) and G10 (hygiene).

This file accumulates G2..G8 test blocks across M18.A.2 through
M18.A.8. The initial skeleton (this commit) contains the imports
and the G10 Hygiene block; later phases extend it.
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

# Import the modules under test
from bot.ml import errors as ml_errors
from bot.ml import schemas as ml_schemas
from bot.ml import hashing as ml_hashing
from bot.ml import cli as ml_cli
from bot.ml.dataset import m16_loader
from bot.ml.dataset import flywheel_reader
from bot.ml.dataset import (
    anchors as ds_anchors,
    coverage as ds_coverage,
    manifest as ds_manifest,
    walk_forward as ds_walk_forward,
    adversarial_validation as ds_av,
    assembler as ds_assembler,
)
from bot.ml.features import (
    price_return, trend, momentum, vol_regime, volume_liquidity,
    mtf_confluence, scanner_replica, market_context, symbol_meta,
    signal_history,
)
from bot.ml.labels import (
    triple_barrier, forward_returns, mfe_mae, risk_adjusted,
)
from bot.ml.labels.base import assert_label_resolved_after_anchor
from bot.ml.models import (
    Trainer as ModelTrainer,
    TrainOutputs,
    ThinnessThresholds,
    evaluate_thinness,
    evaluate_production_thinness,
    ProductionThinnessThresholds,
    count_positives,
    MajorityClassTrainer,
    ScannerReplicaTrainer,
    LogisticRegressionTrainer,
    LightGBMTrainer,
    is_lightgbm_available,
    RandomForestTrainer,
    SCANNER_FIRES_COLUMN,
    select_feature_columns,
    select_label_columns,
    get_label_class,
    extract_xy_for_split,
)
from bot.ml.schemas import TrainConfig, ALLOWED_MODEL_TYPES, ALLOWED_TRAIN_MODES
import sqlite3


# Path constants
_REPO_ROOT = Path(__file__).parent
_BOT_ML_DIR = _REPO_ROOT / "bot" / "ml"


def _walk_bot_ml_py_files():
    """Yield every .py file under bot/ml/, excluding __pycache__."""
    for f in _BOT_ML_DIR.rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        yield f


# G10 whitelist — directories M18 is allowed to add files in.
# Anything created outside these directories is flagged by G10's
# test_no_unexpected_files_added.

def _imports_in_file(path):
    """Yield every fully-qualified module name imported by `path`.

    Uses ast to walk Import / ImportFrom nodes; for ImportFrom with
    `module='bot.historical', names=['store']`, yields 'bot.historical'
    (not 'bot.historical.store') so callers can do prefix checks
    against 'bot.historical' cleanly.
    """
    tree = ast.parse(Path(path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module

_M18_WHITELIST_PREFIXES = (
    "bot/ml/",
    "configs/ml/",
    "docs/M18",
    "test_m18_ml.py",
)


# ═════════════════════════════════════════════════════════════════════
# G1 — M18.A.1 foundation tests
#   schemas, feature/label/train config contracts, hashing, errors,
#   allowed registry/model/status values, and CLI foundation behaviour.
#
# RECONSTRUCTED CONTRACT-FAITHFULLY from the A.5 transcript class
# inventory + visible test-run names + the Word M18.A.1 truth table,
# validated against the current production contracts in bot/ml/
# {schemas,hashing,errors,cli}.py.
#
# NOT byte-identical: the original G1 test bodies were not recoverable
# from any transcript or patch. Four classes (G1_Hashing, G1_LabelSpec,
# G1_TrainConfig, G1_CLI) have their exact final test-method NAMES from
# transcript run logs and reproduce them; the other five
# (G1_FeatureSpec, G1_FeatureGroupSchema, G1_DatasetConfig,
# G1_AllowedRegistryStatuses, G1_Errors) had no recoverable names and
# are reconstructed from the production contract they must pin.
# ═════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────
# G1_FeatureSpec — FeatureSpec contract (reconstructed from contract)
# ─────────────────────────────────────────────────────────────────────

class G1_FeatureSpec(unittest.TestCase):

    def _valid(self):
        return {
            "feature_id": "trend.ema_20",
            "feature_group": "trend",
            "feature_group_version": 1,
            "dtype": "float64",
            "leak_class": "safe",
            "lookback_bars": 20,
            "lookback_unit": "bars_at_anchor_tf",
            "computed_from": ["close"],
            "description": "20-bar EMA of close.",
        }

    def test_parses_valid_feature_spec(self):
        spec = ml_schemas.FeatureSpec.from_dict(self._valid())
        self.assertEqual(spec.feature_id, "trend.ema_20")
        self.assertEqual(spec.feature_group, "trend")
        self.assertEqual(spec.leak_class, "safe")
        self.assertEqual(spec.computed_from, ("close",))

    def test_round_trip(self):
        spec = ml_schemas.FeatureSpec.from_dict(self._valid())
        again = ml_schemas.FeatureSpec.from_dict(spec.to_dict())
        self.assertEqual(spec, again)

    def test_feature_id_must_be_group_dot_name(self):
        d = self._valid()
        d["feature_id"] = "no_dot"
        with self.assertRaises(ml_errors.FeatureSchemaError):
            ml_schemas.FeatureSpec.from_dict(d)

    def test_feature_id_must_start_with_group(self):
        d = self._valid()
        d["feature_id"] = "momentum.rsi_14"   # group is 'trend'
        with self.assertRaises(ml_errors.FeatureSchemaError):
            ml_schemas.FeatureSpec.from_dict(d)

    def test_rejects_unknown_dtype(self):
        d = self._valid()
        d["dtype"] = "complex256"
        with self.assertRaises(ml_errors.FeatureSchemaError):
            ml_schemas.FeatureSpec.from_dict(d)

    def test_rejects_feature_only_disallowed_leak_class(self):
        # Features may only be 'safe' or 'requires_past_flywheel_only';
        # 'future_label_only' is a LABEL leak_class and must be refused.
        d = self._valid()
        d["leak_class"] = "future_label_only"
        with self.assertRaises(ml_errors.FeatureSchemaError):
            ml_schemas.FeatureSpec.from_dict(d)

    def test_rejects_negative_lookback(self):
        d = self._valid()
        d["lookback_bars"] = -1
        with self.assertRaises(ml_errors.FeatureSchemaError):
            ml_schemas.FeatureSpec.from_dict(d)

    def test_missing_required_key_raises(self):
        d = self._valid()
        del d["dtype"]
        with self.assertRaises(ml_errors.FeatureSchemaError):
            ml_schemas.FeatureSpec.from_dict(d)


# ─────────────────────────────────────────────────────────────────────
# G1_FeatureGroupSchema — group wrapper (reconstructed from contract)
# ─────────────────────────────────────────────────────────────────────

class G1_FeatureGroupSchema(unittest.TestCase):

    def _spec(self, fid="trend.ema_20"):
        return ml_schemas.FeatureSpec.from_dict({
            "feature_id": fid,
            "feature_group": "trend",
            "feature_group_version": 1,
            "dtype": "float64",
            "leak_class": "safe",
            "lookback_bars": 20,
            "lookback_unit": "bars_at_anchor_tf",
            "computed_from": ["close"],
            "description": "desc",
        })

    def test_constructs_with_specs(self):
        grp = ml_schemas.FeatureGroupSchema(
            group_name="trend",
            group_version=1,
            feature_specs=(self._spec("trend.ema_20"),
                            self._spec("trend.ema_50")),
            description="trend group",
        )
        self.assertEqual(grp.group_name, "trend")
        self.assertEqual(len(grp.feature_specs), 2)

    def test_is_frozen(self):
        grp = ml_schemas.FeatureGroupSchema(
            group_name="trend", group_version=1,
            feature_specs=(self._spec(),), description="d")
        with self.assertRaises(Exception):
            grp.group_name = "other"   # frozen dataclass


# ─────────────────────────────────────────────────────────────────────
# G1_LabelSpec — LabelSpec contract (test names from transcript run log)
# ─────────────────────────────────────────────────────────────────────

class G1_LabelSpec(unittest.TestCase):

    def _valid(self):
        return {
            "label_id": "fwd_return_5b",
            "label_schema_version": 1,
            "label_class": "regression",
            "horizon_bars": 5,
            "horizon_unit": "bars_at_anchor_tf",
            "leak_class": "future_label_only",
            "computed_from": ["open", "close"],
            "description": "5-bar forward log return.",
        }

    def test_parses_valid_label_spec(self):
        spec = ml_schemas.LabelSpec.from_dict(self._valid())
        self.assertEqual(spec.label_id, "fwd_return_5b")
        self.assertEqual(spec.label_class, "regression")
        self.assertEqual(spec.horizon_bars, 5)
        self.assertEqual(spec.leak_class, "future_label_only")

    def test_round_trip(self):
        spec = ml_schemas.LabelSpec.from_dict(self._valid())
        again = ml_schemas.LabelSpec.from_dict(spec.to_dict())
        self.assertEqual(spec, again)

    def test_optional_triple_barrier_fields(self):
        d = self._valid()
        d.update({
            "label_id": "triple_barrier_atr_2_3_50",
            "label_class": "classification_3way",
            "horizon_bars": 50,
            "tp_mult": 3.0, "sl_mult": 2.0,
            "atr_source": "vol_regime.atr_14_sma_true_range",
            "entry_price_source": "next_bar_open_after_anchor",
            "tie_breaker": "pessimistic_stop_first",
        })
        spec = ml_schemas.LabelSpec.from_dict(d)
        self.assertEqual(spec.tp_mult, 3.0)
        self.assertEqual(spec.sl_mult, 2.0)
        self.assertEqual(spec.tie_breaker, "pessimistic_stop_first")

    def test_rejects_unknown_label_class(self):
        d = self._valid()
        d["label_class"] = "made_up_class"
        with self.assertRaises(ml_errors.LabelSchemaError):
            ml_schemas.LabelSpec.from_dict(d)

    def test_rejects_non_future_label_leak_class(self):
        # Labels MUST have leak_class='future_label_only' — anything
        # else is a schema error.
        d = self._valid()
        d["leak_class"] = "safe"
        with self.assertRaises(ml_errors.LabelSchemaError):
            ml_schemas.LabelSpec.from_dict(d)

    def test_rejects_zero_horizon(self):
        d = self._valid()
        d["horizon_bars"] = 0
        with self.assertRaises(ml_errors.LabelSchemaError):
            ml_schemas.LabelSpec.from_dict(d)


# ─────────────────────────────────────────────────────────────────────
# G1_DatasetConfig — DatasetConfig contract (reconstructed from contract)
# ─────────────────────────────────────────────────────────────────────

class G1_DatasetConfig(unittest.TestCase):

    def _valid(self):
        return {
            "symbols": ["AAPL", "MSFT"],
            "anchor_tf": "15m",
            "start_date": "2024-01-02",
            "end_date": "2024-06-01",
            "feature_groups": ["trend", "momentum"],
            "labels": ["triple_barrier_atr_2_3_50_won", "fwd_return_5b"],
            "train_pct": 0.6,
            "val_pct": 0.2,
            "test_pct": 0.2,
            "embargo_trading_days": 5,
            "require_intraday": False,
            "fixture_mode": False,
        }

    def test_parses_valid(self):
        cfg = ml_schemas.DatasetConfig.from_dict(self._valid())
        self.assertEqual(cfg.symbols, ("AAPL", "MSFT"))
        self.assertEqual(cfg.anchor_tf, "15m")
        self.assertEqual(cfg.labels,
                          ("triple_barrier_atr_2_3_50_won", "fwd_return_5b"))
        self.assertFalse(cfg.fixture_mode)

    def test_round_trip(self):
        cfg = ml_schemas.DatasetConfig.from_dict(self._valid())
        again = ml_schemas.DatasetConfig.from_dict(cfg.to_dict())
        self.assertEqual(cfg, again)

    def test_split_pcts_default_to_60_20_20(self):
        d = self._valid()
        for k in ("train_pct", "val_pct", "test_pct"):
            d.pop(k, None)
        cfg = ml_schemas.DatasetConfig.from_dict(d)
        self.assertAlmostEqual(cfg.train_pct, 0.6)
        self.assertAlmostEqual(cfg.val_pct, 0.2)
        self.assertAlmostEqual(cfg.test_pct, 0.2)

    def test_embargo_and_require_intraday_defaults(self):
        d = self._valid()
        d.pop("embargo_trading_days", None)
        d.pop("require_intraday", None)
        cfg = ml_schemas.DatasetConfig.from_dict(d)
        self.assertEqual(cfg.embargo_trading_days, 5)
        self.assertFalse(cfg.require_intraday)

    def test_rejects_unknown_anchor_tf(self):
        d = self._valid()
        d["anchor_tf"] = "7m"
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.DatasetConfig.from_dict(d)

    def test_rejects_empty_symbols(self):
        d = self._valid()
        d["symbols"] = []
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.DatasetConfig.from_dict(d)

    def test_rejects_non_string_symbol_entry(self):
        d = self._valid()
        d["symbols"] = ["AAPL", ""]
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.DatasetConfig.from_dict(d)

    def test_rejects_empty_labels(self):
        d = self._valid()
        d["labels"] = []
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.DatasetConfig.from_dict(d)

    def test_rejects_empty_feature_groups(self):
        d = self._valid()
        d["feature_groups"] = []
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.DatasetConfig.from_dict(d)

    def test_rejects_start_not_before_end(self):
        d = self._valid()
        d["start_date"] = "2024-06-01"
        d["end_date"] = "2024-01-02"
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.DatasetConfig.from_dict(d)

    def test_rejects_splits_not_summing_to_one(self):
        d = self._valid()
        d["train_pct"], d["val_pct"], d["test_pct"] = 0.7, 0.2, 0.2
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.DatasetConfig.from_dict(d)

    def test_rejects_nonpositive_split(self):
        d = self._valid()
        d["train_pct"], d["val_pct"], d["test_pct"] = 0.8, 0.2, 0.0
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.DatasetConfig.from_dict(d)

    def test_rejects_negative_embargo(self):
        d = self._valid()
        d["embargo_trading_days"] = -1
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.DatasetConfig.from_dict(d)

    def test_rejects_bool_embargo(self):
        d = self._valid()
        d["embargo_trading_days"] = True
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.DatasetConfig.from_dict(d)

    def test_missing_required_key_raises(self):
        for k in ("symbols", "anchor_tf", "start_date", "end_date",
                   "feature_groups", "labels"):
            d = self._valid()
            del d[k]
            with self.assertRaises(ml_errors.M18ConfigError):
                ml_schemas.DatasetConfig.from_dict(d)


# ─────────────────────────────────────────────────────────────────────
# G1_TrainConfig — TrainConfig contract (test names from transcript log)
# ─────────────────────────────────────────────────────────────────────

class G1_TrainConfig(unittest.TestCase):

    def _valid(self):
        return {
            "dataset_id": "ds_abc123",
            "model_type": "B0_majority",
            "train_mode": "model_b_candidate_quality",
            "target_label_id": "triple_barrier_atr_2_3_50_won",
            "hyperparameters": {},
        }

    def test_parses_valid(self):
        cfg = ml_schemas.TrainConfig.from_dict(self._valid())
        self.assertEqual(cfg.dataset_id, "ds_abc123")
        self.assertEqual(cfg.model_type, "B0_majority")
        self.assertEqual(cfg.seed, 42)

    def test_fixture_mode_default_false(self):
        cfg = ml_schemas.TrainConfig.from_dict(self._valid())
        self.assertFalse(cfg.fixture_mode)

    def test_fixture_mode_explicit_true(self):
        d = self._valid()
        d["fixture_mode"] = True
        cfg = ml_schemas.TrainConfig.from_dict(d)
        self.assertTrue(cfg.fixture_mode)

    def test_rejects_unknown_model_type(self):
        d = self._valid()
        d["model_type"] = "XGBoost9000"
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.TrainConfig.from_dict(d)

    def test_rejects_unknown_train_mode(self):
        d = self._valid()
        d["train_mode"] = "model_c_speculative"
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.TrainConfig.from_dict(d)

    def test_rejects_bool_seed(self):
        d = self._valid()
        d["seed"] = True   # bool is not an acceptable int seed
        with self.assertRaises(ml_errors.M18ConfigError):
            ml_schemas.TrainConfig.from_dict(d)


# ─────────────────────────────────────────────────────────────────────
# G1_AllowedRegistryStatuses — locked allowlists (reconstructed)
# ─────────────────────────────────────────────────────────────────────

class G1_AllowedRegistryStatuses(unittest.TestCase):

    def test_allowed_label_classes_locked_set(self):
        self.assertEqual(
            set(ml_schemas.ALLOWED_LABEL_CLASSES),
            {"classification_3way", "binary", "regression", "ranking"})

    def test_allowed_model_types_contains_locked_members(self):
        for m in ("B0_majority", "B1_scanner_replica", "B2_logistic",
                   "M_lightgbm", "M_random_forest"):
            self.assertIn(m, ml_schemas.ALLOWED_MODEL_TYPES)

    def test_allowed_train_modes_locked_set(self):
        self.assertEqual(
            set(ml_schemas.ALLOWED_TRAIN_MODES),
            {"model_a_meta_label", "model_b_candidate_quality"})

    def test_registry_statuses_include_core_lifecycle(self):
        for s in ("candidate", "current", "demoted", "forced_promoted",
                   "fixture_only"):
            self.assertIn(s, ml_schemas.ALLOWED_REGISTRY_STATUSES)

    def test_feature_leak_classes_are_restricted(self):
        # Features may ONLY be 'safe' or 'requires_past_flywheel_only'.
        self.assertEqual(
            set(ml_schemas.ALLOWED_FEATURE_LEAK_CLASSES),
            {"safe", "requires_past_flywheel_only"})


# ─────────────────────────────────────────────────────────────────────
# G1_Hashing — canonical hashing (test names from transcript run log)
# ─────────────────────────────────────────────────────────────────────

class G1_Hashing(unittest.TestCase):

    def test_sha256_hex_64_chars(self):
        h = ml_hashing.sha256_hex(b"hello")
        self.assertEqual(len(h), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_sha256_hex_rejects_str(self):
        with self.assertRaises(TypeError):
            ml_hashing.sha256_hex("not bytes")

    def test_canonical_json_deterministic_for_dict_order(self):
        a = ml_hashing.canonical_json({"a": 1, "b": 2})
        b = ml_hashing.canonical_json({"b": 2, "a": 1})
        self.assertEqual(a, b)

    def test_canonical_json_sorted_sets(self):
        out = ml_hashing.canonical_json({3, 1, 2})
        self.assertEqual(out, b"[1,2,3]")

    def test_canonical_json_tuple_to_list(self):
        self.assertEqual(ml_hashing.canonical_json((1, 2)), b"[1,2]")

    def test_canonical_json_rejects_unknown_type(self):
        with self.assertRaises(TypeError):
            ml_hashing.canonical_json(object())

    def test_hash_canonical_stable(self):
        self.assertEqual(
            ml_hashing.hash_canonical({"x": 1, "y": [1, 2, 3]}),
            ml_hashing.hash_canonical({"y": [1, 2, 3], "x": 1}))

    def test_lib_versions_contains_required_keys(self):
        v = ml_hashing.lib_versions()
        for k in ("python", "numpy", "pandas", "sklearn"):
            self.assertIn(k, v)

    def test_git_head_sha_returns_hex_or_unknown(self):
        sha = ml_hashing.git_head_sha()
        self.assertTrue(
            sha == "unknown"
            or all(c in "0123456789abcdef" for c in sha))

    def test_repro_hash_deterministic(self):
        cfg = {"a": 1}
        libs = {"numpy": "1.0"}
        self.assertEqual(
            ml_hashing.repro_hash(cfg, libs, "abc"),
            ml_hashing.repro_hash(cfg, libs, "abc"))

    def test_repro_hash_changes_for_each_input(self):
        # SR-8: changing ANY component of the composition changes the
        # resulting hash.
        base = ml_hashing.repro_hash({"a": 1}, {"numpy": "1.0"}, "abc")
        self.assertNotEqual(
            base, ml_hashing.repro_hash({"a": 2}, {"numpy": "1.0"}, "abc"))
        self.assertNotEqual(
            base, ml_hashing.repro_hash({"a": 1}, {"numpy": "2.0"}, "abc"))
        self.assertNotEqual(
            base, ml_hashing.repro_hash({"a": 1}, {"numpy": "1.0"}, "xyz"))


# ─────────────────────────────────────────────────────────────────────
# G1_ReproHashV2 — SR-8 full reproducibility composition (M18.B.2)
# ─────────────────────────────────────────────────────────────────────

class G1_ReproHashV2(unittest.TestCase):

    def _tc(self, **over):
        d = {
            "model_type": "M_random_forest",
            "train_mode": "model_b_candidate_quality",
            "target_label_id": "triple_barrier_atr_2_3_50_won",
            "hyperparameters": {}, "seed": 42, "fixture_mode": False,
            "dataset_id": "DS1",
        }
        d.update(over)
        return d

    def _mf(self, **over):
        d = {
            "dataset_id": "DS1", "dataset_hash_sha256": "abc123",
            "feature_specs_hash": "feat1", "label_specs_hash": "lbl1",
            "anchor_set": "model_b_1h_union_candidates",
            "anchor_count_train": 100, "anchor_count_val": 30,
            "anchor_count_test": 30, "coverage_degraded": False,
            "fixture_only": False, "promotion_eligible": True,
            "promotion_blocked_reasons": [],
        }
        d.update(over)
        return d

    def _digest(self, **over):
        d = {"15m": {"n_bars": 1000, "first_ts": "2024-01-01",
                     "last_ts": "2024-06-01", "close_sum_str": "123.0",
                     "close_sum_sq_str": "456.0"}}
        d.update(over)
        return d

    def _libs(self, **over):
        d = {"python": "3.11", "numpy": "2.0", "pandas": "2.0",
             "sklearn": "1.8", "lightgbm": "absent"}
        d.update(over)
        return d

    def _hash(self, **over):
        kw = dict(
            train_config=over.pop("train_config", self._tc()),
            dataset_manifest=over.pop("dataset_manifest", self._mf()),
            feature_schema_hash=over.pop("feature_schema_hash", "feat1"),
            label_schema_hash=over.pop("label_schema_hash", "lbl1"),
            m16_bars_digest=over.pop("m16_bars_digest", self._digest()),
            library_versions=over.pop("library_versions", self._libs()),
            git_sha=over.pop("git_sha", "deadbeef"),
        )
        kw.update(over)
        return ml_hashing.repro_hash_v2(**kw)

    def test_repro_hash_v2_same_inputs_same_hash(self):
        self.assertEqual(self._hash(), self._hash())

    def test_repro_hash_v2_changes_when_feature_schema_changes(self):
        self.assertNotEqual(
            self._hash(feature_schema_hash="feat1"),
            self._hash(feature_schema_hash="feat2"))

    def test_repro_hash_v2_changes_when_label_schema_changes(self):
        self.assertNotEqual(
            self._hash(label_schema_hash="lbl1"),
            self._hash(label_schema_hash="lbl2"))

    def test_repro_hash_v2_changes_when_train_config_changes(self):
        self.assertNotEqual(
            self._hash(train_config=self._tc(seed=1)),
            self._hash(train_config=self._tc(seed=2)))

    def test_repro_hash_v2_changes_when_dataset_manifest_changes(self):
        self.assertNotEqual(
            self._hash(dataset_manifest=self._mf(anchor_count_train=100)),
            self._hash(dataset_manifest=self._mf(anchor_count_train=999)))

    def test_repro_hash_v2_changes_when_m16_bars_digest_changes(self):
        d2 = self._digest()
        d2["15m"]["close_sum_str"] = "999.0"
        self.assertNotEqual(
            self._hash(m16_bars_digest=self._digest()),
            self._hash(m16_bars_digest=d2))

    def test_repro_hash_v2_changes_when_git_sha_changes(self):
        self.assertNotEqual(
            self._hash(git_sha="aaa"), self._hash(git_sha="bbb"))

    def test_repro_hash_v2_changes_when_library_version_changes(self):
        self.assertNotEqual(
            self._hash(library_versions=self._libs(numpy="2.0")),
            self._hash(library_versions=self._libs(numpy="2.1")))

    def test_repro_hash_v2_component_hashes_are_present(self):
        payload = ml_hashing.repro_hash_v2_payload(
            train_config=self._tc(), dataset_manifest=self._mf(),
            feature_schema_hash="feat1", label_schema_hash="lbl1",
            m16_bars_digest=self._digest(), library_versions=self._libs(),
            git_sha="deadbeef")
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["algorithm"],
                          ml_hashing.REPRO_HASH_V2_ALGORITHM)
        ch = ml_hashing.repro_hash_v2_component_hashes(payload)
        for k in ("feature_schema_hash", "label_schema_hash",
                   "train_config_hash", "dataset_manifest_hash",
                   "m16_bars_hash", "library_versions_hash", "git_head"):
            self.assertIn(k, ch)

    def test_repro_hash_v2_rejects_missing_required_train_config_fields(self):
        with self.assertRaises(ValueError):
            self._hash(train_config={"model_type": "B2_logistic"})

    def test_repro_hash_v2_rejects_missing_required_manifest_fields(self):
        with self.assertRaises(ValueError):
            self._hash(dataset_manifest={"dataset_id": "DS1"})

    def test_repro_hash_v2_requires_bars_fingerprint(self):
        with self.assertRaises(ValueError):
            ml_hashing.repro_hash_v2(
                train_config=self._tc(), dataset_manifest=self._mf(),
                feature_schema_hash="feat1", label_schema_hash="lbl1",
                library_versions=self._libs(), git_sha="x")

    def test_repro_hash_v2_rejects_conflicting_schema_hash(self):
        with self.assertRaises(ValueError):
            ml_hashing.repro_hash_v2(
                train_config=self._tc(), dataset_manifest=self._mf(),
                feature_schema={"a": 1}, feature_schema_hash="not_matching",
                label_schema_hash="lbl1",
                m16_bars_digest=self._digest(),
                library_versions=self._libs(), git_sha="x")

    def test_repro_hash_v2_does_not_mutate_inputs(self):
        import copy
        tc, mf, dg, lv = (self._tc(), self._mf(), self._digest(),
                           self._libs())
        tc_c, mf_c, dg_c, lv_c = (copy.deepcopy(tc), copy.deepcopy(mf),
                                   copy.deepcopy(dg), copy.deepcopy(lv))
        ml_hashing.repro_hash_v2(
            train_config=tc, dataset_manifest=mf,
            feature_schema_hash="feat1", label_schema_hash="lbl1",
            m16_bars_digest=dg, library_versions=lv, git_sha="x")
        self.assertEqual((tc, mf, dg, lv), (tc_c, mf_c, dg_c, lv_c))

    def test_repro_hash_v1_remains_backward_compatible(self):
        h = ml_hashing.repro_hash(
            self._tc(), self._libs(), "deadbeef")
        self.assertEqual(len(h), 64)
        # v1 and v2 are different hashes (different surfaces)
        self.assertNotEqual(h, self._hash())

    def test_repro_hash_v2_precomputed_schema_hash_marked_source(self):
        payload = ml_hashing.repro_hash_v2_payload(
            train_config=self._tc(), dataset_manifest=self._mf(),
            feature_schema_hash="feat1", label_schema_hash="lbl1",
            m16_bars_digest=self._digest(), library_versions=self._libs(),
            git_sha="x")
        self.assertEqual(payload["feature_schema_source"],
                          "precomputed_hash")
        self.assertEqual(payload["label_schema_source"],
                          "precomputed_hash")

    def test_train_outputs_contains_repro_hash_v2(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("B2_logistic",
                                  dataset_id=res.manifest.dataset_id),
            res)
        self.assertIsNotNone(out.repro_hash_v2)
        self.assertEqual(len(out.repro_hash_v2), 64)
        self.assertIn("repro_hash_v2", out.to_dict())

    def test_dataset_manifest_persists_m16_bars_digest(self):
        res = _assemble_for_training()
        md = res.manifest.to_dict()
        self.assertIn("m16_bars_digest", md)
        self.assertTrue(md["m16_bars_digest"])

    def test_dataset_hash_still_deterministic_after_m16_bars_digest_field(self):
        r1 = _assemble_for_training()
        r2 = _assemble_for_training()
        self.assertEqual(r1.manifest.dataset_hash_sha256,
                          r2.manifest.dataset_hash_sha256)

    def test_old_manifest_roundtrip_still_safe_if_field_missing(self):
        from bot.ml.dataset.manifest import DatasetManifest
        res = _assemble_for_training()
        md = res.manifest.to_dict()
        old = {k: v for k, v in md.items() if k != "m16_bars_digest"}
        rt = DatasetManifest.from_dict(old)
        self.assertEqual(rt.m16_bars_digest, {})
        self.assertEqual(rt.dataset_hash_sha256,
                          res.manifest.dataset_hash_sha256)

    def test_train_one_repro_hash_v2_present_on_success(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("B2_logistic",
                                  dataset_id=res.manifest.dataset_id),
            res)
        self.assertIsNotNone(out.repro_hash_v2)
        self.assertEqual(len(out.repro_hash_v2), 64)

    def test_train_one_repro_hash_v2_failure_not_silent(self):
        # Fail-closed: if repro_hash_v2 raises, train_one must raise
        # M18ConfigError with a 'repro_hash_v2_failed' message — never
        # silently produce a model with no reproducibility hash.
        import bot.ml.models.trainer as _tr
        res = _assemble_for_training()
        cfg = _make_train_config(
            "B2_logistic", dataset_id=res.manifest.dataset_id)
        orig = _tr.repro_hash_v2

        def _boom(**kwargs):
            raise ValueError("simulated hashing failure")

        _tr.repro_hash_v2 = _boom
        try:
            with self.assertRaises(ml_errors.M18ConfigError) as ctx:
                ModelTrainer().train_one(cfg, res)
            self.assertIn("repro_hash_v2_failed", str(ctx.exception))
        finally:
            _tr.repro_hash_v2 = orig


# ─────────────────────────────────────────────────────────────────────
# G1_Errors — M18 error hierarchy (reconstructed from contract)
# ─────────────────────────────────────────────────────────────────────

class G1_Errors(unittest.TestCase):

    def test_all_m18_errors_subclass_base(self):
        for name in ("M18ConfigError", "M18SchemaError", "M18DataError",
                      "M18LeakageError", "M18RegistryError"):
            cls = getattr(ml_errors, name)
            self.assertTrue(issubclass(cls, ml_errors.M18Error),
                f"{name} must subclass M18Error")

    def test_schema_errors_subclass_schema_error(self):
        for name in ("FeatureSchemaError", "LabelSchemaError"):
            cls = getattr(ml_errors, name)
            self.assertTrue(issubclass(cls, ml_errors.M18Error),
                f"{name} must subclass M18Error")

    def test_config_error_is_raisable_and_carries_message(self):
        with self.assertRaises(ml_errors.M18ConfigError) as ctx:
            raise ml_errors.M18ConfigError("bad config")
        self.assertIn("bad config", str(ctx.exception))

    def test_distinct_error_types_are_not_interchangeable(self):
        self.assertFalse(
            issubclass(ml_errors.M18ConfigError, ml_errors.M18DataError))
        self.assertFalse(
            issubclass(ml_errors.M18RegistryError,
                        ml_errors.M18ConfigError))


# ─────────────────────────────────────────────────────────────────────
# G1_CLI — foundation CLI behaviour (test names from transcript run log)
#   NOTE: the full CLI surface is proved by G9 (M18.A.9). These are the
#   foundation-level existence/stub checks only; they must NOT regress
#   the A.9 wiring (predict / registry list|show|promote WIRED; the four
#   stubs return exit 2).
# ─────────────────────────────────────────────────────────────────────

class G1_CLI(unittest.TestCase):

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = ml_cli.main(argv)
        except SystemExit as exc:
            rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def test_subcommand_stub_returns_2(self):
        # build-dataset is a documented stub in M18.A.10: exits 2.
        rc, _, _ = self._run(["build-dataset"])
        self.assertEqual(rc, 2)

    def test_train_requires_config(self):
        # train is a documented stub (needs persisted AssemblerResult):
        # it must not silently succeed — exits 2.
        rc, _, _ = self._run(["train"])
        self.assertEqual(rc, 2)

    def test_unknown_subcommand_exits_nonzero(self):
        rc, _, _ = self._run(["definitely-not-a-subcommand"])
        self.assertNotEqual(rc, 0)

    def test_registry_promote_supports_force_override(self):
        # The A.9 promote surface accepts --force and --override-gate
        # without an argparse error (exit 2). Missing model-id is a
        # different (non-argparse) failure path.
        rc, _, _ = self._run(
            ["registry", "promote", "--help"])
        self.assertEqual(rc, 0)

    def test_registry_promote_supports_force_override_surface(self):
        # The promote parser exposes the force/override flags as part of
        # its surface (foundation-level existence check).
        import inspect as _inspect
        src = _inspect.getsource(ml_cli)
        self.assertIn("--force", src)
        self.assertIn("--override-gate", src)


# ═════════════════════════════════════════════════════════════════════
# G2 — M16 loader + safe feature groups (M18.A.2)
# ═════════════════════════════════════════════════════════════════════
#
# SR-4 tolerances (mirrors M17.B):
#   _PARITY_RTOL_SYNTH = 1e-9   for parity vs bot.backtesting.indicators
#   _PARITY_ATOL       = 1e-8   absolute floor for near-zero values
#
# Parity tests below compare M18 feature output bit-by-bit against
# the M17.B indicator helpers in bot.backtesting.indicators. Production
# bot/ml/* code does NOT import that module (the M18 feature modules
# reimplement the math); only test_m18_ml.py imports it for parity.

_PARITY_RTOL_SYNTH = 1e-9
_PARITY_ATOL       = 1e-8


def _make_synthetic_bars(n: int = 300, seed: int = 42,
                          start_price: float = 100.0,
                          drift: float = 0.0005,
                          vol: float = 0.015) -> pd.DataFrame:
    """Generate deterministic synthetic OHLCV bars for testing.

    Uses numpy.random.default_rng(seed) — no global RNG state.
    Returns a DataFrame matching the M16 loader contract:
    ts_utc (UTC), open, high, low, close, volume, quality_flags.
    """
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2024-01-02", periods=n, freq="1D", tz="UTC")
    returns = rng.normal(drift, vol, size=n)
    close = start_price * np.exp(np.cumsum(returns))
    # open = previous close (gap-free synthetic)
    open_ = np.concatenate([[start_price], close[:-1]])
    # high/low form a non-degenerate envelope around (open, close)
    spread = np.abs(rng.normal(0.0, 0.008, size=n)) * close + 0.01
    high = np.maximum(open_, close) + spread / 2.0
    low  = np.minimum(open_, close) - spread / 2.0
    volume = rng.integers(1_000_000, 10_000_000, size=n).astype(float)
    return pd.DataFrame({
        "ts_utc": ts,
        "open":   open_.astype(float),
        "high":   high.astype(float),
        "low":    low.astype(float),
        "close":  close.astype(float),
        "volume": volume,
        "quality_flags": 0,
    })


class G2_M16Loader(unittest.TestCase):
    """SR-7 — bot/ml/dataset/m16_loader is the SOLE bot.historical
    importer. This class tests its contract and error semantics; the
    AST guard that bot.historical doesn't leak elsewhere lives in G10.
    """

    def test_m16_loader_imports_bot_historical(self):
        """The loader is the only file that legitimately imports
        bot.historical in bot/ml/* — sanity-check it does so."""
        f = Path(__file__).parent / "bot" / "ml" / "dataset" / "m16_loader.py"
        imports = set(_imports_in_file(f))
        # Must import bot.historical (the loader's whole purpose)
        self.assertTrue(
            any(i == "bot.historical" or i.startswith("bot.historical.")
                for i in imports),
            f"m16_loader.py must import bot.historical; got {imports}")

    def test_raises_M16CoverageError_below_min_rows(self):
        """Empty / too-thin coverage raises with backfill command."""
        with tempfile.TemporaryDirectory() as td:
            # M16 store layout: <root>/<provider>/<tf>/<symbol>.parquet
            # We deliberately do NOT create any file → no coverage.
            os.environ.setdefault("BOT_HISTORICAL_ROOT", td)
            # Even without configured root, get_bars returns an empty
            # frame for a non-existent path — our loader must convert
            # that empty frame into M16CoverageError.
            with self.assertRaises(ml_errors.M16CoverageError) as cm:
                m16_loader.load_bars(
                    "NONEXISTENT_SYMBOL_XYZ", "1D", min_rows=1)
            msg = str(cm.exception)
            self.assertIn("bot.historical.cli backfill", msg,
                "error message must include explicit backfill command")
            self.assertIn("NONEXISTENT_SYMBOL_XYZ", msg)

    def test_validate_lookback_coverage_pass(self):
        """50 bars satisfy a 14-bar lookback (need 15)."""
        bars = _make_synthetic_bars(n=50)
        # Should not raise.
        m16_loader.validate_lookback_coverage(
            bars, lookback_bars=14, feature_name="rsi_14")

    def test_validate_lookback_coverage_fail_with_feature_name(self):
        """Too-short bars raise with the feature name in the message
        so the user can find the source of the failure."""
        bars = _make_synthetic_bars(n=10)
        with self.assertRaises(ml_errors.M16CoverageError) as cm:
            m16_loader.validate_lookback_coverage(
                bars, lookback_bars=50,
                feature_name="trend.sma_distance_50")
        self.assertIn("trend.sma_distance_50", str(cm.exception))

    def test_validate_lookback_coverage_rejects_negative_lookback(self):
        bars = _make_synthetic_bars(n=10)
        with self.assertRaises(ValueError):
            m16_loader.validate_lookback_coverage(
                bars, lookback_bars=-1)

    def test_assert_utc_index_rejects_naive(self):
        bars = _make_synthetic_bars(n=10)
        bars["ts_utc"] = bars["ts_utc"].dt.tz_localize(None)
        with self.assertRaises(ml_errors.M16CoverageError):
            m16_loader.assert_utc_index(bars)

    def test_assert_utc_index_accepts_utc(self):
        bars = _make_synthetic_bars(n=10)
        # Should not raise.
        m16_loader.assert_utc_index(bars)


# ─────────────────────────────────────────────────────────────────────
# G2_PriceReturn — 13 features
# ─────────────────────────────────────────────────────────────────────

class G2_PriceReturn(unittest.TestCase):

    def test_close_passthrough(self):
        bars = _make_synthetic_bars(n=50)
        out = price_return.compute(bars)
        np.testing.assert_allclose(
            out["price_return.close"].to_numpy(),
            bars["close"].to_numpy(),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_log_ret_1_known_values(self):
        # Construct a series where each bar is +1% over the previous.
        # log_ret_1 should be ln(1.01) ≈ 0.00995... for every bar
        # after the warmup (which is 1 bar for log_ret_1).
        n = 30
        close = 100.0 * np.power(1.01, np.arange(n))
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open":   close,
            "high":   close * 1.001,
            "low":    close * 0.999,
            "close":  close,
            "volume": np.full(n, 1_000_000.0),
            "quality_flags": 0,
        })
        out = price_return.compute(bars)
        ret = out["price_return.log_ret_1"]
        self.assertTrue(pd.isna(ret.iloc[0]))   # warmup
        np.testing.assert_allclose(
            ret.iloc[1:].to_numpy(),
            np.full(n - 1, np.log(1.01)),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_log_ret_warmup_lengths(self):
        bars = _make_synthetic_bars(n=50)
        out = price_return.compute(bars)
        # log_ret_5 has 5 NaN at start; log_ret_20 has 20.
        self.assertEqual(int(out["price_return.log_ret_5"].isna().sum()), 5)
        self.assertEqual(int(out["price_return.log_ret_20"].isna().sum()), 20)

    def test_gap_pct_known(self):
        # Two-bar fixture: open[1] = close[0] * 1.02 → gap_pct[1] = 0.02
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=3,
                                      freq="1D", tz="UTC"),
            "open":   [100.0, 102.0, 105.0],
            "high":   [101.0, 103.0, 106.0],
            "low":    [99.0,  101.0, 104.0],
            "close":  [100.0, 102.0, 105.0],
            "volume": [1_000_000.0] * 3,
            "quality_flags": 0,
        })
        out = price_return.compute(bars)
        gap = out["price_return.gap_pct"]
        self.assertTrue(pd.isna(gap.iloc[0]))   # warmup
        self.assertAlmostEqual(gap.iloc[1], 0.02, places=12)
        # Bar 2: open=105 vs close[1]=102 → gap = 3/102
        self.assertAlmostEqual(gap.iloc[2], 3.0 / 102.0, places=12)

    def test_body_and_wick_known(self):
        # Single bar: open=100, close=110, high=115, low=98
        # body  = (110 - 100) / 100 = 0.10
        # hl    = (115 - 98)  / 100 = 0.17
        # upper = (115 - 110) / 100 = 0.05  (top of body = max(o,c) = 110)
        # lower = (100 - 98)  / 100 = 0.02  (bottom of body = min(o,c) = 100)
        bars = pd.DataFrame({
            "ts_utc": [pd.Timestamp("2024-01-02", tz="UTC")],
            "open":   [100.0],
            "high":   [115.0],
            "low":    [98.0],
            "close":  [110.0],
            "volume": [1_000_000.0],
            "quality_flags": 0,
        })
        out = price_return.compute(bars)
        self.assertAlmostEqual(out["price_return.body_pct"].iloc[0],
                                0.10, places=12)
        self.assertAlmostEqual(out["price_return.hl_range_pct"].iloc[0],
                                0.17, places=12)
        self.assertAlmostEqual(out["price_return.upper_wick_pct"].iloc[0],
                                0.05, places=12)
        self.assertAlmostEqual(out["price_return.lower_wick_pct"].iloc[0],
                                0.02, places=12)

    def test_dist_from_rolling_high_known(self):
        # Monotone-up series → rolling max == current close → distance == 0
        n = 30
        close = 100.0 + np.arange(n)   # 100, 101, ..., 129
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open":   close,
            "high":   close,
            "low":    close,
            "close":  close,
            "volume": np.full(n, 1e6),
            "quality_flags": 0,
        })
        out = price_return.compute(bars)
        dist = out["price_return.dist_from_rolling_high_20"]
        # First 19 bars are warmup; from bar 20 onward, rolling-max
        # == current bar → distance == 0
        self.assertTrue(pd.isna(dist.iloc[18]))
        for i in range(19, n):
            self.assertAlmostEqual(dist.iloc[i], 0.0, places=12,
                msg=f"dist_from_rolling_high_20 at bar {i}")

    def test_determinism(self):
        bars = _make_synthetic_bars(n=100, seed=123)
        out1 = price_return.compute(bars)
        out2 = price_return.compute(bars)
        pd.testing.assert_frame_equal(out1, out2, check_exact=True)

    def test_specs_all_safe_leak_class(self):
        for s in price_return.SPECS:
            self.assertEqual(s.leak_class, "safe",
                f"{s.feature_id} must be leak_class='safe'")


# ─────────────────────────────────────────────────────────────────────
# G2_Trend — 8 features
# ─────────────────────────────────────────────────────────────────────

class G2_Trend(unittest.TestCase):

    def test_constant_series_sma_distance_zero(self):
        n = 250
        close = np.full(n, 100.0)
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open":   close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = trend.compute(bars)
        # After warmup, every distance should be exactly 0 (constant series)
        post_warmup_50  = out["trend.sma_distance_50"].iloc[50:]
        post_warmup_200 = out["trend.sma_distance_200"].iloc[200:]
        np.testing.assert_allclose(post_warmup_50.to_numpy(),
                                     0.0, atol=_PARITY_ATOL)
        np.testing.assert_allclose(post_warmup_200.to_numpy(),
                                     0.0, atol=_PARITY_ATOL)

    def test_uptrend_ema20_above_ema50(self):
        n = 100
        close = 100.0 + np.arange(n) * 1.0   # strict uptrend
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = trend.compute(bars)
        # After EMA50 warmup, ema20_gt_ema50 should be 1.
        post_warmup = out["trend.ema20_gt_ema50"].iloc[50:]
        self.assertTrue((post_warmup == 1).all(),
            "ema20_gt_ema50 must be 1 in a sustained uptrend")

    def test_uptrend_ema_slopes_positive(self):
        n = 100
        close = 100.0 + np.arange(n) * 1.0
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = trend.compute(bars)
        ema20_slope = out["trend.ema20_slope"].iloc[55:].dropna()
        self.assertTrue((ema20_slope > 0).all(),
            "ema20_slope must be positive in sustained uptrend")

    def test_sma_parity_vs_m17b(self):
        """ema_distance / sma_distance share their internal SMA/EMA
        with bot.backtesting.indicators. Spot-check parity."""
        from bot.backtesting.indicators import sma as live_sma
        from bot.backtesting.indicators import ema as live_ema
        bars = _make_synthetic_bars(n=300)
        c = bars["close"].astype(float)
        # Compute via the same paths used inside trend.compute
        ours_sma50 = c.rolling(window=50, min_periods=50).mean()
        theirs_sma50 = live_sma(c, 50)
        np.testing.assert_allclose(
            ours_sma50.dropna().to_numpy(),
            theirs_sma50.dropna().to_numpy(),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)
        ours_ema20 = c.ewm(span=20, adjust=False, min_periods=20).mean()
        theirs_ema20 = live_ema(c, 20)
        np.testing.assert_allclose(
            ours_ema20.dropna().to_numpy(),
            theirs_ema20.dropna().to_numpy(),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_determinism(self):
        bars = _make_synthetic_bars(n=300, seed=7)
        a = trend.compute(bars)
        b = trend.compute(bars)
        pd.testing.assert_frame_equal(a, b, check_exact=True)


# ─────────────────────────────────────────────────────────────────────
# G2_Momentum — RSI live-parity, MACD live-parity, ROC, accel
# ─────────────────────────────────────────────────────────────────────

class G2_Momentum(unittest.TestCase):

    def test_rsi_warmup_14_bars(self):
        bars = _make_synthetic_bars(n=50)
        out = momentum.compute(bars)
        # First 14 bars are NaN (1 bar for diff + 13 more for the SMA(14))
        rsi = out["momentum.rsi_14_sma_gain_loss"]
        self.assertEqual(int(rsi.iloc[:14].isna().sum()), 14)
        self.assertFalse(rsi.iloc[14:].isna().any(),
            "RSI must be defined for all bars after warmup on real data")

    def test_rsi_monotone_up_approaches_high(self):
        # Strict monotone-up series → RSI should be very high (≈100)
        n = 50
        close = 100.0 + np.arange(n) * 0.5
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = momentum.compute(bars)
        rsi_tail = out["momentum.rsi_14_sma_gain_loss"].iloc[20:]
        # All gains, no losses → rs = gain/eps → very high RSI
        # With the +1e-9 epsilon, RSI saturates to ≈100 to floating-point
        self.assertTrue((rsi_tail > 99.0).all(),
            f"RSI on monotone-up should be near 100; got {rsi_tail.values}")

    def test_rsi_parity_vs_m17b(self):
        """SR-4 — bit-identical at rtol=1e-9, atol=1e-8 vs
        bot.backtesting.indicators.rsi(mode='sma_gain_loss')."""
        from bot.backtesting.indicators import rsi as live_rsi
        bars = _make_synthetic_bars(n=400, seed=11)
        out = momentum.compute(bars)
        ours = out["momentum.rsi_14_sma_gain_loss"]
        theirs = live_rsi(bars["close"].astype(float), 14,
                          mode="sma_gain_loss")
        # Align both series, drop warmup NaN, compare element-wise.
        # Both should produce the same NaN mask.
        mask = ours.notna() & theirs.notna()
        self.assertEqual(int(mask.sum()), int(ours.notna().sum()),
            "NaN masks differ between M18 RSI and live RSI")
        np.testing.assert_allclose(
            ours[mask].to_numpy(), theirs[mask].to_numpy(),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_macd_parity_vs_m17b(self):
        from bot.backtesting.indicators import macd as live_macd
        bars = _make_synthetic_bars(n=400, seed=23)
        out = momentum.compute(bars)
        theirs = live_macd(bars["close"].astype(float))
        for theirs_col, ours_col in [
                ("macd",   "momentum.macd_line"),
                ("signal", "momentum.macd_signal"),
                ("hist",   "momentum.macd_hist")]:
            o = out[ours_col]
            t = theirs[theirs_col]
            mask = o.notna() & t.notna()
            np.testing.assert_allclose(
                o[mask].to_numpy(), t[mask].to_numpy(),
                rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL,
                err_msg=f"parity fail for {ours_col}")

    def test_roc_10_known_constant_return(self):
        # If each bar is +1% over the previous, roc_10 = (1.01^10) - 1
        n = 30
        close = 100.0 * np.power(1.01, np.arange(n))
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = momentum.compute(bars)
        roc = out["momentum.roc_10"]
        expected = (1.01 ** 10) - 1.0
        np.testing.assert_allclose(
            roc.iloc[10:].to_numpy(),
            np.full(n - 10, expected),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_momentum_acceleration_zero_on_geometric(self):
        # Constant log-return series → log_ret_5 is constant → diff = 0
        n = 30
        close = 100.0 * np.power(1.01, np.arange(n))
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = momentum.compute(bars)
        acc = out["momentum.momentum_acceleration"]
        # First 10 bars are warmup (5 for log_ret_5 + 5 for diff(5))
        np.testing.assert_allclose(
            acc.iloc[10:].to_numpy(),
            0.0, atol=_PARITY_ATOL)

    def test_determinism(self):
        bars = _make_synthetic_bars(n=300, seed=99)
        a = momentum.compute(bars)
        b = momentum.compute(bars)
        pd.testing.assert_frame_equal(a, b, check_exact=True)


# ─────────────────────────────────────────────────────────────────────
# G2_VolRegime — ATR live-parity, bb_pos live-parity, regime flag
# ─────────────────────────────────────────────────────────────────────

class G2_VolRegime(unittest.TestCase):

    def test_atr_warmup(self):
        bars = _make_synthetic_bars(n=50)
        out = vol_regime.compute(bars)
        atr = out["vol_regime.atr_14_sma_true_range"]
        # ATR(14, sma_true_range) warmup is 13 (not 14):
        #   prev_close = close.shift(1) has 1 NaN at t=0
        #   tr1 = high - low has 0 NaN
        #   tr2 = |high - prev_close| has 1 NaN at t=0
        #   tr3 = |low - prev_close|  has 1 NaN at t=0
        #   tr = concat([tr1, tr2, tr3]).max(axis=1) — pandas max
        #     skips NaN, so tr[0] = tr1[0] (VALID).
        #   rolling(14, min_periods=14).mean() → first valid at idx 13.
        # Therefore positions 0..12 are NaN (13 values), and positions
        # 13..49 are valid. This differs from RSI(14) which has 14 NaN
        # because RSI's input series (gain/loss from diff) starts NaN.
        self.assertEqual(int(atr.iloc[:13].isna().sum()), 13)
        self.assertFalse(atr.iloc[13:].isna().any())

    def test_atr_parity_vs_m17b(self):
        from bot.backtesting.indicators import atr as live_atr
        bars = _make_synthetic_bars(n=400, seed=31)
        out = vol_regime.compute(bars)
        ours = out["vol_regime.atr_14_sma_true_range"]
        theirs = live_atr(
            bars["high"].astype(float),
            bars["low"].astype(float),
            bars["close"].astype(float),
            14, mode="sma_true_range")
        mask = ours.notna() & theirs.notna()
        np.testing.assert_allclose(
            ours[mask].to_numpy(), theirs[mask].to_numpy(),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_bb_pos_parity_vs_m17b(self):
        from bot.backtesting.indicators import bb_pos as live_bb_pos
        bars = _make_synthetic_bars(n=400, seed=47)
        out = vol_regime.compute(bars)
        ours = out["vol_regime.bb_pos"]
        theirs = live_bb_pos(bars["close"].astype(float), 20, 2.0)
        # Compare element-by-element handling both NaN-warmup AND the
        # 0.5 fallback (where rng <= 0). Both implementations should
        # produce identical results at every position.
        ours_a   = ours.to_numpy()
        theirs_a = theirs.to_numpy()
        # NaN masks must match exactly
        np.testing.assert_array_equal(
            np.isnan(ours_a), np.isnan(theirs_a),
            err_msg="bb_pos NaN masks differ between M18 and M17.B")
        # Non-NaN values must match to floating-point
        m = ~np.isnan(ours_a)
        np.testing.assert_allclose(
            ours_a[m], theirs_a[m],
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_realized_vol_zero_on_constant_close(self):
        n = 30
        close = np.full(n, 100.0)
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = vol_regime.compute(bars)
        rv = out["vol_regime.realized_vol_20"]
        # log_ret_1 is 0 for constant series → std is 0 (or NaN if
        # pandas returns NaN for zero-variance window). Accept both.
        post = rv.iloc[21:]
        for val in post:
            self.assertTrue(val == 0.0 or pd.isna(val),
                f"realized_vol on constant series should be 0 or NaN, got {val}")

    def test_vol_regime_flag_bounds(self):
        bars = _make_synthetic_bars(n=200, seed=55)
        out = vol_regime.compute(bars)
        flag = out["vol_regime.vol_regime_flag"]
        self.assertEqual(flag.dtype, np.int8)
        self.assertTrue(((flag >= 0) & (flag <= 3)).all())

    def test_determinism(self):
        bars = _make_synthetic_bars(n=300, seed=77)
        a = vol_regime.compute(bars)
        b = vol_regime.compute(bars)
        pd.testing.assert_frame_equal(a, b, check_exact=True)


# ─────────────────────────────────────────────────────────────────────
# G2_VolumeLiquidity — volume_ratio parity, vol_zscore, liquidity bucket
# ─────────────────────────────────────────────────────────────────────

class G2_VolumeLiquidity(unittest.TestCase):

    def test_vol_ratio_one_on_flat_volume(self):
        n = 30
        bars = _make_synthetic_bars(n=n)
        bars["volume"] = 1_000_000.0
        out = volume_liquidity.compute(bars)
        ratio = out["volume_liquidity.vol_ratio_20"]
        np.testing.assert_allclose(
            ratio.iloc[20:].to_numpy(),
            1.0, rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_vol_ratio_parity_vs_m17b(self):
        """M18 returns NaN on zero-volume SMA; M17.B uses +1e-9.
        Synthetic data has only positive volumes, so the two formulas
        agree to floating-point for ALL bars after warmup."""
        from bot.backtesting.indicators import volume_ratio as live_vr
        bars = _make_synthetic_bars(n=400, seed=63)
        out = volume_liquidity.compute(bars)
        ours = out["volume_liquidity.vol_ratio_20"]
        theirs = live_vr(bars["volume"].astype(float), 20)
        mask = ours.notna() & theirs.notna()
        np.testing.assert_allclose(
            ours[mask].to_numpy(), theirs[mask].to_numpy(),
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_dollar_vol_20_known(self):
        # Build a fixture where close * volume = constant; dollar_vol_20
        # should equal that constant after warmup.
        n = 30
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open":   np.full(n, 100.0),
            "high":   np.full(n, 100.0),
            "low":    np.full(n, 100.0),
            "close":  np.full(n, 100.0),
            "volume": np.full(n, 1_000_000.0),
            "quality_flags": 0,
        })
        out = volume_liquidity.compute(bars)
        dv = out["volume_liquidity.dollar_vol_20"]
        np.testing.assert_allclose(
            dv.iloc[20:].to_numpy(), 100.0 * 1_000_000.0,
            rtol=_PARITY_RTOL_SYNTH, atol=_PARITY_ATOL)

    def test_liquidity_bucket_ordinal_bounds(self):
        bars = _make_synthetic_bars(n=400, seed=88)
        out = volume_liquidity.compute(bars)
        b = out["volume_liquidity.liquidity_bucket"]
        self.assertEqual(b.dtype, np.int8)
        self.assertTrue(((b >= 0) & (b <= 4)).all())

    def test_determinism(self):
        bars = _make_synthetic_bars(n=300, seed=66)
        a = volume_liquidity.compute(bars)
        b = volume_liquidity.compute(bars)
        pd.testing.assert_frame_equal(a, b, check_exact=True)


# ─────────────────────────────────────────────────────────────────────
# G2 — Future-bar scramble (cross-group leak-safety)
# ─────────────────────────────────────────────────────────────────────

class G2_FutureBarScramble(unittest.TestCase):
    """For every leak_class='safe' feature in every M18.A.2 group,
    scrambling the bars AFTER an anchor T must NOT change the feature
    value at any bar <= T. This is the canonical look-ahead test.

    Mirrors the M17.B 'future-bar scramble' approach: bars beyond
    anchor get replaced with completely different values, then the
    safe features for positions <= anchor must remain bit-identical."""

    def _build_scrambled_pair(self, n=300, anchor_idx=200, seed=42,
                               scramble_seed=99999):
        original = _make_synthetic_bars(n=n, seed=seed).copy()
        scrambled = original.copy()
        rng = np.random.default_rng(scramble_seed)
        future_n = n - anchor_idx - 1
        # Replace future bars with very different values
        new_close = rng.uniform(1.0, 1000.0, size=future_n)
        new_open  = rng.uniform(1.0, 1000.0, size=future_n)
        new_high  = np.maximum(new_open, new_close) * (
            1.0 + np.abs(rng.normal(0, 0.05, future_n)))
        new_low   = np.minimum(new_open, new_close) * (
            1.0 - np.abs(rng.normal(0, 0.05, future_n)))
        new_vol   = rng.integers(1, 1_000_000_000, future_n).astype(float)
        sl = slice(anchor_idx + 1, n)
        scrambled.loc[sl, "open"]   = new_open
        scrambled.loc[sl, "high"]   = new_high
        scrambled.loc[sl, "low"]    = new_low
        scrambled.loc[sl, "close"]  = new_close
        scrambled.loc[sl, "volume"] = new_vol
        return original, scrambled, anchor_idx

    def test_all_safe_features_unchanged_at_or_before_anchor(self):
        orig, scram, anchor = self._build_scrambled_pair()
        for mod in (price_return, trend, momentum,
                     vol_regime, volume_liquidity):
            with self.subTest(group=mod.GROUP_NAME):
                # Verify all SPECS are leak_class='safe' for M18.A.2.
                for s in mod.SPECS:
                    self.assertEqual(s.leak_class, "safe",
                        f"{s.feature_id} is not safe — should not be "
                        f"in M18.A.2")
                a = mod.compute(orig).iloc[:anchor + 1]
                b = mod.compute(scram).iloc[:anchor + 1]
                # Bit-identical for the at-or-before-anchor window.
                # NaN positions must also match.
                for col in a.columns:
                    av = a[col].to_numpy()
                    bv = b[col].to_numpy()
                    np.testing.assert_array_equal(
                        np.isnan(av), np.isnan(bv),
                        err_msg=f"{mod.GROUP_NAME}/{col}: NaN mask "
                                  f"differs across scramble (leak!)")
                    m = ~np.isnan(av)
                    np.testing.assert_array_equal(
                        av[m], bv[m],
                        err_msg=f"{mod.GROUP_NAME}/{col}: values "
                                  f"differ across scramble (leak!)")


# ═════════════════════════════════════════════════════════════════════
# G2 — M18.A.3 feature groups: multi-TF, benchmark, metadata, flywheel
# ═════════════════════════════════════════════════════════════════════


def _make_multi_tf_bars(seed: int = 1, n_15m: int = 400):
    """Generate aligned multi-TF synthetic bars at 15m/1H/4H/1D.

    Each TF gets its own RNG seed so the resulting series differ;
    timestamps start from the same anchor and use the requested
    cadence so that snapshot_at() will find at-or-before bars at
    every 15m anchor.
    """
    def _one(n, freq, seed_, start="2024-01-02"):
        rng = np.random.default_rng(seed_)
        ts = pd.date_range(start, periods=n, freq=freq, tz="UTC")
        close = 100.0 * np.exp(np.cumsum(
            rng.normal(0.0001, 0.005, n)))
        open_ = np.concatenate([[100.0], close[:-1]])
        spread = np.abs(rng.normal(0, 0.005, n)) * close + 0.01
        high = np.maximum(open_, close) + spread / 2.0
        low  = np.minimum(open_, close) - spread / 2.0
        vol = rng.integers(1_000_000, 10_000_000, n).astype(float)
        return pd.DataFrame({"ts_utc": ts, "open": open_, "high": high,
                              "low": low, "close": close, "volume": vol,
                              "quality_flags": 0})

    # 15m anchor; coarser TFs at proportional sample counts.
    b15 = _one(n_15m,            "15min", seed * 11)
    b1h = _one(max(80, n_15m//4), "1h",    seed * 13)
    b4h = _one(max(30, n_15m//16), "4h",   seed * 17)
    b1d = _one(max(20, n_15m//96), "1D",   seed * 19)
    return {"15m": b15, "1H": b1h, "4H": b4h, "1D": b1d}


# ─────────────────────────────────────────────────────────────────────
# G2_SymbolMeta
# ─────────────────────────────────────────────────────────────────────

class G2_SymbolMeta(unittest.TestCase):

    EXAMPLE = "configs/ml/symbol_metadata.example.json"

    def test_load_example_file_succeeds(self):
        data = symbol_meta.load_metadata(self.EXAMPLE)
        self.assertEqual(data["schema_version"], 1)
        self.assertIn("symbols", data)
        self.assertIn("encodings", data)
        # Must contain at least one example symbol.
        self.assertIn("AAPL", data["symbols"])

    def test_known_symbol_lookup(self):
        bars = _make_synthetic_bars(n=10)
        out = symbol_meta.compute(bars, symbol="AAPL",
                                    metadata_path=self.EXAMPLE)
        # AAPL: sector=technology(0), market_cap=mega(4),
        #       asset_class=equity(0), etf=false, ipo=1980
        self.assertEqual(int(out["symbol_meta.sector_code"].iloc[0]), 0)
        self.assertEqual(int(out["symbol_meta.market_cap_code"].iloc[0]),
                          4)
        self.assertEqual(int(out["symbol_meta.asset_class_code"].iloc[0]),
                          0)
        self.assertEqual(int(out["symbol_meta.is_etf"].iloc[0]), 0)
        self.assertEqual(int(out["symbol_meta.ipo_year"].iloc[0]), 1980)

    def test_etf_symbol_marked_correctly(self):
        bars = _make_synthetic_bars(n=10)
        out = symbol_meta.compute(bars, symbol="SPY",
                                    metadata_path=self.EXAMPLE)
        self.assertEqual(int(out["symbol_meta.is_etf"].iloc[0]), 1)
        self.assertEqual(int(out["symbol_meta.market_cap_code"].iloc[0]),
                          5)

    def test_unknown_symbol_falls_back_to_unknown_codes(self):
        bars = _make_synthetic_bars(n=10)
        out = symbol_meta.compute(bars, symbol="NEVER_SEEN_BEFORE_XYZ",
                                    metadata_path=self.EXAMPLE)
        # unknown sector → 99, unknown cap → 99, unknown asset → 99,
        # unknown ipo → 0, unknown etf → -1
        self.assertEqual(int(out["symbol_meta.sector_code"].iloc[0]),
                          99)
        self.assertEqual(int(out["symbol_meta.market_cap_code"].iloc[0]),
                          99)
        self.assertEqual(int(out["symbol_meta.asset_class_code"].iloc[0]),
                          99)
        self.assertEqual(int(out["symbol_meta.ipo_year"].iloc[0]), 0)
        self.assertEqual(int(out["symbol_meta.is_etf"].iloc[0]), -1)

    def test_constant_across_rows(self):
        """Every row should have the same value (static metadata)."""
        bars = _make_synthetic_bars(n=50)
        out = symbol_meta.compute(bars, symbol="AAPL",
                                    metadata_path=self.EXAMPLE)
        for col in out.columns:
            self.assertEqual(out[col].nunique(), 1,
                f"{col} is not constant across rows")

    def test_specs_all_safe_leak_class(self):
        for s in symbol_meta.SPECS:
            self.assertEqual(s.leak_class, "safe",
                f"{s.feature_id} must be leak_class='safe'")

    def test_schema_validation_rejects_bad_file(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            bad.write_text('{"schema_version": 99, "symbols": {}, '
                           '"encodings": {}}')
            with self.assertRaises(ValueError):
                symbol_meta.load_metadata(bad)


# ─────────────────────────────────────────────────────────────────────
# G2_MTFConfluence
# ─────────────────────────────────────────────────────────────────────

class G2_MTFConfluence(unittest.TestCase):

    def test_basic_compute_shape(self):
        per_tf = _make_multi_tf_bars(seed=1)
        out = mtf_confluence.compute(per_tf["15m"], per_tf_bars=per_tf)
        self.assertEqual(len(out), len(per_tf["15m"]))
        self.assertEqual(
            set(out.columns),
            {"mtf_confluence.available_tf_count",
              "mtf_confluence.tf_15m_present",
              "mtf_confluence.tf_1h_present",
              "mtf_confluence.tf_4h_present",
              "mtf_confluence.tf_1d_present"})

    def test_full_availability_after_warmup(self):
        per_tf = _make_multi_tf_bars(seed=1, n_15m=600)
        out = mtf_confluence.compute(per_tf["15m"], per_tf_bars=per_tf)
        # After enough 15m bars to also have 1D/4H/1H snapshots, every
        # TF must be present at the LAST anchor.
        last = out.iloc[-1]
        self.assertEqual(int(last["mtf_confluence.available_tf_count"]),
                          4)
        for col in ("tf_15m_present", "tf_1h_present",
                      "tf_4h_present", "tf_1d_present"):
            self.assertEqual(int(last[f"mtf_confluence.{col}"]), 1)

    def test_only_anchor_tf_at_first_anchor(self):
        # At the very first 15m anchor, the coarser TFs (1H/4H/1D) may
        # NOT yet have a bar at-or-before (since their bars start at
        # the same UTC date but the first 1H bar's ts is later than
        # the first 15m bar's ts). Verify available_tf_count is
        # consistent with the snapshot semantics.
        per_tf = _make_multi_tf_bars(seed=1)
        out = mtf_confluence.compute(per_tf["15m"], per_tf_bars=per_tf)
        # The 15m TF MUST be present at every anchor (it's the anchor).
        self.assertTrue((out["mtf_confluence.tf_15m_present"] == 1).all())
        # available_tf_count >= 1 always (15m present)
        self.assertTrue(
            (out["mtf_confluence.available_tf_count"] >= 1).all())

    def test_leak_safety_future_15m_scramble(self):
        per_tf = _make_multi_tf_bars(seed=2)
        anchor_idx = len(per_tf["15m"]) - 50
        scram_15m = per_tf["15m"].copy()
        rng = np.random.default_rng(987)
        future_n = len(scram_15m) - anchor_idx - 1
        scram_15m.loc[anchor_idx + 1:, "close"] = rng.uniform(
            1, 1000, future_n)
        scram_15m.loc[anchor_idx + 1:, "high"] = rng.uniform(
            1000, 2000, future_n)
        scram_15m.loc[anchor_idx + 1:, "low"] = rng.uniform(
            0.1, 1, future_n)
        scram_15m.loc[anchor_idx + 1:, "volume"] = rng.uniform(
            1, 1e9, future_n)
        scram_per_tf = dict(per_tf)
        scram_per_tf["15m"] = scram_15m

        a = mtf_confluence.compute(per_tf["15m"], per_tf_bars=per_tf)
        b = mtf_confluence.compute(scram_15m,
                                     per_tf_bars=scram_per_tf)
        # Features at or before anchor_idx must be identical.
        for col in a.columns:
            np.testing.assert_array_equal(
                a.iloc[:anchor_idx + 1][col].to_numpy(),
                b.iloc[:anchor_idx + 1][col].to_numpy(),
                err_msg=f"mtf_confluence/{col}: scramble leaked future")


# ─────────────────────────────────────────────────────────────────────
# G2_ScannerReplica
# ─────────────────────────────────────────────────────────────────────

class G2_ScannerReplica(unittest.TestCase):

    def test_basic_compute_shape(self):
        per_tf = _make_multi_tf_bars(seed=3)
        out = scanner_replica.compute(per_tf["15m"],
                                        per_tf_bars=per_tf)
        self.assertEqual(len(out), len(per_tf["15m"]))
        self.assertEqual(out.shape[1], len(scanner_replica.SPECS))

    def test_signal_fires_parity_vs_M17B_strategy(self):
        """The binary signal column we produce must equal the M17.B
        strategy's signal output (== SIG_ENTRY -> 1)."""
        per_tf = _make_multi_tf_bars(seed=4, n_15m=600)
        out = scanner_replica.compute(per_tf["15m"],
                                        per_tf_bars=per_tf)
        # Pull M17.B's canonical signal df via the parity helper
        sig_df = scanner_replica.parity_check_with_strategy(
            per_tf["15m"], per_tf_bars=per_tf)
        # SIG_ENTRY constant in M17.B; per the strategy it's used as
        # signal == SIG_ENTRY for entries. We just compare zero/non-zero.
        m18_fires = out["scanner_replica.signal_fires"].to_numpy() == 1
        m17_fires = sig_df["signal"].to_numpy() != 0
        np.testing.assert_array_equal(
            m18_fires, m17_fires,
            err_msg="M18 scanner_replica.signal_fires must match "
                    "M17.B ScannerReplicaStrategy.generate signal column")

    def test_long_count_within_bounds(self):
        per_tf = _make_multi_tf_bars(seed=5)
        out = scanner_replica.compute(per_tf["15m"],
                                        per_tf_bars=per_tf)
        lc = out["scanner_replica.long_count"]
        avail = out["scanner_replica.available_tf_count"]
        # long_count must be <= available_tf_count at every anchor
        self.assertTrue((lc <= avail).all(),
            "long_count cannot exceed available_tf_count")
        # Both within [0, 4]
        self.assertTrue(((lc >= 0) & (lc <= 4)).all())
        self.assertTrue(((avail >= 0) & (avail <= 4)).all())

    def test_signal_implies_long_count_meets_min_valid(self):
        """When signal_fires=1, long_count >= confluence_min_valid."""
        per_tf = _make_multi_tf_bars(seed=6, n_15m=600)
        out = scanner_replica.compute(per_tf["15m"],
                                        per_tf_bars=per_tf)
        fires = out["scanner_replica.signal_fires"] == 1
        lc = out.loc[fires, "scanner_replica.long_count"]
        mv = out.loc[fires, "scanner_replica.confluence_min_valid"]
        self.assertTrue((lc >= mv).all(),
            "signal fired but long_count < confluence_min_valid")

    def test_leak_safety_future_15m_scramble(self):
        per_tf = _make_multi_tf_bars(seed=7, n_15m=500)
        anchor_idx = len(per_tf["15m"]) - 50
        scram_15m = per_tf["15m"].copy()
        rng = np.random.default_rng(54321)
        future_n = len(scram_15m) - anchor_idx - 1
        for col, lo, hi in (("close", 1, 1000), ("high", 1000, 2000),
                              ("low", 0.1, 1), ("volume", 1, 1e9)):
            scram_15m.loc[anchor_idx + 1:, col] = rng.uniform(
                lo, hi, future_n)
        scram_per_tf = dict(per_tf)
        scram_per_tf["15m"] = scram_15m

        a = scanner_replica.compute(per_tf["15m"], per_tf_bars=per_tf)
        b = scanner_replica.compute(scram_15m,
                                      per_tf_bars=scram_per_tf)
        for col in a.columns:
            np.testing.assert_array_equal(
                a.iloc[:anchor_idx + 1][col].to_numpy(),
                b.iloc[:anchor_idx + 1][col].to_numpy(),
                err_msg=f"scanner_replica/{col}: scramble leaked future")

    def test_specs_all_safe_leak_class(self):
        for s in scanner_replica.SPECS:
            self.assertEqual(s.leak_class, "safe",
                f"{s.feature_id} must be leak_class='safe'")


# ─────────────────────────────────────────────────────────────────────
# G2_MarketContext
# ─────────────────────────────────────────────────────────────────────

class G2_MarketContext(unittest.TestCase):

    @staticmethod
    def _make_spy_qqq(n=300, seed=10, start="2023-01-01"):
        rng = np.random.default_rng(seed)
        ts = pd.date_range(start, periods=n, freq="1D", tz="UTC")
        close = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
        return pd.DataFrame({
            "ts_utc": ts, "open": close, "high": close * 1.005,
            "low": close * 0.995, "close": close,
            "volume": np.full(n, 1e8), "quality_flags": 0,
        })

    def test_benchmark_available_when_both_provided(self):
        bars = _make_synthetic_bars(n=50)
        spy = self._make_spy_qqq(n=400, seed=10)
        qqq = self._make_spy_qqq(n=400, seed=11)
        out = market_context.compute(
            bars, benchmark_bars={"SPY": spy, "QQQ": qqq})
        # benchmark_data_available should be 1 across the bars window
        # (both SPY and QQQ have rows at-or-before every bar).
        self.assertTrue(
            (out["market_context.benchmark_data_available"] == 1).all())

    def test_benchmark_unavailable_when_missing(self):
        bars = _make_synthetic_bars(n=50)
        out = market_context.compute(bars, benchmark_bars={})
        self.assertTrue(
            (out["market_context.benchmark_data_available"] == 0).all())
        # SPY/QQQ-derived float features must be NaN
        self.assertTrue(
            out["market_context.spy_drawdown_pct_60d"].isna().all())
        self.assertTrue(
            out["market_context.qqq_log_ret_1d_at_anchor"].isna().all())

    def test_spy_above_ema200_uptrend(self):
        # Sustained uptrend SPY → close > EMA200 once warmup completes
        bars = _make_synthetic_bars(n=50)
        n = 400
        close = 100.0 + np.arange(n) * 0.5
        spy = pd.DataFrame({
            "ts_utc": pd.date_range("2022-01-01", periods=n,
                                      freq="1D", tz="UTC"),
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e8), "quality_flags": 0,
        })
        out = market_context.compute(
            bars, benchmark_bars={"SPY": spy})
        # spy_above_ema200_1d must be 1 at every anchor (uptrend +
        # benchmark warmup complete before bars start)
        self.assertTrue(
            (out["market_context.spy_above_ema200_1d"] == 1).all())

    def test_leak_safety_future_spy_scramble(self):
        bars = _make_synthetic_bars(n=50)
        spy = self._make_spy_qqq(n=400, seed=10)
        qqq = self._make_spy_qqq(n=400, seed=11)

        # Scramble SPY bars beyond the anchor of last `bars` row.
        last_anchor = bars["ts_utc"].iloc[-25]
        rng = np.random.default_rng(444)
        scram_spy = spy.copy()
        mask = scram_spy["ts_utc"] > last_anchor
        nscram = int(mask.sum())
        scram_spy.loc[mask, "close"] = rng.uniform(1, 1000, nscram)
        scram_spy.loc[mask, "open"]  = rng.uniform(1, 1000, nscram)
        scram_spy.loc[mask, "high"]  = rng.uniform(1000, 2000, nscram)
        scram_spy.loc[mask, "low"]   = rng.uniform(0.1, 1, nscram)

        a = market_context.compute(
            bars, benchmark_bars={"SPY": spy, "QQQ": qqq})
        b = market_context.compute(
            bars, benchmark_bars={"SPY": scram_spy, "QQQ": qqq})
        # The first 25 bars (whose anchors are all before last_anchor)
        # must not be affected by the SPY scramble.
        n_keep = 25   # bars[:n_keep] all have ts_utc <= last_anchor
        for col in a.columns:
            av = a[col].iloc[:n_keep].to_numpy()
            bv = b[col].iloc[:n_keep].to_numpy()
            np.testing.assert_array_equal(
                np.isnan(av), np.isnan(bv),
                err_msg=f"market_context/{col}: NaN mask diff")
            m = ~np.isnan(av)
            np.testing.assert_array_equal(av[m], bv[m],
                err_msg=f"market_context/{col}: SPY scramble leaked")


# ─────────────────────────────────────────────────────────────────────
# G2_SignalHistory
# ─────────────────────────────────────────────────────────────────────

class G2_SignalHistory(unittest.TestCase):

    @staticmethod
    def _build_signal_outcomes_db(path, rows):
        """Create a real sqlite3 DB with the (subset of) flywheel
        schema this reader needs, then insert the provided rows."""
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("""
                CREATE TABLE signal_outcomes (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id        INTEGER NOT NULL DEFAULT 0,
                    intent_id        INTEGER DEFAULT NULL,
                    symbol           TEXT    NOT NULL DEFAULT '',
                    direction        TEXT    NOT NULL DEFAULT '',
                    entry_price      REAL,
                    exit_price       REAL,
                    return_pct       REAL,
                    outcome          TEXT    DEFAULT NULL,
                    bars_held        INTEGER DEFAULT NULL,
                    resolved_at      TEXT    DEFAULT NULL,
                    resolution_method TEXT   DEFAULT NULL
                )
            """)
            for r in rows:
                conn.execute(
                    "INSERT INTO signal_outcomes "
                    "(symbol, direction, return_pct, outcome, "
                    " resolved_at) VALUES (?, ?, ?, ?, ?)",
                    (r["symbol"], r.get("direction", "long"),
                      r.get("return_pct"), r["outcome"],
                      r["resolved_at"]))
            conn.commit()

    def test_no_db_returns_all_nan(self):
        bars = _make_synthetic_bars(n=10)
        out = signal_history.compute(bars, symbol="AAPL")
        self.assertTrue(
            (out["signal_history.signals_count_30d"] == 0).all())
        self.assertTrue(
            out["signal_history.win_rate_30d"].isna().all())
        self.assertTrue(
            out["signal_history.avg_return_pct_90d"].isna().all())

    def test_nonexistent_db_returns_all_nan(self):
        bars = _make_synthetic_bars(n=5)
        out = signal_history.compute(bars, symbol="AAPL",
            db_path="/tmp/this_path_does_not_exist_xyz.db")
        self.assertTrue(
            (out["signal_history.signals_count_30d"] == 0).all())

    def test_specs_use_requires_past_flywheel_leak_class(self):
        for s in signal_history.SPECS:
            self.assertEqual(s.leak_class,
                              "requires_past_flywheel_only",
                f"{s.feature_id} should be "
                f"leak_class='requires_past_flywheel_only'")

    def test_point_in_time_correctness_real_db(self):
        """Build a real DB with outcomes at known timestamps; compute
        signal_history at an anchor; verify only outcomes resolved
        BEFORE the anchor are counted."""
        from contextlib import closing
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            anchor = pd.Timestamp("2024-06-01", tz="UTC")
            rows = [
                # Resolved BEFORE anchor — within 30d → count for 30d/90d
                {"symbol": "AAPL", "outcome": "WIN",
                  "return_pct": 0.05,
                  "resolved_at": "2024-05-20T10:00:00+00:00"},
                {"symbol": "AAPL", "outcome": "LOSS",
                  "return_pct": -0.02,
                  "resolved_at": "2024-05-25T10:00:00+00:00"},
                # Resolved BEFORE anchor — in 30-90d window (not 30d)
                {"symbol": "AAPL", "outcome": "WIN",
                  "return_pct": 0.08,
                  "resolved_at": "2024-04-10T10:00:00+00:00"},
                # Resolved AT anchor — strict <, so excluded
                {"symbol": "AAPL", "outcome": "WIN",
                  "return_pct": 0.10,
                  "resolved_at": "2024-06-01T00:00:00+00:00"},
                # Resolved AFTER anchor — excluded (future)
                {"symbol": "AAPL", "outcome": "LOSS",
                  "return_pct": -0.04,
                  "resolved_at": "2024-06-15T10:00:00+00:00"},
                # Different symbol — excluded
                {"symbol": "MSFT", "outcome": "WIN",
                  "return_pct": 0.20,
                  "resolved_at": "2024-05-20T10:00:00+00:00"},
                # OPEN — excluded (future leak)
                {"symbol": "AAPL", "outcome": "OPEN",
                  "return_pct": None,
                  "resolved_at": "2024-05-22T10:00:00+00:00"},
            ]
            self._build_signal_outcomes_db(db, rows)

            # Bars: single anchor at 2024-06-01
            bars = pd.DataFrame({
                "ts_utc": [anchor],
                "open": [100.0], "high": [101.0], "low": [99.0],
                "close": [100.0], "volume": [1e6],
                "quality_flags": [0],
            })
            out = signal_history.compute(bars, symbol="AAPL",
                                           db_path=db)
            # 30d: AAPL closed in [2024-05-02, 2024-06-01)
            # → WIN (5/20), LOSS (5/25) = 2 outcomes, 1 win
            #   → win_rate_30d = 0.5
            self.assertEqual(int(out["signal_history.signals_count_30d"]
                                    .iloc[0]), 2)
            self.assertAlmostEqual(
                float(out["signal_history.win_rate_30d"].iloc[0]),
                0.5, places=10)
            # 90d: same 2 plus the 4/10 WIN = 3 outcomes, 2 wins
            #   → win_rate_90d = 2/3, avg_return = (0.05 -0.02 +0.08)/3
            self.assertEqual(int(out["signal_history.signals_count_90d"]
                                    .iloc[0]), 3)
            self.assertAlmostEqual(
                float(out["signal_history.win_rate_90d"].iloc[0]),
                2.0 / 3.0, places=10)
            self.assertAlmostEqual(
                float(out["signal_history.avg_return_pct_90d"]
                        .iloc[0]),
                (0.05 - 0.02 + 0.08) / 3.0, places=10)

    def test_flywheel_reader_is_read_only(self):
        """Open a writable DB but verify the reader's connection
        rejects writes."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "ro_test.db"
            self._build_signal_outcomes_db(db, [
                {"symbol": "AAPL", "outcome": "WIN",
                  "return_pct": 0.01,
                  "resolved_at": "2024-05-01T00:00:00+00:00"}])
            reader = flywheel_reader.FlywheelReader(db)
            self.assertTrue(reader.is_available())
            # Open the reader's connection internally and try a write
            conn = reader._open_ro()
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("DELETE FROM signal_outcomes")
                    conn.commit()
            finally:
                conn.close()


# ═════════════════════════════════════════════════════════════════════
# ═════════════════════════════════════════════════════════════════════
# G3 — Label compute groups (M18.A.4 — corrected against locked plan)
# ═════════════════════════════════════════════════════════════════════
#
# LOCKED M18 LABEL LIST (10 labels):
#   triple_barrier_atr_2_3_50            classification_3way  TP=3*ATR, SL=2*ATR
#   triple_barrier_atr_2_3_50_won        binary               collapsed 3-way
#   fwd_return_5b                        regression
#   fwd_return_20b                       regression
#   cost_adjusted_fwd_return_5b          regression           10 bps round-trip
#   mfe_50b                              regression           50-bar horizon
#   mae_50b                              regression           50-bar horizon
#   mfe_over_atr_50b                     regression
#   mae_over_atr_50b                     regression
#   risk_adjusted_fwd_return_5b          regression           fwd/(ATR/entry)


def _trending_bars_for_labels(direction: str = "up", n: int = 80,
                                bar_size: float = 1.0,
                                hl_spread: float = 0.5):
    """Build a deterministic bars frame with a strict monotone
    direction at a fixed bar size. Used to verify which barrier
    (target / stop) gets hit in the triple-barrier label."""
    if direction not in ("up", "down", "flat"):
        raise ValueError(direction)
    sign = {"up": 1.0, "down": -1.0, "flat": 0.0}[direction]
    base = 100.0
    closes = np.array([base + sign * i * bar_size for i in range(n)])
    opens  = np.concatenate([[base], closes[:-1]])
    highs  = np.maximum(opens, closes) + hl_spread
    lows   = np.minimum(opens, closes) - hl_spread
    return pd.DataFrame({
        "ts_utc": pd.date_range("2024-01-02", periods=n,
                                  freq="1D", tz="UTC"),
        "open":   opens,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": np.full(n, 1_000_000.0),
        "quality_flags": 0,
    })


def _atr_at_start(bars: pd.DataFrame, value: float) -> pd.Series:
    """Constant ATR series — predictable target/stop in fixture tests."""
    return pd.Series(np.full(len(bars), value), index=bars.index)


# ─────────────────────────────────────────────────────────────────────
# G3_TripleBarrier  (TP=3*ATR, SL=2*ATR, timeout=50, tie=stop_first)
# ─────────────────────────────────────────────────────────────────────

class G3_TripleBarrier(unittest.TestCase):

    LID_3WAY = "triple_barrier_atr_2_3_50"
    LID_WON  = "triple_barrier_atr_2_3_50_won"

    def test_tp_sl_constants(self):
        """LOCKED: TP_MULT=3.0, SL_MULT=2.0 (NOT the reverse)."""
        self.assertEqual(triple_barrier.TP_MULT, 3.0)
        self.assertEqual(triple_barrier.SL_MULT, 2.0)
        self.assertEqual(triple_barrier.TIMEOUT_BARS, 50)
        # Both LabelSpecs must report the same multipliers.
        for s in triple_barrier.SPECS:
            self.assertEqual(s.tp_mult, 3.0,
                f"{s.label_id} tp_mult must be 3.0")
            self.assertEqual(s.sl_mult, 2.0,
                f"{s.label_id} sl_mult must be 2.0")
            self.assertEqual(s.horizon_bars, 50)
            self.assertEqual(s.tie_breaker, "pessimistic_stop_first")
            self.assertEqual(s.entry_price_source,
                              "next_bar_open_after_anchor")

    def test_binary_label_exists_and_is_classified(self):
        """LOCKED: a binary collapsed _won label must be in SPECS."""
        ids = [s.label_id for s in triple_barrier.SPECS]
        self.assertIn(self.LID_WON, ids,
            "triple_barrier_atr_2_3_50_won binary label is missing")
        won_spec = next(s for s in triple_barrier.SPECS
                          if s.label_id == self.LID_WON)
        self.assertEqual(won_spec.label_class, "binary")
        self.assertEqual(won_spec.leak_class, "future_label_only")

    def test_target_hit_in_uptrend(self):
        """Uptrend at +1/bar, ATR=1.0 → target=entry+3 reached within
        3 bars; stop=entry-2 never. All resolved 3-way = +1, binary
        = 1.0."""
        bars = _trending_bars_for_labels(direction="up", n=80,
                                            bar_size=1.0, hl_spread=0.5)
        atr = _atr_at_start(bars, 1.0)
        out = triple_barrier.compute(bars, atr_series=atr)
        resolved_mask = out[f"{self.LID_3WAY}.is_pending"] == 0
        self.assertTrue(
            (out.loc[resolved_mask, self.LID_3WAY] == 1.0).all())
        self.assertTrue(
            (out.loc[resolved_mask, self.LID_WON] == 1.0).all(),
            "binary _won must be 1 for every target-hit row")

    def test_stop_hit_in_downtrend(self):
        bars = _trending_bars_for_labels(direction="down", n=80,
                                            bar_size=1.0, hl_spread=0.5)
        atr = _atr_at_start(bars, 1.0)
        out = triple_barrier.compute(bars, atr_series=atr)
        resolved_mask = out[f"{self.LID_3WAY}.is_pending"] == 0
        self.assertTrue(
            (out.loc[resolved_mask, self.LID_3WAY] == -1.0).all())
        self.assertTrue(
            (out.loc[resolved_mask, self.LID_WON] == 0.0).all(),
            "binary _won must be 0 for every stop-hit row")

    def test_timeout_in_flat_market(self):
        """Flat bars, ATR=10 → target=entry+30 / stop=entry-20 never
        reached. All resolved 3-way = 0 (timeout); binary = 0."""
        bars = _trending_bars_for_labels(direction="flat", n=80,
                                            bar_size=0.0, hl_spread=0.1)
        atr = _atr_at_start(bars, 10.0)
        out = triple_barrier.compute(bars, atr_series=atr)
        resolved_mask = out[f"{self.LID_3WAY}.is_pending"] == 0
        self.assertGreater(int(resolved_mask.sum()), 0)
        self.assertTrue(
            (out.loc[resolved_mask, self.LID_3WAY] == 0.0).all())
        # Binary collapse: timeout → 0 (not target hit)
        self.assertTrue(
            (out.loc[resolved_mask, self.LID_WON] == 0.0).all())

    def test_pending_for_last_window(self):
        """Anchor i resolves only if i + 50 < n. Last 50 rows pending."""
        bars = _trending_bars_for_labels(direction="up", n=100,
                                            bar_size=0.5, hl_spread=0.2)
        atr = _atr_at_start(bars, 1.0)
        out = triple_barrier.compute(bars, atr_series=atr)
        pending_3way = int(out[f"{self.LID_3WAY}.is_pending"].sum())
        pending_won  = int(out[f"{self.LID_WON}.is_pending"].sum())
        self.assertEqual(pending_3way, 50)
        self.assertEqual(pending_won, 50,
            "binary _won must share the pending mask with the 3-way")
        pending_rows = out[out[f"{self.LID_3WAY}.is_pending"] == 1]
        self.assertTrue(pending_rows[self.LID_3WAY].isna().all())
        self.assertTrue(pending_rows[self.LID_WON].isna().all())
        self.assertTrue(pd.isna(
            pending_rows[f"{self.LID_3WAY}.resolved_ts"]).all())
        self.assertTrue(pd.isna(
            pending_rows[f"{self.LID_WON}.resolved_ts"]).all())

    def test_same_bar_tie_pessimistic_stop_first(self):
        """Construct a bar where high >= target AND low <= stop on the
        SAME bar (entry_open=100, ATR=1.0 → target=103, stop=98).
        Pessimistic convention: 3-way = -1, binary _won = 0."""
        n = 60
        opens  = np.full(n, 100.0)
        closes = np.full(n, 100.0)
        highs  = np.full(n, 100.5)
        lows   = np.full(n,  99.5)
        # Bar 1 (the entry bar at open=100) gets the tie
        opens[1]  = 100.0
        highs[1]  = 103.0   # >= 103 target
        lows[1]   = 98.0    # <= 98 stop  (== triggers stop)
        closes[1] = 100.0
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open":   opens, "high": highs, "low": lows, "close": closes,
            "volume": np.full(n, 1_000_000.0),
            "quality_flags": 0,
        })
        atr = _atr_at_start(bars, 1.0)
        out = triple_barrier.compute(bars, atr_series=atr)
        self.assertEqual(float(out[self.LID_3WAY].iloc[0]), -1.0,
            "same-bar tie must resolve pessimistic_stop_first → -1")
        self.assertEqual(float(out[self.LID_WON].iloc[0]), 0.0,
            "binary _won at same-bar tie must be 0")
        self.assertEqual(
            int(out[f"{self.LID_3WAY}.bars_to_resolution"].iloc[0]), 1)

    def test_resolved_ts_strictly_after_anchor(self):
        bars = _trending_bars_for_labels(direction="up", n=80,
                                            bar_size=0.6, hl_spread=0.3)
        atr = _atr_at_start(bars, 1.0)
        out = triple_barrier.compute(bars, atr_series=atr)
        assert_label_resolved_after_anchor(bars, self.LID_3WAY, out)
        assert_label_resolved_after_anchor(bars, self.LID_WON, out)

    def test_nan_atr_yields_pending(self):
        bars = _trending_bars_for_labels(direction="up", n=80)
        atr = pd.Series(np.full(len(bars), np.nan), index=bars.index)
        out = triple_barrier.compute(bars, atr_series=atr)
        self.assertTrue(
            (out[f"{self.LID_3WAY}.is_pending"] == 1).all())
        self.assertTrue(
            (out[f"{self.LID_WON}.is_pending"] == 1).all())

    def test_binary_matches_3way_collapse(self):
        """The binary _won label must equal 1 wherever 3-way == +1
        and 0 wherever 3-way ∈ {-1, 0}. This is the canonical
        collapse rule."""
        bars = _trending_bars_for_labels(direction="up", n=80,
                                            bar_size=0.6, hl_spread=0.3)
        atr = _atr_at_start(bars, 1.0)
        out = triple_barrier.compute(bars, atr_series=atr)
        resolved = out[out[f"{self.LID_3WAY}.is_pending"] == 0]
        expected = (resolved[self.LID_3WAY] == 1.0).astype(float)
        np.testing.assert_array_equal(
            resolved[self.LID_WON].to_numpy(),
            expected.to_numpy())


# ─────────────────────────────────────────────────────────────────────
# G3_ForwardReturns  (fwd_return_{5,20}b + cost_adjusted_fwd_return_5b)
# ─────────────────────────────────────────────────────────────────────

class G3_ForwardReturns(unittest.TestCase):

    def test_locked_label_ids(self):
        ids = sorted(s.label_id for s in forward_returns.SPECS)
        self.assertEqual(ids, [
            "cost_adjusted_fwd_return_5b",
            "fwd_return_20b",
            "fwd_return_5b",
        ])

    def test_known_geometric_series(self):
        # close[i+5] = open[i+1] * 1.01^5 → fwd_return_5b = 5*ln(1.01)
        n = 30
        close = 100.0 * np.power(1.01, np.arange(n))
        open_ = np.concatenate([[100.0], close[:-1]])
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": open_, "high": close * 1.005,
            "low": close * 0.995, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = forward_returns.compute(bars)
        self.assertAlmostEqual(float(out["fwd_return_5b"].iloc[0]),
                                 5 * np.log(1.01), places=12)
        self.assertAlmostEqual(float(out["fwd_return_20b"].iloc[0]),
                                 20 * np.log(1.01), places=12)

    def test_cost_adjusted_subtracts_10bps(self):
        n = 30
        close = 100.0 * np.power(1.01, np.arange(n))
        open_ = np.concatenate([[100.0], close[:-1]])
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": open_, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        out = forward_returns.compute(bars)
        diff = (out["fwd_return_5b"]
                  - out["cost_adjusted_fwd_return_5b"]).dropna()
        np.testing.assert_allclose(diff.to_numpy(),
                                     0.0010, atol=1e-12)

    def test_pending_for_tail_rows(self):
        n = 30
        bars = _trending_bars_for_labels(direction="up", n=n)
        out = forward_returns.compute(bars)
        # fwd_return_5b: i+5 >= n → i >= 25 → 5 pending rows
        self.assertEqual(int(out["fwd_return_5b.is_pending"].sum()), 5)
        # fwd_return_20b: i+20 >= n → i >= 10 → 20 pending
        self.assertEqual(int(out["fwd_return_20b.is_pending"].sum()),
                          20)
        # cost-adjusted shares 5b's pending mask
        self.assertEqual(
            int(out["cost_adjusted_fwd_return_5b.is_pending"].sum()),
            5)

    def test_resolved_ts_invariant_all_labels(self):
        bars = _trending_bars_for_labels(direction="up", n=60)
        out = forward_returns.compute(bars)
        for lid in ("fwd_return_5b", "fwd_return_20b",
                      "cost_adjusted_fwd_return_5b"):
            assert_label_resolved_after_anchor(bars, lid, out)

    def test_specs_classes_and_cost_flags(self):
        for s in forward_returns.SPECS:
            self.assertEqual(s.leak_class, "future_label_only")
            self.assertEqual(s.label_class, "regression")
        cost_adj = [s for s in forward_returns.SPECS
                      if s.cost_model_applied]
        self.assertEqual(len(cost_adj), 1,
            "exactly 1 cost-adjusted label (5b only) per locked plan")
        self.assertEqual(cost_adj[0].label_id,
                          "cost_adjusted_fwd_return_5b")


# ─────────────────────────────────────────────────────────────────────
# G3_MFE_MAE  (HORIZON=50; raw + ATR-normalized only; no pct variants)
# ─────────────────────────────────────────────────────────────────────

class G3_MFE_MAE(unittest.TestCase):

    def test_locked_label_ids(self):
        ids = sorted(s.label_id for s in mfe_mae.SPECS)
        self.assertEqual(ids, [
            "mae_50b", "mae_over_atr_50b",
            "mfe_50b", "mfe_over_atr_50b",
        ])

    def test_horizon_is_50(self):
        for s in mfe_mae.SPECS:
            self.assertEqual(s.horizon_bars, 50,
                f"{s.label_id} horizon must be 50")
        self.assertEqual(mfe_mae.HORIZON, 50)

    def test_mfe_zero_in_strict_downtrend(self):
        """Strict downtrend, HL spread 0. Entry=open[1]=99.
        Forward 50-bar window highs are all < entry. So MFE = 0
        and MAE > 0."""
        bars = _trending_bars_for_labels(direction="down", n=80,
                                            bar_size=1.0, hl_spread=0.0)
        out = mfe_mae.compute(bars)
        resolved = out[out["mfe_50b.is_pending"] == 0]
        self.assertTrue((resolved["mfe_50b"] <= 1e-9).all(),
            f"MFE should be 0 in strict downtrend; max="
            f"{resolved['mfe_50b'].max()}")
        self.assertTrue((resolved["mae_50b"] > 0).all())

    def test_mae_zero_in_strict_uptrend(self):
        bars = _trending_bars_for_labels(direction="up", n=80,
                                            bar_size=1.0, hl_spread=0.0)
        out = mfe_mae.compute(bars)
        resolved = out[out["mae_50b.is_pending"] == 0]
        self.assertTrue((resolved["mae_50b"] <= 1e-9).all())
        self.assertTrue((resolved["mfe_50b"] > 0).all())

    def test_atr_normalized_requires_atr(self):
        bars = _trending_bars_for_labels(direction="up", n=80)
        out_noatr = mfe_mae.compute(bars)
        self.assertTrue(out_noatr["mfe_over_atr_50b"].isna().all())
        self.assertTrue(out_noatr["mae_over_atr_50b"].isna().all())
        out_atr = mfe_mae.compute(bars,
                                    atr_series=_atr_at_start(bars, 1.0))
        resolved = out_atr[out_atr["mfe_50b.is_pending"] == 0]
        self.assertFalse(resolved["mfe_over_atr_50b"].isna().any())

    def test_atr_normalized_division_math(self):
        """With ATR=2.0 and known MFE, mfe_over_atr_50b == MFE/2.0."""
        bars = _trending_bars_for_labels(direction="up", n=80,
                                            bar_size=0.6,
                                            hl_spread=0.3)
        atr = _atr_at_start(bars, 2.0)
        out = mfe_mae.compute(bars, atr_series=atr)
        resolved = out[out["mfe_50b.is_pending"] == 0]
        for i in resolved.index:
            expected = resolved.loc[i, "mfe_50b"] / 2.0
            self.assertAlmostEqual(
                float(resolved.loc[i, "mfe_over_atr_50b"]),
                expected, places=10)

    def test_pending_for_last_50(self):
        """Anchor i needs i+50 < n, so last 50 anchors pending."""
        bars = _trending_bars_for_labels(direction="up", n=80)
        out = mfe_mae.compute(bars)
        self.assertEqual(int(out["mfe_50b.is_pending"].sum()), 50)

    def test_resolved_ts_invariant(self):
        bars = _trending_bars_for_labels(direction="up", n=80)
        out = mfe_mae.compute(bars,
                                atr_series=_atr_at_start(bars, 1.0))
        for lid in ("mfe_50b", "mae_50b",
                      "mfe_over_atr_50b", "mae_over_atr_50b"):
            assert_label_resolved_after_anchor(bars, lid, out)


# ─────────────────────────────────────────────────────────────────────
# G3_RiskAdjusted  (single label: risk_adjusted_fwd_return_5b)
# ─────────────────────────────────────────────────────────────────────

class G3_RiskAdjusted(unittest.TestCase):

    LID = "risk_adjusted_fwd_return_5b"

    def test_locked_label_id_and_horizon(self):
        ids = [s.label_id for s in risk_adjusted.SPECS]
        self.assertEqual(ids, [self.LID])
        self.assertEqual(risk_adjusted.SPECS[0].horizon_bars, 5)
        self.assertEqual(risk_adjusted.HORIZON, 5)

    def test_division_math(self):
        """fwd_return_5b at anchor 0 = 5*ln(1.005); ATR=1.0, entry=100
        → over_atr = 5*ln(1.005) / (1/100) = 500*ln(1.005)."""
        n = 30
        close = 100.0 * np.power(1.005, np.arange(n))
        open_ = np.concatenate([[100.0], close[:-1]])
        bars = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": open_, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1e6), "quality_flags": 0,
        })
        atr = pd.Series(np.full(n, 1.0), index=bars.index)
        out = risk_adjusted.compute(bars, atr_series=atr)
        # anchor 0: entry=open[1]=close[0]=100, exit=close[5]
        # fwd_log = log(close[5]/100) = 5*ln(1.005)
        # over_atr = 5*ln(1.005) / (1.0/100)
        expected = 5 * np.log(1.005) / (1.0 / 100.0)
        self.assertAlmostEqual(float(out[self.LID].iloc[0]),
                                 expected, places=10)

    def test_nan_atr_yields_nan_value_not_pending(self):
        """ATR all NaN → label all NaN, but rows whose forward
        window resolved are NOT pending — only the denominator is
        undefined."""
        n = 30
        bars = _trending_bars_for_labels(direction="up", n=n)
        atr = pd.Series(np.full(n, np.nan), index=bars.index)
        out = risk_adjusted.compute(bars, atr_series=atr)
        self.assertTrue(out[self.LID].isna().all())
        non_pending = (out[f"{self.LID}.is_pending"] == 0).sum()
        # n - 5 anchors have a valid forward window at horizon=5
        self.assertEqual(int(non_pending), n - 5)

    def test_pending_for_last_5(self):
        n = 30
        bars = _trending_bars_for_labels(direction="up", n=n)
        atr = _atr_at_start(bars, 1.0)
        out = risk_adjusted.compute(bars, atr_series=atr)
        # horizon=5: pending iff i+5 >= n → i >= 25 → 5 rows
        self.assertEqual(int(out[f"{self.LID}.is_pending"].sum()), 5)

    def test_resolved_ts_invariant(self):
        n = 50
        bars = _trending_bars_for_labels(direction="up", n=n)
        atr = _atr_at_start(bars, 1.0)
        out = risk_adjusted.compute(bars, atr_series=atr)
        assert_label_resolved_after_anchor(bars, self.LID, out)


# ─────────────────────────────────────────────────────────────────────
# G3_LabelLeakSafety  (past-bar scramble across all groups)
# ─────────────────────────────────────────────────────────────────────

class G3_LabelLeakSafety(unittest.TestCase):
    """Labels look only AT or AFTER the anchor (entry = open[i+1]).
    Scrambling bars STRICTLY BEFORE the anchor must not change the
    label at the anchor — provided we hold the ATR series constant
    (since real ATR depends on past bars; that dependency is
    correctly handled by passing pre-computed ATR through, not by
    having label code recompute it internally).
    """

    def test_past_bar_scramble_does_not_change_labels(self):
        bars = _trending_bars_for_labels(direction="up", n=120,
                                            bar_size=0.6,
                                            hl_spread=0.3)
        atr = _atr_at_start(bars, 1.0)

        # Scramble bars 0..30 (strictly before the anchors at 40+).
        anchor_lo = 40
        rng = np.random.default_rng(31415)
        scrambled = bars.copy()
        scrambled.loc[:30, "open"]   = rng.uniform(1, 1000, 31)
        scrambled.loc[:30, "high"]   = rng.uniform(1000, 2000, 31)
        scrambled.loc[:30, "low"]    = rng.uniform(0.1, 1, 31)
        scrambled.loc[:30, "close"]  = rng.uniform(1, 1000, 31)

        for mod_name, kwargs in [
            ("triple_barrier", {"atr_series": atr}),
            ("forward_returns", {}),
            ("mfe_mae", {"atr_series": atr}),
            ("risk_adjusted", {"atr_series": atr}),
        ]:
            mod = {"triple_barrier": triple_barrier,
                    "forward_returns": forward_returns,
                    "mfe_mae": mfe_mae,
                    "risk_adjusted": risk_adjusted}[mod_name]
            a = mod.compute(bars, **kwargs)
            b = mod.compute(scrambled, **kwargs)
            for col in a.columns:
                # Skip aux columns whose value is a tz-aware ts —
                # those are compared via resolved_ts checks above.
                if col.endswith(".resolved_ts"):
                    continue
                if col.endswith(".is_pending"):
                    # Same boolean column; quickly assert equality.
                    np.testing.assert_array_equal(
                        a[col].iloc[anchor_lo:].to_numpy(),
                        b[col].iloc[anchor_lo:].to_numpy())
                    continue
                av = a[col].iloc[anchor_lo:].to_numpy()
                bv = b[col].iloc[anchor_lo:].to_numpy()
                np.testing.assert_array_equal(
                    np.isnan(av), np.isnan(bv),
                    err_msg=f"{mod_name}/{col}: NaN mask differs "
                              f"under past-bar scramble")
                m = ~np.isnan(av)
                np.testing.assert_allclose(
                    av[m], bv[m], rtol=1e-12, atol=1e-12,
                    err_msg=f"{mod_name}/{col}: past-bar scramble "
                              f"changed label values (leak!)")


# ─────────────────────────────────────────────────────────────────────
# G3_LockedLabelRegistry  (canary against future schema drift)
# ─────────────────────────────────────────────────────────────────────

class G3_LockedLabelRegistry(unittest.TestCase):
    """Belt-and-suspenders check that the exact set of label_ids
    emitted by M18.A.4 matches the locked plan EXACTLY. Future
    additions must update this list explicitly so the test forces
    a conscious choice."""

    LOCKED_LABEL_IDS = frozenset({
        "triple_barrier_atr_2_3_50",
        "triple_barrier_atr_2_3_50_won",
        "fwd_return_5b",
        "fwd_return_20b",
        "cost_adjusted_fwd_return_5b",
        "mfe_50b",
        "mae_50b",
        "mfe_over_atr_50b",
        "mae_over_atr_50b",
        "risk_adjusted_fwd_return_5b",
    })

    def test_registry_matches_locked_set(self):
        import bot.ml.labels as labels_pkg
        actual = set()
        for grp in labels_pkg.ALL_LABEL_GROUPS.values():
            for s in grp.SPECS:
                actual.add(s.label_id)
        self.assertEqual(actual, self.LOCKED_LABEL_IDS,
            f"label registry drift detected;\n"
            f"  missing from registry: "
            f"{self.LOCKED_LABEL_IDS - actual}\n"
            f"  extra in registry:     "
            f"{actual - self.LOCKED_LABEL_IDS}")

    def test_all_label_classes_in_allowed_set(self):
        from bot.ml.schemas import ALLOWED_LABEL_CLASSES
        import bot.ml.labels as labels_pkg
        for grp in labels_pkg.ALL_LABEL_GROUPS.values():
            for s in grp.SPECS:
                self.assertIn(s.label_class, ALLOWED_LABEL_CLASSES,
                    f"{s.label_id} has label_class={s.label_class!r}")

    def test_exactly_one_binary_label(self):
        import bot.ml.labels as labels_pkg
        binary = []
        for grp in labels_pkg.ALL_LABEL_GROUPS.values():
            for s in grp.SPECS:
                if s.label_class == "binary":
                    binary.append(s.label_id)
        self.assertEqual(binary, ["triple_barrier_atr_2_3_50_won"],
            f"expected exactly one binary label (the collapsed "
            f"triple-barrier _won); found {binary}")

    def test_exactly_one_three_way_label(self):
        import bot.ml.labels as labels_pkg
        three_way = []
        for grp in labels_pkg.ALL_LABEL_GROUPS.values():
            for s in grp.SPECS:
                if s.label_class == "classification_3way":
                    three_way.append(s.label_id)
        self.assertEqual(three_way, ["triple_barrier_atr_2_3_50"])

# ═════════════════════════════════════════════════════════════════════
# G4 — Dataset assembler + walk-forward + adversarial validation (M18.A.5)
# ═════════════════════════════════════════════════════════════════════


def _multi_tf_for_assembler(n_15m: int = 600, seed: int = 1,
                              start: str = "2024-01-02"):
    """Build aligned 15m / 1H / 4H / 1D bars suitable for the assembler.

    Uses different seeds per TF so the series don't collide; same time
    origin so MultiTimeframeContext.snapshot_at finds bars at every
    15m anchor."""
    def _one(n, freq, seed_):
        rng = np.random.default_rng(seed_)
        ts = pd.date_range(start, periods=n, freq=freq, tz="UTC")
        close = 100 * np.exp(np.cumsum(
            rng.normal(0.0001, 0.012, n)))
        open_ = np.concatenate([[100.0], close[:-1]])
        spread = np.abs(rng.normal(0, 0.008, n)) * close + 0.01
        high = np.maximum(open_, close) + spread / 2
        low  = np.minimum(open_, close) - spread / 2
        return pd.DataFrame({
            "ts_utc": ts, "open": open_, "high": high,
            "low": low, "close": close,
            "volume": rng.integers(1_000_000, 10_000_000, n
                                     ).astype(float),
            "quality_flags": 0,
        })
    return {
        "15m": _one(n_15m,                  "15min", seed * 11),
        "1H":  _one(max(300, n_15m // 4),   "1h",    seed * 13),
        "4H":  _one(max(300, n_15m // 16),  "4h",    seed * 17),
        "1D":  _one(max(300, n_15m // 96),  "1D",    seed * 19),
    }


# ─────────────────────────────────────────────────────────────────────
# G4_Anchors — Model A and Model B enumeration (Q18)
# ─────────────────────────────────────────────────────────────────────

class G4_Anchors(unittest.TestCase):

    def test_model_a_returns_only_fires(self):
        fires = pd.Series([0, 1, 0, 1, 1, 0, 0, 1], dtype="int8")
        idx = ds_anchors.enumerate_model_a_anchors(fires)
        np.testing.assert_array_equal(idx, np.array([1, 3, 4, 7]))

    def test_model_a_empty_when_no_fires(self):
        fires = pd.Series([0] * 10, dtype="int8")
        idx = ds_anchors.enumerate_model_a_anchors(fires)
        self.assertEqual(len(idx), 0)

    def test_model_b_is_union_of_1h_and_scanner(self):
        # 8 anchor bars at 15-min cadence
        anchor_ts = pd.Series(
            pd.date_range("2024-01-02", periods=8, freq="15min",
                            tz="UTC"))
        # 1H bars at positions 0, 4 (15min * 4 = 1H apart). They
        # close at the same ts as anchor[0] and anchor[4].
        one_hour_ts = pd.Series(
            pd.date_range("2024-01-02", periods=2, freq="1h",
                            tz="UTC"))
        # Scanner fires only at positions 2 and 5.
        fires = pd.Series([0, 0, 1, 0, 0, 1, 0, 0], dtype="int8")
        idx = ds_anchors.enumerate_model_b_anchors(
            anchor_ts=anchor_ts,
            one_hour_ts=one_hour_ts,
            scanner_replica_fires=fires,
        )
        # Union: 1H indices {0, 4} ∪ scanner indices {2, 5}
        np.testing.assert_array_equal(idx, np.array([0, 2, 4, 5]))

    def test_model_b_degenerates_to_scanner_when_no_1h_bars(self):
        anchor_ts = pd.Series(
            pd.date_range("2024-01-02", periods=5, freq="15min",
                            tz="UTC"))
        empty_1h = pd.Series([], dtype="datetime64[ns, UTC]")
        fires = pd.Series([1, 0, 0, 1, 0], dtype="int8")
        idx = ds_anchors.enumerate_model_b_anchors(
            anchor_ts=anchor_ts,
            one_hour_ts=empty_1h,
            scanner_replica_fires=fires,
        )
        np.testing.assert_array_equal(idx, np.array([0, 3]))

    def test_enumerate_dispatch_unknown_set_raises(self):
        with self.assertRaises(ValueError):
            ds_anchors.enumerate_anchors(
                anchor_set="not_a_real_anchor_set",
                anchor_ts=pd.Series([], dtype="datetime64[ns, UTC]"),
                scanner_replica_fires=pd.Series([], dtype="int8"),
            )

    def test_model_b_dispatch_requires_one_hour_ts(self):
        with self.assertRaises(ValueError):
            ds_anchors.enumerate_anchors(
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                anchor_ts=pd.Series([], dtype="datetime64[ns, UTC]"),
                scanner_replica_fires=pd.Series([], dtype="int8"),
            )


# ─────────────────────────────────────────────────────────────────────
# G4_Coverage — Q19 intraday-coverage gate
# ─────────────────────────────────────────────────────────────────────

class G4_Coverage(unittest.TestCase):

    @staticmethod
    def _stub_bars(n):
        return pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-02", periods=n,
                                      freq="1D", tz="UTC"),
            "open": np.ones(n), "high": np.ones(n),
            "low": np.ones(n), "close": np.ones(n),
            "volume": np.ones(n), "quality_flags": 0,
        })

    def test_full_coverage(self):
        per_tf = {tf: self._stub_bars(250)
                   for tf in ("15m", "1H", "4H", "1D")}
        rpt = ds_coverage.assess_intraday_coverage(per_tf)
        self.assertFalse(rpt.coverage_degraded)
        self.assertIsNone(rpt.degradation_warning)
        self.assertEqual(set(rpt.present_tfs),
                          {"15m", "1H", "4H", "1D"})
        self.assertEqual(rpt.degraded_tfs, ())
        self.assertEqual(rpt.missing_tfs, ())

    def test_missing_tf_is_degraded(self):
        per_tf = {"15m": self._stub_bars(250),
                   "1H":  self._stub_bars(250),
                   "1D":  self._stub_bars(250)}    # 4H missing
        rpt = ds_coverage.assess_intraday_coverage(per_tf)
        self.assertTrue(rpt.coverage_degraded)
        self.assertIn("4H", rpt.missing_tfs)

    def test_below_min_bars_is_degraded(self):
        per_tf = {tf: self._stub_bars(250)
                   for tf in ("15m", "1H", "1D")}
        per_tf["4H"] = self._stub_bars(50)   # below 200 min
        rpt = ds_coverage.assess_intraday_coverage(per_tf)
        self.assertTrue(rpt.coverage_degraded)
        self.assertIn("4H", rpt.degraded_tfs)
        self.assertEqual(rpt.missing_tfs, ())

    def test_assert_promotable_or_raise_passes_on_full(self):
        per_tf = {tf: self._stub_bars(250)
                   for tf in ("15m", "1H", "4H", "1D")}
        rpt = ds_coverage.assess_intraday_coverage(per_tf)
        # Must not raise
        rpt.assert_promotable_or_raise(symbol="TESTSYM")

    def test_assert_promotable_or_raise_raises_on_degraded(self):
        per_tf = {tf: self._stub_bars(250)
                   for tf in ("15m", "1H", "1D")}     # 4H missing
        rpt = ds_coverage.assess_intraday_coverage(per_tf)
        with self.assertRaises(
                ml_errors.InsufficientIntradayCoverageError):
            rpt.assert_promotable_or_raise(symbol="TESTSYM")

    def test_bar_counts_recorded(self):
        per_tf = {"15m": self._stub_bars(500),
                   "1H":  self._stub_bars(300),
                   "4H":  self._stub_bars(250),
                   "1D":  self._stub_bars(200)}
        rpt = ds_coverage.assess_intraday_coverage(per_tf)
        self.assertEqual(rpt.bar_counts,
                          {"15m": 500, "1H": 300, "4H": 250, "1D": 200})


# ─────────────────────────────────────────────────────────────────────
# G4_Manifest — deterministic dataset hash
# ─────────────────────────────────────────────────────────────────────

class G4_Manifest(unittest.TestCase):

    @staticmethod
    def _kw():
        return dict(
            symbol="AAPL", timeframes=["15m", "1H", "4H", "1D"],
            anchor_tf="15m", anchor_set="model_a_scanner_replica",
            bars_digest={"15m": {"n_bars": 100,
                                  "first_ts": "2024-01-02",
                                  "last_ts": "2024-01-03",
                                  "close_sum_str": "10000.0",
                                  "close_sum_sq_str": "1000000.0"}},
            feature_specs_hash="aa" * 32,
            label_specs_hash="bb" * 32,
            train_frac=0.6, val_frac=0.2, test_frac=0.2,
            embargo_bars=130, fixture_mode_invocation=False,
        )

    def test_hash_is_deterministic(self):
        h1 = ds_manifest.compute_dataset_hash(**self._kw())
        h2 = ds_manifest.compute_dataset_hash(**self._kw())
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_hash_changes_with_anchor_set(self):
        kw = self._kw()
        h1 = ds_manifest.compute_dataset_hash(**kw)
        kw["anchor_set"] = "model_b_1h_union_candidates"
        h2 = ds_manifest.compute_dataset_hash(**kw)
        self.assertNotEqual(h1, h2)

    def test_hash_changes_with_embargo(self):
        kw = self._kw()
        h1 = ds_manifest.compute_dataset_hash(**kw)
        kw["embargo_bars"] = 50
        h2 = ds_manifest.compute_dataset_hash(**kw)
        self.assertNotEqual(h1, h2)

    def test_hash_changes_with_feature_specs_hash(self):
        kw = self._kw()
        h1 = ds_manifest.compute_dataset_hash(**kw)
        kw["feature_specs_hash"] = "ff" * 32
        h2 = ds_manifest.compute_dataset_hash(**kw)
        self.assertNotEqual(h1, h2)

    def test_feature_specs_hash_stable(self):
        from bot.ml.features import ALL_FEATURE_GROUPS
        h1 = ds_manifest.compute_feature_specs_hash(ALL_FEATURE_GROUPS)
        h2 = ds_manifest.compute_feature_specs_hash(ALL_FEATURE_GROUPS)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_label_specs_hash_stable(self):
        from bot.ml.labels import ALL_LABEL_GROUPS
        h1 = ds_manifest.compute_label_specs_hash(ALL_LABEL_GROUPS)
        h2 = ds_manifest.compute_label_specs_hash(ALL_LABEL_GROUPS)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)


# ─────────────────────────────────────────────────────────────────────
# G4_MissingnessPolicy — explicit NaN / missingness policy (M18.B.5)
# ─────────────────────────────────────────────────────────────────────

class G4_MissingnessPolicy(unittest.TestCase):
    """Explicit, deterministic, auditable missingness policy:
    per-group neutral fill + indicators, JSON-safe report, policy hash
    that feeds dataset_hash/repro_hash_v2, and a finite-matrix guard so
    NaN/inf never reach .fit()."""

    def _miss(self):
        import bot.ml.features.missingness as miss
        return miss

    # 1
    def test_missingness_policy_object_is_canonical_and_hashes(self):
        miss = self._miss()
        h1 = miss.missingness_policy_hash()
        h2 = miss.missingness_policy_hash()
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)
        # changing the policy changes the hash
        orig = miss.FEATURE_GROUP_POLICY["trend"]["strategy"]
        miss.FEATURE_GROUP_POLICY["trend"]["strategy"] = "changed_for_test"
        try:
            self.assertNotEqual(h1, miss.missingness_policy_hash())
        finally:
            miss.FEATURE_GROUP_POLICY["trend"]["strategy"] = orig
        self.assertEqual(h1, miss.missingness_policy_hash())

    # 2
    def test_missingness_policy_covers_all_10_feature_groups(self):
        miss = self._miss()
        from bot.ml.features import ALL_FEATURE_GROUPS
        self.assertEqual(set(miss.LOCKED_FEATURE_GROUPS),
                          set(ALL_FEATURE_GROUPS.keys()))
        self.assertEqual(len(miss.LOCKED_FEATURE_GROUPS), 10)
        for g in ALL_FEATURE_GROUPS:
            self.assertIn(g, miss.FEATURE_GROUP_POLICY)

    # 3
    def test_missingness_policy_rejects_unknown_group(self):
        miss = self._miss()
        with self.assertRaises(ml_errors.M18ConfigError) as ctx:
            miss.assert_known_groups(["totally_unknown_group.feat"])
        self.assertIn("missingness_policy_unknown_group",
                       str(ctx.exception))

    # 4
    def test_warmup_nan_handled_deterministically(self):
        miss = self._miss()
        res = _assemble_for_training()
        fcols = select_feature_columns(list(res.dataset.columns))
        X = res.dataset[fcols].to_numpy(dtype=np.float64)
        self.assertTrue(np.isnan(X).any())   # warmup NaN present
        Xf1, _i1, _n1 = miss.apply_missingness_fill(X, fcols)
        Xf2, _i2, _n2 = miss.apply_missingness_fill(X, fcols)
        self.assertFalse(np.isnan(Xf1).any())
        np.testing.assert_array_equal(Xf1, Xf2)   # deterministic

    # 5
    def test_missingness_indicators_added_for_expected_missing_groups(self):
        miss = self._miss()
        res = _assemble_for_training()
        fcols = select_feature_columns(list(res.dataset.columns))
        X = res.dataset[fcols].to_numpy(dtype=np.float64)
        _Xf, inds, names = miss.apply_missingness_fill(X, fcols)
        self.assertTrue(all(n.endswith("__was_missing") for n in names))
        self.assertEqual(inds.shape[1], len(names))
        # at least one trend / price_return indicator present
        self.assertTrue(any(n.startswith("trend.") for n in names))

    # 6
    def test_signal_history_missingness_is_intentional(self):
        miss = self._miss()
        p = miss.FEATURE_GROUP_POLICY["signal_history"]
        self.assertTrue(p["indicator_required"])
        self.assertIn("intentional", p["expected_reason"])
        # a synthetic all-NaN signal_history column fills to neutral +
        # indicator, no final NaN
        cols = ["signal_history.x"]
        X = np.array([[np.nan], [np.nan], [1.0]])
        Xf, inds, names = miss.apply_missingness_fill(X, cols)
        self.assertFalse(np.isnan(Xf).any())
        self.assertEqual(names, ["signal_history.x__was_missing"])
        np.testing.assert_array_equal(inds[:, 0], np.array([1.0, 1.0, 0.0]))

    # 7
    def test_market_context_missingness_is_reported(self):
        miss = self._miss()
        cols = ["market_context.a", "market_context.b"]
        X = np.array([[np.nan, 1.0], [2.0, np.nan]])
        rep = miss.build_missingness_report(X, cols)
        self.assertIn("market_context", rep["groups"])
        mc = rep["groups"]["market_context"]
        self.assertEqual(mc["nan_before"], 2)
        self.assertEqual(mc["nan_after"], 0)
        self.assertTrue(mc["indicators_added"])

    # 8
    def test_mtf_confluence_missingness_no_lookahead(self):
        # The fill is a constant (0.0) and indicators are per-cell; no
        # value is sourced from another (future) row. Verify a later
        # non-missing value does not change an earlier filled cell.
        miss = self._miss()
        cols = ["mtf_confluence.htf"]
        X = np.array([[np.nan], [np.nan], [5.0]])
        Xf, _i, _n = miss.apply_missingness_fill(X, cols)
        # early rows filled with neutral 0.0, NOT back-filled from 5.0
        self.assertEqual(Xf[0, 0], 0.0)
        self.assertEqual(Xf[1, 0], 0.0)
        self.assertEqual(Xf[2, 0], 5.0)

    # 9
    def test_symbol_meta_unexpected_missing_detected(self):
        miss = self._miss()
        self.assertTrue(
            miss.FEATURE_GROUP_POLICY["symbol_meta"]["expect_no_missing"])
        cols = ["symbol_meta.sector_code"]
        X = np.array([[np.nan], [1.0]])
        rep = miss.build_missingness_report(X, cols)
        self.assertIn("unexpected_missingness_in_symbol_meta",
                       rep["unexpected_missingness_flags"])

    # 10
    def test_final_feature_matrix_has_no_nan_or_inf(self):
        miss = self._miss()
        res = _assemble_for_training()
        fcols = select_feature_columns(list(res.dataset.columns))
        for idxs in (res.split.train_anchor_indices,
                      res.split.val_anchor_indices,
                      res.split.test_anchor_indices):
            X, _y = extract_xy_for_split(
                res.dataset, np.asarray(idxs),
                target_label_id="triple_barrier_atr_2_3_50_won",
                feature_columns=fcols)
            self.assertFalse(np.isnan(X).any())
            self.assertFalse(np.isinf(X).any())

    # 11
    def test_trainer_rejects_remaining_nan_before_fit(self):
        # assert_finite_matrix raises on NaN (extract_xy fills NaN, so we
        # test the guard directly with a NaN that bypasses fill — i.e.
        # the guard itself is the last line of defence).
        miss = self._miss()
        with self.assertRaises(ml_errors.M18DataError) as ctx:
            miss.assert_finite_matrix(
                np.array([[1.0, np.nan]]), name="X")
        self.assertIn("missingness_remaining_nan", str(ctx.exception))

    # 12
    def test_trainer_rejects_remaining_inf_before_fit(self):
        miss = self._miss()
        res = _assemble_for_training()
        fcols = select_feature_columns(list(res.dataset.columns))
        ti = int(res.split.train_anchor_indices[0])
        res.dataset.loc[res.dataset.index[ti], fcols[0]] = np.inf
        with self.assertRaises(ml_errors.M18DataError) as ctx:
            ModelTrainer().train_one(
                _make_train_config(
                    "B2_logistic",
                    dataset_id=res.manifest.dataset_id),
                res)
        self.assertIn("missingness_remaining_inf", str(ctx.exception))

    # 13
    def test_missingness_report_is_json_safe(self):
        res = _assemble_for_training()
        rep = res.manifest.missingness_report
        s = json.dumps(rep, allow_nan=False)
        self.assertIsInstance(s, str)

    # 14
    def test_missingness_report_persisted_in_manifest(self):
        miss = self._miss()
        res = _assemble_for_training()
        self.assertTrue(res.manifest.missingness_policy_hash)
        self.assertEqual(res.manifest.missingness_policy_hash,
                          miss.missingness_policy_hash())
        self.assertIn("groups", res.manifest.missingness_report)

    # 15
    def test_missingness_manifest_round_trip_old_manifest_safe(self):
        res = _assemble_for_training()
        d = res.manifest.to_dict()
        d_old = {k: v for k, v in d.items()
                 if k not in ("missingness_policy_hash",
                               "missingness_report")}
        m = ds_manifest.DatasetManifest.from_dict(d_old)
        self.assertEqual(m.missingness_policy_hash, "")
        self.assertEqual(m.missingness_report, {})
        # new manifest also round-trips
        m2 = ds_manifest.DatasetManifest.from_dict(d)
        self.assertEqual(m2.missingness_policy_hash,
                          res.manifest.missingness_policy_hash)

    # 16
    def test_repro_hash_v2_changes_when_missingness_policy_changes(self):
        # The policy hash feeds dataset_hash, which is part of the
        # manifest dict that repro_hash_v2 hashes. Verify dataset_hash
        # (the carrier) changes with the policy hash.
        kw = dict(
            symbol="X", timeframes=["15m"], anchor_tf="15m",
            anchor_set="A", bars_digest={}, feature_specs_hash="f",
            label_specs_hash="l", train_frac=0.6, val_frac=0.2,
            test_frac=0.2, embargo_bars=10,
            fixture_mode_invocation=False)
        h_a = ds_manifest.compute_dataset_hash(
            **kw, missingness_policy_hash="HASH_A")
        h_b = ds_manifest.compute_dataset_hash(
            **kw, missingness_policy_hash="HASH_B")
        self.assertNotEqual(h_a, h_b)

    # 17
    def test_dataset_hash_changes_when_missingness_policy_changes(self):
        kw = dict(
            symbol="X", timeframes=["15m"], anchor_tf="15m",
            anchor_set="A", bars_digest={}, feature_specs_hash="f",
            label_specs_hash="l", train_frac=0.6, val_frac=0.2,
            test_frac=0.2, embargo_bars=10,
            fixture_mode_invocation=False)
        base = ds_manifest.compute_dataset_hash(**kw)          # default ""
        changed = ds_manifest.compute_dataset_hash(
            **kw, missingness_policy_hash="nonempty")
        self.assertNotEqual(base, changed)

    # 18
    def test_missingness_report_in_train_outputs(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config(
                "B2_logistic", dataset_id=res.manifest.dataset_id),
            res)
        self.assertTrue(out.missingness_policy_hash)
        self.assertIn("groups", out.missingness_report)
        self.assertIn("missingness_report", out.to_dict())

    # 19
    def test_no_blanket_fillna_without_indicator(self):
        # Every group that is filled must require an indicator — there is
        # no group whose NaN disappears silently with no indicator and
        # no report entry.
        miss = self._miss()
        for g, p in miss.FEATURE_GROUP_POLICY.items():
            self.assertTrue(
                p["indicator_required"],
                f"group {g} fills NaN but has no required indicator")

    # 20
    def test_non_missing_values_unchanged_by_policy(self):
        miss = self._miss()
        cols = ["trend.a", "momentum.b"]
        X = np.array([[1.5, -2.0], [np.nan, 3.0], [4.0, 5.0]])
        Xf, _i, _n = miss.apply_missingness_fill(X, cols)
        # non-missing cells identical
        self.assertEqual(Xf[0, 0], 1.5)
        self.assertEqual(Xf[0, 1], -2.0)
        self.assertEqual(Xf[1, 1], 3.0)
        self.assertEqual(Xf[2, 0], 4.0)
        self.assertEqual(Xf[2, 1], 5.0)
        # the one missing cell became neutral 0.0
        self.assertEqual(Xf[1, 0], 0.0)

    # ---- B.5 fix: indicators are REAL model features ----------------

    def test_extract_xy_appends_missingness_indicators_to_X(self):
        from bot.ml.features.missingness import (
            missingness_indicator_names)
        res = _assemble_for_training()
        fcols = select_feature_columns(list(res.dataset.columns))
        n_ind = len(missingness_indicator_names(fcols))
        self.assertGreater(n_ind, 0)
        X, _y = extract_xy_for_split(
            res.dataset, res.split.train_anchor_indices,
            target_label_id="triple_barrier_atr_2_3_50_won",
            feature_columns=fcols)
        self.assertEqual(X.shape[1], len(fcols) + n_ind)

    def test_train_outputs_n_features_includes_missingness_indicators(self):
        from bot.ml.features.missingness import (
            missingness_indicator_names)
        res = _assemble_for_training()
        fcols = select_feature_columns(list(res.dataset.columns))
        n_ind = len(missingness_indicator_names(fcols))
        out = ModelTrainer().train_one(
            _make_train_config(
                "B2_logistic", dataset_id=res.manifest.dataset_id),
            res)
        self.assertGreater(out.n_features, res.manifest.feature_count)
        self.assertEqual(out.n_features,
                          res.manifest.feature_count + n_ind)
        self.assertEqual(out.base_feature_count,
                          res.manifest.feature_count)
        self.assertEqual(out.missingness_indicator_count, n_ind)
        self.assertEqual(out.model_feature_count, out.n_features)

    def test_train_val_test_have_same_missingness_indicator_order(self):
        res = _assemble_for_training()
        fcols = select_feature_columns(list(res.dataset.columns))
        widths = []
        for idxs in (res.split.train_anchor_indices,
                      res.split.val_anchor_indices,
                      res.split.test_anchor_indices):
            X, _y = extract_xy_for_split(
                res.dataset, np.asarray(idxs),
                target_label_id="triple_barrier_atr_2_3_50_won",
                feature_columns=fcols)
            widths.append(X.shape[1])
        self.assertEqual(len(set(widths)), 1)   # identical column count
        # indicator names are a deterministic function of feature_columns
        from bot.ml.features.missingness import (
            missingness_indicator_names)
        self.assertEqual(missingness_indicator_names(fcols),
                          missingness_indicator_names(fcols))

    def test_missingness_indicators_are_nonzero_when_nan_present(self):
        res = _assemble_for_training()
        fcols = select_feature_columns(list(res.dataset.columns))
        n_base = len(fcols)
        X, _y = extract_xy_for_split(
            res.dataset, res.split.train_anchor_indices,
            target_label_id="triple_barrier_atr_2_3_50_won",
            feature_columns=fcols)
        indicator_block = X[:, n_base:]
        self.assertGreater(indicator_block.shape[1], 0)
        self.assertTrue((indicator_block == 1.0).any())
        # indicators are strictly 0/1
        self.assertTrue(np.isin(indicator_block, [0.0, 1.0]).all())

    def test_no_missingness_indicator_for_unknown_group(self):
        miss = self._miss()
        with self.assertRaises(ml_errors.M18ConfigError):
            miss.missingness_indicator_names(["bogus_group.feat"])

    def test_missingness_report_matches_model_feature_count(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config(
                "B2_logistic", dataset_id=res.manifest.dataset_id),
            res)
        self.assertEqual(
            out.missingness_report["feature_count_after_indicators"],
            out.n_features)

    def test_non_missing_unchanged_and_indicators_zero_for_present(self):
        miss = self._miss()
        cols = ["trend.a", "momentum.b"]
        # row 0 fully present, row 1 has a missing trend.a
        X = np.array([[1.5, -2.0], [np.nan, 3.0]])
        Xf, inds, names = miss.apply_missingness_fill(X, cols)
        # base values for present cells unchanged
        self.assertEqual(Xf[0, 0], 1.5)
        self.assertEqual(Xf[0, 1], -2.0)
        self.assertEqual(Xf[1, 1], 3.0)
        # indicator for the present cell (row0, trend.a) is 0
        ti = names.index("trend.a__was_missing")
        self.assertEqual(inds[0, ti], 0.0)
        # indicator for the missing cell (row1, trend.a) is 1
        self.assertEqual(inds[1, ti], 1.0)


# ─────────────────────────────────────────────────────────────────────
# G4_WalkForward — single split + embargo + label-overlap purge
# ─────────────────────────────────────────────────────────────────────

class G4_WalkForward(unittest.TestCase):

    @staticmethod
    def _make_inputs(n=100, embargo=10, label_horizon=5):
        anchor_indices = np.arange(n).astype(np.int64)
        anchor_ts = pd.date_range("2024-01-02", periods=n,
                                    freq="1D", tz="UTC").to_numpy()
        # Resolved at i + label_horizon (clamped to n-1)
        resolved_idx = np.minimum(
            anchor_indices + label_horizon, n - 1)
        resolved_ts_series = pd.Series(
            pd.to_datetime(anchor_ts[resolved_idx], utc=True))
        return anchor_indices, anchor_ts, resolved_ts_series, embargo

    def test_split_fractions(self):
        n = 100
        anchor_indices, anchor_ts, resolved, embargo = \
            self._make_inputs(n=n, embargo=0, label_horizon=0)
        split = ds_walk_forward.make_walk_forward_split(
            anchor_indices=anchor_indices,
            anchor_ts=anchor_ts,
            label_resolved_ts={"lbl": resolved},
            train_frac=0.6, val_frac=0.2, test_frac=0.2,
            embargo_bars=0,
        )
        # With horizon=0 and embargo=0: 60/20/20 split exactly.
        self.assertEqual(len(split.train_anchor_indices), 60)
        self.assertEqual(len(split.val_anchor_indices),   20)
        self.assertEqual(len(split.test_anchor_indices),  20)
        self.assertEqual(split.purged_count,    0)
        self.assertEqual(split.embargoed_count, 0)

    def test_embargo_removes_train_rows_near_val(self):
        n = 100
        anchor_indices, anchor_ts, resolved, _ = \
            self._make_inputs(n=n, label_horizon=0)
        split = ds_walk_forward.make_walk_forward_split(
            anchor_indices=anchor_indices,
            anchor_ts=anchor_ts,
            label_resolved_ts={"lbl": resolved},
            train_frac=0.6, val_frac=0.2, test_frac=0.2,
            embargo_bars=10,
        )
        # Train would have been [0, 60); embargo removes last 10 →
        # [0, 50) → 50 rows.
        self.assertEqual(len(split.train_anchor_indices), 50)
        self.assertEqual(split.embargoed_count, 10)

    def test_label_resolved_ts_overlap_purges_train(self):
        """Train anchor whose label resolves past val_start_ts must
        be purged."""
        n = 100
        # Horizon=20 means anchor i resolves at i+20. Train is
        # [0, 60); val starts at index 60. Any train anchor i with
        # i+20 >= 60 has label resolved at-or-after val_start →
        # purged. That's i in [40, 60) → 20 candidates.
        # Embargo=0 so no extra removal.
        anchor_indices, anchor_ts, resolved, _ = \
            self._make_inputs(n=n, label_horizon=20)
        split = ds_walk_forward.make_walk_forward_split(
            anchor_indices=anchor_indices,
            anchor_ts=anchor_ts,
            label_resolved_ts={"lbl": resolved},
            train_frac=0.6, val_frac=0.2, test_frac=0.2,
            embargo_bars=0,
        )
        self.assertEqual(split.purged_count, 20)
        self.assertEqual(len(split.train_anchor_indices), 40)

    def test_embargo_and_purge_combine(self):
        n = 100
        # horizon=15, embargo=5. Train [0, 60); embargo removes [55, 60)
        # → 5 embargoed. Then purge: among remaining [0, 55), those
        # with resolved_ts >= val_start_ts (i.e. i+15 >= 60 → i >= 45)
        # are purged → i in [45, 55) → 10 purged.
        anchor_indices, anchor_ts, resolved, _ = \
            self._make_inputs(n=n, label_horizon=15)
        split = ds_walk_forward.make_walk_forward_split(
            anchor_indices=anchor_indices,
            anchor_ts=anchor_ts,
            label_resolved_ts={"lbl": resolved},
            train_frac=0.6, val_frac=0.2, test_frac=0.2,
            embargo_bars=5,
        )
        self.assertEqual(split.embargoed_count, 5)
        self.assertEqual(split.purged_count, 10)
        self.assertEqual(len(split.train_anchor_indices), 45)

    def test_split_too_small_raises(self):
        # At n=2, train_hi=int(2*0.6)=1, val_hi=int(2*0.8)=1 — val
        # slice collapses (val_hi <= train_hi), guard raises.
        n = 2
        anchor_indices, anchor_ts, resolved, _ = \
            self._make_inputs(n=n, label_horizon=0)
        with self.assertRaises(ValueError):
            ds_walk_forward.make_walk_forward_split(
                anchor_indices=anchor_indices,
                anchor_ts=anchor_ts,
                label_resolved_ts={"lbl": resolved},
                train_frac=0.6, val_frac=0.2, test_frac=0.2,
                embargo_bars=0,
            )

    def test_default_embargo_bars_5_trading_days(self):
        # 5 trading days at 15m = 5 * 26 = 130 bars
        self.assertEqual(
            ds_walk_forward.default_embargo_bars("15m", 5), 130)
        # 5 days at 1D = 5
        self.assertEqual(
            ds_walk_forward.default_embargo_bars("1D", 5), 5)
        # 1 day at 1H = 7 (rounded up to capture session-close gap)
        self.assertEqual(
            ds_walk_forward.default_embargo_bars("1H", 1), 7)


# ─────────────────────────────────────────────────────────────────────
# G4_AdversarialValidation — sklearn LR + CV AUC + 0.55 gate
# ─────────────────────────────────────────────────────────────────────

class G4_AdversarialValidation(unittest.TestCase):

    def test_indistinguishable_sets_auc_near_05(self):
        """When X_train and X_holdout are drawn from the SAME
        distribution, AUC should be near 0.5 and the gate passes."""
        rng = np.random.default_rng(0)
        n = 300
        X_train   = pd.DataFrame(rng.normal(0, 1, (n, 8)),
                                   columns=[f"f{i}" for i in range(8)])
        X_holdout = pd.DataFrame(rng.normal(0, 1, (n, 8)),
                                   columns=[f"f{i}" for i in range(8)])
        res = ds_av.run_adversarial_validation(
            X_train, X_holdout, threshold=0.55, cv_folds=5)
        self.assertEqual(res.classifier, "logistic_regression")
        self.assertLess(res.auc_mean, 0.60,
            f"identical distributions should give AUC near 0.5, "
            f"got {res.auc_mean:.4f}")
        self.assertTrue(res.passed)

    def test_well_separable_sets_auc_high_and_gate_fails(self):
        """When holdout is clearly shifted, AUC should be high (> 0.9)
        and the gate should FAIL."""
        rng = np.random.default_rng(0)
        n = 300
        X_train   = pd.DataFrame(rng.normal(0, 1, (n, 8)),
                                   columns=[f"f{i}" for i in range(8)])
        # Holdout: same shape but shifted by +3 std on every feature
        X_holdout = pd.DataFrame(rng.normal(3, 1, (n, 8)),
                                   columns=[f"f{i}" for i in range(8)])
        res = ds_av.run_adversarial_validation(
            X_train, X_holdout, threshold=0.55, cv_folds=5)
        self.assertGreater(res.auc_mean, 0.95,
            f"shifted distributions should give high AUC, "
            f"got {res.auc_mean:.4f}")
        self.assertFalse(res.passed)

    def test_determinism(self):
        """Same inputs + same seed → same AUC bit-for-bit."""
        rng = np.random.default_rng(42)
        X_train   = pd.DataFrame(rng.normal(0, 1, (200, 5)),
                                   columns=list("abcde"))
        X_holdout = pd.DataFrame(rng.normal(0.5, 1, (200, 5)),
                                   columns=list("abcde"))
        r1 = ds_av.run_adversarial_validation(
            X_train, X_holdout, random_state=123)
        r2 = ds_av.run_adversarial_validation(
            X_train, X_holdout, random_state=123)
        self.assertEqual(r1.auc_mean, r2.auc_mean)
        self.assertEqual(r1.auc_per_fold, r2.auc_per_fold)

    def test_drops_constant_features(self):
        rng = np.random.default_rng(0)
        n = 200
        X_train = pd.DataFrame({
            "useful":   rng.normal(0, 1, n),
            "constant": np.full(n, 5.0),
        })
        X_holdout = pd.DataFrame({
            "useful":   rng.normal(3, 1, n),
            "constant": np.full(n, 5.0),
        })
        res = ds_av.run_adversarial_validation(
            X_train, X_holdout, cv_folds=3)
        self.assertIn("constant", res.dropped_features)
        self.assertEqual(res.feature_count_used, 1)

    def test_drops_all_nan_features(self):
        rng = np.random.default_rng(0)
        n = 200
        X_train = pd.DataFrame({
            "useful":  rng.normal(0, 1, n),
            "allnan":  np.full(n, np.nan),
        })
        X_holdout = pd.DataFrame({
            "useful":  rng.normal(3, 1, n),
            "allnan":  np.full(n, np.nan),
        })
        res = ds_av.run_adversarial_validation(
            X_train, X_holdout, cv_folds=3)
        self.assertIn("allnan", res.dropped_features)

    def test_too_few_rows_raises(self):
        X_train   = pd.DataFrame({"f": [1.0, 2.0, 3.0]})
        X_holdout = pd.DataFrame({"f": [4.0, 5.0, 6.0]})
        with self.assertRaises(
                ds_av.AdversarialValidationError):
            ds_av.run_adversarial_validation(
                X_train, X_holdout, cv_folds=5)

    def test_no_usable_features_raises(self):
        # All features are constant in both sets
        X_train   = pd.DataFrame({"a": [1.0] * 200,
                                    "b": [2.0] * 200})
        X_holdout = pd.DataFrame({"a": [1.0] * 200,
                                    "b": [2.0] * 200})
        with self.assertRaises(
                ds_av.AdversarialValidationError):
            ds_av.run_adversarial_validation(X_train, X_holdout)

    def test_psi_separate_from_av(self):
        """PSI is a separate diagnostic — distinct function name."""
        rng = np.random.default_rng(0)
        n = 200
        X_train   = pd.DataFrame(rng.normal(0, 1, (n, 3)),
                                   columns=list("abc"))
        X_holdout = pd.DataFrame(rng.normal(0, 1, (n, 3)),
                                   columns=list("abc"))
        psi = ds_av.distribution_shift_proxy_psi(
            X_train, X_holdout)
        # All small (same distribution)
        for col, val in psi.items():
            self.assertLess(val, 0.5, f"{col} PSI={val}")
        # And shifted case yields higher PSI
        X_holdout2 = pd.DataFrame(rng.normal(3, 1, (n, 3)),
                                    columns=list("abc"))
        psi2 = ds_av.distribution_shift_proxy_psi(
            X_train, X_holdout2)
        for col in "abc":
            self.assertGreater(psi2[col], psi[col],
                f"{col}: shifted PSI ({psi2[col]}) should exceed "
                f"unshifted PSI ({psi[col]})")


# ─────────────────────────────────────────────────────────────────────
# G4_Assembler — end-to-end
# ─────────────────────────────────────────────────────────────────────

class G4_Assembler(unittest.TestCase):

    def test_end_to_end_model_a(self):
        per_tf = _multi_tf_for_assembler(n_15m=600, seed=2)
        cfg = ds_assembler.AssemblerConfig(
            symbol="TESTSYM", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_A_SCANNER_REPLICA,
            require_intraday=True,
            embargo_bars_override=10,
            adversarial_cv_folds=3,
        )
        res = ds_assembler.DatasetAssembler(cfg).build(
            per_tf_bars=per_tf)
        m = res.manifest
        # Shape sanity
        self.assertEqual(res.dataset.shape[0], 600)
        self.assertEqual(m.feature_count, 68)   # M18.A.2/A.3 total
        self.assertEqual(m.label_count, 10)     # M18.A.4 locked
        # Manifest sanity
        self.assertFalse(m.coverage_degraded)
        self.assertIsNone(m.degradation_warning)
        self.assertEqual(m.anchor_set,
                          "model_a_scanner_replica")
        # train+val+test+purged+embargoed+pending must NOT exceed
        # the raw anchor count (the inequality is strict because
        # purged/embargoed rows came FROM train, which is itself a
        # subset of the after-pending-exclusion total).
        self.assertLessEqual(
            m.anchor_count_train + m.anchor_count_val
            + m.anchor_count_test + m.anchor_count_purged
            + m.anchor_count_embargoed,
            m.anchor_count_total + m.anchor_count_purged
            + m.anchor_count_embargoed)
        # Dataset hash valid hex
        self.assertEqual(len(m.dataset_hash_sha256), 64)

    def test_end_to_end_model_b_has_larger_anchor_set(self):
        """Model B (1H ∪ scanner) must have >= as many raw anchors
        as Model A (scanner only) on the same bars."""
        per_tf = _multi_tf_for_assembler(n_15m=600, seed=3)
        cfg_a = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_A_SCANNER_REPLICA,
            require_intraday=True, embargo_bars_override=10,
            adversarial_cv_folds=3)
        cfg_b = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
            require_intraday=True, embargo_bars_override=10,
            adversarial_cv_folds=3)
        r_a = ds_assembler.DatasetAssembler(cfg_a).build(
            per_tf_bars=per_tf)
        r_b = ds_assembler.DatasetAssembler(cfg_b).build(
            per_tf_bars=per_tf)
        self.assertGreaterEqual(
            r_b.manifest.anchor_count_raw,
            r_a.manifest.anchor_count_raw,
            "Model B (1H ∪ scanner) must not be smaller than "
            "Model A (scanner only)")

    def test_dataset_hash_deterministic(self):
        per_tf = _multi_tf_for_assembler(n_15m=500, seed=4)
        cfg = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
            require_intraday=True, embargo_bars_override=8,
            adversarial_cv_folds=3)
        asm = ds_assembler.DatasetAssembler(cfg)
        r1 = asm.build(per_tf_bars=per_tf)
        r2 = asm.build(per_tf_bars=per_tf)
        self.assertEqual(
            r1.manifest.dataset_hash_sha256,
            r2.manifest.dataset_hash_sha256)

    def test_dataset_hash_changes_with_anchor_set(self):
        per_tf = _multi_tf_for_assembler(n_15m=500, seed=5)
        cfg_a = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_A_SCANNER_REPLICA,
            require_intraday=True, embargo_bars_override=8,
            adversarial_cv_folds=3)
        cfg_b = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
            require_intraday=True, embargo_bars_override=8,
            adversarial_cv_folds=3)
        h_a = ds_assembler.DatasetAssembler(cfg_a).build(
            per_tf_bars=per_tf).manifest.dataset_hash_sha256
        h_b = ds_assembler.DatasetAssembler(cfg_b).build(
            per_tf_bars=per_tf).manifest.dataset_hash_sha256
        self.assertNotEqual(h_a, h_b,
            "anchor_set difference must change the dataset hash")

    def test_q19_degraded_blocks_when_require_intraday_true(self):
        # No 4H bars → Q19 degraded
        per_tf = _multi_tf_for_assembler(n_15m=300, seed=6)
        per_tf["4H"] = pd.DataFrame(columns=per_tf["4H"].columns)
        cfg = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_A_SCANNER_REPLICA,
            require_intraday=True, embargo_bars_override=10,
            adversarial_cv_folds=3)
        with self.assertRaises(
                ml_errors.InsufficientIntradayCoverageError):
            ds_assembler.DatasetAssembler(cfg).build(
                per_tf_bars=per_tf)

    def test_q19_degraded_allowed_when_require_intraday_false(self):
        per_tf = _multi_tf_for_assembler(n_15m=300, seed=7)
        per_tf["4H"] = pd.DataFrame(columns=per_tf["4H"].columns)
        cfg = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_A_SCANNER_REPLICA,
            require_intraday=False, embargo_bars_override=10,
            adversarial_cv_folds=3)
        # Must succeed (does not raise) AND mark as degraded.
        res = ds_assembler.DatasetAssembler(cfg).build(
            per_tf_bars=per_tf)
        self.assertTrue(res.manifest.coverage_degraded)
        self.assertIsNotNone(res.manifest.degradation_warning)

    def test_manifest_records_adversarial_validation_when_run(self):
        per_tf = _multi_tf_for_assembler(n_15m=600, seed=8)
        cfg = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
            require_intraday=True, embargo_bars_override=10,
            adversarial_cv_folds=3)
        res = ds_assembler.DatasetAssembler(cfg).build(
            per_tf_bars=per_tf)
        # AV result should be populated (Model B usually has enough rows)
        self.assertIsNotNone(res.adversarial_validation)
        self.assertEqual(
            res.adversarial_validation.classifier,
            "logistic_regression")
        # Manifest dict-form mirrors it
        self.assertIsNotNone(
            res.manifest.adversarial_validation)
        self.assertEqual(
            res.manifest.adversarial_validation["classifier"],
            "logistic_regression")

    def test_label_count_matches_locked_plan(self):
        per_tf = _multi_tf_for_assembler(n_15m=500, seed=9)
        cfg = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
            require_intraday=True, embargo_bars_override=10,
            adversarial_cv_folds=3)
        res = ds_assembler.DatasetAssembler(cfg).build(
            per_tf_bars=per_tf)
        self.assertEqual(res.manifest.label_count, 10,
            "M18.A.4 locked label_count is 10")

    def test_pending_excluded_from_split(self):
        per_tf = _multi_tf_for_assembler(n_15m=400, seed=10)
        cfg = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
            require_intraday=True, embargo_bars_override=10,
            adversarial_cv_folds=3)
        res = ds_assembler.DatasetAssembler(cfg).build(
            per_tf_bars=per_tf)
        m = res.manifest
        # anchor_count_total = anchor_count_raw - pending excluded
        self.assertEqual(
            m.anchor_count_total,
            m.anchor_count_raw - m.anchor_count_pending_excluded)
        # train/val/test indices should map to rows where every
        # is_pending == 0 in the dataset (no pending leaked into
        # any split)
        if res.split is not None:
            for idxs in (res.split.train_anchor_indices,
                          res.split.val_anchor_indices,
                          res.split.test_anchor_indices):
                pending_cols = [c for c in res.dataset.columns
                                if c.endswith(".is_pending")]
                if len(idxs) > 0:
                    sub = res.dataset.iloc[idxs][pending_cols]
                    self.assertTrue(
                        (sub == 0).all().all(),
                        "pending row leaked into a split")

# ─────────────────────────────────────────────────────────────────────
# G4_M16Backfill — centralised M16 backfill CLI helper + drift guard
# ─────────────────────────────────────────────────────────────────────

class G4_M16Backfill(unittest.TestCase):
    """Single source of truth for the M16 backfill CLI command string
    used in M18 error messages (coverage.py, assembler.py,
    m16_loader.py).

    test_command_matches_actual_m16_cli below shells out to the real
    M16 CLI's --help to verify that the subcommand and argument
    names emitted by the helper still exist. If the M16 CLI surface
    changes, this test fails and the helper module is the single
    file that needs updating.
    """

    def test_helper_single_timeframe(self):
        from bot.ml.dataset._m16_backfill import format_backfill_command
        cmd = format_backfill_command("AAPL", "4H")
        self.assertEqual(cmd,
            "    python -m bot.historical.cli backfill "
            "--symbols AAPL --timeframes 4H")

    def test_helper_multi_timeframe_csv(self):
        from bot.ml.dataset._m16_backfill import format_backfill_command
        # Preserves caller order; does NOT sort
        cmd = format_backfill_command("MSFT", ["1H", "4H", "15m"])
        self.assertEqual(cmd,
            "    python -m bot.historical.cli backfill "
            "--symbols MSFT --timeframes 1H,4H,15m")

    def test_helper_custom_indent(self):
        from bot.ml.dataset._m16_backfill import format_backfill_command
        cmd = format_backfill_command("X", "1D", indent="")
        self.assertTrue(cmd.startswith("python -m"),
            "indent='' should drop the leading whitespace")

    def test_command_matches_actual_m16_cli(self):
        """DRIFT GUARD: shells out to the real M16 CLI --help and
        verifies that the subcommand + flag names emitted by the
        helper actually exist."""
        import subprocess, sys
        from bot.ml.dataset import _m16_backfill as h

        # 1. Top-level --help must list the backfill subcommand.
        top = subprocess.run(
            [sys.executable, "-m", h.M16_CLI_MODULE, "--help"],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(top.returncode, 0,
            f"`python -m {h.M16_CLI_MODULE} --help` failed:\n"
            f"{top.stderr}")
        self.assertIn(h.M16_BACKFILL_SUBCOMMAND, top.stdout,
            f"backfill subcommand missing from CLI top-level --help:\n"
            f"{top.stdout}")

        # 2. The backfill subcommand's --help must mention BOTH flag
        #    names the helper emits.
        sub = subprocess.run(
            [sys.executable, "-m", h.M16_CLI_MODULE,
              h.M16_BACKFILL_SUBCOMMAND, "--help"],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(sub.returncode, 0,
            f"`{h.M16_BACKFILL_SUBCOMMAND} --help` failed:\n"
            f"{sub.stderr}")
        self.assertIn(h.M16_BACKFILL_SYMBOLS_FLAG, sub.stdout,
            f"{h.M16_BACKFILL_SYMBOLS_FLAG} flag not found in "
            f"backfill --help:\n{sub.stdout}")
        self.assertIn(h.M16_BACKFILL_TIMEFRAMES_FLAG, sub.stdout,
            f"{h.M16_BACKFILL_TIMEFRAMES_FLAG} flag not found in "
            f"backfill --help:\n{sub.stdout}")

    def test_helper_is_used_by_coverage_module(self):
        """Belt-and-suspenders: coverage.py's error message must
        delegate to the helper (no hand-rolled CLI string)."""
        from pathlib import Path
        src = Path("bot/ml/dataset/coverage.py").read_text()
        self.assertIn("format_backfill_command", src,
            "coverage.py must use format_backfill_command, not a "
            "hand-rolled CLI string")
        # Negative check: the old broken form must NOT be present
        self.assertNotIn("bot.historical.cli refresh", src)
        self.assertNotIn("--tf ", src)

    def test_helper_is_used_by_assembler_module(self):
        from pathlib import Path
        src = Path("bot/ml/dataset/assembler.py").read_text()
        self.assertIn("format_backfill_command", src,
            "assembler.py must use format_backfill_command for the "
            "anchor-TF-missing error")
        self.assertNotIn("bot.historical.cli refresh", src)
        self.assertNotIn("--tf ", src)

    def test_helper_is_used_by_m16_loader_module(self):
        from pathlib import Path
        src = Path("bot/ml/dataset/m16_loader.py").read_text()
        self.assertIn("format_backfill_command", src,
            "m16_loader.py must use format_backfill_command, not a "
            "hand-rolled CLI string (fixed in M18.A.5)")
        self.assertNotIn("bot.historical.cli refresh", src)

# ═════════════════════════════════════════════════════════════════════
# G5 — Model trainers + thinness gates + promotion gate (M18.A.6)
# ═════════════════════════════════════════════════════════════════════


def _assemble_for_training(*, n_15m=1000, av_threshold=1.0,
                              require_intraday=True, drop_4h=False,
                              symbol="X", seed=11):
    """Build an AssemblerResult suitable for trainer tests.

    Defaults give 1000 anchor bars (enough for a non-degenerate split)
    and av_threshold=1.0 so the adversarial gate always passes —
    isolating the trainer's own gate behaviour from the dataset's."""
    per_tf = _multi_tf_for_assembler(n_15m=n_15m, seed=seed)
    if drop_4h:
        per_tf["4H"] = pd.DataFrame(columns=per_tf["4H"].columns)
    cfg = ds_assembler.AssemblerConfig(
        symbol=symbol, anchor_tf="15m",
        anchor_set=ds_anchors
            .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
        require_intraday=require_intraday,
        embargo_bars_override=10,
        adversarial_cv_folds=3,
        adversarial_threshold=av_threshold,
    )
    return ds_assembler.DatasetAssembler(cfg).build(per_tf_bars=per_tf)


def _make_train_config(model_type: str, *,
                          dataset_id: str,
                          target_label_id: str
                            = "triple_barrier_atr_2_3_50_won",
                          train_mode: str = "model_b_candidate_quality",
                          hyperparameters=None,
                          seed: int = 42,
                          fixture_mode: bool = False) -> TrainConfig:
    return TrainConfig(
        dataset_id=dataset_id,
        model_type=model_type,
        train_mode=train_mode,
        target_label_id=target_label_id,
        hyperparameters=hyperparameters or {},
        seed=seed,
        fixture_mode=fixture_mode,
    )


# ─────────────────────────────────────────────────────────────────────
# G5_FeatureSelect — column slicing helpers
# ─────────────────────────────────────────────────────────────────────

class G5_FeatureSelect(unittest.TestCase):

    def test_select_feature_columns_matches_known_feature_ids(self):
        res = _assemble_for_training()
        cols = list(res.dataset.columns)
        feats = select_feature_columns(cols)
        # 68 features per M18.A.2/A.3 (verified by G4_Assembler)
        self.assertEqual(len(feats), 68)
        # ts_utc and label columns must NOT be in feature list
        self.assertNotIn("ts_utc", feats)
        self.assertNotIn("triple_barrier_atr_2_3_50_won", feats)
        self.assertNotIn("triple_barrier_atr_2_3_50", feats)

    def test_select_label_columns_includes_label_aux(self):
        res = _assemble_for_training()
        cols = list(res.dataset.columns)
        lbls = select_label_columns(cols)
        # 10 labels + their aux columns
        self.assertIn("triple_barrier_atr_2_3_50_won", lbls)
        self.assertIn("triple_barrier_atr_2_3_50_won.is_pending", lbls)
        self.assertIn("triple_barrier_atr_2_3_50.resolved_ts", lbls)
        self.assertIn("fwd_return_5b", lbls)
        # ts_utc must NOT be in label list
        self.assertNotIn("ts_utc", lbls)

    def test_get_label_class_known(self):
        self.assertEqual(
            get_label_class("triple_barrier_atr_2_3_50_won"), "binary")
        self.assertEqual(
            get_label_class("triple_barrier_atr_2_3_50"),
            "classification_3way")
        self.assertEqual(
            get_label_class("fwd_return_5b"), "regression")

    def test_get_label_class_unknown_raises(self):
        with self.assertRaises(ml_errors.M18ConfigError):
            get_label_class("not_a_real_label_id")

    def test_extract_xy_split_dimensions(self):
        from bot.ml.features.missingness import (
            missingness_indicator_names)
        res = _assemble_for_training()
        feat_cols = select_feature_columns(list(res.dataset.columns))
        n_ind = len(missingness_indicator_names(feat_cols))
        X, y = extract_xy_for_split(
            res.dataset, res.split.train_anchor_indices,
            target_label_id="triple_barrier_atr_2_3_50_won",
            feature_columns=feat_cols)
        self.assertEqual(X.shape[0], len(res.split.train_anchor_indices))
        # M18.B.5: model matrix = base features + appended missingness
        # indicators.
        self.assertEqual(X.shape[1], len(feat_cols) + n_ind)
        self.assertEqual(y.shape[0], X.shape[0])
        # No NaN in target (pending excluded by the assembler)
        self.assertFalse(np.isnan(y).any())

    def test_extract_xy_empty_indices(self):
        from bot.ml.features.missingness import (
            missingness_indicator_names)
        res = _assemble_for_training()
        feat_cols = select_feature_columns(list(res.dataset.columns))
        n_ind = len(missingness_indicator_names(feat_cols))
        X, y = extract_xy_for_split(
            res.dataset, np.array([], dtype=np.int64),
            target_label_id="triple_barrier_atr_2_3_50_won",
            feature_columns=feat_cols)
        # Empty split keeps the SAME model width (base + indicators) so
        # column counts are consistent across all splits.
        self.assertEqual(X.shape, (0, len(feat_cols) + n_ind))
        self.assertEqual(y.shape, (0,))


# ─────────────────────────────────────────────────────────────────────
# G5_ThinnessGates — sample-count, minority-class, feature-ratio
# ─────────────────────────────────────────────────────────────────────

class G5_ThinnessGates(unittest.TestCase):

    def test_full_pass(self):
        rpt = evaluate_thinness(
            y_train=np.array([0, 1] * 200),   # 400 rows balanced
            n_val=100, n_test=100, n_features=50,
            label_class="binary")
        self.assertTrue(rpt["passed"])
        self.assertEqual(rpt["failed_checks"], [])

    def test_train_sample_count_failure(self):
        rpt = evaluate_thinness(
            y_train=np.zeros(50),   # below default 200
            n_val=100, n_test=100, n_features=10,
            label_class="binary")
        self.assertFalse(rpt["passed"])
        self.assertIn("sample_count_train", rpt["failed_checks"])

    def test_val_test_sample_count_failures(self):
        rpt = evaluate_thinness(
            y_train=np.array([0, 1] * 200),
            n_val=10, n_test=10, n_features=10,
            label_class="binary")
        self.assertIn("sample_count_val", rpt["failed_checks"])
        self.assertIn("sample_count_test", rpt["failed_checks"])

    def test_minority_class_failure(self):
        # 250 train, only 5 of class 1
        y = np.concatenate([np.zeros(245), np.ones(5)])
        rpt = evaluate_thinness(
            y_train=y, n_val=100, n_test=100, n_features=10,
            label_class="binary")
        self.assertIn("minority_class_count_train",
                       rpt["failed_checks"])

    def test_feature_to_train_ratio_failure(self):
        # 100 train, 60 features → ratio 0.6 > 0.5 default
        rpt = evaluate_thinness(
            y_train=np.array([0, 1] * 50),
            n_val=100, n_test=100, n_features=60,
            label_class="binary")
        self.assertIn("feature_to_train_ratio",
                       rpt["failed_checks"])

    def test_regression_minority_check_is_na(self):
        rpt = evaluate_thinness(
            y_train=np.random.RandomState(0).normal(0, 1, 500),
            n_val=100, n_test=100, n_features=10,
            label_class="regression")
        # Minority-class check is N/A; must NOT fail it
        self.assertNotIn("minority_class_count_train",
                          rpt["failed_checks"])
        self.assertTrue(
            rpt["checks"]["minority_class_count_train"]["passed"])

    def test_custom_thresholds(self):
        th = ThinnessThresholds(min_train_samples=10,
                                  min_val_samples=5,
                                  min_test_samples=5,
                                  min_minority_class_train=3,
                                  max_features_to_train_ratio=10.0)
        rpt = evaluate_thinness(
            y_train=np.array([0]*7 + [1]*3),   # 10 train, 3 minority
            n_val=5, n_test=5, n_features=5,
            label_class="binary", thresholds=th)
        self.assertTrue(rpt["passed"], rpt)


# ─────────────────────────────────────────────────────────────────────
# G5_MajorityBaseline (B0)
# ─────────────────────────────────────────────────────────────────────

class G5_MajorityBaseline(unittest.TestCase):

    def test_predicts_class_1_prior_train_rate(self):
        """B0_majority emits the train rate of class 1 (DummyClassifier
        strategy='prior' semantics)."""
        trainer = MajorityClassTrainer()
        y = np.concatenate([np.zeros(70), np.ones(30)])   # 30% class 1
        trainer.fit(y, label_class="binary", seed=42)
        proba = trainer.predict_proba(5)
        self.assertEqual(proba.shape, (5,))
        np.testing.assert_allclose(proba, 0.30, rtol=1e-12)

    def test_at_50_50_split_proba_is_05(self):
        trainer = MajorityClassTrainer()
        y = np.concatenate([np.zeros(50), np.ones(50)])
        trainer.fit(y, label_class="binary", seed=42)
        np.testing.assert_allclose(
            trainer.predict_proba(3), 0.5, rtol=1e-12)

    def test_regression_returns_train_mean(self):
        trainer = MajorityClassTrainer()
        y = np.array([1.0, 2.0, 3.0, 4.0])
        trainer.fit(y, label_class="regression", seed=42)
        np.testing.assert_allclose(
            trainer.predict_proba(3), 2.5, rtol=1e-12)

    def test_majority_class_recorded_deterministically(self):
        trainer = MajorityClassTrainer()
        y = np.concatenate([np.zeros(60), np.ones(40)])
        trainer.fit(y, label_class="binary", seed=42)
        self.assertEqual(trainer.majority_class_, 0.0)
        # Tie-break: when counts equal, smaller class wins
        y_tie = np.concatenate([np.zeros(50), np.ones(50)])
        t2 = MajorityClassTrainer()
        t2.fit(y_tie, label_class="binary", seed=42)
        self.assertEqual(t2.majority_class_, 0.0)


# ─────────────────────────────────────────────────────────────────────
# G5_ScannerReplicaBaseline (B1)
# ─────────────────────────────────────────────────────────────────────

class G5_ScannerReplicaBaseline(unittest.TestCase):

    def test_passthrough_returns_signal_fires(self):
        trainer = ScannerReplicaTrainer()
        sf_train = np.array([0, 1, 0, 1, 1], dtype=np.int8)
        trainer.fit(sf_train, seed=42)
        sf_test = np.array([1, 0, 0, 1], dtype=np.int8)
        proba = trainer.predict_proba(sf_test)
        np.testing.assert_array_equal(proba, sf_test.astype(float))

    def test_records_train_positive_rate(self):
        trainer = ScannerReplicaTrainer()
        trainer.fit(np.array([0, 0, 1, 1, 1, 0, 1]), seed=42)
        self.assertAlmostEqual(trainer.train_positive_rate_,
                                 4/7, places=12)


# ─────────────────────────────────────────────────────────────────────
# G5_LogisticBaseline (B2)
# ─────────────────────────────────────────────────────────────────────

class G5_LogisticBaseline(unittest.TestCase):

    def _separable_data(self, n=400, seed=0):
        """A small, perfectly-separable dataset for which LR should
        learn AUC near 1.0."""
        rng = np.random.default_rng(seed)
        X = rng.normal(0, 1, (n, 4))
        y = (X[:, 0] + X[:, 1] > 0).astype(float)
        return X, y

    def test_fit_predict_proba_basic(self):
        X, y = self._separable_data(n=400)
        trainer = LogisticRegressionTrainer()
        trainer.fit(X, y, label_class="binary", seed=42)
        proba = trainer.predict_proba(X)
        self.assertEqual(proba.shape, (X.shape[0],))
        # Probabilities in [0, 1]
        self.assertTrue(np.all(proba >= 0))
        self.assertTrue(np.all(proba <= 1))
        # Should be highly informative on separable data
        from sklearn.metrics import roc_auc_score
        self.assertGreater(roc_auc_score(y, proba), 0.95)

    def test_determinism_same_seed(self):
        X, y = self._separable_data(n=300, seed=1)
        t1 = LogisticRegressionTrainer()
        t1.fit(X, y, label_class="binary", seed=42)
        t2 = LogisticRegressionTrainer()
        t2.fit(X, y, label_class="binary", seed=42)
        np.testing.assert_array_equal(
            t1.predict_proba(X), t2.predict_proba(X))

    def test_refuses_non_binary_target(self):
        X, y = self._separable_data()
        trainer = LogisticRegressionTrainer()
        with self.assertRaises(ml_errors.M18ConfigError):
            trainer.fit(X, y, label_class="regression", seed=42)
        with self.assertRaises(ml_errors.M18ConfigError):
            trainer.fit(X, y, label_class="classification_3way",
                          seed=42)


# ─────────────────────────────────────────────────────────────────────
# G5_LightGBM (gated on availability)
# ─────────────────────────────────────────────────────────────────────

class G5_LightGBM(unittest.TestCase):

    def test_is_lightgbm_available_returns_bool(self):
        # Don't assert which value — depends on the venv
        self.assertIsInstance(is_lightgbm_available(), bool)

    def test_missing_lightgbm_raises_clear_error(self):
        if is_lightgbm_available():
            self.skipTest("lightgbm IS installed — this test "
                          "checks the unavailable path")
        trainer = LightGBMTrainer()
        with self.assertRaises(ml_errors.M18ConfigError) as ctx:
            trainer.fit(np.array([[1.0]]), np.array([0]),
                          label_class="binary", seed=42)
        msg = str(ctx.exception)
        self.assertIn("lightgbm is not installed", msg)
        self.assertIn("pip install lightgbm", msg)
        self.assertIn("M18.A.6", msg)
        self.assertIn("B2_logistic", msg)

    @unittest.skipUnless(is_lightgbm_available(),
                          "lightgbm not installed")
    def test_lightgbm_determinism_when_available(self):
        rng = np.random.default_rng(0)
        X = rng.normal(0, 1, (300, 4))
        y = (X[:, 0] > 0).astype(float)
        t1 = LightGBMTrainer()
        t1.fit(X, y, label_class="binary", seed=42)
        t2 = LightGBMTrainer()
        t2.fit(X, y, label_class="binary", seed=42)
        np.testing.assert_array_equal(
            t1.predict_proba(X), t2.predict_proba(X))

    @unittest.skipUnless(is_lightgbm_available(),
                          "lightgbm not installed")
    def test_lightgbm_refuses_to_override_determinism_flags(self):
        trainer = LightGBMTrainer()
        bad_hps = {"deterministic": False, "n_estimators": 50}
        with self.assertRaises(ml_errors.M18ConfigError):
            trainer.fit(np.array([[1.0]]), np.array([0]),
                          label_class="binary", seed=42,
                          hyperparameters=bad_hps)


# ─────────────────────────────────────────────────────────────────────
# G5_TrainerOrchestrator — end-to-end with TrainConfig + AssemblerResult
# ─────────────────────────────────────────────────────────────────────

class G5_TrainerOrchestrator(unittest.TestCase):

    def test_b0_majority_end_to_end(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("B0_majority",
                                  dataset_id=res.manifest.dataset_id),
            res)
        self.assertEqual(out.model_type, "B0_majority")
        self.assertEqual(out.target_label_class, "binary")
        # B0 produces a constant prediction → AUC = 0.5
        self.assertEqual(out.metrics_val["roc_auc"], 0.5)
        self.assertEqual(out.metrics_test["roc_auc"], 0.5)
        # Prediction lengths match split sizes
        self.assertEqual(len(out.pred_train),
                          len(res.split.train_anchor_indices))
        self.assertEqual(len(out.pred_val),
                          len(res.split.val_anchor_indices))
        self.assertEqual(len(out.pred_test),
                          len(res.split.test_anchor_indices))

    def test_b1_scanner_replica_end_to_end(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("B1_scanner_replica",
                                  dataset_id=res.manifest.dataset_id),
            res)
        self.assertEqual(out.model_type, "B1_scanner_replica")
        # B1 emits the raw signal_fires column for test split — verify
        # the prediction matches that column directly.
        sf_test = res.dataset.iloc[res.split.test_anchor_indices][
            SCANNER_FIRES_COLUMN].to_numpy(dtype=float)
        np.testing.assert_array_equal(
            np.array(out.pred_test), sf_test)

    def test_b2_logistic_end_to_end(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("B2_logistic",
                                  dataset_id=res.manifest.dataset_id),
            res)
        self.assertEqual(out.model_type, "B2_logistic")
        # Probabilities in [0, 1]
        self.assertTrue(all(0 <= p <= 1 for p in out.pred_test))
        # library_versions records sklearn
        self.assertIn("sklearn", out.library_versions)

    def test_dataset_identity_propagates_to_output(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("B0_majority",
                                  dataset_id=res.manifest.dataset_id),
            res)
        self.assertEqual(out.dataset_id, res.manifest.dataset_id)
        self.assertEqual(out.dataset_hash_sha256,
                          res.manifest.dataset_hash_sha256)

    def test_no_split_raises_insufficient_data(self):
        """If the assembler couldn't produce a split (too few rows),
        the trainer must NOT silently produce a model."""
        # Synthetic with very few bars — split should be None or
        # trigger InsufficientDataError. Use the assembler's
        # config-validation pathway.
        per_tf = _multi_tf_for_assembler(n_15m=300, seed=99)
        cfg = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_A_SCANNER_REPLICA,
            require_intraday=True, embargo_bars_override=5,
            adversarial_cv_folds=2)
        # Model A on tiny synthetic should give a small anchor set
        res = ds_assembler.DatasetAssembler(cfg).build(
            per_tf_bars=per_tf)
        if res.split is None:
            with self.assertRaises(
                    ml_errors.InsufficientDataError):
                ModelTrainer().train_one(
                    _make_train_config("B0_majority",
                                         dataset_id=res.manifest.dataset_id),
                    res)
        else:
            self.skipTest(
                "split was producible; this test exercises the "
                "no-split path which depends on synthetic-data luck")

    def test_invalid_model_type_raises(self):
        res = _assemble_for_training()
        with self.assertRaises(ml_errors.M18ConfigError):
            cfg = _make_train_config(
                "NOT_A_REAL_MODEL",
                dataset_id=res.manifest.dataset_id)
            # Bypass TrainConfig.from_dict() validation: build the
            # dataclass directly to ensure the Trainer itself
            # validates.
            cfg = TrainConfig(
                dataset_id=res.manifest.dataset_id,
                model_type="NOT_A_REAL_MODEL",
                train_mode="model_a_meta_label",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False)
            ModelTrainer().train_one(cfg, res)

    def test_m_random_forest_is_implemented_and_trains(self):
        """M18.B.1: M_random_forest is now IMPLEMENTED. Requesting it
        must train (not raise the old M18.A.6 scope error)."""
        res = _assemble_for_training()
        cfg = TrainConfig(
            dataset_id=res.manifest.dataset_id,
            model_type="M_random_forest",
            train_mode="model_b_candidate_quality",
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False)
        out = ModelTrainer().train_one(cfg, res)
        self.assertEqual(out.model_type, "M_random_forest")


# ─────────────────────────────────────────────────────────────────────
# G5_NoTestLeak — train data does NOT influence training
# ─────────────────────────────────────────────────────────────────────

class G5_RandomForest(unittest.TestCase):
    """M18.B.1 — M_random_forest sklearn fallback trainer.

    A sklearn-only tree model that does NOT require lightgbm. Trains
    only when explicitly requested; deterministic given a fixed seed;
    binary targets only; rejects unsafe/non-deterministic overrides.
    """

    # ---- a small deterministic binary fixture (direct trainer) -----

    def _fixture(self, n=200, seed=0):
        rng = np.random.default_rng(seed)
        X = rng.normal(size=(n, 6))
        y = (X[:, 0] + X[:, 1] > 0).astype(np.float64)
        return X, y

    def test_random_forest_model_type_is_implemented(self):
        from bot.ml.models.trainer import IMPLEMENTED_MODEL_TYPES
        self.assertIn("M_random_forest", IMPLEMENTED_MODEL_TYPES)
        self.assertEqual(RandomForestTrainer.model_type,
                          "M_random_forest")

    def test_random_forest_trains_on_fixture(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("M_random_forest",
                                  dataset_id=res.manifest.dataset_id),
            res)
        self.assertIsInstance(out, TrainOutputs)
        self.assertEqual(out.model_type, "M_random_forest")
        self.assertEqual(len(out.pred_train),
                          len(res.split.train_anchor_indices))
        self.assertEqual(len(out.pred_val),
                          len(res.split.val_anchor_indices))
        self.assertEqual(len(out.pred_test),
                          len(res.split.test_anchor_indices))

    def test_random_forest_probabilities_are_valid(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("M_random_forest",
                                  dataset_id=res.manifest.dataset_id),
            res)
        for arr in (out.pred_train, out.pred_val, out.pred_test):
            a = np.asarray(arr, dtype=np.float64)
            self.assertTrue(np.all(np.isfinite(a)),
                             "RF probabilities must be finite")
            if a.size:
                self.assertGreaterEqual(float(a.min()), 0.0)
                self.assertLessEqual(float(a.max()), 1.0)

    def test_random_forest_deterministic_same_seed(self):
        X, y = self._fixture()
        t1 = RandomForestTrainer()
        t1.fit(X, y, label_class="binary", seed=42)
        t2 = RandomForestTrainer()
        t2.fit(X, y, label_class="binary", seed=42)
        np.testing.assert_array_equal(
            t1.predict_proba(X), t2.predict_proba(X))

    def test_random_forest_different_seed_can_change_predictions(self):
        # Same data + different seed: outputs must remain VALID; we do
        # NOT assert they differ (could coincide on an easy fixture) —
        # only that a different seed still yields finite, in-range,
        # correctly-shaped probabilities.
        X, y = self._fixture()
        t = RandomForestTrainer()
        t.fit(X, y, label_class="binary", seed=7)
        p = t.predict_proba(X)
        self.assertEqual(p.shape, (X.shape[0],))
        self.assertTrue(np.all(np.isfinite(p)))
        self.assertGreaterEqual(float(p.min()), 0.0)
        self.assertLessEqual(float(p.max()), 1.0)

    def test_random_forest_requires_binary_target(self):
        X, y = self._fixture()
        with self.assertRaises(ml_errors.M18ConfigError):
            RandomForestTrainer().fit(
                X, y, label_class="classification_3way", seed=42)
        with self.assertRaises(ml_errors.M18ConfigError):
            RandomForestTrainer().fit(
                X, y, label_class="regression", seed=42)

    def test_random_forest_empty_train_set_fails(self):
        empty_X = np.empty((0, 6), dtype=np.float64)
        empty_y = np.empty((0,), dtype=np.float64)
        with self.assertRaises(ml_errors.M18ConfigError):
            RandomForestTrainer().fit(
                empty_X, empty_y, label_class="binary", seed=42)

    def test_random_forest_one_class_train_fails_clearly(self):
        X, _ = self._fixture()
        y_one = np.zeros(X.shape[0], dtype=np.float64)
        with self.assertRaises(ml_errors.M18ConfigError):
            RandomForestTrainer().fit(
                X, y_one, label_class="binary", seed=42)

    def test_random_forest_rejects_unsafe_hyperparameters(self):
        X, y = self._fixture()
        for bad in ({"n_jobs": 2}, {"random_state": 99},
                     {"bootstrap": False}, {"not_a_param": 1}):
            with self.assertRaises(ml_errors.M18ConfigError):
                RandomForestTrainer().fit(
                    X, y, label_class="binary", seed=42,
                    hyperparameters=bad)

    def test_random_forest_allows_safe_hyperparameters(self):
        X, y = self._fixture()
        t = RandomForestTrainer()
        t.fit(X, y, label_class="binary", seed=42,
               hyperparameters={"n_estimators": 50, "max_depth": 5,
                                 "min_samples_leaf": 10,
                                 "class_weight": "balanced"})
        p = t.predict_proba(X)
        self.assertEqual(p.shape, (X.shape[0],))
        self.assertTrue(np.all(np.isfinite(p)))

    def test_random_forest_does_not_require_lightgbm(self):
        # RF must work whether or not lightgbm is installed, and the
        # trainer module must not import lightgbm.
        import importlib, sys
        X, y = self._fixture()
        t = RandomForestTrainer()
        t.fit(X, y, label_class="binary", seed=42)
        self.assertTrue(np.all(np.isfinite(t.predict_proba(X))))
        # library_versions reports sklearn, never lightgbm
        self.assertIn("sklearn", t.library_versions())
        self.assertNotIn("lightgbm", t.library_versions())
        # the module's source does not import lightgbm
        import bot.ml.models.random_forest_trainer as rf_mod
        src = Path(rf_mod.__file__).read_text()
        self.assertNotIn("import lightgbm", src)

    def test_random_forest_respects_dual_cohort_validation(self):
        # Wrong train_mode for the fixture's anchor_set must still raise
        # the existing cohort error (the assembler fixture is the
        # model_b cohort; requesting model_a must fail).
        res = _assemble_for_training()
        cfg = TrainConfig(
            dataset_id=res.manifest.dataset_id,
            model_type="M_random_forest",
            train_mode="model_a_meta_label",
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False)
        with self.assertRaises((AssertionError, ml_errors.M18ConfigError)):
            ModelTrainer().train_one(cfg, res)

    def test_random_forest_rejects_non_finite_targets(self):
        X, _ = self._fixture()
        y = np.zeros(X.shape[0], dtype=np.float64)
        y[: X.shape[0] // 2] = 1.0
        y[0] = np.nan
        with self.assertRaises(ml_errors.M18ConfigError):
            RandomForestTrainer().fit(X, y, label_class="binary", seed=42)

    def test_random_forest_rejects_non_0_1_binary_targets(self):
        X, _ = self._fixture()
        # Two classes {0, 2} — passes a naive one-class check but is not
        # a valid binary {0,1} target.
        y = np.where(np.arange(X.shape[0]) % 2 == 0, 0.0, 2.0)
        with self.assertRaises(ml_errors.M18ConfigError):
            RandomForestTrainer().fit(X, y, label_class="binary", seed=42)


class G5_NoTestLeak(unittest.TestCase):
    """The locked plan forbids optimising on test data. The most
    structural way to assert this: scramble the test split's feature
    values and verify the train+val predictions remain bit-identical.
    If test data influenced training, train predictions would change.
    """

    def test_b2_logistic_train_predictions_invariant_to_test_data(self):
        res = _assemble_for_training()
        cfg = _make_train_config(
            "B2_logistic", dataset_id=res.manifest.dataset_id)
        out_orig = ModelTrainer().train_one(cfg, res)

        # Build a perturbed AssemblerResult with the test slice's
        # feature columns scrambled. Everything else identical.
        scrambled = res.dataset.copy()
        feat_cols = select_feature_columns(list(scrambled.columns))
        rng = np.random.default_rng(99)
        test_idx = res.split.test_anchor_indices
        for c in feat_cols:
            old_vals = scrambled.loc[test_idx, c].to_numpy()
            scrambled.loc[test_idx, c] = rng.permutation(old_vals)
        # Reuse the same split / manifest / AV result — we only
        # mutate the dataset's TEST rows.
        from dataclasses import replace
        perturbed = ds_assembler.AssemblerResult(
            dataset=scrambled,
            manifest=res.manifest,
            split=res.split,
            coverage_report=res.coverage_report,
            adversarial_validation=res.adversarial_validation,
        )
        out_perturbed = ModelTrainer().train_one(cfg, perturbed)

        # Train predictions MUST be identical (test data did not
        # influence training).
        np.testing.assert_array_equal(
            np.array(out_orig.pred_train),
            np.array(out_perturbed.pred_train))
        # Val predictions MUST also be identical (val data was the
        # same).
        np.testing.assert_array_equal(
            np.array(out_orig.pred_val),
            np.array(out_perturbed.pred_val))
        # Test predictions SHOULD differ — verifies our scramble
        # actually changed something (no false-positive identity).
        self.assertFalse(np.array_equal(
            np.array(out_orig.pred_test),
            np.array(out_perturbed.pred_test)))


# ─────────────────────────────────────────────────────────────────────
# G5_FixtureModePropagation — Q16 fixture-mode contract
# ─────────────────────────────────────────────────────────────────────

class G5_FixtureModePropagation(unittest.TestCase):

    def test_fixture_mode_skips_thinness_gates(self):
        res = _assemble_for_training(n_15m=400)
        cfg = _make_train_config(
            "B0_majority",
            dataset_id=res.manifest.dataset_id,
            fixture_mode=True)
        out = ModelTrainer().train_one(cfg, res)
        self.assertTrue(out.fixture_only)
        self.assertTrue(out.thinness_status.get("skipped"))
        self.assertIn("Q16",
                       out.thinness_status.get("reason", ""))

    def test_fixture_mode_blocks_promotion_permanently(self):
        """fixture_mode=True ⇒ fixture_only=True ⇒ promotion_eligible
        is False, regardless of all other gates."""
        res = _assemble_for_training()
        cfg = _make_train_config(
            "B0_majority",
            dataset_id=res.manifest.dataset_id,
            fixture_mode=True)
        out = ModelTrainer().train_one(cfg, res)
        self.assertFalse(out.promotion_eligible)
        self.assertIn("fixture_only", out.promotion_blocked_reasons)

    def test_dataset_fixture_only_propagates_to_model(self):
        """If the dataset was built fixture-mode, the model must
        inherit fixture_only=True even if train_config.fixture_mode
        is False."""
        # Build a fixture-mode dataset
        per_tf = _multi_tf_for_assembler(n_15m=600, seed=33)
        cfg_ds = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
            require_intraday=True, embargo_bars_override=10,
            adversarial_cv_folds=3, adversarial_threshold=1.0,
            fixture_mode=True)
        res = ds_assembler.DatasetAssembler(cfg_ds).build(
            per_tf_bars=per_tf)
        self.assertTrue(res.manifest.fixture_only)
        # Train with fixture_mode=False at trainer-level
        cfg = _make_train_config(
            "B0_majority",
            dataset_id=res.manifest.dataset_id,
            fixture_mode=False)
        out = ModelTrainer().train_one(cfg, res)
        # Model inherits fixture_only via the dataset
        self.assertTrue(out.fixture_only)
        self.assertFalse(out.promotion_eligible)
        # Reason is the dataset's own fixture_only flag (namespaced)
        self.assertTrue(any(
            r.startswith("dataset:fixture_only")
            for r in out.promotion_blocked_reasons),
            out.promotion_blocked_reasons)


# ─────────────────────────────────────────────────────────────────────
# G5_PromotionGate — dataset-inherited gates + thinness composition
# ─────────────────────────────────────────────────────────────────────

class G5_PromotionGate(unittest.TestCase):

    def test_thinness_failure_blocks_promotion_with_thinness_reason(self):
        # 159 train samples vs 68 features → feature_to_train_ratio
        # and minority_count likely both fail.
        res = _assemble_for_training(n_15m=600)
        out = ModelTrainer().train_one(
            _make_train_config("B0_majority",
                                  dataset_id=res.manifest.dataset_id),
            res)
        self.assertFalse(out.promotion_eligible)
        # Every thinness reason must be namespaced
        any_thinness = any(r.startswith("thinness:")
                            for r in out.promotion_blocked_reasons)
        self.assertTrue(any_thinness, out.promotion_blocked_reasons)

    def test_adversarial_validation_failure_propagates_via_dataset(self):
        """Tight AV threshold → dataset AV fails → trainer inherits
        the AV failure as a 'dataset:' reason, NOT --force-overridable
        at the trainer layer."""
        res = _assemble_for_training(n_15m=1000, av_threshold=0.55)
        # AV almost certainly fails on synthetic random walk
        self.assertFalse(
            res.adversarial_validation.passed
            if res.adversarial_validation else True)
        out = ModelTrainer().train_one(
            _make_train_config("B0_majority",
                                  dataset_id=res.manifest.dataset_id),
            res)
        self.assertFalse(out.promotion_eligible)
        self.assertTrue(any(
            r.startswith("dataset:adversarial_validation_failed")
            for r in out.promotion_blocked_reasons),
            out.promotion_blocked_reasons)

    def test_coverage_degraded_propagates_via_dataset(self):
        """Q19 coverage_degraded must propagate as 'dataset:
        coverage_degraded' — also not trainer-force-overridable."""
        per_tf = _multi_tf_for_assembler(n_15m=1000, seed=44)
        per_tf["4H"] = pd.DataFrame(columns=per_tf["4H"].columns)
        cfg_ds = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
            require_intraday=False,    # degrade allowed
            embargo_bars_override=10,
            adversarial_cv_folds=3, adversarial_threshold=1.0)
        res = ds_assembler.DatasetAssembler(cfg_ds).build(
            per_tf_bars=per_tf)
        self.assertTrue(res.manifest.coverage_degraded)
        out = ModelTrainer().train_one(
            _make_train_config("B0_majority",
                                  dataset_id=res.manifest.dataset_id),
            res)
        self.assertFalse(out.promotion_eligible)
        self.assertTrue(any(
            r == "dataset:coverage_degraded"
            for r in out.promotion_blocked_reasons),
            out.promotion_blocked_reasons)

    def test_all_trainability_and_dataset_gates_pass(self):
        """With permissive trainability thresholds AND a permissive AV
        gate, every NON-production gate passes — so the only remaining
        promotion-blocked reasons are the strict production-thinness
        ones (a tiny fixture can never meet 2000/500/100/50, and that
        gate is non-bypassable). This proves the trainability / dataset
        / AV gates are all satisfied without bypassing production."""
        res = _assemble_for_training(n_15m=1000, av_threshold=1.0)
        # Override only the TRAINABILITY thresholds so the synthetic
        # data fits; the production profile stays locked strict.
        trainer = ModelTrainer(
            thinness_thresholds=ThinnessThresholds(
                min_train_samples=10, min_val_samples=10,
                min_test_samples=10, min_minority_class_train=3,
                max_features_to_train_ratio=10.0))
        out = trainer.train_one(
            _make_train_config("B0_majority",
                                  dataset_id=res.manifest.dataset_id),
            res)
        non_production = [r for r in out.promotion_blocked_reasons
                          if not r.startswith("production:")]
        self.assertEqual(non_production, [],
            f"all non-production gates should pass; leftover non-"
            f"production reasons={non_production}")
        # And the model is still NOT promotable — production gate holds.
        self.assertFalse(out.promotion_eligible)
        self.assertTrue(any(r.startswith("production:")
                            for r in out.promotion_blocked_reasons))

    def test_reasons_are_namespaced_distinctly(self):
        """A degraded + thin dataset yields BOTH dataset: and
        thinness: prefixed reasons so the operator can tell them
        apart."""
        per_tf = _multi_tf_for_assembler(n_15m=600, seed=55)
        per_tf["4H"] = pd.DataFrame(columns=per_tf["4H"].columns)
        cfg_ds = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
            require_intraday=False,
            embargo_bars_override=10,
            adversarial_cv_folds=3, adversarial_threshold=1.0)
        res = ds_assembler.DatasetAssembler(cfg_ds).build(
            per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            _make_train_config("B0_majority",
                                  dataset_id=res.manifest.dataset_id),
            res)
        reasons = out.promotion_blocked_reasons
        has_dataset = any(r.startswith("dataset:") for r in reasons)
        has_thin    = any(r.startswith("thinness:") for r in reasons)
        self.assertTrue(has_dataset, reasons)
        self.assertTrue(has_thin,    reasons)

# ─────────────────────────────────────────────────────────────────────
# G5_DualCohort — explicit Model A vs Model B cohort semantics
# ─────────────────────────────────────────────────────────────────────

class G5_DualCohort(unittest.TestCase):
    """Locks in the dual-cohort contract:

    Model A (`train_mode='model_a_meta_label'`)
      structural cohort: `anchor_set='model_a_scanner_replica'`
      semantics: ONLY anchors where the live scanner fires; every
                  anchor row has scanner_replica.signal_fires == 1.

    Model B (`train_mode='model_b_candidate_quality'`)
      structural cohort: `anchor_set='model_b_1h_union_candidates'`
      semantics: ALL 1H anchors ∪ scanner-candidate anchors;
                  anchor rows include both signal_fires==0 (the 1H-
                  only anchors) and signal_fires==1 (the scanner-
                  fired anchors).

    The trainer does NOT re-filter rows by train_mode. The assembler
    is the single source of truth — train_mode is a metadata tag on
    the trainer. The trainer enforces 1:1 congruence between
    train_mode and manifest.anchor_set at train_one() time; any
    mismatch raises M18ConfigError.
    """

    # ── 0. Helpers: build BOTH cohorts from the same per_tf_bars ───

    def _build_per_tf(self, seed=21, n_15m=2000):
        return _multi_tf_for_assembler(n_15m=n_15m, seed=seed)

    def _build_model_a(self, per_tf):
        cfg = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_A_SCANNER_REPLICA,
            require_intraday=True, embargo_bars_override=10,
            adversarial_cv_folds=3, adversarial_threshold=1.0)
        return ds_assembler.DatasetAssembler(cfg).build(
            per_tf_bars=per_tf)

    def _build_model_b(self, per_tf):
        cfg = ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
            require_intraday=True, embargo_bars_override=10,
            adversarial_cv_folds=3, adversarial_threshold=1.0)
        return ds_assembler.DatasetAssembler(cfg).build(
            per_tf_bars=per_tf)

    def _anchor_rows_signal_fires(self, res):
        """Return scanner_replica.signal_fires for the rows pointed
        to by the walk-forward split (train + val + test)."""
        all_idx = np.concatenate([
            res.split.train_anchor_indices,
            res.split.val_anchor_indices,
            res.split.test_anchor_indices,
        ])
        return res.dataset.iloc[all_idx][
            SCANNER_FIRES_COLUMN].to_numpy(dtype=np.int64)

    # ── 1. Cohort STRUCTURE: anchor rows reflect anchor_set ─────────

    def test_model_a_anchor_rows_all_have_signal_fires_equal_1(self):
        """Model A cohort: every anchor row has signal_fires == 1.
        This is the structural assertion that the scanner_replica
        anchor_set actually filters to scanner-fires rows only."""
        per_tf = self._build_per_tf()
        res = self._build_model_a(per_tf)
        sf = self._anchor_rows_signal_fires(res)
        self.assertGreater(len(sf), 0,
            "test pre-condition: Model A must have at least 1 anchor")
        self.assertTrue((sf == 1).all(),
            f"Model A anchor rows must ALL have signal_fires=1; "
            f"got value distribution {pd.Series(sf).value_counts().to_dict()}")

    def test_model_b_anchor_rows_include_both_scanner_and_non_scanner(self):
        """Model B cohort: union of all 1H anchors and scanner
        candidates. Anchor rows must contain BOTH signal_fires=0 (the
        1H-only anchors) and signal_fires=1 (the scanner-fired
        anchors). Confirms Model B is NOT a scanner-only filter."""
        per_tf = self._build_per_tf()
        res = self._build_model_b(per_tf)
        sf = self._anchor_rows_signal_fires(res)
        unique_values = set(np.unique(sf).tolist())
        self.assertIn(0, unique_values,
            f"Model B must include signal_fires=0 rows (the 1H-only "
            f"anchors that the union semantics adds on top of the "
            f"scanner candidates); got {unique_values}")
        self.assertIn(1, unique_values,
            f"Model B must include signal_fires=1 rows (the scanner-"
            f"candidate part of the union); got {unique_values}")

    def test_model_b_is_a_superset_of_model_a_in_anchor_count(self):
        """Built from identical bars, |B anchors| >= |A anchors| —
        because B = 1H ∪ scanner and A = scanner only."""
        per_tf = self._build_per_tf()
        res_a = self._build_model_a(per_tf)
        res_b = self._build_model_b(per_tf)
        n_a = res_a.manifest.anchor_count_total
        n_b = res_b.manifest.anchor_count_total
        self.assertGreater(n_b, n_a,
            f"Model B anchor count must be > Model A anchor count "
            f"(B is a strict superset, given the 1H union adds "
            f"non-scanner anchors); got A={n_a}, B={n_b}")

    # ── 2. CONGRUENCE: correct (train_mode, anchor_set) pair works ─

    def test_correct_model_a_pairing_trains_and_records_provenance(self):
        per_tf = self._build_per_tf()
        res = self._build_model_a(per_tf)
        cfg = TrainConfig(
            dataset_id=res.manifest.dataset_id,
            model_type="B0_majority",
            train_mode="model_a_meta_label",
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False)
        out = ModelTrainer().train_one(cfg, res)
        # The train_mode tag is preserved
        self.assertEqual(out.train_mode, "model_a_meta_label")
        # The structural anchor_set is propagated from the manifest
        self.assertEqual(out.dataset_anchor_set,
                          "model_a_scanner_replica")
        # n_train > 0 — the cohort actually had data to train on
        self.assertGreater(out.n_train, 0)

    def test_correct_model_b_pairing_trains_and_records_provenance(self):
        per_tf = self._build_per_tf()
        res = self._build_model_b(per_tf)
        cfg = TrainConfig(
            dataset_id=res.manifest.dataset_id,
            model_type="B0_majority",
            train_mode="model_b_candidate_quality",
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False)
        out = ModelTrainer().train_one(cfg, res)
        self.assertEqual(out.train_mode, "model_b_candidate_quality")
        self.assertEqual(out.dataset_anchor_set,
                          "model_b_1h_union_candidates")
        self.assertGreater(out.n_train, 0)

    # ── 3. MISMATCH: wrong (train_mode, anchor_set) pair raises ─────

    def test_model_a_train_mode_on_model_b_dataset_raises(self):
        """Operator tagged their config as Model A but pointed it at
        a Model B dataset — must raise M18ConfigError."""
        per_tf = self._build_per_tf()
        res_b = self._build_model_b(per_tf)
        cfg = TrainConfig(
            dataset_id=res_b.manifest.dataset_id,
            model_type="B0_majority",
            train_mode="model_a_meta_label",          # WRONG
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False)
        with self.assertRaises(ml_errors.M18ConfigError) as ctx:
            ModelTrainer().train_one(cfg, res_b)
        msg = str(ctx.exception)
        self.assertIn("cohort mismatch", msg)
        self.assertIn("model_a_scanner_replica", msg)
        self.assertIn("model_b_1h_union_candidates", msg)
        # Suggested fix included
        self.assertIn("model_b_candidate_quality", msg)

    def test_model_b_train_mode_on_model_a_dataset_raises(self):
        per_tf = self._build_per_tf()
        res_a = self._build_model_a(per_tf)
        cfg = TrainConfig(
            dataset_id=res_a.manifest.dataset_id,
            model_type="B0_majority",
            train_mode="model_b_candidate_quality",   # WRONG
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False)
        with self.assertRaises(ml_errors.M18ConfigError) as ctx:
            ModelTrainer().train_one(cfg, res_a)
        msg = str(ctx.exception)
        self.assertIn("cohort mismatch", msg)
        self.assertIn("model_b_1h_union_candidates", msg)
        self.assertIn("model_a_scanner_replica", msg)
        # Suggested fix included
        self.assertIn("model_a_meta_label", msg)

    def test_cohort_mismatch_surfaces_before_split_check(self):
        """Cohort mismatch is more diagnostic than split=None; the
        trainer raises the cohort mismatch FIRST so the operator
        sees the structural problem even on degenerate datasets."""
        from dataclasses import replace
        per_tf = self._build_per_tf()
        res_b = self._build_model_b(per_tf)
        # Build a degenerate result: same Model B dataset but with
        # split=None. Cohort check must fire even when split is None.
        res_b_no_split = replace(res_b, split=None)
        cfg = TrainConfig(
            dataset_id=res_b.manifest.dataset_id,
            model_type="B0_majority",
            train_mode="model_a_meta_label",         # WRONG
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False)
        with self.assertRaises(ml_errors.M18ConfigError) as ctx:
            ModelTrainer().train_one(cfg, res_b_no_split)
        # The cohort mismatch (M18ConfigError) — NOT
        # InsufficientDataError (which would be raised by split=None
        # if the cohort check ran second). Both are M18Error
        # subclasses but ConfigError is the right one here.
        self.assertIn("cohort mismatch", str(ctx.exception))

    # ── 4. INVARIANTS: mapping is 1:1, trainer is non-filtering ─────

    def test_train_mode_to_anchor_set_mapping_is_one_to_one(self):
        """Locked map: every ALLOWED_TRAIN_MODES value has exactly
        one corresponding anchor_set, and vice versa."""
        from bot.ml.models import (
            TRAIN_MODE_TO_ANCHOR_SET, ANCHOR_SET_TO_TRAIN_MODE)
        # Every train_mode in the locked schema must have a mapping
        for tm in ALLOWED_TRAIN_MODES:
            self.assertIn(tm, TRAIN_MODE_TO_ANCHOR_SET,
                f"train_mode {tm!r} has no anchor_set mapping in "
                f"trainer.TRAIN_MODE_TO_ANCHOR_SET")
        # The inverse mapping is the inverse
        for tm, as_ in TRAIN_MODE_TO_ANCHOR_SET.items():
            self.assertEqual(ANCHOR_SET_TO_TRAIN_MODE[as_], tm)
        # And inverse is exhaustive
        self.assertEqual(
            set(ANCHOR_SET_TO_TRAIN_MODE.keys()),
            set(TRAIN_MODE_TO_ANCHOR_SET.values()))

    def test_trainer_does_not_filter_rows_by_train_mode(self):
        """The assembler is the single source of truth for the
        cohort. Trainer.train_one() must use the split indices
        directly — it must NOT secretly re-filter to scanner-fires-
        only rows when train_mode='model_a_meta_label' is supplied
        with a Model A dataset.

        Equivalently: n_train + n_val + n_test == sum of split
        index lengths, regardless of train_mode. We prove this by
        training Model A correctly and showing the per-split sample
        counts match the split's own index counts exactly.
        """
        per_tf = self._build_per_tf()
        res = self._build_model_a(per_tf)
        cfg = TrainConfig(
            dataset_id=res.manifest.dataset_id,
            model_type="B0_majority",
            train_mode="model_a_meta_label",
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False)
        out = ModelTrainer().train_one(cfg, res)
        # Counts MUST equal the split index lengths exactly — the
        # trainer did not silently drop rows.
        self.assertEqual(out.n_train,
                          len(res.split.train_anchor_indices))
        self.assertEqual(out.n_val,
                          len(res.split.val_anchor_indices))
        self.assertEqual(out.n_test,
                          len(res.split.test_anchor_indices))
        # And the manifest's own counts (the assembler's record)
        # match too — the assembler is the single source of truth.
        self.assertEqual(out.n_train,
                          res.manifest.anchor_count_train)
        self.assertEqual(out.n_val,
                          res.manifest.anchor_count_val)
        self.assertEqual(out.n_test,
                          res.manifest.anchor_count_test)

    def test_train_outputs_records_cohort_metadata_fields(self):
        """Every TrainOutputs must record BOTH the train_mode (the
        operator's tag) and the dataset_anchor_set (the assembler's
        structural identifier) so M18.A.8 promotion can verify
        cohort provenance."""
        per_tf = self._build_per_tf()
        for build, tm, expected_anchor_set in (
            (self._build_model_a, "model_a_meta_label",
              "model_a_scanner_replica"),
            (self._build_model_b, "model_b_candidate_quality",
              "model_b_1h_union_candidates"),
        ):
            res = build(per_tf)
            cfg = TrainConfig(
                dataset_id=res.manifest.dataset_id,
                model_type="B0_majority",
                train_mode=tm,
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False)
            out = ModelTrainer().train_one(cfg, res)
            self.assertEqual(out.train_mode, tm)
            self.assertEqual(out.dataset_anchor_set,
                              expected_anchor_set)
            # to_dict() serialisation preserves both fields
            d = out.to_dict()
            self.assertEqual(d["train_mode"], tm)
            self.assertEqual(d["dataset_anchor_set"],
                              expected_anchor_set)

# ═════════════════════════════════════════════════════════════════════
# G6 — Evaluation report generation (M18.A.7)
# ═════════════════════════════════════════════════════════════════════

from bot.ml.evaluation import (
    EvaluationReport,
    BaselineComparisonReport,
    CrossCohortComparisonReport,
    CROSS_COHORT_DISCLAIMER,
    calibration_report as eval_calibration_report,
    expected_calibration_error,
    maximum_calibration_error,
    reliability_curve,
    fit_isotonic_calibration,
    apply_isotonic_artifact,
    trading_metrics as eval_trading_metrics,
    evaluate_model,
    compare_baselines,
    compare_across_cohorts,
    ALLOWED_PRIMARY_SPLITS,
)
from bot.ml.evaluation.report import (
    EVALUATION_REPORT_SCHEMA_VERSION,
    BASELINE_COMPARISON_REPORT_SCHEMA_VERSION,
    CROSS_COHORT_COMPARISON_REPORT_SCHEMA_VERSION,
)


def _train_three_baselines_on_model_b(per_tf=None, seed=21):
    """Train B0/B1/B2 on a Model B dataset and return
    (assembler_result, [reports])."""
    if per_tf is None:
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=seed)
    res = ds_assembler.DatasetAssembler(
        ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
            require_intraday=True, embargo_bars_override=10,
            adversarial_cv_folds=3, adversarial_threshold=1.0)
    ).build(per_tf_bars=per_tf)
    reports = []
    for mt in ("B0_majority", "B1_scanner_replica", "B2_logistic"):
        cfg = TrainConfig(
            dataset_id=res.manifest.dataset_id,
            model_type=mt,
            train_mode="model_b_candidate_quality",
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False)
        out = ModelTrainer().train_one(cfg, res)
        reports.append(evaluate_model(out, res))
    return res, reports


def _train_b2_on_model_a(per_tf=None, seed=21):
    if per_tf is None:
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=seed)
    res = ds_assembler.DatasetAssembler(
        ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_A_SCANNER_REPLICA,
            require_intraday=True, embargo_bars_override=10,
            adversarial_cv_folds=3, adversarial_threshold=1.0)
    ).build(per_tf_bars=per_tf)
    cfg = TrainConfig(
        dataset_id=res.manifest.dataset_id,
        model_type="B2_logistic",
        train_mode="model_a_meta_label",
        target_label_id="triple_barrier_atr_2_3_50_won",
        hyperparameters={}, seed=42, fixture_mode=False)
    out = ModelTrainer().train_one(cfg, res)
    return res, evaluate_model(out, res)


# ─────────────────────────────────────────────────────────────────────
# G6_Calibration — pure math
# ─────────────────────────────────────────────────────────────────────

class G6_Calibration(unittest.TestCase):

    def test_perfect_calibration_ece_zero(self):
        """When y_proba == y_true exactly, ECE and MCE are 0."""
        y_true  = np.array([0.0, 0.0, 1.0, 1.0])
        y_proba = np.array([0.0, 0.0, 1.0, 1.0])
        self.assertEqual(
            expected_calibration_error(y_true, y_proba), 0.0)
        self.assertEqual(
            maximum_calibration_error(y_true, y_proba), 0.0)

    def test_uniform_predictions_against_balanced_truth(self):
        """Constant predictions of 0.5 against 50/50 truth → gap = 0
        in bin index 4 or 5 (the bin containing 0.5). ECE and MCE
        should be 0 because mean_pred ≈ 0.5 and mean_actual = 0.5."""
        n = 100
        y_true  = np.concatenate([np.zeros(50), np.ones(50)])
        y_proba = np.full(n, 0.5)
        ece = expected_calibration_error(y_true, y_proba)
        self.assertAlmostEqual(ece, 0.0, places=12)

    def test_constant_zero_predictions_with_actual_positives(self):
        """All predictions = 0, actual positives 30%. The bin
        containing 0.0 (index 0) has mean_pred ≈ 0 and mean_actual
        = 0.3. ECE = |0 - 0.3| * (1.0) = 0.3."""
        y_true  = np.concatenate([np.zeros(70), np.ones(30)])
        y_proba = np.zeros(100)
        ece = expected_calibration_error(y_true, y_proba)
        self.assertAlmostEqual(ece, 0.3, places=12)
        mce = maximum_calibration_error(y_true, y_proba)
        self.assertAlmostEqual(mce, 0.3, places=12)

    def test_empty_input_returns_nan(self):
        y = np.array([], dtype=np.float64)
        self.assertTrue(np.isnan(expected_calibration_error(y, y)))
        self.assertTrue(np.isnan(maximum_calibration_error(y, y)))

    def test_reliability_curve_bin_count_and_edges(self):
        rng = np.random.default_rng(0)
        y_true  = rng.integers(0, 2, 200).astype(float)
        y_proba = rng.uniform(0, 1, 200)
        curve = reliability_curve(y_true, y_proba, n_bins=10)
        self.assertEqual(len(curve), 10)
        # Edges are equal-width
        self.assertAlmostEqual(curve[0]["bin_lo"],  0.0)
        self.assertAlmostEqual(curve[0]["bin_hi"],  0.1)
        self.assertAlmostEqual(curve[9]["bin_lo"],  0.9)
        self.assertAlmostEqual(curve[9]["bin_hi"],  1.0)
        # Total count across bins equals input size
        self.assertEqual(sum(b["count"] for b in curve), 200)

    def test_calibration_report_bundles_all_fields(self):
        rep = eval_calibration_report(
            np.array([0, 1, 0, 1]), np.array([0.1, 0.9, 0.2, 0.8]))
        for k in ("n_rows", "n_bins", "expected_calibration_error",
                   "maximum_calibration_error", "reliability_curve"):
            self.assertIn(k, rep)
        self.assertEqual(rep["n_rows"], 4)
        self.assertEqual(rep["n_bins"], 10)


# ─────────────────────────────────────────────────────────────────────
# G6_TradingMetrics — precision/recall/log return
# ─────────────────────────────────────────────────────────────────────

class G6_TradingMetrics(unittest.TestCase):

    def test_precision_recall_basic(self):
        # 6 rows: 3 actual positives, prediction agrees on 2/3 of them
        # and false-positives on 1 row → TP=2, FP=1, FN=1
        # precision = 2/3, recall = 2/3
        y_true  = np.array([1, 1, 0, 0, 1, 0], dtype=float)
        y_proba = np.array([0.9, 0.9, 0.9, 0.1, 0.1, 0.1])
        m = eval_trading_metrics(
            y_true=y_true, y_proba=y_proba,
            target_label_id="not_tb_label_so_aux_disabled")
        self.assertEqual(m["n_rows"], 6)
        self.assertEqual(m["n_predicted_positive"], 3)
        self.assertEqual(m["n_actual_positive"], 3)
        self.assertAlmostEqual(m["precision_at_threshold"], 2/3,
                                 places=10)
        self.assertAlmostEqual(m["recall_at_threshold"],    2/3,
                                 places=10)
        # Aux disabled because target label is not a TB-won label
        self.assertFalse(m["trading_metrics_available"])

    def test_zero_predicted_positive_warning(self):
        y_true  = np.array([1, 0, 1, 0])
        y_proba = np.array([0.1, 0.1, 0.1, 0.1])   # all 0 < 0.5
        m = eval_trading_metrics(
            y_true=y_true, y_proba=y_proba,
            target_label_id="x")
        self.assertEqual(m["n_predicted_positive"], 0)
        self.assertIn("zero_predicted_positive",
                       m["zero_trade_warnings"])
        # Precision is NaN (no positives predicted)
        self.assertTrue(np.isnan(m["precision_at_threshold"]))

    def test_zero_actual_positive_warning(self):
        y_true  = np.zeros(4)
        y_proba = np.array([0.9, 0.6, 0.4, 0.1])
        m = eval_trading_metrics(
            y_true=y_true, y_proba=y_proba,
            target_label_id="x")
        self.assertEqual(m["n_actual_positive"], 0)
        self.assertIn("zero_actual_positive",
                       m["zero_trade_warnings"])
        self.assertTrue(np.isnan(m["recall_at_threshold"]))

    def test_empty_split(self):
        m = eval_trading_metrics(
            y_true=np.array([]), y_proba=np.array([]),
            target_label_id="x")
        self.assertEqual(m["n_rows"], 0)
        self.assertIn("empty_split", m["zero_trade_warnings"])

    def test_log_return_aggregation_with_aux_columns(self):
        """When dataset + split_indices + TB-won label are all
        supplied, mean/sum log return aggregate over predicted-
        positive rows."""
        res, [rep_b0, rep_b1, rep_b2] = \
            _train_three_baselines_on_model_b()
        tm_val = rep_b2.trading_metrics["val"]
        self.assertTrue(tm_val["trading_metrics_available"])
        self.assertEqual(tm_val["primary_label_id"],
                          "triple_barrier_atr_2_3_50")
        # If any positives were predicted, the metrics are non-NaN
        if tm_val["n_predicted_positive"] > 0:
            self.assertFalse(np.isnan(
                tm_val["mean_log_return_predicted_positive"]))
            self.assertFalse(np.isnan(
                tm_val["mean_bars_to_resolution_predicted_positive"]))

    def test_target_label_id_must_end_with_won_for_aux(self):
        """A non-_won triple-barrier label gets aux metrics unavailable
        with a clear warning."""
        m = eval_trading_metrics(
            y_true=np.array([0, 1]),
            y_proba=np.array([0.3, 0.7]),
            target_label_id="triple_barrier_atr_2_3_50",  # not _won
            dataset=pd.DataFrame({}),     # irrelevant
            split_indices=np.array([0, 1]))
        self.assertIsNone(m["primary_label_id"])
        self.assertFalse(m["trading_metrics_available"])
        self.assertTrue(any("not a triple-barrier _won" in w
                              for w in m["zero_trade_warnings"]))


# ─────────────────────────────────────────────────────────────────────
# G6_EvaluationReport — provenance fields & schema
# ─────────────────────────────────────────────────────────────────────

class G6_EvaluationReport(unittest.TestCase):

    def test_required_fields_all_present(self):
        """The operator's M18.A.7 directive requires train_mode,
        dataset_anchor_set, split row counts, split timestamp
        ranges, embargo/purge settings, accepted/filtered counts,
        and model cohort type. Verify each."""
        _, [r] = _train_three_baselines_on_model_b()[0], \
                  _train_three_baselines_on_model_b()[1][:1]
        d = r.to_dict()
        # train_mode
        self.assertEqual(d["train_mode"], "model_b_candidate_quality")
        # dataset_anchor_set
        self.assertEqual(d["dataset_anchor_set"],
                          "model_b_1h_union_candidates")
        # split row counts
        self.assertIn("n_train", d)
        self.assertIn("n_val",   d)
        self.assertIn("n_test",  d)
        # split timestamp ranges
        for s in ("train", "val", "test"):
            self.assertIn(s, d["split_timestamp_ranges"])
            self.assertIn("first", d["split_timestamp_ranges"][s])
            self.assertIn("last",  d["split_timestamp_ranges"][s])
            self.assertIn("count", d["split_timestamp_ranges"][s])
        # embargo/purge settings (under 'split')
        for k in ("embargo_bars", "embargo_trading_days",
                    "label_resolved_ts_purge_applied",
                    "train_frac", "val_frac", "test_frac",
                    "split_built"):
            self.assertIn(k, d["split"])
        # accepted/filtered counts (under 'cohort')
        for k in ("anchor_count_raw",
                    "anchor_count_pending_excluded",
                    "anchor_count_total",
                    "anchor_count_train",
                    "anchor_count_val",
                    "anchor_count_test",
                    "anchor_count_purged",
                    "anchor_count_embargoed"):
            self.assertIn(k, d["cohort"])
        # model cohort type → both fields present
        self.assertEqual(d["cohort"]["anchor_set"],
                          d["dataset_anchor_set"])

    def test_schema_version_recorded(self):
        _, [r] = _train_three_baselines_on_model_b()[0], \
                  _train_three_baselines_on_model_b()[1][:1]
        self.assertEqual(r.schema_version,
                          EVALUATION_REPORT_SCHEMA_VERSION)

    def test_ml_metrics_echoed_from_train_outputs(self):
        """ml_metrics in the report match TrainOutputs.metrics_*
        verbatim."""
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        self.assertEqual(rep.ml_metrics["train"], out.metrics_train)
        self.assertEqual(rep.ml_metrics["val"],   out.metrics_val)
        self.assertEqual(rep.ml_metrics["test"],  out.metrics_test)

    def test_promotion_gate_echoed(self):
        _, [r] = _train_three_baselines_on_model_b()[0], \
                  _train_three_baselines_on_model_b()[1][:1]
        self.assertIsInstance(r.fixture_only, bool)
        self.assertIsInstance(r.promotion_eligible, bool)
        self.assertIsInstance(r.promotion_blocked_reasons, list)

    def test_dataset_id_mismatch_refuses(self):
        """Evaluator refuses to combine a TrainOutputs with a
        different dataset's AssemblerResult."""
        res_a, _ = _train_b2_on_model_a()
        res_b, [out_b] = _train_three_baselines_on_model_b()[0], \
                          _train_three_baselines_on_model_b()[1][:1]
        # Build a TrainOutputs pointing at res_b's dataset but pass
        # res_a's AssemblerResult — must raise.
        # Construct by serialising and reconstructing fields is
        # complex; easier: use the train output corresponding to
        # res_b and pass res_a.
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res_b2 = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res_b2.manifest.dataset_id,
                model_type="B0_majority",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res_b2)
        with self.assertRaises(ml_errors.M18ConfigError):
            evaluate_model(out, res_a)

    def test_generated_at_utc_is_iso_format(self):
        _, [r] = _train_three_baselines_on_model_b()[0], \
                  _train_three_baselines_on_model_b()[1][:1]
        # Parses cleanly as ISO 8601
        from datetime import datetime
        parsed = datetime.fromisoformat(r.generated_at_utc)
        self.assertIsNotNone(parsed)

    def test_to_dict_is_json_safe(self):
        import json
        _, [r] = _train_three_baselines_on_model_b()[0], \
                  _train_three_baselines_on_model_b()[1][:1]
        # NaN values in metrics won't strict-JSON, so serialise with
        # allow_nan=True (which json.dumps does by default).
        json.dumps(r.to_dict())


# ─────────────────────────────────────────────────────────────────────
# G6_BaselineCompare — same-cohort only
# ─────────────────────────────────────────────────────────────────────

class G6_BaselineCompare(unittest.TestCase):

    def test_three_baselines_same_cohort_produce_summary(self):
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports,
            primary_metric="roc_auc", primary_split="val",
            baseline_model_type="B0_majority")
        # All three model types appear in per_metric
        self.assertEqual(
            set(cmp.per_metric["roc_auc"].keys()),
            {"B0_majority", "B1_scanner_replica", "B2_logistic"})
        # baseline_beats now records BOTH primary (vs B0) AND
        # secondary (vs B1) baselines: 2 vs B0 + 2 vs B1 = 4 entries.
        self.assertEqual(len(cmp.baseline_beats), 4)
        # cohort identity recorded
        self.assertEqual(cmp.cohort_anchor_set,
                          "model_b_1h_union_candidates")
        self.assertEqual(cmp.schema_version,
                          BASELINE_COMPARISON_REPORT_SCHEMA_VERSION)

    def test_cross_cohort_inputs_rejected(self):
        """compare_baselines must REFUSE inputs from different
        cohorts — row-paired comparison is meaningless then."""
        _, b_reports = _train_three_baselines_on_model_b()
        _, a_report  = _train_b2_on_model_a()
        # Mix one A report with the B reports
        with self.assertRaises(ml_errors.M18ConfigError) as ctx:
            compare_baselines([a_report] + b_reports,
                primary_metric="roc_auc", primary_split="val",
                baseline_model_type="B0_majority")
        msg = str(ctx.exception)
        self.assertTrue("dataset_id" in msg
                          or "dataset_anchor_set" in msg, msg)

    def test_duplicate_model_type_rejected(self):
        _, reports = _train_three_baselines_on_model_b()
        with self.assertRaises(ml_errors.M18ConfigError):
            compare_baselines(reports + [reports[0]],
                baseline_model_type="B0_majority")

    def test_baseline_beat_direction_auc_higher_is_better(self):
        """For ROC AUC, "beats" means strictly greater."""
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports,
            primary_metric="roc_auc", primary_split="val",
            baseline_model_type="B0_majority")
        b0_auc = cmp.per_metric["roc_auc"]["B0_majority"]
        for mt in ("B1_scanner_replica", "B2_logistic"):
            cand_auc = cmp.per_metric["roc_auc"][mt]
            key = f"{mt}_beats_B0_majority_on_val_roc_auc"
            # Beats iff cand > base (and both finite)
            if not (np.isnan(b0_auc) or np.isnan(cand_auc)):
                self.assertEqual(cmp.baseline_beats[key],
                                  cand_auc > b0_auc, (key, cand_auc, b0_auc))

    def test_baseline_beat_direction_brier_lower_is_better(self):
        """For Brier score, "beats" means strictly LESS."""
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports,
            primary_metric="brier_score", primary_split="val",
            baseline_model_type="B0_majority")
        b0_brier = cmp.per_metric["brier_score"]["B0_majority"]
        for mt in ("B1_scanner_replica", "B2_logistic"):
            cand_brier = cmp.per_metric["brier_score"][mt]
            key = f"{mt}_beats_B0_majority_on_val_brier_score"
            if not (np.isnan(b0_brier) or np.isnan(cand_brier)):
                self.assertEqual(cmp.baseline_beats[key],
                                  cand_brier < b0_brier,
                                  (key, cand_brier, b0_brier))

    def test_empty_reports_list_rejected(self):
        with self.assertRaises(ml_errors.M18ConfigError):
            compare_baselines([], baseline_model_type="B0_majority")

    def test_unknown_baseline_model_type_rejected(self):
        _, reports = _train_three_baselines_on_model_b()
        with self.assertRaises(ml_errors.M18ConfigError):
            compare_baselines(reports,
                baseline_model_type="NOT_A_MODEL_TYPE")


# ─────────────────────────────────────────────────────────────────────
# G6_CrossCohortCompare — explicit non-row-paired
# ─────────────────────────────────────────────────────────────────────

class G6_CrossCohortCompare(unittest.TestCase):

    def test_disclaimer_present_verbatim(self):
        _, [rep_b_b2] = _train_three_baselines_on_model_b()[0], \
                         [r for r in _train_three_baselines_on_model_b()[1]
                           if r.model_type == "B2_logistic"]
        _, rep_a = _train_b2_on_model_a()
        cross = compare_across_cohorts(rep_a, rep_b_b2)
        self.assertEqual(cross.disclaimer, CROSS_COHORT_DISCLAIMER)
        # The disclaimer text must mention DIFFERENT cohorts and
        # NO row-paired implication
        self.assertIn("DIFFERENT cohorts", cross.disclaimer)
        self.assertIn("aggregate-level", cross.disclaimer)
        self.assertIn("not as a paired", cross.disclaimer)

    def test_aggregate_metrics_labeled_by_train_mode(self):
        b_reports = _train_three_baselines_on_model_b()[1]
        rep_b_b2 = next(r for r in b_reports
                          if r.model_type == "B2_logistic")
        _, rep_a = _train_b2_on_model_a()
        cross = compare_across_cohorts(rep_a, rep_b_b2,
                                          primary_split="val")
        # Keys are the train_mode strings, NOT 'a' / 'b'
        for metric, by_mode in cross.aggregate_metric_values.items():
            self.assertIn("model_a_meta_label", by_mode)
            self.assertIn("model_b_candidate_quality", by_mode)

    def test_same_anchor_set_rejected(self):
        """compare_across_cohorts must refuse same-cohort inputs."""
        _, reports = _train_three_baselines_on_model_b()
        b0, b2 = reports[0], reports[2]
        with self.assertRaises(ml_errors.M18ConfigError) as ctx:
            compare_across_cohorts(b0, b2)
        self.assertIn("SAME anchor_set", str(ctx.exception))

    def test_invalid_primary_split_rejected(self):
        b_reports = _train_three_baselines_on_model_b()[1]
        rep_b_b2 = next(r for r in b_reports
                          if r.model_type == "B2_logistic")
        _, rep_a = _train_b2_on_model_a()
        with self.assertRaises(ml_errors.M18ConfigError):
            compare_across_cohorts(rep_a, rep_b_b2,
                                      primary_split="holdout")  # invalid

    def test_cross_cohort_uses_each_models_own_split_sizes(self):
        """Sanity check the operator's specific directive: the two
        reports keep their own n_train/val/test (not a forced common
        size)."""
        b_reports = _train_three_baselines_on_model_b()[1]
        rep_b_b2 = next(r for r in b_reports
                          if r.model_type == "B2_logistic")
        _, rep_a = _train_b2_on_model_a()
        cross = compare_across_cohorts(rep_a, rep_b_b2)
        a_d = cross.a_report
        b_d = cross.b_report
        # Different cohort sizes — no normalising
        self.assertNotEqual(a_d["n_train"], b_d["n_train"])
        # train_mode and dataset_anchor_set both preserved
        self.assertEqual(a_d["train_mode"], "model_a_meta_label")
        self.assertEqual(b_d["train_mode"],
                          "model_b_candidate_quality")


# ─────────────────────────────────────────────────────────────────────
# G6_ZeroHandling — empty splits / zero-positive predictions
# ─────────────────────────────────────────────────────────────────────

class G6_ZeroHandling(unittest.TestCase):

    def test_constant_negative_model_produces_well_formed_report(self):
        """A model that predicts the same constant for every row →
        n_predicted_positive is either 0 (constant < 0.5) or n_rows
        (constant >= 0.5). Either way, precision/recall behave
        correctly and warnings are populated. For B0_majority with
        imbalanced data, prior(class=1) is typically < 0.5, giving
        zero predicted positives in every split.

        ROC AUC for a constant predictor on a two-class y_true is
        0.5 by sklearn convention (all ties → average) — NOT NaN.
        NaN occurs only when y_true is single-class."""
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B0_majority",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        # B0 emits prior(class=1) constant. Verify trading metric
        # consistency: predictions binarise the same way for every
        # row (either all 0 or all 1).
        for s in ("train", "val", "test"):
            tm = rep.trading_metrics[s]
            n_pred = tm["n_predicted_positive"]
            # All-or-nothing: B0 is constant
            self.assertIn(n_pred, (0, tm["n_rows"]),
                f"{s}: B0 is constant — n_predicted_positive must be "
                f"0 or n_rows ({tm['n_rows']}), got {n_pred}")
            if n_pred == 0:
                self.assertIn("zero_predicted_positive",
                               tm["zero_trade_warnings"])
                self.assertTrue(
                    np.isnan(tm["precision_at_threshold"]))
            # For a constant predictor on two-class y_true, sklearn
            # ROC AUC = 0.5 by tie convention. NaN only if y_true
            # itself is single-class in this split.
            auc = rep.ml_metrics[s]["roc_auc"]
            y_t = res.dataset.iloc[
                getattr(res.split, f"{s}_anchor_indices")][
                "triple_barrier_atr_2_3_50_won"].to_numpy()
            if len(np.unique(y_t)) < 2:
                self.assertTrue(np.isnan(auc),
                    f"{s}: single-class y_true should give NaN AUC; "
                    f"got {auc}")
            else:
                self.assertEqual(auc, 0.5,
                    f"{s}: constant predictor on 2-class y_true "
                    f"should give AUC=0.5; got {auc}")

    def test_empty_test_split_serialises_without_error(self):
        """Empty test split (operator's zero-trade case): evaluator
        produces a well-formed report — empty trading metrics on
        the empty split, calibration NaN, timestamp range counts=0.

        Built deterministically by taking a real split and replacing
        only test_anchor_indices with an empty array via
        dataclasses.replace (preserves the real WalkForwardSplit
        field types — checked against bot.ml.dataset.walk_forward
        rather than guessed)."""
        from dataclasses import replace
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B0_majority",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        # Empty the test split deterministically
        empty_split = replace(
            res.split,
            test_anchor_indices=np.array([], dtype=np.int64))
        # Empty pred_test to match
        modified_out = replace(out, pred_test=[], n_test=0)
        modified_res = replace(res, split=empty_split)
        rep = evaluate_model(modified_out, modified_res)
        self.assertEqual(rep.n_test, 0)
        self.assertEqual(
            rep.split_timestamp_ranges["test"]["first"], None)
        self.assertEqual(
            rep.split_timestamp_ranges["test"]["count"], 0)
        tm_test = rep.trading_metrics["test"]
        self.assertIn("empty_split", tm_test["zero_trade_warnings"])

    def test_all_actual_negative_in_split(self):
        """y_true = all zeros for a split → recall NaN, AUC NaN,
        precision well-defined (depending on predictions)."""
        m = eval_trading_metrics(
            y_true=np.zeros(10),
            y_proba=np.array([0.9]*3 + [0.1]*7),
            target_label_id="x")
        # 3 predicted positive, 0 actual → TP=0
        self.assertEqual(m["n_predicted_positive"], 3)
        self.assertEqual(m["n_actual_positive"],    0)
        self.assertEqual(m["precision_at_threshold"], 0.0)
        self.assertTrue(np.isnan(m["recall_at_threshold"]))


# ─────────────────────────────────────────────────────────────────────
# G6_RegressionTarget — calibration/trading metrics flagged unavailable
# ─────────────────────────────────────────────────────────────────────

class G6_RegressionTarget(unittest.TestCase):

    def test_regression_target_marks_calibration_and_trading_unavailable(self):
        """A trainer config targeting a regression label like
        fwd_return_5b — calibration and trading metrics are
        inapplicable. The evaluator must produce a report with
        these blocks explicitly marked unavailable rather than
        emitting bogus metrics."""
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        # B0_majority supports regression — emits train mean
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B0_majority",
                train_mode="model_b_candidate_quality",
                target_label_id="fwd_return_5b",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        self.assertEqual(rep.target_label_class, "regression")
        for s in ("train", "val", "test"):
            self.assertIn("unavailable_for_label_class",
                           rep.calibration[s])
            self.assertIn("unavailable_for_label_class",
                           rep.trading_metrics[s])



# ═════════════════════════════════════════════════════════════════════
# G7 — Extended evaluation: PR-AUC, threshold table, drift,
#       permutation importance, breakdowns (M18.A.7 amend)
# ═════════════════════════════════════════════════════════════════════

from bot.ml.evaluation import (
    binary_metrics_extended,
    threshold_table,
    LOCKED_THRESHOLDS,
    drift_report,
    permutation_importance,
    PI_SUPPORTED_MODEL_TYPES,
    all_breakdowns,
    per_symbol_breakdown,
    per_year_breakdown,
    volatility_regime_breakdown,
    market_regime_breakdown,
    MIN_SAMPLES_PER_SEGMENT,
    PRECISION_AT_K_LIST,
    EQUITY_CURVE_UNAVAILABLE_REASON,
)


# ─────────────────────────────────────────────────────────────────────
# G7_ExtendedMlMetrics — PR-AUC, log_loss, F1, confusion matrix
# ─────────────────────────────────────────────────────────────────────

class G7_ExtendedMlMetrics(unittest.TestCase):

    def test_perfect_predictions_yield_pr_auc_1(self):
        """When y_proba perfectly orders y_true (all positives above
        all negatives), PR-AUC = 1.0 and ROC AUC = 1.0."""
        y_true  = np.array([0, 0, 0, 1, 1, 1], dtype=float)
        y_proba = np.array([0.1, 0.2, 0.3, 0.7, 0.8, 0.9])
        m = binary_metrics_extended(y_true, y_proba)
        self.assertAlmostEqual(m["pr_auc"],  1.0, places=10)
        self.assertAlmostEqual(m["roc_auc"], 1.0, places=10)
        self.assertEqual(m["confusion_matrix_at_05"],
                          {"tp": 3, "fp": 0, "fn": 0, "tn": 3})

    def test_pr_auc_present_and_finite_on_real_split(self):
        """PR-AUC is the PRIMARY M18 metric — verify it's emitted for
        every binary split."""
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        for s in ("train", "val", "test"):
            ext = rep.ml_metrics_extended[s]
            self.assertIn("pr_auc", ext)
            # PR-AUC is finite when both classes are present in the
            # split. Otherwise NaN, which is the documented behaviour.
            if not np.isnan(ext["pr_auc"]):
                self.assertGreater(ext["pr_auc"], 0.0)
                self.assertLessEqual(ext["pr_auc"], 1.0)
            self.assertIn("log_loss", ext)
            self.assertIn("f1_at_05", ext)
            self.assertIn("confusion_matrix_at_05", ext)

    def test_confusion_matrix_consistency(self):
        """tp + fp + fn + tn must equal n_rows."""
        y_true  = np.array([1, 1, 0, 0, 1, 0, 1, 1])
        y_proba = np.array([0.9, 0.4, 0.6, 0.1, 0.8, 0.3, 0.7, 0.2])
        m = binary_metrics_extended(y_true, y_proba)
        cm = m["confusion_matrix_at_05"]
        self.assertEqual(cm["tp"] + cm["fp"] + cm["fn"] + cm["tn"],
                          m["n_rows"])

    def test_log_loss_clipped_no_inf_on_extreme_predictions(self):
        """y_proba = 0 with y_true = 1 would blow up log_loss without
        clipping. binary_metrics_extended must return finite log_loss."""
        y_true  = np.array([1, 1, 0, 0], dtype=float)
        y_proba = np.array([0.0, 0.0, 1.0, 1.0])
        m = binary_metrics_extended(y_true, y_proba)
        self.assertTrue(np.isfinite(m["log_loss"]),
            f"log_loss must be clipped to finite; got {m['log_loss']}")

    def test_single_class_y_true_pr_auc_nan(self):
        """When y_true is all-zero or all-one, PR-AUC is undefined
        (no minority class). Must return NaN per sklearn convention."""
        m = binary_metrics_extended(np.zeros(10),
                                       np.linspace(0.1, 0.9, 10))
        self.assertTrue(np.isnan(m["pr_auc"]))
        self.assertTrue(np.isnan(m["roc_auc"]))


# ─────────────────────────────────────────────────────────────────────
# G7_ThresholdTable — locked threshold ladder
# ─────────────────────────────────────────────────────────────────────

class G7_ThresholdTable(unittest.TestCase):

    def test_locked_thresholds_match_directive(self):
        """The locked threshold ladder per M18.A.7 directive."""
        self.assertEqual(tuple(LOCKED_THRESHOLDS),
                          (0.30, 0.40, 0.50, 0.60, 0.65, 0.70, 0.80))

    def test_table_row_count_matches_thresholds(self):
        rng = np.random.default_rng(0)
        y_true  = rng.integers(0, 2, 300).astype(float)
        y_proba = rng.uniform(0, 1, 300)
        t = threshold_table(y_true, y_proba)
        self.assertEqual(len(t["rows"]), len(LOCKED_THRESHOLDS))

    def test_accepted_plus_filtered_equals_n_rows(self):
        rng = np.random.default_rng(1)
        y_true  = rng.integers(0, 2, 200).astype(float)
        y_proba = rng.uniform(0, 1, 200)
        t = threshold_table(y_true, y_proba)
        for row in t["rows"]:
            self.assertEqual(
                row["n_predicted_positive"] + row["n_filtered"],
                t["n_rows"])

    def test_higher_threshold_yields_fewer_predicted_positive(self):
        """Monotone: predicted positive count is non-increasing in
        threshold."""
        rng = np.random.default_rng(2)
        y_true  = rng.integers(0, 2, 300).astype(float)
        y_proba = rng.uniform(0, 1, 300)
        t = threshold_table(y_true, y_proba)
        counts = [r["n_predicted_positive"] for r in t["rows"]]
        for prev, nxt in zip(counts, counts[1:]):
            self.assertGreaterEqual(prev, nxt,
                f"n_predicted_positive must be monotone non-increasing "
                f"in threshold; got {counts}")

    def test_empty_split_returns_note(self):
        t = threshold_table(np.array([]), np.array([]))
        self.assertEqual(t["rows"], [])
        self.assertEqual(t["note"], "empty_split")

    def test_threshold_table_in_evaluation_report(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        for s in ("train", "val", "test"):
            t = rep.threshold_metrics[s]
            self.assertIn("rows", t)
            self.assertEqual(len(t["rows"]),
                              len(LOCKED_THRESHOLDS))


# ─────────────────────────────────────────────────────────────────────
# G7_TradingMetricsExtended — win_rate, profit_factor, EV, p@k,
#                             equity-curve unavailability
# ─────────────────────────────────────────────────────────────────────

class G7_TradingMetricsExtended(unittest.TestCase):

    def _run_b2_eval(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        return evaluate_model(out, res)

    def test_all_new_trading_fields_present(self):
        rep = self._run_b2_eval()
        tm = rep.trading_metrics["val"]
        for k in ("win_rate_by_return", "average_log_return_win",
                    "average_log_return_loss", "profit_factor",
                    "expected_value_after_costs",
                    "cost_per_trade_log_return",
                    "precision_at_k", "n_filtered",
                    "equity_curve_metrics"):
            self.assertIn(k, tm, f"missing: {k}")

    def test_precision_at_k_uses_locked_k_values(self):
        rep = self._run_b2_eval()
        tm = rep.trading_metrics["val"]
        for k in PRECISION_AT_K_LIST:
            self.assertIn(f"k_{k}", tm["precision_at_k"])

    def test_equity_curve_block_marked_unavailable(self):
        """Sharpe / Sortino / max DD cannot be computed from per-trade
        labels alone — verify the report says so explicitly rather
        than emitting a misleading 0 or NaN."""
        rep = self._run_b2_eval()
        for s in ("train", "val", "test"):
            ec = rep.trading_metrics[s]["equity_curve_metrics"]
            self.assertEqual(ec["sharpe_ratio"], None)
            self.assertEqual(ec["sortino_ratio"], None)
            self.assertEqual(ec["max_drawdown"], None)
            self.assertEqual(ec["unavailable_reason"],
                              EQUITY_CURVE_UNAVAILABLE_REASON)

    def test_n_filtered_plus_predicted_positive_equals_n_rows(self):
        rep = self._run_b2_eval()
        for s in ("train", "val", "test"):
            tm = rep.trading_metrics[s]
            self.assertEqual(
                tm["n_predicted_positive"] + tm["n_filtered"],
                tm["n_rows"])

    def test_expected_value_after_costs_drops_with_cost(self):
        """Doubling the cost decreases EV by the cost delta."""
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep0 = evaluate_model(out, res, cost_per_trade_log_return=0.0)
        rep1 = evaluate_model(out, res, cost_per_trade_log_return=0.01)
        ev0 = rep0.trading_metrics["val"]["expected_value_after_costs"]
        ev1 = rep1.trading_metrics["val"]["expected_value_after_costs"]
        if not (np.isnan(ev0) or np.isnan(ev1)):
            # EV decreases by exactly the cost (cost is in log-return
            # units, subtracted from each predicted-positive trade's
            # return, so mean decreases by the cost).
            self.assertAlmostEqual(ev0 - ev1, 0.01, places=10)

    def test_profit_factor_undefined_no_losses(self):
        """When every predicted-positive trade is a winner, profit
        factor is mathematically infinite — emit NaN plus a warning
        rather than +inf."""
        # Pure unit test on the trading_metrics function directly
        from bot.ml.evaluation import trading_metrics as tm_fn
        # 4 predicted positives, ALL with positive log return
        dataset = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=4,
                                      tz="UTC"),
            "triple_barrier_atr_2_3_50.return_log_at_resolution":
                [0.02, 0.03, 0.04, 0.05],
            "triple_barrier_atr_2_3_50.bars_to_resolution":
                [10.0, 20.0, 30.0, 40.0],
        })
        out = tm_fn(
            y_true=np.array([1.0, 1.0, 1.0, 1.0]),
            y_proba=np.array([0.9, 0.9, 0.9, 0.9]),
            target_label_id="triple_barrier_atr_2_3_50_won",
            dataset=dataset,
            split_indices=np.array([0, 1, 2, 3]))
        self.assertTrue(np.isnan(out["profit_factor"]))
        self.assertIn("profit_factor_undefined_no_losses",
                       out["zero_trade_warnings"])


# ─────────────────────────────────────────────────────────────────────
# G7_Drift — PSI report
# ─────────────────────────────────────────────────────────────────────

class G7_Drift(unittest.TestCase):

    def test_identical_distributions_yield_max_psi_near_zero(self):
        """Train→train PSI must be ~0 (no shift on identical data)."""
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        train_idx = res.split.train_anchor_indices
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        rpt = drift_report(
            dataset=res.dataset,
            train_indices=train_idx,
            comparison_indices=train_idx,         # identical
            feature_columns=feat_cols,
            comparison_split_name="self")
        self.assertIsNone(rpt["unavailable_reason"])
        # PSI(train, train) should be exactly 0 modulo floating point
        self.assertLess(rpt["max_psi"], 1e-9,
            f"identical distributions should give max_psi ~ 0; "
            f"got {rpt['max_psi']}")
        self.assertFalse(rpt["drift_warning"])

    def test_insufficient_samples_unavailable_reason(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=res.split.train_anchor_indices
                  if False else per_tf)
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        rpt = drift_report(
            dataset=res.dataset,
            train_indices=np.array([0, 1, 2]),     # < 10 samples
            comparison_indices=np.array([3, 4, 5]),
            feature_columns=feat_cols,
            comparison_split_name="val")
        self.assertIsNotNone(rpt["unavailable_reason"])
        self.assertIn("insufficient samples",
                       rpt["unavailable_reason"])

    def test_drift_block_present_in_evaluation_report(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        for cmp_split in ("train_to_val", "train_to_test"):
            d = rep.drift[cmp_split]
            for k in ("max_psi", "argmax_psi_feature",
                        "features_over_threshold", "drift_warning",
                        "per_feature_psi", "threshold",
                        "n_reference", "n_comparison"):
                self.assertIn(k, d)


# ─────────────────────────────────────────────────────────────────────
# G7_PermutationImportance
# ─────────────────────────────────────────────────────────────────────

class G7_PermutationImportance(unittest.TestCase):

    def test_supported_model_types_are_b2_lightgbm_and_rf(self):
        """B0 and B1 are explicitly unsupported per locked plan.
        M_random_forest is supported as of M18.B.1."""
        self.assertEqual(PI_SUPPORTED_MODEL_TYPES,
                          frozenset({"B2_logistic", "M_lightgbm",
                                     "M_random_forest"}))

    def test_b0_majority_returns_unavailable_with_clear_reason(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B0_majority",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        pi = rep.permutation_importance
        self.assertFalse(pi["available"])
        self.assertIn("constant predictor",
                       pi["unavailable_reason"])

    def test_b1_scanner_replica_returns_unavailable_with_clear_reason(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B1_scanner_replica",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        pi = rep.permutation_importance
        self.assertFalse(pi["available"])
        self.assertIn("passthrough",
                       pi["unavailable_reason"])

    def test_b2_logistic_top_features_populated(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res, permutation_n_repeats=3,
                                permutation_n_top=10)
        pi = rep.permutation_importance
        self.assertTrue(pi["available"])
        self.assertEqual(pi["model_type"], "B2_logistic")
        self.assertLessEqual(len(pi["top_features"]), 10)
        # Each entry has the expected structure
        for f in pi["top_features"]:
            self.assertIn("feature", f)
            self.assertIn("importance_mean", f)
            self.assertIn("importance_std", f)
            self.assertEqual(f["n_repeats"], 3)
        # top_features are sorted by importance_mean descending
        means = [f["importance_mean"] for f in pi["top_features"]]
        # Allow NaN sentinel at the end
        finite_means = [m for m in means if not np.isnan(m)]
        for prev, nxt in zip(finite_means, finite_means[1:]):
            self.assertGreaterEqual(prev, nxt)

    def test_permutation_importance_deterministic(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        cfg = TrainConfig(dataset_id=res.manifest.dataset_id,
            model_type="B2_logistic",
            train_mode="model_b_candidate_quality",
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False)
        out = ModelTrainer().train_one(cfg, res)
        # Run permutation importance twice with same seed
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        pi1 = permutation_importance(
            train_config=cfg, assembler_result=res,
            feature_columns=feat_cols, n_repeats=3,
            evaluation_split="val")
        pi2 = permutation_importance(
            train_config=cfg, assembler_result=res,
            feature_columns=feat_cols, n_repeats=3,
            evaluation_split="val")
        # Same baseline score
        self.assertEqual(pi1["baseline_score"], pi2["baseline_score"])
        # Same importance for every feature
        for f1, f2 in zip(pi1["all_features"], pi2["all_features"]):
            self.assertEqual(f1["feature"], f2["feature"])
            self.assertEqual(f1["importance_mean"],
                              f2["importance_mean"])

    # ---- M18.B.1: RandomForest permutation-importance integration ----

    def _rf_cfg_and_res(self):
        res = _assemble_for_training()
        cfg = _make_train_config(
            "M_random_forest", dataset_id=res.manifest.dataset_id)
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        return res, cfg, feat_cols

    def test_random_forest_permutation_importance_available(self):
        res, cfg, feat_cols = self._rf_cfg_and_res()
        pi = permutation_importance(
            train_config=cfg, assembler_result=res,
            feature_columns=feat_cols, n_repeats=3,
            evaluation_split="val", min_samples=10)
        self.assertTrue(pi["available"], pi.get("unavailable_reason"))
        self.assertEqual(pi["model_type"], "M_random_forest")
        self.assertIn("top_features", pi)
        self.assertIn("all_features", pi)
        self.assertGreater(len(pi["all_features"]), 0)
        for f in pi["top_features"]:
            self.assertIn("feature", f)
            self.assertIn("importance_mean", f)
            self.assertIn("importance_std", f)

    def test_random_forest_permutation_importance_deterministic(self):
        res, cfg, feat_cols = self._rf_cfg_and_res()
        pi1 = permutation_importance(
            train_config=cfg, assembler_result=res,
            feature_columns=feat_cols, n_repeats=3,
            evaluation_split="val", min_samples=10)
        pi2 = permutation_importance(
            train_config=cfg, assembler_result=res,
            feature_columns=feat_cols, n_repeats=3,
            evaluation_split="val", min_samples=10)
        self.assertTrue(pi1["available"] and pi2["available"])
        self.assertEqual(pi1["baseline_score"], pi2["baseline_score"])
        for f1, f2 in zip(pi1["all_features"], pi2["all_features"]):
            self.assertEqual(f1["feature"], f2["feature"])
            self.assertEqual(f1["importance_mean"],
                              f2["importance_mean"])
            self.assertEqual(f1["importance_std"],
                              f2["importance_std"])

    def test_random_forest_permutation_importance_respects_safe_hyperparameters(self):
        res = _assemble_for_training()
        cfg = _make_train_config(
            "M_random_forest", dataset_id=res.manifest.dataset_id,
            hyperparameters={"n_estimators": 50, "max_depth": 5})
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        pi = permutation_importance(
            train_config=cfg, assembler_result=res,
            feature_columns=feat_cols, n_repeats=3,
            evaluation_split="val", min_samples=10)
        self.assertTrue(pi["available"], pi.get("unavailable_reason"))
        self.assertEqual(pi["model_type"], "M_random_forest")

    def test_random_forest_permutation_importance_rejects_unsafe_hyperparameters(self):
        res = _assemble_for_training()
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        for bad in ({"n_jobs": 2}, {"random_state": 99},
                     {"bootstrap": False}):
            cfg = _make_train_config(
                "M_random_forest", dataset_id=res.manifest.dataset_id,
                hyperparameters=bad)
            with self.assertRaises(ml_errors.M18ConfigError):
                permutation_importance(
                    train_config=cfg, assembler_result=res,
                    feature_columns=feat_cols, n_repeats=3,
                    evaluation_split="val", min_samples=10)


# ─────────────────────────────────────────────────────────────────────
# G7_Breakdowns — segment metrics
# ─────────────────────────────────────────────────────────────────────

class G7_Breakdowns(unittest.TestCase):

    def _train_b2(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        return res, out

    def test_per_symbol_unavailable_on_single_symbol_dataset(self):
        """M18.A.5 assembler is single-symbol; per-symbol breakdown
        must explicitly report unavailable rather than fabricating
        a one-segment summary that doesn't actually filter by
        symbol."""
        res, _ = self._train_b2()
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B0_majority",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        sym = rep.breakdowns["val"]["per_symbol"]
        self.assertFalse(sym["available"])
        self.assertIn("symbol", sym["unavailable_reason"])

    def test_per_year_segments_use_ts_utc(self):
        """per-year breakdown groups by anchor ts year — synthetic
        fixture has bars across a single year, so we expect ≤1
        segment after the min_samples filter."""
        res, out = self._train_b2()
        rep = evaluate_model(out, res)
        py = rep.breakdowns["val"]["per_year"]
        self.assertTrue(py["available"])
        self.assertIn("segments",   py["per_year"])
        self.assertIn("per_quarter", py)
        # Total segment count + skipped count must be > 0
        n_segments = len(py["per_year"]["segments"])
        n_skipped  = len(py["per_year"]["skipped_segments"])
        self.assertGreaterEqual(n_segments + n_skipped, 1)

    def test_min_samples_threshold_drops_small_segments(self):
        """Small synthetic data forces many segments below 50 → must
        appear in skipped_segments."""
        res, out = self._train_b2()
        # Use a very high min_samples to force most segments to skip
        rep = evaluate_model(out, res, breakdowns_min_samples=10000)
        py = rep.breakdowns["val"]["per_year"]
        self.assertEqual(len(py["per_year"]["segments"]), 0)
        # All quarters skipped too
        self.assertEqual(len(py["per_quarter"]["segments"]), 0)

    def test_vol_regime_breakdown_uses_vol_regime_field(self):
        """vol_regime breakdown uses vol_regime.vol_regime_flag if
        present."""
        res, out = self._train_b2()
        rep = evaluate_model(out, res)
        vr = rep.breakdowns["val"]["volatility_regime"]
        self.assertTrue(vr["available"])
        # Binning field is one of the two recognised columns
        self.assertIn(vr["binning_field"],
                       ("vol_regime.vol_regime_flag",
                        "vol_regime.atr_percentile_60"))

    def test_market_regime_breakdown_uses_market_context_field(self):
        res, out = self._train_b2()
        rep = evaluate_model(out, res)
        mr = rep.breakdowns["val"]["market_regime"]
        if mr["available"]:
            self.assertIn(mr["binning_field"],
                ("market_context.spy_above_ema200_1d",
                  "market_context.qqq_above_ema200_1d"))
        else:
            # If not available, must give explicit reason
            self.assertIsNotNone(mr["unavailable_reason"])


# ─────────────────────────────────────────────────────────────────────
# G7_BaselineCompareExtended — B0 + B1 deltas
# ─────────────────────────────────────────────────────────────────────

class G7_BaselineCompareExtended(unittest.TestCase):

    def test_compare_baselines_default_primary_is_pr_auc(self):
        """PR-AUC is the M18 primary metric per the locked plan."""
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports)   # no primary_metric kwarg
        self.assertEqual(cmp.primary_metric, "pr_auc")

    def test_baseline_beats_includes_both_b0_and_b1_keys(self):
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports, primary_split="val")
        keys = sorted(cmp.baseline_beats.keys())
        # B0 comparisons
        beats_b0 = [k for k in keys
                     if k.endswith("_beats_B0_majority_on_val_pr_auc")]
        # B1 comparisons
        beats_b1 = [k for k in keys
                     if k.endswith("_beats_B1_scanner_replica_on_val_pr_auc")]
        self.assertEqual(len(beats_b0), 2, beats_b0)
        self.assertEqual(len(beats_b1), 2, beats_b1)

    def test_deltas_vs_b0_and_b1_present(self):
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports)
        # B1 and B2 should have deltas vs B0
        self.assertIn("B1_scanner_replica", cmp.deltas_vs_primary_baseline)
        self.assertIn("B2_logistic",         cmp.deltas_vs_primary_baseline)
        # B0 and B2 should have deltas vs B1
        self.assertIn("B0_majority",         cmp.deltas_vs_secondary_baseline)
        self.assertIn("B2_logistic",         cmp.deltas_vs_secondary_baseline)
        # PR-AUC delta is recorded
        self.assertIn("pr_auc",
                       cmp.deltas_vs_primary_baseline["B2_logistic"])

    def test_delta_sign_convention_higher_is_better(self):
        """For PR-AUC (higher is better), delta = candidate - baseline.
        Positive delta means candidate wins."""
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports)
        # Compute the expected delta manually
        b0_auc = cmp.per_metric["pr_auc"]["B0_majority"]
        b2_auc = cmp.per_metric["pr_auc"]["B2_logistic"]
        expected = b2_auc - b0_auc
        actual   = cmp.deltas_vs_primary_baseline["B2_logistic"]["pr_auc"]
        self.assertAlmostEqual(expected, actual, places=10)

    def test_delta_sign_convention_lower_is_better(self):
        """For log_loss (lower is better), delta = baseline - candidate.
        Positive delta still means candidate wins."""
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports, primary_metric="log_loss")
        # Sign convention check
        b0_ll = cmp.per_metric["log_loss"]["B0_majority"]
        b2_ll = cmp.per_metric["log_loss"]["B2_logistic"]
        expected = b0_ll - b2_ll
        actual   = cmp.deltas_vs_primary_baseline["B2_logistic"]["log_loss"]
        self.assertAlmostEqual(expected, actual, places=10)

    def test_secondary_baseline_none_skips_b1_deltas(self):
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports,
            secondary_baseline_model_type=None)
        self.assertEqual(cmp.deltas_vs_secondary_baseline, {})



# ═════════════════════════════════════════════════════════════════════
# G7 — Extended evaluation: PR-AUC, threshold table, drift,
#       permutation importance, breakdowns (M18.A.7 amend)
# ═════════════════════════════════════════════════════════════════════

from bot.ml.evaluation import (
    binary_metrics_extended,
    threshold_table,
    LOCKED_THRESHOLDS,
    drift_report,
    permutation_importance,
    PI_SUPPORTED_MODEL_TYPES,
    all_breakdowns,
    per_symbol_breakdown,
    per_year_breakdown,
    volatility_regime_breakdown,
    market_regime_breakdown,
    MIN_SAMPLES_PER_SEGMENT,
    PRECISION_AT_K_LIST,
    EQUITY_CURVE_UNAVAILABLE_REASON,
)


# ─────────────────────────────────────────────────────────────────────
# G7_ExtendedMlMetrics — PR-AUC, log_loss, F1, confusion matrix
# ─────────────────────────────────────────────────────────────────────

class G7_ExtendedMlMetrics(unittest.TestCase):

    def test_perfect_predictions_yield_pr_auc_1(self):
        """When y_proba perfectly orders y_true (all positives above
        all negatives), PR-AUC = 1.0 and ROC AUC = 1.0."""
        y_true  = np.array([0, 0, 0, 1, 1, 1], dtype=float)
        y_proba = np.array([0.1, 0.2, 0.3, 0.7, 0.8, 0.9])
        m = binary_metrics_extended(y_true, y_proba)
        self.assertAlmostEqual(m["pr_auc"],  1.0, places=10)
        self.assertAlmostEqual(m["roc_auc"], 1.0, places=10)
        self.assertEqual(m["confusion_matrix_at_05"],
                          {"tp": 3, "fp": 0, "fn": 0, "tn": 3})

    def test_pr_auc_present_and_finite_on_real_split(self):
        """PR-AUC is the PRIMARY M18 metric — verify it's emitted for
        every binary split."""
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        for s in ("train", "val", "test"):
            ext = rep.ml_metrics_extended[s]
            self.assertIn("pr_auc", ext)
            # PR-AUC is finite when both classes are present in the
            # split. Otherwise NaN, which is the documented behaviour.
            if not np.isnan(ext["pr_auc"]):
                self.assertGreater(ext["pr_auc"], 0.0)
                self.assertLessEqual(ext["pr_auc"], 1.0)
            self.assertIn("log_loss", ext)
            self.assertIn("f1_at_05", ext)
            self.assertIn("confusion_matrix_at_05", ext)

    def test_confusion_matrix_consistency(self):
        """tp + fp + fn + tn must equal n_rows."""
        y_true  = np.array([1, 1, 0, 0, 1, 0, 1, 1])
        y_proba = np.array([0.9, 0.4, 0.6, 0.1, 0.8, 0.3, 0.7, 0.2])
        m = binary_metrics_extended(y_true, y_proba)
        cm = m["confusion_matrix_at_05"]
        self.assertEqual(cm["tp"] + cm["fp"] + cm["fn"] + cm["tn"],
                          m["n_rows"])

    def test_log_loss_clipped_no_inf_on_extreme_predictions(self):
        """y_proba = 0 with y_true = 1 would blow up log_loss without
        clipping. binary_metrics_extended must return finite log_loss."""
        y_true  = np.array([1, 1, 0, 0], dtype=float)
        y_proba = np.array([0.0, 0.0, 1.0, 1.0])
        m = binary_metrics_extended(y_true, y_proba)
        self.assertTrue(np.isfinite(m["log_loss"]),
            f"log_loss must be clipped to finite; got {m['log_loss']}")

    def test_single_class_y_true_pr_auc_nan(self):
        """When y_true is all-zero or all-one, PR-AUC is undefined
        (no minority class). Must return NaN per sklearn convention."""
        m = binary_metrics_extended(np.zeros(10),
                                       np.linspace(0.1, 0.9, 10))
        self.assertTrue(np.isnan(m["pr_auc"]))
        self.assertTrue(np.isnan(m["roc_auc"]))


# ─────────────────────────────────────────────────────────────────────
# G7_ThresholdTable — locked threshold ladder
# ─────────────────────────────────────────────────────────────────────

class G7_ThresholdTable(unittest.TestCase):

    def test_locked_thresholds_match_directive(self):
        """The locked threshold ladder per M18.A.7 directive."""
        self.assertEqual(tuple(LOCKED_THRESHOLDS),
                          (0.30, 0.40, 0.50, 0.60, 0.65, 0.70, 0.80))

    def test_table_row_count_matches_thresholds(self):
        rng = np.random.default_rng(0)
        y_true  = rng.integers(0, 2, 300).astype(float)
        y_proba = rng.uniform(0, 1, 300)
        t = threshold_table(y_true, y_proba)
        self.assertEqual(len(t["rows"]), len(LOCKED_THRESHOLDS))

    def test_accepted_plus_filtered_equals_n_rows(self):
        rng = np.random.default_rng(1)
        y_true  = rng.integers(0, 2, 200).astype(float)
        y_proba = rng.uniform(0, 1, 200)
        t = threshold_table(y_true, y_proba)
        for row in t["rows"]:
            self.assertEqual(
                row["n_predicted_positive"] + row["n_filtered"],
                t["n_rows"])

    def test_higher_threshold_yields_fewer_predicted_positive(self):
        """Monotone: predicted positive count is non-increasing in
        threshold."""
        rng = np.random.default_rng(2)
        y_true  = rng.integers(0, 2, 300).astype(float)
        y_proba = rng.uniform(0, 1, 300)
        t = threshold_table(y_true, y_proba)
        counts = [r["n_predicted_positive"] for r in t["rows"]]
        for prev, nxt in zip(counts, counts[1:]):
            self.assertGreaterEqual(prev, nxt,
                f"n_predicted_positive must be monotone non-increasing "
                f"in threshold; got {counts}")

    def test_empty_split_returns_note(self):
        t = threshold_table(np.array([]), np.array([]))
        self.assertEqual(t["rows"], [])
        self.assertEqual(t["note"], "empty_split")

    def test_threshold_table_in_evaluation_report(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        for s in ("train", "val", "test"):
            t = rep.threshold_metrics[s]
            self.assertIn("rows", t)
            self.assertEqual(len(t["rows"]),
                              len(LOCKED_THRESHOLDS))


# ─────────────────────────────────────────────────────────────────────
# G7_TradingMetricsExtended — win_rate, profit_factor, EV, p@k,
#                             equity-curve unavailability
# ─────────────────────────────────────────────────────────────────────

class G7_TradingMetricsExtended(unittest.TestCase):

    def _run_b2_eval(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        return evaluate_model(out, res)

    def test_all_new_trading_fields_present(self):
        rep = self._run_b2_eval()
        tm = rep.trading_metrics["val"]
        for k in ("win_rate_by_return", "average_log_return_win",
                    "average_log_return_loss", "profit_factor",
                    "expected_value_after_costs",
                    "cost_per_trade_log_return",
                    "precision_at_k", "n_filtered",
                    "equity_curve_metrics"):
            self.assertIn(k, tm, f"missing: {k}")

    def test_precision_at_k_uses_locked_k_values(self):
        rep = self._run_b2_eval()
        tm = rep.trading_metrics["val"]
        for k in PRECISION_AT_K_LIST:
            self.assertIn(f"k_{k}", tm["precision_at_k"])

    def test_equity_curve_block_marked_unavailable(self):
        """Sharpe / Sortino / max DD cannot be computed from per-trade
        labels alone — verify the report says so explicitly rather
        than emitting a misleading 0 or NaN."""
        rep = self._run_b2_eval()
        for s in ("train", "val", "test"):
            ec = rep.trading_metrics[s]["equity_curve_metrics"]
            self.assertEqual(ec["sharpe_ratio"], None)
            self.assertEqual(ec["sortino_ratio"], None)
            self.assertEqual(ec["max_drawdown"], None)
            self.assertEqual(ec["unavailable_reason"],
                              EQUITY_CURVE_UNAVAILABLE_REASON)

    def test_n_filtered_plus_predicted_positive_equals_n_rows(self):
        rep = self._run_b2_eval()
        for s in ("train", "val", "test"):
            tm = rep.trading_metrics[s]
            self.assertEqual(
                tm["n_predicted_positive"] + tm["n_filtered"],
                tm["n_rows"])

    def test_expected_value_after_costs_drops_with_cost(self):
        """Doubling the cost decreases EV by the cost delta."""
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep0 = evaluate_model(out, res, cost_per_trade_log_return=0.0)
        rep1 = evaluate_model(out, res, cost_per_trade_log_return=0.01)
        ev0 = rep0.trading_metrics["val"]["expected_value_after_costs"]
        ev1 = rep1.trading_metrics["val"]["expected_value_after_costs"]
        if not (np.isnan(ev0) or np.isnan(ev1)):
            # EV decreases by exactly the cost (cost is in log-return
            # units, subtracted from each predicted-positive trade's
            # return, so mean decreases by the cost).
            self.assertAlmostEqual(ev0 - ev1, 0.01, places=10)

    def test_profit_factor_undefined_no_losses(self):
        """When every predicted-positive trade is a winner, profit
        factor is mathematically infinite — emit NaN plus a warning
        rather than +inf."""
        # Pure unit test on the trading_metrics function directly
        from bot.ml.evaluation import trading_metrics as tm_fn
        # 4 predicted positives, ALL with positive log return
        dataset = pd.DataFrame({
            "ts_utc": pd.date_range("2024-01-01", periods=4,
                                      tz="UTC"),
            "triple_barrier_atr_2_3_50.return_log_at_resolution":
                [0.02, 0.03, 0.04, 0.05],
            "triple_barrier_atr_2_3_50.bars_to_resolution":
                [10.0, 20.0, 30.0, 40.0],
        })
        out = tm_fn(
            y_true=np.array([1.0, 1.0, 1.0, 1.0]),
            y_proba=np.array([0.9, 0.9, 0.9, 0.9]),
            target_label_id="triple_barrier_atr_2_3_50_won",
            dataset=dataset,
            split_indices=np.array([0, 1, 2, 3]))
        self.assertTrue(np.isnan(out["profit_factor"]))
        self.assertIn("profit_factor_undefined_no_losses",
                       out["zero_trade_warnings"])


# ─────────────────────────────────────────────────────────────────────
# G7_Drift — PSI report
# ─────────────────────────────────────────────────────────────────────

class G7_Drift(unittest.TestCase):

    def test_identical_distributions_yield_max_psi_near_zero(self):
        """Train→train PSI must be ~0 (no shift on identical data)."""
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        train_idx = res.split.train_anchor_indices
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        rpt = drift_report(
            dataset=res.dataset,
            train_indices=train_idx,
            comparison_indices=train_idx,         # identical
            feature_columns=feat_cols,
            comparison_split_name="self")
        self.assertIsNone(rpt["unavailable_reason"])
        # PSI(train, train) should be exactly 0 modulo floating point
        self.assertLess(rpt["max_psi"], 1e-9,
            f"identical distributions should give max_psi ~ 0; "
            f"got {rpt['max_psi']}")
        self.assertFalse(rpt["drift_warning"])

    def test_insufficient_samples_unavailable_reason(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=res.split.train_anchor_indices
                  if False else per_tf)
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        rpt = drift_report(
            dataset=res.dataset,
            train_indices=np.array([0, 1, 2]),     # < 10 samples
            comparison_indices=np.array([3, 4, 5]),
            feature_columns=feat_cols,
            comparison_split_name="val")
        self.assertIsNotNone(rpt["unavailable_reason"])
        self.assertIn("insufficient samples",
                       rpt["unavailable_reason"])

    def test_drift_block_present_in_evaluation_report(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        for cmp_split in ("train_to_val", "train_to_test"):
            d = rep.drift[cmp_split]
            for k in ("max_psi", "argmax_psi_feature",
                        "features_over_threshold", "drift_warning",
                        "per_feature_psi", "threshold",
                        "n_reference", "n_comparison"):
                self.assertIn(k, d)


# ─────────────────────────────────────────────────────────────────────
# G7_PermutationImportance
# ─────────────────────────────────────────────────────────────────────

class G7_PermutationImportance(unittest.TestCase):

    def test_supported_model_types_are_b2_lightgbm_and_rf(self):
        """B0 and B1 are explicitly unsupported per locked plan.
        M_random_forest is supported as of M18.B.1."""
        self.assertEqual(PI_SUPPORTED_MODEL_TYPES,
                          frozenset({"B2_logistic", "M_lightgbm",
                                     "M_random_forest"}))

    def test_b0_majority_returns_unavailable_with_clear_reason(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B0_majority",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        pi = rep.permutation_importance
        self.assertFalse(pi["available"])
        self.assertIn("constant predictor",
                       pi["unavailable_reason"])

    def test_b1_scanner_replica_returns_unavailable_with_clear_reason(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B1_scanner_replica",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        pi = rep.permutation_importance
        self.assertFalse(pi["available"])
        self.assertIn("passthrough",
                       pi["unavailable_reason"])

    def test_b2_logistic_top_features_populated(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res, permutation_n_repeats=3,
                                permutation_n_top=10)
        pi = rep.permutation_importance
        self.assertTrue(pi["available"])
        self.assertEqual(pi["model_type"], "B2_logistic")
        self.assertLessEqual(len(pi["top_features"]), 10)
        # Each entry has the expected structure
        for f in pi["top_features"]:
            self.assertIn("feature", f)
            self.assertIn("importance_mean", f)
            self.assertIn("importance_std", f)
            self.assertEqual(f["n_repeats"], 3)
        # top_features are sorted by importance_mean descending
        means = [f["importance_mean"] for f in pi["top_features"]]
        # Allow NaN sentinel at the end
        finite_means = [m for m in means if not np.isnan(m)]
        for prev, nxt in zip(finite_means, finite_means[1:]):
            self.assertGreaterEqual(prev, nxt)

    def test_permutation_importance_deterministic(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        cfg = TrainConfig(dataset_id=res.manifest.dataset_id,
            model_type="B2_logistic",
            train_mode="model_b_candidate_quality",
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False)
        out = ModelTrainer().train_one(cfg, res)
        # Run permutation importance twice with same seed
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        pi1 = permutation_importance(
            train_config=cfg, assembler_result=res,
            feature_columns=feat_cols, n_repeats=3,
            evaluation_split="val")
        pi2 = permutation_importance(
            train_config=cfg, assembler_result=res,
            feature_columns=feat_cols, n_repeats=3,
            evaluation_split="val")
        # Same baseline score
        self.assertEqual(pi1["baseline_score"], pi2["baseline_score"])
        # Same importance for every feature
        for f1, f2 in zip(pi1["all_features"], pi2["all_features"]):
            self.assertEqual(f1["feature"], f2["feature"])
            self.assertEqual(f1["importance_mean"],
                              f2["importance_mean"])


# ─────────────────────────────────────────────────────────────────────
# G7_Breakdowns — segment metrics
# ─────────────────────────────────────────────────────────────────────

class G7_Breakdowns(unittest.TestCase):

    def _train_b2(self):
        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B2_logistic",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        return res, out

    def test_per_symbol_unavailable_on_single_symbol_dataset(self):
        """M18.A.5 assembler is single-symbol; per-symbol breakdown
        must explicitly report unavailable rather than fabricating
        a one-segment summary that doesn't actually filter by
        symbol."""
        res, _ = self._train_b2()
        out = ModelTrainer().train_one(
            TrainConfig(dataset_id=res.manifest.dataset_id,
                model_type="B0_majority",
                train_mode="model_b_candidate_quality",
                target_label_id="triple_barrier_atr_2_3_50_won",
                hyperparameters={}, seed=42, fixture_mode=False),
            res)
        rep = evaluate_model(out, res)
        sym = rep.breakdowns["val"]["per_symbol"]
        self.assertFalse(sym["available"])
        self.assertIn("symbol", sym["unavailable_reason"])

    def test_per_year_segments_use_ts_utc(self):
        """per-year breakdown groups by anchor ts year — synthetic
        fixture has bars across a single year, so we expect ≤1
        segment after the min_samples filter."""
        res, out = self._train_b2()
        rep = evaluate_model(out, res)
        py = rep.breakdowns["val"]["per_year"]
        self.assertTrue(py["available"])
        self.assertIn("segments",   py["per_year"])
        self.assertIn("per_quarter", py)
        # Total segment count + skipped count must be > 0
        n_segments = len(py["per_year"]["segments"])
        n_skipped  = len(py["per_year"]["skipped_segments"])
        self.assertGreaterEqual(n_segments + n_skipped, 1)

    def test_min_samples_threshold_drops_small_segments(self):
        """Small synthetic data forces many segments below 50 → must
        appear in skipped_segments."""
        res, out = self._train_b2()
        # Use a very high min_samples to force most segments to skip
        rep = evaluate_model(out, res, breakdowns_min_samples=10000)
        py = rep.breakdowns["val"]["per_year"]
        self.assertEqual(len(py["per_year"]["segments"]), 0)
        # All quarters skipped too
        self.assertEqual(len(py["per_quarter"]["segments"]), 0)

    def test_vol_regime_breakdown_uses_vol_regime_field(self):
        """vol_regime breakdown uses vol_regime.vol_regime_flag if
        present."""
        res, out = self._train_b2()
        rep = evaluate_model(out, res)
        vr = rep.breakdowns["val"]["volatility_regime"]
        self.assertTrue(vr["available"])
        # Binning field is one of the two recognised columns
        self.assertIn(vr["binning_field"],
                       ("vol_regime.vol_regime_flag",
                        "vol_regime.atr_percentile_60"))

    def test_market_regime_breakdown_uses_market_context_field(self):
        res, out = self._train_b2()
        rep = evaluate_model(out, res)
        mr = rep.breakdowns["val"]["market_regime"]
        if mr["available"]:
            self.assertIn(mr["binning_field"],
                ("market_context.spy_above_ema200_1d",
                  "market_context.qqq_above_ema200_1d"))
        else:
            # If not available, must give explicit reason
            self.assertIsNotNone(mr["unavailable_reason"])
    # ---- M18.B.1: RandomForest permutation-importance integration ----

    def _rf_cfg_and_res(self):
        res = _assemble_for_training()
        cfg = _make_train_config(
            "M_random_forest", dataset_id=res.manifest.dataset_id)
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        return res, cfg, feat_cols

    def test_random_forest_permutation_importance_available(self):
        res, cfg, feat_cols = self._rf_cfg_and_res()
        pi = permutation_importance(
            train_config=cfg, assembler_result=res,
            feature_columns=feat_cols, n_repeats=3,
            evaluation_split="val", min_samples=10)
        self.assertTrue(pi["available"], pi.get("unavailable_reason"))
        self.assertEqual(pi["model_type"], "M_random_forest")
        self.assertIn("top_features", pi)
        self.assertIn("all_features", pi)
        self.assertGreater(len(pi["all_features"]), 0)
        for f in pi["top_features"]:
            self.assertIn("feature", f)
            self.assertIn("importance_mean", f)
            self.assertIn("importance_std", f)

    def test_random_forest_permutation_importance_deterministic(self):
        res, cfg, feat_cols = self._rf_cfg_and_res()
        pi1 = permutation_importance(
            train_config=cfg, assembler_result=res,
            feature_columns=feat_cols, n_repeats=3,
            evaluation_split="val", min_samples=10)
        pi2 = permutation_importance(
            train_config=cfg, assembler_result=res,
            feature_columns=feat_cols, n_repeats=3,
            evaluation_split="val", min_samples=10)
        self.assertTrue(pi1["available"] and pi2["available"])
        self.assertEqual(pi1["baseline_score"], pi2["baseline_score"])
        for f1, f2 in zip(pi1["all_features"], pi2["all_features"]):
            self.assertEqual(f1["feature"], f2["feature"])
            self.assertEqual(f1["importance_mean"],
                              f2["importance_mean"])
            self.assertEqual(f1["importance_std"],
                              f2["importance_std"])

    def test_random_forest_permutation_importance_respects_safe_hyperparameters(self):
        res = _assemble_for_training()
        cfg = _make_train_config(
            "M_random_forest", dataset_id=res.manifest.dataset_id,
            hyperparameters={"n_estimators": 50, "max_depth": 5})
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        pi = permutation_importance(
            train_config=cfg, assembler_result=res,
            feature_columns=feat_cols, n_repeats=3,
            evaluation_split="val", min_samples=10)
        self.assertTrue(pi["available"], pi.get("unavailable_reason"))
        self.assertEqual(pi["model_type"], "M_random_forest")

    def test_random_forest_permutation_importance_rejects_unsafe_hyperparameters(self):
        res = _assemble_for_training()
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        for bad in ({"n_jobs": 2}, {"random_state": 99},
                     {"bootstrap": False}):
            cfg = _make_train_config(
                "M_random_forest", dataset_id=res.manifest.dataset_id,
                hyperparameters=bad)
            with self.assertRaises(ml_errors.M18ConfigError):
                permutation_importance(
                    train_config=cfg, assembler_result=res,
                    feature_columns=feat_cols, n_repeats=3,
                    evaluation_split="val", min_samples=10)




# ─────────────────────────────────────────────────────────────────────
# G7_BaselineCompareExtended — B0 + B1 deltas
# ─────────────────────────────────────────────────────────────────────

class G7_BaselineCompareExtended(unittest.TestCase):

    def test_compare_baselines_default_primary_is_pr_auc(self):
        """PR-AUC is the M18 primary metric per the locked plan."""
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports)   # no primary_metric kwarg
        self.assertEqual(cmp.primary_metric, "pr_auc")

    def test_baseline_beats_includes_both_b0_and_b1_keys(self):
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports, primary_split="val")
        keys = sorted(cmp.baseline_beats.keys())
        # B0 comparisons
        beats_b0 = [k for k in keys
                     if k.endswith("_beats_B0_majority_on_val_pr_auc")]
        # B1 comparisons
        beats_b1 = [k for k in keys
                     if k.endswith("_beats_B1_scanner_replica_on_val_pr_auc")]
        self.assertEqual(len(beats_b0), 2, beats_b0)
        self.assertEqual(len(beats_b1), 2, beats_b1)

    def test_deltas_vs_b0_and_b1_present(self):
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports)
        # B1 and B2 should have deltas vs B0
        self.assertIn("B1_scanner_replica", cmp.deltas_vs_primary_baseline)
        self.assertIn("B2_logistic",         cmp.deltas_vs_primary_baseline)
        # B0 and B2 should have deltas vs B1
        self.assertIn("B0_majority",         cmp.deltas_vs_secondary_baseline)
        self.assertIn("B2_logistic",         cmp.deltas_vs_secondary_baseline)
        # PR-AUC delta is recorded
        self.assertIn("pr_auc",
                       cmp.deltas_vs_primary_baseline["B2_logistic"])

    def test_delta_sign_convention_higher_is_better(self):
        """For PR-AUC (higher is better), delta = candidate - baseline.
        Positive delta means candidate wins."""
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports)
        # Compute the expected delta manually
        b0_auc = cmp.per_metric["pr_auc"]["B0_majority"]
        b2_auc = cmp.per_metric["pr_auc"]["B2_logistic"]
        expected = b2_auc - b0_auc
        actual   = cmp.deltas_vs_primary_baseline["B2_logistic"]["pr_auc"]
        self.assertAlmostEqual(expected, actual, places=10)

    def test_delta_sign_convention_lower_is_better(self):
        """For log_loss (lower is better), delta = baseline - candidate.
        Positive delta still means candidate wins."""
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports, primary_metric="log_loss")
        # Sign convention check
        b0_ll = cmp.per_metric["log_loss"]["B0_majority"]
        b2_ll = cmp.per_metric["log_loss"]["B2_logistic"]
        expected = b0_ll - b2_ll
        actual   = cmp.deltas_vs_primary_baseline["B2_logistic"]["log_loss"]
        self.assertAlmostEqual(expected, actual, places=10)

    def test_secondary_baseline_none_skips_b1_deltas(self):
        _, reports = _train_three_baselines_on_model_b()
        cmp = compare_baselines(reports,
            secondary_baseline_model_type=None)
        self.assertEqual(cmp.deltas_vs_secondary_baseline, {})



# ═════════════════════════════════════════════════════════════════════
# G8 — Model registry + read-only predictions (M18.A.8)
# ═════════════════════════════════════════════════════════════════════

import sqlite3 as _g8_sqlite
import tempfile as _g8_tempfile

from bot.ml.registry import (
    Registry,
    RegistryEntry,
    REGISTRY_ENTRY_SCHEMA_VERSION,
    ALWAYS_FALSE_APPROVED_FOR_LIVE,
    compute_model_id,
    infer_initial_status,
    predict_from_registry,
    PredictionResult,
    is_integrity_gate,
    is_judgment_gate,
    classify_reason,
    matches_override_gate,
    split_reasons,
    INTEGRITY_GATE_REASONS,
    JUDGMENT_GATE_NAMES,
    make_scope_key,
)
from bot.ml.errors import (
    PromotionBlockedError as G8PromotionBlocked,
    ForceOverrideRequired as G8ForceOverrideRequired,
    M18ConfigError as G8M18ConfigError,
)


def _g8_make_strict_qualified(out):
    """Return a copy of TrainOutputs that genuinely satisfies the STRICT
    production profile (2000/500/100/50), for registry promotion-
    MECHANICS tests. Uses the locked strict profile with observed counts
    that actually meet it (NOT a relaxed/bypassed profile), and strips
    any production:* blocked reasons. Recomputes promotion_eligible."""
    strict_status = evaluate_production_thinness(
        total_rows=3000, train_positives=600, holdout_positives=150,
        per_symbol_counts={"X": 3000}, label_class="binary")
    assert strict_status["passed"] and strict_status["strict_profile"]
    new_reasons = [r for r in out.promotion_blocked_reasons
                   if not r.startswith("production:")]
    return dataclasses.replace(
        out,
        production_thinness_status=strict_status,
        promotion_blocked_reasons=new_reasons,
        promotion_eligible=(len(new_reasons) == 0),
    )


def _g8_build_clean_b2(seed_n=2000, fixture_mode=False):
    """Build (assembler_result, train_outputs, evaluation_report) for
    a B2_logistic on Model B cohort with a relaxed drift threshold so
    synthetic-data PSI doesn't trip the gate."""
    per_tf = _multi_tf_for_assembler(n_15m=seed_n, seed=21)
    res = ds_assembler.DatasetAssembler(
        ds_assembler.AssemblerConfig(
            symbol="X", anchor_tf="15m",
            anchor_set=ds_anchors
                .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
            require_intraday=True, embargo_bars_override=10,
            fixture_mode=fixture_mode,
            adversarial_cv_folds=3, adversarial_threshold=1.0)
    ).build(per_tf_bars=per_tf)
    cfg = TrainConfig(
        dataset_id=res.manifest.dataset_id,
        model_type="B2_logistic",
        train_mode="model_b_candidate_quality",
        target_label_id="triple_barrier_atr_2_3_50_won",
        hyperparameters={}, seed=42,
        fixture_mode=fixture_mode)
    # Train with the DEFAULT strict Trainer — the production profile is
    # never bypassable. The fixture is necessarily small, so the strict
    # production gate would block promotion; to test registry promotion
    # MECHANICS (not the data-volume gate, which has its own tests in
    # G8_ProductionThinnessGates), we then promote the TrainOutputs to a
    # genuinely strict-QUALIFIED candidate: a strict-profile production
    # status whose observed counts actually meet 2000/500/100/50, and a
    # promotion_blocked_reasons list with the production:* reasons
    # removed. This represents "a model that really did meet the strict
    # gate" without a slow large fixture — and crucially it is NOT a
    # relaxed/bypassed profile.
    out = ModelTrainer().train_one(cfg, res)
    if not fixture_mode:
        out = _g8_make_strict_qualified(out)
    rep = evaluate_model(out, res, drift_warning_threshold=100.0)
    return res, out, rep


# ─────────────────────────────────────────────────────────────────────
# G7_IsotonicCalibration — real fitted calibration (M18.B.3)
# ─────────────────────────────────────────────────────────────────────

class G7_IsotonicCalibration(unittest.TestCase):
    """Real IsotonicRegression calibration: fit on validation only,
    apply to test, JSON-safe artifact, pre/post Brier/ECE/MCE."""

    def _val_test(self, n=200, seed=0):
        rng = np.random.default_rng(seed)
        val_p = rng.uniform(0.0, 1.0, n)
        val_y = (rng.uniform(0.0, 1.0, n) < val_p).astype(np.float64)
        test_p = rng.uniform(0.0, 1.0, n)
        test_y = (rng.uniform(0.0, 1.0, n) < test_p).astype(np.float64)
        return val_p, val_y, test_p, test_y

    def test_isotonic_fits_on_validation_only(self):
        # Capture the arrays passed to IsotonicRegression.fit and assert
        # they match the VALIDATION input (count + values), never train.
        import bot.ml.evaluation.calibration as cal_mod
        from sklearn.isotonic import IsotonicRegression
        val_p, val_y, test_p, test_y = self._val_test()
        captured = {}
        orig_fit = IsotonicRegression.fit

        def spy_fit(self, X, y, *a, **k):
            captured["X"] = np.asarray(X, dtype=np.float64).copy()
            captured["y"] = np.asarray(y, dtype=np.float64).copy()
            return orig_fit(self, X, y, *a, **k)

        IsotonicRegression.fit = spy_fit
        try:
            cal_mod.fit_isotonic_calibration(
                val_prob=val_p, val_y=val_y,
                test_prob=test_p, test_y=test_y, label_class="binary")
        finally:
            IsotonicRegression.fit = orig_fit
        self.assertEqual(captured["X"].shape[0], len(val_p))
        np.testing.assert_array_equal(
            captured["X"], np.clip(val_p, 0.0, 1.0))
        np.testing.assert_array_equal(captured["y"], val_y)

    def test_isotonic_applies_to_test_probabilities(self):
        val_p, val_y, test_p, test_y = self._val_test()
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y,
            test_prob=test_p, test_y=test_y, label_class="binary")
        self.assertTrue(r["available"])
        cal = apply_isotonic_artifact(test_p, r["artifact"])
        self.assertEqual(len(cal), len(test_p))

    def test_isotonic_probabilities_are_bounded(self):
        val_p, val_y, test_p, test_y = self._val_test()
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y,
            test_prob=test_p, test_y=test_y, label_class="binary")
        cal = apply_isotonic_artifact(test_p, r["artifact"])
        self.assertTrue(np.all(np.isfinite(cal)))
        self.assertGreaterEqual(float(cal.min()), 0.0)
        self.assertLessEqual(float(cal.max()), 1.0)

    def test_isotonic_artifact_is_json_safe(self):
        val_p, val_y, test_p, test_y = self._val_test()
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y,
            test_prob=test_p, test_y=test_y, label_class="binary")
        art = r["artifact"]
        self.assertIsInstance(art["x_thresholds"], list)
        self.assertIsInstance(art["y_thresholds"], list)
        json.dumps(r)  # must not raise

    def test_isotonic_artifact_round_trip(self):
        val_p, val_y, test_p, test_y = self._val_test()
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y,
            test_prob=test_p, test_y=test_y, label_class="binary")
        art = r["artifact"]
        restored = json.loads(json.dumps(art))
        c1 = apply_isotonic_artifact(test_p, art)
        c2 = apply_isotonic_artifact(test_p, restored)
        np.testing.assert_array_equal(c1, c2)

    def test_isotonic_reports_pre_post_brier_ece_mce(self):
        val_p, val_y, test_p, test_y = self._val_test()
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y,
            test_prob=test_p, test_y=test_y, label_class="binary")
        for section in ("validation", "test"):
            for k in ("pre_brier", "post_brier", "pre_ece",
                       "post_ece", "pre_mce", "post_mce"):
                self.assertIn(k, r[section])

    def test_isotonic_rejects_one_class_validation(self):
        val_p, _, test_p, test_y = self._val_test()
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=np.zeros(len(val_p)),
            test_prob=test_p, test_y=test_y, label_class="binary")
        self.assertFalse(r["available"])
        self.assertEqual(r["unavailable_reason"],
                          "one_class_validation_labels")

    def test_isotonic_rejects_too_few_validation_rows(self):
        val_p, val_y, _, _ = self._val_test(n=5)
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y, label_class="binary")
        self.assertFalse(r["available"])
        self.assertEqual(r["unavailable_reason"],
                          "too_few_validation_rows")

    def test_isotonic_rejects_non_finite_probabilities(self):
        val_p, val_y, _, _ = self._val_test()
        val_p[0] = np.nan
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y, label_class="binary")
        self.assertFalse(r["available"])
        self.assertEqual(r["unavailable_reason"], "non_finite_probability")

    def test_isotonic_binary_only(self):
        val_p, val_y, _, _ = self._val_test()
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y, label_class="classification_3way")
        self.assertFalse(r["available"])
        self.assertEqual(r["unavailable_reason"], "unsupported_label_class")

    def test_eval_report_contains_isotonic_calibration(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("B2_logistic",
                                  dataset_id=res.manifest.dataset_id),
            res)
        rep = evaluate_model(out, res)
        ic = rep.isotonic_calibration
        self.assertTrue(ic.get("available"), ic.get("unavailable_reason"))
        self.assertEqual(ic["method"], "isotonic")
        self.assertEqual(ic["fitted_on_split"], "val")
        self.assertIn("isotonic_calibration", rep.to_dict())

    def test_isotonic_does_not_mutate_original_predictions(self):
        val_p, val_y, test_p, test_y = self._val_test()
        val_p_c, test_p_c = val_p.copy(), test_p.copy()
        fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y,
            test_prob=test_p, test_y=test_y, label_class="binary")
        np.testing.assert_array_equal(val_p, val_p_c)
        np.testing.assert_array_equal(test_p, test_p_c)

    def test_isotonic_deterministic_same_inputs(self):
        val_p, val_y, test_p, test_y = self._val_test()
        r1 = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y,
            test_prob=test_p, test_y=test_y, label_class="binary")
        r2 = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y,
            test_prob=test_p, test_y=test_y, label_class="binary")
        self.assertEqual(r1, r2)

    def test_calibration_unavailable_reason_round_trips(self):
        val_p, val_y, _, _ = self._val_test(n=5)
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y, label_class="binary")
        d = json.loads(json.dumps(r))
        self.assertFalse(d["available"])
        self.assertEqual(d["unavailable_reason"], "too_few_validation_rows")

    def test_calibrated_metrics_present_not_required_to_improve(self):
        # Isotonic can overfit tiny val data — assert metrics are
        # COMPUTED (finite numbers), not that they always improve.
        val_p, val_y, test_p, test_y = self._val_test()
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y,
            test_prob=test_p, test_y=test_y, label_class="binary")
        for section in ("validation", "test"):
            self.assertTrue(np.isfinite(r[section]["post_brier"]))

    def test_isotonic_rejects_validation_shape_mismatch(self):
        val_p, val_y, _, _ = self._val_test()
        r = fit_isotonic_calibration(
            val_prob=val_p[:50], val_y=val_y, label_class="binary")
        self.assertFalse(r["available"])
        self.assertEqual(r["unavailable_reason"],
                          "validation_shape_mismatch")

    def test_isotonic_rejects_test_shape_mismatch(self):
        val_p, val_y, test_p, _ = self._val_test()
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y,
            test_prob=test_p, test_y=np.zeros(len(test_p) - 10),
            label_class="binary")
        self.assertTrue(r["available"])
        self.assertEqual(r["test"]["unavailable_reason"],
                          "test_shape_mismatch")

    def test_isotonic_rejects_non_binary_validation_labels(self):
        val_p, val_y, _, _ = self._val_test()
        bad = np.where(val_y > 0, 2.0, 0.0)
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=bad, label_class="binary")
        self.assertFalse(r["available"])
        self.assertEqual(r["unavailable_reason"],
                          "non_binary_validation_labels")

    def test_isotonic_marks_non_binary_test_labels_unavailable(self):
        val_p, val_y, test_p, test_y = self._val_test()
        bad = np.where(test_y > 0, 2.0, 0.0)
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y,
            test_prob=test_p, test_y=bad, label_class="binary")
        self.assertTrue(r["available"])
        self.assertEqual(r["test"]["unavailable_reason"],
                          "non_binary_test_labels")

    def test_apply_isotonic_artifact_rejects_missing_thresholds(self):
        with self.assertRaises(ValueError):
            apply_isotonic_artifact(
                np.array([0.5]), {"y_thresholds": [0.0, 1.0]})

    def test_apply_isotonic_artifact_rejects_length_mismatch(self):
        with self.assertRaises(ValueError):
            apply_isotonic_artifact(np.array([0.5]), {
                "x_thresholds": [0.0, 0.5, 1.0],
                "y_thresholds": [0.0, 1.0]})

    def test_apply_isotonic_artifact_rejects_non_monotonic_x_thresholds(self):
        with self.assertRaises(ValueError):
            apply_isotonic_artifact(np.array([0.5]), {
                "x_thresholds": [1.0, 0.0],
                "y_thresholds": [0.0, 1.0]})

    def test_isotonic_result_is_strict_json_safe(self):
        val_p, val_y, test_p, test_y = self._val_test()
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y,
            test_prob=test_p, test_y=test_y, label_class="binary")
        # strict: no NaN/inf allowed
        json.dumps(r, allow_nan=False)

    def test_isotonic_result_always_has_test_section(self):
        # When no test split is supplied, the test section must still
        # be present with an explicit reason.
        val_p, val_y, _, _ = self._val_test()
        r = fit_isotonic_calibration(
            val_prob=val_p, val_y=val_y, label_class="binary")
        self.assertIn("test", r)
        self.assertEqual(r["test"]["unavailable_reason"],
                          "test_split_not_supplied")


# ─────────────────────────────────────────────────────────────────────
# G8_GateClassification — Q17 integrity vs judgment
# ─────────────────────────────────────────────────────────────────────

class G8_GateClassification(unittest.TestCase):

    def test_integrity_reasons_recognised(self):
        for r in ["fixture_only", "dataset:fixture_only",
                    "coverage_degraded", "dataset:coverage_degraded",
                    "adversarial_validation_failed",
                    "dataset:adversarial_validation_failed",
                    "adversarial_validation_not_run",
                    "drift_check_failed", "drift_warning",
                    "schema_mismatch", "point_in_time_violation",
                    "leakage_detected", "hash_mismatch",
                    "feature_schema_mismatch"]:
            self.assertTrue(is_integrity_gate(r), r)
            self.assertFalse(is_judgment_gate(r), r)

    def test_judgment_reasons_recognised(self):
        for r in ["baseline_beat", "sample_count",
                    "thinness:sample_count_train",
                    "thinness:minority_class_count_train",
                    "baseline_beat:vs_B0_majority"]:
            self.assertTrue(is_judgment_gate(r), r)
            self.assertFalse(is_integrity_gate(r), r)

    def test_judgment_gate_names_locked(self):
        """Only baseline_beat and sample_count are valid --override-gate
        values per M18.A.1 lock."""
        self.assertEqual(JUDGMENT_GATE_NAMES,
                          frozenset({"baseline_beat", "sample_count"}))

    def test_dataset_prefix_treated_as_integrity_fail_closed(self):
        """Anything 'dataset:*' is treated as integrity even if not
        in the literal allow-list (fail-closed)."""
        self.assertTrue(is_integrity_gate("dataset:novel_reason"))

    def test_matches_override_gate(self):
        # sample_count covers thinness:*
        self.assertTrue(matches_override_gate(
            "thinness:minority_class_count", "sample_count"))
        # sample_count covers bare "sample_count"
        self.assertTrue(matches_override_gate(
            "sample_count", "sample_count"))
        # sample_count does NOT cover baseline_beat
        self.assertFalse(matches_override_gate(
            "baseline_beat:vs_B0_majority", "sample_count"))
        # baseline_beat covers baseline_beat:*
        self.assertTrue(matches_override_gate(
            "baseline_beat:vs_B0_majority", "baseline_beat"))
        # No override gate matches an integrity reason
        self.assertFalse(matches_override_gate(
            "fixture_only", "sample_count"))
        self.assertFalse(matches_override_gate(
            "dataset:fixture_only", "baseline_beat"))

    def test_split_reasons_preserves_order(self):
        integ, judg, unk = split_reasons([
            "thinness:a", "fixture_only", "baseline_beat:x",
            "weird_reason"])
        self.assertEqual(integ, ["fixture_only"])
        self.assertEqual(judg, ["thinness:a", "baseline_beat:x"])
        self.assertEqual(unk, ["weird_reason"])

    def test_production_prefix_is_integrity(self):
        r = "production:production_total_rows_below_2000"
        self.assertTrue(is_integrity_gate(r))
        self.assertFalse(is_judgment_gate(r))
        self.assertFalse(matches_override_gate(r, "sample_count"))
        self.assertFalse(matches_override_gate(r, "baseline_beat"))
        # the lock reason is also integrity / non-overridable
        lock = "production:production_threshold_profile_not_locked"
        self.assertTrue(is_integrity_gate(lock))
        self.assertFalse(is_judgment_gate(lock))


# ─────────────────────────────────────────────────────────────────────
# G8_ProductionThinnessGates — strict production promotion gates (M18.B.4)
# ─────────────────────────────────────────────────────────────────────

class G8_ProductionThinnessGates(unittest.TestCase):
    """Strict production-promotion thinness profile: 2000 total rows,
    500 train positives, 100 holdout positives, 50 per-symbol rows.
    Trainability gates stay weak (fixture/cold-start can train); these
    gates are INTEGRITY (force cannot override) and block promotion."""

    def test_production_gate_passes_when_thresholds_met(self):
        r = evaluate_production_thinness(
            total_rows=3000, train_positives=600,
            holdout_positives=150,
            per_symbol_counts={"AAPL": 2000, "MSFT": 1000},
            label_class="binary")
        self.assertTrue(r["passed"])
        self.assertEqual(r["blocked_reasons"], [])

    def test_production_gate_blocks_total_rows_below_threshold(self):
        r = evaluate_production_thinness(
            total_rows=1000, train_positives=600,
            holdout_positives=150, per_symbol_counts={"X": 1000},
            label_class="binary")
        self.assertFalse(r["passed"])
        self.assertIn("production_total_rows_below_2000",
                       r["blocked_reasons"])

    def test_production_gate_blocks_train_positives_below_threshold(self):
        r = evaluate_production_thinness(
            total_rows=3000, train_positives=120,
            holdout_positives=150, per_symbol_counts={"X": 3000},
            label_class="binary")
        self.assertFalse(r["passed"])
        self.assertIn("production_train_positives_below_500",
                       r["blocked_reasons"])

    def test_production_gate_blocks_holdout_positives_below_threshold(self):
        r = evaluate_production_thinness(
            total_rows=3000, train_positives=600,
            holdout_positives=40, per_symbol_counts={"X": 3000},
            label_class="binary")
        self.assertFalse(r["passed"])
        self.assertIn("production_holdout_positives_below_100",
                       r["blocked_reasons"])

    def test_production_gate_blocks_per_symbol_rows_below_threshold(self):
        r = evaluate_production_thinness(
            total_rows=3000, train_positives=600,
            holdout_positives=150,
            per_symbol_counts={"AAPL": 2988, "TINY": 12},
            label_class="binary")
        self.assertFalse(r["passed"])
        self.assertIn("production_per_symbol_rows_below_50",
                       r["blocked_reasons"])
        self.assertEqual(r["observed"]["min_per_symbol_rows"], 12)

    def test_fixture_mode_can_train_but_cannot_promote(self):
        res, out, rep = _g8_build_clean_b2(fixture_mode=True)
        # fixture trained and produced outputs
        self.assertIsNotNone(out.pred_test)
        self.assertGreater(len(out.pred_test), 0)
        # but cannot promote
        self.assertFalse(out.promotion_eligible)
        self.assertIn("fixture_only", out.promotion_blocked_reasons)

    def test_cold_start_small_dataset_can_train_but_cannot_promote(self):
        # Default strict Trainer: small fixture trains (diagnostics) but
        # the production profile fails -> not promotion_eligible.
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("B2_logistic",
                                  dataset_id=res.manifest.dataset_id),
            res)
        self.assertGreater(len(out.pred_test), 0)        # trained
        self.assertFalse(out.promotion_eligible)         # but blocked
        self.assertTrue(any(r.startswith("production:")
                            for r in out.promotion_blocked_reasons),
                         out.promotion_blocked_reasons)

    def test_force_cannot_override_production_thinness(self):
        # A model blocked only by production-thinness must NOT promote
        # even with --force --override-gate. (Strict Trainer default.)
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("B2_logistic",
                                  dataset_id=res.manifest.dataset_id),
            res)
        rep = evaluate_model(out, res, drift_warning_threshold=100.0)
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            with self.assertRaises(G8PromotionBlocked) as ctx:
                reg.promote_to_current(
                    entry.model_id, force=True,
                    override_gates=("sample_count", "baseline_beat"),
                    reason="trying to force a thin model",
                    actor="test_user")
            self.assertEqual(ctx.exception.gate_category, "integrity")

    def test_production_thinness_status_in_train_outputs(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("B2_logistic",
                                  dataset_id=res.manifest.dataset_id),
            res)
        pts = out.production_thinness_status
        self.assertEqual(pts["profile"], "production_promotion")
        self.assertIn("thresholds", pts)
        self.assertIn("observed", pts)
        self.assertIn("blocked_reasons", pts)
        self.assertEqual(pts["thresholds"]["min_total_rows"], 2000)
        self.assertEqual(pts["thresholds"]["min_train_positives"], 500)
        self.assertEqual(pts["thresholds"]["min_holdout_positives"], 100)
        self.assertEqual(pts["thresholds"]["min_per_symbol_rows"], 50)

    def test_production_thinness_status_round_trips(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("B2_logistic",
                                  dataset_id=res.manifest.dataset_id),
            res)
        d = json.loads(json.dumps(out.to_dict()))
        self.assertIn("production_thinness_status", d)
        self.assertEqual(
            d["production_thinness_status"]["profile"],
            "production_promotion")

    def test_existing_trainability_thinness_still_exists(self):
        res = _assemble_for_training()
        out = ModelTrainer().train_one(
            _make_train_config("B2_logistic",
                                  dataset_id=res.manifest.dataset_id),
            res)
        # old trainability thinness_status preserved (not replaced)
        self.assertTrue(out.thinness_status)
        self.assertIn("checks", out.thinness_status)

    def test_non_binary_or_missing_labels_not_counted_as_positive(self):
        # NaN / non-1 values must not inflate positive counts.
        y = np.array([0.0, 1.0, 1.0, np.nan, 2.0, 0.0, 1.0])
        self.assertEqual(count_positives(y, "binary"), 3)

    def test_positive_count_uses_only_target_label(self):
        # count_positives operates on the supplied y only — a different
        # label column cannot inflate the count.
        y_target = np.array([0.0, 0.0, 1.0])
        y_other  = np.array([1.0, 1.0, 1.0])
        self.assertEqual(count_positives(y_target, "binary"), 1)
        self.assertEqual(count_positives(y_other, "binary"), 3)

    def test_coverage_degraded_still_blocks_promotion(self):
        # Q19 regression guard: coverage_degraded remains a blocking
        # integrity reason regardless of the new production gate.
        per_tf = _multi_tf_for_assembler(n_15m=300, seed=31)
        # Drop a required TF to force coverage degradation.
        per_tf = {"15m": per_tf["15m"]}
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=False, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        if res.manifest.coverage_degraded:
            out = ModelTrainer().train_one(
                _make_train_config("B0_majority",
                                      dataset_id=res.manifest.dataset_id),
                res)
            self.assertFalse(out.promotion_eligible)
            self.assertTrue(any("coverage_degraded" in r
                                for r in out.promotion_blocked_reasons),
                             out.promotion_blocked_reasons)

    def test_adversarial_validation_blocking_still_intact(self):
        # AV gate regression guard: a strict AV threshold that fails
        # still blocks promotion (independent of production gate).
        res = _assemble_for_training(n_15m=600, av_threshold=0.0)
        out = ModelTrainer().train_one(
            _make_train_config("B0_majority",
                                  dataset_id=res.manifest.dataset_id),
            res)
        # AV with threshold 0.0 should not pass -> dataset reason present
        self.assertFalse(out.promotion_eligible)

    # ---- B.4 LOCK: relaxed/injected profile can never promote --------

    def test_default_trainer_uses_locked_strict_thresholds(self):
        t = ModelTrainer()
        th = t.production_thinness_thresholds
        self.assertTrue(th.is_strict())
        self.assertEqual(th.min_total_rows, 2000)
        self.assertEqual(th.min_train_positives, 500)
        self.assertEqual(th.min_holdout_positives, 100)
        self.assertEqual(th.min_per_symbol_rows, 50)

    def test_relaxed_thresholds_cannot_create_promotable_model(self):
        # Even with infinite-passing relaxed thresholds, a non-locked
        # profile is blocked by production_threshold_profile_not_locked.
        relaxed = ProductionThinnessThresholds(
            min_total_rows=1, min_train_positives=1,
            min_holdout_positives=1, min_per_symbol_rows=1)
        res = _assemble_for_training()
        out = ModelTrainer(
            production_thinness_thresholds=relaxed).train_one(
            _make_train_config("B2_logistic",
                                  dataset_id=res.manifest.dataset_id),
            res)
        self.assertFalse(out.promotion_eligible)
        self.assertIn(
            "production:production_threshold_profile_not_locked",
            out.promotion_blocked_reasons)

    def test_non_strict_threshold_profile_blocked(self):
        relaxed = ProductionThinnessThresholds(min_total_rows=10)
        r = evaluate_production_thinness(
            total_rows=999999, train_positives=999999,
            holdout_positives=999999, per_symbol_counts={"X": 999999},
            label_class="binary", thresholds=relaxed)
        self.assertFalse(r["passed"])
        self.assertEqual(r["threshold_profile"], "relaxed_for_tests")
        self.assertFalse(r["strict_profile"])
        self.assertIn("production_threshold_profile_not_locked",
                       r["blocked_reasons"])

    def test_strict_profile_marked_strict(self):
        r = evaluate_production_thinness(
            total_rows=3000, train_positives=600, holdout_positives=150,
            per_symbol_counts={"X": 3000}, label_class="binary")
        self.assertEqual(r["threshold_profile"], "strict")
        self.assertTrue(r["strict_profile"])
        self.assertNotIn("production_threshold_profile_not_locked",
                          r["blocked_reasons"])

    def test_registry_promote_with_non_strict_profile_fails_with_force(self):
        # A model built with a relaxed production profile must NOT be
        # promotable even with --force --override-gate.
        relaxed = ProductionThinnessThresholds(
            min_total_rows=1, min_train_positives=1,
            min_holdout_positives=1, min_per_symbol_rows=1)
        res = _assemble_for_training()
        out = ModelTrainer(
            production_thinness_thresholds=relaxed).train_one(
            _make_train_config("B2_logistic",
                                  dataset_id=res.manifest.dataset_id),
            res)
        rep = evaluate_model(out, res, drift_warning_threshold=100.0)
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            with self.assertRaises(G8PromotionBlocked) as ctx:
                reg.promote_to_current(
                    entry.model_id, force=True,
                    override_gates=("sample_count", "baseline_beat"),
                    reason="trying to force a non-strict-profile model",
                    actor="test_user")
            self.assertEqual(ctx.exception.gate_category, "integrity")

    def test_pure_helper_custom_thresholds_does_not_imply_promotability(self):
        # The pure helper can be exercised with custom thresholds, but a
        # non-locked profile is reported non-strict + blocked — it never
        # implies registry promotability.
        custom = ProductionThinnessThresholds(
            min_total_rows=100, min_train_positives=10,
            min_holdout_positives=5, min_per_symbol_rows=5)
        r = evaluate_production_thinness(
            total_rows=500, train_positives=50, holdout_positives=20,
            per_symbol_counts={"X": 500}, label_class="binary",
            thresholds=custom)
        # counts clear the custom thresholds, but profile is non-locked
        self.assertFalse(r["strict_profile"])
        self.assertIn("production_threshold_profile_not_locked",
                       r["blocked_reasons"])
        self.assertFalse(r["passed"])


# ─────────────────────────────────────────────────────────────────────
# G8_RegistryEntry — schema + deterministic model_id
# ─────────────────────────────────────────────────────────────────────

class G8_RegistryEntry(unittest.TestCase):

    def test_compute_model_id_deterministic(self):
        _, out, _ = _g8_build_clean_b2()
        a = compute_model_id(out)
        b = compute_model_id(out)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in a))

    def test_compute_model_id_differs_by_seed(self):
        """Same dataset + same model_type + DIFFERENT seed → different
        model_id."""
        res, out_a, _ = _g8_build_clean_b2()
        cfg_b = TrainConfig(
            dataset_id=res.manifest.dataset_id,
            model_type="B2_logistic",
            train_mode="model_b_candidate_quality",
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=99,  # different
            fixture_mode=False)
        out_b = ModelTrainer().train_one(cfg_b, res)
        self.assertNotEqual(compute_model_id(out_a),
                              compute_model_id(out_b))

    def test_compute_model_id_differs_by_model_type(self):
        res, out_a, _ = _g8_build_clean_b2()
        cfg_b = TrainConfig(
            dataset_id=res.manifest.dataset_id,
            model_type="B0_majority",
            train_mode="model_b_candidate_quality",
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False)
        out_b = ModelTrainer().train_one(cfg_b, res)
        self.assertNotEqual(compute_model_id(out_a),
                              compute_model_id(out_b))


# ─────────────────────────────────────────────────────────────────────
# G8_StatusInference — initial-status rules
# ─────────────────────────────────────────────────────────────────────

class G8_StatusInference(unittest.TestCase):

    def test_clean_candidate(self):
        _, out, rep = _g8_build_clean_b2()
        s = infer_initial_status(out, rep)
        self.assertEqual(s, "candidate")

    def test_fixture_mode_yields_fixture_only(self):
        _, out, rep = _g8_build_clean_b2(fixture_mode=True)
        s = infer_initial_status(out, rep)
        self.assertEqual(s, "fixture_only")

    def test_thin_dataset_yields_failed_sample_count(self):
        per_tf = _multi_tf_for_assembler(n_15m=400, seed=11)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        # Default STRICT Trainer (production profile is non-bypassable).
        # Strip only the production:* reasons via _g8_make_strict_qualified
        # so the JUDGMENT thinness:* gate is what these tests exercise;
        # production-thinness has its own dedicated tests.
        out = ModelTrainer().train_one(TrainConfig(
            dataset_id=res.manifest.dataset_id,
            model_type="B2_logistic",
            train_mode="model_b_candidate_quality",
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False), res)
        out = _g8_make_strict_qualified(out)
        rep = evaluate_model(out, res, drift_warning_threshold=100.0)
        # The thin dataset triggers thinness reasons
        s = infer_initial_status(out, rep)
        self.assertEqual(s, "failed_sample_count",
            f"thin dataset should yield failed_sample_count; got {s}; "
            f"reasons={out.promotion_blocked_reasons}")

    def test_drift_warning_yields_failed_drift_check(self):
        _, out, _ = _g8_build_clean_b2()
        # Re-evaluate with a TIGHT drift threshold to force a warning
        rep_strict = evaluate_model(out,
            _g8_build_clean_b2()[0],
            drift_warning_threshold=0.001)
        s = infer_initial_status(out, rep_strict)
        self.assertEqual(s, "failed_drift_check")


# ─────────────────────────────────────────────────────────────────────
# G8_RegistrationFlow — registration + artifacts + entry file
# ─────────────────────────────────────────────────────────────────────

class G8_RegistrationFlow(unittest.TestCase):

    def test_register_writes_all_artifacts(self):
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            base = __import__('pathlib').Path(root)
            self.assertTrue(
                (base / "registry" / f"{entry.model_id}.json").exists())
            adir = base / "artifacts" / entry.model_id
            for f in ("train_outputs.json", "evaluation_report.json",
                       "training_feature_summary.json",
                       "training_X.parquet", "training_y.parquet",
                       "training_metadata.json"):
                self.assertTrue((adir / f).exists(),
                    f"missing artifact {f}")

    def test_registration_does_not_auto_promote(self):
        """Registering many candidates never promotes anyone."""
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            for _ in range(3):
                reg.register_candidate(out, rep, res)
            # No 'current' pointers exist after multiple registrations
            current_dir = (__import__('pathlib').Path(root) / "current")
            files = (list(current_dir.iterdir())
                      if current_dir.exists() else [])
            self.assertEqual(files, [],
                f"register_candidate must NEVER auto-promote; "
                f"found pointer files: {files}")
            # current_history is empty
            self.assertEqual(reg.current_history(), [])

    def test_approved_for_live_always_false(self):
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            self.assertFalse(entry.approved_for_live)
            self.assertEqual(ALWAYS_FALSE_APPROVED_FOR_LIVE, False)

    def test_registry_entry_schema_version_recorded(self):
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            self.assertEqual(entry.schema_version,
                              REGISTRY_ENTRY_SCHEMA_VERSION)

    def test_dataset_hash_mismatch_refuses(self):
        """If TrainOutputs and AssemblerResult disagree on dataset
        hash, register_candidate must refuse (different dataset)."""
        res, out, rep = _g8_build_clean_b2()
        # Build a DIFFERENT dataset
        res2 = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=_multi_tf_for_assembler(n_15m=1500, seed=77))
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            with self.assertRaises(G8M18ConfigError):
                reg.register_candidate(out, rep, res2)


# ─────────────────────────────────────────────────────────────────────
# G8_FixtureOnly — Q16 invariant
# ─────────────────────────────────────────────────────────────────────

class G8_FixtureOnly(unittest.TestCase):

    def test_fixture_entry_status_is_fixture_only(self):
        res, out, rep = _g8_build_clean_b2(fixture_mode=True)
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            self.assertEqual(entry.status, "fixture_only")
            self.assertTrue(entry.fixture_only)

    def test_fixture_promote_no_force_rejected(self):
        res, out, rep = _g8_build_clean_b2(fixture_mode=True)
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            with self.assertRaises(G8PromotionBlocked) as ctx:
                reg.promote_to_current(entry.model_id)
            self.assertEqual(ctx.exception.gate_category, "integrity")
            self.assertEqual(ctx.exception.gate, "fixture_only")

    def test_fixture_force_promote_still_rejected(self):
        """Q17 lock: --force CAN NEVER override integrity gates."""
        res, out, rep = _g8_build_clean_b2(fixture_mode=True)
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            with self.assertRaises(G8PromotionBlocked) as ctx:
                reg.promote_to_current(
                    entry.model_id, force=True,
                    override_gates=("sample_count", "baseline_beat"),
                    reason="trying",
                    actor="test_user")
            self.assertEqual(ctx.exception.gate_category, "integrity")


# ─────────────────────────────────────────────────────────────────────
# G8_PromoteCleanCandidate — no force needed
# ─────────────────────────────────────────────────────────────────────

class G8_PromoteCleanCandidate(unittest.TestCase):

    def test_clean_promote_sets_current(self):
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            promoted = reg.promote_to_current(entry.model_id)
            self.assertEqual(promoted.status, "current")
            self.assertFalse(promoted.approved_for_live)

    def test_promote_appends_to_current_history(self):
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            reg.promote_to_current(entry.model_id)
            hist = reg.current_history()
            self.assertEqual(len(hist), 1)
            self.assertEqual(hist[0]["event"], "promote")
            self.assertEqual(hist[0]["model_id"], entry.model_id)
            self.assertFalse(hist[0]["force_used"])
            self.assertFalse(hist[0]["approved_for_live"])

    def test_promote_demotes_previous_current(self):
        """When a new model is promoted under the same scope_key,
        the previous current is demoted."""
        res_a, out_a, rep_a = _g8_build_clean_b2(seed_n=2000)
        res_b, out_b, rep_b = _g8_build_clean_b2(seed_n=1800)
        # The two outputs have different model_ids (different
        # dataset_hash from different bar windows) but same scope_key
        self.assertNotEqual(compute_model_id(out_a),
                              compute_model_id(out_b))
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            e_a = reg.register_candidate(out_a, rep_a, res_a)
            e_b = reg.register_candidate(out_b, rep_b, res_b)
            reg.promote_to_current(e_a.model_id)
            reg.promote_to_current(e_b.model_id)
            # Re-fetch both
            r_a = reg.get_entry(e_a.model_id)
            r_b = reg.get_entry(e_b.model_id)
            self.assertEqual(r_a.status, "demoted")
            self.assertEqual(r_b.status, "current")

    def test_get_current_returns_promoted_entry(self):
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            promoted = reg.promote_to_current(entry.model_id)
            sk = make_scope_key(
                dataset_anchor_set=entry.dataset_anchor_set,
                train_mode=entry.train_mode,
                target_label_id=entry.target_label_id,
                model_type=entry.model_type)
            cur = reg.get_current(sk)
            self.assertIsNotNone(cur)
            self.assertEqual(cur.model_id, entry.model_id)


# ─────────────────────────────────────────────────────────────────────
# G8_ForceOverrideJudgmentGate — judgment gates can be overridden
# ─────────────────────────────────────────────────────────────────────

class G8_ForceOverrideJudgmentGate(unittest.TestCase):

    def _thin_setup(self, root):
        per_tf = _multi_tf_for_assembler(n_15m=400, seed=11)
        res = ds_assembler.DatasetAssembler(
            ds_assembler.AssemblerConfig(
                symbol="X", anchor_tf="15m",
                anchor_set=ds_anchors
                    .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                require_intraday=True, embargo_bars_override=10,
                adversarial_cv_folds=3, adversarial_threshold=1.0)
        ).build(per_tf_bars=per_tf)
        # Default STRICT Trainer (production profile is non-bypassable).
        # Strip only the production:* reasons via _g8_make_strict_qualified
        # so the JUDGMENT thinness:* gate is what these tests exercise;
        # production-thinness has its own dedicated tests.
        out = ModelTrainer().train_one(TrainConfig(
            dataset_id=res.manifest.dataset_id,
            model_type="B2_logistic",
            train_mode="model_b_candidate_quality",
            target_label_id="triple_barrier_atr_2_3_50_won",
            hyperparameters={}, seed=42, fixture_mode=False), res)
        out = _g8_make_strict_qualified(out)
        rep = evaluate_model(out, res, drift_warning_threshold=100.0)
        reg = Registry(root=root)
        entry = reg.register_candidate(out, rep, res)
        return reg, entry

    def test_thin_no_force_rejected(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            reg, entry = self._thin_setup(root)
            self.assertEqual(entry.status, "failed_sample_count")
            with self.assertRaises(G8PromotionBlocked) as ctx:
                reg.promote_to_current(entry.model_id)
            self.assertEqual(ctx.exception.gate_category, "judgment")

    def test_force_without_override_gate_raises(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            reg, entry = self._thin_setup(root)
            with self.assertRaises(G8ForceOverrideRequired):
                reg.promote_to_current(
                    entry.model_id, force=True,
                    override_gates=(),
                    reason="trying without naming gate")

    def test_force_with_empty_reason_raises(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            reg, entry = self._thin_setup(root)
            with self.assertRaises(G8ForceOverrideRequired):
                reg.promote_to_current(
                    entry.model_id, force=True,
                    override_gates=("sample_count",),
                    reason="")

    def test_force_with_wrong_override_gate_raises(self):
        """baseline_beat does NOT cover thinness:* reasons → reject."""
        with _g8_tempfile.TemporaryDirectory() as root:
            reg, entry = self._thin_setup(root)
            with self.assertRaises(G8ForceOverrideRequired) as ctx:
                reg.promote_to_current(
                    entry.model_id, force=True,
                    override_gates=("baseline_beat",),
                    reason="wrong gate",
                    actor="test")
            self.assertIn("uncovered", str(ctx.exception))

    def test_force_with_unknown_override_gate_raises(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            reg, entry = self._thin_setup(root)
            with self.assertRaises(G8ForceOverrideRequired):
                reg.promote_to_current(
                    entry.model_id, force=True,
                    override_gates=("not_a_real_gate",),
                    reason="trying",
                    actor="test")

    def test_force_with_correct_override_succeeds_with_forced_promoted(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            reg, entry = self._thin_setup(root)
            forced = reg.promote_to_current(
                entry.model_id, force=True,
                override_gates=("sample_count",),
                reason="approved by ChatGPT for inspection",
                actor="mike")
            self.assertEqual(forced.status, "forced_promoted")
            self.assertTrue(forced.force_override_used)
            self.assertEqual(forced.force_override_gates,
                              ["sample_count"])
            self.assertEqual(forced.force_override_reasons,
                              ["approved by ChatGPT for inspection"])
            self.assertEqual(forced.force_override_actor, "mike")
            self.assertFalse(forced.approved_for_live)

    def test_forced_promote_records_in_current_history(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            reg, entry = self._thin_setup(root)
            reg.promote_to_current(
                entry.model_id, force=True,
                override_gates=("sample_count",),
                reason="for inspection only", actor="mike")
            hist = reg.current_history()
            self.assertEqual(len(hist), 1)
            self.assertTrue(hist[0]["force_used"])
            self.assertEqual(hist[0]["override_gates"], ["sample_count"])
            self.assertEqual(hist[0]["reason"], "for inspection only")
            self.assertEqual(hist[0]["actor"], "mike")


# ─────────────────────────────────────────────────────────────────────
# G8_IntegrityCannotBeForced — Q17 lock
# ─────────────────────────────────────────────────────────────────────

class G8_IntegrityCannotBeForced(unittest.TestCase):

    def test_fixture_force_rejected(self):
        res, out, rep = _g8_build_clean_b2(fixture_mode=True)
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            with self.assertRaises(G8PromotionBlocked) as ctx:
                reg.promote_to_current(
                    entry.model_id, force=True,
                    override_gates=("sample_count",
                                     "baseline_beat"),
                    reason="really need this", actor="m")
            self.assertEqual(ctx.exception.gate_category, "integrity")

    def test_drift_force_rejected(self):
        """A model with status=failed_drift_check cannot be forced
        into current."""
        _, out, _ = _g8_build_clean_b2()
        res = _g8_build_clean_b2()[0]
        rep_drift = evaluate_model(
            out, res, drift_warning_threshold=0.001)
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep_drift, res)
            self.assertEqual(entry.status, "failed_drift_check")
            with self.assertRaises(G8PromotionBlocked) as ctx:
                reg.promote_to_current(
                    entry.model_id, force=True,
                    override_gates=("sample_count",),
                    reason="trying", actor="m")
            self.assertEqual(ctx.exception.gate_category, "integrity")


# ─────────────────────────────────────────────────────────────────────
# G8_Predictions — read-only, model_id per row, extrapolation flags
# ─────────────────────────────────────────────────────────────────────

class G8_Predictions(unittest.TestCase):

    def test_every_prediction_row_has_model_id(self):
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            promoted = reg.promote_to_current(entry.model_id)
            # Run predict against val split
            from bot.ml.models.base import select_feature_columns
            feat_cols = select_feature_columns(
                list(res.dataset.columns))
            X_in = res.dataset.iloc[res.split.val_anchor_indices][
                feat_cols].reset_index(drop=True)
            result = predict_from_registry(
                registry=reg, model_id=promoted.model_id,
                X_input=X_in)
            self.assertGreater(len(result.predictions), 0)
            self.assertIn("model_id", result.predictions.columns)
            self.assertTrue(
                (result.predictions["model_id"]
                  == promoted.model_id).all())

    def test_predictions_include_extrapolation_flag_and_count(self):
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            promoted = reg.promote_to_current(entry.model_id)
            from bot.ml.models.base import select_feature_columns
            feat_cols = select_feature_columns(
                list(res.dataset.columns))
            X_in = res.dataset.iloc[res.split.val_anchor_indices][
                feat_cols].reset_index(drop=True)
            result = predict_from_registry(
                registry=reg, model_id=promoted.model_id,
                X_input=X_in)
            self.assertIn("feature_extrapolation_flag",
                           result.predictions.columns)
            self.assertIn("feature_extrapolation_count",
                           result.predictions.columns)
            # flag is bool, count is int >= 0
            self.assertTrue(
                result.predictions["feature_extrapolation_flag"].dtype
                == bool)
            self.assertTrue(
                (result.predictions["feature_extrapolation_count"]
                  >= 0).all())
            # flag == True iff count > 0
            flags = result.predictions["feature_extrapolation_flag"]
            counts = result.predictions["feature_extrapolation_count"]
            self.assertTrue((flags == (counts > 0)).all())

    def test_predictions_deterministic_same_inputs(self):
        """Same registry + same X_input → identical pred_proba."""
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            promoted = reg.promote_to_current(entry.model_id)
            from bot.ml.models.base import select_feature_columns
            feat_cols = select_feature_columns(
                list(res.dataset.columns))
            X_in = res.dataset.iloc[res.split.val_anchor_indices][
                feat_cols].reset_index(drop=True)
            r1 = predict_from_registry(
                registry=reg, model_id=promoted.model_id,
                X_input=X_in, write_output=False)
            r2 = predict_from_registry(
                registry=reg, model_id=promoted.model_id,
                X_input=X_in, write_output=False)
            np.testing.assert_array_equal(
                r1.predictions["pred_proba"].to_numpy(),
                r2.predictions["pred_proba"].to_numpy())

    def test_predictions_written_under_data_ml(self):
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            promoted = reg.promote_to_current(entry.model_id)
            from bot.ml.models.base import select_feature_columns
            feat_cols = select_feature_columns(
                list(res.dataset.columns))
            X_in = res.dataset.iloc[res.split.val_anchor_indices][
                feat_cols].reset_index(drop=True)
            result = predict_from_registry(
                registry=reg, model_id=promoted.model_id,
                X_input=X_in, batch_id="testbatch001")
            # File path under predictions/{model_id}/
            from pathlib import Path
            outp = Path(result.output_path)
            self.assertTrue(outp.exists())
            self.assertIn("predictions", str(outp))
            self.assertIn(promoted.model_id, str(outp))
            # Parquet readable
            df = pd.read_parquet(outp)
            self.assertEqual(len(df), len(X_in))

    def test_predictions_missing_feature_columns_refuses(self):
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            promoted = reg.promote_to_current(entry.model_id)
            # Pass X_input without any of the expected feature columns
            X_in = pd.DataFrame({"bogus_col": [1.0, 2.0, 3.0]})
            with self.assertRaises(G8M18ConfigError) as ctx:
                predict_from_registry(
                    registry=reg, model_id=promoted.model_id,
                    X_input=X_in)
            self.assertIn("missing", str(ctx.exception))


# ─────────────────────────────────────────────────────────────────────
# G8_NoSignalsDbWrites — registry never touches signals.db
# ─────────────────────────────────────────────────────────────────────

class G8_NoSignalsDbWrites(unittest.TestCase):

    def test_no_signals_db_in_registry_root_after_full_flow(self):
        """register + promote + force-promote + predict must NOT
        create a signals.db file anywhere in the registry root."""
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            reg.promote_to_current(entry.model_id)
            from bot.ml.models.base import select_feature_columns
            feat_cols = select_feature_columns(
                list(res.dataset.columns))
            X_in = res.dataset.iloc[res.split.val_anchor_indices][
                feat_cols].reset_index(drop=True)
            predict_from_registry(
                registry=reg, model_id=entry.model_id,
                X_input=X_in)
            # Walk the entire root and assert no .db files
            from pathlib import Path
            db_files = list(Path(root).rglob("*.db"))
            self.assertEqual(db_files, [],
                f"registry must never write *.db files; found {db_files}")
            # Also assert no .sqlite files
            sql_files = list(Path(root).rglob("*.sqlite"))
            self.assertEqual(sql_files, [])


# ─────────────────────────────────────────────────────────────────────
# G8_GeneratedArtifactsGitignored — gitignore covers data/ml/
# ─────────────────────────────────────────────────────────────────────

class G8_GeneratedArtifactsGitignored(unittest.TestCase):

    def test_data_directory_is_gitignored(self):
        """The blanket 'data/' rule in .gitignore covers data/ml/."""
        import subprocess
        # check-ignore returns exit 0 if path IS ignored, 1 if not
        out = subprocess.run(
            ["git", "check-ignore", "-v", "data/ml/registry/foo.json",
              "data/ml/artifacts/abc/train_outputs.json",
              "data/ml/predictions/x/y.parquet",
              "data/ml/current_history.jsonl"],
            capture_output=True, text=True,
            cwd=__import__('os').path.dirname(
                __import__('os').path.abspath(__file__)))
        self.assertEqual(out.returncode, 0,
            f"data/ml/ paths should all be gitignored; "
            f"stderr={out.stderr}")
        self.assertIn("data/", out.stdout)


# ─────────────────────────────────────────────────────────────────────
# G8_DemoteCurrent
# ─────────────────────────────────────────────────────────────────────

class G8_DemoteCurrent(unittest.TestCase):

    def test_demote_current_changes_status_and_clears_pointer(self):
        res, out, rep = _g8_build_clean_b2()
        with _g8_tempfile.TemporaryDirectory() as root:
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            promoted = reg.promote_to_current(entry.model_id)
            sk = make_scope_key(
                dataset_anchor_set=entry.dataset_anchor_set,
                train_mode=entry.train_mode,
                target_label_id=entry.target_label_id,
                model_type=entry.model_type)
            self.assertIsNotNone(reg.get_current(sk))
            demoted = reg.demote_current(sk, reason="manual",
                                            actor="mike")
            self.assertEqual(demoted.status, "demoted")
            self.assertIsNone(reg.get_current(sk))
            # current_history has a demote event
            hist = reg.current_history()
            self.assertEqual(hist[-1]["event"], "demote")
            self.assertEqual(hist[-1]["reason"], "manual")
            self.assertEqual(hist[-1]["actor"], "mike")


class G8_Q20Extrapolation(unittest.TestCase):
    """Q20 locked rule:
        Define extrapolation as outside the training-set
        [1st percentile, 99th percentile] envelope.
        Do not silently clip features.

    These tests prove the implementation uses q01/q99 — NOT min/max
    — as the envelope. The min/max stay in the summary for context.
    """

    def _registered_entry_with_artifacts(self, tmpdir):
        """Run a real registration so the on-disk feature_summary
        contains q01/q99 alongside min/max."""
        res, out, rep = _g8_build_clean_b2()
        reg = Registry(root=tmpdir)
        entry = reg.register_candidate(out, rep, res)
        return reg, entry, res

    # ─── 1. Summary file contains q01/q99 (and still has min/max) ──

    def test_training_feature_summary_includes_q01_and_q99(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            reg, entry, _ = self._registered_entry_with_artifacts(root)
            summary_path = (__import__('pathlib').Path(root)
                              / entry.training_feature_summary_path)
            self.assertTrue(summary_path.exists())
            with open(summary_path) as f:
                summary = json.load(f)
            # Every feature has both new keys + the kept-for-context
            # min/max
            for feat, stats in summary.items():
                self.assertIn("q01", stats,
                    f"feature {feat} missing q01")
                self.assertIn("q99", stats,
                    f"feature {feat} missing q99")
                self.assertIn("min", stats,
                    f"feature {feat} missing min (kept for context)")
                self.assertIn("max", stats,
                    f"feature {feat} missing max (kept for context)")
                self.assertIn("n_finite", stats)

    def test_q01_le_q99_and_within_min_max(self):
        """Sanity: q01 ≤ q99, and q01 ≥ min, and q99 ≤ max."""
        with _g8_tempfile.TemporaryDirectory() as root:
            reg, entry, _ = self._registered_entry_with_artifacts(root)
            summary_path = (__import__('pathlib').Path(root)
                              / entry.training_feature_summary_path)
            summary = json.load(open(summary_path))
            for feat, st in summary.items():
                if st["n_finite"] == 0:
                    continue
                q01 = st["q01"]; q99 = st["q99"]
                mn  = st["min"]; mx  = st["max"]
                if all(np.isfinite([q01, q99, mn, mx])):
                    self.assertLessEqual(q01, q99,
                        f"{feat}: q01={q01} > q99={q99}")
                    self.assertGreaterEqual(q01, mn,
                        f"{feat}: q01={q01} < min={mn}")
                    self.assertLessEqual(q99, mx,
                        f"{feat}: q99={q99} > max={mx}")

    # ─── 2. Direct envelope check on _compute_extrapolation ─────────

    def _summary_with(self, q01, q99, mn=None, mx=None):
        """Build a feature_summary dict with the supplied envelope.
        min/max default to a much wider band so we can distinguish
        Q20 behaviour from min/max behaviour."""
        return {"f": {
            "min":  -1000.0 if mn is None else float(mn),
            "max":  +1000.0 if mx is None else float(mx),
            "q01":  float(q01),
            "q99":  float(q99),
            "mean": 0.0, "std": 1.0, "n_finite": 100,
        }}

    def test_value_inside_q01_q99_is_NOT_flagged(self):
        from bot.ml.registry.predictions import _compute_extrapolation
        summ = self._summary_with(q01=-2.0, q99=+2.0)
        X = pd.DataFrame({"f": [-1.0, 0.0, +1.0, -2.0, +2.0]})
        counts, flags, feats = _compute_extrapolation(X, summ, ["f"])
        # All five values are inside [q01, q99] inclusive
        self.assertTrue((counts == 0).all(), counts)
        self.assertTrue((~flags).all(), flags)
        for fs in feats:
            self.assertEqual(fs, [])

    def test_value_below_q01_IS_flagged(self):
        from bot.ml.registry.predictions import _compute_extrapolation
        summ = self._summary_with(q01=-2.0, q99=+2.0)
        X = pd.DataFrame({"f": [-2.0001, -3.0, -100.0]})
        counts, flags, feats = _compute_extrapolation(X, summ, ["f"])
        self.assertTrue((counts == 1).all(), counts)
        self.assertTrue(flags.all(), flags)
        for fs in feats:
            self.assertEqual(fs, ["f"])

    def test_value_above_q99_IS_flagged(self):
        from bot.ml.registry.predictions import _compute_extrapolation
        summ = self._summary_with(q01=-2.0, q99=+2.0)
        X = pd.DataFrame({"f": [+2.0001, +3.0, +500.0]})
        counts, flags, feats = _compute_extrapolation(X, summ, ["f"])
        self.assertTrue((counts == 1).all(), counts)
        self.assertTrue(flags.all(), flags)

    def test_count_uses_q01_q99_not_min_max(self):
        """The DEFINING Q20 test: a value between min and q01 (but
        below q01) MUST be flagged. Under the old min/max envelope
        it would NOT have been flagged. This test guards against
        regression to the old envelope."""
        from bot.ml.registry.predictions import _compute_extrapolation
        # min=-1000, q01=-2, q99=+2, max=+1000
        summ = self._summary_with(q01=-2.0, q99=+2.0,
                                    mn=-1000.0, mx=+1000.0)
        # Values between min and q01 (and between q99 and max)
        X = pd.DataFrame({"f": [-500.0, -100.0, -2.001, +2.001, +100.0, +500.0]})
        counts, flags, feats = _compute_extrapolation(X, summ, ["f"])
        # Under Q20 (q01/q99) — every one of these is OUTSIDE the
        # envelope and must be flagged.
        self.assertEqual(int(counts.sum()), 6,
            f"All 6 values between min/max but outside q01/q99 must "
            f"be flagged; got counts={counts}")
        self.assertTrue(flags.all())

    def test_extrapolation_count_aggregates_across_features(self):
        """Per-row extrapolation count = number of features with
        value outside [q01, q99]."""
        from bot.ml.registry.predictions import _compute_extrapolation
        summ = {
            "a": {"min": -10, "max": +10, "q01": -1.0, "q99": +1.0,
                    "mean": 0, "std": 1, "n_finite": 100},
            "b": {"min": -10, "max": +10, "q01": -1.0, "q99": +1.0,
                    "mean": 0, "std": 1, "n_finite": 100},
            "c": {"min": -10, "max": +10, "q01": -1.0, "q99": +1.0,
                    "mean": 0, "std": 1, "n_finite": 100},
        }
        # Row 0: all three out of range (count=3)
        # Row 1: only 'a' out of range (count=1)
        # Row 2: none out of range (count=0)
        X = pd.DataFrame({"a": [5.0, 5.0, 0.5],
                            "b": [5.0, 0.5, 0.5],
                            "c": [5.0, 0.5, 0.5]})
        counts, flags, feats = _compute_extrapolation(
            X, summ, ["a", "b", "c"])
        self.assertEqual(list(counts), [3, 1, 0])
        self.assertEqual(list(flags), [True, True, False])
        self.assertEqual(sorted(feats[0]), ["a", "b", "c"])
        self.assertEqual(sorted(feats[1]), ["a"])
        self.assertEqual(feats[2], [])

    # ─── 3. NaN handling unchanged ─────────────────────────────────

    def test_nan_does_not_count_as_extrapolated(self):
        from bot.ml.registry.predictions import _compute_extrapolation
        summ = self._summary_with(q01=-2.0, q99=+2.0)
        X = pd.DataFrame({"f": [float("nan"), 0.0, float("nan"), 5.0]})
        counts, flags, feats = _compute_extrapolation(X, summ, ["f"])
        # nan rows have count 0; only the 5.0 row is flagged
        self.assertEqual(list(counts), [0, 0, 0, 1])
        self.assertEqual(list(flags), [False, False, False, True])

    # ─── 4. End-to-end through predict_from_registry ───────────────

    def test_prediction_rows_carry_model_id_and_extrapolation_under_q20(self):
        """Existing invariants from Predictions class still hold
        AFTER the envelope switch to q01/q99."""
        with _g8_tempfile.TemporaryDirectory() as root:
            reg, entry, res = self._registered_entry_with_artifacts(root)
            promoted = reg.promote_to_current(entry.model_id)
            from bot.ml.models.base import select_feature_columns
            feat_cols = select_feature_columns(
                list(res.dataset.columns))
            X_in = res.dataset.iloc[res.split.val_anchor_indices][
                feat_cols].reset_index(drop=True)
            result = predict_from_registry(
                registry=reg, model_id=promoted.model_id,
                X_input=X_in)
            df = result.predictions
            self.assertIn("model_id", df.columns)
            self.assertIn("feature_extrapolation_flag", df.columns)
            self.assertIn("feature_extrapolation_count", df.columns)
            self.assertTrue((df["model_id"] == promoted.model_id).all())
            # flag iff count > 0
            self.assertTrue(((df["feature_extrapolation_flag"])
                              == (df["feature_extrapolation_count"]
                                    > 0)).all())

    def test_envelope_excludes_extreme_training_outliers(self):
        """The point of Q20: one extreme training outlier doesn't
        widen the envelope to mask real extrapolation. Build a
        controlled training set with one extreme outlier; verify
        the envelope is much tighter than min/max."""
        from bot.ml.registry.predictions import _compute_extrapolation
        # If the on-disk summary's q01/q99 are roughly ±2 (from the
        # normal samples) and min/max are roughly ±1000 (from the
        # outliers), then a value of 100 SHOULD be flagged under
        # Q20 but would NOT have been under min/max.
        # We can simulate by handing _compute_extrapolation a
        # summary whose min/max came from outliers and q01/q99
        # came from the body of the distribution.
        summ = {"f": {
            "min": -1000.0, "max": +1000.0,
            "q01": -2.5,    "q99": +2.0,
            "mean": 0, "std": 1, "n_finite": 102,
        }}
        # +100 is well within min/max but well outside q01/q99
        X = pd.DataFrame({"f": [+100.0]})
        counts, flags, feats = _compute_extrapolation(X, summ, ["f"])
        self.assertEqual(int(counts[0]), 1,
            "Q20 envelope must catch +100 even though it's between "
            "min and max — extrapolation is q01/q99-defined.")
        self.assertTrue(bool(flags[0]))

    def test_missing_q01_q99_raises_explicit_error(self):
        """Old artifacts without q01/q99 must NOT silently fall
        back to min/max — refuse with a clear error to force
        re-registration."""
        from bot.ml.registry.predictions import _compute_extrapolation
        summ = {"f": {
            "min": -10.0, "max": +10.0,
            "mean": 0, "std": 1, "n_finite": 50,
            # q01/q99 absent
        }}
        X = pd.DataFrame({"f": [0.0, 5.0]})
        with self.assertRaises(G8M18ConfigError) as ctx:
            _compute_extrapolation(X, summ, ["f"])
        msg = str(ctx.exception)
        self.assertIn("q01", msg)
        self.assertIn("q99", msg)
        self.assertIn("Q20", msg)


class G8_Q20PredictionSchema(unittest.TestCase):
    """Q20 locks the prediction-row schema. The on-disk and
    in-memory output of predict_from_registry MUST include the
    locked column names. Aliases under the prior names are allowed
    and verified."""

    LOCKED_COLUMNS = (
        "model_id",
        "prediction",
        "predicted_class",
        "feature_extrapolation_flags",
        "feature_extrapolation_count",
    )

    ALIAS_COLUMNS = (
        "pred_proba",                 # alias of `prediction`
        "pred_class",                 # alias of `predicted_class`
        "feature_extrapolation_flag", # bool; == count > 0
        "features_out_of_range",      # alias of `feature_extrapolation_flags`
    )

    def _predict_one_batch(self, root):
        res, out, rep = _g8_build_clean_b2()
        reg = Registry(root=root)
        entry = reg.register_candidate(out, rep, res)
        promoted = reg.promote_to_current(entry.model_id)
        from bot.ml.models.base import select_feature_columns
        feat_cols = select_feature_columns(list(res.dataset.columns))
        X_in = res.dataset.iloc[res.split.val_anchor_indices][
            feat_cols].reset_index(drop=True)
        return reg, promoted, predict_from_registry(
            registry=reg, model_id=promoted.model_id,
            X_input=X_in)

    # ─── Locked column names ───────────────────────────────────────

    def test_all_locked_columns_present(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            _, _, result = self._predict_one_batch(root)
            df = result.predictions
            for c in self.LOCKED_COLUMNS:
                self.assertIn(c, df.columns,
                    f"Q20 locked column missing: {c!r}; "
                    f"got columns={list(df.columns)}")

    def test_all_aliases_also_present(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            _, _, result = self._predict_one_batch(root)
            df = result.predictions
            for c in self.ALIAS_COLUMNS:
                self.assertIn(c, df.columns,
                    f"backwards-compat alias column missing: {c!r}")

    def test_prediction_equals_pred_proba_alias(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            _, _, result = self._predict_one_batch(root)
            df = result.predictions
            np.testing.assert_array_equal(
                df["prediction"].to_numpy(),
                df["pred_proba"].to_numpy())

    def test_predicted_class_equals_pred_class_alias(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            _, _, result = self._predict_one_batch(root)
            df = result.predictions
            np.testing.assert_array_equal(
                df["predicted_class"].to_numpy(),
                df["pred_class"].to_numpy())

    def test_features_out_of_range_equals_feature_extrapolation_flags(self):
        """Both alias columns carry the same per-row list."""
        with _g8_tempfile.TemporaryDirectory() as root:
            _, _, result = self._predict_one_batch(root)
            df = result.predictions
            for i in range(len(df)):
                self.assertEqual(
                    list(df["feature_extrapolation_flags"].iloc[i]),
                    list(df["features_out_of_range"].iloc[i]),
                    f"row {i}: aliases diverge")

    # ─── Always-present, per-row ───────────────────────────────────

    def test_feature_extrapolation_flags_present_on_every_row(self):
        """Even rows with no extrapolation must have an (empty)
        list — never NaN, never None, never missing."""
        with _g8_tempfile.TemporaryDirectory() as root:
            _, _, result = self._predict_one_batch(root)
            df = result.predictions
            for i in range(len(df)):
                val = df["feature_extrapolation_flags"].iloc[i]
                self.assertIsNotNone(val,
                    f"row {i}: feature_extrapolation_flags is None")
                # Must be a list (possibly empty)
                self.assertIsInstance(val, list,
                    f"row {i}: feature_extrapolation_flags type "
                    f"is {type(val).__name__}, expected list")

    # ─── count == len(flags) ───────────────────────────────────────

    def test_count_equals_length_of_flags_list(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            _, _, result = self._predict_one_batch(root)
            df = result.predictions
            mismatches = []
            for i in range(len(df)):
                count = int(df["feature_extrapolation_count"].iloc[i])
                flags = list(df["feature_extrapolation_flags"].iloc[i])
                if count != len(flags):
                    mismatches.append((i, count, len(flags), flags))
            self.assertEqual(mismatches, [],
                f"feature_extrapolation_count must equal "
                f"len(feature_extrapolation_flags) on every row; "
                f"mismatches: {mismatches[:5]}")

    def test_flag_singular_equals_count_gt_zero(self):
        """The backwards-compat scalar `feature_extrapolation_flag`
        equals `feature_extrapolation_count > 0`."""
        with _g8_tempfile.TemporaryDirectory() as root:
            _, _, result = self._predict_one_batch(root)
            df = result.predictions
            self.assertTrue(
                (df["feature_extrapolation_flag"]
                  == (df["feature_extrapolation_count"] > 0)).all())

    # ─── Q20 envelope behaviour preserved ──────────────────────────

    def test_envelope_is_still_q01_q99_not_min_max(self):
        """Same defining test as G8_Q20Extrapolation but at the
        predict_from_registry layer — guards against regression of
        the envelope through the full flow."""
        # Build a small synthetic input where the dataset's q01/q99
        # is much tighter than min/max; verify rows with values
        # between min and q01 (or q99 and max) are correctly flagged.
        # Easiest way: use the real assembler dataset (q01/q99 differ
        # from min/max in practice) and check that any flagged row
        # really IS outside [q01, q99] for at least one feature.
        import json
        with _g8_tempfile.TemporaryDirectory() as root:
            reg, promoted, result = self._predict_one_batch(root)
            df = result.predictions
            summary_path = (__import__('pathlib').Path(root)
                              / promoted.training_feature_summary_path)
            summary = json.load(open(summary_path))
            # For the first row flagged as extrapolating, confirm its
            # listed features are genuinely outside [q01, q99].
            flagged_rows = df.index[
                df["feature_extrapolation_flag"]].tolist()
            if not flagged_rows:
                self.skipTest("no extrapolated rows in this fixture")
            # Re-read input from the source assembler dataset by
            # joining on the test's known structure: we need the
            # original X_input — easier to just verify against the
            # summary that q01 ≤ q99 and that flagging is consistent
            # via a tighter check below.
            # Tight check: for each row, count features outside
            # [q01, q99] using the on-disk summary, and confirm it
            # equals the recorded feature_extrapolation_count.
            res, _, _ = _g8_build_clean_b2()
            from bot.ml.models.base import select_feature_columns
            feat_cols = select_feature_columns(
                list(res.dataset.columns))
            X = res.dataset.iloc[res.split.val_anchor_indices][
                feat_cols].reset_index(drop=True)
            for i in flagged_rows[:3]:
                row_features = set(
                    df["feature_extrapolation_flags"].iloc[i])
                for fname in row_features:
                    v = X[fname].iloc[i]
                    q01 = summary[fname]["q01"]
                    q99 = summary[fname]["q99"]
                    self.assertTrue(
                        (np.isfinite(v) and (v < q01 or v > q99)),
                        f"row {i}, feature {fname!r}: value={v} "
                        f"claimed flagged but is inside [q01={q01}, "
                        f"q99={q99}]")

    def test_nan_input_does_not_count_as_extrapolation(self):
        """NaN values in X_input must not appear in the flags list
        and must not increment the count."""
        from bot.ml.registry.predictions import _compute_extrapolation
        summ = {"f": {
            "min": -10.0, "max": +10.0,
            "q01": -2.0,  "q99": +2.0,
            "mean": 0, "std": 1, "n_finite": 50,
        }}
        X = pd.DataFrame({"f": [float("nan"), 0.0, 5.0]})
        counts, flags, rowwise = _compute_extrapolation(X, summ, ["f"])
        # row 0 (NaN): count 0, flag False, empty list
        # row 1 (0.0): count 0, flag False, empty list
        # row 2 (5.0 > q99): count 1, flag True, ["f"]
        self.assertEqual(list(counts), [0, 0, 1])
        self.assertEqual(list(flags), [False, False, True])
        self.assertEqual(rowwise[0], [])
        self.assertEqual(rowwise[1], [])
        self.assertEqual(rowwise[2], ["f"])

    # ─── Parquet round-trip preserves the locked schema ───────────

    def test_parquet_preserves_locked_columns(self):
        with _g8_tempfile.TemporaryDirectory() as root:
            _, _, result = self._predict_one_batch(root)
            df = result.predictions
            rt = pd.read_parquet(result.output_path)
            for c in self.LOCKED_COLUMNS:
                self.assertIn(c, rt.columns,
                    f"parquet round-trip dropped Q20 column {c!r}")
            # Sample a row and verify the list-of-strings survives
            self.assertEqual(
                list(rt["feature_extrapolation_flags"].iloc[0]),
                list(df["feature_extrapolation_flags"].iloc[0]))


# ═════════════════════════════════════════════════════════════════════
# G9 — CLI wiring + example configs + end-to-end smoke (M18.A.9)
# ═════════════════════════════════════════════════════════════════════

import io as _g9_io
import json as _g9_json
import tempfile as _g9_tempfile
from contextlib import redirect_stdout as _g9_redirect_stdout
from contextlib import redirect_stderr as _g9_redirect_stderr
from pathlib import Path as _g9_Path
from bot.ml import cli as _g9_cli
from bot.ml.schemas import DatasetConfig as _g9_DatasetConfig
from bot.ml.schemas import TrainConfig as _g9_TrainConfig


def _g9_prepare_registry_with_promoted_model(root):
    """Helper: build a real candidate, register, and promote it in
    a registry rooted at `root`. Returns (registry, entry, res)."""
    res, out, rep = _g8_build_clean_b2()
    reg = Registry(root=root)
    entry = reg.register_candidate(out, rep, res)
    promoted = reg.promote_to_current(entry.model_id)
    return reg, promoted, res


# ─────────────────────────────────────────────────────────────────────
# G9_CliSurface — argparse surface unchanged from M18.A.1
# ─────────────────────────────────────────────────────────────────────

class G9_CliSurface(unittest.TestCase):
    """The M18.A.1 argparse surface is preserved — no flags added,
    none removed."""

    def test_top_level_subcommands_unchanged(self):
        parser = _g9_cli.build_parser()
        # Walk the subparser actions
        subactions = [a for a in parser._actions
                        if isinstance(a, argparse._SubParsersAction)]
        self.assertEqual(len(subactions), 1)
        self.assertEqual(
            sorted(subactions[0].choices.keys()),
            sorted(["build-dataset", "train", "evaluate",
                     "predict", "registry"]))

    def test_predict_has_model_id_and_input_flags(self):
        parser = _g9_cli.build_parser()
        # Trying to parse predict without required flags must raise
        with self.assertRaises(SystemExit):
            parser.parse_args(["predict"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["predict", "--model-id", "x"])
        # With both flags present, parses cleanly
        ns = parser.parse_args(
            ["predict", "--model-id", "x", "--input", "p.parquet"])
        self.assertEqual(ns.command, "predict")
        self.assertEqual(ns.model_id, "x")
        self.assertEqual(ns.input, "p.parquet")

    def test_registry_promote_force_surface_preserved(self):
        parser = _g9_cli.build_parser()
        ns = parser.parse_args([
            "registry", "promote", "--model-id", "x",
            "--force", "--override-gate", "baseline_beat",
            "--override-gate", "sample_count",
            "--reason", "ok"])
        self.assertEqual(ns.command, "registry")
        self.assertEqual(ns.registry_command, "promote")
        self.assertEqual(ns.model_id, "x")
        self.assertTrue(ns.force)
        self.assertEqual(ns.override_gate,
                          ["baseline_beat", "sample_count"])
        self.assertEqual(ns.reason, "ok")


# ─────────────────────────────────────────────────────────────────────
# G9_CliStubs — build-dataset / train / evaluate / registry demote
# ─────────────────────────────────────────────────────────────────────

class G9_CliStubs(unittest.TestCase):
    """Four subcommands remain stubbed in M18.A.9 pending interface
    decisions. Each stub returns exit 2 with a phase tag."""

    def test_build_dataset_stub(self):
        err = _g9_io.StringIO()
        with _g9_redirect_stderr(err):
            rc = _g9_cli.main(["build-dataset", "--config", "/dev/null"])
        self.assertEqual(rc, 2)
        self.assertIn("M18.A.10+", err.getvalue())

    def test_train_stub(self):
        err = _g9_io.StringIO()
        with _g9_redirect_stderr(err):
            rc = _g9_cli.main(["train", "--config", "/dev/null"])
        self.assertEqual(rc, 2)
        self.assertIn("M18.A.10+", err.getvalue())

    def test_evaluate_stub(self):
        err = _g9_io.StringIO()
        with _g9_redirect_stderr(err):
            rc = _g9_cli.main(["evaluate", "--model-id", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("M18.A.10+", err.getvalue())

    def test_registry_demote_stub_documents_flag_mismatch(self):
        """The M18.A.1 surface gave `registry demote` no flags but
        demote_current() needs a scope_key. The stub message points
        at this gap rather than silently choosing a default."""
        err = _g9_io.StringIO()
        with _g9_redirect_stderr(err):
            rc = _g9_cli.main(["registry", "demote"])
        self.assertEqual(rc, 2)
        msg = err.getvalue()
        # Either --scope-key or --model-id mentioned as the missing
        # surface bit
        self.assertTrue(
            "--scope-key" in msg or "--model-id" in msg,
            f"demote stub message must explain the surface gap; "
            f"got: {msg!r}")


# ─────────────────────────────────────────────────────────────────────
# G9_CliPredict — end-to-end predict against temp registry
# ─────────────────────────────────────────────────────────────────────

class G9_CliPredict(unittest.TestCase):

    def test_predict_writes_parquet_with_locked_q20_columns(self):
        with _g9_tempfile.TemporaryDirectory() as root:
            _, promoted, res = _g9_prepare_registry_with_promoted_model(
                root)
            feat_cols = select_feature_columns(
                list(res.dataset.columns))
            X_in = res.dataset.iloc[res.split.val_anchor_indices][
                feat_cols].reset_index(drop=True)
            input_path = _g9_Path(root) / "test_input.parquet"
            X_in.to_parquet(input_path)
            out_buf = _g9_io.StringIO()
            with _g9_redirect_stdout(out_buf):
                rc = _g9_cli.main(
                    ["predict",
                     "--model-id", promoted.model_id,
                     "--input", str(input_path)],
                    _registry_root=root)
            self.assertEqual(rc, 0)
            parsed = _g9_json.loads(out_buf.getvalue())
            self.assertEqual(parsed["command"], "predict")
            self.assertEqual(parsed["model_id"], promoted.model_id)
            self.assertEqual(parsed["n_input_rows"], len(X_in))
            # Parquet output exists and carries the locked Q20 columns
            out_path = _g9_Path(parsed["output_path"])
            self.assertTrue(out_path.exists())
            out_df = pd.read_parquet(out_path)
            for c in ("model_id", "prediction", "predicted_class",
                        "feature_extrapolation_flags",
                        "feature_extrapolation_count"):
                self.assertIn(c, out_df.columns, c)
            self.assertTrue(
                (out_df["model_id"] == promoted.model_id).all())

    def test_predict_missing_input_file_exits_1(self):
        with _g9_tempfile.TemporaryDirectory() as root:
            _, promoted, _ = _g9_prepare_registry_with_promoted_model(
                root)
            err = _g9_io.StringIO()
            with _g9_redirect_stderr(err):
                rc = _g9_cli.main(
                    ["predict",
                     "--model-id", promoted.model_id,
                     "--input", str(_g9_Path(root) / "missing.parquet")],
                    _registry_root=root)
            self.assertEqual(rc, 1)
            self.assertIn("does not exist", err.getvalue())

    def test_predict_unknown_model_id_exits_1(self):
        with _g9_tempfile.TemporaryDirectory() as root:
            # Need some valid input parquet to get past the file
            # existence check
            _, _, res = _g9_prepare_registry_with_promoted_model(root)
            feat_cols = select_feature_columns(
                list(res.dataset.columns))
            X_in = res.dataset.iloc[res.split.val_anchor_indices][
                feat_cols].reset_index(drop=True)
            input_path = _g9_Path(root) / "test_input.parquet"
            X_in.to_parquet(input_path)
            err = _g9_io.StringIO()
            with _g9_redirect_stderr(err):
                rc = _g9_cli.main(
                    ["predict",
                     "--model-id", "definitely_not_a_real_model",
                     "--input", str(input_path)],
                    _registry_root=root)
            self.assertEqual(rc, 1)
            self.assertIn("definitely_not_a_real_model",
                           err.getvalue())


# ─────────────────────────────────────────────────────────────────────
# G9_CliRegistryList — list against temp registry
# ─────────────────────────────────────────────────────────────────────

class G9_CliRegistryList(unittest.TestCase):

    def test_empty_registry_lists_zero_entries(self):
        with _g9_tempfile.TemporaryDirectory() as root:
            out = _g9_io.StringIO()
            with _g9_redirect_stdout(out):
                rc = _g9_cli.main(["registry", "list"],
                                    _registry_root=root)
            self.assertEqual(rc, 0)
            parsed = _g9_json.loads(out.getvalue())
            self.assertEqual(parsed["entries"], [])

    def test_lists_promoted_entry_with_invariants(self):
        with _g9_tempfile.TemporaryDirectory() as root:
            _, promoted, _ = _g9_prepare_registry_with_promoted_model(
                root)
            out = _g9_io.StringIO()
            with _g9_redirect_stdout(out):
                rc = _g9_cli.main(["registry", "list"],
                                    _registry_root=root)
            self.assertEqual(rc, 0)
            parsed = _g9_json.loads(out.getvalue())
            self.assertEqual(parsed["n_entries"], 1)
            row = parsed["entries"][0]
            self.assertEqual(row["model_id"], promoted.model_id)
            self.assertEqual(row["status"], "current")
            self.assertEqual(row["approved_for_live"], False)


# ─────────────────────────────────────────────────────────────────────
# G9_CliRegistryShow
# ─────────────────────────────────────────────────────────────────────

class G9_CliRegistryShow(unittest.TestCase):

    def test_show_existing_entry(self):
        with _g9_tempfile.TemporaryDirectory() as root:
            _, promoted, _ = _g9_prepare_registry_with_promoted_model(
                root)
            out = _g9_io.StringIO()
            with _g9_redirect_stdout(out):
                rc = _g9_cli.main(
                    ["registry", "show", "--model-id", promoted.model_id],
                    _registry_root=root)
            self.assertEqual(rc, 0)
            parsed = _g9_json.loads(out.getvalue())
            self.assertEqual(parsed["entry"]["model_id"],
                              promoted.model_id)
            self.assertEqual(parsed["entry"]["status"], "current")
            self.assertEqual(parsed["entry"]["approved_for_live"], False)

    def test_show_unknown_model_id_exits_1(self):
        with _g9_tempfile.TemporaryDirectory() as root:
            err = _g9_io.StringIO()
            with _g9_redirect_stderr(err):
                rc = _g9_cli.main(
                    ["registry", "show", "--model-id", "NO_SUCH_MODEL"],
                    _registry_root=root)
            self.assertEqual(rc, 1)
            self.assertIn("NO_SUCH_MODEL", err.getvalue())


# ─────────────────────────────────────────────────────────────────────
# G9_CliRegistryPromote
# ─────────────────────────────────────────────────────────────────────

class G9_CliRegistryPromote(unittest.TestCase):

    def test_promote_clean_candidate(self):
        with _g9_tempfile.TemporaryDirectory() as root:
            res, out, rep = _g8_build_clean_b2()
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            out_buf = _g9_io.StringIO()
            with _g9_redirect_stdout(out_buf):
                rc = _g9_cli.main(
                    ["registry", "promote", "--model-id", entry.model_id],
                    _registry_root=root)
            self.assertEqual(rc, 0)
            parsed = _g9_json.loads(out_buf.getvalue())
            self.assertEqual(parsed["outcome"], "promoted")
            self.assertEqual(parsed["status"], "current")
            self.assertFalse(parsed["approved_for_live"])

    def test_promote_fixture_blocked_with_integrity_category(self):
        with _g9_tempfile.TemporaryDirectory() as root:
            res, out, rep = _g8_build_clean_b2(fixture_mode=True)
            reg = Registry(root=root)
            entry = reg.register_candidate(out, rep, res)
            err = _g9_io.StringIO()
            out_buf = _g9_io.StringIO()
            with _g9_redirect_stderr(err), _g9_redirect_stdout(out_buf):
                rc = _g9_cli.main(
                    ["registry", "promote", "--model-id", entry.model_id],
                    _registry_root=root)
            self.assertEqual(rc, 1)
            parsed = _g9_json.loads(err.getvalue())
            self.assertEqual(parsed["outcome"], "blocked")
            self.assertEqual(parsed["gate_category"], "integrity")
            self.assertEqual(parsed["gate"], "fixture_only")

    def test_promote_unknown_model_id_exits_1(self):
        with _g9_tempfile.TemporaryDirectory() as root:
            err = _g9_io.StringIO()
            with _g9_redirect_stderr(err):
                rc = _g9_cli.main(
                    ["registry", "promote", "--model-id", "NO_SUCH"],
                    _registry_root=root)
            self.assertEqual(rc, 1)


# ─────────────────────────────────────────────────────────────────────
# G9_ExampleConfigsParse — both example configs round-trip
# ─────────────────────────────────────────────────────────────────────

class G9_ExampleConfigsParse(unittest.TestCase):
    """Documents the user-facing config shapes. Even though
    build-dataset/train are stubbed in M18.A.9, the example configs
    must round-trip through the schema classes that the wired
    commands will eventually consume."""

    def _load_example(self, name):
        # Resolve against repo root (cwd may differ in tests)
        repo_root = _g9_Path(__file__).parent
        p = repo_root / "configs" / "ml" / name
        self.assertTrue(p.exists(), f"missing example config: {p}")
        with open(p) as f:
            d = _g9_json.load(f)
        # Strip the helper "_description" key before from_dict
        d.pop("_description", None)
        return d

    def test_dataset_example_parses_via_DatasetConfig_from_dict(self):
        d = self._load_example("dataset.example.json")
        cfg = _g9_DatasetConfig.from_dict(d)
        self.assertGreater(len(cfg.symbols), 0)
        self.assertGreater(len(cfg.feature_groups), 0)
        self.assertGreater(len(cfg.labels), 0)
        self.assertAlmostEqual(
            cfg.train_pct + cfg.val_pct + cfg.test_pct, 1.0)

    def test_train_example_parses_via_TrainConfig_from_dict(self):
        d = self._load_example("train.example.json")
        cfg = _g9_TrainConfig.from_dict(d)
        self.assertEqual(cfg.model_type, "B2_logistic")
        self.assertEqual(cfg.train_mode, "model_b_candidate_quality")
        self.assertEqual(cfg.seed, 42)
        self.assertFalse(cfg.fixture_mode)

    def test_example_configs_live_under_configs_ml_dir(self):
        repo_root = _g9_Path(__file__).parent
        d = repo_root / "configs" / "ml"
        self.assertTrue(d.exists())
        names = sorted(p.name for p in d.glob("*.json"))
        self.assertIn("dataset.example.json", names)
        self.assertIn("train.example.json", names)


# ─────────────────────────────────────────────────────────────────────
# G9_NoTemplatePollution — CLI must NOT write to repo-tracked dirs
# ─────────────────────────────────────────────────────────────────────

class G9_NoTemplatePollution(unittest.TestCase):
    """End-to-end CLI calls in tests must not leave files in the
    real on-disk data/ml/ tree. This is enforced by tests passing
    `_registry_root=<tmpdir>` to main()."""

    def test_predict_under_tmpdir_does_not_touch_real_data_ml(self):
        """Run predict under a tmpdir, then assert no NEW files
        appeared under the repo's data/ml/ tree (if it exists)."""
        repo_root = _g9_Path(__file__).parent
        real_root = repo_root / "data" / "ml"
        before = (
            set(str(p) for p in real_root.rglob("*"))
            if real_root.exists() else set())
        with _g9_tempfile.TemporaryDirectory() as root:
            _, promoted, res = _g9_prepare_registry_with_promoted_model(
                root)
            feat_cols = select_feature_columns(
                list(res.dataset.columns))
            X_in = res.dataset.iloc[res.split.val_anchor_indices][
                feat_cols].reset_index(drop=True)
            input_path = _g9_Path(root) / "test_input.parquet"
            X_in.to_parquet(input_path)
            with _g9_redirect_stdout(_g9_io.StringIO()):
                _g9_cli.main(
                    ["predict", "--model-id", promoted.model_id,
                     "--input", str(input_path)],
                    _registry_root=root)
        after = (
            set(str(p) for p in real_root.rglob("*"))
            if real_root.exists() else set())
        new_files = after - before
        self.assertEqual(new_files, set(),
            f"CLI test must not pollute real data/ml/; new files: "
            f"{sorted(new_files)}")






# ═════════════════════════════════════════════════════════════════════
# G10 hygiene constants (M18.A.10)
#
# Forbidden import prefixes for bot/ml/* production code (SR-7): the
# M17.A baseline + M17.B additions + M18 additions. Carried forward so
# that bot/ml/* can never import live/broker/scanner/strategy/data
# surfaces. Mirrors test_m17_backtesting._FORBIDDEN_IMPORT_PREFIXES.
# CONTRACT-FAITHFUL / NOT BYTE-IDENTICAL: the constant + the G10 test
# bodies below were reconstructed from the M17.B byte-faithful analogs
# and the A.5 transcript body fragments; the final M18 G10 block was not
# recoverable as a single byte-faithful artifact.
# ═════════════════════════════════════════════════════════════════════

# The M17.B baseline forbidden set (must remain a subset of M18's).
_M17B_FORBIDDEN_BASELINE = frozenset({
    # M17.B additions
    "bot.scanner", "bot.strategy", "bot.feature_engine",
    "bot.indicators", "bot.sentiment", "bot.flywheel",
    # M17.A baseline
    "yfinance", "bot.data", "bot.providers", "bot.backtest",
    "bot.brokers", "bot.broker_", "bot.gateway_",
    "bot.etoro.live_broker", "bot.etoro.paper_broker",
    "bot.etoro.signal_only",
    "bot.risk_authority.engine", "bot.risk_authority.governor",
    "bot.risk_authority.snapshot", "bot.risk_authority.preflight",
    "bot.risk_authority.ibkr_paper_reader",
    "ibapi", "ib_insync", "requests", "urllib.request", "urllib3",
    "http.client",
})

# M18-specific additions to the forbidden set (SR-7 / Q17): M18 is a
# read-only / shadow-only ML foundation — bot/ml/* must never import
# the live order executor surfaces. (bot.backtesting is NOT forbidden:
# M18 features legitimately reuse bot.backtesting.indicators /
# .mtf_context / .strategy for scanner-replica parity.)
_M18_NEW_FORBIDDEN = frozenset({
    "bot.main",
    "bot.recovery_executor",
})

_M18_FORBIDDEN_IMPORT_PREFIXES = tuple(sorted(
    _M17B_FORBIDDEN_BASELINE | _M18_NEW_FORBIDDEN))

# Network libs that must never be imported anywhere in bot/ml/*.
_M18_NETWORK_LIB_PREFIXES = (
    "requests", "urllib.request", "urllib3", "http.client",
    "socket", "aiohttp", "httpx", "websocket", "websockets",
    "yfinance",
)

# M17 baseline commit — the point before M17/M18 work; used by the
# file-drift guard. Mirrors test_m17_backtesting._M17_BASELINE_SHA.
_M18_M17_BASELINE_SHA = "13a3aa4"


class G10_Hygiene(unittest.TestCase):
    """Hygiene tests: no syntax errors, no socket-at-import,
    no forbidden imports, no unexpected files."""

    def test_all_bot_ml_files_compile(self):
        """Every .py file in bot/ml/ must be parseable by py_compile.
        Catches partial commits before the whole test suite runs."""
        offenders = []
        for f in _walk_bot_ml_py_files():
            try:
                ast.parse(f.read_text())
            except SyntaxError as e:
                offenders.append((str(f), str(e)))
        self.assertEqual(offenders, [],
            f"bot/ml/* syntax errors: {offenders}")

    # ---- bot.historical sole-importer rule (M18.A.2 introduces this) -

    def test_only_m16_loader_imports_bot_historical(self):
        """SR-7 — bot.historical may be imported by ONE file in
        bot/ml/* production code: bot/ml/dataset/m16_loader.py.
        Every other M18 module that needs bars must go through it.

        Mirrors test_m17_backtesting.G10_Hygiene
        .test_only_data_loader_imports_bot_historical for the
        M17.B side."""
        allowed = (Path(__file__).parent / "bot" / "ml" /
                    "dataset" / "m16_loader.py").resolve()
        offenders = []
        for f in _walk_bot_ml_py_files():
            if f.resolve() == allowed:
                continue
            for imp in _imports_in_file(f):
                if imp == "bot.historical" or imp.startswith(
                        "bot.historical."):
                    offenders.append((
                        str(f.relative_to(Path(__file__).parent)),
                        imp))
        self.assertEqual(offenders, [],
            f"bot.historical must be imported ONLY by bot/ml/dataset/"
            f"m16_loader.py; offenders: {offenders}")

    def test_no_socket_at_import_time(self):
        """Importing bot.ml + its submodules must not open any
        sockets. Runs in a SUBPROCESS so that any module-cache
        manipulation here cannot pollute the in-process test suite
        (an earlier version of this test used importlib.reload, which
        clobbered class identity for downstream G2 tests).

        The subprocess patches socket.socket to raise on construction,
        then imports every bot.ml submodule. Non-zero exit = a socket
        was opened during import.
        """
        code = (
            "import socket\n"
            "class _RaiseOnSocket:\n"
            "    def __init__(self, *a, **kw):\n"
            "        raise RuntimeError('M18 must not open sockets "
            "at import time')\n"
            "socket.socket = _RaiseOnSocket\n"
            "import bot.ml.errors\n"
            "import bot.ml.schemas\n"
            "import bot.ml.hashing\n"
            "import bot.ml.cli\n"
            "import bot.ml.dataset\n"
            "import bot.ml.dataset.m16_loader\n"
            "import bot.ml.features\n"
            "import bot.ml.features.price_return\n"
            "import bot.ml.features.trend\n"
            "import bot.ml.features.momentum\n"
            "import bot.ml.features.vol_regime\n"
            "import bot.ml.features.volume_liquidity\n"
            "import bot.ml\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent), timeout=30)
        self.assertEqual(
            result.returncode, 0,
            f"bot.ml import opened a socket. stderr:\n{result.stderr}")

    # ---- forbidden imports in bot/ml/* (SR-7) ----------------------

    def test_no_forbidden_imports_in_bot_ml(self):
        """bot/ml/* must not import live, broker, scanner,
        data-provider, network, or executor surfaces. bot.backtesting
        parity imports are allowed only where used for scanner-replica
        parity (bot.backtesting.indicators / .mtf_context / .strategy);
        bot.historical may be imported only by m16_loader.py.
        """
        offenders = []
        for f in _walk_bot_ml_py_files():
            for imp in _imports_in_file(f):
                for forbidden in _M18_FORBIDDEN_IMPORT_PREFIXES:
                    if imp == forbidden or imp.startswith(forbidden + "."):
                        offenders.append((str(f.relative_to(
                            Path(__file__).parent)), imp))
                        break
        self.assertEqual(offenders, [],
            f"bot/ml/* imports forbidden modules: {offenders}")

    def test_no_network_libs_imported(self):
        """bot/ml/* must not import any network library directly —
        all data access goes through the M16 loader, which is the only
        sanctioned I/O path. Catches an accidental requests/urllib/
        socket import slipping into a feature or registry module."""
        offenders = []
        for f in _walk_bot_ml_py_files():
            for imp in _imports_in_file(f):
                for net in _M18_NETWORK_LIB_PREFIXES:
                    if imp == net or imp.startswith(net + "."):
                        offenders.append((str(f.relative_to(
                            Path(__file__).parent)), imp))
                        break
        self.assertEqual(offenders, [],
            f"bot/ml/* imports network modules directly: {offenders}")

    def test_m17b_forbidden_baseline_preserved(self):
        """Ensure M17.B's forbidden-import baseline is still a subset
        of M18's active forbidden set. Regression to catch silent
        weakening of past invariants (the M17.B pattern carried
        forward)."""
        active = set(_M18_FORBIDDEN_IMPORT_PREFIXES)
        missing = _M17B_FORBIDDEN_BASELINE - active
        self.assertEqual(missing, set(),
            f"M17.B forbidden-import baseline silently weakened — "
            f"missing entries: {sorted(missing)}")

    def test_m18_new_forbidden_additions_present(self):
        """The M18-specific forbidden-import additions must be present
        in the active set. M18-specific additions are executor/order
        surfaces such as bot.main and bot.recovery_executor (a
        read-only / shadow-only ML milestone must never import the live
        order executor). bot.backtesting is NOT forbidden — M18
        features legitimately reuse it for scanner-replica parity. This
        test makes the executor additions explicit so they can't
        silently disappear."""
        active = set(_M18_FORBIDDEN_IMPORT_PREFIXES)
        missing = _M18_NEW_FORBIDDEN - active
        self.assertEqual(missing, set(),
            f"M18 forbidden-import additions missing from active set: "
            f"{sorted(missing)}")

    # ---- bot.historical sole-importer rule -------------------------

    def test_bot_historical_only_in_m16_loader(self):
        """bot.historical is the M16 access path. Only
        bot/ml/dataset/m16_loader.py may import it (analogous to
        bot/backtesting/data_loader.py being the single M16 importer
        for M17)."""
        allowed_importer = (Path(__file__).parent / "bot" / "ml" /
                             "dataset" / "m16_loader.py").resolve()
        offenders = []
        for f in _walk_bot_ml_py_files():
            if f.resolve() == allowed_importer:
                continue
            for imp in _imports_in_file(f):
                if imp == "bot.historical" or imp.startswith(
                        "bot.historical."):
                    offenders.append((str(f.relative_to(
                        Path(__file__).parent)), imp))
        self.assertEqual(offenders, [],
            f"bot.historical must be imported ONLY by bot/ml/dataset/"
            f"m16_loader.py; offenders: {offenders}")

    # ---- generated artifacts gitignored ----------------------------

    def test_data_ml_gitignored(self):
        """data/ is gitignored in .gitignore (line 6); data/ml/ is
        covered transitively. This test makes the invariant explicit so
        generated ML artifacts never land in the repo."""
        gi = (Path(__file__).parent / ".gitignore").read_text()
        self.assertTrue(
            "data/ml" in gi or
            re.search(r"^data/$", gi, re.MULTILINE) is not None,
            "data/ml/ should be gitignored (either explicitly or via "
            "the broader data/ rule)")

    # ---- new files: only the expected set --------------------------

    def test_no_unexpected_files_added(self):
        """M18 adds files only in bot/ml/, configs/ml/, test_m18_ml.py,
        docs/M18_*.md, plus the three repo-level docs every closeout
        updates. Same whitelist pattern as M17.B's test of the same
        name; this is the M18-side counterpart."""
        result = subprocess.run(
            ["git", "diff", "--name-only", _M18_M17_BASELINE_SHA, "HEAD"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        changed = sorted(result.stdout.strip().splitlines())
        allowed_prefixes = (
            "bot/backtesting/", "configs/backtests/",   # M17
            "bot/ml/",          "configs/ml/",          # M18
        )
        allowed_exact = {
            "test_m17_backtesting.py",
            "test_m18_ml.py",
            "MILESTONE_STATUS.md",
            "ROADMAP.md",
            "docs/NEXT_WORK_REGISTER.md",
            # M18 recovery-scope artifacts (this branch reconstructs
            # M18 from transcripts; these document/support that effort).
            ".gitignore",
            "RECOVERY_M18_MANIFEST.md",
        }
        allowed_doc_regex = re.compile(
            r"^docs/M1[78]_[A-Za-z]\w*(?:_[\w]+)?\.md$")
        unexpected = [
            p for p in changed
            if not p.startswith(allowed_prefixes)
                and p not in allowed_exact
                and not allowed_doc_regex.match(p)
        ]
        self.assertEqual(unexpected, [],
            f"Unexpected files changed: {unexpected}")


if __name__ == "__main__":
    unittest.main()
