"""M14.G — Risk Authority Dashboard (read-only visibility) test suite.

Proves the M14.G hard constraints and acceptance criteria:

  Hard constraints:
    * read-only dashboard/API visibility only
    * no broker writes, no live orders, no eToro POST/DELETE/PUT/PATCH
    * no dashboard live-write button
    * no scanner-to-live shortcut
    * no manual_reset implementation
    * do not weaken M13.5 or M14.F protections

  Acceptance:
    * four read-only endpoints implemented
    * read-only dashboard tab visible
    * known-zero vs unknown clearly displayed
    * latest RiskDecision rows visible
    * per-scope PnL/exposure status visible
    * latest snapshot summary visible
    * authority/governor display visible
    * no DB writes from new endpoints
    * no POST/DELETE/PUT/PATCH from new JS
    * no broker/live-write imports
    * scanner isolation preserved

No live calls, no eToro write endpoint contacted, no order placed.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO = str(Path(__file__).resolve().parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from bot.flywheel import init_flywheel_tables
from bot.risk_authority.dashboard_read import (
    DECISIONS_DEFAULT_LIMIT,
    DECISIONS_MAX_LIMIT,
    get_authority_view,
    get_latest_snapshot,
    get_scope_status,
    list_recent_decisions,
)
from bot.risk_authority.snapshot import ALL_BROKER_SCOPES


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


class _DB:
    """Temp SQLite with flywheel + audit tables ready."""

    def __init__(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        f.close()
        self.path = f.name
        with self.conn() as c:
            init_flywheel_tables(c)

    def conn(self):
        return sqlite3.connect(self.path)

    def cleanup(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _seed_scope_row(conn: sqlite3.Connection, *,
                     scope: str,
                     pnl_known: bool = True,
                     exposure_known: bool = True,
                     realised_pnl: float = 0.0,
                     realised_loss: float = 0.0,
                     open_positions: int = 0,
                     capital: float = 0.0,
                     trading_day: Optional[str] = None) -> None:
    if trading_day is None:
        trading_day = _today_utc()
    lifecycle = {
        "status": "fresh" if pnl_known else "unknown",
        "exposure_status": ("exposure_fresh" if exposure_known
                            else "exposure_unknown"),
        "exposure_fresh_reads_count": 3 if exposure_known else 0,
    }
    conn.execute(
        "INSERT OR REPLACE INTO daily_state_per_broker "
        "(date, broker_scope, realised_pnl_usd, realised_daily_loss, "
        " daily_pnl_available, daily_loss_block_active, "
        " open_positions, capital_deployed, peak_equity, "
        " drawdown_from_peak, fresh_reads_count, "
        " source, last_ingested_at, lifecycle_json, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trading_day, scope, realised_pnl, realised_loss,
         1 if pnl_known else 0, 0,
         open_positions, capital, None, 0.0,
         3 if pnl_known else 0,
         "ingested", datetime.now(timezone.utc).isoformat(),
         json.dumps(lifecycle),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _seed_decision(conn: sqlite3.Connection, *,
                    decision_id: str,
                    scope: str = "etoro_real",
                    result: str = "block",
                    authority_before: str = "AUTO_ALLOWED",
                    authority_after: str = "OFF",
                    reason_codes=("global_kill",),
                    snapshot_id: Optional[int] = None,
                    taken_at: Optional[str] = None,
                    source: str = "manual",
                    actor: str = "operator") -> None:
    if taken_at is None:
        taken_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO risk_decisions "
        "(decision_id, taken_at, broker_scope, requested_action, "
        " request_json, result, authority_before, authority_after, "
        " reason_codes, recovery_paths, snapshot_id, source, actor, "
        " explainer, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (decision_id, taken_at, scope, "trade_open",
         json.dumps({"symbol": "AAPL", "amount_usd": 50.0}),
         result, authority_before, authority_after,
         json.dumps(list(reason_codes)),
         json.dumps({r: "fix" for r in reason_codes}),
         snapshot_id, source, actor,
         f"explainer for {decision_id}",
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _seed_snapshot(conn: sqlite3.Connection, *,
                    any_pnl_unknown: bool = False,
                    any_exposure_unknown: bool = False,
                    combined_capital: float = 0.0,
                    combined_positions: int = 0) -> int:
    snap_json = {
        "trading_day_utc": _today_utc(),
        "scopes": {},
        "global_view": {
            "combined_capital_deployed": (None if any_exposure_unknown
                                           else combined_capital),
            "combined_open_positions": (None if any_exposure_unknown
                                         else combined_positions),
            "combined_realised_daily_loss": (None if any_pnl_unknown else 0.0),
            "per_symbol_exposure": {},
            "any_pnl_unknown": any_pnl_unknown,
            "any_exposure_unknown": any_exposure_unknown,
            "unknown_pnl_scopes": (["etoro_real"] if any_pnl_unknown else []),
            "unknown_exposure_scopes": (["etoro_real"]
                                         if any_exposure_unknown else []),
        },
    }
    fresh = {
        "any_pnl_unknown": any_pnl_unknown,
        "any_exposure_unknown": any_exposure_unknown,
    }
    cur = conn.execute(
        "INSERT INTO risk_snapshots "
        "(taken_at, policy_version, snapshot_json, freshness_summary, "
        " source, created_at) VALUES (?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), 1,
         json.dumps(snap_json), json.dumps(fresh),
         "pre_decision",
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def _flask_client(db_path: str):
    """Build a Flask test client pointed at a temp DB. Monkey-patches
    dashboard.app.DB_PATH because it's a module-level Path constant
    (not env-driven). Tests run in-process — no real network."""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, _REPO)
    if "dashboard.app" in _sys.modules:
        del _sys.modules["dashboard.app"]
    if "dashboard" in _sys.modules:
        del _sys.modules["dashboard"]
    from dashboard import app as _app_mod
    _app_mod.DB_PATH = _Path(db_path)
    app = _app_mod.app
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess["logged_in"] = True
        sess["authed"] = True
    return client, app


# ─────────────────────────────────────────────────────────────────────────────
# Group 1 — dashboard_read helper functions
# ─────────────────────────────────────────────────────────────────────────────


class TestDashboardReadHelpers(unittest.TestCase):

    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def test_list_recent_decisions_empty_db(self):
        with self.fx.conn() as c:
            r = list_recent_decisions(c)
        self.assertEqual(r["decisions"], [])
        self.assertEqual(r["total_count"], 0)
        self.assertIn("as_of_utc", r)

    def test_list_recent_decisions_desc_order(self):
        with self.fx.conn() as c:
            _seed_decision(c, decision_id="d-1", taken_at="2026-05-29T10:00:00Z")
            _seed_decision(c, decision_id="d-2", taken_at="2026-05-29T12:00:00Z")
            _seed_decision(c, decision_id="d-3", taken_at="2026-05-29T11:00:00Z")
            r = list_recent_decisions(c)
        ids = [d["decision_id"] for d in r["decisions"]]
        self.assertEqual(ids, ["d-2", "d-3", "d-1"])

    def test_list_recent_decisions_limit_clamped_to_max(self):
        with self.fx.conn() as c:
            for i in range(150):
                _seed_decision(c, decision_id=f"d-{i}",
                                taken_at=f"2026-05-{(i%28)+1:02d}T10:00:00Z")
            r = list_recent_decisions(c, limit=10_000)
        self.assertEqual(len(r["decisions"]), DECISIONS_MAX_LIMIT)
        self.assertEqual(r["total_count"], 150)

    def test_list_recent_decisions_scope_filter(self):
        with self.fx.conn() as c:
            _seed_decision(c, decision_id="d-etoro",
                            scope="etoro_real",
                            taken_at="2026-05-29T10:00:00Z")
            _seed_decision(c, decision_id="d-ibkr",
                            scope="ibkr_live",
                            taken_at="2026-05-29T11:00:00Z")
            r = list_recent_decisions(c, scope="etoro_real")
        self.assertEqual(len(r["decisions"]), 1)
        self.assertEqual(r["decisions"][0]["decision_id"], "d-etoro")

    def test_list_recent_decisions_rejects_invalid_scope(self):
        with self.fx.conn() as c:
            with self.assertRaises(ValueError):
                list_recent_decisions(c, scope="not_a_scope")

    def test_list_recent_decisions_parses_json_columns(self):
        with self.fx.conn() as c:
            _seed_decision(c, decision_id="d-1",
                            reason_codes=("global_kill", "broker_kill"))
            r = list_recent_decisions(c)
        d = r["decisions"][0]
        self.assertEqual(d["reason_codes"], ["global_kill", "broker_kill"])
        self.assertIn("global_kill", d["recovery_paths"])
        self.assertEqual(d["request_payload"]["symbol"], "AAPL")

    def test_get_scope_status_always_returns_4_scopes(self):
        with self.fx.conn() as c:
            r = get_scope_status(c)
        self.assertEqual(set(r["scopes"]), set(ALL_BROKER_SCOPES))

    def test_get_scope_status_known_zero_vs_unknown(self):
        with self.fx.conn() as c:
            _seed_scope_row(c, scope="etoro_real",
                              pnl_known=True, realised_pnl=0.0,
                              realised_loss=0.0,
                              exposure_known=True, open_positions=0,
                              capital=0.0)
            _seed_scope_row(c, scope="ibkr_live",
                              pnl_known=False,
                              exposure_known=False)
            r = get_scope_status(c)
        kz = r["scopes"]["etoro_real"]
        uz = r["scopes"]["ibkr_live"]
        # Same numeric values, different known/unknown status.
        self.assertEqual(kz["realised_pnl_usd"], uz["realised_pnl_usd"])
        self.assertEqual(kz["open_positions"], uz["open_positions"])
        # But the booleans distinguish them.
        self.assertTrue(kz["pnl_known"])
        self.assertTrue(kz["pnl_known_zero"])
        self.assertFalse(uz["pnl_known"])
        self.assertFalse(uz["pnl_known_zero"])
        self.assertTrue(kz["exposure_known"])
        self.assertTrue(kz["exposure_known_zero"])
        self.assertFalse(uz["exposure_known"])
        self.assertFalse(uz["exposure_known_zero"])

    def test_get_scope_status_warnings_field(self):
        with self.fx.conn() as c:
            _seed_scope_row(c, scope="etoro_real",
                              pnl_known=False, exposure_known=False)
            r = get_scope_status(c)
        warnings = r["scopes"]["etoro_real"]["warnings"]
        self.assertIn("pnl_unknown", warnings)
        self.assertIn("exposure_unknown", warnings)

    def test_get_scope_status_clean_scope_has_no_warnings(self):
        with self.fx.conn() as c:
            _seed_scope_row(c, scope="etoro_real")
            r = get_scope_status(c)
        self.assertEqual(r["scopes"]["etoro_real"]["warnings"], [])

    def test_get_latest_snapshot_empty(self):
        with self.fx.conn() as c:
            self.assertIsNone(get_latest_snapshot(c))

    def test_get_latest_snapshot_parses_json(self):
        with self.fx.conn() as c:
            sid = _seed_snapshot(c, combined_capital=123.45,
                                  combined_positions=2)
            r = get_latest_snapshot(c)
        self.assertEqual(r["snapshot_id"], sid)
        self.assertEqual(r["combined"]["combined_capital_deployed"], 123.45)
        self.assertEqual(r["combined"]["combined_open_positions"], 2)

    def test_get_latest_snapshot_preserves_null_on_unknown(self):
        with self.fx.conn() as c:
            _seed_snapshot(c, any_exposure_unknown=True)
            r = get_latest_snapshot(c)
        # Hard invariant: unknown ⇒ null in JSON, NOT 0.
        self.assertIsNone(r["combined"]["combined_capital_deployed"])
        self.assertTrue(r["combined"]["any_exposure_unknown"])
        self.assertIn("etoro_real", r["combined"]["unknown_exposure_scopes"])

    def test_get_authority_view_empty_returns_all_scopes(self):
        with self.fx.conn() as c:
            r = get_authority_view(c)
        self.assertEqual(set(r["scopes"]), set(ALL_BROKER_SCOPES))
        for s, view in r["scopes"].items():
            self.assertIsNone(view["latest_authority_after"])
            self.assertFalse(view["manual_reset_would_be_required"])

    def test_get_authority_view_manual_reset_required_for_kill(self):
        with self.fx.conn() as c:
            _seed_decision(c, decision_id="d-kill",
                            scope="etoro_real",
                            reason_codes=("global_kill",))
            r = get_authority_view(c)
        v = r["scopes"]["etoro_real"]
        self.assertEqual(v["latest_downgrade_reason"], "global_kill")
        self.assertTrue(v["manual_reset_would_be_required"])

    def test_get_authority_view_no_manual_reset_for_auto_restorable(self):
        with self.fx.conn() as c:
            _seed_decision(c, decision_id="d-pnl",
                            scope="etoro_real",
                            reason_codes=("daily_pnl_unknown",))
            r = get_authority_view(c)
        v = r["scopes"]["etoro_real"]
        self.assertEqual(v["latest_downgrade_reason"], "daily_pnl_unknown")
        self.assertFalse(v["manual_reset_would_be_required"])


# ─────────────────────────────────────────────────────────────────────────────
# Group 2 — HTTP routes
# ─────────────────────────────────────────────────────────────────────────────


class TestHTTPRoutes(unittest.TestCase):

    def setUp(self):
        self.fx = _DB()
        self.client, self.app = _flask_client(self.fx.path)

    def tearDown(self):
        self.fx.cleanup()

    def test_decisions_200_on_empty_db(self):
        r = self.client.get("/api/risk-authority/decisions")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content_type, "application/json")
        body = r.get_json()
        self.assertEqual(body["decisions"], [])
        self.assertEqual(body["total_count"], 0)

    def test_decisions_200_with_seeded_data(self):
        with self.fx.conn() as c:
            _seed_decision(c, decision_id="d-1")
        r = self.client.get("/api/risk-authority/decisions")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(len(body["decisions"]), 1)
        self.assertEqual(body["decisions"][0]["decision_id"], "d-1")

    def test_decisions_limit_clamped_server_side(self):
        with self.fx.conn() as c:
            for i in range(150):
                _seed_decision(c, decision_id=f"d-{i}",
                                taken_at=f"2026-05-{(i%28)+1:02d}T10:00:00Z")
        r = self.client.get("/api/risk-authority/decisions?limit=10000")
        body = r.get_json()
        self.assertLessEqual(len(body["decisions"]), DECISIONS_MAX_LIMIT)

    def test_decisions_invalid_scope_returns_400(self):
        r = self.client.get("/api/risk-authority/decisions?scope=evil_broker")
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertIn("error", body)

    def test_decisions_empty_scope_treated_as_no_filter(self):
        r = self.client.get("/api/risk-authority/decisions?scope=")
        self.assertEqual(r.status_code, 200)

    def test_scopes_200_returns_all_4(self):
        r = self.client.get("/api/risk-authority/scopes")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(set(body["scopes"]), set(ALL_BROKER_SCOPES))

    def test_snapshot_latest_200_when_empty(self):
        r = self.client.get("/api/risk-authority/snapshot/latest")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIsNone(body["snapshot_id"])
        self.assertIn("message", body)

    def test_snapshot_latest_200_with_data(self):
        with self.fx.conn() as c:
            _seed_snapshot(c, combined_capital=100.0)
        r = self.client.get("/api/risk-authority/snapshot/latest")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIsNotNone(body["snapshot_id"])
        self.assertEqual(body["combined"]["combined_capital_deployed"], 100.0)

    def test_authority_200_with_seeded_decisions(self):
        with self.fx.conn() as c:
            _seed_decision(c, decision_id="d-1",
                            scope="etoro_real",
                            reason_codes=("global_kill",))
        r = self.client.get("/api/risk-authority/authority")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(
            body["scopes"]["etoro_real"]["manual_reset_would_be_required"])

    def test_all_endpoints_return_json_content_type(self):
        for url in ("/api/risk-authority/decisions",
                    "/api/risk-authority/scopes",
                    "/api/risk-authority/snapshot/latest",
                    "/api/risk-authority/authority"):
            r = self.client.get(url)
            self.assertEqual(r.content_type, "application/json",
                f"{url} content_type={r.content_type}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 3 — Read-only contract (AST + HTTP method audit)
# ─────────────────────────────────────────────────────────────────────────────


class TestReadOnlyContract(unittest.TestCase):

    DASHBOARD_READ = os.path.join(_REPO,
                                   "bot/risk_authority/dashboard_read.py")
    DASHBOARD_APP  = os.path.join(_REPO, "dashboard/app.py")

    FORBIDDEN_DB_METHODS = {"executemany", "executescript"}
    # Tracked separately because conn.execute() with a SELECT is fine,
    # but anything else is a write.
    FORBIDDEN_SQL_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE INTO",
                               "DROP", "CREATE", "ALTER", "TRUNCATE")

    def _ast_call_strings(self, path):
        """Return all string constants passed to .execute() / .executemany()
        in the file (these are the SQL statements)."""
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        sql_strings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("execute", "executemany"):
                    if node.args:
                        a = node.args[0]
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            sql_strings.append((node.lineno, a.value))
                        elif isinstance(a, ast.JoinedStr):
                            # f-string — concat the const parts.
                            parts = []
                            for v in a.values:
                                if isinstance(v, ast.Constant):
                                    parts.append(str(v.value))
                            sql_strings.append((node.lineno, "".join(parts)))
        return sql_strings

    def test_dashboard_read_no_write_methods(self):
        """AST scan: dashboard_read.py contains no .executemany / .commit /
        .executescript calls, and any conn.execute() arg is a SELECT or
        SELECT COUNT(*)."""
        with open(self.DASHBOARD_READ) as f:
            tree = ast.parse(f.read(), filename=self.DASHBOARD_READ)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("commit", "executemany",
                                       "executescript", "rollback"):
                    offenders.append(f".{node.func.attr} @{node.lineno}")
        self.assertEqual(offenders, [],
            f"dashboard_read.py has forbidden DB method calls: {offenders}")

    def test_dashboard_read_only_select_statements(self):
        """Every conn.execute() in dashboard_read.py must be a SELECT
        (or SELECT COUNT(*))."""
        for lineno, sql in self._ast_call_strings(self.DASHBOARD_READ):
            sql_strip = sql.strip().upper()
            self.assertTrue(
                sql_strip.startswith("SELECT"),
                f"non-SELECT SQL in dashboard_read.py @{lineno}: "
                f"{sql_strip[:60]!r}"
            )
            for forbidden in self.FORBIDDEN_SQL_PREFIXES:
                self.assertFalse(sql_strip.startswith(forbidden),
                    f"forbidden SQL prefix in dashboard_read.py "
                    f"@{lineno}: {forbidden}")

    def test_new_flask_routes_no_write_sql(self):
        """The four new M14.G routes must not contain write SQL.
        Locate them by name and AST-walk only those functions."""
        with open(self.DASHBOARD_APP) as f:
            tree = ast.parse(f.read(), filename=self.DASHBOARD_APP)
        targets = {
            "risk_authority_decisions",
            "risk_authority_scopes",
            "risk_authority_snapshot_latest",
            "risk_authority_authority",
        }
        found = set()
        offenders = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name in targets):
                found.add(node.name)
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)):
                        if sub.func.attr in ("commit", "executemany",
                                              "executescript",
                                              "rollback"):
                            offenders.append(
                                f"{node.name}: .{sub.func.attr} @{sub.lineno}")
                        if sub.func.attr in ("execute",):
                            if sub.args and isinstance(sub.args[0],
                                                         ast.Constant):
                                s = str(sub.args[0].value).strip().upper()
                                if not s.startswith("SELECT"):
                                    offenders.append(
                                        f"{node.name}: non-SELECT @{sub.lineno}: "
                                        f"{s[:50]!r}")
        self.assertEqual(found, targets,
            f"M14.G routes not all found by AST: missing={targets - found}")
        self.assertEqual(offenders, [],
            f"M14.G route handlers have write surface: {offenders}")

    def test_new_endpoints_reject_post(self):
        fx = _DB()
        try:
            client, _ = _flask_client(fx.path)
            for url in ("/api/risk-authority/decisions",
                        "/api/risk-authority/scopes",
                        "/api/risk-authority/snapshot/latest",
                        "/api/risk-authority/authority"):
                r = client.post(url)
                self.assertEqual(r.status_code, 405,
                    f"{url} accepts POST (status={r.status_code})")
        finally:
            fx.cleanup()

    def test_new_endpoints_reject_delete(self):
        fx = _DB()
        try:
            client, _ = _flask_client(fx.path)
            for url in ("/api/risk-authority/decisions",
                        "/api/risk-authority/scopes",
                        "/api/risk-authority/snapshot/latest",
                        "/api/risk-authority/authority"):
                r = client.delete(url)
                self.assertEqual(r.status_code, 405)
        finally:
            fx.cleanup()

    def test_new_endpoints_reject_put_patch(self):
        fx = _DB()
        try:
            client, _ = _flask_client(fx.path)
            for url in ("/api/risk-authority/decisions",
                        "/api/risk-authority/scopes",
                        "/api/risk-authority/snapshot/latest",
                        "/api/risk-authority/authority"):
                self.assertEqual(client.put(url).status_code, 405)
                self.assertEqual(client.patch(url).status_code, 405)
        finally:
            fx.cleanup()

    def test_runtime_probe_with_not_write_connection(self):
        """Wrap the sqlite connection so any non-SELECT statement raises;
        run each helper through it. All four must pass."""
        fx = _DB()
        try:
            class NotWriteConn:
                def __init__(self, inner):
                    self._inner = inner

                def execute(self, sql, params=()):
                    s = sql.strip().upper()
                    if not s.startswith("SELECT"):
                        raise AssertionError(
                            f"write SQL attempted via dashboard_read: "
                            f"{sql[:80]!r}")
                    return self._inner.execute(sql, params)

                def __getattr__(self, name):
                    if name in ("commit", "executemany", "executescript",
                                "rollback"):
                        raise AssertionError(
                            f"forbidden DB method called via "
                            f"dashboard_read: {name}")
                    return getattr(self._inner, name)

            with fx.conn() as raw:
                # Seed a row so the helpers have data to read.
                _seed_decision(raw, decision_id="d-1")
                _seed_scope_row(raw, scope="etoro_real")
                _seed_snapshot(raw)
            with fx.conn() as raw:
                wrapped = NotWriteConn(raw)
                list_recent_decisions(wrapped)
                get_scope_status(wrapped)
                get_latest_snapshot(wrapped)
                get_authority_view(wrapped)
        finally:
            fx.cleanup()

    def test_js_fetch_calls_are_get_only(self):
        """Scan the inline HTML/JS in dashboard/app.py for fetch calls
        with method: 'POST'/'DELETE'/'PUT'/'PATCH' literals in the
        Risk Authority loader region."""
        with open(self.DASHBOARD_APP) as f:
            src = f.read()
        # Isolate the loadRiskAuthority function block.
        start = src.find("function loadRiskAuthority()")
        self.assertGreater(start, 0, "loadRiskAuthority not found")
        # Find the closing of that function — next "function " or "}\n"
        # at column 0. Conservative bound: 8000 chars after start.
        block = src[start:start + 8000]
        for forbidden in ("method:'POST'", 'method:"POST"',
                          "method: 'POST'", 'method: "POST"',
                          "method:'DELETE'", 'method:"DELETE"',
                          "method: 'DELETE'", 'method: "DELETE"',
                          "method:'PUT'", 'method:"PUT"',
                          "method:'PATCH'", 'method:"PATCH"'):
            self.assertNotIn(forbidden, block,
                f"loadRiskAuthority contains forbidden fetch method "
                f"literal: {forbidden!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 4 — No broker contact / no live-write imports
# ─────────────────────────────────────────────────────────────────────────────


class TestNoBrokerContact(unittest.TestCase):

    PATH = os.path.join(_REPO, "bot/risk_authority/dashboard_read.py")

    FORBIDDEN_MODULES = {
        "bot.etoro.live_broker", "tools.etoro_live_write", "bot.brokers",
        "bot.risk_authority.preflight",
        "bot.risk_authority.ingest", "bot.risk_authority.ingest_etoro",
        "bot.risk_authority.ingest_ibkr",
        "bot.risk_authority.ingest_exposure",
        "bot.risk_authority.ingest_etoro_exposure",
        "bot.risk_authority.ingest_ibkr_exposure",
    }
    FORBIDDEN_NAMES = {"EtoroLiveBroker", "IBKRBroker", "PaperBroker"}
    FORBIDDEN_ORDER = {"placeOrder", "cancelOrder", "modifyOrder",
                        "reqGlobalCancel"}
    FORBIDDEN_HTTP = {"POST", "DELETE", "PUT", "PATCH"}
    FORBIDDEN_HTTP_METHODS = {"post", "delete", "put", "patch"}

    def _scan(self, path):
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                m = node.module or ""
                if m in self.FORBIDDEN_MODULES:
                    offenders.append(f"ImportFrom {m} @{node.lineno}")
                for a in node.names:
                    if a.name in self.FORBIDDEN_NAMES:
                        offenders.append(
                            f"ImportFrom name {a.name} @{node.lineno}")
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in self.FORBIDDEN_MODULES:
                        offenders.append(f"Import {a.name} @{node.lineno}")
            if isinstance(node, ast.Name) and node.id in self.FORBIDDEN_NAMES:
                offenders.append(f"Name {node.id} @{node.lineno}")
            if isinstance(node, ast.Call):
                for a in node.args:
                    if (isinstance(a, ast.Constant)
                            and isinstance(a.value, str)
                            and a.value.upper() in self.FORBIDDEN_HTTP):
                        offenders.append(f"call arg {a.value!r} @{a.lineno}")
                for kw in node.keywords:
                    if (kw.arg == "method"
                            and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)
                            and kw.value.value.upper() in self.FORBIDDEN_HTTP):
                        offenders.append(
                            f"method={kw.value.value} @{kw.value.lineno}")
                fn = node.func
                if isinstance(fn, ast.Attribute):
                    if fn.attr in self.FORBIDDEN_HTTP_METHODS:
                        offenders.append(f".{fn.attr} @{fn.lineno}")
                    if fn.attr in self.FORBIDDEN_ORDER:
                        offenders.append(f".{fn.attr} (order) @{fn.lineno}")
        return offenders

    def test_dashboard_read_no_broker_or_order_imports(self):
        offenders = self._scan(self.PATH)
        self.assertEqual(offenders, [],
            f"dashboard_read.py has forbidden references: {offenders}")

    def test_new_routes_no_broker_or_order_imports(self):
        """Scan just the four new function bodies for forbidden refs."""
        with open(os.path.join(_REPO, "dashboard/app.py")) as f:
            tree = ast.parse(f.read())
        targets = {
            "risk_authority_decisions", "risk_authority_scopes",
            "risk_authority_snapshot_latest", "risk_authority_authority",
        }
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in targets:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.ImportFrom):
                        m = sub.module or ""
                        if m in self.FORBIDDEN_MODULES:
                            offenders.append(
                                f"{node.name}: ImportFrom {m} @{sub.lineno}")
                        for a in sub.names:
                            if a.name in self.FORBIDDEN_NAMES:
                                offenders.append(
                                    f"{node.name}: ImportFrom {a.name} "
                                    f"@{sub.lineno}")
                    if isinstance(sub, ast.Name) and sub.id in self.FORBIDDEN_NAMES:
                        offenders.append(
                            f"{node.name}: Name {sub.id} @{sub.lineno}")
                    if isinstance(sub, ast.Call) and isinstance(sub.func,
                                                                 ast.Attribute):
                        if sub.func.attr in self.FORBIDDEN_ORDER:
                            offenders.append(
                                f"{node.name}: .{sub.func.attr} @{sub.lineno}")
                        if sub.func.attr in self.FORBIDDEN_HTTP_METHODS:
                            offenders.append(
                                f"{node.name}: .{sub.func.attr} @{sub.lineno}")
        self.assertEqual(offenders, [],
            f"M14.G route handlers have forbidden refs: {offenders}")

    def test_importing_dashboard_does_not_load_live_write_or_preflight(self):
        check = (
            "import sys\n"
            "import dashboard.app\n"
            "forbidden = [m for m in (\n"
            "    'tools.etoro_live_write',\n"
            "    'bot.etoro.live_broker',\n"
            "    'bot.risk_authority.preflight',\n"
            ") if m in sys.modules]\n"
            "print('loaded_forbidden:', forbidden)\n"
            "sys.exit(0 if not forbidden else 1)\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", check],
            capture_output=True, text=True, cwd=_REPO,
        )
        self.assertEqual(r.returncode, 0,
            f"dashboard import pulled in forbidden modules. "
            f"stdout={r.stdout!r} stderr={r.stderr!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 5 — Scanner isolation invariant preserved
# ─────────────────────────────────────────────────────────────────────────────


class TestScannerIsolation(unittest.TestCase):

    def test_scanner_does_not_load_m14_g_modules(self):
        check = (
            "import sys\n"
            "import bot.scanner, bot.strategy, bot.risk, bot.brokers\n"
            "forbidden = [m for m in (\n"
            "    'dashboard.app',\n"
            "    'bot.risk_authority.dashboard_read',\n"
            "    'tools.etoro_live_write',\n"
            "    'bot.risk_authority.preflight',\n"
            ") if m in sys.modules]\n"
            "print('loaded_forbidden:', forbidden)\n"
            "sys.exit(0 if not forbidden else 1)\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", check],
            capture_output=True, text=True, cwd=_REPO,
        )
        self.assertEqual(r.returncode, 0,
            f"scanner-isolation violated. stdout={r.stdout!r} "
            f"stderr={r.stderr!r}")

    def test_dashboard_read_does_not_import_scanner_runtime(self):
        """dashboard_read.py must not import bot.scanner/strategy/risk
        or any broker adapter."""
        with open(os.path.join(_REPO,
                                "bot/risk_authority/dashboard_read.py")) as f:
            tree = ast.parse(f.read())
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                m = node.module or ""
                if m in ("bot.scanner", "bot.strategy", "bot.risk",
                          "bot.brokers"):
                    offenders.append(f"ImportFrom {m} @{node.lineno}")
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in ("bot.scanner", "bot.strategy",
                                   "bot.risk", "bot.brokers"):
                        offenders.append(f"Import {a.name} @{node.lineno}")
        self.assertEqual(offenders, [],
            f"dashboard_read.py imports scanner runtime: {offenders}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 6 — known-zero ≠ unknown across JSON boundary
# ─────────────────────────────────────────────────────────────────────────────


class TestKnownZeroSemantics(unittest.TestCase):

    def setUp(self):
        self.fx = _DB()
        self.client, _ = _flask_client(self.fx.path)

    def tearDown(self):
        self.fx.cleanup()

    def test_known_zero_pnl_endpoint(self):
        with self.fx.conn() as c:
            _seed_scope_row(c, scope="etoro_real",
                              pnl_known=True, realised_pnl=0.0,
                              realised_loss=0.0)
        body = self.client.get("/api/risk-authority/scopes").get_json()
        v = body["scopes"]["etoro_real"]
        self.assertTrue(v["pnl_known"])
        self.assertTrue(v["pnl_known_zero"])
        self.assertEqual(v["realised_daily_loss"], 0.0)

    def test_unknown_zero_pnl_endpoint(self):
        with self.fx.conn() as c:
            _seed_scope_row(c, scope="etoro_real",
                              pnl_known=False, realised_pnl=0.0,
                              realised_loss=0.0)
        body = self.client.get("/api/risk-authority/scopes").get_json()
        v = body["scopes"]["etoro_real"]
        self.assertFalse(v["pnl_known"])
        # The critical assertion: known_zero is NOT true when pnl is unknown,
        # even though the numeric values are 0.
        self.assertFalse(v["pnl_known_zero"])
        self.assertEqual(v["realised_daily_loss"], 0.0)

    def test_known_zero_exposure_endpoint(self):
        with self.fx.conn() as c:
            _seed_scope_row(c, scope="etoro_real",
                              exposure_known=True,
                              open_positions=0, capital=0.0)
        body = self.client.get("/api/risk-authority/scopes").get_json()
        v = body["scopes"]["etoro_real"]
        self.assertTrue(v["exposure_known"])
        self.assertTrue(v["exposure_known_zero"])

    def test_unknown_zero_exposure_endpoint(self):
        with self.fx.conn() as c:
            _seed_scope_row(c, scope="etoro_real",
                              exposure_known=False,
                              open_positions=0, capital=0.0)
        body = self.client.get("/api/risk-authority/scopes").get_json()
        v = body["scopes"]["etoro_real"]
        self.assertFalse(v["exposure_known"])
        self.assertFalse(v["exposure_known_zero"])

    def test_four_scopes_always_present_even_when_empty(self):
        body = self.client.get("/api/risk-authority/scopes").get_json()
        self.assertEqual(set(body["scopes"]), set(ALL_BROKER_SCOPES))
        for scope in ALL_BROKER_SCOPES:
            v = body["scopes"][scope]
            self.assertEqual(v["pnl_status"], "absent")
            self.assertFalse(v["pnl_known"])
            self.assertFalse(v["pnl_known_zero"])


# ─────────────────────────────────────────────────────────────────────────────
# Group 7 — Existing dashboard endpoints unchanged
# ─────────────────────────────────────────────────────────────────────────────


class TestExistingEndpointsUnchanged(unittest.TestCase):

    def setUp(self):
        self.fx = _DB()
        self.client, self.app = _flask_client(self.fx.path)

    def tearDown(self):
        self.fx.cleanup()

    def test_api_health_still_responds(self):
        r = self.client.get("/api/health")
        # Existing endpoint may return 200 or 500 depending on
        # underlying state, but it must still be REGISTERED.
        self.assertNotEqual(r.status_code, 404,
            "/api/health is not registered after M14.G")

    def test_api_portfolio_risk_state_registered(self):
        urls = {str(r) for r in self.app.url_map.iter_rules()}
        self.assertIn("/api/portfolio-risk/state", urls)
        self.assertIn("/api/portfolio-risk/snapshots", urls)
        self.assertIn("/api/portfolio-risk/rejections", urls)

    def test_pre_m14g_routes_still_registered(self):
        urls = {str(r) for r in self.app.url_map.iter_rules()}
        # Sample of pre-existing routes that must survive M14.G.
        for pre_existing in ("/api/status", "/api/signals", "/api/logs",
                              "/api/broker-allocation",
                              "/api/kill-switch/state",
                              "/api/strategy"):
            self.assertIn(pre_existing, urls,
                f"pre-M14.G route {pre_existing} missing after M14.G")


# ─────────────────────────────────────────────────────────────────────────────
# Group 8 — Risk Authority tab is visible (HTML present)
# ─────────────────────────────────────────────────────────────────────────────


class TestTabVisible(unittest.TestCase):

    def test_nav_link_present(self):
        with open(os.path.join(_REPO, "dashboard/app.py")) as f:
            src = f.read()
        self.assertIn('id="n-riskauth"', src,
            "Risk Authority nav link missing")
        self.assertIn("loadRiskAuthority()", src,
            "Risk Authority loader function missing")

    def test_page_div_present(self):
        with open(os.path.join(_REPO, "dashboard/app.py")) as f:
            src = f.read()
        self.assertIn('id="riskauth"', src,
            "Risk Authority page div missing")

    def test_manual_reset_button_absent(self):
        """Hard constraint: no manual_reset implementation in M14.G."""
        with open(os.path.join(_REPO, "dashboard/app.py")) as f:
            src = f.read()
        # The string 'manual_reset' can appear in comments and in the
        # `manual_reset_would_be_required` field name (which is a
        # display-only flag). What MUST NOT exist:
        #   * a button onclick handler that submits a manual_reset
        #   * a POST endpoint to /api/risk-authority/manual_reset
        forbidden_button_patterns = (
            "manualReset(", "doManualReset(",
            "/api/risk-authority/manual_reset",
            "/api/risk-authority/reset",
            'onclick="manualReset',
            'onclick="reset_authority',
        )
        for pat in forbidden_button_patterns:
            self.assertNotIn(pat, src,
                f"forbidden manual_reset surface present: {pat!r}")

    def test_no_authority_editing_routes(self):
        from dashboard.app import app
        urls = [str(r) for r in app.url_map.iter_rules()]
        # No POST/PUT/PATCH route under /api/risk-authority/ exists.
        for r in app.url_map.iter_rules():
            if str(r).startswith("/api/risk-authority/"):
                methods = r.methods - {"HEAD", "OPTIONS"}
                self.assertEqual(methods, {"GET"},
                    f"M14.G route {r} has non-GET methods: {methods}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
