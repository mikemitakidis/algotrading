"""M20.D — paper order + simulated fill proof tests.

Through the real build_paper_order / simulate_paper_fill: valid order/fill
creation, deterministic IDs, the LONG slippage/commission model, every safe-
reject path, no position/PnL construction, and the safety boundary. Reuses the
frozen M20.A PaperOrder / PaperFill contracts (no schema change).
"""
import ast
import pathlib
import subprocess
import unittest

import bot.paper as bp
from bot.paper import (
    PaperRoutingDecision, PaperSizingPreview, PaperSide, PaperOrderType,
    PaperOrderStatus, PaperOrder, PaperFill, compute_paper_sizing,
    build_paper_order, simulate_paper_fill, PaperOrderResult, PaperFillResult,
)

_PKG_DIR = pathlib.Path(__file__).resolve().parent / "bot" / "paper"
_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_BASELINE = "e823fe6779deaccc7b8ff7859c17b4dab564b868"
_M20UA_HEAD = "97f02326e12d9e381d94544555524c2d87b2cf27"
_M20C_HEAD = "408fc0f8152a56c0117cc6abbed60d8551dbcfdb"
_TS = "2026-06-18T10:00:00+00:00"


def _decision(eligible=True, side="LONG", cid="c1", symbol="AAPL"):
    return PaperRoutingDecision(
        m19_candidate_id=cid, symbol=symbol, side=side,
        decision_bucket="HIGH_CONVICTION", confidence_bucket="HIGH",
        paper_routing_eligible=eligible, evaluated_at_utc=_TS)


def _sizing(decision=None, **kw):
    kw.setdefault("paper_equity", 100000.0)
    kw.setdefault("available_paper_cash", 50000.0)
    kw.setdefault("reference_price", 100.0)
    kw.setdefault("stop_distance", 5.0)
    kw.setdefault("evaluated_at_utc", _TS)
    return compute_paper_sizing(decision or _decision(), **kw)


def _order(**kw):
    d = kw.pop("decision", _decision())
    s = kw.pop("sizing", None) or _sizing(d)
    kw.setdefault("reference_price", 100.0)
    kw.setdefault("created_at_utc", _TS)
    return build_paper_order(d, s, **kw)


class M20DOrders(unittest.TestCase):

    def test_valid_sizing_creates_order(self):
        r = _order()
        self.assertTrue(r.ok)
        self.assertIsInstance(r.order, PaperOrder)
        self.assertIsNone(r.rejection_reason)

    def test_order_has_ppr_id(self):
        self.assertTrue(_order().order.paper_order_id.startswith("PPR-"))

    def test_order_status_pending_simulation(self):
        self.assertEqual(_order().order.status,
                         PaperOrderStatus.PENDING_SIMULATION)

    def test_order_quantity_and_reference_price(self):
        r = _order()
        self.assertAlmostEqual(r.order.quantity, 200.0)
        self.assertAlmostEqual(r.order.reference_price, 100.0)
        self.assertEqual(r.order.side, PaperSide.LONG)

    def test_invalid_sizing_rejects_without_order(self):
        bad = _sizing(reference_price=100.0, stop_distance=None,
                      stop_loss_price=None)  # missing stop -> ineligible
        r = _order(sizing=bad)
        self.assertFalse(r.ok)
        self.assertIsNone(r.order)
        self.assertEqual(r.rejection_reason, "sizing_not_eligible")

    def test_non_long_rejects(self):
        # sizing with a SHORT decision is itself ineligible; assert reject
        d = _decision(side="SHORT")
        r = _order(decision=d, sizing=_sizing(d))
        self.assertFalse(r.ok)
        self.assertIsNone(r.order)

    def test_non_routable_decision_rejects(self):
        d = _decision(eligible=False)
        r = build_paper_order(d, _sizing(), reference_price=100.0,
                              created_at_utc=_TS)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "not_paper_routable")

    def test_invalid_reference_price_rejects(self):
        for bad in (0.0, -5.0, float("inf")):
            r = _order(reference_price=bad)
            self.assertFalse(r.ok)
            self.assertEqual(r.rejection_reason, "invalid_reference_price")

    def test_invalid_timestamp_rejects(self):
        for bad in ("not-a-date", "2026-06-18T10:00:00", ""):
            r = _order(created_at_utc=bad)
            self.assertFalse(r.ok)
            self.assertEqual(r.rejection_reason, "invalid_timestamp")

    def test_reference_price_mismatch_warns_not_rejects(self):
        # sizing basis is 100; supply 101 -> advisory warning, still ok
        r = _order(reference_price=101.0)
        self.assertTrue(r.ok)
        self.assertIn("reference_price_differs_from_sizing_basis", r.warnings)

    def test_deterministic_order_id(self):
        self.assertEqual(_order().order.paper_order_id,
                         _order().order.paper_order_id)

    def test_inputs_not_mutated(self):
        d = _decision()
        s = _sizing(d)
        d_before, s_before = d.to_dict(), s.to_dict()
        build_paper_order(d, s, reference_price=100.0, created_at_utc=_TS)
        self.assertEqual(d.to_dict(), d_before)
        self.assertEqual(s.to_dict(), s_before)


class M20DFills(unittest.TestCase):

    def _valid_order(self):
        return _order().order

    def test_valid_order_creates_fill(self):
        r = simulate_paper_fill(self._valid_order(),
                                simulated_market_price=100.0, fill_time_utc=_TS)
        self.assertTrue(r.ok)
        self.assertIsInstance(r.fill, PaperFill)

    def test_fill_has_pfl_id(self):
        r = simulate_paper_fill(self._valid_order(),
                                simulated_market_price=100.0, fill_time_utc=_TS)
        self.assertTrue(r.fill.paper_fill_id.startswith("PFL-"))

    def test_fill_references_order_id(self):
        o = self._valid_order()
        r = simulate_paper_fill(o, simulated_market_price=100.0,
                                fill_time_utc=_TS)
        self.assertEqual(r.fill.paper_order_id, o.paper_order_id)
        self.assertTrue(r.fill.paper_order_id.startswith("PPR-"))

    def test_fill_price_includes_slippage(self):
        o = self._valid_order()
        r = simulate_paper_fill(o, simulated_market_price=100.0,
                                fill_time_utc=_TS, slippage_bps=10)
        self.assertAlmostEqual(r.fill.fill_price, 100.1)
        self.assertGreater(r.fill.fill_price, 100.0)

    def test_zero_slippage_accepted(self):
        o = self._valid_order()
        r = simulate_paper_fill(o, simulated_market_price=100.0,
                                fill_time_utc=_TS, slippage_bps=0)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.fill.fill_price, 100.0)
        self.assertAlmostEqual(r.fill.assumed_slippage, 0.0)

    def test_zero_commission_accepted(self):
        o = self._valid_order()
        r = simulate_paper_fill(o, simulated_market_price=100.0,
                                fill_time_utc=_TS, commission_bps=0,
                                flat_commission=0)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.fill.assumed_commission, 0.0)

    def test_commission_max_flat_bps(self):
        o = self._valid_order()  # quantity 200
        # bps binds: 200*100.1*2/10000 = 4.004 > flat 1
        r1 = simulate_paper_fill(o, simulated_market_price=100.0,
                                 fill_time_utc=_TS, slippage_bps=10,
                                 commission_bps=2, flat_commission=1.0)
        self.assertAlmostEqual(r1.fill.assumed_commission, 4.004)
        # flat binds: bps tiny, flat 5
        r2 = simulate_paper_fill(o, simulated_market_price=100.0,
                                 fill_time_utc=_TS, commission_bps=0.1,
                                 flat_commission=5.0)
        self.assertAlmostEqual(r2.fill.assumed_commission, 5.0)

    def test_assumed_slippage_is_total_cost(self):
        o = self._valid_order()  # quantity 200
        r = simulate_paper_fill(o, simulated_market_price=100.0,
                                fill_time_utc=_TS, slippage_bps=10)
        # (100.1 - 100) * 200 = 20.0
        self.assertAlmostEqual(r.fill.assumed_slippage, 20.0)

    def test_negative_slippage_rejected(self):
        r = simulate_paper_fill(self._valid_order(),
                                simulated_market_price=100.0, fill_time_utc=_TS,
                                slippage_bps=-1)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "negative_slippage")

    def test_negative_commission_rejected(self):
        r = simulate_paper_fill(self._valid_order(),
                                simulated_market_price=100.0, fill_time_utc=_TS,
                                commission_bps=-1)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "negative_commission")

    def test_negative_flat_commission_rejected(self):
        r = simulate_paper_fill(self._valid_order(),
                                simulated_market_price=100.0, fill_time_utc=_TS,
                                flat_commission=-1)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "negative_flat_commission")

    def test_invalid_market_price_rejects(self):
        for bad in (0.0, -10.0, float("nan")):
            r = simulate_paper_fill(self._valid_order(),
                                    simulated_market_price=bad,
                                    fill_time_utc=_TS)
            self.assertFalse(r.ok)
            self.assertEqual(r.rejection_reason, "invalid_market_price")

    def test_invalid_fill_timestamp_rejects(self):
        r = simulate_paper_fill(self._valid_order(),
                                simulated_market_price=100.0,
                                fill_time_utc="nope")
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "invalid_timestamp")

    def test_deterministic_fill_id_and_output(self):
        o = self._valid_order()
        a = simulate_paper_fill(o, simulated_market_price=100.0,
                                fill_time_utc=_TS, slippage_bps=10,
                                commission_bps=2)
        b = simulate_paper_fill(o, simulated_market_price=100.0,
                                fill_time_utc=_TS, slippage_bps=10,
                                commission_bps=2)
        self.assertEqual(a.fill.paper_fill_id, b.fill.paper_fill_id)
        self.assertEqual(a.fill.to_dict(), b.fill.to_dict())

    def test_order_not_mutated(self):
        o = self._valid_order()
        before = o.to_dict()
        simulate_paper_fill(o, simulated_market_price=100.0, fill_time_utc=_TS)
        self.assertEqual(o.to_dict(), before)


class M20DNoPositionOrPnL(unittest.TestCase):

    def test_no_position_or_pnl_construction(self):
        for mod in ("orders.py", "fills.py"):
            src = (_PKG_DIR / mod).read_text()
            for tok in ("PaperPosition(", "PaperPnLSnapshot(",
                        "execute_order", "place_order", "submit_order"):
                self.assertNotIn(tok, src, f"{mod}:{tok}")

    def test_no_storage_or_io_tokens(self):
        for mod in ("orders.py", "fills.py"):
            src = (_PKG_DIR / mod).read_text()
            for tok in ("open(", "sqlite3", "json.dump", "to_csv",
                        ".connect(", "data/paper", "signals.db", "mkstemp("):
                self.assertNotIn(tok, src, f"{mod}:{tok}")


class M20DSafetyGuards(unittest.TestCase):

    FORBIDDEN_ROOTS = {"ib_insync", "requests", "urllib", "aiohttp", "socket",
                       "http", "main", "dashboard", "sqlite3", "yfinance",
                       "random"}
    FORBIDDEN_PREFIXES = ("bot.brokers", "bot.live", "bot.etoro", "bot.risk",
                          "bot.risk_authority", "bot.flywheel", "bot.scanner",
                          "bot.strategy", "dashboard", "main")

    def test_no_forbidden_imports(self):
        offenders = []
        for mod in ("orders.py", "fills.py"):
            tree = ast.parse((_PKG_DIR / mod).read_text())
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        root = a.name.split(".")[0]
                        if root in self.FORBIDDEN_ROOTS or \
                                a.name.startswith(self.FORBIDDEN_PREFIXES):
                            offenders.append(f"{mod}:{a.name}")
                elif isinstance(n, ast.ImportFrom) and n.module:
                    root = n.module.split(".")[0]
                    if root in self.FORBIDDEN_ROOTS or \
                            n.module.startswith(self.FORBIDDEN_PREFIXES):
                        offenders.append(f"{mod}:{n.module}")
        self.assertEqual(offenders, [])

    def test_no_wallclock_token(self):
        for mod in ("orders.py", "fills.py"):
            src = (_PKG_DIR / mod).read_text()
            self.assertNotIn("datetime.now", src)
            self.assertNotIn("time.time", src)

    def test_import_writes_nothing(self):
        import importlib
        d = _REPO_ROOT / "data" / "paper"
        before = sorted(p.name for p in d.glob("*")) if d.exists() else []
        importlib.import_module("bot.paper.orders")
        importlib.import_module("bot.paper.fills")
        after = sorted(p.name for p in d.glob("*")) if d.exists() else []
        self.assertEqual(before, after)


class M20DFrozenChecks(unittest.TestCase):

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

    def test_schema_unchanged(self):
        # M20.D must not change the frozen M20.A schema/contracts
        self._unchanged(_M20C_HEAD, "bot/paper/schema.py")

    def test_paper_only_authorised_d_diff(self):
        r = subprocess.run(["git", "diff", "--name-only", _M20C_HEAD, "HEAD",
                            "--", "bot/paper"], capture_output=True, text=True,
                           timeout=10)
        changed = set(r.stdout.split())
        self.assertTrue(changed <= {"bot/paper/orders.py", "bot/paper/fills.py",
                                    "bot/paper/__init__.py"}, changed)


if __name__ == "__main__":
    unittest.main()
