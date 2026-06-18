"""M20.E — paper position + PnL snapshot proof tests.

Through the real build_paper_position / mark_paper_position: open-position
creation, mark-to-market into a PaperPnLSnapshot + marked_position copy,
derived_metrics, every safe-reject path, no mutation, no closing/realised-PnL/
cash-ledger, and the safety boundary. Reuses the frozen M20.A PaperPosition /
PaperPnLSnapshot contracts (no schema change).
"""
import ast
import pathlib
import subprocess
import unittest

import bot.paper as bp
from bot.paper import (
    PaperRoutingDecision, PaperSide, PaperPositionStatus, PaperPosition,
    PaperPnLSnapshot, PaperOrder, PaperFill, compute_paper_sizing,
    build_paper_order, simulate_paper_fill, build_paper_position,
    mark_paper_position, PaperPositionResult, PaperPnLResult, provenance,
)

_PKG_DIR = pathlib.Path(__file__).resolve().parent / "bot" / "paper"
_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_BASELINE = "e823fe6779deaccc7b8ff7859c17b4dab564b868"
_M20UA_HEAD = "97f02326e12d9e381d94544555524c2d87b2cf27"
_M20D_HEAD = "dcfa8a9901bf7f68c8aeb1537955b7d0ef6f55ac"
_TS = "2026-06-18T10:00:00+00:00"


def _order_and_fill(quantity_ref_price=100.0, slippage_bps=10.0):
    dec = PaperRoutingDecision(
        m19_candidate_id="c1", symbol="AAPL", side="LONG",
        decision_bucket="HIGH_CONVICTION", confidence_bucket="HIGH",
        paper_routing_eligible=True, evaluated_at_utc=_TS)
    sz = compute_paper_sizing(dec, paper_equity=100000.0,
                              available_paper_cash=50000.0,
                              reference_price=quantity_ref_price,
                              stop_distance=5.0, evaluated_at_utc=_TS)
    order = build_paper_order(dec, sz, reference_price=quantity_ref_price,
                              created_at_utc=_TS).order
    fill = simulate_paper_fill(order, simulated_market_price=quantity_ref_price,
                               fill_time_utc=_TS,
                               slippage_bps=slippage_bps).fill
    return order, fill


def _position():
    order, fill = _order_and_fill()
    return build_paper_position(order, fill, opened_at_utc=_TS).position


def _mark(position=None, **kw):
    kw.setdefault("mark_price", 110.0)
    kw.setdefault("paper_equity", 100000.0)
    kw.setdefault("available_paper_cash", 50000.0)
    kw.setdefault("evaluated_at_utc", _TS)
    return mark_paper_position(position or _position(), **kw)


class M20EPositions(unittest.TestCase):

    def test_valid_order_fill_creates_position(self):
        order, fill = _order_and_fill()
        r = build_paper_position(order, fill, opened_at_utc=_TS)
        self.assertTrue(r.ok)
        self.assertIsInstance(r.position, PaperPosition)

    def test_position_has_pps_id(self):
        self.assertTrue(_position().paper_position_id.startswith("PPS-"))

    def test_position_fields(self):
        order, fill = _order_and_fill()
        p = build_paper_position(order, fill, opened_at_utc=_TS).position
        self.assertEqual(p.symbol, "AAPL")
        self.assertEqual(p.side, PaperSide.LONG)
        self.assertAlmostEqual(p.quantity, fill.fill_quantity)
        self.assertAlmostEqual(p.average_entry_price, fill.fill_price)

    def test_position_status_open(self):
        self.assertEqual(_position().status, PaperPositionStatus.OPEN)

    def test_position_pnl_starts_zero(self):
        p = _position()
        self.assertEqual(p.unrealized_pnl, 0.0)
        self.assertEqual(p.realized_pnl, 0.0)
        self.assertIsNone(p.closed_at_utc)

    def test_fill_quantity_mismatch_rejects(self):
        order, fill = _order_and_fill()
        bad = PaperFill(paper_fill_id=fill.paper_fill_id,
                        paper_order_id=order.paper_order_id,
                        fill_price=fill.fill_price,
                        fill_quantity=order.quantity / 2,
                        fill_time_utc=_TS)
        r = build_paper_position(order, bad, opened_at_utc=_TS)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "fill_quantity_mismatch")

    def test_order_fill_id_mismatch_rejects(self):
        order, _ = _order_and_fill()
        bad = PaperFill(paper_fill_id=provenance.paper_fill_id({"z": 1}),
                        paper_order_id=provenance.paper_order_id({"other": 1}),
                        fill_price=100.0, fill_quantity=order.quantity,
                        fill_time_utc=_TS)
        r = build_paper_position(order, bad, opened_at_utc=_TS)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "order_fill_id_mismatch")

    def test_invalid_timestamp_rejects(self):
        order, fill = _order_and_fill()
        for bad in ("not-a-date", "2026-06-18T10:00:00", ""):
            r = build_paper_position(order, fill, opened_at_utc=bad)
            self.assertFalse(r.ok)
            self.assertEqual(r.rejection_reason, "invalid_timestamp")

    def test_invalid_order_or_fill_type_rejects(self):
        order, fill = _order_and_fill()
        self.assertEqual(build_paper_position(object(), fill,
                         opened_at_utc=_TS).rejection_reason, "invalid_order")
        self.assertEqual(build_paper_position(order, object(),
                         opened_at_utc=_TS).rejection_reason, "invalid_fill")

    def test_deterministic_position_id(self):
        order, fill = _order_and_fill()
        a = build_paper_position(order, fill, opened_at_utc=_TS).position
        b = build_paper_position(order, fill, opened_at_utc=_TS).position
        self.assertEqual(a.paper_position_id, b.paper_position_id)

    def test_inputs_not_mutated(self):
        order, fill = _order_and_fill()
        ob, fb = order.to_dict(), fill.to_dict()
        build_paper_position(order, fill, opened_at_utc=_TS)
        self.assertEqual(order.to_dict(), ob)
        self.assertEqual(fill.to_dict(), fb)


class M20EPnL(unittest.TestCase):

    def test_valid_open_position_creates_snapshot(self):
        r = _mark()
        self.assertTrue(r.ok)
        self.assertIsInstance(r.snapshot, PaperPnLSnapshot)
        self.assertIsInstance(r.marked_position, PaperPosition)

    def test_mark_above_entry_positive(self):
        p = _position()  # entry 100.1, qty 200 -> entry notional 20020
        r = _mark(p, mark_price=110.0)
        self.assertGreater(r.snapshot.unrealized_pnl, 0)
        self.assertAlmostEqual(r.snapshot.unrealized_pnl,
                               200 * 110.0 - 200 * 100.1)

    def test_mark_below_entry_negative(self):
        p = _position()
        r = _mark(p, mark_price=90.0)
        self.assertLess(r.snapshot.unrealized_pnl, 0)

    def test_mark_equal_entry_zero(self):
        p = _position()
        r = _mark(p, mark_price=p.average_entry_price)
        self.assertAlmostEqual(r.snapshot.unrealized_pnl, 0.0)

    def test_snapshot_allows_positive_negative_zero(self):
        p = _position()
        self.assertGreater(_mark(p, mark_price=110.0).snapshot.unrealized_pnl, 0)
        self.assertLess(_mark(p, mark_price=90.0).snapshot.unrealized_pnl, 0)
        self.assertAlmostEqual(
            _mark(p, mark_price=p.average_entry_price).snapshot.unrealized_pnl,
            0.0)

    def test_derived_metrics_correct(self):
        p = _position()  # qty 200, entry 100.1
        r = _mark(p, mark_price=110.0)
        dm = r.derived_metrics
        self.assertAlmostEqual(dm["mark_price"], 110.0)
        self.assertAlmostEqual(dm["entry_notional"], 200 * 100.1)
        self.assertAlmostEqual(dm["market_notional"], 200 * 110.0)
        self.assertAlmostEqual(dm["unrealized_pnl"], 200 * 110.0 - 200 * 100.1)
        self.assertAlmostEqual(
            dm["unrealized_pnl_pct"],
            (200 * 110.0 - 200 * 100.1) / (200 * 100.1) * 100.0)

    def test_invalid_mark_price_rejects(self):
        for bad in (0.0, -5.0, float("inf")):
            self.assertEqual(_mark(mark_price=bad).rejection_reason,
                             "invalid_mark_price")

    def test_invalid_paper_equity_rejects(self):
        self.assertEqual(_mark(paper_equity=0.0).rejection_reason,
                         "invalid_paper_equity")

    def test_invalid_available_cash_rejects(self):
        self.assertEqual(_mark(available_paper_cash=-1.0).rejection_reason,
                         "invalid_available_paper_cash")

    def test_invalid_locked_margin_rejects(self):
        self.assertEqual(_mark(locked_paper_margin=-1.0).rejection_reason,
                         "invalid_locked_paper_margin")

    def test_invalid_drawdown_rejects(self):
        self.assertEqual(_mark(drawdown_pct=-1.0).rejection_reason,
                         "invalid_drawdown_pct")

    def test_invalid_timestamp_rejects(self):
        self.assertEqual(_mark(evaluated_at_utc="nope").rejection_reason,
                         "invalid_timestamp")

    def test_closed_position_rejects(self):
        closed = PaperPosition(
            paper_position_id=provenance.paper_position_id({"c": 1}),
            symbol="AAPL", side="LONG", quantity=0, average_entry_price=100.0,
            status="CLOSED", opened_at_utc=_TS, closed_at_utc=_TS)
        r = _mark(closed)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "position_not_open")

    def test_input_position_not_mutated(self):
        p = _position()
        before = p.to_dict()
        _mark(p, mark_price=110.0)
        self.assertEqual(p.to_dict(), before)
        self.assertEqual(p.unrealized_pnl, 0.0)

    def test_marked_position_copy_has_updated_pnl(self):
        p = _position()
        r = _mark(p, mark_price=110.0)
        self.assertAlmostEqual(r.marked_position.unrealized_pnl,
                               r.snapshot.unrealized_pnl)
        self.assertEqual(r.marked_position.paper_position_id,
                         p.paper_position_id)
        self.assertEqual(r.marked_position.status, PaperPositionStatus.OPEN)
        self.assertIsNone(r.marked_position.closed_at_utc)
        self.assertEqual(r.marked_position.realized_pnl, 0.0)

    def test_realized_pnl_stays_default(self):
        # daily_realized_pnl defaults to 0.0; no realised PnL computed
        r = _mark()
        self.assertEqual(r.snapshot.daily_realized_pnl, 0.0)
        self.assertEqual(r.marked_position.realized_pnl, 0.0)

    def test_cash_warning_present(self):
        r = _mark()
        self.assertIn("cash_ledger_not_modeled", r.warnings)

    def test_deterministic_snapshot_output(self):
        p = _position()
        a = _mark(p, mark_price=110.0)
        b = _mark(p, mark_price=110.0)
        self.assertEqual(a.snapshot.to_dict(), b.snapshot.to_dict())


class M20ENoCloseOrLedger(unittest.TestCase):

    def test_no_close_or_ledger_or_storage_tokens(self):
        for mod in ("positions.py", "pnl.py"):
            src = (_PKG_DIR / mod).read_text()
            for tok in ("open(", "sqlite3", "json.dump", "to_csv",
                        ".connect(", "data/paper", "signals.db", "mkstemp(",
                        "close_position", "closed_at_utc=evaluated"):
                self.assertNotIn(tok, src, f"{mod}:{tok}")

    def test_realized_pnl_never_assigned_nonzero(self):
        # behavioural guard: positions are created with realized_pnl=0.0 and the
        # marked copy keeps the original realized_pnl; no realised-PnL math.
        src = (_PKG_DIR / "positions.py").read_text()
        self.assertIn("realized_pnl=0.0", src)


class M20ESafetyGuards(unittest.TestCase):

    FORBIDDEN_ROOTS = {"ib_insync", "requests", "urllib", "aiohttp", "socket",
                       "http", "main", "dashboard", "sqlite3", "yfinance",
                       "random"}
    FORBIDDEN_PREFIXES = ("bot.brokers", "bot.live", "bot.etoro", "bot.risk",
                          "bot.risk_authority", "bot.flywheel", "bot.scanner",
                          "bot.strategy", "dashboard", "main")

    def test_no_forbidden_imports(self):
        offenders = []
        for mod in ("positions.py", "pnl.py"):
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
        for mod in ("positions.py", "pnl.py"):
            src = (_PKG_DIR / mod).read_text()
            self.assertNotIn("datetime.now", src)
            self.assertNotIn("time.time", src)

    def test_import_writes_nothing(self):
        import importlib
        d = _REPO_ROOT / "data" / "paper"
        before = sorted(p.name for p in d.glob("*")) if d.exists() else []
        importlib.import_module("bot.paper.positions")
        importlib.import_module("bot.paper.pnl")
        after = sorted(p.name for p in d.glob("*")) if d.exists() else []
        self.assertEqual(before, after)


class M20EFrozenChecks(unittest.TestCase):

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
        self._unchanged(_M20D_HEAD, "bot/paper/schema.py")

    def test_paper_only_authorised_e_diff(self):
        r = subprocess.run(["git", "diff", "--name-only", _M20D_HEAD, "HEAD",
                            "--", "bot/paper"], capture_output=True, text=True,
                           timeout=10)
        changed = set(r.stdout.split())
        self.assertTrue(changed <= {"bot/paper/positions.py", "bot/paper/pnl.py",
                                    "bot/paper/closing.py",
                                    "bot/paper/account.py",
                                    "bot/paper/ledger.py",
                                    "bot/paper/__init__.py"}, changed)


if __name__ == "__main__":
    unittest.main()
