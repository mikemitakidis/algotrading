"""M21 E6 — dashboard template-controls blueprint (isolated; dashboard/app.py is NOT changed).

The blueprint is mounted on a bare Flask app, exactly as the D3 view tests do.
The store is the real WP9 ``bot.templates.controls`` store in a temporary file.
"""
import ast
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from flask import Flask

from bot.templates import controls as TC
from bot.templates.routing import LIVE_TEMPLATES, is_live
from dashboard import template_controls_view as V

REPO = Path(__file__).resolve().parent
CSRF = "e6-test-token"
CFG2 = {"lane": "paper", "selected": ["scanner", "mean_reversion"], "champion": "scanner",
        "params": {"mean_reversion": {"rsi_entry": 25.0}}}
CFG_ALL = {"lane": "paper", "selected": ["scanner", "mean_reversion", "breakout_momentum", "pullback_in_trend"],
           "champion": "mean_reversion", "params": {}}


class Base(unittest.TestCase):
    writes = True
    configured = True

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="e6_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = os.path.join(self.dir, "controls.db")
        app = Flask("e6_test")
        app.secret_key = "e6-test-only"
        if self.configured:
            app.config[V.DB_KEY] = self.db
        if self.writes:
            app.config[V.WRITES_KEY] = True
        app.register_blueprint(V.bp)
        self.app = app
        self.client = app.test_client()

    def login(self, csrf=CSRF):
        with self.client.session_transaction() as s:
            s["authed"] = True
            if csrf:
                s["csrf_token"] = csrf

    def post(self, action, body, *, token=CSRF):
        h = {"X-CSRF-Token": token} if token else {}
        r = self.client.post("/api/template-controls/" + action, json=body, headers=h)
        return r.status_code, r.get_json()

    def active(self):
        return TC.read_active(self.db)


class Access(Base):
    def test_every_route_requires_auth(self):
        for url in ("/api/template-controls", "/template-controls"):
            self.assertEqual(self.client.get(url).status_code, 401, url)
        for a in ("propose", "approve", "activate", "rollback", "withdraw"):
            self.assertEqual(self.client.post("/api/template-controls/" + a, json={}).status_code, 401, a)
        self.assertFalse(os.path.exists(self.db))

    def test_csrf_required_for_writes(self):
        self.login()
        self.assertEqual(self.post("propose", {"config": CFG2, "author": "a", "reason": "r"}, token=None)[0], 403)
        code, body = self.post("propose", {"config": CFG2, "author": "a", "reason": "r"}, token="wrong")
        self.assertEqual((code, body["error"]), (403, "csrf_invalid"))
        self.assertFalse(os.path.exists(self.db))

    def test_get_only_pages(self):
        self.login()
        for m in ("put", "delete", "patch"):
            self.assertEqual(getattr(self.client, m)("/api/template-controls").status_code, 405)

    def test_get_never_creates_the_store(self):
        self.login()
        r = self.client.get("/api/template-controls")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual((d["active"]["source"], d["active"]["version"]), (TC.SOURCE_DEFAULT, None))
        self.assertEqual(d["active"]["config"], TC.default_config())
        self.assertTrue(d["store_problem"].startswith("store_unreadable"))
        self.assertEqual(d["live_templates"], sorted(LIVE_TEMPLATES))
        self.assertIn("rsi_entry", d["templates"]["mean_reversion"]["params"])
        self.assertFalse(os.path.exists(self.db))


class NotConfigured(Base):
    configured = False

    def test_unconfigured_is_503(self):
        self.login()
        self.assertEqual(self.client.get("/api/template-controls").status_code, 503)
        self.assertEqual(self.post("propose", {"config": CFG2, "author": "a", "reason": "r"})[0], 503)


class FromEnvironment(Base):
    configured = False
    writes = False

    def test_environment_configures_path_and_writes(self):
        self.login()
        with mock.patch.dict(os.environ, {V.DB_KEY: self.db}, clear=False):
            os.environ.pop(V.WRITES_KEY, None)
            self.assertEqual(self.client.get("/api/template-controls").status_code, 200)
            self.assertEqual(self.post("propose", {"config": CFG2, "author": "a", "reason": "r"})[0], 403)
            for val in ("true", "yes", "0", ""):
                with mock.patch.dict(os.environ, {V.WRITES_KEY: val}):
                    self.assertEqual(self.post("propose", {"config": CFG2, "author": "a", "reason": "r"})[0], 403)
            with mock.patch.dict(os.environ, {V.WRITES_KEY: "1"}):
                self.assertEqual(self.post("propose", {"config": CFG2, "author": "a", "reason": "r"})[0], 200)
        self.app.config[V.WRITES_KEY] = False                       # app config wins over the environment
        with mock.patch.dict(os.environ, {V.DB_KEY: self.db, V.WRITES_KEY: "1"}):
            self.assertEqual(self.post("propose", {"config": CFG2, "author": "a", "reason": "r"})[0], 403)


class ReadOnly(Base):
    writes = False

    def test_writes_disabled_by_default(self):
        self.login()
        code, body = self.post("propose", {"config": CFG2, "author": "a", "reason": "r"})
        self.assertEqual((code, body["error"]), (403, "read_only"))
        self.assertFalse(os.path.exists(self.db))
        html = self.client.get("/template-controls").data.decode()
        self.assertIn("Read-only", html)
        self.assertNotIn("<form", html)


class Lifecycle(Base):
    def test_propose_approve_activate_rollback(self):
        self.login()
        self.assertEqual(self.post("propose", {"config": CFG2, "author": "mike", "reason": "try MR"}),
                         (200, {"ok": True, "result": "proposed version 1"}))
        self.assertEqual(self.post("activate", {"version": 1, "actor": "mike", "expected_current": None})[0], 409)
        self.assertEqual(self.post("approve", {"version": 1, "approver": "Mike M", "reason": "ok"})[0], 200)
        self.assertEqual(self.post("activate", {"version": 1, "actor": "mike", "expected_current": None})[0], 200)
        a = self.active()
        self.assertEqual((a.version, a.source, a.config["selected"]), (1, TC.SOURCE_STORE, CFG2["selected"]))
        self.assertEqual(a.config["params"]["mean_reversion"]["rsi_entry"], 25.0)
        self.post("propose", {"config": CFG_ALL, "author": "mike", "reason": "all four in shadow"})
        self.post("approve", {"version": 2, "approver": "Mike M", "reason": "ok"})
        self.assertEqual(self.post("activate", {"version": 2, "actor": "mike", "expected_current": 1})[0], 200)
        self.assertEqual(self.active().version, 2)
        # selection never makes a template tradable
        self.assertEqual(LIVE_TEMPLATES, frozenset({"scanner"}))
        self.assertFalse(any(is_live(t) for t in CFG_ALL["selected"] if t != "scanner"))
        self.assertEqual(self.post("rollback", {"actor": "mike", "reason": "undo", "expected_current": 2}),
                         (200, {"ok": True, "result": "rolled back to version 1"}))
        self.assertEqual(self.active().version, 1)
        actions = [r["action"] for r in TC.TemplateControls(self.db, clock=V._utc_now).audit()]
        self.assertIn("ACTIVATE_REFUSED", actions)
        self.assertEqual(actions[-1], "ROLLED_BACK")
        self.assertTrue(all(r["reason"].startswith("[dashboard] ")
                            for r in TC.TemplateControls(self.db, clock=V._utc_now).versions()))

    def test_compare_and_swap_is_mandatory(self):
        self.login()
        self.post("propose", {"config": CFG2, "author": "mike", "reason": "x"})
        self.post("approve", {"version": 1, "approver": "Mike M", "reason": "ok"})
        code, body = self.post("activate", {"version": 1, "actor": "mike"})
        self.assertEqual((code, body["detail"]), (400, "expected_current is required"))
        self.post("activate", {"version": 1, "actor": "mike", "expected_current": None})
        self.post("propose", {"config": CFG_ALL, "author": "mike", "reason": "y"})
        self.post("approve", {"version": 2, "approver": "Mike M", "reason": "ok"})
        code, body = self.post("activate", {"version": 2, "actor": "mike", "expected_current": None})
        self.assertEqual(code, 409)
        self.assertIn("reload", body["detail"])
        code, body = self.post("activate", {"version": 2, "actor": "mike", "expected_current": 7})
        self.assertEqual(code, 409)
        self.assertIn("concurrent change", body["detail"])
        self.assertEqual(self.post("rollback", {"actor": "mike", "reason": "r"})[0], 400)
        self.assertEqual(self.active().version, 1)

    def test_approval_needs_a_named_human(self):
        self.login()
        self.post("propose", {"config": CFG2, "author": "mike", "reason": "x"})
        for who in ("claude", "System", "bot"):
            code, body = self.post("approve", {"version": 1, "approver": who, "reason": "ok"})
            self.assertEqual(code, 409, who)
            self.assertIn("named human", body["detail"])
        self.assertEqual(TC.TemplateControls(self.db, clock=V._utc_now).versions()[0]["status"], TC.PROPOSED)

    def test_invalid_requests_refused(self):
        self.login()
        for cfg, why in (({"lane": "live", "selected": ["scanner"], "champion": "scanner"}, "only the paper lane"),
                         ({"selected": ["nope"], "champion": "nope"}, "not an Appendix A template"),
                         ({"selected": ["scanner", "mean_reversion"], "champion": "scanner",
                           "params": {"mean_reversion": {"rsi_entry": 99.0}}}, "outside")):
            code, body = self.post("propose", {"config": cfg, "author": "mike", "reason": "x"})
            self.assertEqual(code, 409)
            self.assertIn(why, body["detail"])
        for body, why in (({"config": "[1]", "author": "m", "reason": "x"}, "config must be a JSON object"),
                          ({"config": CFG2, "reason": "x"}, "author is required"),
                          ({"config": CFG2, "author": " ", "reason": "x"}, "author is required")):
            code, out = self.post("propose", body)
            self.assertEqual((code, out["detail"]), (400, why))
        for v in ("1.5", "0", "-1", True, "x", "01"):
            self.assertEqual(self.post("approve", {"version": v, "approver": "Mike M", "reason": "ok"})[0], 400, v)
        self.assertEqual(TC.TemplateControls(self.db, clock=V._utc_now).versions(), [])

    def test_withdraw(self):
        self.login()
        self.post("propose", {"config": CFG2, "author": "mike", "reason": "x"})
        self.assertEqual(self.post("withdraw", {"version": 1, "actor": "mike", "reason": "no"})[0], 200)
        self.assertEqual(self.post("approve", {"version": 1, "approver": "Mike M", "reason": "ok"})[0], 409)


class Page(Base):
    def test_page_escapes_and_forms_post_back_with_csrf(self):
        self.login()
        self.post("propose", {"config": CFG2, "author": "<script>alert(1)</script>", "reason": "<b>r</b>"})
        html = self.client.get("/template-controls").data.decode()
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<b>r</b>", html)
        self.assertIn('name="csrf_token" value="%s"' % CSRF, html)
        self.assertIn("Paper lane only", html)
        r = self.client.post("/api/template-controls/approve",
                             data={"csrf_token": CSRF, "version": "1", "approver": "Mike M", "reason": "ok"})
        self.assertEqual(r.status_code, 303)
        q = parse_qs(urlparse(r.headers["Location"]).query)
        self.assertEqual((q["status"], q["result"]), (["200"], ["approved version 1"]))
        r = self.client.post("/api/template-controls/approve",
                             data={"csrf_token": CSRF, "version": "1", "approver": "Mike M", "reason": "ok"})
        q = parse_qs(urlparse(r.headers["Location"]).query)
        self.assertEqual(q["status"], ["409"])
        page = self.client.get(r.headers["Location"]).data.decode()
        self.assertIn("not PROPOSED", page)
        self.assertEqual(self.client.post("/api/template-controls/approve",
                                          data={"version": "1", "approver": "M", "reason": "ok"}).status_code, 403)


class Isolation(unittest.TestCase):
    def test_no_broker_order_or_network_imports(self):
        tree = ast.parse((REPO / "dashboard" / "template_controls_view.py").read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add(n.module or "")
        self.assertEqual(sorted(m for m in mods if m.startswith(("bot", "dashboard"))),
                         ["bot.templates", "bot.templates.core", "bot.templates.routing", "dashboard.auth.csrf"])
        for bad in ("requests", "urllib.request", "socket", "ib_insync", "subprocess"):
            self.assertNotIn(bad, mods)

    def test_dashboard_app_does_not_register_it(self):
        src = (REPO / "dashboard" / "app.py").read_text()
        self.assertNotIn("template_controls_view", src)


if __name__ == "__main__":
    unittest.main()
