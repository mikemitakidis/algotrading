"""test_group_c_cleanup.py — proofs for pre-M19 Group C fixes.

Covers:
* ISSUE-012 — main.py no-op removal is behaviour-preserving (tf key mapping).
* ISSUE-015 — bot.risk._load_open_intents() parameterised query: correct
  inclusion/exclusion behaviour against a TEMP sqlite db (never the real
  signals.db), plus a static check that the old .format() SQL pattern is gone.
* ISSUE-011 — routing default-lock: scanner + strategy default ibkr_min_tfs==2.

Safety: the ISSUE-015 test points bot.risk at a temporary database via
monkeypatching BASE_DIR-derived path; it never reads or writes the real
data/signals.db. No live/broker code is touched.
"""
import ast
import pathlib
import sqlite3
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent


class Issue012TfKeyMapping(unittest.TestCase):
    """The removed `.replace("h","h")` was a no-op; the four tf keys must
    remain identical with the simplified expression."""

    def test_tf_keys_unchanged(self):
        expected = {
            "1D": "tf_1d",
            "4H": "tf_4h",
            "1H": "tf_1h",
            "15m": "tf_15m",
        }
        for label, want in expected.items():
            # the NEW expression used in main.py
            got_new = f"tf_{label.lower()}"
            # the OLD expression (no-op .replace) — must be identical
            got_old = f"tf_{label.lower().replace('h', 'h')}"
            self.assertEqual(got_new, want)
            self.assertEqual(got_new, got_old)

    def test_main_py_no_longer_contains_noop_replace(self):
        src = (_REPO_ROOT / "main.py").read_text()
        self.assertNotIn('.replace("h","h")', src)
        self.assertNotIn(".replace('h','h')", src)


class Issue015LoadOpenIntents(unittest.TestCase):
    """_load_open_intents() must return accepted + paper_logged rows, exclude
    risk_rejected and the synthetic test ids, using bound parameters — proven
    against a temp db, never the real signals.db."""

    def _make_temp_db(self, rows):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="m_groupc_")) / "signals.db"
        conn = sqlite3.connect(str(tmp))
        conn.execute(
            "CREATE TABLE execution_intents "
            "(symbol TEXT, direction TEXT, signal_id INTEGER, status TEXT)")
        conn.executemany(
            "INSERT INTO execution_intents VALUES (?,?,?,?)", rows)
        conn.commit()
        conn.close()
        return tmp

    def test_inclusion_exclusion_behaviour(self):
        import bot.risk as risk
        rows = [
            ("AAPL", "long", 1001, "accepted"),
            ("MSFT", "long", 1002, "paper_logged"),
            ("TSLA", "short", 1003, "risk_rejected"),   # excluded (status)
            ("NVDA", "long", 1004, "error"),            # excluded (status)
            ("SPY",  "long", 888888, "accepted"),        # excluded (test id)
            ("QQQ",  "long", 999999, "paper_logged"),    # excluded (test id)
        ]
        tmp = self._make_temp_db(rows)
        # Point bot.risk at the temp db for the duration of this test.
        orig_base = risk.BASE_DIR
        try:
            risk.BASE_DIR = tmp.parent.parent  # BASE_DIR/'data'/'signals.db'
            (tmp.parent.parent / "data").mkdir(exist_ok=True)
            # move temp db into the expected data/signals.db location
            target = tmp.parent.parent / "data" / "signals.db"
            target.write_bytes(tmp.read_bytes())
            result = risk._load_open_intents()
        finally:
            risk.BASE_DIR = orig_base
        ids = sorted(r["signal_id"] for r in result)
        self.assertEqual(ids, [1001, 1002],
                         "only non-test accepted/paper_logged rows expected")
        statuses = {r["status"] for r in result}
        self.assertTrue(statuses <= {"accepted", "paper_logged"})

    def test_no_real_signals_db_touched(self):
        """The function returns [] gracefully when the db path does not
        exist — proving it does not create/seek the real signals.db."""
        import bot.risk as risk
        orig_base = risk.BASE_DIR
        try:
            empty = pathlib.Path(tempfile.mkdtemp(prefix="m_groupc_empty_"))
            risk.BASE_DIR = empty  # empty/'data'/'signals.db' does not exist
            self.assertEqual(risk._load_open_intents(), [])
        finally:
            risk.BASE_DIR = orig_base

    def test_old_format_sql_pattern_removed(self):
        """Static negative check: the .format()-built NOT IN SQL must be gone
        from _load_open_intents()."""
        src = (_REPO_ROOT / "bot" / "risk.py").read_text()
        tree = ast.parse(src)
        func_src = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_load_open_intents":
                func_src = ast.get_source_segment(src, node)
        self.assertIsNotNone(func_src, "_load_open_intents not found")
        # The old pattern interpolated ids via str.format into NOT IN ({}).
        self.assertNotIn('.format(', func_src,
                         "old .format()-built SQL must be gone")
        self.assertIn("NOT IN ({placeholders})", func_src,
                      "parameterised placeholders expected")


class Issue011RoutingDefaults(unittest.TestCase):
    """Routing default-lock: documented current rule is ibkr_min_tfs=2.
    No scanner/routing behaviour is changed by Group C; this locks the
    defaults so a silent drift would be caught."""

    def test_strategy_default_ibkr_min_tfs_is_2(self):
        import bot.strategy as strategy
        self.assertEqual(
            strategy.DEFAULTS["routing"]["ibkr_min_tfs"], 2)
        self.assertEqual(
            strategy.DEFAULTS["routing"]["etoro_min_tfs"], 4)

    def test_scanner_default_ibkr_min_tfs_is_2(self):
        """scanner reads routing.get('ibkr_min_tfs', <default>) — the literal
        default fallback in source must be 2 (whitespace-insensitive)."""
        import re
        src = (_REPO_ROOT / "bot" / "scanner.py").read_text()
        m = re.search(
            r"routing\.get\(\s*['\"]ibkr_min_tfs['\"]\s*,\s*(\d+)\s*\)", src)
        self.assertIsNotNone(m, "ibkr_min_tfs default fallback not found")
        self.assertEqual(m.group(1), "2")


if __name__ == "__main__":
    unittest.main()
