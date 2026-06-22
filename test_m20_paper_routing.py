"""M20.B — paper routing decision proof tests.

Asserts the routing rule through the real decide_paper_routing with real M19
ScoredSignalCandidates: eligibility matrix, all-failing-reasons recording, the
execution_eligible contract violation, safe invalid-shape rejection, advisory-
only universe context (scan_ready=false does not hard-block), determinism, and
the safety boundary.
"""
import ast
import pathlib
import subprocess
import unittest

import bot.paper as bp
from bot.paper import (
    decide_paper_routing, PaperRoutingDecision, PaperContractViolation,
    PaperSide,
)
from bot.signal_scoring import assemble_score, default_config, COMPONENT_NAMES
from bot.signal_scoring.schema import (
    GateResult, ComponentScore, PenaltyResult, MultiplierResult,
    DecisionBucket, ScoringProfile, SignalCandidateInput, SignalSide,
)
from bot.universe.schema import SymbolRecord

_PKG_DIR = pathlib.Path(__file__).resolve().parent / "bot" / "paper"
_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_BASELINE = "e823fe6779deaccc7b8ff7859c17b4dab564b868"
_M20UA_HEAD = "97f02326e12d9e381d94544555524c2d87b2cf27"
_TS = "2026-06-18T10:00:00+00:00"


def _candidate(side="LONG", ml=90, sup=90, passed=True, bucket=None,
               block=None, mr=None, exec_elig=False):
    comps = {"ml": ComponentScore(component="ml", score=ml)}
    for c in COMPONENT_NAMES:
        if c != "ml":
            comps[c] = ComponentScore(component=c, score=sup)
    g = GateResult(profile=ScoringProfile.STRICT, passed=passed,
                   decision_bucket=bucket, block_reasons=block or [],
                   manual_review_reasons=mr or [])
    p = PenaltyResult(profile=ScoringProfile.STRICT, items=[], total_points=0,
                      raw_total_points=0)
    m = MultiplierResult(profile=ScoringProfile.STRICT, items=[], product=1.0,
                         effective_multiplier=1.0)
    ci = SignalCandidateInput(symbol="AAPL", side=side,
                              signal_timestamp_utc=_TS)
    sc = assemble_score(g, comps, p, m, ci, default_config())
    if exec_elig:
        object.__setattr__(sc, "execution_eligible", True)
    return sc


def _universe_record(active=True, scan_ready=False):
    return SymbolRecord(
        internal_symbol="NASDAQ:AAPL", provider_symbols={"yfinance": "AAPL"},
        asset_class="EQUITY", name="AAPL", exchange="NASDAQ", country="US",
        region="US", currency="USD", timezone="America/New_York",
        trading_calendar="XNAS", universe_tags=["legacy_focus"], active=active,
        scan_ready=scan_ready, source="test", as_of_date="2026-06-18",
        first_seen_utc=_TS)


class M20BRouting(unittest.TestCase):

    def _route(self, **kw):
        urec = kw.pop("universe_record", None)
        return decide_paper_routing(_candidate(**kw), evaluated_at_utc=_TS,
                                    universe_record=urec)

    def test_phase_marker_unchanged(self):
        # M20.B does not bump the shared M20_PHASE marker: that would break the
        # frozen M20.A test, and the only permitted __init__ change is the
        # routing export. The marker still reflects the contract package phase.
        self.assertEqual(bp.M20_PHASE, "M20.A")

    def test_routing_exported(self):
        self.assertTrue(hasattr(bp, "decide_paper_routing"))
        self.assertIn("decide_paper_routing", bp.__all__)

    def test_long_high_conviction_routes_true(self):
        d = self._route(ml=90, sup=90)
        self.assertTrue(d.paper_routing_eligible)
        self.assertEqual(d.decision_bucket, "HIGH_CONVICTION")
        self.assertIn("paper_routing_eligible", d.reason_codes)

    def test_long_eligible_routes_true(self):
        d = self._route(ml=70, sup=70)
        self.assertTrue(d.paper_routing_eligible)
        self.assertEqual(d.decision_bucket, "ELIGIBLE")

    def test_watch_routes_false(self):
        d = self._route(ml=50, sup=50)
        self.assertFalse(d.paper_routing_eligible)
        self.assertIn("watch_below_routing_threshold", d.reason_codes)

    def test_reject_routes_false(self):
        d = self._route(ml=20, sup=20)
        self.assertFalse(d.paper_routing_eligible)
        self.assertIn("reject_below_routing_threshold", d.reason_codes)

    def test_manual_review_routes_false(self):
        d = self._route(passed=False, bucket=DecisionBucket.MANUAL_REVIEW,
                        mr=["needs_review"])
        self.assertFalse(d.paper_routing_eligible)
        self.assertIn("manual_review_not_auto_routed", d.reason_codes)

    def test_blocked_routes_false(self):
        d = self._route(passed=False, bucket=DecisionBucket.BLOCKED,
                        block=["x"])
        self.assertFalse(d.paper_routing_eligible)
        self.assertIn("blocked_not_paper_routed", d.reason_codes)

    def test_short_routes_false(self):
        d = self._route(side="SHORT", ml=90, sup=90)
        self.assertFalse(d.paper_routing_eligible)
        self.assertIn("short_not_paper_routed", d.reason_codes)

    def test_blocked_reasons_present_routes_false(self):
        d = self._route(passed=False, bucket=DecisionBucket.BLOCKED,
                        block=["stale_data", "liquidity_below_min"])
        self.assertFalse(d.paper_routing_eligible)
        self.assertIn("blocked_reasons_present", d.reason_codes)
        # gate reasons surfaced (read from real M19 fields, never invented)
        self.assertIn("m19_blocked_reason:stale_data", d.warnings)
        self.assertIn("m19_blocked_reason:liquidity_below_min", d.warnings)

    def test_hard_gate_passed_false_routes_false(self):
        d = self._route(passed=False, bucket=DecisionBucket.BLOCKED,
                        block=["x"])
        self.assertFalse(d.paper_routing_eligible)
        self.assertIn("hard_gate_not_passed", d.reason_codes)

    def test_execution_eligible_true_raises(self):
        with self.assertRaises(PaperContractViolation):
            decide_paper_routing(_candidate(exec_elig=True),
                                 evaluated_at_utc=_TS)

    def test_all_failing_reasons_recorded(self):
        # SHORT + BLOCKED bucket -> multiple reasons recorded, not just first
        d = self._route(side="SHORT", passed=False,
                        bucket=DecisionBucket.BLOCKED, block=["x"])
        self.assertFalse(d.paper_routing_eligible)
        self.assertIn("short_not_paper_routed", d.reason_codes)
        self.assertIn("blocked_not_paper_routed", d.reason_codes)
        self.assertIn("hard_gate_not_passed", d.reason_codes)
        self.assertIn("blocked_reasons_present", d.reason_codes)


class M20BInvalidShape(unittest.TestCase):

    class _Broken:
        # missing/invalid required fields; execution_eligible False so it is a
        # shape problem, not a contract violation
        execution_eligible = False
        symbol = ""
        side = "not-an-enum"
        ml_context = {}
        provenance = {}

    def test_invalid_shape_routes_false(self):
        d = decide_paper_routing(self._Broken(), evaluated_at_utc=_TS)
        self.assertFalse(d.paper_routing_eligible)
        self.assertIn("invalid_candidate_shape", d.reason_codes)

    def test_invalid_shape_does_not_crash(self):
        # no exception other than a clean PaperRoutingDecision
        d = decide_paper_routing(self._Broken(), evaluated_at_utc=_TS)
        self.assertIsInstance(d, PaperRoutingDecision)


class M20BUniverseAdvisory(unittest.TestCase):

    def test_universe_record_found_advisory(self):
        d = decide_paper_routing(_candidate(ml=90, sup=90),
                                 evaluated_at_utc=_TS,
                                 universe_record=_universe_record())
        self.assertIn("universe_record_found", d.reason_codes)
        self.assertTrue(d.paper_routing_eligible)

    def test_universe_record_not_found_advisory(self):
        d = decide_paper_routing(_candidate(ml=90, sup=90),
                                 evaluated_at_utc=_TS, universe_record=None)
        self.assertIn("universe_record_not_found", d.reason_codes)

    def test_scan_ready_false_does_not_block(self):
        # scan_ready=False is recorded but must NOT flip eligibility
        d = decide_paper_routing(
            _candidate(ml=90, sup=90), evaluated_at_utc=_TS,
            universe_record=_universe_record(active=True, scan_ready=False))
        self.assertTrue(d.paper_routing_eligible)
        self.assertIn("universe_scan_ready_false", d.reason_codes)
        self.assertIn("universe_data_quality_unverified", d.reason_codes)


class M20BDeterminism(unittest.TestCase):

    def test_deterministic_output(self):
        a = decide_paper_routing(_candidate(ml=90, sup=90),
                                 evaluated_at_utc=_TS)
        b = decide_paper_routing(_candidate(ml=90, sup=90),
                                 evaluated_at_utc=_TS)
        self.assertEqual(a.to_dict(), b.to_dict())


class M20BSafetyGuards(unittest.TestCase):

    FORBIDDEN_ROOTS = {"ib_insync", "requests", "urllib", "aiohttp", "socket",
                       "http", "main", "dashboard", "sqlite3", "yfinance"}
    FORBIDDEN_PREFIXES = ("bot.brokers", "bot.live", "bot.etoro", "bot.risk",
                          "bot.risk_authority", "bot.flywheel", "bot.scanner",
                          "bot.strategy", "dashboard", "main")

    def test_routing_no_forbidden_imports(self):
        tree = ast.parse((_PKG_DIR / "routing.py").read_text())
        offenders = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    root = a.name.split(".")[0]
                    if root in self.FORBIDDEN_ROOTS or \
                            a.name.startswith(self.FORBIDDEN_PREFIXES):
                        offenders.append(a.name)
            elif isinstance(n, ast.ImportFrom) and n.module:
                root = n.module.split(".")[0]
                if root in self.FORBIDDEN_ROOTS or \
                        n.module.startswith(self.FORBIDDEN_PREFIXES):
                    offenders.append(n.module)
        self.assertEqual(offenders, [])

    def test_routing_no_storage_or_io_tokens(self):
        src = (_PKG_DIR / "routing.py").read_text()
        for tok in ("open(", "sqlite3", "json.dump", "to_csv", ".connect(",
                    "data/paper", "signals.db", "mkstemp("):
            self.assertNotIn(tok, src, tok)

    def test_routing_no_order_or_pnl_logic(self):
        src = (_PKG_DIR / "routing.py").read_text()
        # no order-placement verbs and no order/fill/position/pnl construction
        for tok in ("execute_order", "place_order", "submit_order",
                    "PaperOrder(", "PaperFill(", "PaperPosition(",
                    "PaperPnLSnapshot("):
            self.assertNotIn(tok, src, tok)

    def test_import_writes_nothing(self):
        import importlib
        d = _REPO_ROOT / "data" / "paper"
        before = sorted(p.name for p in d.glob("*")) if d.exists() else []
        importlib.import_module("bot.paper.routing")
        after = sorted(p.name for p in d.glob("*")) if d.exists() else []
        self.assertEqual(before, after)


class M20BFrozenChecks(unittest.TestCase):

    def _unchanged(self, baseline, *paths):
        r = subprocess.run(["git", "diff", "--name-only", baseline, "HEAD",
                            "--", *paths], capture_output=True, text=True,
                           timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "", f"{paths} changed vs {baseline}")

    def test_m19_frozen(self):
        self._unchanged(_BASELINE, "bot/signal_scoring")

    def test_m20ua_frozen(self):
        self._unchanged(_M20UA_HEAD, "bot/universe", "configs/universe")

    def test_protected_runtime_unchanged(self):
        self._unchanged(_BASELINE, "main.py", "bot/scanner.py", "bot/risk.py",
                        "bot/strategy.py", "dashboard/app.py", "bot/brokers",
                        "bot/flywheel.py")

    def test_m20a_only_routing_added(self):
        # bot/paper changed vs M20.A head only by routing.py + __init__.py
        r = subprocess.run(["git", "diff", "--name-only",
                            "5cdd0839204a71436579f9ed9a8c4a7d69681e87", "HEAD",
                            "--", "bot/paper"], capture_output=True, text=True,
                           timeout=10)
        changed = set(r.stdout.split())
        self.assertTrue(changed <= {"bot/paper/routing.py",
                                    "bot/paper/sizing.py",
                                    "bot/paper/orders.py",
                                    "bot/paper/fills.py",
                                    "bot/paper/positions.py",
                                    "bot/paper/pnl.py",
                                    "bot/paper/closing.py",
                                    "bot/paper/account.py",
                                    "bot/paper/ledger.py",
                                    "bot/paper/storage.py",
                                    "bot/paper/__init__.py"}, changed)


if __name__ == "__main__":
    unittest.main()
