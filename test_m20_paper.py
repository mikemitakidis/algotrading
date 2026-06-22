"""M20.A — paper-trading firewall contracts proof tests.

Contracts-first: schema round-trips, enum/ID/IS_LIVE invariants, lifecycle
transition matrix, deterministic provenance, the M19 ingestion contract guard,
and the safety boundary (no broker/live/risk/etoro/flywheel/main/dashboard/
network imports; no DB/persistence; no shared inheritance with broker/live
classes; no unsafe bare live-verb names; M19 + protected runtime files
unchanged; no writes on import).
"""
import ast
import os
import pathlib
import unittest

import bot.paper as bp
from bot.paper import (
    PaperRoutingDecision, PaperOrder, PaperFill, PaperPosition,
    PaperPnLSnapshot, PaperEvent, PaperTradingConfig, default_paper_config,
    PaperSide, PaperOrderType, PaperEventType, PaperOrderStatus,
    PaperPositionStatus,
    PaperContractViolation, InvalidPaperTransition,
    assert_m19_candidate_contract, is_valid_transition, validate_transition,
    TERMINAL_STATES, provenance,
)

_PKG_DIR = pathlib.Path(__file__).resolve().parent / "bot" / "paper"
_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_TS = "2026-06-17T10:15:00Z"
_M20_BASELINE = "e823fe6779deaccc7b8ff7859c17b4dab564b868"


def _order(**over):
    base = dict(
        paper_order_id=provenance.paper_order_id({"o": 1}),
        m19_candidate_id="c1", symbol="AAPL", side="LONG",
        order_type="MARKET", quantity=10, reference_price=100.0,
        paper_routing_eligible=True, status="PENDING_SIMULATION",
        created_at_utc=_TS)
    base.update(over)
    return PaperOrder(**base)


class M20AContracts(unittest.TestCase):

    def test_phase_marker(self):
        self.assertEqual(bp.M20_PHASE, "M20.A")

    def test_is_live_false_on_all_schemas(self):
        rd = PaperRoutingDecision(
            m19_candidate_id="c1", symbol="AAPL", side="LONG",
            decision_bucket="HIGH_CONVICTION", confidence_bucket="HIGH",
            paper_routing_eligible=True, evaluated_at_utc=_TS)
        po = _order()
        pf = PaperFill(paper_fill_id=provenance.paper_fill_id({"f": 1}),
                       paper_order_id=po.paper_order_id, fill_price=100.0,
                       fill_quantity=10, fill_time_utc=_TS)
        pp = PaperPosition(
            paper_position_id=provenance.paper_position_id({"p": 1}),
            symbol="AAPL", side="LONG", quantity=10,
            average_entry_price=100.0, status="OPEN", opened_at_utc=_TS)
        pnl = PaperPnLSnapshot(timestamp_utc=_TS, total_paper_equity=100000.0,
                               available_paper_cash=90000.0)
        ev = PaperEvent(paper_event_id=provenance.paper_event_id({"e": 1}),
                        event_time_utc=_TS, event_type="ORDER_CREATED",
                        m19_candidate_id="c1")
        for obj in (rd, po, pf, pp, pnl, ev, default_paper_config()):
            self.assertIs(obj.IS_LIVE, False)
            self.assertIs(obj.to_dict()["IS_LIVE"], False)

    def test_round_trips(self):
        po = _order()
        self.assertEqual(PaperOrder.from_dict(po.to_dict()).to_dict(),
                         po.to_dict())
        rd = PaperRoutingDecision(
            m19_candidate_id="c1", symbol="AAPL", side="LONG",
            decision_bucket="ELIGIBLE", confidence_bucket="MEDIUM_HIGH",
            paper_routing_eligible=True, evaluated_at_utc=_TS,
            m19_input_digest="d1", calibration_applied=False,
            reason_codes=["x"])
        self.assertEqual(
            PaperRoutingDecision.from_dict(rd.to_dict()).to_dict(),
            rd.to_dict())

    def test_unknown_field_rejected(self):
        d = _order().to_dict()
        d["surprise"] = 1
        with self.assertRaises(ValueError):
            PaperOrder.from_dict(d)

    def test_enum_validation(self):
        with self.assertRaises(ValueError):
            PaperRoutingDecision(
                m19_candidate_id="c", symbol="A", side="DIAGONAL",
                decision_bucket="X", confidence_bucket="Y",
                paper_routing_eligible=False, evaluated_at_utc=_TS)
        with self.assertRaises(ValueError):
            _order(order_type="TELEPATHIC")

    def test_id_prefixes_enforced(self):
        with self.assertRaises(ValueError):
            _order(paper_order_id="XXX-1")
        with self.assertRaises(ValueError):
            PaperFill(paper_fill_id="NOPE-1",
                      paper_order_id=provenance.paper_order_id({"o": 1}),
                      fill_price=1, fill_quantity=1, fill_time_utc=_TS)

    def test_timestamp_must_be_utc(self):
        for bad in ("2026-06-17T10:15:00", "not-a-date",
                    "2026-06-17T10:15:00+02:00"):
            with self.assertRaises(ValueError):
                PaperPnLSnapshot(timestamp_utc=bad, total_paper_equity=1,
                                 available_paper_cash=1)

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            PaperTradingConfig(paper_equity=-1)
        with self.assertRaises(ValueError):
            PaperTradingConfig(risk_per_trade_pct=0)
        self.assertEqual(default_paper_config().paper_equity, 100000.0)


class M20ALifecycle(unittest.TestCase):

    def test_valid_transitions(self):
        for f, t in (("PENDING_SIMULATION", "ROUTED_TO_PAPER"),
                     ("PENDING_SIMULATION", "REJECTED_BY_PAPER_RISK"),
                     ("ROUTED_TO_PAPER", "PARTIAL_FILL"),
                     ("ROUTED_TO_PAPER", "FILLED"),
                     ("PARTIAL_FILL", "FILLED"),
                     ("PARTIAL_FILL", "CLOSED"),
                     ("FILLED", "CLOSED")):
            self.assertTrue(is_valid_transition(f, t), f"{f}->{t}")

    def test_forbidden_transitions(self):
        for f, t in (("PENDING_SIMULATION", "CLOSED"),
                     ("PENDING_SIMULATION", "FILLED"),
                     ("CLOSED", "ROUTED_TO_PAPER"),
                     ("CLOSED", "FILLED"),
                     ("REJECTED_BY_PAPER_RISK", "FILLED"),
                     ("CANCELED", "ROUTED_TO_PAPER"),
                     ("EXPIRED", "FILLED")):
            self.assertFalse(is_valid_transition(f, t), f"{f}->{t}")
            with self.assertRaises(InvalidPaperTransition):
                validate_transition(f, t)

    def test_terminal_states_have_no_exits(self):
        self.assertEqual(TERMINAL_STATES, frozenset({
            PaperOrderStatus.REJECTED_BY_PAPER_RISK,
            PaperOrderStatus.CANCELED,
            PaperOrderStatus.EXPIRED,
            PaperOrderStatus.CLOSED}))
        for term in TERMINAL_STATES:
            for s in PaperOrderStatus:
                self.assertFalse(is_valid_transition(term, s))

    def test_full_state_pair_matrix(self):
        # every ordered pair is either explicitly allowed or rejected; no crash
        for f in PaperOrderStatus:
            for t in PaperOrderStatus:
                self.assertIn(is_valid_transition(f, t), (True, False))

    def test_unknown_status_rejected(self):
        with self.assertRaises(InvalidPaperTransition):
            validate_transition("WALKING", "CLOSED")


class M20AProvenance(unittest.TestCase):

    def test_deterministic_ids(self):
        ident = {"sym": "AAPL", "cid": "c1"}
        self.assertEqual(provenance.paper_order_id(ident),
                         provenance.paper_order_id(ident))

    def test_changed_input_changes_id(self):
        self.assertNotEqual(provenance.paper_order_id({"x": 1}),
                            provenance.paper_order_id({"x": 2}))

    def test_prefixes(self):
        self.assertTrue(provenance.paper_order_id({}).startswith("PPR-"))
        self.assertTrue(provenance.paper_fill_id({}).startswith("PFL-"))
        self.assertTrue(provenance.paper_position_id({}).startswith("PPS-"))
        self.assertTrue(provenance.paper_event_id({}).startswith("PEV-"))

    def test_canonical_json_sorted_and_finite(self):
        self.assertEqual(provenance.canonical_json({"b": 1, "a": 2}),
                         '{"a":2,"b":1}')
        with self.assertRaises(ValueError):
            provenance.canonical_json({"x": float("nan")})

    def test_no_wallclock_or_rng_tokens(self):
        src = (_PKG_DIR / "provenance.py").read_text()
        for tok in ("datetime.now", "time.time", "random", "uuid"):
            self.assertNotIn(tok, src)


class M20AM19Contract(unittest.TestCase):

    class _FakeCandidate:
        def __init__(self, execution_eligible):
            self.execution_eligible = execution_eligible

    def test_execution_eligible_true_raises(self):
        with self.assertRaises(PaperContractViolation):
            assert_m19_candidate_contract(self._FakeCandidate(True))

    def test_execution_eligible_false_accepted(self):
        # returns None, no raise
        self.assertIsNone(
            assert_m19_candidate_contract(self._FakeCandidate(False)))

    def test_no_positive_branch_on_execution_eligible(self):
        # No bot/paper module may branch positively on execution_eligible
        # (e.g. `if candidate.execution_eligible:` used as a go signal). The
        # only permitted reference is the contract guard comparing `is True`.
        for path in _PKG_DIR.glob("*.py"):
            src = path.read_text()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.If):
                    test = node.test
                    # a bare attribute test `if x.execution_eligible:`
                    if isinstance(test, ast.Attribute) and \
                            test.attr == "execution_eligible":
                        self.fail(f"{path.name} branches positively on "
                                  f"execution_eligible")


class M20ASafetyGuards(unittest.TestCase):

    FORBIDDEN_ROOTS = {
        "ib_insync", "requests", "urllib", "urllib2", "urllib3",
        "aiohttp", "socket", "http", "main", "dashboard", "sqlite3",
        "yfinance",
    }
    FORBIDDEN_PREFIXES = (
        "bot.brokers", "bot.live", "bot.etoro", "bot.risk",
        "bot.risk_authority", "bot.flywheel", "bot.scanner", "bot.strategy",
        "dashboard", "main",
    )

    def _iter(self):
        return sorted(_PKG_DIR.glob("*.py"))

    def test_no_forbidden_imports(self):
        offenders = []
        for path in self._iter():
            tree = ast.parse(path.read_text())
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        root = a.name.split(".")[0]
                        if root in self.FORBIDDEN_ROOTS or \
                                a.name.startswith(self.FORBIDDEN_PREFIXES):
                            offenders.append(f"{path.name}:{a.name}")
                elif isinstance(n, ast.ImportFrom) and n.module:
                    root = n.module.split(".")[0]
                    if root in self.FORBIDDEN_ROOTS or \
                            n.module.startswith(self.FORBIDDEN_PREFIXES):
                        offenders.append(f"{path.name}:{n.module}")
        self.assertEqual(offenders, [], f"forbidden imports: {offenders}")

    def test_no_db_network_file_tokens(self):
        tokens = ("sqlite3", "signals.db", ".connect(", "socket.socket",
                  "requests.get", "requests.post", "urlopen", "open(",
                  "mkstemp(", ".to_csv(", ".to_parquet(")
        # storage.py (M20.H) is the sanctioned explicit-call-only JSONL file-IO
        # module; its own guards (test_m20_paper_storage) enforce no sqlite/
        # network and tempfile-only tests. Exclude it from the package-wide
        # file-token scan. Network/db tokens remain forbidden there via its
        # dedicated AST import guard.
        net_db_tokens = ("sqlite3", "signals.db", ".connect(", "socket.socket",
                         "requests.get", "requests.post", "urlopen",
                         ".to_csv(", ".to_parquet(")
        offenders = []
        for path in self._iter():
            src = path.read_text()
            check = net_db_tokens if path.name == "storage.py" else tokens
            for t in check:
                if t in src:
                    offenders.append(f"{path.name}:{t}")
        self.assertEqual(offenders, [], f"forbidden tokens: {offenders}")

    def test_no_unsafe_live_verb_names(self):
        # No bare execute_order/place_order/submit_order function defs.
        banned = {"execute_order", "place_order", "submit_order"}
        offenders = []
        for path in self._iter():
            tree = ast.parse(path.read_text())
            for n in ast.walk(tree):
                if isinstance(n, ast.FunctionDef) and n.name in banned:
                    offenders.append(f"{path.name}:{n.name}")
        self.assertEqual(offenders, [], f"unsafe live verbs: {offenders}")

    def test_no_data_ml_or_m19_tokens(self):
        for path in self._iter():
            src = path.read_text()
            self.assertNotIn("data/ml", src, f"{path.name}")
            self.assertNotIn("data/m19", src, f"{path.name}")

    def test_no_shared_inheritance_with_broker_live(self):
        # No paper schema MRO may contain a broker/live/etoro class.
        for cls in (PaperOrder, PaperFill, PaperPosition, PaperPnLSnapshot,
                    PaperEvent, PaperRoutingDecision, PaperTradingConfig):
            for base in cls.__mro__:
                mod = getattr(base, "__module__", "")
                self.assertFalse(
                    mod.startswith(("bot.brokers", "bot.live", "bot.etoro")),
                    f"{cls.__name__} inherits from {mod}")

    def test_import_writes_nothing(self):
        import importlib
        d = _REPO_ROOT / "data" / "paper"
        before = sorted(p.name for p in d.glob("*")) if d.exists() else []
        importlib.import_module("bot.paper")
        after = sorted(p.name for p in d.glob("*")) if d.exists() else []
        self.assertEqual(before, after)

    def test_m19_files_unchanged(self):
        import subprocess
        r = subprocess.run(
            ["git", "diff", "--name-only", _M20_BASELINE, "HEAD", "--",
             "bot/signal_scoring"], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "", "M19 files changed (frozen)")

    def test_protected_runtime_files_unchanged(self):
        import subprocess
        r = subprocess.run(
            ["git", "diff", "--name-only", _M20_BASELINE, "HEAD", "--",
             "main.py", "bot/scanner.py", "bot/risk.py", "bot/strategy.py",
             "dashboard/app.py", "bot/brokers", "bot/flywheel.py"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "",
                         "protected runtime files changed")


class M20AStorage(unittest.TestCase):

    def test_no_store_module(self):
        self.assertFalse((_PKG_DIR / "store.py").exists())
        self.assertFalse((_PKG_DIR / "io.py").exists())

    def test_no_engine_or_sizing_modules(self):
        # sizing.py was correctly forbidden during M20.A, but M20.C now
        # legitimately adds bot/paper/sizing.py. engine/simulator/risk_sim
        # remain forbidden.
        for name in ("engine.py", "simulator.py", "risk_sim.py"):
            self.assertFalse((_PKG_DIR / name).exists(),
                             f"{name} must not exist in M20.A")

    def test_no_sqlite_anywhere(self):
        for path in _PKG_DIR.glob("*.py"):
            self.assertNotIn("sqlite3", path.read_text(), f"{path.name}")


class M20ACorrectivePass(unittest.TestCase):
    """Corrective M20.A: position status separated from order status; impossible
    numeric paper values rejected at contract-freeze time."""

    def _pos(self, **over):
        base = dict(
            paper_position_id=provenance.paper_position_id({"p": 1}),
            symbol="AAPL", side="LONG", quantity=10,
            average_entry_price=100.0, status="OPEN", opened_at_utc=_TS)
        base.update(over)
        return PaperPosition(**base)

    # ── position status separation ──
    def test_position_status_open(self):
        self.assertEqual(self._pos(status="OPEN").status,
                         PaperPositionStatus.OPEN)

    def test_position_status_closed(self):
        self.assertEqual(self._pos(status="CLOSED", quantity=0,
                                   closed_at_utc=_TS).status,
                         PaperPositionStatus.CLOSED)

    def test_position_status_filled_rejected(self):
        with self.assertRaises(ValueError):
            self._pos(status="FILLED")

    def test_position_status_partial_fill_rejected(self):
        with self.assertRaises(ValueError):
            self._pos(status="PARTIAL_FILL")

    def test_position_does_not_reuse_order_status(self):
        # PaperPosition.status must be a PaperPositionStatus, not a
        # PaperOrderStatus (schema uses `from __future__ import annotations`,
        # so the raw annotation is a string — assert via the coerced instance
        # and the annotation text).
        ann = PaperPosition.__annotations__["status"]
        self.assertEqual(ann, "PaperPositionStatus")
        self.assertIsInstance(self._pos().status, PaperPositionStatus)
        self.assertNotIsInstance(self._pos().status, PaperOrderStatus)
        # PaperOrderStatus has no OPEN member; PaperPositionStatus does
        self.assertFalse(hasattr(PaperOrderStatus, "OPEN"))
        self.assertTrue(hasattr(PaperPositionStatus, "OPEN"))

    # ── PaperOrder numeric validation ──
    def test_order_rejects_bad_numbers(self):
        for bad in (dict(quantity=0), dict(quantity=-1),
                    dict(reference_price=0), dict(reference_price=-5),
                    dict(limit_price=0), dict(limit_price=-1),
                    dict(simulated_stop_loss=0),
                    dict(simulated_stop_loss=-2),
                    dict(simulated_take_profit=0),
                    dict(simulated_take_profit=-3)):
            with self.assertRaises(ValueError, msg=str(bad)):
                _order(**bad)

    def test_order_accepts_valid_optionals(self):
        po = _order(limit_price=101.0, simulated_stop_loss=98.0,
                    simulated_take_profit=104.0)
        self.assertEqual(po.limit_price, 101.0)

    # ── PaperFill numeric validation ──
    def test_fill_rejects_bad_numbers(self):
        oid = provenance.paper_order_id({"o": 1})
        fid = provenance.paper_fill_id({"f": 1})
        for bad in (dict(fill_price=0), dict(fill_price=-1),
                    dict(fill_quantity=0), dict(fill_quantity=-1),
                    dict(assumed_slippage=-0.1),
                    dict(assumed_commission=-0.1)):
            base = dict(paper_fill_id=fid, paper_order_id=oid,
                        fill_price=100.0, fill_quantity=10, fill_time_utc=_TS)
            base.update(bad)
            with self.assertRaises(ValueError, msg=str(bad)):
                PaperFill(**base)

    def test_fill_accepts_zero_slippage_commission(self):
        pf = PaperFill(paper_fill_id=provenance.paper_fill_id({"f": 2}),
                       paper_order_id=provenance.paper_order_id({"o": 2}),
                       fill_price=100.0, fill_quantity=10, fill_time_utc=_TS,
                       assumed_slippage=0.0, assumed_commission=0.0)
        self.assertEqual(pf.assumed_slippage, 0.0)

    # ── PaperPosition numeric validation ──
    def test_position_rejects_negative_quantity(self):
        with self.assertRaises(ValueError):
            self._pos(quantity=-1)

    def test_position_rejects_bad_entry_when_qty_positive(self):
        for bad in (0, -5):
            with self.assertRaises(ValueError):
                self._pos(quantity=10, average_entry_price=bad)

    def test_position_allows_zero_quantity_closed(self):
        # a fully closed position may have quantity 0 (entry not required > 0)
        p = self._pos(quantity=0, average_entry_price=0.0, status="CLOSED",
                      closed_at_utc=_TS)
        self.assertEqual(p.quantity, 0)

    # ── PaperPnLSnapshot numeric validation ──
    def test_pnl_rejects_negative_balances(self):
        for bad in (dict(total_paper_equity=-1),
                    dict(available_paper_cash=-1),
                    dict(locked_paper_margin=-1),
                    dict(drawdown_pct=-1)):
            base = dict(timestamp_utc=_TS, total_paper_equity=100000.0,
                        available_paper_cash=90000.0)
            base.update(bad)
            with self.assertRaises(ValueError, msg=str(bad)):
                PaperPnLSnapshot(**base)

    def test_pnl_allows_negative_pnl_fields(self):
        snap = PaperPnLSnapshot(timestamp_utc=_TS, total_paper_equity=100000.0,
                                available_paper_cash=90000.0,
                                daily_realized_pnl=-500.0,
                                unrealized_pnl=-1200.0)
        self.assertEqual(snap.daily_realized_pnl, -500.0)
        self.assertEqual(snap.unrealized_pnl, -1200.0)


if __name__ == "__main__":
    unittest.main()
