"""test_m19_signal_scoring.py — pre-execution M19.A contract/config/provenance.

M19.A scope: contracts + config + provenance ONLY. No scoring, no gates, no
adapters, no output writer. Tests cover schema round-trip + validation, config
defaults + validation, deterministic provenance, the short-side structural
rule, and static safety guards (no broker/live/main/dashboard/network imports;
no signals.db / data/ml / data/m19 writes anywhere in the package).
"""
import ast
import pathlib
import unittest

from bot.signal_scoring import (
    SCHEMA_VERSION_INPUT, SCHEMA_VERSION_OUTPUT,
    ScoringProfile, SignalSide, DecisionBucket, ConfidenceBucket,
    PenaltySeverity, SignalCandidateInput, ScoredSignalCandidate,
    SignalScoringConfig, default_config, DEFAULT_PROFILE,
)
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
        forbidden_call_tokens = [
            "sqlite3.connect", "socket.socket",
            "requests.get", "requests.post", "urlopen",
            ".to_csv(", ".to_parquet(", ".to_pickle(",
            "open(",  # no file writes from contracts/config/provenance
        ]
        offenders = []
        for path in self._iter_pkg_files():
            src = path.read_text()
            for s in forbidden_call_tokens:
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


if __name__ == "__main__":
    unittest.main()
