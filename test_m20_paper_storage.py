"""M20.H — paper storage (JSONL) proof tests.

All writes go to tempfile directories; the real repo data/paper/ must stay empty.
Covers the three append/load round-trips, append-additivity, id-dedup, canonical
formatting, missing/corrupt/empty-line handling, handle closing, no import-time
writes, the lightweight replay summary, and the safety boundary. Reuses the
frozen schemas' to_dict/from_dict (no schema change).
"""
import ast
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

import bot.paper as bp
from bot.paper import (
    PaperEvent, PaperEventType, PaperPnLSnapshot, PaperAccountState,
    new_account, provenance, PaperStorageResult,
    append_events, load_events, append_snapshots, load_snapshots,
    append_account_states, load_account_states, replay_events_summary,
)

_PKG_DIR = pathlib.Path(__file__).resolve().parent / "bot" / "paper"
_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_REAL_PAPER_DIR = _REPO_ROOT / "data" / "paper"
_BASELINE = "e823fe6779deaccc7b8ff7859c17b4dab564b868"
_M20UA_HEAD = "97f02326e12d9e381d94544555524c2d87b2cf27"
_M20G_HEAD = "cfea38667dba32648f244b37c0f281a3383a105e"
_T0 = "2026-06-18T10:00:00+00:00"
_T1 = "2026-06-18T11:00:00+00:00"
_T2 = "2026-06-18T12:00:00+00:00"


def _event(seed, event_type=PaperEventType.POSITION_OPENED, detail=None):
    return PaperEvent(
        paper_event_id=provenance.paper_event_id({"s": seed}),
        event_time_utc=_T0, event_type=event_type, m19_candidate_id="account",
        detail=detail or {"cash_delta": -1.0}, reason_codes=[])


def _snapshot(ts=_T1):
    return PaperPnLSnapshot(timestamp_utc=ts, total_paper_equity=100000.0,
                            available_paper_cash=90000.0, unrealized_pnl=10.0)


def _account(ts=_T0):
    return new_account(starting_equity=100000.0, as_of_utc=ts).account_state


class M20HEvents(unittest.TestCase):

    def test_append_and_load_round_trip(self):
        e1, e2 = _event("a"), _event("b", PaperEventType.POSITION_CLOSED,
                                      {"net_realized_pnl": 5.0})
        with tempfile.TemporaryDirectory() as d:
            w = append_events([e1, e2], directory=d)
            self.assertTrue(w.ok)
            self.assertEqual(w.written, 2)
            r = load_events(w.path)
            self.assertTrue(r.ok)
            self.assertEqual([x.to_dict() for x in r.records],
                             [e1.to_dict(), e2.to_dict()])

    def test_records_are_paper_events(self):
        with tempfile.TemporaryDirectory() as d:
            w = append_events([_event("a")], directory=d)
            r = load_events(w.path)
            self.assertIsInstance(r.records[0], PaperEvent)

    def test_append_is_additive(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "events.jsonl")
            append_events([_event("a")], path=p)
            append_events([_event("b")], path=p)
            self.assertEqual(load_events(p).loaded, 2)

    def test_duplicate_id_skipped(self):
        e = _event("a")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "events.jsonl")
            append_events([e], path=p)
            w = append_events([e, _event("b")], path=p)
            self.assertEqual(w.written, 1)
            self.assertEqual(w.duplicate_skipped, 1)
            self.assertEqual(load_events(p).loaded, 2)

    def test_canonical_format(self):
        e = _event("a")
        with tempfile.TemporaryDirectory() as d:
            w = append_events([e], directory=d)
            with open(w.path) as fh:
                line = fh.readline().rstrip("\n")
            self.assertEqual(line, json.dumps(e.to_dict(), sort_keys=True,
                                              separators=(",", ":")))

    def test_invalid_record_type_rejects(self):
        with tempfile.TemporaryDirectory() as d:
            w = append_events([_snapshot()], directory=d)
            self.assertFalse(w.ok)
            self.assertEqual(w.rejection_reason, "invalid_record_type")


class M20HSnapshots(unittest.TestCase):

    def test_round_trip(self):
        s = _snapshot()
        with tempfile.TemporaryDirectory() as d:
            w = append_snapshots([s], directory=d)
            self.assertTrue(w.path.endswith("snapshots.jsonl"))
            r = load_snapshots(w.path)
            self.assertEqual([x.to_dict() for x in r.records], [s.to_dict()])
            self.assertIsInstance(r.records[0], PaperPnLSnapshot)

    def test_duplicate_timestamp_skipped(self):
        s = _snapshot(_T1)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "snapshots.jsonl")
            append_snapshots([s], path=p)
            w = append_snapshots([s], path=p)
            self.assertEqual(w.written, 0)
            self.assertEqual(w.duplicate_skipped, 1)


class M20HAccountStates(unittest.TestCase):

    def test_round_trip(self):
        st = _account()
        with tempfile.TemporaryDirectory() as d:
            w = append_account_states([st], directory=d)
            self.assertTrue(w.path.endswith("account_state.jsonl"))
            r = load_account_states(w.path)
            self.assertEqual([x.to_dict() for x in r.records], [st.to_dict()])
            self.assertIsInstance(r.records[0], PaperAccountState)

    def test_duplicate_as_of_skipped(self):
        st = _account(_T0)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "account_state.jsonl")
            append_account_states([st], path=p)
            w = append_account_states([st], path=p)
            self.assertEqual(w.written, 0)
            self.assertEqual(w.duplicate_skipped, 1)


class M20HLoaderRobustness(unittest.TestCase):

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            r = load_events(os.path.join(d, "nope.jsonl"))
            self.assertFalse(r.ok)
            self.assertEqual(r.rejection_reason, "file_not_found")

    def test_corrupt_line_with_line_number(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "events.jsonl")
            with open(p, "w") as fh:
                fh.write(json.dumps(_event("a").to_dict()) + "\n{bad\n")
            r = load_events(p)
            self.assertFalse(r.ok)
            self.assertEqual(r.rejection_reason, "corrupt_record")
            self.assertEqual(r.derived_metrics["line_number"], 2)

    def test_empty_lines_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "events.jsonl")
            with open(p, "w") as fh:
                fh.write(json.dumps(_event("a").to_dict()) + "\n\n   \n"
                         + json.dumps(_event("b").to_dict()) + "\n")
            self.assertEqual(load_events(p).loaded, 2)

    def test_loader_closes_handles(self):
        import warnings
        with tempfile.TemporaryDirectory() as d:
            w = append_events([_event("a")], directory=d)
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                load_events(w.path)


class M20HNoImportWrites(unittest.TestCase):

    def test_import_creates_no_files(self):
        before = (sorted(p.name for p in _REAL_PAPER_DIR.glob("*"))
                  if _REAL_PAPER_DIR.exists() else None)
        import importlib
        importlib.import_module("bot.paper.storage")
        after = (sorted(p.name for p in _REAL_PAPER_DIR.glob("*"))
                 if _REAL_PAPER_DIR.exists() else None)
        self.assertEqual(before, after)

    def test_explicit_write_creates_parent_lazily(self):
        with tempfile.TemporaryDirectory() as d:
            nested = os.path.join(d, "deep", "paper")
            self.assertFalse(os.path.exists(nested))
            w = append_events([_event("a")], directory=nested)
            self.assertTrue(w.ok)
            self.assertTrue(os.path.exists(os.path.join(nested,
                                                        "events.jsonl")))

    def test_real_data_paper_remains_empty(self):
        n = (len([p for p in _REAL_PAPER_DIR.glob("*") if p.is_file()])
             if _REAL_PAPER_DIR.exists() else 0)
        self.assertEqual(n, 0)


class M20HReplaySummary(unittest.TestCase):

    def test_summary_from_detail_only(self):
        e_open = _event("a", PaperEventType.POSITION_OPENED,
                        {"cash_delta": -20000.0, "paper_position_id": "PPS-a"})
        e_close = _event("b", PaperEventType.POSITION_CLOSED,
                         {"cash_delta": 21997.0, "net_realized_pnl": 1997.0,
                          "paper_position_id": "PPS-a"})
        r = replay_events_summary([e_open, e_close])
        dm = r.derived_metrics
        self.assertAlmostEqual(dm["cash_delta_total"], 1997.0)
        self.assertAlmostEqual(dm["realized_pnl_total"], 1997.0)
        self.assertEqual(dm["open_position_ids"], [])
        self.assertEqual(dm["closed_position_ids"], ["PPS-a"])
        self.assertEqual(dm["event_count"], 2)

    def test_open_without_close_remains_open(self):
        e_open = _event("a", PaperEventType.POSITION_OPENED,
                        {"cash_delta": -5000.0, "paper_position_id": "PPS-x"})
        r = replay_events_summary([e_open])
        self.assertEqual(r.derived_metrics["open_position_ids"], ["PPS-x"])

    def test_summary_deterministic(self):
        evs = [_event("a", PaperEventType.POSITION_OPENED,
                      {"cash_delta": -1.0, "paper_position_id": "PPS-a"})]
        self.assertEqual(replay_events_summary(evs).derived_metrics,
                         replay_events_summary(evs).derived_metrics)

    def test_no_price_recompute_token(self):
        src = (_PKG_DIR / "storage.py").read_text()
        # replay must not reference price/mark fields for PnL recomputation
        self.assertNotIn("average_entry_price", src)
        self.assertNotIn("mark_price", src)
        self.assertNotIn("exit_price", src)


class M20HNoMutation(unittest.TestCase):

    def test_records_not_mutated_by_append(self):
        e = _event("a")
        before = e.to_dict()
        with tempfile.TemporaryDirectory() as d:
            append_events([e], directory=d)
        self.assertEqual(e.to_dict(), before)


class M20HGitignore(unittest.TestCase):

    def test_gitignore_contains_data_paper(self):
        gi = (_REPO_ROOT / ".gitignore").read_text().splitlines()
        self.assertIn("data/paper/", [ln.strip() for ln in gi])


class M20HSafetyGuards(unittest.TestCase):

    FORBIDDEN_ROOTS = {"sqlite3", "ib_insync", "requests", "urllib", "aiohttp",
                       "socket", "http", "main", "dashboard", "yfinance",
                       "random"}
    FORBIDDEN_PREFIXES = ("bot.brokers", "bot.live", "bot.etoro", "bot.risk",
                          "bot.risk_authority", "bot.flywheel", "bot.scanner",
                          "bot.strategy", "dashboard", "main")

    def test_no_forbidden_imports(self):
        tree = ast.parse((_PKG_DIR / "storage.py").read_text())
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

    def test_no_sqlite_usage(self):
        # AST import guard already covers sqlite3; here ensure no sqlite3 module
        # usage tokens (the word may legitimately appear in the boundary docstring).
        src = (_PKG_DIR / "storage.py").read_text()
        self.assertNotIn("import sqlite3", src)
        self.assertNotIn("sqlite3.", src)
        self.assertNotIn(".db", src)

    def test_no_wallclock_token(self):
        src = (_PKG_DIR / "storage.py").read_text()
        self.assertNotIn("datetime.now", src)
        self.assertNotIn("time.time", src)


class M20HFrozenChecks(unittest.TestCase):

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
        self._unchanged(_M20G_HEAD, "bot/paper/schema.py")

    def test_paper_only_authorised_h_diff(self):
        r = subprocess.run(["git", "diff", "--name-only", _M20G_HEAD, "HEAD",
                            "--", "bot/paper"], capture_output=True, text=True,
                           timeout=10)
        changed = set(r.stdout.split())
        self.assertTrue(changed <= {"bot/paper/storage.py",
                                    "bot/paper/__init__.py"}, changed)


if __name__ == "__main__":
    unittest.main()
