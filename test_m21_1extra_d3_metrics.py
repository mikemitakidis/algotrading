#!/usr/bin/env python3
"""M21.1extra-D3 — read-only paper outcome metrics + authenticated panel.

Proves:
  * metric maths (win rate, avg win/loss, total P&L, expectancy, profit factor,
    max drawdown) on synthetic fixtures, overall and per strategy;
  * zero genuine outcomes -> every metric None / "N/A" (never 0 or invented);
  * mechanical lifecycles and simulated / unknown-source rows never count;
  * the database is opened read-only (bytes and mtime unchanged; no file is
    created when absent; a write through the module's connection fails);
  * both routes require an authenticated session and are GET-only;
  * no broker / order / gateway / Telegram imports.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import _test_isolation  # noqa: E402
_test_isolation.install()

from bot.runtime import paper_outcome_metrics as M  # noqa: E402

REPO = Path(__file__).resolve().parent
METRICS_SRC = REPO / "bot" / "runtime" / "paper_outcome_metrics.py"
VIEW_SRC = REPO / "dashboard" / "paper_metrics_view.py"


def _outcome(tid, pnl, exit_at, *, strategy="trend_v1", source="ibkr_paper",
             edge=1, closed=True, symbol="AAA"):
    return {"trade_id": tid, "strategy_id": strategy, "symbol": symbol,
            "direction": "long", "qty": 1.0, "entry_price": 100.0,
            "exit_price": 100.0 + pnl if pnl is not None else None,
            "entry_filled_at_utc": "2026-10-01T14:00:00+00:00",
            "exit_filled_at_utc": exit_at if closed else None,
            "exit_reason": "take_profit" if (pnl or 0) > 0 else "stop_loss",
            "gross_pnl": pnl, "fees": 0.0, "realized_pnl": pnl,
            "data_source": source, "record_kind": "edge_paper_trade",
            "is_edge_outcome": edge}


class D3DbFixture:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="d3_metrics_")
        self.path = os.path.join(self.dir, "paper_lifecycles.db")

    def create(self, outcomes=(), equity=(), lifecycles=()):
        con = sqlite3.connect(self.path)
        con.execute(
            "CREATE TABLE paper_trade_outcomes (trade_id TEXT PRIMARY KEY, strategy_id TEXT,"
            " symbol TEXT, direction TEXT, qty REAL, entry_price REAL, exit_price REAL,"
            " entry_filled_at_utc TEXT, exit_filled_at_utc TEXT, exit_reason TEXT,"
            " gross_pnl REAL, fees REAL, realized_pnl REAL, data_source TEXT,"
            " record_kind TEXT, is_edge_outcome INTEGER)")
        for o in outcomes:
            con.execute("INSERT INTO paper_trade_outcomes (%s) VALUES (%s)" % (
                ",".join(o), ",".join("?" * len(o))), list(o.values()))
        con.execute("CREATE TABLE paper_equity_snapshots (snapshot_id TEXT PRIMARY KEY,"
                    " as_of_utc TEXT, net_liquidation REAL, currency TEXT, data_source TEXT)")
        for i, (ts, v, src) in enumerate(equity):
            con.execute("INSERT INTO paper_equity_snapshots VALUES (?,?,?,?,?)",
                        ("s%d" % i, ts, v, "USD", src))
        con.execute("CREATE TABLE paper_lifecycles (lifecycle_id TEXT PRIMARY KEY, symbol TEXT,"
                    " lifecycle_confirmed INTEGER, flatten_confirmed INTEGER,"
                    " persisted_at_utc TEXT, record_kind TEXT, is_edge_outcome INTEGER)")
        for i, (sym, conf, flat, ts) in enumerate(lifecycles):
            con.execute("INSERT INTO paper_lifecycles VALUES (?,?,?,?,?,?,?)",
                        ("l%d" % i, sym, conf, flat, ts, "mechanical_paper_lifecycle", 0))
        con.commit()
        con.close()
        return self.path

    def fingerprint(self):
        st = os.stat(self.path)
        return (hashlib.sha256(Path(self.path).read_bytes()).hexdigest(), st.st_mtime_ns,
                sorted(os.listdir(self.dir)))

    def cleanup(self):
        shutil.rmtree(self.dir, True)


class D3MetricMaths(unittest.TestCase):
    def test_known_series(self):
        rows = [_outcome("t1", 10.0, "2026-10-01T15:00:00+00:00"),
                _outcome("t2", -4.0, "2026-10-02T15:00:00+00:00"),
                _outcome("t3", -6.0, "2026-10-03T15:00:00+00:00"),
                _outcome("t4", 20.0, "2026-10-04T15:00:00+00:00"),
                _outcome("t5", 0.0, "2026-10-05T15:00:00+00:00")]
        m = M.edge_metrics(rows)
        self.assertEqual((m["trades"], m["wins"], m["losses"], m["breakeven"]), (5, 2, 2, 1))
        self.assertAlmostEqual(m["win_rate"], 0.4)
        self.assertAlmostEqual(m["avg_win"], 15.0)
        self.assertAlmostEqual(m["avg_loss"], -5.0)
        self.assertAlmostEqual(m["total_pnl"], 20.0)
        self.assertAlmostEqual(m["expectancy"], 4.0)
        self.assertAlmostEqual(m["profit_factor"], 3.0)
        # cumulative: 10, 6, 0, 20, 20 -> peak 10 then trough 0 -> drawdown 10
        self.assertAlmostEqual(m["max_drawdown"], 10.0)
        self.assertEqual(m["first_exit_utc"], "2026-10-01T15:00:00+00:00")

    def test_drawdown_uses_exit_order_not_insert_order(self):
        rows = [_outcome("b", -5.0, "2026-10-02T15:00:00+00:00"),
                _outcome("a", 5.0, "2026-10-01T15:00:00+00:00")]
        self.assertAlmostEqual(M.edge_metrics(rows)["max_drawdown"], 5.0)   # +5 then -5
        self.assertAlmostEqual(M.max_drawdown([-3.0, -2.0]), 5.0)           # from the 0 start
        self.assertEqual(M.max_drawdown([1.0, 2.0]), 0.0)
        self.assertIsNone(M.max_drawdown([]))

    def test_only_wins_or_only_losses(self):
        w = M.edge_metrics([_outcome("w", 3.0, "2026-10-01T15:00:00+00:00")])
        self.assertIsNone(w["avg_loss"])
        self.assertIsNone(w["profit_factor"])
        self.assertEqual(w["win_rate"], 1.0)
        lo = M.edge_metrics([_outcome("l", -3.0, "2026-10-01T15:00:00+00:00")])
        self.assertIsNone(lo["avg_win"])
        self.assertEqual(lo["profit_factor"], 0.0)

    def test_empty_is_all_none(self):
        m = M.edge_metrics([])
        self.assertEqual(m["trades"], 0)
        for k in ("win_rate", "avg_win", "avg_loss", "total_pnl", "expectancy",
                  "profit_factor", "max_drawdown"):
            self.assertIsNone(m[k], k)
            self.assertEqual(M.display(m[k]), "N/A")

    def test_genuine_filter(self):
        self.assertTrue(M.is_genuine_outcome(_outcome("g", 1.0, "2026-10-01T15:00:00+00:00")))
        for bad in (_outcome("s", 1.0, "x", source="simulated"),
                    _outcome("u", 1.0, "x", source=None),
                    _outcome("m", 1.0, "x", edge=0),
                    _outcome("o", 1.0, "x", closed=False),
                    _outcome("n", None, "x"),
                    dict(_outcome("nan", 1.0, "x"), realized_pnl=float("nan"))):
            self.assertFalse(M.is_genuine_outcome(bad), bad["trade_id"])

    def test_equity_drawdown(self):
        e = M.equity_metrics([
            {"as_of_utc": "2026-10-01", "net_liquidation": 1000.0, "currency": "USD", "data_source": "ibkr_paper"},
            {"as_of_utc": "2026-10-02", "net_liquidation": 1100.0, "currency": "USD", "data_source": "ibkr_paper"},
            {"as_of_utc": "2026-10-03", "net_liquidation": 990.0, "currency": "USD", "data_source": "ibkr_paper"},
            {"as_of_utc": "2026-10-04", "net_liquidation": 5.0, "currency": "USD", "data_source": "simulated"}])
        self.assertEqual(e["snapshots"], 3)
        self.assertEqual(e["latest_equity"], 990.0)
        self.assertAlmostEqual(e["equity_drawdown"], 110.0)
        self.assertAlmostEqual(e["equity_drawdown_pct"], 0.1)
        self.assertIsNone(M.equity_metrics([])["latest_equity"])


class D3DatabaseReads(unittest.TestCase):
    def setUp(self):
        self.fx = D3DbFixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_absent_database_is_na_and_not_created(self):
        m = M.compute_paper_metrics(self.fx.path)
        self.assertFalse(m["db_present"])
        self.assertFalse(os.path.exists(self.fx.path))
        self.assertEqual(m["edge_outcomes"]["overall"]["trades"], 0)
        self.assertIsNone(m["edge_outcomes"]["overall"]["win_rate"])
        self.assertIsNone(m["broker_equity"]["latest_equity"])
        self.assertEqual(m["mechanical_lifecycles"]["total"], 0)

    def test_mechanical_only_database_today_shows_na(self):
        # Today's reality: only mechanical immediate-flatten lifecycles.
        con = sqlite3.connect(self.fx.path)
        con.execute("CREATE TABLE paper_lifecycles (lifecycle_id TEXT PRIMARY KEY, symbol TEXT,"
                    " lifecycle_confirmed INTEGER, flatten_confirmed INTEGER,"
                    " persisted_at_utc TEXT)")
        con.execute("INSERT INTO paper_lifecycles VALUES ('oid:1','F',1,1,'2026-10-20T15:00:00+00:00')")
        con.commit()
        con.close()
        m = M.compute_paper_metrics(self.fx.path)
        self.assertTrue(m["db_present"])
        self.assertFalse(m["outcomes_table_present"])
        self.assertIsNone(m["edge_outcomes"]["overall"]["win_rate"])
        self.assertEqual(m["mechanical_lifecycles"]["total"], 1)
        self.assertEqual(m["mechanical_lifecycles"]["lifecycle_confirmed"], 1)

    def test_per_strategy_grouping_and_exclusions(self):
        self.fx.create(outcomes=[
            _outcome("a1", 5.0, "2026-10-01T15:00:00+00:00", strategy="trend_v1"),
            _outcome("a2", -2.0, "2026-10-02T15:00:00+00:00", strategy="trend_v1"),
            _outcome("b1", 3.0, "2026-10-01T16:00:00+00:00", strategy="meanrev_v1"),
            _outcome("u1", -1.0, "2026-10-03T15:00:00+00:00", strategy=None),
            _outcome("s1", 999.0, "2026-10-03T15:00:00+00:00", source="simulated"),
            _outcome("o1", 50.0, "2026-10-03T15:00:00+00:00", closed=False)],
            lifecycles=[("F", 1, 1, "2026-10-20T15:00:00+00:00")])
        m = M.compute_paper_metrics(self.fx.path)
        e = m["edge_outcomes"]
        self.assertEqual(e["rows_total"], 6)
        self.assertEqual(e["rows_genuine"], 4)
        self.assertEqual(e["excluded_non_genuine"], 2)
        self.assertEqual(e["overall"]["trades"], 4)
        self.assertAlmostEqual(e["overall"]["total_pnl"], 5.0)
        self.assertEqual(sorted(e["by_strategy"]), ["meanrev_v1", "trend_v1", M.UNATTRIBUTED])
        self.assertAlmostEqual(e["by_strategy"]["trend_v1"]["win_rate"], 0.5)
        self.assertEqual(m["mechanical_lifecycles"]["total"], 1)

    def test_reads_never_write(self):
        self.fx.create(outcomes=[_outcome("a", 1.0, "2026-10-01T15:00:00+00:00")],
                       equity=[("2026-10-01T15:00:00+00:00", 1000.0, "ibkr_paper")])
        before = self.fx.fingerprint()
        for _ in range(3):
            M.compute_paper_metrics(self.fx.path)
        self.assertEqual(self.fx.fingerprint(), before)
        conn = M._connect_ro(self.fx.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO paper_trade_outcomes (trade_id) VALUES ('x')")
        finally:
            conn.close()
        self.assertEqual(self.fx.fingerprint(), before)

    def test_corrupt_database_reports_error_not_numbers(self):
        Path(self.fx.path).write_bytes(b"this is not sqlite" * 100)
        m = M.compute_paper_metrics(self.fx.path)
        self.assertTrue(m["error"].startswith("database_unreadable"))
        self.assertIsNone(m["edge_outcomes"]["overall"]["win_rate"])


class D3DashboardView(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from dashboard.paper_metrics_view import bp
        self.fx = D3DbFixture()
        self.fx.create(outcomes=[_outcome("a", 2.5, "2026-10-01T15:00:00+00:00", symbol="<b>X</b>",
                                          strategy="<i>strat</i>")],
                       lifecycles=[("<script>", 1, 1, "2026-10-20T15:00:00+00:00")])
        app = Flask("d3_test")
        app.secret_key = "d3-test-only"
        app.config["PAPER_LIFECYCLES_DB_PATH"] = self.fx.path
        app.register_blueprint(bp)
        self.client = app.test_client()

    def tearDown(self):
        self.fx.cleanup()

    def _login(self):
        with self.client.session_transaction() as s:
            s["authed"] = True

    def test_requires_auth(self):
        for url in ("/api/paper-loop/metrics", "/paper-loop/metrics"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 401, url)

    def test_get_only(self):
        self._login()
        for url in ("/api/paper-loop/metrics", "/paper-loop/metrics"):
            for method in ("post", "put", "delete", "patch"):
                self.assertEqual(getattr(self.client, method)(url).status_code, 405)

    def test_api_returns_metrics(self):
        self._login()
        r = self.client.get("/api/paper-loop/metrics")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data["edge_outcomes"]["overall"]["trades"], 1)
        self.assertAlmostEqual(data["edge_outcomes"]["overall"]["total_pnl"], 2.5)

    def test_panel_shows_na_and_escapes(self):
        self._login()
        html = self.client.get("/paper-loop/metrics").data.decode()
        self.assertIn("Win rate", html)
        self.assertIn("N/A", html)                  # avg loss: no losing trades
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<i>strat</i>", html)      # strategy ids come from the DB
        self.assertIn("&lt;i&gt;strat&lt;/i&gt;", html)
        with self.client.session_transaction() as s:
            s.clear()
        self.assertEqual(self.client.get("/paper-loop/metrics").status_code, 401)


class D3AppIntegration(unittest.TestCase):
    """The real dashboard app registers the D3 blueprint (integration patch)."""

    def setUp(self):
        import sys
        for mod in ("dashboard.app", "dashboard"):
            sys.modules.pop(mod, None)
        from dashboard import app as app_mod
        self.fx = D3DbFixture()
        self.fx.create(outcomes=[_outcome("a", 1.5, "2026-10-01T15:00:00+00:00")])
        self.app = app_mod.app
        self.app.config["TESTING"] = True
        self.app.config["PAPER_LIFECYCLES_DB_PATH"] = self.fx.path
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.config.pop("PAPER_LIFECYCLES_DB_PATH", None)
        self.fx.cleanup()

    def test_routes_registered_get_only_and_auth_required(self):
        rules = {str(r): r.methods - {"HEAD", "OPTIONS"} for r in self.app.url_map.iter_rules()}
        for url in ("/api/paper-loop/metrics", "/paper-loop/metrics"):
            self.assertEqual(rules.get(url), {"GET"}, url)
            self.assertEqual(self.client.get(url).status_code, 401, url)
        with self.client.session_transaction() as s:
            s["authed"] = True
        r = self.client.get("/api/paper-loop/metrics")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.data)["edge_outcomes"]["overall"]["trades"], 1)

    def test_nav_links_to_panel(self):
        src = (REPO / "dashboard" / "app.py").read_text(encoding="utf-8")
        self.assertIn('href="/paper-loop/metrics"', src)


class D3StaticSafety(unittest.TestCase):
    FORBIDDEN = ("bot.brokers", "bot.etoro", "ib_insync", "tools.etoro_live_write",
                 "tools.paper_loop.m21_1extra_b2b_single_submit", "bot.notifier",
                 "bot.paper_loop_d2.lifecycle", "requests", "urllib.request", "socket")

    def _imports(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module)
        return mods

    def test_no_broker_order_or_network_imports(self):
        for src in (METRICS_SRC, VIEW_SRC):
            mods = self._imports(src)
            bad = [m for m in mods for f in self.FORBIDDEN if m == f or m.startswith(f + ".")]
            self.assertEqual(bad, [], src.name)

    def test_only_read_only_sql(self):
        src = METRICS_SRC.read_text(encoding="utf-8").upper()
        for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ", "ALTER ", "REPLACE "):
            self.assertNotIn(verb, src.replace("CREATED", ""), verb)
        self.assertIn("MODE=RO", src)

    def test_view_routes_are_get_only(self):
        tree = ast.parse(VIEW_SRC.read_text(encoding="utf-8"))
        routes = 0
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "route":
                routes += 1
                methods = [kw for kw in n.keywords if kw.arg == "methods"]
                self.assertEqual(len(methods), 1)
                self.assertEqual(ast.literal_eval(methods[0].value), ["GET"])
        self.assertEqual(routes, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
