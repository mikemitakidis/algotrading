"""test_m15_3_c_audit_export.py — M15.3.C compliance audit/export tests.

Per the M15.3.C pre-code checklist (approved 2026-06-04, with the
operator's corrections inline):

  G1  TestExportAuth                  unauthenticated → 401
  G2  TestExportFormatJSONL           JSONL output parses, manifest first,
                                       sha256 verifies, row counts match
  G3  TestExportFormatCSV             ZIP layout, manifest.txt present,
                                       CSVs RFC-4180 quoted, sha256 verifies
  G4  TestExportScope                 only ALLOWED_KINDS appear in
                                       auth_events; only source='manual_reset'
                                       appears in risk_decisions; no other
                                       tables leak in
  G5  TestExportDateFilters           from/to inclusive, malformed → 400,
                                       to<from → 400, empty range → empty
  G6  TestExportRowCap                100k cap → 400 row_cap_exceeded
  G7  TestExportRedaction             secret-substring match → 500
                                       redaction_violation; failure meta-audit
                                       row written; no secret values returned
  G8  TestExportSelfAudit             every export attempt writes one
                                       audit_export_request row; export_id
                                       in manifest matches extras_json
  G9  TestExportRateLimit             10/hour → 11th gets 429
  G10 TestNoBrokerImports             AST scan — no broker/scanner/strategy/
                                       engine imports in audit_export module
  G11 TestProtectedFilesUntouched     0/24 diff vs M15.3.B-closeout (384e484)
  G12 TestAllowedKindsRegistered      audit_export_request in ALLOWED_KINDS;
                                       runtime snapshot matches live set

Same fixture pattern as M15.3.B: import-first-then-clean against VPS-style
polluted .env, fresh rate-limiters per call, fresh replay-cache per call.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_REPO = str(Path(__file__).resolve().parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_AUTH_ENV_KEYS = (
    "DASHBOARD_PASSWORD_HASH",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_SECRET_KEY",
    "DASHBOARD_HTTPS_MODE",
    "DASHBOARD_COOKIE_SECURE",
    "DASHBOARD_BIND_HOST",
    "DASHBOARD_ACCEPT_PLAINTEXT_EXPOSURE",
    "DASHBOARD_SESSION_IDLE_MIN",
    "DASHBOARD_SESSION_MAX_HOUR",
    "LOGIN_LOCKOUT_WINDOW_SEC",
    "LOGIN_LOCKOUT_THRESHOLD",
    "LOGIN_LOCKOUT_DURATION_SEC",
    "DASHBOARD_TOTP_SECRET",
    "DASHBOARD_PORT",
)


def _clean_auth_env():
    for k in _AUTH_ENV_KEYS:
        os.environ.pop(k, None)


_DASHAPP_SINGLETON = None


def _make_test_app(*, password="testpw-12345", db_path=None,
                    totp_secret=None):
    global _DASHAPP_SINGLETON
    if _DASHAPP_SINGLETON is None:
        from dashboard import app as dashapp
        _DASHAPP_SINGLETON = dashapp
    dashapp = _DASHAPP_SINGLETON

    _clean_auth_env()
    os.environ["DASHBOARD_SECRET_KEY"] = "test_secret_key_M15.3.C_xxxx"
    os.environ["DASHBOARD_PASSWORD"] = password
    if totp_secret is not None:
        os.environ["DASHBOARD_TOTP_SECRET"] = totp_secret

    dashapp.app.config["TESTING"] = True

    from dashboard.auth.sessions import harden_app_config
    import logging
    silent = logging.getLogger("test_silent_m15_3_c")
    silent.addHandler(logging.NullHandler())
    silent.propagate = False
    harden_app_config(dashapp.app, logger=silent)

    from dashboard.auth.rate_limit import RateLimiter
    from dashboard.auth.audit_export import make_export_limiter
    from dashboard.auth.totp import reset_default_replay_cache

    dashapp._m153a_login_limiter = RateLimiter(
        threshold=5, window_sec=600, lockout_sec=900)
    dashapp._m153c_export_limiter = make_export_limiter()
    reset_default_replay_cache()

    if db_path is not None:
        dashapp.DB_PATH = Path(db_path)
        from dashboard.auth.audit import ensure_auth_events_schema
        from bot.flywheel import ensure_daily_state_per_broker_migrations
        c = sqlite3.connect(db_path)
        try:
            ensure_auth_events_schema(c)
            ensure_daily_state_per_broker_migrations(c)
            c.execute("CREATE TABLE IF NOT EXISTS portfolio_risk_state ("
                      "  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
            c.commit()
        finally:
            c.close()
    return dashapp


def _fresh_secret():
    import pyotp
    return pyotp.random_base32(length=32)


def _login(client, *, password="testpw-12345", totp_secret=None):
    body = {"password": password}
    if totp_secret is not None:
        import pyotp
        body["totp_code"] = pyotp.TOTP(totp_secret).now()
    r = client.post("/api/login", json=body)
    if r.status_code != 200:
        return None
    return (r.get_json() or {}).get("csrf_token", "")


def _ensure_schema(db_path):
    """Idempotent — safe to call from a seed helper before _make_test_app."""
    from dashboard.auth.audit import ensure_auth_events_schema
    from bot.flywheel import ensure_daily_state_per_broker_migrations
    c = sqlite3.connect(db_path)
    try:
        ensure_auth_events_schema(c)
        ensure_daily_state_per_broker_migrations(c)
        c.execute(
            "CREATE TABLE IF NOT EXISTS portfolio_risk_state ("
            "  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        c.commit()
    finally:
        c.close()


def _seed_auth_events(db_path, *, count_by_kind):
    """Seed N rows of each kind into auth_events at given dates."""
    _ensure_schema(db_path)
    c = sqlite3.connect(db_path)
    try:
        idx = 0
        for kind, n in count_by_kind.items():
            for i in range(n):
                idx += 1
                day = 2 + (idx % 3)  # 2026-06-02..04
                c.execute(
                    "INSERT INTO auth_events "
                    "(ts_utc, kind, client_ip, user_agent, "
                    " session_id, success, extras_json) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f"2026-06-0{day}T10:00:{idx:02d}+00:00",
                     kind, "1.2.3.4", "TestUA/1.0", "hashedsess",
                     1, json.dumps({"seed_index": idx})))
        c.commit()
    finally:
        c.close()


def _seed_risk_decisions(db_path, *, manual_reset_n=1, other_sources_n=0):
    _ensure_schema(db_path)
    c = sqlite3.connect(db_path)
    try:
        for i in range(manual_reset_n):
            c.execute(
                "INSERT INTO risk_decisions "
                "(decision_id, taken_at, broker_scope, requested_action, "
                " result, authority_before, authority_after, "
                " reason_codes, source, actor, explainer, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"mr-seed{i:04d}",
                 f"2026-06-02T11:00:{i:02d}+00:00",
                 "GLOBAL", "query_authority", "allow", "OFF", "OFF",
                 json.dumps(["manual_reset"]), "manual_reset", "operator",
                 f"seed {i}", f"2026-06-02T11:00:{i:02d}+00:00"))
        # Also seed some OTHER-source rows that MUST NOT appear in exports.
        for i in range(other_sources_n):
            c.execute(
                "INSERT INTO risk_decisions "
                "(decision_id, taken_at, broker_scope, requested_action, "
                " result, authority_before, authority_after, "
                " reason_codes, source, actor, explainer, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"auto-seed{i:04d}",
                 f"2026-06-02T12:00:{i:02d}+00:00",
                 "ibkr_paper", "trade_open", "allow",
                 "AUTO_ALLOWED", "AUTO_ALLOWED",
                 json.dumps(["ok"]), "auto", "engine",
                 f"auto seed {i}",
                 f"2026-06-02T12:00:{i:02d}+00:00"))
        c.commit()
    finally:
        c.close()


# ─────────────────────────────────────────────────────────────────────────────
# G1 — endpoint auth gate
# ─────────────────────────────────────────────────────────────────────────────


class TestExportAuth(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def test_unauthenticated_returns_401(self):
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=_fresh_secret())
        cli = dashapp.app.test_client()
        r = cli.get("/api/audit-export?from=2026-06-01&to=2026-06-04")
        self.assertEqual(r.status_code, 401)

    def test_post_method_returns_405(self):
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=_fresh_secret())
        cli = dashapp.app.test_client()
        r = cli.post("/api/audit-export")
        self.assertEqual(r.status_code, 405)


# ─────────────────────────────────────────────────────────────────────────────
# G2 — JSONL output
# ─────────────────────────────────────────────────────────────────────────────


class TestExportFormatJSONL(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _setup(self):
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        csrf = _login(cli, totp_secret=secret)
        self.assertIsNotNone(csrf)
        return cli

    def test_jsonl_returns_200_with_correct_content_type(self):
        _seed_auth_events(self.tmp_db.name,
                            count_by_kind={"login_success": 2,
                                             "manual_reset_success": 1})
        _seed_risk_decisions(self.tmp_db.name, manual_reset_n=1)
        cli = self._setup()
        r = cli.get("/api/audit-export?format=jsonl&from=2026-06-01"
                       "&to=2026-06-30")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"],
                          "application/x-ndjson")
        self.assertIn(".jsonl", r.headers["Content-Disposition"])

    def test_jsonl_manifest_is_first_line_and_well_formed(self):
        _seed_auth_events(self.tmp_db.name,
                            count_by_kind={"login_success": 1})
        cli = self._setup()
        r = cli.get("/api/audit-export?format=jsonl"
                       "&from=2026-06-01&to=2026-06-30")
        lines = r.data.split(b"\n")
        manifest = json.loads(lines[0])
        for k in ("_schema_version", "_export_id", "_generated_at_utc",
                   "_generated_by_actor", "_date_range", "_row_counts",
                   "_sha256_payload", "_format"):
            self.assertIn(k, manifest, f"manifest missing {k!r}")
        self.assertEqual(manifest["_schema_version"], 1)
        self.assertEqual(manifest["_format"], "jsonl")
        self.assertEqual(manifest["_generated_by_actor"], "operator")
        self.assertTrue(manifest["_export_id"].startswith("exp-"))

    def test_jsonl_sha256_verifies(self):
        _seed_auth_events(self.tmp_db.name,
                            count_by_kind={"login_success": 3})
        _seed_risk_decisions(self.tmp_db.name, manual_reset_n=2)
        cli = self._setup()
        r = cli.get("/api/audit-export?format=jsonl"
                       "&from=2026-06-01&to=2026-06-30")
        lines = r.data.split(b"\n")
        manifest = json.loads(lines[0])
        body_after_manifest = b"\n".join(lines[1:])
        actual = hashlib.sha256(body_after_manifest).hexdigest()
        self.assertEqual(manifest["_sha256_payload"], actual,
            "manifest sha256 must match body SHA-256")

    def test_jsonl_row_counts_match_body(self):
        _seed_auth_events(self.tmp_db.name,
                            count_by_kind={"login_success": 4,
                                             "manual_reset_preview": 2})
        _seed_risk_decisions(self.tmp_db.name, manual_reset_n=3)
        cli = self._setup()
        r = cli.get("/api/audit-export?format=jsonl"
                       "&from=2026-06-01&to=2026-06-30")
        lines = [l for l in r.data.split(b"\n") if l]
        manifest = json.loads(lines[0])
        body_lines = [json.loads(l) for l in lines[1:]]
        auth_count = sum(1 for l in body_lines
                          if l["_source"] == "auth_events")
        rd_count = sum(1 for l in body_lines
                        if l["_source"] == "risk_decisions_manual_reset")
        # Counts in body MUST match the manifest. We don't compare to
        # the seeded count directly because the export-call itself
        # writes a meta-audit row that bumps auth_events.
        self.assertEqual(manifest["_row_counts"]["auth_events"], auth_count)
        self.assertEqual(
            manifest["_row_counts"]["risk_decisions_manual_reset"],
            rd_count)


# ─────────────────────────────────────────────────────────────────────────────
# G3 — CSV-ZIP output
# ─────────────────────────────────────────────────────────────────────────────


class TestExportFormatCSV(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _setup(self):
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        _login(cli, totp_secret=secret)
        return cli

    def test_csv_returns_zip_with_three_files(self):
        _seed_auth_events(self.tmp_db.name,
                            count_by_kind={"login_success": 1})
        _seed_risk_decisions(self.tmp_db.name, manual_reset_n=1)
        cli = self._setup()
        r = cli.get("/api/audit-export?format=csv"
                       "&from=2026-06-01&to=2026-06-30")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "application/zip")
        self.assertIn(".zip", r.headers["Content-Disposition"])
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = sorted(zf.namelist())
        self.assertEqual(names, [
            "auth_events.csv",
            "manifest.txt",
            "risk_decisions_manual_reset.csv",
        ])

    def test_csv_manifest_txt_contains_key_fields(self):
        _seed_auth_events(self.tmp_db.name,
                            count_by_kind={"login_success": 1})
        cli = self._setup()
        r = cli.get("/api/audit-export?format=csv"
                       "&from=2026-06-01&to=2026-06-30")
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        manifest_txt = zf.read("manifest.txt").decode("utf-8")
        for k in ("_schema_version:", "_export_id:", "_generated_at_utc:",
                   "_sha256_payload:", "_format: csv",
                   "_date_range.from_iso:", "_date_range.to_iso:",
                   "_row_counts.auth_events:",
                   "_row_counts.risk_decisions_manual_reset:"):
            self.assertIn(k, manifest_txt,
                          f"manifest.txt missing {k!r}")

    def test_csv_files_parse_as_rfc4180(self):
        """Ensure rows with embedded commas/quotes/newlines round-trip
        correctly via csv.reader."""
        import csv as _csv
        _ensure_schema(self.tmp_db.name)
        # Seed a row whose extras_json contains commas and quotes.
        c = sqlite3.connect(self.tmp_db.name)
        try:
            c.execute(
                "INSERT INTO auth_events "
                "(ts_utc, kind, client_ip, user_agent, "
                " session_id, success, extras_json) "
                "VALUES (?,?,?,?,?,?,?)",
                ("2026-06-02T10:00:00+00:00",
                 "manual_reset_success", "1.2.3.4", "UA",
                 "sh", 1,
                 json.dumps({"reason": 'has "quotes", and commas, '
                                          'and\nnewlines'})))
            c.commit()
        finally:
            c.close()
        cli = self._setup()
        r = cli.get("/api/audit-export?format=csv"
                       "&from=2026-06-01&to=2026-06-30")
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        auth_csv = zf.read("auth_events.csv").decode("utf-8")
        rows = list(_csv.reader(io.StringIO(auth_csv)))
        # Header + at least the seeded row.
        self.assertGreaterEqual(len(rows), 2)
        header = rows[0]
        self.assertEqual(header[0], "id")
        # Find the row with our payload. JSON-encodes the embedded
        # quotes (`"` → `\"`), so we search for the un-escaped tokens
        # `quotes`, `commas`, `newlines` which survive both JSON
        # encoding and CSV reader RFC-4180 de-quoting.
        found = False
        for row in rows[1:]:
            extras_str = row[-1]
            if extras_str and "quotes" in extras_str and "commas" in extras_str and "newlines" in extras_str:
                # The CSV reader successfully de-quoted the cell — all
                # three tokens that survive JSON encoding made it
                # through the CSV round-trip in a single cell.
                found = True
        self.assertTrue(found, "embedded-special-chars row not roundtripped")

    def test_csv_sha256_verifies(self):
        _seed_auth_events(self.tmp_db.name,
                            count_by_kind={"login_success": 1})
        _seed_risk_decisions(self.tmp_db.name, manual_reset_n=1)
        cli = self._setup()
        r = cli.get("/api/audit-export?format=csv"
                       "&from=2026-06-01&to=2026-06-30")
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        manifest_txt = zf.read("manifest.txt").decode("utf-8")
        auth_csv = zf.read("auth_events.csv")
        rd_csv = zf.read("risk_decisions_manual_reset.csv")
        # Extract sha from manifest.txt
        sha_line = [l for l in manifest_txt.splitlines()
                     if l.startswith("_sha256_payload:")][0]
        manifest_sha = sha_line.split(":", 1)[1].strip()
        h = hashlib.sha256()
        h.update(auth_csv)
        h.update(rd_csv)
        self.assertEqual(manifest_sha, h.hexdigest())


# ─────────────────────────────────────────────────────────────────────────────
# G4 — export scope (Q-C.1)
# ─────────────────────────────────────────────────────────────────────────────


class TestExportScope(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _setup(self):
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        _login(cli, totp_secret=secret)
        return cli

    def test_only_allowed_kinds_appear_in_export(self):
        from dashboard.auth.audit import ALLOWED_KINDS
        _seed_auth_events(self.tmp_db.name,
                            count_by_kind={"login_success": 1,
                                             "manual_reset_preview": 1,
                                             "totp_success": 1})
        cli = self._setup()
        r = cli.get("/api/audit-export?format=jsonl"
                       "&from=2026-06-01&to=2026-06-30")
        lines = [json.loads(l) for l in r.data.split(b"\n") if l]
        for entry in lines[1:]:
            if entry["_source"] == "auth_events":
                self.assertIn(entry["kind"], ALLOWED_KINDS,
                    f"unexpected kind in export: {entry['kind']!r}")

    def test_risk_decisions_non_manual_reset_excluded(self):
        """Per Q-C.1: only source='manual_reset' rows appear.
        Seed 2 manual_reset + 5 'auto' + 3 'manual' + 2 'reconciled'.
        Export must contain ONLY the 2 manual_reset rows."""
        _seed_risk_decisions(self.tmp_db.name,
                              manual_reset_n=2,
                              other_sources_n=5)
        # Add the other-source kinds too:
        c = sqlite3.connect(self.tmp_db.name)
        try:
            for i in range(3):
                c.execute(
                    "INSERT INTO risk_decisions "
                    "(decision_id, taken_at, broker_scope, requested_action, "
                    " result, authority_before, authority_after, "
                    " reason_codes, source, actor, explainer, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"manual-{i}", "2026-06-02T13:00:00+00:00",
                     "ibkr_paper", "trade_open", "block",
                     "OFF", "OFF",
                     json.dumps(["foo"]), "manual", "operator",
                     "test", "2026-06-02T13:00:00+00:00"))
            for i in range(2):
                c.execute(
                    "INSERT INTO risk_decisions "
                    "(decision_id, taken_at, broker_scope, requested_action, "
                    " result, authority_before, authority_after, "
                    " reason_codes, source, actor, explainer, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"recon-{i}", "2026-06-02T14:00:00+00:00",
                     "ibkr_paper", "query_authority", "allow",
                     "OFF", "OFF",
                     json.dumps(["x"]), "reconciled", "engine",
                     "test", "2026-06-02T14:00:00+00:00"))
            c.commit()
        finally:
            c.close()
        cli = self._setup()
        r = cli.get("/api/audit-export?format=jsonl"
                       "&from=2026-06-01&to=2026-06-30")
        lines = [json.loads(l) for l in r.data.split(b"\n") if l]
        manifest = lines[0]
        self.assertEqual(
            manifest["_row_counts"]["risk_decisions_manual_reset"], 2,
            "exactly 2 manual_reset rows expected; other sources EXCLUDED")
        for entry in lines[1:]:
            if entry["_source"] == "risk_decisions_manual_reset":
                self.assertEqual(entry["source"], "manual_reset")

    def test_no_signals_or_execution_intents_leak_in(self):
        """The export MUST NOT touch signals / execution_intents / etc."""
        cli = self._setup()
        r = cli.get("/api/audit-export?format=jsonl"
                       "&from=2026-06-01&to=2026-06-30")
        lines = [json.loads(l) for l in r.data.split(b"\n") if l]
        for entry in lines[1:]:
            self.assertIn(entry["_source"],
                            ("auth_events", "risk_decisions_manual_reset"),
                f"unexpected _source: {entry['_source']!r}")


# ─────────────────────────────────────────────────────────────────────────────
# G5 — date filters
# ─────────────────────────────────────────────────────────────────────────────


class TestExportDateFilters(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _setup(self):
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        _login(cli, totp_secret=secret)
        return cli

    def test_from_to_inclusive_window(self):
        _seed_auth_events(self.tmp_db.name,
                            count_by_kind={"login_success": 3})
        cli = self._setup()
        # from=06-02 to=06-04 — all 3 seeded rows on days 2/3/4 must appear.
        r = cli.get("/api/audit-export?format=jsonl"
                       "&from=2026-06-02&to=2026-06-04")
        manifest = json.loads(r.data.split(b"\n")[0])
        self.assertGreaterEqual(manifest["_row_counts"]["auth_events"], 3)

    def test_malformed_date_returns_400(self):
        cli = self._setup()
        r = cli.get("/api/audit-export?format=jsonl&from=not-a-date")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "date_format_invalid")

    def test_reversed_range_returns_400(self):
        cli = self._setup()
        r = cli.get("/api/audit-export?format=jsonl"
                       "&from=2026-06-30&to=2026-06-01")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "date_range_invalid")

    def test_empty_range_returns_valid_empty_manifest(self):
        cli = self._setup()
        # No rows seeded; both dates in the future.
        r = cli.get("/api/audit-export?format=jsonl"
                       "&from=2099-01-01&to=2099-01-02")
        self.assertEqual(r.status_code, 200)
        lines = [l for l in r.data.split(b"\n") if l]
        manifest = json.loads(lines[0])
        # Exactly the manifest line, no body.
        self.assertEqual(len(lines), 1)
        self.assertEqual(manifest["_row_counts"]["auth_events"], 0)
        self.assertEqual(
            manifest["_row_counts"]["risk_decisions_manual_reset"], 0)
        # SHA-256 of empty body
        self.assertEqual(manifest["_sha256_payload"],
                          hashlib.sha256(b"").hexdigest())

    def test_format_invalid_returns_400(self):
        cli = self._setup()
        r = cli.get("/api/audit-export?format=xml&from=2026-06-01")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "format_invalid")


# ─────────────────────────────────────────────────────────────────────────────
# G6 — row cap
# ─────────────────────────────────────────────────────────────────────────────


class TestExportRowCap(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def test_row_cap_exceeded_returns_400(self):
        from dashboard.auth import audit_export as ae
        # Monkey-patch the cap down so we can test cheaply.
        original = ae.MAX_EXPORT_ROWS
        try:
            ae.MAX_EXPORT_ROWS = 5
            _seed_auth_events(self.tmp_db.name,
                                count_by_kind={"login_success": 10})
            secret = _fresh_secret()
            dashapp = _make_test_app(db_path=self.tmp_db.name,
                                       totp_secret=secret)
            cli = dashapp.app.test_client()
            _login(cli, totp_secret=secret)
            r = cli.get("/api/audit-export?format=jsonl"
                           "&from=2026-06-01&to=2026-06-30")
            self.assertEqual(r.status_code, 400)
            d = r.get_json()
            self.assertEqual(d["error"], "row_cap_exceeded")
            self.assertEqual(d["max_rows"], 5)
            self.assertIn("hint", d)
            self.assertIn("row_counts", d)
        finally:
            ae.MAX_EXPORT_ROWS = original


# ─────────────────────────────────────────────────────────────────────────────
# G7 — redaction (Q-C.5 fail-fast)
# ─────────────────────────────────────────────────────────────────────────────


class TestExportRedaction(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def test_scan_for_secrets_finds_env_secret(self):
        from dashboard.auth.audit_export import scan_for_secrets
        secret_value = "SECRET_VALUE_THAT_IS_LONG_ENOUGH_TO_QUALIFY"
        clean, viol = scan_for_secrets(
            b"some text " + secret_value.encode() + b" more text",
            env_overrides={"DASHBOARD_TOTP_SECRET": secret_value},
        )
        self.assertFalse(clean)
        self.assertIn("DASHBOARD_TOTP_SECRET", viol)

    def test_scan_for_secrets_ignores_short_values(self):
        """Per audit_export design: env values shorter than threshold
        are skipped to avoid false positives."""
        from dashboard.auth.audit_export import scan_for_secrets
        clean, viol = scan_for_secrets(
            b"...abc...",
            env_overrides={"DASHBOARD_PASSWORD": "abc"},
        )
        self.assertTrue(clean)
        self.assertEqual(viol, [])

    def test_scan_for_secrets_catches_otpauth_uri(self):
        from dashboard.auth.audit_export import scan_for_secrets
        clean, viol = scan_for_secrets(
            b"... someone leaked otpauth://totp/Bot? ...",
            env_overrides={},
        )
        self.assertFalse(clean)
        self.assertIn("otpauth_uri", viol)

    def test_redaction_violation_endpoint_returns_500(self):
        """If a known secret is detected in the export body, the
        endpoint fails fast with redaction_violation."""
        # Seed an audit row whose extras_json contains a faked TOTP
        # secret pattern. This is contrived — the audit invariant
        # guarantees this never happens in production — but it tests
        # the defence-in-depth scan.
        # Use a valid base32 string so pyotp can use it for login.
        leaked_secret = "JBSWY3DPEHPK3PXPHGNSWAYABCDEFGHI"
        _ensure_schema(self.tmp_db.name)
        c = sqlite3.connect(self.tmp_db.name)
        try:
            c.execute(
                "INSERT INTO auth_events "
                "(ts_utc, kind, client_ip, user_agent, "
                " session_id, success, extras_json) "
                "VALUES (?,?,?,?,?,?,?)",
                ("2026-06-02T10:00:00+00:00",
                 "login_success", "1.2.3.4", "UA",
                 "sh", 1,
                 # Pretend a buggy audit writer leaked the secret:
                 json.dumps({"oops_leak": leaked_secret})))
            c.commit()
        finally:
            c.close()
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        # Override the env so the scanner has a known secret to find.
        os.environ["DASHBOARD_TOTP_SECRET"] = leaked_secret
        cli = dashapp.app.test_client()
        _login(cli, totp_secret=leaked_secret)
        r = cli.get("/api/audit-export?format=jsonl"
                       "&from=2026-06-01&to=2026-06-30")
        self.assertEqual(r.status_code, 500)
        d = r.get_json()
        self.assertEqual(d["error"], "redaction_violation")
        # Response carries labels (DASHBOARD_TOTP_SECRET) but NOT the value.
        self.assertIn("DASHBOARD_TOTP_SECRET", d["violation_labels"])
        body_text = json.dumps(d)
        self.assertNotIn(leaked_secret, body_text,
            "response must NOT contain the leaked secret value")
        # Meta-audit row carries labels but not the secret value.
        c = sqlite3.connect(self.tmp_db.name)
        try:
            row = c.execute(
                "SELECT extras_json FROM auth_events "
                "WHERE kind='audit_export_request' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            c.close()
        self.assertIsNotNone(row)
        extras = json.loads(row[0])
        self.assertEqual(extras["reason"], "redaction_violation")
        self.assertIn("DASHBOARD_TOTP_SECRET",
                       extras.get("redaction_violations", []))
        # Critical: extras_json string must NOT contain the leaked value.
        self.assertNotIn(leaked_secret, row[0])


# ─────────────────────────────────────────────────────────────────────────────
# G8 — self-audit
# ─────────────────────────────────────────────────────────────────────────────


class TestExportSelfAudit(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _setup(self):
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        _login(cli, totp_secret=secret)
        return cli

    def test_success_writes_audit_export_request_row(self):
        cli = self._setup()
        r = cli.get("/api/audit-export?format=jsonl"
                       "&from=2026-06-01&to=2026-06-30")
        self.assertEqual(r.status_code, 200)
        manifest = json.loads(r.data.split(b"\n")[0])
        export_id_from_manifest = manifest["_export_id"]
        c = sqlite3.connect(self.tmp_db.name)
        try:
            row = c.execute(
                "SELECT success, extras_json FROM auth_events "
                "WHERE kind='audit_export_request' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            c.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)
        extras = json.loads(row[1])
        self.assertEqual(extras["export_id"], export_id_from_manifest,
            "manifest export_id MUST match audit row's export_id")

    def test_failure_writes_audit_export_request_failure_row(self):
        cli = self._setup()
        cli.get("/api/audit-export?format=xml")
        c = sqlite3.connect(self.tmp_db.name)
        try:
            row = c.execute(
                "SELECT success, extras_json FROM auth_events "
                "WHERE kind='audit_export_request' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            c.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 0)
        extras = json.loads(row[1])
        self.assertEqual(extras["reason"], "format_invalid")


# ─────────────────────────────────────────────────────────────────────────────
# G9 — rate limit
# ─────────────────────────────────────────────────────────────────────────────


class TestExportRateLimit(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def test_ten_per_hour_then_429(self):
        """Per Q-C.8: 10 exports/hour/IP. The 11th gets 429.
        The limiter only counts FAILED attempts as recorded failures,
        but successful attempts also bump the counter via the standard
        RateLimiter API. To test reliably: drive 10 failed-format calls,
        then assert the 11th is 429."""
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        _login(cli, totp_secret=secret)
        for i in range(10):
            r = cli.get("/api/audit-export?format=xml")  # invalid format
            self.assertEqual(r.status_code, 400)
        r = cli.get("/api/audit-export?format=jsonl"
                       "&from=2026-06-01&to=2026-06-04")
        self.assertEqual(r.status_code, 429)
        d = r.get_json()
        self.assertEqual(d["error"], "rate_limited")
        self.assertGreater(d["retry_after_sec"], 0)


# ─────────────────────────────────────────────────────────────────────────────
# G10 — AST scan: no broker / scanner / strategy / engine imports
# ─────────────────────────────────────────────────────────────────────────────


class TestNoBrokerImports(unittest.TestCase):
    _FORBIDDEN_IMPORT_MODULES = (
        "ib_insync",
        "ibapi",
        "bot.broker_ibkr",
        "bot.broker_etoro",
        "bot.broker_router",
        "bot.broker_paper",
        "bot.gateway_health",
        "bot.gateway_watchdog",
        "bot.scanner",
        "bot.strategy",
        "bot.risk_authority.engine",
        "bot.risk_authority.governor",
        "bot.risk_authority.snapshot",
        "bot.risk_authority.preflight",
        "bot.risk_authority.ibkr_paper_reader",
        "bot.risk_authority.audit_decisions",  # M14 audit-write; export is read-only
        "bot.etoro.live_broker",
    )
    _FORBIDDEN_METHOD_NAMES = (
        "placeOrder", "place_order",
        "cancelOrder", "cancel_order",
        "modifyOrder", "modify_order",
        "closePosition", "close_position",
        "submitOrder", "submit_order",
    )

    def _read(self, relpath):
        return (Path(_REPO) / relpath).read_text(encoding="utf-8")

    def _imported_modules(self, src):
        out = set()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    out.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                out.add(mod)
                for alias in node.names:
                    if alias.name and mod:
                        out.add(f"{mod}.{alias.name}")
        return out

    def test_audit_export_module_no_broker_imports(self):
        src = self._read("dashboard/auth/audit_export.py")
        imported = self._imported_modules(src)
        for forbidden in self._FORBIDDEN_IMPORT_MODULES:
            for imp in imported:
                self.assertFalse(
                    imp == forbidden or imp.startswith(forbidden + "."),
                    f"audit_export imports {imp!r} (matches forbidden "
                    f"{forbidden!r})")

    def test_audit_export_module_no_broker_method_strings(self):
        src = self._read("dashboard/auth/audit_export.py")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for forbidden in self._FORBIDDEN_METHOD_NAMES:
                    self.assertNotIn(forbidden, node.value,
                        f"audit_export string literal contains broker "
                        f"method name {forbidden!r}: {node.value!r}")

    def test_audit_export_endpoint_no_broker_imports(self):
        """The m153c_audit_export endpoint body in dashboard/app.py
        must not import broker code."""
        src = self._read("dashboard/app.py")
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                and node.name == "m153c_audit_export"):
                target = node
                break
        self.assertIsNotNone(target)
        for inner in ast.walk(target):
            if isinstance(inner, ast.ImportFrom):
                mod = inner.module or ""
                for forbidden in self._FORBIDDEN_IMPORT_MODULES:
                    self.assertFalse(
                        mod == forbidden or mod.startswith(forbidden + "."),
                        f"m153c_audit_export imports from {mod!r} "
                        f"(matches forbidden {forbidden!r})")


# ─────────────────────────────────────────────────────────────────────────────
# G11 — protected files
# ─────────────────────────────────────────────────────────────────────────────


class TestProtectedFilesUntouched(unittest.TestCase):
    """M15.3.C must NOT modify protected runtime files vs 384e484
    (M15.3.B closeout HEAD)."""

    _BASELINE = "384e484"
    _PROTECTED = (
        "main.py",
        "bot/scanner.py",
        "bot/strategy.py",
        "bot/risk.py",
        "bot/risk_authority/engine.py",
        "bot/risk_authority/governor.py",
        "bot/risk_authority/authority.py",
        "bot/risk_authority/snapshot.py",
        "bot/risk_authority/audit_decisions.py",  # M15.3.B touched it; frozen now
        "bot/risk_authority/preflight.py",
        "bot/risk_authority/ingest_ibkr_exposure.py",
        "bot/risk_authority/ibkr_paper_reader.py",
        "bot/risk_authority/exposure_reading.py",
        "bot/risk_authority/ingest_exposure.py",
        "bot/gateway_health.py",
        "bot/gateway_watchdog.py",
        "bot/etoro/live_broker.py",
        "tools/etoro_live_write.py",
        "tools/ingest_exposure_state.py",
        "infra/systemd/algo-trader.service",
        "infra/systemd/algo-trader-dashboard.service",
        "infra/systemd/ibgateway.service.documented",
        "sync.sh",
        "deploy.sh",
        # Also: dashboard/auth/manual_reset.py — M15.3.B's helper module
        # is now frozen; M15.3.C must not modify it.
        "dashboard/auth/manual_reset.py",
    )

    def test_zero_protected_files_changed_vs_baseline(self):
        try:
            modified = []
            for f in self._PROTECTED:
                full = Path(_REPO) / f
                if not full.exists():
                    continue
                r = subprocess.run(
                    ["git", "diff", "--stat", self._BASELINE, "--", f],
                    cwd=_REPO, capture_output=True, text=True, timeout=10,
                )
                if r.stdout.strip():
                    modified.append(f)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            self.skipTest("git not available")
            return
        self.assertEqual(modified, [],
            f"M15.3.C must not modify protected files; modified: {modified}")


# ─────────────────────────────────────────────────────────────────────────────
# G12 — ALLOWED_KINDS registered + drift check
# ─────────────────────────────────────────────────────────────────────────────


class TestAllowedKindsRegistered(unittest.TestCase):
    def test_audit_export_request_in_allowed_kinds(self):
        from dashboard.auth.audit import ALLOWED_KINDS
        self.assertIn("audit_export_request", ALLOWED_KINDS)

    def test_module_snapshot_matches_live_allowed_kinds(self):
        """audit_export.py snapshots ALLOWED_KINDS at import time. The
        snapshot must match the live set — catches drift."""
        from dashboard.auth.audit import ALLOWED_KINDS
        from dashboard.auth.audit_export import (
            ALLOWED_AUTH_EVENT_KINDS_AT_EXPORT_TIME,
        )
        self.assertEqual(set(ALLOWED_AUTH_EVENT_KINDS_AT_EXPORT_TIME),
                          set(ALLOWED_KINDS))
