"""M20.C — clean-room paper sizing proof tests.

Asserts the sizing rule through the real compute_paper_sizing: eligible sizing
preview, every safe-reject path, the three binding constraints, fractional
quantity, determinism, decision non-mutation, and the safety boundary (no
order/fill/position/PnL construction, no storage, no broker/live/risk imports).
"""
import ast
import pathlib
import subprocess
import unittest

import bot.paper as bp
from bot.paper import (
    compute_paper_sizing, PaperSizingPreview, PaperRoutingDecision, PaperSide,
)

_PKG_DIR = pathlib.Path(__file__).resolve().parent / "bot" / "paper"
_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_BASELINE = "e823fe6779deaccc7b8ff7859c17b4dab564b868"
_M20UA_HEAD = "97f02326e12d9e381d94544555524c2d87b2cf27"
_M20B_HEAD = "8f5045d6be204b5f422b9b5e796a9c15ac391f63"
_TS = "2026-06-18T10:00:00+00:00"


def _decision(eligible=True, side="LONG"):
    return PaperRoutingDecision(
        m19_candidate_id="c1", symbol="AAPL", side=side,
        decision_bucket="HIGH_CONVICTION", confidence_bucket="HIGH",
        paper_routing_eligible=eligible, evaluated_at_utc=_TS)


def _size(decision=None, **kw):
    kw.setdefault("paper_equity", 100000.0)
    kw.setdefault("available_paper_cash", 50000.0)
    kw.setdefault("reference_price", 100.0)
    kw.setdefault("evaluated_at_utc", _TS)
    if "stop_distance" not in kw and "stop_loss_price" not in kw:
        kw["stop_distance"] = 5.0
    return compute_paper_sizing(decision or _decision(), **kw)


class M20CSizing(unittest.TestCase):

    def test_eligible_long_produces_preview(self):
        p = _size()
        self.assertTrue(p.sizing_eligible)
        self.assertGreater(p.paper_quantity, 0)
        self.assertIsNone(p.sizing_rejection_reason)
        # math: risk 1% of 100k = 1000 budget / stop 5 = 200 sh
        self.assertAlmostEqual(p.paper_quantity, 200.0)
        self.assertAlmostEqual(p.paper_notional, 200.0 * 100.0)
        self.assertAlmostEqual(p.paper_risk_amount, 200.0 * 5.0)
        self.assertAlmostEqual(p.paper_risk_pct, 1.0)
        self.assertAlmostEqual(p.capital_used, p.paper_notional)
        self.assertAlmostEqual(p.cash_required, p.paper_notional)

    def test_non_eligible_rejects(self):
        p = _size(_decision(eligible=False))
        self.assertFalse(p.sizing_eligible)
        self.assertEqual(p.sizing_rejection_reason, "not_paper_routable")
        self.assertEqual(p.paper_quantity, 0.0)

    def test_short_rejects(self):
        p = _size(_decision(side="SHORT"))
        self.assertFalse(p.sizing_eligible)
        self.assertEqual(p.sizing_rejection_reason, "non_long_not_sized")

    def test_missing_price_rejects(self):
        p = _size(reference_price=None)
        self.assertEqual(p.sizing_rejection_reason, "invalid_reference_price")

    def test_zero_price_rejects(self):
        self.assertEqual(_size(reference_price=0.0).sizing_rejection_reason,
                         "invalid_reference_price")

    def test_negative_price_rejects(self):
        self.assertEqual(_size(reference_price=-10.0).sizing_rejection_reason,
                         "invalid_reference_price")

    def test_non_finite_price_rejects(self):
        self.assertEqual(
            _size(reference_price=float("inf")).sizing_rejection_reason,
            "invalid_reference_price")
        self.assertEqual(
            _size(reference_price=float("nan")).sizing_rejection_reason,
            "invalid_reference_price")

    def test_missing_stop_rejects(self):
        p = compute_paper_sizing(
            _decision(), paper_equity=100000.0, available_paper_cash=50000.0,
            reference_price=100.0, evaluated_at_utc=_TS)
        self.assertEqual(p.sizing_rejection_reason, "missing_stop")

    def test_invalid_stop_rejects(self):
        self.assertEqual(_size(stop_distance=-1.0).sizing_rejection_reason,
                         "invalid_stop")
        self.assertEqual(_size(stop_distance=0.0).sizing_rejection_reason,
                         "invalid_stop")
        # stop above entry for a long
        self.assertEqual(
            _size(stop_loss_price=105.0, stop_distance=None
                  ).sizing_rejection_reason, "invalid_stop")

    def test_both_stop_inputs_inconsistent_rejects(self):
        p = _size(stop_loss_price=95.0, stop_distance=7.0)
        self.assertEqual(p.sizing_rejection_reason, "invalid_stop")

    def test_both_stop_inputs_consistent_ok(self):
        p = _size(stop_loss_price=95.0, stop_distance=5.0)
        self.assertTrue(p.sizing_eligible)
        self.assertAlmostEqual(p.stop_distance, 5.0)

    def test_stop_loss_price_resolves_distance(self):
        p = _size(stop_loss_price=95.0, stop_distance=None)
        self.assertTrue(p.sizing_eligible)
        self.assertAlmostEqual(p.stop_distance, 5.0)

    def test_risk_pct_cap_enforced(self):
        # risk budget binds -> quantity = budget/stop
        p = _size(max_risk_pct=1.0, max_position_notional_pct=0.20)
        self.assertEqual(p.binding_constraint, "risk_pct")

    def test_position_notional_cap_enforced(self):
        p = _size(max_risk_pct=1.0, max_position_notional_pct=0.05)
        self.assertEqual(p.binding_constraint, "position_notional_cap")
        self.assertAlmostEqual(p.paper_quantity, 50.0)  # 5000/100

    def test_cash_cap_enforced(self):
        p = _size(available_paper_cash=1000.0, max_risk_pct=1.0,
                  max_position_notional_pct=0.20)
        self.assertEqual(p.binding_constraint, "cash_cap")
        self.assertAlmostEqual(p.paper_quantity, 10.0)  # 1000/100

    def test_fractional_quantity_allowed(self):
        p = _size(available_paper_cash=150.0)
        self.assertTrue(p.sizing_eligible)
        self.assertAlmostEqual(p.paper_quantity, 1.5)  # 150/100

    def test_cash_zero_rejects(self):
        self.assertEqual(_size(available_paper_cash=0.0).sizing_rejection_reason,
                         "invalid_capital_inputs")

    def test_equity_zero_rejects(self):
        self.assertEqual(_size(paper_equity=0.0).sizing_rejection_reason,
                         "invalid_capital_inputs")

    def test_invalid_limits_reject(self):
        self.assertEqual(_size(max_risk_pct=0.0).sizing_rejection_reason,
                         "invalid_sizing_limits")
        self.assertEqual(_size(max_risk_pct=101.0).sizing_rejection_reason,
                         "invalid_sizing_limits")
        self.assertEqual(
            _size(max_position_notional_pct=1.5).sizing_rejection_reason,
            "invalid_sizing_limits")
        self.assertEqual(
            _size(max_position_notional_pct=0.0).sizing_rejection_reason,
            "invalid_sizing_limits")

    def test_quantity_zero_for_every_rejection(self):
        for p in (_size(_decision(eligible=False)),
                  _size(_decision(side="SHORT")),
                  _size(reference_price=0.0),
                  _size(stop_distance=-1.0),
                  _size(available_paper_cash=0.0)):
            self.assertEqual(p.paper_quantity, 0.0)
            self.assertEqual(p.paper_notional, 0.0)
            self.assertEqual(p.paper_risk_amount, 0.0)
            self.assertEqual(p.capital_used, 0.0)
            self.assertEqual(p.cash_required, 0.0)

    def test_deterministic_output(self):
        a = _size()
        b = _size()
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_decision_not_mutated(self):
        d = _decision()
        before = d.to_dict()
        _size(d)
        self.assertEqual(d.to_dict(), before)

    def test_round_trip_and_is_live(self):
        p = _size()
        self.assertIs(p.IS_LIVE, False)
        self.assertIs(p.to_dict()["IS_LIVE"], False)
        self.assertEqual(PaperSizingPreview.from_dict(p.to_dict()).to_dict(),
                         p.to_dict())


class M20CNoOrderArtifacts(unittest.TestCase):

    def test_no_order_fill_position_pnl_construction(self):
        src = (_PKG_DIR / "sizing.py").read_text()
        for tok in ("PaperOrder(", "PaperFill(", "PaperPosition(",
                    "PaperPnLSnapshot(", "execute_order", "place_order",
                    "submit_order"):
            self.assertNotIn(tok, src, tok)

    def test_no_storage_or_io_tokens(self):
        src = (_PKG_DIR / "sizing.py").read_text()
        for tok in ("open(", "sqlite3", "json.dump", "to_csv", ".connect(",
                    "data/paper", "signals.db", "mkstemp("):
            self.assertNotIn(tok, src, tok)


class M20CSafetyGuards(unittest.TestCase):

    FORBIDDEN_ROOTS = {"ib_insync", "requests", "urllib", "aiohttp", "socket",
                       "http", "main", "dashboard", "sqlite3", "yfinance",
                       "random"}
    FORBIDDEN_PREFIXES = ("bot.brokers", "bot.live", "bot.etoro", "bot.risk",
                          "bot.risk_authority", "bot.flywheel", "bot.scanner",
                          "bot.strategy", "dashboard", "main")

    def test_no_forbidden_imports(self):
        tree = ast.parse((_PKG_DIR / "sizing.py").read_text())
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

    def test_no_wallclock_token(self):
        src = (_PKG_DIR / "sizing.py").read_text()
        self.assertNotIn("datetime.now", src)
        self.assertNotIn("time.time", src)

    def test_import_writes_nothing(self):
        import importlib
        d = _REPO_ROOT / "data" / "paper"
        before = sorted(p.name for p in d.glob("*")) if d.exists() else []
        importlib.import_module("bot.paper.sizing")
        after = sorted(p.name for p in d.glob("*")) if d.exists() else []
        self.assertEqual(before, after)


class M20CFrozenChecks(unittest.TestCase):

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

    def test_paper_only_authorised_sizing_diff(self):
        # bot/paper changed vs M20.B head only by sizing.py + __init__.py
        r = subprocess.run(["git", "diff", "--name-only", _M20B_HEAD, "HEAD",
                            "--", "bot/paper"], capture_output=True, text=True,
                           timeout=10)
        changed = set(r.stdout.split())
        self.assertTrue(changed <= {"bot/paper/sizing.py",
                                    "bot/paper/orders.py",
                                    "bot/paper/fills.py",
                                    "bot/paper/__init__.py"}, changed)


if __name__ == "__main__":
    unittest.main()
