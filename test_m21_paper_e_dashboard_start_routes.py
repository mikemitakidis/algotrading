"""test_m21_paper_e_dashboard_start_routes.py: M21 PR-e (H-1 / F-7).

The five dashboard routes that (re)started the bot used to launch main.py as a
dashboard child process, outside systemd and outside the Gate A-0 guard. Now:

  * no route ever launches main.py itself (no Popen of main.py in dashboard/app.py);
  * /api/start, /api/restart, /api/telegram/save, /api/strategy/save and
    /api/strategy/reset refuse to (re)start the bot while
    /var/lib/algo-trader/ALLOW_BOT_START is absent, and then leave any running bot
    untouched (no pkill);
  * with the allow-file present they go through `systemctl start algo-trader.service`;
  * settings are still saved when the restart is refused (the refusal is reported);
  * /api/stop is unchanged; auth and CSRF are unchanged.

subprocess is replaced by a recorder: no process is started, killed or controlled.
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _test_isolation  # noqa: E402
_test_isolation.install()

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import test_m15_3_a_dashboard_auth as A  # noqa: E402

PW = "pw-test-1234"


class _Proc:
    returncode = 0
    stdout = b""
    stderr = b""


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "s.db")
        self.app = A._make_test_app(password=PW, db_path=self.db)
        self.allow = Path(self.tmp.name) / "ALLOW_BOT_START"
        self.calls = []
        self.lock = threading.Lock()

        def run(argv, *a, **k):
            with self.lock:
                self.calls.append(("run", list(argv)))
            return _Proc()

        def popen(argv, *a, **k):
            with self.lock:
                self.calls.append(("popen", list(argv)))
            raise AssertionError("dashboard must not Popen")
        for target, new in ((self.app.subprocess, "run"), (self.app.subprocess, "Popen")):
            p = mock.patch.object(target, new, run if new == "run" else popen)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(self.app, "_BOT_START_ALLOW_FILE", self.allow)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.app.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        self.c = self.app.app.test_client()
        r = self.c.post("/api/login", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.h = {"X-CSRF-Token": r.get_json()["csrf_token"]}

    def settle(self):
        for t in list(threading.enumerate()):
            if t is not threading.current_thread() and t.daemon and t.name != "MainThread":
                t.join(timeout=5)

    def argvs(self):
        self.settle()
        with self.lock:
            return [a for _, a in self.calls]

    def bot_calls(self):
        return [a for a in self.argvs() if a[:1] in (["systemctl"], ["pkill"])
                or any("main.py" in x for x in a)]


class Refused(_Base):
    """No allow-file: nothing is started, stopped or killed."""

    def test_start(self):
        r = self.c.post("/api/start", headers=self.h)
        self.assertEqual(r.status_code, 409)
        self.assertIn("ALLOW_BOT_START", r.get_json()["error"])
        self.assertEqual(self.bot_calls(), [])

    def test_restart(self):
        r = self.c.post("/api/restart", headers=self.h)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.bot_calls(), [])

    def test_strategy_reset_still_resets_but_does_not_touch_the_bot(self):
        with mock.patch("bot.strategy.reset") as reset:
            r = self.c.post("/api/strategy/reset", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(reset.called)
        self.assertIn("ALLOW_BOT_START", r.get_json()["bot_restart"])
        self.assertEqual(self.bot_calls(), [])

    def test_strategy_save_still_saves_but_does_not_touch_the_bot(self):
        with mock.patch("bot.strategy.save", return_value=[]) as save:
            r = self.c.post("/api/strategy/save", headers=self.h, json={"x": 1})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(save.called)
        self.assertIn("ALLOW_BOT_START", r.get_json()["bot_restart"])
        self.assertEqual(self.bot_calls(), [])

    def test_telegram_save_still_saves_but_does_not_touch_the_bot(self):
        with mock.patch.object(self.app, "_write_env") as w, \
                mock.patch.object(self.app, "load_dotenv"):
            r = self.c.post("/api/telegram/save", headers=self.h, json={"enabled": False})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(w.called)
        self.assertIn("ALLOW_BOT_START", r.get_json()["bot_restart"])
        self.assertEqual(self.bot_calls(), [])

    def test_csrf_and_auth_unchanged(self):
        self.assertEqual(self.c.post("/api/start").status_code, 403)
        anon = self.app.app.test_client()
        self.assertIn(anon.post("/api/start", headers=self.h).status_code, (401, 403))
        self.assertEqual(self.bot_calls(), [])


class Allowed(_Base):
    """Allow-file present: through systemd only."""

    def setUp(self):
        super().setUp()
        self.allow.touch()

    def test_start_uses_systemctl(self):
        r = self.c.post("/api/start", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.bot_calls(), [["systemctl", "start", "algo-trader.service"]])

    def test_start_reports_systemctl_failure(self):
        class Bad(_Proc):
            returncode = 1
        with mock.patch.object(self.app.subprocess, "run", lambda *a, **k: Bad()):
            r = self.c.post("/api/start", headers=self.h)
        self.assertEqual(r.status_code, 500)

    def test_restart_routes_stop_then_start_the_unit(self):
        with mock.patch("bot.strategy.reset"), mock.patch("bot.strategy.save", return_value=[]), \
                mock.patch.object(self.app, "_write_env"), mock.patch.object(self.app, "load_dotenv"):
            for path, body in (("/api/restart", None), ("/api/strategy/reset", None),
                               ("/api/strategy/save", {"x": 1}),
                               ("/api/telegram/save", {"enabled": False})):
                with self.lock:
                    self.calls.clear()
                r = self.c.post(path, headers=self.h, json=body)
                self.assertEqual(r.status_code, 200, path)
                self.assertEqual(self.bot_calls(), [["pkill", "-f", "main.py"],
                                                    ["systemctl", "start", "algo-trader.service"]],
                                 path)

    def test_allow_file_removed_before_the_delayed_start(self):
        # restart is requested with the file present, which disappears before
        # the background start: the bot is stopped and NOT restarted (safe side).
        orig = self.app._bot_start_allowed
        seq = iter([True, False])
        with mock.patch.object(self.app, "_bot_start_allowed", lambda: next(seq, False)):
            r = self.c.post("/api/restart", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.bot_calls(), [["pkill", "-f", "main.py"]])
        self.assertTrue(callable(orig))


class StopUnchanged(_Base):
    def test_stop(self):
        r = self.c.post("/api/stop", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.bot_calls(), [["pkill", "-f", "main.py"]])


class Source(unittest.TestCase):
    SRC = (REPO / "dashboard" / "app.py").read_text()

    def test_no_dashboard_child_process_of_main_py(self):
        tree = ast.parse(self.SRC)
        popens = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and getattr(n.func, "attr", "") == "Popen"]
        self.assertEqual(popens, [])
        consts = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                  and isinstance(n.value, str)]
        self.assertNotIn("main.py", [c for c in consts if c.endswith("main.py") and "/" in c])

    def test_allow_file_is_the_gate_a0_file(self):
        from dashboard import app as dashapp
        self.assertEqual(str(dashapp._BOT_START_ALLOW_FILE), "/var/lib/algo-trader/ALLOW_BOT_START")
        self.assertEqual(dashapp._BOT_UNIT, "algo-trader.service")

    def test_every_former_launcher_goes_through_the_gate(self):
        tree = ast.parse(self.SRC)
        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for name in ("restart", "telegram_save", "strategy_save", "strategy_reset"):
            body = ast.dump(funcs[name])
            self.assertIn("_restart_bot_async", body, name)
            self.assertNotIn("'pkill'", body, name)
        self.assertIn("_bot_start_allowed", ast.dump(funcs["start"]))
        self.assertIn("_bot_start_allowed", ast.dump(funcs["_run_bot"]))
        self.assertIn("_bot_start_allowed", ast.dump(funcs["_restart_bot_async"]))


if __name__ == "__main__":
    unittest.main()
