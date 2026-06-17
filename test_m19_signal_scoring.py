"""test_m19_signal_scoring.py — pre-execution M19.A contract/config/provenance.

M19.A scope: contracts + config + provenance ONLY. No scoring, no gates, no
adapters, no output writer. Tests cover schema round-trip + validation, config
defaults + validation, deterministic provenance, the short-side structural
rule, and static safety guards (no broker/live/main/dashboard/network imports;
no signals.db / data/ml / data/m19 writes anywhere in the package).
"""
import ast
import os
import json
import tempfile
import pathlib
import unittest

from bot.signal_scoring import (
    SCHEMA_VERSION_INPUT, SCHEMA_VERSION_OUTPUT,
    ScoringProfile, SignalSide, DecisionBucket, ConfidenceBucket,
    PenaltySeverity, SignalCandidateInput, ScoredSignalCandidate,
    SignalScoringConfig, default_config, DEFAULT_PROFILE,
    ComponentScore,
    PenaltyItem, PenaltyResult, MultiplierItem, MultiplierResult,
    evaluate_penalties, evaluate_multipliers,
    PENALTY_NAMES, MULTIPLIER_NAMES,
    score_candidate, assemble_score,
    adapter_from_scanner_signal, adapter_from_candidate_snapshot,
    merge_ml_prediction, merge_readiness_advisories,
    scored_candidate_to_jsonl_line, is_write_safe_path,
    write_scored_candidates_jsonl,
    build_scoring_audit_record, build_scoring_audit_summary,
)
from bot.signal_scoring.schema import GateResult, GateFailure
from bot.signal_scoring import provenance
from bot.signal_scoring.config import CONFIG_SCHEMA_VERSION

_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_PKG_DIR = _REPO_ROOT / "bot" / "signal_scoring"


def _valid_input(**over):
    base = dict(symbol="AAPL", side="LONG",
                signal_timestamp_utc="2026-06-17T10:15:00Z")
    base.update(over)
    return SignalCandidateInput(**base)


# ───────────────────────── config ─────────────────────────
class M19AConfig(unittest.TestCase):

    def test_default_config_validates(self):
        c = default_config()
        c.validate()  # must not raise
        self.assertEqual(c.config_schema_version, CONFIG_SCHEMA_VERSION)

    def test_strict_is_default_profile(self):
        self.assertEqual(DEFAULT_PROFILE, ScoringProfile.STRICT)
        self.assertEqual(default_config().profile, ScoringProfile.STRICT)

    def test_research_profile_validates(self):
        c = default_config(ScoringProfile.RESEARCH)
        c.validate()
        self.assertEqual(c.profile, ScoringProfile.RESEARCH)

    def test_invalid_profile_rejected(self):
        with self.assertRaises(ValueError):
            SignalScoringConfig(profile="banana")

    def test_weights_sum_validation(self):
        bad = default_config().to_dict()
        bad["weights"] = dict(bad["weights"])
        bad["weights"]["ml"] = 0.99  # break the sum
        with self.assertRaises(ValueError):
            SignalScoringConfig.from_dict(bad)

    def test_weight_out_of_range_rejected(self):
        bad = default_config().to_dict()
        bad["weights"] = dict(bad["weights"])
        bad["weights"]["ml"] = 1.5
        with self.assertRaises(ValueError):
            SignalScoringConfig.from_dict(bad)

    def test_anchor_support_sum_validation(self):
        with self.assertRaises(ValueError):
            SignalScoringConfig(ml_anchor_weight=0.6, support_weight=0.5)

    def test_threshold_order_validation(self):
        bad = default_config().to_dict()
        bad["thresholds"] = dict(bad["thresholds"])
        bad["thresholds"]["eligible_min"] = 90  # > high_conviction_min(82)
        with self.assertRaises(ValueError):
            SignalScoringConfig.from_dict(bad)

    def test_negative_penalty_rejected(self):
        bad = default_config().to_dict()
        bad["penalties"] = dict(bad["penalties"])
        bad["penalties"]["weak_scanner_confluence"] = -1
        with self.assertRaises(ValueError):
            SignalScoringConfig.from_dict(bad)

    def test_multiplier_floor_range(self):
        bad = default_config().to_dict()
        bad["multipliers"] = dict(bad["multipliers"])
        bad["multipliers"]["multiplier_floor"] = 1.5
        with self.assertRaises(ValueError):
            SignalScoringConfig.from_dict(bad)

    def test_default_output_forbids_sqlite(self):
        self.assertFalse(default_config().output["allow_sqlite_write"])
        bad = default_config().to_dict()
        bad["output"] = dict(bad["output"])
        bad["output"]["allow_sqlite_write"] = True
        with self.assertRaises(ValueError):
            SignalScoringConfig.from_dict(bad)

    def test_default_output_in_memory_no_files(self):
        o = default_config().output
        self.assertEqual(o["default_mode"], "in_memory")
        self.assertFalse(o["allow_jsonl"])
        self.assertFalse(o["commit_outputs"])

    def test_config_roundtrip(self):
        c = default_config()
        c2 = SignalScoringConfig.from_dict(c.to_dict())
        self.assertEqual(c.to_dict(), c2.to_dict())


# ─────────────────────── provenance ───────────────────────
class M19AProvenance(unittest.TestCase):

    def test_canonical_json_deterministic(self):
        a = {"b": 1, "a": 2, "c": [3, 2, 1]}
        b = {"c": [3, 2, 1], "a": 2, "b": 1}
        self.assertEqual(provenance.canonical_json(a),
                         provenance.canonical_json(b))

    def test_canonical_json_rejects_nonfinite(self):
        with self.assertRaises(ValueError):
            provenance.canonical_json({"x": float("nan")})

    def test_config_hash_deterministic(self):
        self.assertEqual(default_config().config_hash(),
                         default_config().config_hash())

    def test_config_hash_changes_with_config(self):
        c = default_config()
        d = c.to_dict()
        d["ml_anchor_weight"] = 0.45
        d["support_weight"] = 0.55
        self.assertNotEqual(c.config_hash(),
                            SignalScoringConfig.from_dict(d).config_hash())

    def test_input_digest_deterministic(self):
        i1 = _valid_input().to_dict()
        i2 = _valid_input().to_dict()
        self.assertEqual(provenance.input_digest(i1),
                         provenance.input_digest(i2))

    def test_candidate_id_deterministic(self):
        i = _valid_input()
        idg = provenance.input_digest(i.to_dict())
        ch = default_config().config_hash()
        kw = dict(schema_version=i.schema_version, symbol=i.symbol,
                  side=i.side.value, signal_timestamp_utc=i.signal_timestamp_utc,
                  input_digest_hex=idg, config_hash_hex=ch)
        self.assertEqual(provenance.candidate_id(**kw),
                         provenance.candidate_id(**kw))

    def test_candidate_id_changes_with_signal(self):
        ch = default_config().config_hash()
        i1 = _valid_input()
        i2 = _valid_input(signal_timestamp_utc="2026-06-17T11:15:00Z")
        cid1 = provenance.candidate_id(
            schema_version=i1.schema_version, symbol=i1.symbol,
            side=i1.side.value, signal_timestamp_utc=i1.signal_timestamp_utc,
            input_digest_hex=provenance.input_digest(i1.to_dict()),
            config_hash_hex=ch)
        cid2 = provenance.candidate_id(
            schema_version=i2.schema_version, symbol=i2.symbol,
            side=i2.side.value, signal_timestamp_utc=i2.signal_timestamp_utc,
            input_digest_hex=provenance.input_digest(i2.to_dict()),
            config_hash_hex=ch)
        self.assertNotEqual(cid1, cid2)

    def test_candidate_id_no_wallclock_no_rng(self):
        """candidate_id must depend only on its inputs — two calls with the
        same args are identical even across time."""
        i = _valid_input()
        idg = provenance.input_digest(i.to_dict())
        ch = default_config().config_hash()
        kw = dict(schema_version=i.schema_version, symbol="AAPL", side="LONG",
                  signal_timestamp_utc=i.signal_timestamp_utc,
                  input_digest_hex=idg, config_hash_hex=ch)
        ids = {provenance.candidate_id(**kw) for _ in range(5)}
        self.assertEqual(len(ids), 1)


# ───────────────────────── schema ─────────────────────────
class M19ASchema(unittest.TestCase):

    def test_input_roundtrip(self):
        i = _valid_input(scanner_context={"valid_count": 3})
        i2 = SignalCandidateInput.from_dict(i.to_dict())
        self.assertEqual(i.to_dict(), i2.to_dict())

    def test_output_roundtrip(self):
        o = ScoredSignalCandidate(
            symbol="AAPL", side="LONG",
            signal_timestamp_utc="2026-06-17T10:15:00Z",
            candidate_id="x" * 64)
        o2 = ScoredSignalCandidate.from_dict(o.to_dict())
        self.assertEqual(o.to_dict(), o2.to_dict())

    def test_unknown_side_rejected(self):
        with self.assertRaises(ValueError):
            _valid_input(side="SIDEWAYS")

    def test_bad_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            _valid_input(signal_timestamp_utc="not-a-date")

    def test_missing_schema_version_rejected_input(self):
        d = _valid_input().to_dict()
        d.pop("schema_version")
        with self.assertRaises(ValueError):
            SignalCandidateInput.from_dict(d)

    def test_wrong_input_schema_version_rejected(self):
        with self.assertRaises(ValueError):
            _valid_input(schema_version="bogus_v9")

    def test_enums_present(self):
        self.assertEqual(
            {b.value for b in DecisionBucket},
            {"BLOCKED", "REJECT", "WATCH", "MANUAL_REVIEW",
             "ELIGIBLE", "HIGH_CONVICTION"})
        self.assertEqual({s.value for s in SignalSide}, {"LONG", "SHORT"})
        self.assertEqual({p.value for p in ScoringProfile},
                         {"strict", "research"})
        self.assertEqual({c.value for c in ConfidenceBucket},
                         {"LOW", "MEDIUM", "MEDIUM_HIGH", "HIGH"})
        self.assertEqual({p.value for p in PenaltySeverity},
                         {"info", "warning", "major", "blocking"})

    def test_short_cannot_be_execution_eligible(self):
        with self.assertRaises(ValueError):
            ScoredSignalCandidate(
                symbol="AAPL", side="SHORT",
                signal_timestamp_utc="2026-06-17T10:15:00Z",
                candidate_id="x" * 64, execution_eligible=True)

    def test_short_cannot_be_eligible_bucket(self):
        for bucket in (DecisionBucket.ELIGIBLE, DecisionBucket.HIGH_CONVICTION):
            with self.assertRaises(ValueError):
                ScoredSignalCandidate(
                    symbol="AAPL", side="SHORT",
                    signal_timestamp_utc="2026-06-17T10:15:00Z",
                    candidate_id="x" * 64, decision_bucket=bucket)

    def test_short_default_fixture_not_execution_eligible(self):
        o = ScoredSignalCandidate(
            symbol="AAPL", side="SHORT",
            signal_timestamp_utc="2026-06-17T10:15:00Z",
            candidate_id="x" * 64)
        self.assertFalse(o.execution_eligible)


# ──────────────────── static safety guards ────────────────────
class M19ASafetyGuards(unittest.TestCase):

    FORBIDDEN_IMPORTS = {
        "ib_insync", "requests", "urllib", "urllib2", "urllib3",
        "aiohttp", "socket", "http", "main", "dashboard",
    }
    FORBIDDEN_PREFIXES = ("bot.brokers", "bot.live", "dashboard", "main")

    def _iter_pkg_files(self):
        return sorted(_PKG_DIR.glob("*.py"))

    def test_no_forbidden_imports(self):
        offenders = []
        for path in self._iter_pkg_files():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        root = a.name.split(".")[0]
                        if root in self.FORBIDDEN_IMPORTS:
                            offenders.append(f"{path.name}: import {a.name}")
                        if a.name.startswith(self.FORBIDDEN_PREFIXES):
                            offenders.append(f"{path.name}: import {a.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    root = mod.split(".")[0]
                    if root in self.FORBIDDEN_IMPORTS:
                        offenders.append(f"{path.name}: from {mod}")
                    if mod.startswith(self.FORBIDDEN_PREFIXES):
                        offenders.append(f"{path.name}: from {mod}")
        self.assertEqual(offenders, [], f"forbidden imports: {offenders}")

    def test_no_network_or_db_write_tokens(self):
        """No DB/network CLIENT or WRITE call patterns anywhere in the package
        source. We scan for actual call/usage tokens (not descriptive prose in
        docstrings/comments, which would self-match a substring scan — a known
        hazard), backed by the runtime import-writes-nothing test below."""
        db_network_tokens = [
            "sqlite3.connect", "socket.socket",
            "requests.get", "requests.post", "urlopen",
            ".to_csv(", ".to_parquet(", ".to_pickle(",
        ]
        file_open_tokens = ["open(", "mkstemp(", "os.replace("]
        offenders = []
        for path in self._iter_pkg_files():
            src = path.read_text()
            for s in db_network_tokens:
                if s in src:
                    offenders.append(f"{path.name}: contains {s!r}")
            if path.name != "io.py":
                for s in file_open_tokens:
                    if s in src:
                        offenders.append(f"{path.name}: contains {s!r}")
        self.assertEqual(offenders, [], f"forbidden tokens: {offenders}")

    def test_importing_package_writes_nothing(self):
        """Importing the package must not create signals.db / data/ml /
        data/m19 artifacts."""
        import importlib
        before = {p for p in (
            _REPO_ROOT / "signals.db",
            _REPO_ROOT / "data" / "ml",
            _REPO_ROOT / "data" / "m19") if p.exists()}
        importlib.import_module("bot.signal_scoring")
        after = {p for p in (
            _REPO_ROOT / "signals.db",
            _REPO_ROOT / "data" / "ml",
            _REPO_ROOT / "data" / "m19") if p.exists()}
        self.assertEqual(before, after,
                         "import created a forbidden artifact")


class M19ACorrectivePass(unittest.TestCase):
    """Corrective M19.A hardening: UTC-only timestamps, unknown-field
    rejection on all three contracts, exact config-key validation, and
    score-range validation."""

    # 1. UTC timestamp validation
    def test_timestamp_z_accepted(self):
        _valid_input(signal_timestamp_utc="2026-06-17T10:15:00Z")

    def test_timestamp_offset_zero_accepted(self):
        _valid_input(signal_timestamp_utc="2026-06-17T10:15:00+00:00")

    def test_timestamp_naive_rejected(self):
        with self.assertRaises(ValueError):
            _valid_input(signal_timestamp_utc="2026-06-17T10:15:00")

    def test_timestamp_nonutc_offset_rejected(self):
        with self.assertRaises(ValueError):
            _valid_input(signal_timestamp_utc="2026-06-17T13:15:00+03:00")

    # 2. unknown top-level fields rejected
    def test_input_unknown_field_rejected(self):
        d = _valid_input().to_dict()
        d["surprise"] = 1
        with self.assertRaises(ValueError):
            SignalCandidateInput.from_dict(d)

    def test_output_unknown_field_rejected(self):
        o = ScoredSignalCandidate(
            symbol="AAPL", side="LONG",
            signal_timestamp_utc="2026-06-17T10:15:00Z", candidate_id="x" * 64)
        d = o.to_dict()
        d["surprise"] = 1
        with self.assertRaises(ValueError):
            ScoredSignalCandidate.from_dict(d)

    def test_config_unknown_field_rejected(self):
        d = default_config().to_dict()
        d["surprise"] = 1
        with self.assertRaises(ValueError):
            SignalScoringConfig.from_dict(d)

    # 3. exact config-key validation
    def test_missing_required_weight_key_rejected(self):
        d = default_config().to_dict()
        d["weights"] = dict(d["weights"])
        d["weights"].pop("calibration_uncertainty")
        with self.assertRaises(ValueError):
            SignalScoringConfig.from_dict(d)

    def test_unknown_extra_weight_key_rejected(self):
        d = default_config().to_dict()
        d["weights"] = dict(d["weights"])
        d["weights"]["bogus_factor"] = 0.0
        with self.assertRaises(ValueError):
            SignalScoringConfig.from_dict(d)

    def test_malformed_weights_single_key_rejected(self):
        d = default_config().to_dict()
        d["weights"] = {"ml": 1.0}
        with self.assertRaises(ValueError):
            SignalScoringConfig.from_dict(d)

    def test_exact_eleven_weight_keys_enforced(self):
        expected = {
            "ml", "scanner", "technical_confluence", "trend", "momentum",
            "volume_liquidity", "volatility", "market_regime",
            "risk_adjusted", "data_quality", "calibration_uncertainty"}
        self.assertEqual(set(default_config().weights), expected)

    def test_missing_required_block_key_rejected(self):
        for block, drop in (("thresholds", "eligible_min"),
                            ("ml", "high_conviction_probability"),
                            ("output", "allow_sqlite_write")):
            d = default_config().to_dict()
            d[block] = dict(d[block])
            d[block].pop(drop)
            with self.assertRaises(ValueError):
                SignalScoringConfig.from_dict(d)

    def test_extra_block_key_rejected(self):
        d = default_config().to_dict()
        d["ml"] = dict(d["ml"])
        d["ml"]["secret_knob"] = 1
        with self.assertRaises(ValueError):
            SignalScoringConfig.from_dict(d)

    # final_score range validation
    def test_final_score_out_of_range_rejected(self):
        for bad in (-1.0, 100.1):
            with self.assertRaises(ValueError):
                ScoredSignalCandidate(
                    symbol="AAPL", side="LONG",
                    signal_timestamp_utc="2026-06-17T10:15:00Z",
                    candidate_id="x" * 64, final_score=bad)
            with self.assertRaises(ValueError):
                ScoredSignalCandidate(
                    symbol="AAPL", side="LONG",
                    signal_timestamp_utc="2026-06-17T10:15:00Z",
                    candidate_id="x" * 64, final_score_100=bad)

    def test_final_score_in_range_accepted(self):
        ScoredSignalCandidate(
            symbol="AAPL", side="LONG",
            signal_timestamp_utc="2026-06-17T10:15:00Z",
            candidate_id="x" * 64, final_score=76.4, final_score_100=76.4)


class M19BHardGates(unittest.TestCase):
    """M19.B hard-gate engine: gates only, fail-safe, deterministic."""

    def _cfg(self, profile=None):
        return default_config(profile) if profile else default_config()

    def _clean(self, side="LONG", **over):
        from bot.signal_scoring import SignalCandidateInput
        blocks = dict(
            ml_context={
                "model_id": "m1", "calibration_applied": True,
                "prediction_calibrated": 0.68, "prediction_raw": 0.64,
                "price_adjustment_mode": "raw",
                "allow_adjusted_prices_for_ml": False,
                "model_readiness_passed": True,
                "production_thinness_status": "ok"},
            data_quality_context={
                "schema_match": True, "stale_data_flag": False,
                "data_freshness_minutes": 5, "missing_feature_count": 0},
            advisory_context={
                "adjusted_price_pit_risk": False,
                "scanner_replica_short_side_validated": False,
                "fourh_bucket_alignment": "utc_fixed"},
            timeframe_context={"available_timeframes": 4,
                               "valid_timeframes": 4},
            risk_preview={"risk_preview_available": True,
                          "risk_authority_status": "ok"},
            liquidity_context={"avg_dollar_volume_20d": 50_000_000,
                               "price": 150.0},
        )
        # allow targeted overrides of nested keys via block kwargs
        for blk, patch in over.items():
            if blk in blocks and isinstance(patch, dict):
                merged = dict(blocks[blk]); merged.update(patch)
                blocks[blk] = merged
            else:
                blocks[blk] = patch
        return SignalCandidateInput(
            symbol="AAPL", side=side,
            signal_timestamp_utc="2026-06-17T10:15:00Z", **blocks)

    def _run(self, ci=None, profile=None, **over):
        from bot.signal_scoring import evaluate_hard_gates
        if ci is None:
            ci = self._clean(**over)
        return evaluate_hard_gates(ci, self._cfg(profile))

    def _block_codes(self, r):
        return set(r.block_reasons)

    # all-pass
    def test_clean_long_passes(self):
        r = self._run()
        self.assertTrue(r.passed)
        self.assertIsNone(r.decision_bucket)
        self.assertEqual(r.failures, [])

    # individual BLOCK gates
    def test_schema_mismatch_blocks(self):
        r = self._run(data_quality_context={"schema_match": False})
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("schema_mismatch", self._block_codes(r))

    def test_risk_preview_unavailable_blocks(self):
        r = self._run(risk_preview={"risk_preview_available": False})
        self.assertIn("risk_preview_unavailable", self._block_codes(r))

    def test_risk_authority_blocked_blocks(self):
        r = self._run(risk_preview={"risk_authority_status": "blocked"})
        self.assertIn("risk_authority_blocked", self._block_codes(r))

    def test_insufficient_timeframes_blocks(self):
        r = self._run(timeframe_context={"available_timeframes": 3})
        self.assertIn("insufficient_available_timeframes", self._block_codes(r))

    def test_insufficient_timeframes_list_form(self):
        r = self._run(timeframe_context={
            "available_timeframes": ["1D", "4H", "1H"]})  # 3 < 4
        self.assertIn("insufficient_available_timeframes", self._block_codes(r))

    def test_stale_data_flag_blocks(self):
        r = self._run(data_quality_context={"stale_data_flag": True})
        self.assertIn("stale_data", self._block_codes(r))

    def test_stale_data_freshness_blocks(self):
        r = self._run(data_quality_context={"data_freshness_minutes": 31})
        self.assertIn("stale_data", self._block_codes(r))

    def test_pit_via_advisory_flag_blocks(self):
        r = self._run(advisory_context={"adjusted_price_pit_risk": True})
        self.assertIn("adjusted_price_pit_risk", self._block_codes(r))

    def test_pit_via_adjusted_mode_without_flag_blocks(self):
        r = self._run(ml_context={"price_adjustment_mode": "adjusted",
                                  "allow_adjusted_prices_for_ml": False})
        self.assertIn("adjusted_price_pit_risk", self._block_codes(r))

    def test_adjusted_mode_with_allow_flag_does_not_pit_block(self):
        r = self._run(ml_context={"price_adjustment_mode": "adjusted",
                                  "allow_adjusted_prices_for_ml": True})
        self.assertNotIn("adjusted_price_pit_risk", self._block_codes(r))

    def test_model_readiness_failed_blocks(self):
        r = self._run(ml_context={"model_readiness_passed": False})
        self.assertIn("model_readiness_failed", self._block_codes(r))

    def test_production_thinness_blocked_blocks(self):
        r = self._run(ml_context={"production_thinness_status": "blocked"})
        self.assertIn("production_thinness_blocked", self._block_codes(r))

    def test_thinness_warned_does_not_block(self):
        r = self._run(ml_context={"production_thinness_status": "warned"})
        self.assertNotIn("production_thinness_blocked", self._block_codes(r))
        self.assertTrue(r.passed)  # warned is a later-phase penalty, not a gate

    def test_below_min_liquidity_blocks(self):
        r = self._run(liquidity_context={"avg_dollar_volume_20d": 1_000_000})
        self.assertIn("below_min_liquidity", self._block_codes(r))

    def test_price_below_min_blocks(self):
        r = self._run(liquidity_context={"price": 1.0})
        self.assertIn("below_min_liquidity", self._block_codes(r))

    # calibration profile behaviour
    def test_missing_calibration_strict_blocks(self):
        r = self._run(ml_context={"calibration_applied": False,
                                  "prediction_calibrated": None})
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("calibration_unavailable", self._block_codes(r))

    def test_missing_calibration_research_manual_review(self):
        r = self._run(profile=ScoringProfile.RESEARCH,
                      ml_context={"calibration_applied": False,
                                  "prediction_calibrated": None})
        self.assertEqual(r.decision_bucket.value, "MANUAL_REVIEW")
        self.assertIn("calibration_unavailable", r.manual_review_reasons)

    # short side profile behaviour
    def test_short_strict_blocks(self):
        r = self._run(side="SHORT")
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("short_side_blocked", self._block_codes(r))

    def test_short_research_manual_review(self):
        r = self._run(side="SHORT", profile=ScoringProfile.RESEARCH)
        self.assertEqual(r.decision_bucket.value, "MANUAL_REVIEW")
        self.assertIn("short_side_manual_review", r.manual_review_reasons)

    # precedence
    def test_block_takes_precedence_over_manual_review(self):
        # research SHORT (MANUAL_REVIEW) + schema mismatch (BLOCK) -> BLOCKED
        r = self._run(side="SHORT", profile=ScoringProfile.RESEARCH,
                      data_quality_context={"schema_match": False})
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("schema_mismatch", self._block_codes(r))
        self.assertIn("short_side_manual_review", r.manual_review_reasons)

    # missing key
    def test_missing_required_key_blocks(self):
        ci = self._clean()
        d = ci.to_dict()
        d["ml_context"] = dict(d["ml_context"])
        d["ml_context"].pop("model_readiness_passed")
        from bot.signal_scoring import SignalCandidateInput
        ci2 = SignalCandidateInput.from_dict(d)
        r = self._run(ci=ci2)
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("missing_context_key", self._block_codes(r))

    # invalid value/type fail-safe
    def test_invalid_liquidity_value_blocks_safely(self):
        r = self._run(liquidity_context={"avg_dollar_volume_20d": "abc"})
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("invalid_context_value", self._block_codes(r))

    def test_invalid_timeframe_value_blocks_safely(self):
        r = self._run(timeframe_context={"available_timeframes": "four"})
        self.assertIn("invalid_context_value", self._block_codes(r))

    def test_invalid_calibration_applied_string_blocks_safely(self):
        # "yes" must NOT be silently treated as True
        r = self._run(ml_context={"calibration_applied": "yes"})
        self.assertIn("invalid_context_value", self._block_codes(r))

    def test_invalid_price_none_blocks_safely(self):
        r = self._run(liquidity_context={"price": None})
        self.assertIn("invalid_context_value", self._block_codes(r))

    # determinism + ordering + round-trip
    def test_gate_result_deterministic(self):
        ci = self._clean(side="SHORT")
        cfg = self._cfg()
        from bot.signal_scoring import evaluate_hard_gates
        r1 = evaluate_hard_gates(ci, cfg).to_dict()
        r2 = evaluate_hard_gates(ci, cfg).to_dict()
        self.assertEqual(r1, r2)

    def test_evaluated_gates_order_stable(self):
        from bot.signal_scoring import GATE_ORDER
        r = self._run()
        self.assertEqual(tuple(r.evaluated_gates), GATE_ORDER)

    def test_gate_order_is_explicit_not_alphabetic(self):
        from bot.signal_scoring import GATE_ORDER
        self.assertEqual(GATE_ORDER[0], "missing_context_key")
        self.assertNotEqual(list(GATE_ORDER), sorted(GATE_ORDER))

    def test_gate_result_roundtrip(self):
        from bot.signal_scoring import GateResult
        r = self._run(side="SHORT")
        r2 = GateResult.from_dict(r.to_dict())
        self.assertEqual(r.to_dict(), r2.to_dict())

    def test_gate_failure_roundtrip(self):
        from bot.signal_scoring import GateFailure
        r = self._run(side="SHORT")
        f = r.failures[0]
        f2 = GateFailure.from_dict(f.to_dict())
        self.assertEqual(f.to_dict(), f2.to_dict())

    def test_gate_failure_cannot_be_pass(self):
        from bot.signal_scoring import GateFailure, GateOutcome
        with self.assertRaises(ValueError):
            GateFailure(gate_name="x", outcome=GateOutcome.PASS,
                        reason_code="y")

    # ── corrective pass: non-dict context blocks must fail safe ──
    def test_non_dict_ml_context_blocks_safely(self):
        r = self._run(ml_context="bad")
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("missing_context_key", r.block_reasons)

    def test_non_dict_data_quality_context_blocks_safely(self):
        r = self._run(data_quality_context="bad")
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("missing_context_key", r.block_reasons)

    def test_non_dict_risk_preview_blocks_safely(self):
        r = self._run(risk_preview="bad")
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("missing_context_key", r.block_reasons)

    def test_non_dict_liquidity_context_blocks_safely(self):
        r = self._run(liquidity_context="bad")
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("missing_context_key", r.block_reasons)

    def test_non_dict_block_does_not_raise(self):
        """All gate-critical blocks non-dict at once must still return a
        GateResult (not raise) and BLOCK."""
        from bot.signal_scoring import evaluate_hard_gates, SignalCandidateInput
        ci = SignalCandidateInput(
            symbol="AAPL", side="LONG",
            signal_timestamp_utc="2026-06-17T10:15:00Z",
            ml_context="x", data_quality_context="x", advisory_context="x",
            timeframe_context="x", risk_preview="x", liquidity_context="x")
        r = evaluate_hard_gates(ci, self._cfg())
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("missing_context_key", r.block_reasons)

    # ── corrective pass: unknown enum strings must fail safe ──
    def test_unknown_price_adjustment_mode_blocks(self):
        r = self._run(ml_context={"price_adjustment_mode": "banana"})
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("invalid_context_value", r.block_reasons)

    def test_unknown_production_thinness_status_blocks(self):
        r = self._run(ml_context={"production_thinness_status": "mystery"})
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("invalid_context_value", r.block_reasons)

    def test_unknown_risk_authority_status_blocks(self):
        r = self._run(risk_preview={"risk_authority_status": "bad_status"})
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("invalid_context_value", r.block_reasons)

    def test_known_enum_values_still_accepted(self):
        # warned/ok are valid and must not trip invalid_context_value
        for status in ("ok", "warned"):
            r = self._run(ml_context={"production_thinness_status": status})
            self.assertNotIn("invalid_context_value", r.block_reasons)
        r2 = self._run(ml_context={"price_adjustment_mode": "adjusted",
                                   "allow_adjusted_prices_for_ml": True})
        self.assertNotIn("invalid_context_value", r2.block_reasons)

    # ── corrective pass: calibrated probability type/range validation ──
    def test_calibrated_probability_non_numeric_blocks(self):
        r = self._run(ml_context={"calibration_applied": True,
                                  "prediction_calibrated": "abc"})
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("invalid_context_value", r.block_reasons)

    def test_calibrated_probability_bool_blocks(self):
        r = self._run(ml_context={"calibration_applied": True,
                                  "prediction_calibrated": True})
        self.assertEqual(r.decision_bucket.value, "BLOCKED")
        self.assertIn("invalid_context_value", r.block_reasons)

    def test_calibrated_probability_below_zero_blocks(self):
        r = self._run(ml_context={"calibration_applied": True,
                                  "prediction_calibrated": -0.1})
        self.assertIn("invalid_context_value", r.block_reasons)

    def test_calibrated_probability_above_one_blocks(self):
        r = self._run(ml_context={"calibration_applied": True,
                                  "prediction_calibrated": 1.2})
        self.assertIn("invalid_context_value", r.block_reasons)

    def test_calibrated_probability_valid_passes_gate(self):
        r = self._run(ml_context={"calibration_applied": True,
                                  "prediction_calibrated": 0.68})
        self.assertTrue(r.passed)
        self.assertNotIn("invalid_context_value", r.block_reasons)
        self.assertNotIn("calibration_unavailable", r.block_reasons)

    def test_calibrated_probability_none_is_unavailable_not_invalid(self):
        # None remains the "unavailable" path (strict BLOCK), NOT invalid value
        r = self._run(ml_context={"calibration_applied": True,
                                  "prediction_calibrated": None})
        self.assertIn("calibration_unavailable", r.block_reasons)
        self.assertNotIn("invalid_context_value", r.block_reasons)


class M19CComponents(unittest.TestCase):
    """M19.C component scorers: pure, profile-neutral, 0-100, fail-safe."""

    def _cfg(self):
        return default_config()

    def _full_blocks(self):
        return dict(
            ml_context={"model_id": "m1", "calibration_applied": True,
                        "prediction_calibrated": 0.68, "prediction_raw": 0.64,
                        "price_adjustment_mode": "raw",
                        "allow_adjusted_prices_for_ml": False,
                        "model_readiness_passed": True,
                        "production_thinness_status": "ok",
                        "feature_extrapolation_count": 0},
            data_quality_context={"schema_match": True, "stale_data_flag": False,
                                  "data_freshness_minutes": 5,
                                  "missing_feature_count": 0},
            advisory_context={"adjusted_price_pit_risk": False},
            timeframe_context={"available_timeframes": 4, "valid_timeframes": 4},
            risk_preview={"risk_preview_available": True,
                          "risk_authority_status": "ok",
                          "reward_risk_ratio": 2.0},
            liquidity_context={"avg_dollar_volume_20d": 60_000_000,
                               "price": 150.0, "spread_pct": 0.05},
            technical_context={"ema20": 151, "ema50": 148, "rsi": 60,
                               "macd_hist": 0.4, "volume_ratio": 1.6,
                               "atr_pct": 0.02},
            volatility_context={"atr_pct": 0.02, "volatility_band": "normal"},
            regime_context={"regime_label": "bull", "benchmark_trend": "up",
                            "regime_source": "supplied_input"},
            scanner_context={"valid_count": 4, "required_count": 4,
                             "available_timeframes": 4, "valid_timeframes": 4},
        )

    def _ci(self, side="LONG", *, replace=None):
        """Build input. `replace` is a dict of block_name -> full replacement
        block (no merge), so tests can drop/replace blocks precisely."""
        from bot.signal_scoring import SignalCandidateInput
        blocks = self._full_blocks()
        if replace:
            for k, v in replace.items():
                blocks[k] = v
        return SignalCandidateInput(
            symbol="AAPL", side=side,
            signal_timestamp_utc="2026-06-17T10:15:00Z", **blocks)

    def _score(self, name, **kw):
        from bot.signal_scoring import score_component
        return score_component(name, self._ci(**kw), self._cfg())

    # one clean-input band test per component
    def test_clean_bands(self):
        from bot.signal_scoring import score_all_components, COMPONENT_NAMES
        res = score_all_components(self._ci(), self._cfg())
        self.assertEqual(set(res), set(COMPONENT_NAMES))
        for name, c in res.items():
            self.assertTrue(0.0 <= c.score <= 100.0)
        self.assertGreaterEqual(res["ml"].score, 60)
        self.assertGreaterEqual(res["scanner"].score, 85)
        self.assertGreaterEqual(res["risk_adjusted"].score, 85)
        self.assertGreaterEqual(res["data_quality"].score, 95)

    # scanner boundaries
    def test_scanner_4_of_4(self):
        c = self._score("scanner", replace={"scanner_context": {
            "valid_count": 4, "available_timeframes": 4}})
        self.assertGreaterEqual(c.score, 90)

    def test_scanner_3_of_4(self):
        c = self._score("scanner", replace={"scanner_context": {
            "valid_count": 3, "available_timeframes": 4}})
        self.assertTrue(50 <= c.score <= 85)

    def test_scanner_2_of_4_low(self):
        c = self._score("scanner", replace={"scanner_context": {
            "valid_count": 2, "available_timeframes": 4}})
        self.assertLess(c.score, 50)

    # ML calibrated/raw/missing
    def test_ml_calibrated_used(self):
        c = self._score("ml")
        self.assertIn("ml_calibrated_probability_used", c.reason_codes)
        self.assertAlmostEqual(c.score, 68.0, places=4)

    def test_ml_raw_fallback_warns(self):
        c = self._score("ml", replace={"ml_context": {
            "calibration_applied": False, "prediction_calibrated": None,
            "prediction_raw": 0.6}})
        self.assertIn("raw_probability_used", c.warnings)
        self.assertAlmostEqual(c.score, 60.0, places=4)

    def test_ml_both_unavailable_low(self):
        c = self._score("ml", replace={"ml_context": {
            "calibration_applied": False, "prediction_calibrated": None,
            "prediction_raw": None}})
        self.assertEqual(c.score, 25.0)
        self.assertIn("ml_probability_unavailable", c.blocked_reasons)

    # RSI / MACD edge + side awareness
    def test_momentum_long_vs_short_side_aware(self):
        long_c = self._score("momentum", side="LONG", replace={
            "technical_context": {"rsi": 60, "macd_hist": 0.5}})
        short_c = self._score("momentum", side="SHORT", replace={
            "technical_context": {"rsi": 60, "macd_hist": 0.5}})
        self.assertGreater(long_c.score, short_c.score)

    def test_trend_side_awareness(self):
        # ema20>ema50 favors LONG, penalizes SHORT
        long_c = self._score("trend", side="LONG", replace={
            "technical_context": {"ema20": 151, "ema50": 148}})
        short_c = self._score("trend", side="SHORT", replace={
            "technical_context": {"ema20": 151, "ema50": 148}})
        self.assertGreater(long_c.score, short_c.score)

    # regime side awareness
    def test_regime_side_awareness(self):
        long_bull = self._score("market_regime", side="LONG", replace={
            "regime_context": {"regime_label": "bull"}})
        short_bull = self._score("market_regime", side="SHORT", replace={
            "regime_context": {"regime_label": "bull"}})
        self.assertGreater(long_bull.score, short_bull.score)

    def test_regime_unknown_neutral(self):
        c = self._score("market_regime", replace={
            "regime_context": {"regime_label": "unknown"}})
        self.assertEqual(c.score, 50.0)

    # reward:risk boundaries
    def test_reward_risk_below_min(self):
        c = self._score("risk_adjusted", replace={"risk_preview": {
            "reward_risk_ratio": 1.0}})
        self.assertLess(c.score, 50)

    def test_reward_risk_at_min(self):
        c = self._score("risk_adjusted", replace={"risk_preview": {
            "reward_risk_ratio": 1.5}})
        self.assertTrue(50 <= c.score < 90)

    def test_reward_risk_ideal(self):
        c = self._score("risk_adjusted", replace={"risk_preview": {
            "reward_risk_ratio": 2.5}})
        self.assertGreaterEqual(c.score, 90)

    # ATR% bands
    def test_volatility_below_min(self):
        c = self._score("volatility", replace={"volatility_context": {
            "atr_pct": 0.001}})
        self.assertLess(c.score, 40)

    def test_volatility_ideal(self):
        c = self._score("volatility", replace={"volatility_context": {
            "atr_pct": 0.02}})
        self.assertGreaterEqual(c.score, 80)

    def test_volatility_elevated(self):
        c = self._score("volatility", replace={"volatility_context": {
            "atr_pct": 0.08}})
        self.assertLess(c.score, 40)

    # liquidity ideal/thin
    def test_liquidity_ideal(self):
        c = self._score("volume_liquidity", replace={"liquidity_context": {
            "avg_dollar_volume_20d": 60_000_000, "price": 150.0}})
        self.assertGreaterEqual(c.score, 90)

    def test_liquidity_thin_but_allowed(self):
        c = self._score("volume_liquidity", replace={"liquidity_context": {
            "avg_dollar_volume_20d": 12_000_000, "price": 150.0}})
        self.assertTrue(50 <= c.score < 90)

    # data quality degraded
    def test_data_quality_degraded(self):
        c = self._score("data_quality", replace={"data_quality_context": {
            "missing_feature_count": 2, "schema_match": True,
            "stale_data_flag": False, "data_freshness_minutes": 5}})
        self.assertLess(c.score, 100)

    # calibration uncertainty cases
    def test_calibration_uncertainty_clean(self):
        c = self._score("calibration_uncertainty")
        self.assertGreaterEqual(c.score, 95)

    def test_calibration_uncertainty_raw_and_extrapolation(self):
        c = self._score("calibration_uncertainty", replace={"ml_context": {
            "calibration_applied": False, "feature_extrapolation_count": 3,
            "production_thinness_status": "warned"}})
        self.assertLess(c.score, 60)
        self.assertIn("raw_probability_used", c.warnings)

    # fallback behaviour
    def test_missing_soft_key_neutral_fallback(self):
        c = self._score("trend", replace={"technical_context": {}})
        self.assertEqual(c.score, 50.0)
        self.assertIn("missing_soft_input", c.warnings)

    def test_invalid_soft_value_low_fallback(self):
        c = self._score("momentum", replace={"technical_context": {
            "rsi": "high", "macd_hist": "x"}})
        self.assertEqual(c.score, 25.0)
        self.assertIn("invalid_soft_input", c.warnings)

    def test_inputs_used_records_fallback(self):
        c = self._score("risk_adjusted", replace={"risk_preview": {}})
        self.assertEqual(c.score, 50.0)
        self.assertIn("reward_risk_ratio", c.inputs_used)

    # determinism + contract
    def test_component_deterministic(self):
        from bot.signal_scoring import score_all_components
        a = {k: v.to_dict() for k, v in
             score_all_components(self._ci(), self._cfg()).items()}
        b = {k: v.to_dict() for k, v in
             score_all_components(self._ci(), self._cfg()).items()}
        self.assertEqual(a, b)

    def test_component_score_roundtrip(self):
        c = self._score("ml")
        c2 = ComponentScore.from_dict(c.to_dict())
        self.assertEqual(c.to_dict(), c2.to_dict())

    def test_component_score_unknown_field_rejected(self):
        c = self._score("ml")
        d = c.to_dict(); d["surprise"] = 1
        with self.assertRaises(ValueError):
            ComponentScore.from_dict(d)

    def test_unknown_component_name_rejected(self):
        from bot.signal_scoring import score_component, make_component_score
        from bot.signal_scoring import COMPONENT_NAMES
        with self.assertRaises(ValueError):
            score_component("bogus", self._ci(), self._cfg())
        with self.assertRaises(ValueError):
            make_component_score("bogus", 50, allowed_components=COMPONENT_NAMES)

    def test_non_numeric_score_rejected_by_builder(self):
        from bot.signal_scoring import make_component_score, COMPONENT_NAMES
        with self.assertRaises(ValueError):
            make_component_score("ml", "x", allowed_components=COMPONENT_NAMES)

    def test_bool_score_rejected_by_builder(self):
        from bot.signal_scoring import make_component_score, COMPONENT_NAMES
        with self.assertRaises(ValueError):
            make_component_score("ml", True, allowed_components=COMPONENT_NAMES)

    def test_builder_clamps_score(self):
        from bot.signal_scoring import make_component_score, COMPONENT_NAMES
        self.assertEqual(make_component_score(
            "ml", 150, allowed_components=COMPONENT_NAMES).score, 100.0)
        self.assertEqual(make_component_score(
            "ml", -5, allowed_components=COMPONENT_NAMES).score, 0.0)

    def test_profile_neutral(self):
        from bot.signal_scoring import score_all_components, ScoringProfile
        strict = {k: v.score for k, v in score_all_components(
            self._ci(), default_config(ScoringProfile.STRICT)).items()}
        research = {k: v.score for k, v in score_all_components(
            self._ci(), default_config(ScoringProfile.RESEARCH)).items()}
        self.assertEqual(strict, research)

    def test_component_names_exact_order(self):
        from bot.signal_scoring import COMPONENT_NAMES
        self.assertEqual(COMPONENT_NAMES, (
            "ml", "scanner", "technical_confluence", "trend", "momentum",
            "volume_liquidity", "volatility", "market_regime",
            "risk_adjusted", "data_quality", "calibration_uncertainty"))

    def test_components_do_not_import_gates(self):
        import ast
        src = (_PKG_DIR / "components.py").read_text()
        tree = ast.parse(src)
        offenders = []
        gate_symbols = {"evaluate_hard_gates", "GateResult", "GateFailure"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith("gates"):
                    offenders.append(f"import-module:{node.module}")
                for a in node.names:
                    if a.name in gate_symbols:
                        offenders.append(f"import-name:{a.name}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.endswith("gates"):
                        offenders.append(f"import:{a.name}")
            elif isinstance(node, ast.Call):
                # flag any call to a gate symbol (Name or Attribute)
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id in gate_symbols:
                    offenders.append(f"call:{fn.id}")
                elif isinstance(fn, ast.Attribute) and fn.attr in gate_symbols:
                    offenders.append(f"call:{fn.attr}")
            elif isinstance(node, ast.Name) and node.id in gate_symbols:
                # a bare reference to a gate symbol in code (not docstring)
                offenders.append(f"ref:{node.id}")
        self.assertEqual(offenders, [], f"gate dependency found: {offenders}")

    # ── corrective pass ──
    # Fix 1: ComponentScore.from_dict rejects unknown component name
    def test_component_score_from_dict_rejects_unknown_name(self):
        with self.assertRaises(ValueError):
            ComponentScore.from_dict({"component": "bogus", "score": 50})

    def test_component_score_from_dict_accepts_known_name(self):
        c = ComponentScore.from_dict({"component": "ml", "score": 50})
        self.assertEqual(c.component, "ml")

    # Fix 2: non-dict soft blocks -> invalid (25), not missing (50)
    def test_non_dict_technical_block_is_invalid(self):
        c = self._score("technical_confluence",
                        replace={"technical_context": "bad"})
        self.assertEqual(c.score, 25.0)
        self.assertIn("invalid_soft_input", c.warnings)

    def test_non_dict_volatility_block_is_invalid(self):
        c = self._score("volatility", replace={"volatility_context": "bad"})
        self.assertEqual(c.score, 25.0)
        self.assertIn("invalid_soft_input", c.warnings)

    def test_non_dict_regime_block_is_invalid(self):
        c = self._score("market_regime", replace={"regime_context": "bad"})
        self.assertEqual(c.score, 25.0)
        self.assertIn("invalid_soft_input", c.warnings)

    def test_non_dict_risk_block_is_invalid(self):
        c = self._score("risk_adjusted", replace={"risk_preview": "bad"})
        self.assertEqual(c.score, 25.0)
        self.assertIn("invalid_soft_input", c.warnings)

    def test_non_dict_data_quality_block_is_invalid(self):
        c = self._score("data_quality", replace={"data_quality_context": "bad"})
        self.assertEqual(c.score, 25.0)
        self.assertIn("invalid_soft_input", c.warnings)

    def test_non_dict_ml_block_calibration_uncertainty_invalid(self):
        c = self._score("calibration_uncertainty", replace={"ml_context": "bad"})
        self.assertEqual(c.score, 25.0)
        self.assertIn("invalid_soft_input", c.warnings)

    # Fix 3: partial missing/invalid soft keys must warn
    def test_volume_liquidity_missing_volume_ratio_warns(self):
        c = self._score("volume_liquidity", replace={
            "liquidity_context": {"avg_dollar_volume_20d": 60_000_000,
                                  "price": 150.0},
            "technical_context": {}})  # volume_ratio absent
        self.assertIn("missing_soft_input", c.warnings)

    def test_volume_liquidity_invalid_volume_ratio_warns(self):
        c = self._score("volume_liquidity", replace={
            "liquidity_context": {"avg_dollar_volume_20d": 60_000_000,
                                  "price": 150.0},
            "technical_context": {"volume_ratio": "x"}})
        self.assertIn("invalid_soft_input", c.warnings)

    def test_data_quality_partial_missing_warns(self):
        c = self._score("data_quality", replace={
            "data_quality_context": {"schema_match": True}})  # others missing
        self.assertIn("missing_soft_input", c.warnings)

    def test_data_quality_partial_invalid_warns(self):
        c = self._score("data_quality", replace={
            "data_quality_context": {"schema_match": True,
                                     "stale_data_flag": False,
                                     "data_freshness_minutes": 5,
                                     "missing_feature_count": "x"}})
        self.assertIn("invalid_soft_input", c.warnings)

    def test_calibration_uncertainty_partial_missing_warns(self):
        c = self._score("calibration_uncertainty", replace={
            "ml_context": {"calibration_applied": True}})  # others missing
        self.assertIn("missing_soft_input", c.warnings)

    # ── corrective pass (keys alignment + ml invalid/missing) ──
    def test_component_readable_keys_match_actual_reads(self):
        """COMPONENT_READABLE_KEYS must declare exactly the (block, key) pairs
        each scorer actually reads via _get(...). Guards against drift."""
        import ast
        from bot.signal_scoring import keys as K
        src = (_PKG_DIR / "components.py").read_text()
        tree = ast.parse(src)
        # resolve K.CONST -> value for translating attribute reads to strings
        kconsts = {n: getattr(K, n) for n in dir(K)
                   if n.isupper() and isinstance(getattr(K, n), str)}
        declared = {}
        for comp, pairs in K.COMPONENT_READABLE_KEYS.items():
            s = set()
            for block_name, keys in pairs:
                for key in keys:
                    s.add((block_name, key))
            declared[comp] = s
        actual = {}
        for node in tree.body:
            if not (isinstance(node, ast.FunctionDef)
                    and node.name.startswith("score_")
                    and node.name not in ("score_component",
                                          "score_all_components")):
                continue
            comp = node.name[len("score_"):]
            block_vars = {}
            reads = set()
            for n in ast.walk(node):
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) \
                   and isinstance(n.value.func, ast.Name) \
                   and n.value.func.id == "_block":
                    bn = n.value.args[1].value
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            block_vars[t.id] = bn
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                   and n.func.id == "_get":
                    blk = n.args[0]
                    if isinstance(blk, ast.Name):
                        bname = block_vars.get(blk.id)
                    elif isinstance(blk, ast.Call) \
                            and isinstance(blk.func, ast.Name) \
                            and blk.func.id == "_block":
                        bname = blk.args[1].value
                    else:
                        bname = None
                    key = n.args[1]
                    if isinstance(key, ast.Attribute):
                        kname = kconsts.get(key.attr)
                    elif isinstance(key, ast.Constant):
                        kname = key.value
                    else:
                        kname = None
                    if bname and kname:
                        reads.add((bname, kname))
            actual[comp] = reads
        for comp in K.COMPONENT_NAMES:
            self.assertEqual(
                declared.get(comp), actual.get(comp),
                f"{comp}: declared {declared.get(comp)} != reads "
                f"{actual.get(comp)}")

    def test_volume_liquidity_records_all_four_inputs(self):
        c = self._score("volume_liquidity")
        for k in ("avg_dollar_volume_20d", "price", "spread_pct",
                  "volume_ratio"):
            self.assertIn(k, c.inputs_used)

    def test_volume_liquidity_missing_spread_warns(self):
        c = self._score("volume_liquidity", replace={
            "liquidity_context": {"avg_dollar_volume_20d": 60_000_000,
                                  "price": 150.0},  # spread_pct missing
            "technical_context": {"volume_ratio": 1.6}})
        self.assertIn("missing_soft_input", c.warnings)

    def test_volume_liquidity_invalid_spread_warns(self):
        c = self._score("volume_liquidity", replace={
            "liquidity_context": {"avg_dollar_volume_20d": 60_000_000,
                                  "price": 150.0, "spread_pct": "x"},
            "technical_context": {"volume_ratio": 1.6}})
        self.assertIn("invalid_soft_input", c.warnings)

    def test_volume_liquidity_readable_keys_multiblock(self):
        from bot.signal_scoring import keys as K
        pairs = dict(K.COMPONENT_READABLE_KEYS["volume_liquidity"])
        self.assertIn("technical_context", pairs)
        self.assertIn("volume_ratio", pairs["technical_context"])

    def test_ml_invalid_raw_emits_invalid(self):
        c = self._score("ml", replace={"ml_context": {
            "calibration_applied": False, "prediction_raw": "bad"}})
        self.assertEqual(c.score, 25.0)
        self.assertIn("invalid_soft_input", c.warnings)

    def test_ml_invalid_calibrated_raw_missing_emits_invalid(self):
        c = self._score("ml", replace={"ml_context": {
            "calibration_applied": True, "prediction_calibrated": "bad"}})
        self.assertEqual(c.score, 25.0)
        self.assertIn("invalid_soft_input", c.warnings)

    def test_ml_both_missing_emits_missing_not_invalid(self):
        c = self._score("ml", replace={"ml_context": {
            "calibration_applied": False}})
        self.assertEqual(c.score, 25.0)
        self.assertIn("missing_soft_input", c.warnings)
        self.assertNotIn("invalid_soft_input", c.warnings)
        self.assertIn("ml_probability_unavailable", c.blocked_reasons)

    # invalid calibrated (when applied) must NOT fall back to raw
    def test_ml_invalid_calibrated_with_valid_raw_is_invalid(self):
        c = self._score("ml", replace={"ml_context": {
            "calibration_applied": True, "prediction_calibrated": "bad",
            "prediction_raw": 0.62}})
        self.assertEqual(c.score, 25.0)
        self.assertIn("invalid_soft_input", c.warnings)
        self.assertNotIn("raw_probability_used", c.warnings)

    def test_ml_none_calibrated_with_valid_raw_uses_raw(self):
        c = self._score("ml", replace={"ml_context": {
            "calibration_applied": True, "prediction_calibrated": None,
            "prediction_raw": 0.62}})
        self.assertAlmostEqual(c.score, 62.0, places=4)
        self.assertIn("raw_probability_used", c.warnings)
        self.assertNotIn("invalid_soft_input", c.warnings)

    def test_ml_applied_missing_uses_raw(self):
        c = self._score("ml", replace={"ml_context": {
            "prediction_raw": 0.62}})
        self.assertAlmostEqual(c.score, 62.0, places=4)
        self.assertIn("raw_probability_used", c.warnings)
        self.assertNotIn("invalid_soft_input", c.warnings)

    def test_ml_applied_invalid_string_is_invalid(self):
        c = self._score("ml", replace={"ml_context": {
            "calibration_applied": "yes", "prediction_raw": 0.62}})
        self.assertEqual(c.score, 25.0)
        self.assertIn("invalid_soft_input", c.warnings)


class M19DPenaltiesMultipliers(unittest.TestCase):
    """M19.D friction layer: penalties + multipliers. Matrices A-J + extras.
    A complete clean base is used; each row overrides one field so warning
    assertions are precise."""

    def _cfg(self, profile=None):
        return default_config(profile) if profile else default_config()

    def _clean_blocks(self):
        # all penalty triggers benign + all multiplier inputs benign/clean
        return dict(
            ml_context={"calibration_applied": True,
                        "feature_extrapolation_count": 0,
                        "production_thinness_status": "ok"},
            scanner_context={"valid_count": 4, "available_timeframes": 4},
            risk_preview={"reward_risk_ratio": 2.0},
            regime_context={"regime_label": "bull"},
            volatility_context={"atr_pct": 0.02},
            liquidity_context={"avg_dollar_volume_20d": 60_000_000},
            advisory_context={"fourh_bucket_alignment": "session_aligned"},
        )

    def _ci(self, side="LONG", *, replace=None):
        blocks = self._clean_blocks()
        if replace:
            for k, v in replace.items():
                blocks[k] = v
        return SignalCandidateInput(
            symbol="AAPL", side=side,
            signal_timestamp_utc="2026-06-17T10:15:00Z", **blocks)

    def _pen(self, *, replace=None, profile=None):
        return evaluate_penalties(self._ci(replace=replace), self._cfg(profile))

    def _mul(self, side="LONG", *, replace=None, profile=None):
        return evaluate_multipliers(self._ci(side=side, replace=replace),
                                    self._cfg(profile))

    def _pts(self, result, name):
        for i in result.items:
            if i.name == name:
                return i.points
        return None

    def _factor(self, result, name):
        for i in result.items:
            if i.name == name:
                return i.factor
        return None

    # clean baseline
    def test_clean_input_no_penalties_no_warnings(self):
        r = self._pen()
        self.assertEqual(r.items, [])
        self.assertEqual(r.total_points, 0)
        self.assertEqual(r.warnings, [])

    def test_clean_input_neutral_multipliers(self):
        r = self._mul()
        # bull/LONG -> aligned 1.0; session_aligned -> no item; clean otherwise
        self.assertEqual(r.effective_multiplier, 1.0)
        self.assertEqual(r.warnings, [])

    # ── Matrix A: each_feature_extrapolation ──
    def test_matrix_a_extrapolation(self):
        cases = {0: None, 1: 3.0, 5: 15.0, 7: 20, 100: 20}
        for count, expected in cases.items():
            ml = dict(self._clean_blocks()["ml_context"])
            ml["feature_extrapolation_count"] = count
            r = self._pen(replace={"ml_context": ml})
            self.assertEqual(self._pts(r, "each_feature_extrapolation"),
                             expected, f"count={count}")

    def test_matrix_a_extrapolation_missing(self):
        ml = {"calibration_applied": True, "production_thinness_status": "ok"}
        r = self._pen(replace={"ml_context": ml})
        self.assertIsNone(self._pts(r, "each_feature_extrapolation"))
        self.assertIn("missing_soft_input", r.warnings)

    def test_matrix_a_extrapolation_invalid_full_cap(self):
        for bad in ("x", -1, True):
            ml = dict(self._clean_blocks()["ml_context"])
            ml["feature_extrapolation_count"] = bad
            r = self._pen(replace={"ml_context": ml})
            self.assertEqual(self._pts(r, "each_feature_extrapolation"), 20,
                             f"bad={bad!r}")
            self.assertIn("invalid_soft_input", r.warnings)

    # ── Matrix B: uncalibrated_ml_probability ──
    def test_matrix_b_calibration(self):
        ml = dict(self._clean_blocks()["ml_context"]); ml["calibration_applied"] = True
        self.assertIsNone(self._pts(self._pen(replace={"ml_context": ml}),
                                    "uncalibrated_ml_probability"))
        ml2 = dict(ml); ml2["calibration_applied"] = False
        self.assertEqual(self._pts(self._pen(replace={"ml_context": ml2}),
                                   "uncalibrated_ml_probability"), 15)

    def test_matrix_b_calibration_missing_no_penalty(self):
        ml = {"feature_extrapolation_count": 0, "production_thinness_status": "ok"}
        r = self._pen(replace={"ml_context": ml})
        self.assertIsNone(self._pts(r, "uncalibrated_ml_probability"))
        self.assertIn("missing_soft_input", r.warnings)

    def test_matrix_b_calibration_invalid_penalty(self):
        ml = dict(self._clean_blocks()["ml_context"]); ml["calibration_applied"] = "yes"
        r = self._pen(replace={"ml_context": ml})
        self.assertEqual(self._pts(r, "uncalibrated_ml_probability"), 15)
        self.assertIn("invalid_soft_input", r.warnings)

    # ── Matrix C: production_thinness_warning ──
    def test_matrix_c_thinness(self):
        for status, expected in (("ok", None), ("warned", 8), ("blocked", None)):
            ml = dict(self._clean_blocks()["ml_context"])
            ml["production_thinness_status"] = status
            r = self._pen(replace={"ml_context": ml})
            self.assertEqual(self._pts(r, "production_thinness_warning"),
                             expected, f"status={status}")

    def test_matrix_c_thinness_missing(self):
        ml = {"calibration_applied": True, "feature_extrapolation_count": 0}
        r = self._pen(replace={"ml_context": ml})
        self.assertIsNone(self._pts(r, "production_thinness_warning"))
        self.assertIn("missing_soft_input", r.warnings)

    def test_matrix_c_thinness_invalid(self):
        ml = dict(self._clean_blocks()["ml_context"])
        ml["production_thinness_status"] = "mystery"
        r = self._pen(replace={"ml_context": ml})
        self.assertEqual(self._pts(r, "production_thinness_warning"), 8)
        self.assertIn("invalid_soft_input", r.warnings)

    # ── Matrix D: scanner ──
    def test_matrix_d_scanner(self):
        cases = {(4, 4): (None, None), (3, 4): (None, 5), (2, 4): (10, 10)}
        for (v, a), (weak, miss) in cases.items():
            r = self._pen(replace={"scanner_context": {
                "valid_count": v, "available_timeframes": a}})
            self.assertEqual(self._pts(r, "weak_scanner_confluence"), weak,
                             f"valid={v} avail={a}")
            self.assertEqual(self._pts(r, "missing_noncritical_timeframe"),
                             miss, f"valid={v} avail={a}")

    def test_matrix_d_scanner_missing(self):
        r = self._pen(replace={"scanner_context": {"available_timeframes": 4}})
        self.assertIsNone(self._pts(r, "weak_scanner_confluence"))
        self.assertIn("missing_soft_input", r.warnings)

    def test_matrix_d_valid_gt_available_is_invalid(self):
        r = self._pen(replace={"scanner_context": {
            "valid_count": 5, "available_timeframes": 4}})
        item = [i for i in r.items
                if i.name == "missing_noncritical_timeframe"][0]
        self.assertEqual(item.reason_code,
                         "missing_noncritical_timeframe_invalid")
        self.assertIn("invalid_soft_input", r.warnings)

    def test_matrix_d_valid_count_bool_invalid(self):
        r = self._pen(replace={"scanner_context": {
            "valid_count": True, "available_timeframes": 4}})
        self.assertEqual(self._pts(r, "weak_scanner_confluence"), 10)
        self.assertIn("invalid_soft_input", r.warnings)

    def test_matrix_d_available_bool_invalid(self):
        r = self._pen(replace={"scanner_context": {
            "valid_count": 4, "available_timeframes": True}})
        self.assertIn("invalid_soft_input", r.warnings)

    def test_matrix_d_available_list_form_supported(self):
        r = self._pen(replace={"scanner_context": {
            "valid_count": 4, "available_timeframes": ["1D", "4H", "1H", "15m"]}})
        # 4 valid of 4 available -> no penalties from list form
        self.assertIsNone(self._pts(r, "missing_noncritical_timeframe"))
        self.assertNotIn("invalid_soft_input", r.warnings)

    # ── Matrix E: poor_reward_risk ──
    def test_matrix_e_reward_risk(self):
        for rr, expected in ((2.0, None), (1.5, None), (1.49, 15)):
            r = self._pen(replace={"risk_preview": {"reward_risk_ratio": rr}})
            self.assertEqual(self._pts(r, "poor_reward_risk"), expected,
                             f"rr={rr}")

    def test_matrix_e_reward_risk_missing(self):
        r = self._pen(replace={"risk_preview": {}})
        self.assertIsNone(self._pts(r, "poor_reward_risk"))
        self.assertIn("missing_soft_input", r.warnings)

    def test_matrix_e_reward_risk_invalid(self):
        r = self._pen(replace={"risk_preview": {"reward_risk_ratio": "bad"}})
        self.assertEqual(self._pts(r, "poor_reward_risk"), 15)
        self.assertIn("invalid_soft_input", r.warnings)

    # ── Matrix F: regime multiplier (label x side) ──
    def test_matrix_f_regime(self):
        cases = {
            ("bull", "LONG"): (1.0, "regime_aligned"),
            ("bull", "SHORT"): (0.85, "regime_countertrend"),
            ("bear", "LONG"): (0.85, "regime_countertrend"),
            ("bear", "SHORT"): (1.0, "regime_aligned"),
            ("unknown", "LONG"): (0.95, "regime_unknown"),
            ("unknown", "SHORT"): (0.95, "regime_unknown"),
        }
        for (label, side), (factor, rcode) in cases.items():
            r = self._mul(side=side,
                          replace={"regime_context": {"regime_label": label}})
            item = [i for i in r.items if i.name == "regime"][0]
            self.assertEqual((item.factor, item.reason_code), (factor, rcode),
                             f"{label}/{side}")

    def test_matrix_f_regime_missing_neutral(self):
        r = self._mul(replace={"regime_context": {}})
        self.assertIsNone(self._factor(r, "regime"))
        self.assertIn("missing_soft_input", r.warnings)

    def test_matrix_f_regime_invalid_conservative(self):
        r = self._mul(replace={"regime_context": {"regime_label": 123}})
        self.assertEqual(self._factor(r, "regime"), 0.85)
        self.assertIn("invalid_soft_input", r.warnings)

    # ── Matrix G: volatility multiplier ──
    def test_matrix_g_volatility(self):
        cases = {0.02: (1.0, "volatility_normal"),
                 0.05: (0.92, "volatility_elevated"),
                 0.07: (0.92, "volatility_above_max")}
        for atr, (factor, rcode) in cases.items():
            r = self._mul(replace={"volatility_context": {"atr_pct": atr}})
            item = [i for i in r.items if i.name == "volatility"][0]
            self.assertEqual((item.factor, item.reason_code), (factor, rcode),
                             f"atr={atr}")

    def test_matrix_g_volatility_missing_neutral(self):
        r = self._mul(replace={"volatility_context": {}})
        self.assertIsNone(self._factor(r, "volatility"))
        self.assertIn("missing_soft_input", r.warnings)

    def test_matrix_g_volatility_invalid_conservative(self):
        for bad in (True, -0.1, "bad"):
            r = self._mul(replace={"volatility_context": {"atr_pct": bad}})
            self.assertEqual(self._factor(r, "volatility"), 0.92, f"bad={bad!r}")
            self.assertIn("invalid_soft_input", r.warnings)

    # ── Matrix H: liquidity multiplier ──
    def test_matrix_h_liquidity(self):
        cases = {60_000_000: (1.0, "liquidity_ideal"),
                 12_000_000: (0.9, "liquidity_thin_but_allowed"),
                 5_000_000: (0.9, "liquidity_below_min")}
        for adv20, (factor, rcode) in cases.items():
            r = self._mul(replace={"liquidity_context":
                                   {"avg_dollar_volume_20d": adv20}})
            item = [i for i in r.items if i.name == "liquidity"][0]
            self.assertEqual((item.factor, item.reason_code), (factor, rcode),
                             f"adv20={adv20}")

    def test_matrix_h_liquidity_missing_neutral(self):
        r = self._mul(replace={"liquidity_context": {}})
        self.assertIsNone(self._factor(r, "liquidity"))
        self.assertIn("missing_soft_input", r.warnings)

    def test_matrix_h_liquidity_invalid_conservative(self):
        for bad in (True, -5, "bad"):
            r = self._mul(replace={"liquidity_context":
                                   {"avg_dollar_volume_20d": bad}})
            self.assertEqual(self._factor(r, "liquidity"), 0.9, f"bad={bad!r}")
            self.assertIn("invalid_soft_input", r.warnings)

    # ── Matrix I: fourh_alignment multiplier ──
    def test_matrix_i_fourh(self):
        r = self._mul(replace={"advisory_context":
                               {"fourh_bucket_alignment": "utc_fixed"}})
        self.assertEqual(self._factor(r, "fourh_alignment"), 0.95)

    def test_matrix_i_fourh_other_string_neutral(self):
        r = self._mul(replace={"advisory_context":
                               {"fourh_bucket_alignment": "session_aligned"}})
        self.assertIsNone(self._factor(r, "fourh_alignment"))

    def test_matrix_i_fourh_missing_neutral(self):
        r = self._mul(replace={"advisory_context": {}})
        self.assertIsNone(self._factor(r, "fourh_alignment"))
        self.assertIn("missing_soft_input", r.warnings)

    def test_matrix_i_fourh_nonstring_invalid(self):
        r = self._mul(replace={"advisory_context":
                               {"fourh_bucket_alignment": 123}})
        self.assertEqual(self._factor(r, "fourh_alignment"), 0.95)
        self.assertIn("invalid_soft_input", r.warnings)

    # ── Matrix J: cap / floor ──
    def test_matrix_j_penalty_cap(self):
        r = self._pen(replace={
            "ml_context": {"calibration_applied": False,
                           "feature_extrapolation_count": 100,
                           "production_thinness_status": "warned"},
            "scanner_context": {"valid_count": 1, "available_timeframes": 4},
            "risk_preview": {"reward_risk_ratio": 1.0}})
        self.assertGreater(r.raw_total_points, 30)
        self.assertEqual(r.total_points, 30)

    def test_matrix_j_multiplier_floor(self):
        r = self._mul(side="LONG", replace={
            "regime_context": {"regime_label": "bear"},      # 0.85
            "volatility_context": {"atr_pct": 0.05},          # 0.92
            "liquidity_context": {"avg_dollar_volume_20d": 12_000_000},  # 0.90
            "advisory_context": {"fourh_bucket_alignment": "utc_fixed"}})  # 0.95
        self.assertLess(r.product, 0.70)
        self.assertEqual(r.effective_multiplier, 0.70)

    def test_multiplier_product_above_floor_passes_through(self):
        r = self._mul(replace={
            "regime_context": {"regime_label": "unknown"}})  # only 0.95
        self.assertAlmostEqual(r.product, 0.95, places=4)
        self.assertAlmostEqual(r.effective_multiplier, 0.95, places=4)

    # ── contract behaviour ──
    def test_penalty_result_roundtrip(self):
        r = self._pen(replace={"ml_context": {"calibration_applied": False,
                      "feature_extrapolation_count": 0,
                      "production_thinness_status": "ok"}})
        r2 = PenaltyResult.from_dict(r.to_dict())
        self.assertEqual(r.to_dict(), r2.to_dict())

    def test_multiplier_result_roundtrip(self):
        r = self._mul()
        r2 = MultiplierResult.from_dict(r.to_dict())
        self.assertEqual(r.to_dict(), r2.to_dict())

    def test_penalty_item_unknown_field_rejected(self):
        d = PenaltyItem(name="poor_reward_risk", points=15,
                        reason_code="x").to_dict()
        d["surprise"] = 1
        with self.assertRaises(ValueError):
            PenaltyItem.from_dict(d)

    def test_penalty_item_unknown_name_rejected(self):
        with self.assertRaises(ValueError):
            PenaltyItem.from_dict({"name": "bogus", "points": 5,
                                   "reason_code": "x"})

    def test_multiplier_item_unknown_field_rejected(self):
        d = MultiplierItem(name="regime", factor=1.0, reason_code="x").to_dict()
        d["surprise"] = 1
        with self.assertRaises(ValueError):
            MultiplierItem.from_dict(d)

    def test_multiplier_item_unknown_name_rejected(self):
        with self.assertRaises(ValueError):
            MultiplierItem.from_dict({"name": "bogus", "factor": 1.0,
                                      "reason_code": "x"})

    def test_penalty_negative_points_rejected(self):
        with self.assertRaises(ValueError):
            PenaltyItem(name="poor_reward_risk", points=-1, reason_code="x")

    def test_penalty_bool_points_rejected(self):
        with self.assertRaises(ValueError):
            PenaltyItem(name="poor_reward_risk", points=True, reason_code="x")

    def test_penalty_nonnumeric_points_rejected(self):
        with self.assertRaises(ValueError):
            PenaltyItem(name="poor_reward_risk", points="x", reason_code="x")

    def test_multiplier_factor_zero_rejected(self):
        with self.assertRaises(ValueError):
            MultiplierItem(name="regime", factor=0, reason_code="x")

    def test_multiplier_factor_negative_rejected(self):
        with self.assertRaises(ValueError):
            MultiplierItem(name="regime", factor=-1, reason_code="x")

    def test_multiplier_bool_factor_rejected(self):
        with self.assertRaises(ValueError):
            MultiplierItem(name="regime", factor=True, reason_code="x")

    def test_multiplier_nonnumeric_factor_rejected(self):
        with self.assertRaises(ValueError):
            MultiplierItem(name="regime", factor="x", reason_code="x")

    def test_deterministic(self):
        ci = self._ci(replace={"ml_context": {"calibration_applied": False,
                      "feature_extrapolation_count": 2,
                      "production_thinness_status": "warned"}})
        cfg = self._cfg()
        self.assertEqual(evaluate_penalties(ci, cfg).to_dict(),
                         evaluate_penalties(ci, cfg).to_dict())
        self.assertEqual(evaluate_multipliers(ci, cfg).to_dict(),
                         evaluate_multipliers(ci, cfg).to_dict())

    def test_reason_codes_sorted(self):
        r = self._pen(replace={
            "ml_context": {"calibration_applied": False,
                           "feature_extrapolation_count": 2,
                           "production_thinness_status": "warned"},
            "scanner_context": {"valid_count": 1, "available_timeframes": 4},
            "risk_preview": {"reward_risk_ratio": 1.0}})
        self.assertEqual(r.reason_codes, sorted(r.reason_codes))
        self.assertEqual(r.warnings, sorted(r.warnings))

    def test_profile_neutral(self):
        for replace in (None, {"ml_context": {"calibration_applied": False,
                                              "feature_extrapolation_count": 3,
                                              "production_thinness_status": "warned"}}):
            ci_s = self._ci(replace=replace)
            strict_p = evaluate_penalties(
                ci_s, default_config(ScoringProfile.STRICT)).to_dict()
            research_p = evaluate_penalties(
                ci_s, default_config(ScoringProfile.RESEARCH)).to_dict()
            strict_p["profile"] = research_p["profile"] = "X"
            self.assertEqual(strict_p, research_p)
            strict_m = evaluate_multipliers(
                ci_s, default_config(ScoringProfile.STRICT)).to_dict()
            research_m = evaluate_multipliers(
                ci_s, default_config(ScoringProfile.RESEARCH)).to_dict()
            strict_m["profile"] = research_m["profile"] = "X"
            self.assertEqual(strict_m, research_m)

    def test_non_dict_block_is_invalid(self):
        # non-dict ml_context -> uncalibrated + extrapolation + thinness all
        # read invalid -> adverse penalties + invalid warning, no crash
        r = evaluate_penalties(
            SignalCandidateInput(symbol="AAPL", side="LONG",
                signal_timestamp_utc="2026-06-17T10:15:00Z",
                ml_context="bad"), self._cfg())
        self.assertIn("invalid_soft_input", r.warnings)
        self.assertEqual(self._pts(r, "uncalibrated_ml_probability"), 15)

    def test_canonical_name_tuples(self):
        self.assertEqual(PENALTY_NAMES, (
            "uncalibrated_ml_probability", "each_feature_extrapolation",
            "production_thinness_warning", "missing_noncritical_timeframe",
            "weak_scanner_confluence", "poor_reward_risk"))
        self.assertEqual(MULTIPLIER_NAMES, (
            "regime", "volatility", "liquidity", "fourh_alignment"))

    # anti-drift: declared readable keys match actual reads
    def test_penalty_multiplier_readable_keys_match_reads(self):
        import ast
        from bot.signal_scoring import keys as K
        src = (_PKG_DIR / "penalties.py").read_text()
        tree = ast.parse(src)
        kconsts = {n: getattr(K, n) for n in dir(K)
                   if n.isupper() and isinstance(getattr(K, n), str)}
        # collect all (block, key) reads in the module, grouped by function
        func_reads = {}
        for node in tree.body:
            if not (isinstance(node, ast.FunctionDef)
                    and node.name.startswith("evaluate_")):
                continue
            block_vars = {}
            reads = set()
            for n in ast.walk(node):
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) \
                   and isinstance(n.value.func, ast.Name) \
                   and n.value.func.id == "_block":
                    bn = n.value.args[1].value
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            block_vars[t.id] = bn
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                   and n.func.id == "_get":
                    blk = n.args[0]
                    bname = block_vars.get(blk.id) if isinstance(blk, ast.Name) \
                        else None
                    key = n.args[1]
                    kname = kconsts.get(key.attr) if isinstance(key, ast.Attribute) \
                        else (key.value if isinstance(key, ast.Constant) else None)
                    if bname and kname:
                        reads.add((bname, kname))
            func_reads[node.name] = reads
        # union of declared penalty keys must be subset of evaluate_penalties reads
        declared_pen = set()
        for pairs in K.PENALTY_READABLE_KEYS.values():
            for b, keys in pairs:
                for k in keys:
                    declared_pen.add((b, k))
        declared_mul = set()
        for pairs in K.MULTIPLIER_READABLE_KEYS.values():
            for b, keys in pairs:
                for k in keys:
                    declared_mul.add((b, k))
        self.assertEqual(declared_pen, func_reads.get("evaluate_penalties"),
                         "penalty declared keys != actual reads")
        self.assertEqual(declared_mul, func_reads.get("evaluate_multipliers"),
                         "multiplier declared keys != actual reads")

    # no forbidden dependencies
    def test_penalties_no_forbidden_dependencies(self):
        import ast
        src = (_PKG_DIR / "penalties.py").read_text()
        tree = ast.parse(src)
        forbidden = {"evaluate_hard_gates", "score_all_components",
                     "score_component", "GateResult", "GateFailure",
                     "ComponentScore", "ScoredSignalCandidate"}
        offenders = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                fn = n.func
                if isinstance(fn, ast.Name) and fn.id in forbidden:
                    offenders.append(fn.id)
                elif isinstance(fn, ast.Attribute) and fn.attr in forbidden:
                    offenders.append(fn.attr)
            elif isinstance(n, ast.ImportFrom) and n.module:
                if n.module.endswith(("gates", "components")):
                    offenders.append(n.module)
            elif isinstance(n, ast.Name) and n.id in forbidden:
                offenders.append(n.id)
        # composite/score/bucket tokens must not appear as CODE (AST names/
        # attributes), not merely in docstrings/comments. ScoredSignalCandidate
        # is covered by the import/name/call walk above.
        code_forbidden = {"final_score", "pre_penalty_score",
                          "post_penalty_score", "decision_bucket",
                          "confidence_bucket", "execution_eligible"}
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and n.id in code_forbidden:
                offenders.append(n.id)
            elif isinstance(n, ast.Attribute) and n.attr in code_forbidden:
                offenders.append(n.attr)
        self.assertEqual(offenders, [], f"forbidden dependency: {offenders}")


class M19EComposite(unittest.TestCase):
    """M19.E composite score & bucket assembly. Matrices A-J + hard-BLOCK
    transparency, manual-review cap, short cap, config-threshold movement."""

    def _cfg(self, profile=None):
        return default_config(profile) if profile else default_config()

    def _comps(self, ml, support):
        d = {"ml": ComponentScore(component="ml", score=ml)}
        from bot.signal_scoring import COMPONENT_NAMES
        for c in COMPONENT_NAMES:
            if c != "ml":
                d[c] = ComponentScore(component=c, score=support)
        return d

    def _gate(self, passed=True, bucket=None, profile=ScoringProfile.STRICT,
              block=None, mr=None):
        return GateResult(profile=profile, passed=passed,
                          decision_bucket=bucket, block_reasons=block or [],
                          manual_review_reasons=mr or [])

    def _pen(self, total=0.0, raw=None, items=None):
        return PenaltyResult(profile=ScoringProfile.STRICT, items=items or [],
                             total_points=total,
                             raw_total_points=raw if raw is not None else total)

    def _mul(self, eff=1.0, prod=1.0, items=None):
        return MultiplierResult(profile=ScoringProfile.STRICT,
                                items=items or [], product=prod,
                                effective_multiplier=eff)

    def _ci(self, side="LONG"):
        return SignalCandidateInput(symbol="AAPL", side=side,
                                    signal_timestamp_utc="2026-06-17T10:15:00Z")

    def _assemble(self, *, ml=70, support=70, gate=None, pen=None, mul=None,
                  side="LONG", cfg=None):
        return assemble_score(
            gate or self._gate(), self._comps(ml, support),
            pen or self._pen(), mul or self._mul(), self._ci(side),
            cfg or self._cfg())

    # ── Matrix B: score -> decision bucket (gate PASS, LONG) ──
    def test_matrix_b_decision_buckets(self):
        cases = {0: "REJECT", 44.9: "REJECT", 45: "WATCH", 57.9: "WATCH",
                 58: "MANUAL_REVIEW", 64.9: "MANUAL_REVIEW", 65: "ELIGIBLE",
                 81.9: "ELIGIBLE", 82: "HIGH_CONVICTION", 100: "HIGH_CONVICTION"}
        for score, bucket in cases.items():
            sc = self._assemble(ml=score, support=score)
            self.assertEqual(sc.decision_bucket.value, bucket,
                             f"score={score}")

    # ── Matrix C: confidence bucket ──
    def test_matrix_c_confidence(self):
        cases = {44.9: "LOW", 45: "MEDIUM", 64.9: "MEDIUM", 65: "MEDIUM_HIGH",
                 81.9: "MEDIUM_HIGH", 82: "HIGH", 100: "HIGH"}
        for score, conf in cases.items():
            sc = self._assemble(ml=score, support=score)
            self.assertEqual(sc.confidence_bucket.value, conf, f"score={score}")

    # ── Matrix D: composite math numerical examples ──
    def test_matrix_d_composite_math(self):
        # base=0.55*anchor+0.45*support ; pre=base*mult ; final=clamp(pre-pen)
        cases = [(80, 60, 1.0, 0, 71.0), (80, 60, 0.85, 10, 50.35),
                 (90, 90, 1.0, 0, 90.0), (30, 30, 0.70, 30, 0.0),
                 (100, 100, 1.0, 0, 100.0)]
        for anchor, support, m, p, expected in cases:
            sc = self._assemble(ml=anchor, support=support,
                                mul=self._mul(eff=m), pen=self._pen(p))
            self.assertAlmostEqual(sc.final_score, expected, places=2,
                                   msg=f"a={anchor} s={support} m={m} p={p}")

    def test_support_renormalised_over_non_ml(self):
        # ML weight excluded: anchor varies but support depends only on the
        # 10 non-ML components. Two inputs with same support, different ml ->
        # base differs only via the anchor term.
        cfg = self._cfg()
        sc_a = self._assemble(ml=100, support=50, cfg=cfg)
        sc_b = self._assemble(ml=0, support=50, cfg=cfg)
        # base_a - base_b = ml_anchor_weight*(100-0) = 0.55*100 = 55
        self.assertAlmostEqual(sc_a.final_score - sc_b.final_score, 55.0,
                               places=4)

    def test_multiplier_applied_before_penalty(self):
        # base=100, mult=0.9, pen=10 -> (100*0.9)-10 = 80 (not (100-10)*0.9=81)
        sc = self._assemble(ml=100, support=100, mul=self._mul(eff=0.9),
                            pen=self._pen(10))
        self.assertAlmostEqual(sc.final_score, 80.0, places=4)

    # ── Matrix F: clamp ──
    def test_matrix_f_clamp_low(self):
        sc = self._assemble(ml=0, support=0, pen=self._pen(50))
        self.assertEqual(sc.final_score, 0.0)

    def test_matrix_f_clamp_high(self):
        sc = self._assemble(ml=100, support=100, mul=self._mul(eff=1.0))
        self.assertEqual(sc.final_score, 100.0)

    def test_final_score_equals_final_score_100(self):
        for score in (0, 33.3, 58, 82, 100):
            sc = self._assemble(ml=score, support=score)
            self.assertEqual(sc.final_score, sc.final_score_100)

    # ── Matrix A + hard-BLOCK transparency ──
    def test_matrix_a_block_overrides_score(self):
        sc = self._assemble(ml=100, support=100,
                            gate=self._gate(passed=False,
                                            bucket=DecisionBucket.BLOCKED,
                                            block=["min_liquidity"]))
        self.assertEqual(sc.decision_bucket, DecisionBucket.BLOCKED)
        self.assertEqual(sc.final_score, 0.0)
        self.assertEqual(sc.final_score_100, 0.0)
        self.assertFalse(sc.execution_eligible)
        # sub-results still embedded for explainability
        self.assertTrue(sc.component_scores)
        self.assertIn("effective_multiplier", sc.multipliers)
        self.assertIn("total_points", sc.penalties)

    def test_block_short_still_blocked(self):
        sc = self._assemble(ml=100, support=100, side="SHORT",
                            gate=self._gate(passed=False,
                                            bucket=DecisionBucket.BLOCKED,
                                            block=["short_side"],
                                            profile=ScoringProfile.STRICT))
        self.assertEqual(sc.decision_bucket, DecisionBucket.BLOCKED)
        self.assertEqual(sc.final_score, 0.0)
        self.assertFalse(sc.execution_eligible)

    # ── manual-review cap matrix ──
    def test_manual_review_caps_high_conviction(self):
        sc = self._assemble(ml=100, support=100,
                            gate=self._gate(passed=False,
                                            bucket=DecisionBucket.MANUAL_REVIEW,
                                            profile=ScoringProfile.RESEARCH,
                                            mr=["short_side"]))
        self.assertEqual(sc.decision_bucket, DecisionBucket.MANUAL_REVIEW)
        self.assertFalse(sc.execution_eligible)
        self.assertIn("would_be_high_conviction_capped_to_manual_review",
                      sc.reason_codes)
        self.assertIn("manual_review_gate_cap", sc.reason_codes)

    def test_manual_review_caps_eligible(self):
        sc = self._assemble(ml=70, support=70,
                            gate=self._gate(passed=False,
                                            bucket=DecisionBucket.MANUAL_REVIEW,
                                            profile=ScoringProfile.RESEARCH,
                                            mr=["x"]))
        self.assertEqual(sc.decision_bucket, DecisionBucket.MANUAL_REVIEW)
        self.assertIn("would_be_eligible_capped_to_manual_review",
                      sc.reason_codes)

    def test_manual_review_low_score_not_promoted(self):
        # MR gate but a WATCH-level score stays WATCH (cap only lowers)
        sc = self._assemble(ml=50, support=50,
                            gate=self._gate(passed=False,
                                            bucket=DecisionBucket.MANUAL_REVIEW,
                                            profile=ScoringProfile.RESEARCH,
                                            mr=["x"]))
        self.assertEqual(sc.decision_bucket, DecisionBucket.WATCH)

    # ── short cap matrix ──
    def test_short_cannot_be_high_conviction(self):
        sc = self._assemble(ml=100, support=100, side="SHORT",
                            gate=self._gate(passed=True))
        self.assertNotIn(sc.decision_bucket,
                         (DecisionBucket.ELIGIBLE,
                          DecisionBucket.HIGH_CONVICTION))
        self.assertFalse(sc.execution_eligible)
        self.assertIn("would_be_high_conviction_capped_for_short",
                      sc.reason_codes)
        self.assertIn("short_side_not_executable", sc.reason_codes)

    def test_short_cannot_be_eligible(self):
        sc = self._assemble(ml=70, support=70, side="SHORT",
                            gate=self._gate(passed=True))
        self.assertNotIn(sc.decision_bucket,
                         (DecisionBucket.ELIGIBLE,
                          DecisionBucket.HIGH_CONVICTION))
        self.assertFalse(sc.execution_eligible)
        self.assertIn("would_be_eligible_capped_for_short", sc.reason_codes)

    # ── execution eligibility ──
    def test_execution_eligible_long_happy_path(self):
        sc = self._assemble(ml=90, support=90, gate=self._gate(passed=True))
        self.assertTrue(sc.execution_eligible)
        self.assertEqual(sc.decision_bucket, DecisionBucket.HIGH_CONVICTION)

    def test_execution_eligible_false_when_watch(self):
        sc = self._assemble(ml=50, support=50, gate=self._gate(passed=True))
        self.assertFalse(sc.execution_eligible)

    def test_execution_eligible_false_when_gate_not_passed(self):
        sc = self._assemble(ml=90, support=90,
                            gate=self._gate(passed=False,
                                            bucket=DecisionBucket.MANUAL_REVIEW,
                                            profile=ScoringProfile.RESEARCH,
                                            mr=["x"]))
        self.assertFalse(sc.execution_eligible)

    # ── config-threshold movement (proves no magic numbers) ──
    def test_config_threshold_movement(self):
        base = default_config()
        th = dict(base.thresholds)
        th["eligible_min"] = 70
        moved = SignalScoringConfig(thresholds=th)
        sc_default = self._assemble(ml=67, support=67, cfg=base)
        sc_moved = self._assemble(ml=67, support=67, cfg=moved)
        self.assertEqual(sc_default.decision_bucket, DecisionBucket.ELIGIBLE)
        self.assertEqual(sc_moved.decision_bucket, DecisionBucket.MANUAL_REVIEW)

    # ── warning / reason-code union (deduped, sorted) ──
    def test_warning_reason_code_union(self):
        comps = self._comps(70, 70)
        comps["ml"] = ComponentScore(component="ml", score=70,
                                     warnings=["raw_probability_used"],
                                     reason_codes=["ml_eligible_band"])
        comps["scanner"] = ComponentScore(component="scanner", score=70,
                                          warnings=["missing_soft_input"])
        pen = PenaltyResult(profile=ScoringProfile.STRICT, items=[],
                            total_points=0, raw_total_points=0,
                            reason_codes=["poor_reward_risk"],
                            warnings=["invalid_soft_input"])
        mul = MultiplierResult(profile=ScoringProfile.STRICT, items=[],
                               product=1.0, effective_multiplier=1.0,
                               reason_codes=["regime_aligned"],
                               warnings=["missing_soft_input"])
        sc = assemble_score(self._gate(passed=True), comps, pen, mul,
                            self._ci(), self._cfg())
        # deduped + sorted
        self.assertEqual(sc.warnings, sorted(set(sc.warnings)))
        self.assertEqual(sc.reason_codes, sorted(set(sc.reason_codes)))
        self.assertIn("raw_probability_used", sc.warnings)
        self.assertIn("invalid_soft_input", sc.warnings)
        # missing_soft_input came from two sources -> appears once
        self.assertEqual(sc.warnings.count("missing_soft_input"), 1)
        self.assertIn("poor_reward_risk", sc.reason_codes)
        self.assertIn("regime_aligned", sc.reason_codes)

    # ── ScoredSignalCandidate contract ──
    def test_scored_candidate_roundtrip(self):
        sc = self._assemble(ml=70, support=70)
        sc2 = ScoredSignalCandidate.from_dict(sc.to_dict())
        self.assertEqual(sc.to_dict(), sc2.to_dict())

    def test_scored_candidate_unknown_field_rejected(self):
        d = self._assemble(ml=70, support=70).to_dict()
        d["surprise"] = 1
        with self.assertRaises(ValueError):
            ScoredSignalCandidate.from_dict(d)

    # ── deterministic provenance ──
    def test_deterministic_provenance(self):
        ci = SignalCandidateInput(
            symbol="AAPL", side="LONG",
            signal_timestamp_utc="2026-06-17T10:15:00Z",
            ml_context={"calibration_applied": True,
                        "prediction_calibrated": 0.7,
                        "model_readiness_passed": True,
                        "production_thinness_status": "ok",
                        "price_adjustment_mode": "raw"},
            data_quality_context={"schema_match": True, "stale_data_flag": False,
                                  "data_freshness_minutes": 5,
                                  "missing_feature_count": 0},
            advisory_context={"adjusted_price_pit_risk": False},
            timeframe_context={"available_timeframes": 4, "valid_timeframes": 4},
            risk_preview={"risk_preview_available": True,
                          "risk_authority_status": "ok",
                          "reward_risk_ratio": 2.0},
            liquidity_context={"avg_dollar_volume_20d": 60_000_000,
                               "price": 150.0})
        cfg = self._cfg()
        a = score_candidate(ci, cfg)
        b = score_candidate(ci, cfg)
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual(a.candidate_id, b.candidate_id)

    def test_provenance_differs_for_different_input(self):
        cfg = self._cfg()
        a = score_candidate(self._ci("LONG"), cfg)
        b = score_candidate(self._ci("SHORT"), cfg)
        self.assertNotEqual(a.candidate_id, b.candidate_id)

    def test_score_candidate_end_to_end_no_crash(self):
        sc = score_candidate(self._ci("LONG"), self._cfg())
        self.assertIsInstance(sc, ScoredSignalCandidate)

    # ── AST guards ──
    def test_composite_calls_existing_evaluators(self):
        import ast
        src = (_PKG_DIR / "composite.py").read_text()
        tree = ast.parse(src)
        called = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                called.add(n.func.id)
        for fn in ("evaluate_hard_gates", "score_all_components",
                   "evaluate_penalties", "evaluate_multipliers",
                   "assemble_score"):
            self.assertIn(fn, called, f"composite must call {fn}")

    def test_composite_no_forbidden_dependencies(self):
        import ast
        src = (_PKG_DIR / "composite.py").read_text()
        tree = ast.parse(src)
        offenders = []
        forbidden_mod = ("broker", "brokers", "live", "dashboard", "main",
                         "socket", "requests", "urllib", "aiohttp", "sqlite3",
                         "yfinance")
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if any(a.name == m or a.name.startswith(m + ".")
                           for m in forbidden_mod):
                        offenders.append(a.name)
            elif isinstance(n, ast.ImportFrom) and n.module:
                if any(n.module == m or n.module.startswith(m + ".")
                       or n.module.endswith("." + m)
                       for m in forbidden_mod):
                    offenders.append(n.module)
            elif isinstance(n, ast.Call):
                fn = n.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else fn.id if isinstance(fn, ast.Name) else "")
                if name in ("open", "write", "connect", "fetch", "get",
                            "post", "execute", "executemany"):
                    offenders.append(f"call:{name}")
        self.assertEqual(offenders, [], f"forbidden dependency: {offenders}")

    def test_no_files_created_on_import(self):
        import importlib
        import bot.signal_scoring.composite as comp
        importlib.reload(comp)
        self.assertEqual(
            len([p for p in (_REPO_ROOT / "data" / "m19").glob("*")]
                if (_REPO_ROOT / "data" / "m19").exists() else []), 0)


class M19FAdapters(unittest.TestCase):
    """M19.F pure adapters: upstream -> SignalCandidateInput. Matrices A-J +
    side-channel kwargs, malformed pass-through, raw-never-copied, SHORT,
    non-mutation, round-trip, no I/O."""

    def _scan(self, **over):
        sig = {"symbol": "AAPL", "direction": "long",
               "timestamp": "2026-06-17T10:15:00Z"}
        sig.update(over)
        return sig

    # ── Matrix A: side mapping ──
    def test_matrix_a_side_mapping(self):
        for d, expected in (("long", "LONG"), ("buy", "LONG"),
                            ("short", "SHORT"), ("sell", "SHORT"),
                            ("LONG", "LONG"), ("Short", "SHORT")):
            ci = adapter_from_scanner_signal(self._scan(direction=d))
            self.assertEqual(ci.side.value, expected, f"direction={d}")

    def test_matrix_a_side_invalid(self):
        for bad in ("", None, "flat", 123):
            with self.assertRaises(ValueError):
                adapter_from_scanner_signal(self._scan(direction=bad))

    # ── Matrix B: timestamp normalization ──
    def test_matrix_b_timestamp_accepted(self):
        from datetime import datetime, timezone, timedelta
        aware = datetime(2026, 6, 17, 10, 15, tzinfo=timezone.utc)
        for ts in (aware, "2026-06-17T10:15:00Z", "2026-06-17T10:15:00+00:00"):
            ci = adapter_from_scanner_signal(self._scan(timestamp=ts))
            self.assertTrue(ci.signal_timestamp_utc.endswith("+00:00"))

    def test_matrix_b_aware_nonutc_datetime_converted(self):
        # aware datetime object (per Q7) -> converted to UTC
        from datetime import datetime, timezone, timedelta
        dt = datetime(2026, 6, 17, 10, 15, tzinfo=timezone(timedelta(hours=2)))
        ci = adapter_from_scanner_signal(self._scan(timestamp=dt))
        self.assertEqual(ci.signal_timestamp_utc, "2026-06-17T08:15:00+00:00")

    def test_matrix_b_timestamp_rejected(self):
        from datetime import datetime
        naive = datetime(2026, 6, 17, 10, 15)
        for ts in (naive, "2026-06-17T10:15:00", "2026-06-17T10:15:00+02:00",
                   "not-a-date", None, 123):
            with self.assertRaises(ValueError):
                adapter_from_scanner_signal(self._scan(timestamp=ts))

    # ── Matrix C: required field rejection ──
    def test_matrix_c_required_fields(self):
        for sig in ({"direction": "long", "timestamp": "2026-06-17T10:15:00Z"},
                    {"symbol": "", "direction": "long",
                     "timestamp": "2026-06-17T10:15:00Z"},
                    {"symbol": "A", "timestamp": "2026-06-17T10:15:00Z"},
                    {"symbol": "A", "direction": "long"}):
            with self.assertRaises(ValueError):
                adapter_from_scanner_signal(sig)

    # ── Matrix D: scanner indicator mapping ──
    def test_matrix_d_scanner_indicators(self):
        ci = adapter_from_scanner_signal(self._scan(
            rsi=60, macd_hist=0.4, bb_pos=0.5, vwap_dev=0.1, vol_ratio=1.6))
        tc = ci.technical_context
        self.assertEqual(tc["rsi"], 60)
        self.assertEqual(tc["macd_hist"], 0.4)
        self.assertEqual(tc["bb_pos"], 0.5)
        self.assertEqual(tc["vwap_dev"], 0.1)
        self.assertEqual(tc["volume_ratio"], 1.6)

    def test_matrix_d_missing_indicator_omitted(self):
        ci = adapter_from_scanner_signal(self._scan(macd_hist=0.4))
        self.assertNotIn("rsi", ci.technical_context)

    def test_matrix_d_malformed_indicator_passthrough(self):
        ci = adapter_from_scanner_signal(self._scan(rsi="x"))
        self.assertEqual(ci.technical_context["rsi"], "x")

    # ── Matrix E: risk preview derivation ──
    def test_matrix_e_reward_risk(self):
        ci = adapter_from_scanner_signal(self._scan(
            direction="long", entry_price=100, stop_loss=98, target_price=104))
        self.assertAlmostEqual(ci.risk_preview["reward_risk_ratio"], 2.0)
        self.assertTrue(ci.risk_preview["risk_preview_available"])

    def test_matrix_e_reward_risk_short(self):
        ci = adapter_from_scanner_signal(self._scan(
            direction="short", entry_price=100, stop_loss=102, target_price=94))
        self.assertAlmostEqual(ci.risk_preview["reward_risk_ratio"], 3.0)
        self.assertTrue(ci.risk_preview["risk_preview_available"])

    def test_matrix_e_missing_stop_unavailable(self):
        ci = adapter_from_scanner_signal(self._scan(
            entry_price=100, target_price=104))
        self.assertNotIn("reward_risk_ratio", ci.risk_preview)
        self.assertFalse(ci.risk_preview["risk_preview_available"])

    def test_matrix_e_div_by_zero_no_crash(self):
        ci = adapter_from_scanner_signal(self._scan(
            entry_price=100, stop_loss=100, target_price=104))
        self.assertNotIn("reward_risk_ratio", ci.risk_preview)
        self.assertFalse(ci.risk_preview["risk_preview_available"])

    def test_matrix_e_invalid_price_no_crash(self):
        ci = adapter_from_scanner_signal(self._scan(
            entry_price="bad", stop_loss=98, target_price=104))
        self.assertNotIn("reward_risk_ratio", ci.risk_preview)

    # ── Matrix F: atr_pct derivation (asserts downstream invalid via the
    #     real volatility component + multiplier, not just key presence) ──
    def _vol_invalid_fires(self, ci):
        from bot.signal_scoring import score_component, evaluate_multipliers
        comp = score_component("volatility", ci, default_config())
        mult = evaluate_multipliers(ci, default_config())
        return ("invalid_soft_input" in comp.warnings
                and "invalid_soft_input" in mult.warnings)

    def test_matrix_f_atr_pct_derived(self):
        ci = adapter_from_scanner_signal(self._scan(atr=2, entry_price=100))
        self.assertAlmostEqual(ci.volatility_context["atr_pct"], 0.02)
        self.assertFalse(self._vol_invalid_fires(ci))

    def test_matrix_f_atr_no_entry_omitted(self):
        ci = adapter_from_scanner_signal(self._scan(atr=2))
        self.assertNotIn("atr_pct", ci.volatility_context)
        self.assertFalse(self._vol_invalid_fires(ci))

    def test_matrix_f_no_atr_omitted(self):
        ci = adapter_from_scanner_signal(self._scan(entry_price=100))
        self.assertNotIn("atr_pct", ci.volatility_context)

    def test_matrix_f_atr_malformed_triggers_downstream_invalid(self):
        ci = adapter_from_scanner_signal(self._scan(atr="bad", entry_price=100))
        self.assertIn("atr_pct", ci.volatility_context)
        self.assertTrue(self._vol_invalid_fires(ci))

    def test_matrix_f_entry_malformed_triggers_downstream_invalid(self):
        ci = adapter_from_scanner_signal(self._scan(atr=2, entry_price="bad"))
        self.assertIn("atr_pct", ci.volatility_context)
        self.assertTrue(self._vol_invalid_fires(ci))

    def test_matrix_f_entry_zero_triggers_downstream_invalid(self):
        ci = adapter_from_scanner_signal(self._scan(atr=2, entry_price=0))
        self.assertIn("atr_pct", ci.volatility_context)
        self.assertTrue(self._vol_invalid_fires(ci))

    def test_matrix_f_entry_negative_triggers_downstream_invalid(self):
        ci = adapter_from_scanner_signal(self._scan(atr=2, entry_price=-5))
        self.assertIn("atr_pct", ci.volatility_context)
        self.assertTrue(self._vol_invalid_fires(ci))

    # ── Matrix G: ML merge ──
    def test_matrix_g_ml_merge_full(self):
        base = adapter_from_scanner_signal(self._scan())
        m = merge_ml_prediction(base, {
            "model_id": "m1", "prediction_raw": 0.6,
            "prediction_calibrated": 0.68,
            "prediction_calibration_applied": True})
        self.assertEqual(m.ml_context["prediction_raw"], 0.6)
        self.assertEqual(m.ml_context["prediction_calibrated"], 0.68)
        self.assertIs(m.ml_context["calibration_applied"], True)
        self.assertEqual(m.ml_context["model_id"], "m1")

    def test_matrix_g_raw_only_calibrated_absent(self):
        base = adapter_from_scanner_signal(self._scan())
        m = merge_ml_prediction(base, {"prediction_raw": 0.6,
                                       "prediction_calibration_applied": False})
        self.assertNotIn("prediction_calibrated", m.ml_context)
        self.assertEqual(m.ml_context["prediction_raw"], 0.6)

    def test_raw_never_copied_into_calibrated(self):
        base = adapter_from_scanner_signal(self._scan())
        m = merge_ml_prediction(base, {"prediction_raw": 0.6})
        self.assertNotIn("prediction_calibrated", m.ml_context)

    def test_calibration_applied_from_predict_time_field(self):
        base = adapter_from_scanner_signal(self._scan())
        m = merge_ml_prediction(base, {
            "prediction_raw": 0.6,
            "predict_time_calibration_applied": True,
            "prediction_calibrated": 0.7})
        self.assertIs(m.ml_context["calibration_applied"], True)

    # ── Matrix H: readiness merge ──
    def test_matrix_h_readiness_merge(self):
        base = adapter_from_scanner_signal(self._scan())
        r = merge_readiness_advisories(base, {
            "fourh_bucket_alignment": "utc_fixed",
            "adjusted_price_pit_risk": False,
            "scanner_replica_short_side_validated": False,
            "price_adjustment_mode": "raw"})
        self.assertEqual(r.advisory_context["fourh_bucket_alignment"],
                         "utc_fixed")
        self.assertIs(r.advisory_context["adjusted_price_pit_risk"], False)
        self.assertIs(
            r.advisory_context["scanner_replica_short_side_validated"], False)
        self.assertEqual(r.ml_context["price_adjustment_mode"], "raw")

    def test_matrix_h_readiness_absent_omitted(self):
        base = adapter_from_scanner_signal(self._scan())
        r = merge_readiness_advisories(base, {})
        self.assertEqual(r.advisory_context, {})

    # ── Matrix I: snapshot adapter ──
    def test_matrix_i_snapshot_full(self):
        snap = {"symbol": "AAPL", "direction": "long",
                "timestamp": "2026-06-17T10:15:00Z", "valid_count": 3,
                "available_tfs": 4, "min_valid": 3,
                "tfs_passing": "15m+1H+4H", "rsi": 55, "macd_hist": 0.2,
                "atr": 2.0}
        ci = adapter_from_candidate_snapshot(snap)
        self.assertEqual(ci.scanner_context["valid_count"], 3)
        self.assertEqual(ci.scanner_context["available_timeframes"], 4)
        self.assertEqual(ci.scanner_context["valid_timeframes"],
                         ["15m", "1H", "4H"])
        self.assertEqual(ci.technical_context["rsi"], 55)
        self.assertEqual(ci.timeframe_context["valid_timeframes"],
                         ["15m", "1H", "4H"])

    def test_matrix_i_snapshot_required_fields(self):
        with self.assertRaises(ValueError):
            adapter_from_candidate_snapshot({"direction": "long",
                "timestamp": "2026-06-17T10:15:00Z"})

    # ── Matrix J: purity / determinism / non-mutation ──
    def test_matrix_j_deterministic(self):
        sig = self._scan(rsi=60, entry_price=100, stop_loss=98,
                         target_price=104)
        a = adapter_from_scanner_signal(sig)
        b = adapter_from_scanner_signal(sig)
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_merge_does_not_mutate_original(self):
        base = adapter_from_scanner_signal(self._scan())
        base_before = base.to_dict()
        merge_ml_prediction(base, {"prediction_raw": 0.6})
        merge_readiness_advisories(base, {"fourh_bucket_alignment": "utc_fixed"})
        self.assertEqual(base.to_dict(), base_before)
        self.assertEqual(base.ml_context, {})
        self.assertEqual(base.advisory_context, {})

    def test_adapter_output_roundtrips(self):
        ci = adapter_from_scanner_signal(self._scan(
            rsi=60, entry_price=100, stop_loss=98, target_price=104))
        ci2 = SignalCandidateInput.from_dict(ci.to_dict())
        self.assertEqual(ci.to_dict(), ci2.to_dict())

    # ── optional side-channel kwargs ──
    def test_side_channel_kwargs(self):
        ci = adapter_from_scanner_signal(
            self._scan(),
            liquidity={"avg_dollar_volume_20d": 60_000_000, "spread_pct": 0.05},
            regime={"regime_label": "bull"},
            data_quality={"schema_match": True})
        self.assertEqual(ci.liquidity_context["avg_dollar_volume_20d"],
                         60_000_000)
        self.assertEqual(ci.regime_context["regime_label"], "bull")
        self.assertIs(ci.data_quality_context["schema_match"], True)

    def test_side_channel_absent_blocks_empty(self):
        ci = adapter_from_scanner_signal(self._scan())
        self.assertEqual(ci.regime_context, {})
        self.assertEqual(ci.data_quality_context, {})

    # ── SHORT faithfully built ──
    def test_short_built_faithfully(self):
        ci = adapter_from_scanner_signal(self._scan(direction="short"))
        self.assertEqual(ci.side, SignalSide.SHORT)

    # ── end-to-end into scoring (adapter output is valid for score_candidate) ──
    def test_adapter_output_scores_without_crash(self):
        ci = adapter_from_scanner_signal(self._scan(
            rsi=60, entry_price=100, stop_loss=98, target_price=104))
        sc = score_candidate(ci, default_config())
        self.assertIsInstance(sc, ScoredSignalCandidate)

    def test_malformed_passthrough_triggers_downstream_invalid(self):
        # rsi malformed -> passed through -> momentum/technical see invalid
        base = adapter_from_scanner_signal(self._scan(rsi="x", macd_hist="y"))
        from bot.signal_scoring import score_component
        c = score_component("momentum", base, default_config())
        self.assertIn("invalid_soft_input", c.warnings)

    # ── AST guard: no I/O / no forbidden deps ──
    def test_adapters_no_forbidden_dependencies(self):
        import ast
        src = (_PKG_DIR / "adapters.py").read_text()
        tree = ast.parse(src)
        offenders = []
        forbidden_mod = ("broker", "brokers", "live", "dashboard", "main",
                         "socket", "requests", "urllib", "aiohttp", "sqlite3",
                         "yfinance", "bot.scanner", "bot.strategy", "bot.risk",
                         "bot.flywheel")
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if any(a.name == m or a.name.startswith(m + ".")
                           for m in forbidden_mod):
                        offenders.append(a.name)
            elif isinstance(n, ast.ImportFrom) and n.module:
                if any(n.module == m or n.module.startswith(m + ".")
                       for m in forbidden_mod):
                    offenders.append(n.module)
            elif isinstance(n, ast.Call):
                fn = n.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else fn.id if isinstance(fn, ast.Name) else "")
                if name in ("open", "connect", "execute", "executemany",
                            "fetch", "post", "commit", "write"):
                    offenders.append(f"call:{name}")
        self.assertEqual(offenders, [], f"forbidden dependency: {offenders}")

    def test_no_files_created_on_import(self):
        import importlib
        import bot.signal_scoring.adapters as ad
        importlib.reload(ad)
        m19 = _REPO_ROOT / "data" / "m19"
        self.assertEqual(
            len(list(m19.glob("*"))) if m19.exists() else 0, 0)


class M19GOutputAudit(unittest.TestCase):
    """M19.G optional JSONL output + pure audit. Matrices A-G, proven through
    the REAL writer and audit functions (not just predicates)."""

    def _mk(self, side="LONG", ml=70, support=70, gate_bucket=None,
            passed=True, block=None):
        from bot.signal_scoring import COMPONENT_NAMES
        comps = {"ml": ComponentScore(component="ml", score=ml)}
        for c in COMPONENT_NAMES:
            if c != "ml":
                comps[c] = ComponentScore(component=c, score=support)
        g = GateResult(profile=ScoringProfile.STRICT, passed=passed,
                       decision_bucket=gate_bucket, block_reasons=block or [])
        p = PenaltyResult(profile=ScoringProfile.STRICT, items=[],
                          total_points=0, raw_total_points=0)
        m = MultiplierResult(profile=ScoringProfile.STRICT, items=[],
                             product=1.0, effective_multiplier=1.0)
        ci = SignalCandidateInput(symbol="AAPL", side=side,
                                  signal_timestamp_utc="2026-06-17T10:15:00Z")
        return assemble_score(g, comps, p, m, ci, default_config())

    # ── Matrix A: path safety (asserted via the real writer, proving no file
    #    is created on rejection) ──
    def test_matrix_a_tempdir_ok(self):
        with tempfile.TemporaryDirectory() as td:
            ok, _ = is_write_safe_path(os.path.join(td, "out.jsonl"))
            self.assertTrue(ok)

    def test_matrix_a_rejections_no_file_created(self):
        bad_paths = [
            _REPO_ROOT / "signals.db",
            _REPO_ROOT / "data" / "ml" / "x.jsonl",
            _REPO_ROOT / "data" / "m19" / "x.jsonl",
            _REPO_ROOT / "bot" / "x.jsonl",
            _REPO_ROOT / "configs" / "x.jsonl",
            _REPO_ROOT / "docs" / "x.jsonl",
            _REPO_ROOT / "x.jsonl",
        ]
        for p in bad_paths:
            ok, _ = is_write_safe_path(p)
            self.assertFalse(ok, f"should reject {p}")
            existed = p.exists()
            with self.assertRaises(ValueError):
                write_scored_candidates_jsonl([self._mk()], p)
            # writer must not have created the file
            self.assertEqual(p.exists(), existed, f"writer created {p}")

    def test_matrix_a_missing_parent_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "nope", "x.jsonl")
            ok, _ = is_write_safe_path(p)
            self.assertFalse(ok)
            with self.assertRaises(ValueError):
                write_scored_candidates_jsonl([self._mk()], p)

    # ── corrective: forbidden names/segments UNDER tempdir must still reject
    #    (parents pre-created so rejection is proven by name/segment, not by a
    #    missing parent) ──
    def test_temp_signals_db_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "signals.db")
            ok, reason = is_write_safe_path(p)
            self.assertFalse(ok)
            self.assertIn("signals.db", reason)
            with self.assertRaises(ValueError):
                write_scored_candidates_jsonl([self._mk()], p)
            self.assertFalse(os.path.exists(p))

    def test_temp_data_ml_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            parent = os.path.join(td, "data", "ml")
            os.makedirs(parent)  # parent exists -> rejection must be by segment
            p = os.path.join(parent, "x.jsonl")
            ok, reason = is_write_safe_path(p)
            self.assertFalse(ok)
            self.assertIn("data/ml", reason)
            with self.assertRaises(ValueError):
                write_scored_candidates_jsonl([self._mk()], p)
            self.assertFalse(os.path.exists(p))

    def test_temp_data_m19_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            parent = os.path.join(td, "data", "m19")
            os.makedirs(parent)  # parent exists -> rejection must be by segment
            p = os.path.join(parent, "x.jsonl")
            ok, reason = is_write_safe_path(p)
            self.assertFalse(ok)
            self.assertIn("data/m19", reason)
            with self.assertRaises(ValueError):
                write_scored_candidates_jsonl([self._mk()], p)
            self.assertFalse(os.path.exists(p))

    def test_temp_normal_file_still_allowed(self):
        # the temp-only rule is NOT weakened: a normal temp file is allowed
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "out.jsonl")
            ok, _ = is_write_safe_path(p)
            self.assertTrue(ok)
            self.assertEqual(write_scored_candidates_jsonl([self._mk()], p), 1)

    # ── Matrix B: write semantics ──
    def test_matrix_b_three_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "o.jsonl")
            n = write_scored_candidates_jsonl(
                [self._mk(), self._mk(), self._mk()], out)
            self.assertEqual(n, 3)
            with open(out, encoding="utf-8") as fh:
                self.assertEqual(sum(1 for _ in fh), 3)

    def test_matrix_b_existing_no_allow_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "o.jsonl")
            write_scored_candidates_jsonl([self._mk()], out)
            with open(out, "rb") as fh:
                original = fh.read()
            with self.assertRaises(ValueError):
                write_scored_candidates_jsonl([self._mk(), self._mk()], out)
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), original)

    def test_matrix_b_existing_allow_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "o.jsonl")
            write_scored_candidates_jsonl([self._mk(), self._mk()], out)
            n = write_scored_candidates_jsonl([self._mk()], out,
                                              allow_existing=True)
            self.assertEqual(n, 1)
            with open(out, encoding="utf-8") as fh:
                self.assertEqual(sum(1 for _ in fh), 1)

    def test_matrix_b_empty_iterable(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "empty.jsonl")
            n = write_scored_candidates_jsonl([], out)
            self.assertEqual(n, 0)
            self.assertTrue(os.path.exists(out))
            self.assertEqual(os.path.getsize(out), 0)

    def test_matrix_b_missing_path_arg_typeerror(self):
        with self.assertRaises(TypeError):
            write_scored_candidates_jsonl([self._mk()])  # noqa

    # ── Matrix C: JSONL line shape ──
    def test_matrix_c_long_roundtrip(self):
        c = self._mk(side="LONG")
        line = scored_candidate_to_jsonl_line(c)
        self.assertNotIn("\n", line)
        self.assertEqual(json.loads(line), c.to_dict())

    def test_matrix_c_blocked_preserved(self):
        c = self._mk(gate_bucket=DecisionBucket.BLOCKED, passed=False,
                     block=["x"])
        self.assertEqual(json.loads(scored_candidate_to_jsonl_line(c))
                         ["decision_bucket"], "BLOCKED")

    def test_matrix_c_short_preserved(self):
        c = self._mk(side="SHORT")
        self.assertEqual(json.loads(scored_candidate_to_jsonl_line(c))
                         ["side"], "SHORT")

    def test_matrix_c_one_newline_per_record(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "o.jsonl")
            write_scored_candidates_jsonl([self._mk(), self._mk()], out)
            with open(out, encoding="utf-8") as fh:
                content = fh.read()
            self.assertEqual(content.count("\n"), 2)
            for line in content.splitlines():
                self.assertNotIn("\n", line)

    # ── Matrix D: determinism ──
    def test_matrix_d_same_candidate_identical_line(self):
        c = self._mk()
        self.assertEqual(scored_candidate_to_jsonl_line(c),
                         scored_candidate_to_jsonl_line(c))

    def test_matrix_d_same_list_identical_file(self):
        c = self._mk()
        with tempfile.TemporaryDirectory() as td:
            a = os.path.join(td, "a.jsonl")
            b = os.path.join(td, "b.jsonl")
            write_scored_candidates_jsonl([c, c], a)
            write_scored_candidates_jsonl([c, c], b)
            with open(a, "rb") as fa, open(b, "rb") as fb:
                self.assertEqual(fa.read(), fb.read())

    def test_matrix_d_warnings_sorted(self):
        line = scored_candidate_to_jsonl_line(self._mk())
        parsed = json.loads(line)
        self.assertEqual(parsed["reason_codes"], sorted(parsed["reason_codes"]))
        self.assertEqual(parsed["warnings"], sorted(parsed["warnings"]))

    # ── Matrix E: audit record ──
    def test_matrix_e_record_field_set(self):
        r = build_scoring_audit_record(self._mk())
        self.assertEqual(set(r.keys()), {
            "schema_version", "candidate_id", "symbol", "side", "profile",
            "decision_bucket", "confidence_bucket", "execution_eligible",
            "final_score", "hard_gate_passed", "block_reasons", "reason_codes",
            "warnings", "config_hash", "input_digest"})

    def test_matrix_e_eligible(self):
        r = build_scoring_audit_record(self._mk(ml=90, support=90))
        self.assertTrue(r["execution_eligible"])
        self.assertEqual(r["decision_bucket"], "HIGH_CONVICTION")

    def test_matrix_e_blocked(self):
        r = build_scoring_audit_record(self._mk(
            gate_bucket=DecisionBucket.BLOCKED, passed=False,
            block=["min_liquidity"]))
        self.assertEqual(r["decision_bucket"], "BLOCKED")
        self.assertFalse(r["execution_eligible"])
        self.assertIn("min_liquidity", r["block_reasons"])

    def test_matrix_e_no_wallclock_or_runtime_fields(self):
        r = build_scoring_audit_record(self._mk())
        for forbidden in ("timestamp", "now", "hostname", "host", "pid",
                          "random", "uuid", "wall_clock", "env"):
            self.assertNotIn(forbidden, r)

    def test_matrix_e_deterministic(self):
        self.assertEqual(build_scoring_audit_record(self._mk()),
                         build_scoring_audit_record(self._mk()))

    # ── Matrix F: audit summary ──
    def test_matrix_f_mixed_counts(self):
        s = build_scoring_audit_summary([
            self._mk(ml=90, support=90),                       # HIGH_CONVICTION
            self._mk(gate_bucket=DecisionBucket.BLOCKED, passed=False,
                     block=["x"]),                             # BLOCKED
            self._mk(side="SHORT")])                           # capped
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["by_decision_bucket"]["HIGH_CONVICTION"], 1)
        self.assertEqual(s["by_decision_bucket"]["BLOCKED"], 1)
        self.assertEqual(s["execution_eligible_count"], 1)

    def test_matrix_f_empty(self):
        s = build_scoring_audit_summary([])
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["execution_eligible_count"], 0)
        self.assertTrue(all(v == 0 for v in s["by_decision_bucket"].values()))
        self.assertTrue(all(v == 0 for v in s["by_confidence_bucket"].values()))

    def test_matrix_f_summary_deterministic(self):
        cands = [self._mk(), self._mk(side="SHORT")]
        self.assertEqual(build_scoring_audit_summary(cands),
                         build_scoring_audit_summary(cands))

    # ── Matrix G: purity ──
    def test_matrix_g_import_creates_no_file(self):
        import importlib
        import bot.signal_scoring.io as io_mod
        import bot.signal_scoring.audit as audit_mod
        for m19 in (_REPO_ROOT / "data" / "m19",):
            before = list(m19.glob("*")) if m19.exists() else []
            importlib.reload(io_mod)
            importlib.reload(audit_mod)
            after = list(m19.glob("*")) if m19.exists() else []
            self.assertEqual(before, after)

    def test_matrix_g_audit_creates_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = os.getcwd()
            os.chdir(td)
            try:
                build_scoring_audit_record(self._mk())
                build_scoring_audit_summary([self._mk(), self._mk()])
                self.assertEqual(os.listdir(td), [])
            finally:
                os.chdir(cwd)

    def test_matrix_g_scoring_modules_do_not_import_io(self):
        import ast
        for mod in ("audit.py", "adapters.py", "composite.py", "gates.py",
                    "components.py", "penalties.py"):
            tree = ast.parse((_PKG_DIR / mod).read_text())
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and n.module:
                    self.assertFalse(
                        n.module.endswith(".io")
                        or n.module == "bot.signal_scoring.io",
                        f"{mod} imports io")
                if isinstance(n, ast.Import):
                    for a in n.names:
                        self.assertFalse(a.name.endswith("signal_scoring.io"),
                                         f"{mod} imports io")

    def test_matrix_g_io_is_only_module_with_open_tokens(self):
        tokens = ("open(", "mkstemp(", "os.replace(")
        for path in _PKG_DIR.glob("*.py"):
            src = path.read_text()
            if path.name == "io.py":
                self.assertTrue(any(t in src for t in tokens))
            else:
                for t in tokens:
                    self.assertNotIn(t, src, f"{path.name} has {t}")

    def test_no_sqlite_no_network_in_io_audit(self):
        # io.py legitimately NAMES signals.db / data paths in order to REJECT
        # them, so we check for genuine library-usage tokens, not the path-guard
        # string literals.
        for mod in ("io.py", "audit.py"):
            src = (_PKG_DIR / mod).read_text()
            for tok in ("sqlite3", "requests.", "urllib.request", "aiohttp",
                        "socket.socket"):
                self.assertNotIn(tok, src, f"{mod} contains {tok}")


if __name__ == "__main__":
    unittest.main()
