"""M20.G — paper account + ledger proof tests.

Through the real account transitions (new_account, open_position_in_account,
mark_account, close_position_in_account) and ledger builder (build_account_event):
cash/equity/realised-PnL accounting, single-source realised PnL from
PaperCloseResult, duplicate-close guard, reject-not-clamp on negative cash/equity,
PaperEvent reuse, close-time PaperPnLSnapshot, no mutation, and the safety
boundary. Reuses frozen M20.A PaperEvent + PaperPnLSnapshot (no schema change).
"""
import ast
import pathlib
import subprocess
import unittest

import bot.paper as bp
from bot.paper import (
    new_account, open_position_in_account, mark_account,
    close_position_in_account, build_account_event, close_paper_position,
    PaperAccountState, PaperAccountResult, PaperLedgerResult, PaperPosition,
    PaperEvent, PaperEventType, PaperPnLSnapshot, PaperPositionStatus,
    provenance,
)

_PKG_DIR = pathlib.Path(__file__).resolve().parent / "bot" / "paper"
_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_BASELINE = "e823fe6779deaccc7b8ff7859c17b4dab564b868"
_M20UA_HEAD = "97f02326e12d9e381d94544555524c2d87b2cf27"
_M20F_HEAD = "9157d664943519b9681c76abc7ba3414d336525a"
_T0 = "2026-06-18T10:00:00+00:00"
_T1 = "2026-06-18T11:00:00+00:00"
_T2 = "2026-06-18T12:00:00+00:00"


def _pos(seed, quantity=200.0, average_entry_price=100.0):
    return PaperPosition(
        paper_position_id=provenance.paper_position_id({"s": seed}),
        symbol="AAPL", side="LONG", quantity=quantity,
        average_entry_price=average_entry_price, status="OPEN",
        opened_at_utc=_T0)


def _account(starting_equity=100000.0):
    return new_account(starting_equity=starting_equity,
                       as_of_utc=_T0).account_state


def _opened(starting_equity=100000.0, quantity=200.0, average_entry_price=100.0,
            entry_commission=0.0, seed=1):
    st = _account(starting_equity)
    p = _pos(seed, quantity, average_entry_price)
    notional = quantity * average_entry_price
    r = open_position_in_account(st, p, fill_notional=notional,
                                 entry_commission=entry_commission,
                                 event_time_utc=_T1)
    return r.account_state, p


class M20GAccountState(unittest.TestCase):

    def test_new_account_cash_equals_equity(self):
        st = _account(100000.0)
        self.assertEqual(st.available_paper_cash, 100000.0)
        self.assertEqual(st.starting_equity, 100000.0)
        self.assertEqual(st.open_positions, ())

    def test_is_live_false(self):
        self.assertFalse(_account().IS_LIVE)
        self.assertFalse(_account().to_dict()["IS_LIVE"])

    def test_round_trip(self):
        st, _ = _opened()
        d = st.to_dict()
        self.assertEqual(PaperAccountState.from_dict(d).to_dict(), d)

    def test_from_dict_rejects_unknown(self):
        d = _account().to_dict()
        d["bogus"] = 1
        with self.assertRaises(ValueError):
            PaperAccountState.from_dict(d)

    def test_locked_margin_zero(self):
        self.assertEqual(_account().locked_paper_margin, 0.0)


class M20GOpen(unittest.TestCase):

    def test_open_reduces_cash_by_notional_plus_commission(self):
        st = _account(100000.0)
        p = _pos(1, 200.0, 100.0)
        r = open_position_in_account(st, p, fill_notional=20000.0,
                                     entry_commission=5.0, event_time_utc=_T1)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.account_state.available_paper_cash, 79995.0)

    def test_open_adds_position(self):
        st, p = _opened()
        self.assertEqual(len(st.open_positions), 1)
        self.assertEqual(st.open_positions[0].paper_position_id,
                         p.paper_position_id)

    def test_open_insufficient_cash_rejects(self):
        st = _account(10000.0)
        p = _pos(1, 2000.0, 100.0)
        r = open_position_in_account(st, p, fill_notional=200000.0,
                                     event_time_utc=_T1)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "insufficient_cash")

    def test_open_duplicate_id_rejects(self):
        st, p = _opened()
        r = open_position_in_account(st, p, fill_notional=20000.0,
                                     event_time_utc=_T1)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "duplicate_position_id")

    def test_commission_reduces_cash(self):
        st = _account(100000.0)
        p = _pos(1, 200.0, 100.0)
        no_comm = open_position_in_account(
            st, p, fill_notional=20000.0, entry_commission=0.0,
            event_time_utc=_T1).account_state.available_paper_cash
        with_comm = open_position_in_account(
            st, p, fill_notional=20000.0, entry_commission=10.0,
            event_time_utc=_T1).account_state.available_paper_cash
        self.assertAlmostEqual(no_comm - with_comm, 10.0)

    def test_open_emits_paper_event(self):
        st = _account()
        p = _pos(1)
        r = open_position_in_account(st, p, fill_notional=20000.0,
                                     event_time_utc=_T1)
        self.assertEqual(len(r.events), 1)
        ev = r.events[0]
        self.assertIsInstance(ev, PaperEvent)
        self.assertTrue(ev.paper_event_id.startswith("PEV-"))
        self.assertEqual(ev.event_type, PaperEventType.POSITION_OPENED)
        self.assertIn("cash_delta", ev.detail)

    def test_open_does_not_mutate_input_state(self):
        st = _account()
        before = st.to_dict()
        open_position_in_account(st, _pos(1), fill_notional=20000.0,
                                 event_time_utc=_T1)
        self.assertEqual(st.to_dict(), before)


class M20GMark(unittest.TestCase):

    def test_mark_updates_unrealized_and_equity(self):
        st, p = _opened(quantity=200.0, average_entry_price=100.0)
        m = mark_account(st, {p.paper_position_id: 110.0},
                         evaluated_at_utc=_T1)
        self.assertTrue(m.ok)
        self.assertAlmostEqual(m.derived_metrics["unrealized_pnl"], 2000.0)
        # cash 80000 + 200*110 = 102000
        self.assertAlmostEqual(m.snapshot.total_paper_equity, 102000.0)

    def test_mark_does_not_change_cash(self):
        st, p = _opened()
        cash_before = st.available_paper_cash
        m = mark_account(st, {p.paper_position_id: 110.0},
                         evaluated_at_utc=_T1)
        self.assertEqual(m.account_state.available_paper_cash, cash_before)

    def test_mark_requires_marks_for_all_open(self):
        st, p = _opened()
        r = mark_account(st, {}, evaluated_at_utc=_T1)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "marks_mismatch_open_positions")

    def test_mark_rejects_invalid_mark(self):
        st, p = _opened()
        for bad in (0.0, -5.0):
            r = mark_account(st, {p.paper_position_id: bad},
                             evaluated_at_utc=_T1)
            self.assertFalse(r.ok)
            self.assertEqual(r.rejection_reason, "invalid_mark_price")

    def test_multi_position_aggregate(self):
        st = _account(100000.0)
        pa = _pos("x", 100.0, 50.0)   # notional 5000
        pb = _pos("y", 200.0, 100.0)  # notional 20000
        st = open_position_in_account(st, pa, fill_notional=5000.0,
                                      event_time_utc=_T1).account_state
        st = open_position_in_account(st, pb, fill_notional=20000.0,
                                      event_time_utc=_T1).account_state
        m = mark_account(st, {pa.paper_position_id: 55.0,
                              pb.paper_position_id: 110.0},
                         evaluated_at_utc=_T1)
        # upnl = 100*5 + 200*10 = 2500; cash=75000; omv=5500+22000=27500
        self.assertAlmostEqual(m.derived_metrics["unrealized_pnl"], 2500.0)
        self.assertAlmostEqual(m.snapshot.total_paper_equity, 102500.0)


class M20GClose(unittest.TestCase):

    def _close_result(self, position, exit_price=110.0, entry_commission=0.0,
                      exit_commission=0.0):
        return close_paper_position(position, exit_price=exit_price,
                                    closed_at_utc=_T2,
                                    entry_commission=entry_commission,
                                    exit_commission=exit_commission)

    def test_close_removes_position(self):
        st, p = _opened()
        cr = self._close_result(p)
        r = close_position_in_account(st, cr, event_time_utc=_T2)
        self.assertTrue(r.ok)
        self.assertEqual(len(r.account_state.open_positions), 0)

    def test_close_records_processed_id(self):
        st, p = _opened()
        r = close_position_in_account(st, self._close_result(p),
                                      event_time_utc=_T2)
        self.assertIn(p.paper_position_id,
                      r.account_state.processed_close_ids)

    def test_close_adds_exit_notional_minus_commission(self):
        st, p = _opened(quantity=200.0, average_entry_price=100.0)
        # cash after open = 80000; exit 110 -> exit_notional 22000, comm 3
        cr = self._close_result(p, exit_price=110.0, exit_commission=3.0)
        r = close_position_in_account(st, cr, event_time_utc=_T2)
        self.assertAlmostEqual(r.account_state.available_paper_cash,
                               80000.0 + 22000.0 - 3.0)

    def test_realized_pnl_from_close_result_only(self):
        st, p = _opened(quantity=200.0, average_entry_price=100.0)
        cr = self._close_result(p, exit_price=110.0, entry_commission=5.0,
                                exit_commission=3.0)
        # gross 2000 - 8 = 1992
        self.assertAlmostEqual(cr.closed_position.realized_pnl, 1992.0)
        r = close_position_in_account(st, cr, event_time_utc=_T2)
        self.assertAlmostEqual(r.account_state.realized_pnl_cumulative, 1992.0)
        self.assertAlmostEqual(r.derived_metrics["net_realized_pnl"], 1992.0)

    def test_closed_realized_equals_net(self):
        st, p = _opened()
        cr = self._close_result(p, exit_price=120.0)
        self.assertAlmostEqual(cr.closed_position.realized_pnl,
                               cr.derived_metrics["net_realized_pnl"])
        r = close_position_in_account(st, cr, event_time_utc=_T2)
        self.assertAlmostEqual(r.account_state.realized_pnl_cumulative,
                               cr.derived_metrics["net_realized_pnl"])

    def test_realized_cumulative_updates_once(self):
        st, p = _opened()
        cr = self._close_result(p, exit_price=110.0)
        r1 = close_position_in_account(st, cr, event_time_utc=_T2)
        first = r1.account_state.realized_pnl_cumulative
        r2 = close_position_in_account(r1.account_state, cr,
                                       event_time_utc=_T2)
        self.assertFalse(r2.ok)
        self.assertEqual(r2.rejection_reason, "already_closed_in_account")
        # cumulative unchanged after rejected duplicate
        self.assertEqual(r1.account_state.realized_pnl_cumulative, first)

    def test_duplicate_close_no_state_change(self):
        st, p = _opened()
        cr = self._close_result(p)
        r1 = close_position_in_account(st, cr, event_time_utc=_T2)
        r2 = close_position_in_account(r1.account_state, cr,
                                       event_time_utc=_T2)
        self.assertFalse(r2.ok)
        self.assertIsNone(r2.account_state)

    def test_close_unknown_position_rejects(self):
        st = _account()
        p = _pos("never_opened")
        cr = self._close_result(p)
        r = close_position_in_account(st, cr, event_time_utc=_T2)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "position_not_in_account")

    def test_close_snapshot_honest(self):
        st, p = _opened(quantity=200.0, average_entry_price=100.0)
        cr = self._close_result(p, exit_price=110.0, exit_commission=3.0)
        r = close_position_in_account(st, cr, event_time_utc=_T2)
        self.assertIsInstance(r.snapshot, PaperPnLSnapshot)
        self.assertGreaterEqual(r.snapshot.total_paper_equity, 0.0)
        self.assertGreaterEqual(r.snapshot.available_paper_cash, 0.0)
        self.assertEqual(r.snapshot.locked_paper_margin, 0.0)
        self.assertAlmostEqual(r.snapshot.available_paper_cash,
                               r.account_state.available_paper_cash)

    def test_close_emits_paper_event(self):
        st, p = _opened()
        r = close_position_in_account(st, self._close_result(p),
                                      event_time_utc=_T2)
        ev = r.events[0]
        self.assertTrue(ev.paper_event_id.startswith("PEV-"))
        self.assertEqual(ev.event_type, PaperEventType.POSITION_CLOSED)
        self.assertIn("net_realized_pnl", ev.detail)
        self.assertIn("cash_delta", ev.detail)

    def test_close_does_not_mutate_input_state(self):
        st, p = _opened()
        before = st.to_dict()
        close_position_in_account(st, self._close_result(p),
                                  event_time_utc=_T2)
        self.assertEqual(st.to_dict(), before)

    def test_invalid_close_result_rejects(self):
        st, _ = _opened()
        bad = close_paper_position(_pos(1), exit_price=0.0, closed_at_utc=_T2)
        r = close_position_in_account(st, bad, event_time_utc=_T2)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "invalid_close_result")


class M20GNegativeGuards(unittest.TestCase):

    def test_negative_cash_open_rejects(self):
        st = _account(100.0)
        p = _pos(1, 200.0, 100.0)
        r = open_position_in_account(st, p, fill_notional=20000.0,
                                     event_time_utc=_T1)
        self.assertEqual(r.rejection_reason, "insufficient_cash")

    def test_would_overdraw_on_close_rejects(self):
        # craft a close whose negative cash delta exceeds available cash:
        # open uses most of cash, exit_notional tiny, exit_commission huge.
        st = _account(100000.0)
        p = _pos(1, 200.0, 100.0)
        st = open_position_in_account(st, p, fill_notional=20000.0,
                                      event_time_utc=_T1).account_state
        # exit_notional - exit_commission must be < -cash(80000) to overdraw;
        # impossible with exit_price>0 & commission>=0 unless commission huge.
        cr = close_paper_position(p, exit_price=1.0, closed_at_utc=_T2,
                                  exit_commission=80000.0 + 200.0 + 1.0)
        r = close_position_in_account(st, cr, event_time_utc=_T2)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "would_overdraw_cash")


class M20GLedger(unittest.TestCase):

    def test_build_event_reuses_paper_event(self):
        r = build_account_event(event_type="PNL_COMPUTED",
                                event_time_utc=_T1,
                                detail={"x": 1})
        self.assertTrue(r.ok)
        self.assertIsInstance(r.event, PaperEvent)
        self.assertTrue(r.event.paper_event_id.startswith("PEV-"))

    def test_build_event_deterministic(self):
        a = build_account_event(event_type="POSITION_OPENED",
                                event_time_utc=_T1, detail={"cash_delta": -5})
        b = build_account_event(event_type="POSITION_OPENED",
                                event_time_utc=_T1, detail={"cash_delta": -5})
        self.assertEqual(a.event.paper_event_id, b.event.paper_event_id)

    def test_build_event_invalid_type_rejects(self):
        r = build_account_event(event_type="NOT_A_TYPE", event_time_utc=_T1)
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "invalid_event_type")

    def test_build_event_invalid_timestamp_rejects(self):
        r = build_account_event(event_type="PNL_COMPUTED", event_time_utc="x")
        self.assertFalse(r.ok)
        self.assertEqual(r.rejection_reason, "invalid_timestamp")


class M20GReconciliation(unittest.TestCase):

    def test_equity_reconciles_after_round_trip(self):
        st = _account(100000.0)
        p = _pos(1, 200.0, 100.0)
        st = open_position_in_account(st, p, fill_notional=20000.0,
                                      entry_commission=5.0,
                                      event_time_utc=_T1).account_state
        cr = close_paper_position(p, exit_price=110.0, closed_at_utc=_T2,
                                  entry_commission=5.0, exit_commission=3.0)
        st = close_position_in_account(st, cr, event_time_utc=_T2).account_state
        # all flat: equity == cash == starting + realized_cumulative
        self.assertAlmostEqual(
            st.available_paper_cash,
            st.starting_equity + st.realized_pnl_cumulative)


class M20GNoStorageOrIO(unittest.TestCase):

    def test_no_storage_or_io_tokens(self):
        for mod in ("account.py", "ledger.py"):
            src = (_PKG_DIR / mod).read_text()
            for tok in ("open(", "sqlite3", "json.dump", "to_csv", ".connect(",
                        "data/paper", "signals.db", "mkstemp(", ".write(",
                        "Path(", "jsonl"):
                self.assertNotIn(tok, src, f"{mod}:{tok}")


class M20GSafetyGuards(unittest.TestCase):

    FORBIDDEN_ROOTS = {"ib_insync", "requests", "urllib", "aiohttp", "socket",
                       "http", "main", "dashboard", "sqlite3", "yfinance",
                       "random"}
    FORBIDDEN_PREFIXES = ("bot.brokers", "bot.live", "bot.etoro", "bot.risk",
                          "bot.risk_authority", "bot.flywheel", "bot.scanner",
                          "bot.strategy", "dashboard", "main")

    def test_no_forbidden_imports(self):
        offenders = []
        for mod in ("account.py", "ledger.py"):
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
        for mod in ("account.py", "ledger.py"):
            src = (_PKG_DIR / mod).read_text()
            self.assertNotIn("datetime.now", src)
            self.assertNotIn("time.time", src)

    def test_import_writes_nothing(self):
        import importlib
        d = _REPO_ROOT / "data" / "paper"
        before = sorted(x.name for x in d.glob("*")) if d.exists() else []
        importlib.import_module("bot.paper.account")
        importlib.import_module("bot.paper.ledger")
        after = sorted(x.name for x in d.glob("*")) if d.exists() else []
        self.assertEqual(before, after)


class M20GFrozenChecks(unittest.TestCase):

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
        self._unchanged(_M20F_HEAD, "bot/paper/schema.py")

    def test_paper_only_authorised_g_diff(self):
        r = subprocess.run(["git", "diff", "--name-only", _M20F_HEAD, "HEAD",
                            "--", "bot/paper"], capture_output=True, text=True,
                           timeout=10)
        changed = set(r.stdout.split())
        self.assertTrue(changed <= {"bot/paper/account.py", "bot/paper/ledger.py",
                                    "bot/paper/__init__.py"}, changed)


if __name__ == "__main__":
    unittest.main()
