"""M20.F — paper position closing + realised PnL proof tests.

Through the real close_paper_position: full close of an OPEN LONG position into a
CLOSED copy with realised PnL, every safe-reject path, no mutation, no snapshot/
cash/ledger/storage, and the safety boundary. Reuses the frozen M20.A
PaperPosition contract (no schema change).
"""
import ast
import pathlib
import subprocess
import unittest

import bot.paper as bp
from bot.paper import (
    close_paper_position, PaperCloseResult, PaperPosition, PaperPositionStatus,
    PaperSide, provenance,
)

_PKG_DIR = pathlib.Path(__file__).resolve().parent / "bot" / "paper"
_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_BASELINE = "e823fe6779deaccc7b8ff7859c17b4dab564b868"
# M20.UE flag-gated selection seam commit (approved; main.py sha256-pinned).
_M20UE_HEAD = "d077260d189a8fe6927b7c994f45872800df243a"
_M20UA_HEAD = "97f02326e12d9e381d94544555524c2d87b2cf27"
_M20E_HEAD = "d19001dc708586a8c83c866a9c4c591d1ab3e612"
_OPEN = "2026-06-18T10:00:00+00:00"
_LATER = "2026-06-18T16:00:00+00:00"
_EARLIER = "2026-06-18T09:00:00+00:00"


def _position(status="OPEN", quantity=200.0, average_entry_price=100.1,
              side="LONG"):
    return PaperPosition(
        paper_position_id=provenance.paper_position_id({"x": 1}),
        symbol="AAPL", side=side, quantity=quantity,
        average_entry_price=average_entry_price, status=status,
        opened_at_utc=_OPEN,
        closed_at_utc=(_OPEN if status == "CLOSED" else None))


def _close(position=None, **kw):
    kw.setdefault("exit_price", 110.0)
    kw.setdefault("closed_at_utc", _LATER)
    return close_paper_position(position or _position(), **kw)


class M20FClosing(unittest.TestCase):

    def test_valid_close_succeeds(self):
        r = _close()
        self.assertTrue(r.ok)
        self.assertIsInstance(r.closed_position, PaperPosition)

    def test_closed_position_same_id(self):
        p = _position()
        r = _close(p)
        self.assertEqual(r.closed_position.paper_position_id,
                         p.paper_position_id)

    def test_closed_position_keeps_quantity(self):
        p = _position(quantity=200.0)
        r = _close(p)
        self.assertEqual(r.closed_position.quantity, 200.0)

    def test_closed_status(self):
        self.assertEqual(_close().closed_position.status,
                         PaperPositionStatus.CLOSED)

    def test_closed_at_set(self):
        self.assertEqual(_close().closed_position.closed_at_utc, _LATER)

    def test_unrealized_becomes_zero(self):
        self.assertEqual(_close().closed_position.unrealized_pnl, 0.0)

    def test_realized_equals_net(self):
        r = _close(exit_price=110.0, entry_commission=1.0, exit_commission=2.0)
        # entry 200*100.1=20020; exit 200*110=22000; gross 1980; net 1977
        self.assertAlmostEqual(r.closed_position.realized_pnl, 1977.0)
        self.assertAlmostEqual(r.derived_metrics["net_realized_pnl"], 1977.0)

    def test_gross_realized_correct(self):
        r = _close(exit_price=110.0)
        self.assertAlmostEqual(r.derived_metrics["gross_realized_pnl"],
                               200 * 110.0 - 200 * 100.1)

    def test_net_realized_correct(self):
        r = _close(exit_price=110.0, entry_commission=5.0, exit_commission=7.0)
        gross = 200 * 110.0 - 200 * 100.1
        self.assertAlmostEqual(r.derived_metrics["net_realized_pnl"],
                               gross - 12.0)
        self.assertAlmostEqual(r.derived_metrics["total_commission"], 12.0)

    def test_realized_pnl_pct_correct(self):
        r = _close(exit_price=110.0, entry_commission=1.0, exit_commission=2.0)
        entry_notional = 200 * 100.1
        net = (200 * 110.0 - 200 * 100.1) - 3.0
        self.assertAlmostEqual(r.derived_metrics["realized_pnl_pct"],
                               net / entry_notional * 100.0)

    def test_commission_deduction(self):
        no_comm = _close(exit_price=110.0).derived_metrics["net_realized_pnl"]
        with_comm = _close(exit_price=110.0, entry_commission=10.0,
                           exit_commission=5.0
                           ).derived_metrics["net_realized_pnl"]
        self.assertAlmostEqual(no_comm - with_comm, 15.0)

    def test_zero_commissions_accepted(self):
        r = _close(exit_price=110.0, entry_commission=0.0, exit_commission=0.0)
        self.assertTrue(r.ok)

    def test_exit_above_entry_positive(self):
        self.assertGreater(
            _close(exit_price=120.0).derived_metrics["gross_realized_pnl"], 0)

    def test_exit_below_entry_negative(self):
        self.assertLess(
            _close(exit_price=90.0).derived_metrics["gross_realized_pnl"], 0)

    def test_exit_equal_entry_zero_gross(self):
        self.assertAlmostEqual(
            _close(exit_price=100.1).derived_metrics["gross_realized_pnl"], 0.0)

    def test_derived_metrics_keys(self):
        dm = _close().derived_metrics
        for k in ("entry_notional", "exit_notional", "gross_realized_pnl",
                  "entry_commission", "exit_commission", "total_commission",
                  "net_realized_pnl", "realized_pnl_pct", "exit_price"):
            self.assertIn(k, dm)


class M20FRejections(unittest.TestCase):

    def test_invalid_position_type(self):
        self.assertEqual(_close(object()).rejection_reason, "invalid_position")

    def test_closed_position_rejects(self):
        r = _close(_position(status="CLOSED", quantity=0.0))
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "position_not_open")

    def test_non_long_rejects(self):
        self.assertEqual(_close(_position(side="SHORT")).rejection_reason,
                         "non_long_not_closed")

    def test_non_positive_quantity_rejects(self):
        # quantity 0 with status OPEN -> non_positive_quantity
        self.assertEqual(
            _close(_position(quantity=0.0, average_entry_price=100.0)
                   ).rejection_reason, "non_positive_quantity")

    def test_non_positive_entry_price_rejects(self):
        # construct a position with qty>0 requires avg>0; use a tiny qty>0 but
        # avg 0 is rejected by schema, so simulate via a valid object then check
        # the close guard catches a zero avg if it somehow appears.
        p = _position(quantity=200.0, average_entry_price=100.0)
        object.__setattr__(p, "average_entry_price", 0.0)
        self.assertEqual(_close(p).rejection_reason, "non_positive_entry_price")

    def test_invalid_exit_price_rejects(self):
        for bad in (0.0, -5.0, float("inf"), float("nan")):
            self.assertEqual(_close(exit_price=bad).rejection_reason,
                             "invalid_exit_price")

    def test_negative_entry_commission_rejects(self):
        self.assertEqual(_close(entry_commission=-1.0).rejection_reason,
                         "negative_entry_commission")

    def test_negative_exit_commission_rejects(self):
        self.assertEqual(_close(exit_commission=-1.0).rejection_reason,
                         "negative_exit_commission")

    def test_invalid_timestamp_rejects(self):
        for bad in ("nope", "2026-06-18T16:00:00", ""):
            self.assertEqual(_close(closed_at_utc=bad).rejection_reason,
                             "invalid_timestamp")

    def test_close_before_open_rejects(self):
        self.assertEqual(_close(closed_at_utc=_EARLIER).rejection_reason,
                         "close_before_open")

    def test_close_equal_open_accepted(self):
        self.assertTrue(_close(closed_at_utc=_OPEN).ok)

    def test_no_closed_position_on_rejection(self):
        for r in (_close(exit_price=0.0), _close(_position(side="SHORT")),
                  _close(closed_at_utc=_EARLIER)):
            self.assertFalse(r.ok)
            self.assertIsNone(r.closed_position)


class M20FNoMutation(unittest.TestCase):

    def test_input_not_mutated(self):
        p = _position()
        before = p.to_dict()
        _close(p)
        self.assertEqual(p.to_dict(), before)
        self.assertEqual(p.status, PaperPositionStatus.OPEN)
        self.assertEqual(p.realized_pnl, 0.0)
        self.assertIsNone(p.closed_at_utc)

    def test_closed_position_is_copy(self):
        p = _position()
        r = _close(p)
        self.assertIsNot(r.closed_position, p)

    def test_deterministic_output(self):
        a = _close(_position(), exit_price=110.0)
        b = _close(_position(), exit_price=110.0)
        self.assertEqual(a.closed_position.to_dict(),
                         b.closed_position.to_dict())
        self.assertEqual(a.derived_metrics, b.derived_metrics)


class M20FNoLedgerOrSnapshot(unittest.TestCase):

    def test_no_snapshot_ledger_or_storage_tokens(self):
        src = (_PKG_DIR / "closing.py").read_text()
        for tok in ("PaperPnLSnapshot", "available_paper_cash",
                    "total_paper_equity", "open(", "sqlite3", "json.dump",
                    "to_csv", ".connect(", "data/paper", "signals.db",
                    "mkstemp(", "partial", "scale_out"):
            self.assertNotIn(tok, src, tok)


class M20FSafetyGuards(unittest.TestCase):

    FORBIDDEN_ROOTS = {"ib_insync", "requests", "urllib", "aiohttp", "socket",
                       "http", "main", "dashboard", "sqlite3", "yfinance",
                       "random"}
    FORBIDDEN_PREFIXES = ("bot.brokers", "bot.live", "bot.etoro", "bot.risk",
                          "bot.risk_authority", "bot.flywheel", "bot.scanner",
                          "bot.strategy", "dashboard", "main")

    def test_no_forbidden_imports(self):
        tree = ast.parse((_PKG_DIR / "closing.py").read_text())
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
        src = (_PKG_DIR / "closing.py").read_text()
        self.assertNotIn("datetime.now", src)
        self.assertNotIn("time.time", src)

    def test_import_writes_nothing(self):
        import importlib
        d = _REPO_ROOT / "data" / "paper"
        before = sorted(p.name for p in d.glob("*")) if d.exists() else []
        importlib.import_module("bot.paper.closing")
        after = sorted(p.name for p in d.glob("*")) if d.exists() else []
        self.assertEqual(before, after)


class M20FFrozenChecks(unittest.TestCase):

    def _unchanged(self, baseline, *paths):
        r = subprocess.run(["git", "diff", "--name-only", baseline, "HEAD",
                            "--", *paths], capture_output=True, text=True,
                           timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "", f"{paths} changed vs {baseline}")

    def test_m19_frozen(self):
        self._unchanged(_BASELINE, "bot/signal_scoring")

    def test_m20ua_frozen(self):
        # M20.UA universe CODE stays frozen; M20.UB authorises universe
        # data/docs changes (us_seed/us_expanded/UNIVERSE_STATUS.md), so guard
        # the code modules rather than the whole tree.
        self._unchanged(_M20UA_HEAD, "bot/universe/schema.py",
                        "bot/universe/registry.py", "bot/universe/suffixes.py")

    def test_protected_runtime_unchanged(self):
        self._unchanged(_M20UE_HEAD, "main.py")
        self._unchanged(_BASELINE, "bot/scanner.py", "bot/risk.py",
                        "bot/strategy.py", "dashboard/app.py", "bot/brokers",
                        "bot/flywheel.py")

    def test_schema_unchanged(self):
        self._unchanged(_M20E_HEAD, "bot/paper/schema.py")

    def test_paper_only_authorised_f_diff(self):
        r = subprocess.run(["git", "diff", "--name-only", _M20E_HEAD, "HEAD",
                            "--", "bot/paper"], capture_output=True, text=True,
                           timeout=10)
        changed = set(r.stdout.split())
        self.assertTrue(changed <= {"bot/paper/closing.py",
                                    "bot/paper/account.py",
                                    "bot/paper/ledger.py",
                                    "bot/paper/storage.py",
                                    "bot/paper/__init__.py"}, changed)


if __name__ == "__main__":
    unittest.main()
