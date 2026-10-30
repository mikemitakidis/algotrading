"""M21 E6 — read-only dashboard view of the template configuration (isolated; dashboard/app.py is NOT changed).

The blueprint is mounted on a bare Flask app, exactly as the D3 view tests do. The store is the real WP9
``bot.templates.controls`` store in a temporary file; changes in these tests are made the way WP14 applies
them (through ``TemplateControls``), never through the dashboard.

Release-safety review (30 Oct): the first version had propose/approve/activate/rollback/withdraw routes.
They changed the active configuration with no WP14 promotion (no shadow evidence, no change budget, no
operator token). They were removed; ``ReadOnlyByDesign`` pins that no route can change the store.
"""
import ast
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import _test_isolation  # noqa: E402  (C5a: isolate before any dashboard import)
_test_isolation.install()

from flask import Flask  # noqa: E402

from bot.templates import controls as TC  # noqa: E402
from bot.templates.routing import LIVE_TEMPLATES  # noqa: E402
from dashboard import template_controls_view as V  # noqa: E402

REPO = Path(__file__).resolve().parent
CFG2 = {"lane": "paper", "selected": ["scanner", "mean_reversion"], "champion": "scanner",
        "params": {"mean_reversion": {"rsi_entry": 25.0}}}
OLD_WRITE_ROUTES = ("propose", "approve", "activate", "rollback", "withdraw")


def clock():
    return datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def sha(path):
    with open(path, "rb") as fh:
        return fh.read()


class Base(unittest.TestCase):
    configured = True

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="e6_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = os.path.join(self.dir, "controls.db")
        app = Flask("e6_test")
        app.secret_key = "e6-test-only"
        if self.configured:
            app.config[V.DB_KEY] = self.db
        app.register_blueprint(V.bp)
        self.app = app
        self.client = app.test_client()

    def login(self):
        with self.client.session_transaction() as s:
            s["authed"] = True
            s["csrf_token"] = "tok"

    def apply_like_wp14(self, cfg=CFG2, expected=None):
        """What WP14's apply does: propose -> approve (named human) -> activate with compare-and-swap."""
        st = TC.TemplateControls(self.db, clock=clock)
        v = st.propose(cfg, author="wp14", reason="promotion q1")
        st.approve(v, approver="Mike M", reason="token ok")
        st.activate(v, actor="wp14", expected_current=expected)
        return v


class Access(Base):
    def test_every_route_requires_auth(self):
        for url in ("/api/template-controls", "/template-controls"):
            self.assertEqual(self.client.get(url).status_code, 401, url)
        self.assertFalse(os.path.exists(self.db))

    def test_get_never_creates_the_store(self):
        self.login()
        r = self.client.get("/api/template-controls")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["read_only"])
        self.assertEqual((d["active"]["source"], d["active"]["version"]), (TC.SOURCE_DEFAULT, None))
        self.assertEqual(d["active"]["config"], TC.default_config())
        self.assertTrue(d["store_problem"].startswith("store_unreadable"))
        self.assertEqual(d["live_templates"], sorted(LIVE_TEMPLATES))
        self.assertIn("rsi_entry", d["templates"]["mean_reversion"]["params"])
        self.assertFalse(os.path.exists(self.db))


class NotConfigured(Base):
    configured = False

    def test_unconfigured_is_503_and_the_environment_can_configure_the_path(self):
        self.login()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(V.DB_KEY, None)
            self.assertEqual(self.client.get("/api/template-controls").status_code, 503)
            self.assertEqual(self.client.get("/template-controls").status_code, 503)
        with mock.patch.dict(os.environ, {V.DB_KEY: self.db}):
            self.assertEqual(self.client.get("/api/template-controls").status_code, 200)


class ReadOnlyByDesign(Base):
    def test_no_route_can_change_the_store(self):
        self.login()
        self.apply_like_wp14()
        before = sha(self.db)
        with mock.patch.dict(os.environ, {"TEMPLATE_CONTROLS_WRITES_ENABLED": "1"}):   # the old switch is gone
            self.app.config["TEMPLATE_CONTROLS_WRITES_ENABLED"] = True
            for url in ["/api/template-controls", "/template-controls"] + \
                       ["/api/template-controls/" + a for a in OLD_WRITE_ROUTES]:
                for m in ("post", "put", "patch", "delete"):
                    r = getattr(self.client, m)(url, json={"version": 1, "config": CFG2, "author": "x",
                                                           "approver": "Mike M", "actor": "x", "reason": "r",
                                                           "expected_current": 1},
                                                headers={"X-CSRF-Token": "tok"})
                    self.assertIn(r.status_code, (404, 405), (m, url))
        self.assertEqual(sha(self.db), before)
        self.assertEqual(TC.read_active(self.db).version, 1)
        rules = [(r.rule, sorted(r.methods - {"HEAD", "OPTIONS"})) for r in self.app.url_map.iter_rules()
                 if r.endpoint.startswith("template_controls.")]
        self.assertEqual(sorted(rules), [("/api/template-controls", ["GET"]), ("/template-controls", ["GET"])])


class State(Base):
    def test_shows_what_the_promotion_process_applied(self):
        self.login()
        v1 = self.apply_like_wp14()
        cfg3 = dict(CFG2, params={"mean_reversion": {"rsi_entry": 28.0}})
        v2 = self.apply_like_wp14(cfg3, expected=v1)
        d = self.client.get("/api/template-controls").get_json()
        self.assertEqual((d["active"]["version"], d["active"]["source"]), (v2, TC.SOURCE_STORE))
        self.assertEqual(d["active"]["config"]["params"]["mean_reversion"]["rsi_entry"], 28.0)
        self.assertEqual([(v["version"], v["status"], v["intact"]) for v in d["versions"]],
                         [(v1, TC.SUPERSEDED, True), (v2, TC.ACTIVE, True)])
        self.assertIn("Promotion & Approval", d["change_path"])

    def test_a_tampered_version_is_shown_as_such_and_the_last_good_one_is_active(self):
        self.login()
        v1 = self.apply_like_wp14()
        v2 = self.apply_like_wp14(dict(CFG2, champion="mean_reversion"), expected=v1)
        import sqlite3
        c = sqlite3.connect(self.db)
        with c:
            c.execute("UPDATE template_config_versions SET config_json=? WHERE version=?",
                      (json.dumps(dict(CFG2, champion="mean_reversion", lane="live")), v2))
        c.close()
        d = self.client.get("/api/template-controls").get_json()
        self.assertEqual((d["active"]["version"], d["active"]["source"]), (v1, TC.SOURCE_LAST_GOOD))
        self.assertEqual({v["version"]: v["intact"] for v in d["versions"]}, {v1: True, v2: False})


class Page(Base):
    def test_page_is_escaped_and_has_no_forms(self):
        self.login()
        st = TC.TemplateControls(self.db, clock=clock)
        st.propose(CFG2, author="<script>alert(1)</script>", reason="<b>r</b>")
        html = self.client.get("/template-controls").data.decode()
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<b>r</b>", html)
        for s in ("<form", "csrf_token", "<button", "method=\"post\""):
            self.assertNotIn(s, html)
        self.assertIn("Read-only. Paper lane only.", html)


class Isolation(unittest.TestCase):
    SRC = (REPO / "dashboard" / "template_controls_view.py").read_text()

    def test_imports_and_no_writer(self):
        tree = ast.parse(self.SRC)
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add(n.module or "")
        self.assertEqual(sorted(m for m in mods if m.startswith(("bot", "dashboard", "tools", "research"))),
                         ["bot.templates", "bot.templates.core", "bot.templates.routing"])
        for bad in ("requests", "urllib.request", "socket", "ib_insync", "subprocess"):
            self.assertNotIn(bad, mods)
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse(calls & {"propose", "approve", "activate", "rollback", "withdraw", "commit"}, calls)
        self.assertNotIn("TemplateControls(", self.SRC)
        self.assertNotIn("methods=[\"POST\"]", self.SRC)
        for stmt in ("INSERT", "UPDATE", "DELETE", "CREATE"):
            self.assertNotIn(stmt + " ", self.SRC)

    def test_dashboard_app_does_not_register_it(self):
        src = (REPO / "dashboard" / "app.py").read_text()
        self.assertNotIn("template_controls_view", src)


if __name__ == "__main__":
    unittest.main()
