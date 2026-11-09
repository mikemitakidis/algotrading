"""test_m21_paper_e_dashboard_start_routes.py: M21 PR-e (H-1 / F-7), dashboard bot control.

Review finding 4 (Issue #3 comment 6733991637): the dashboard must control the bot only
through systemd, never kill unrelated processes, and never report success before
systemd shows it. These tests replace the `subprocess` module the dashboard uses with a
stateful stand-in for systemd, and the /proc scan with a fake /proc tree. Nothing is
started, stopped, signalled or killed.

Covered: the five former launchers plus /api/stop; refusal without ALLOW_BOT_START; the
Gate A-0 guard evaluated by systemd at start time (allow-file removed after the
dashboard's own check); start/restart/stop failures, timeouts and crashes after start;
unit not loaded / masked / disabled; a main.py outside the unit (reported, never
killed); concurrent operations; settings saved but a stopped bot never started; state
unreadable; auth and CSRF unchanged; source checks (no Popen, no pkill, no kill).
"""
from __future__ import annotations

import ast
import os
import subprocess as real_subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _test_isolation  # noqa: E402
_test_isolation.install()

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import test_m15_3_a_dashboard_auth as A  # noqa: E402

PW = "pw-test-1234"
UNIT = "algo-trader.service"


class FakeSystemd:
    """Answers `systemctl show|start|restart|try-restart|stop algo-trader.service` like
    systemd: the Gate A-0 guard (allow-file) is evaluated when the unit is started."""

    def __init__(self, allow_path: Path):
        self.allow = allow_path
        self.state = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                      "MainPID": "0", "ConditionResult": "yes", "UnitFileState": "disabled"}
        self.calls = []
        self.rc = {}                 # verb -> forced exit code
        self.timeout = set()         # verbs that time out
        self.show_fails = False
        self.crash_after_start = False
        self.no_main_pid = False
        self.stop_noop = False
        self.before_start = None     # hook run just before the guard is evaluated
        self.gate = None             # threading.Event that blocks a verb until set
        self.entered = threading.Event()
        self.lock = threading.Lock()

    def run(self, argv, *a, timeout=None, **k):
        with self.lock:
            self.calls.append(list(argv))
        if argv[0] != "systemctl":
            raise AssertionError("dashboard ran a non-systemctl command: %r" % (argv,))
        verb, unit = argv[1], argv[2]
        if unit != UNIT:
            raise AssertionError("dashboard touched another unit: %r" % (argv,))
        if verb == "show":
            if self.show_fails:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            props = [x.split("=", 1)[1] for x in argv[3:]]
            return SimpleNamespace(returncode=0, stdout="".join(
                "%s=%s\n" % (p, self.state[p]) for p in props), stderr="")
        if self.gate is not None:
            self.entered.set()
            self.gate.wait(10)
        if verb in self.timeout:
            raise real_subprocess.TimeoutExpired(argv, timeout)
        if self.rc.get(verb, 0) != 0:
            return SimpleNamespace(returncode=self.rc[verb], stdout="", stderr="")
        if verb in ("start", "restart") or (verb == "try-restart" and self.state["ActiveState"] == "active"):
            if self.before_start:
                self.before_start()
            if not self.allow.exists():
                self.state.update(ActiveState="inactive", SubState="dead", MainPID="0",
                                  ConditionResult="no")
            elif self.crash_after_start:
                self.state.update(ActiveState="failed", SubState="failed", MainPID="0",
                                  ConditionResult="yes")
            else:
                self.state.update(ActiveState="active", SubState="running",
                                  MainPID="0" if self.no_main_pid else "4242", ConditionResult="yes")
        elif verb == "stop" and not self.stop_noop:
            self.state.update(ActiveState="inactive", SubState="dead", MainPID="0")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def verbs(self):
        with self.lock:
            return [c[1] for c in self.calls if c[1] != "show"]


def fake_subprocess(sd):
    def popen(*a, **k):
        raise AssertionError("dashboard must not Popen")
    return SimpleNamespace(run=sd.run, Popen=popen, TimeoutExpired=real_subprocess.TimeoutExpired,
                           SubprocessError=real_subprocess.SubprocessError,
                           CalledProcessError=real_subprocess.CalledProcessError,
                           DEVNULL=real_subprocess.DEVNULL, PIPE=real_subprocess.PIPE)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.app = A._make_test_app(password=PW, db_path=str(t / "s.db"))
        self.allow = t / "ALLOW_BOT_START"
        self.sd = FakeSystemd(self.allow)
        self.proc = t / "proc"
        self.proc.mkdir()
        self.kills = []
        for target, attr, new in (
                (self.app, "subprocess", fake_subprocess(self.sd)),
                (self.app, "_BOT_START_ALLOW_FILE", self.allow),
                (self.app, "_PROC_ROOT", str(self.proc)),
                (self.app, "_BOT_SETTLE_SEC", 0),
                (self.app.os, "kill", lambda *a: self.kills.append(a)),
                (self.app.os, "killpg", lambda *a: self.kills.append(a))):
            p = mock.patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)
        self.c = self.app.app.test_client()
        r = self.c.post("/api/login", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.h = {"X-CSRF-Token": r.get_json()["csrf_token"]}

    # a fake /proc entry
    def process(self, pid, argv, cgroup, cwd=None):
        d = self.proc / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "cgroup").write_text(cgroup)
        if cwd:
            os.symlink(cwd, d / "cwd")

    def running(self):
        self.allow.touch()
        self.sd.state.update(ActiveState="active", SubState="running", MainPID="4242")

    def post(self, path, body=None):
        r = self.c.post(path, headers=self.h, json=body)
        return r.status_code, r.get_json()

    def tearDown(self):
        self.assertEqual(self.kills, [], "the dashboard signalled a process")


# ─── refused without the allow-file ─────────────────────────────────────────
class Refused(_Base):
    def test_start_and_restart(self):
        for path in ("/api/start", "/api/restart"):
            code, res = self.post(path)
            self.assertEqual(code, 409, path)
            self.assertFalse(res["ok"])
            self.assertIn("ALLOW_BOT_START", res["error"])
        self.assertEqual(self.sd.verbs(), [])

    def test_settings_saved_and_a_stopped_bot_is_never_started(self):
        self.allow.touch()                                    # even with the allow-file
        with mock.patch("bot.strategy.reset") as reset, \
                mock.patch("bot.strategy.save", return_value=[]) as save, \
                mock.patch.object(self.app, "_write_env") as w, \
                mock.patch.object(self.app, "load_dotenv"):
            for path, body in (("/api/strategy/reset", None), ("/api/strategy/save", {"x": 1}),
                               ("/api/telegram/save", {"enabled": False})):
                code, res = self.post(path, body)
                self.assertEqual(code, 200, path)
                self.assertEqual((res["ok"], res["saved"], res["bot_restart"]),
                                 (True, True, "not_running"), path)
        self.assertTrue(reset.called and save.called and w.called)
        self.assertEqual(self.sd.verbs(), [])

    def test_running_bot_without_allow_file_is_left_alone_and_reported(self):
        self.running()
        self.allow.unlink()
        with mock.patch("bot.strategy.save", return_value=[]):
            code, res = self.post("/api/strategy/save", {"x": 1})
        self.assertEqual(code, 409)
        self.assertEqual((res["ok"], res["saved"], res["bot_restart"]), (False, True, "refused"))
        self.assertEqual(self.sd.verbs(), [])
        self.assertEqual(self.sd.state["ActiveState"], "active")

    def test_csrf_and_auth_unchanged(self):
        self.assertEqual(self.c.post("/api/start").status_code, 403)
        self.assertEqual(self.c.post("/api/stop").status_code, 403)
        anon = self.app.app.test_client()
        self.assertIn(anon.post("/api/start", headers=self.h).status_code, (401, 403))
        self.assertEqual(self.sd.verbs(), [])


# ─── systemd-only operations with accurate results ─────────────────────────
class Operations(_Base):
    def setUp(self):
        super().setUp()
        self.allow.touch()

    def test_start_reports_success_only_when_systemd_shows_it_running(self):
        code, res = self.post("/api/start")
        self.assertEqual(code, 200, res)
        self.assertTrue(res["ok"])
        self.assertEqual(res["state"]["MainPID"], "4242")
        self.assertEqual(self.sd.verbs(), ["start"])

    def test_restart_is_a_systemd_restart(self):
        self.running()
        code, res = self.post("/api/restart")
        self.assertEqual((code, res["ok"]), (200, True))
        self.assertEqual(self.sd.verbs(), ["restart"])

    def test_disabled_unit_can_still_be_started_by_systemd(self):
        self.sd.state["UnitFileState"] = "disabled"
        self.assertEqual(self.post("/api/start")[0], 200)

    def test_unit_not_loaded_or_masked(self):
        for load in ("not-found", "masked"):
            self.sd.state["LoadState"] = load
            code, res = self.post("/api/start")
            self.assertEqual(code, 409, load)
            self.assertIn("not loaded", res["error"])
        self.assertEqual(self.sd.verbs(), [])

    def test_systemctl_failure(self):
        self.sd.rc["start"] = 1
        code, res = self.post("/api/start")
        self.assertEqual(code, 500)
        self.assertIn("failed (exit 1)", res["error"])

    def test_systemctl_timeout_is_outcome_unknown(self):
        self.sd.timeout.add("restart")
        code, res = self.post("/api/restart")
        self.assertEqual(code, 504)
        self.assertIn("outcome unknown", res["error"])

    def test_crash_right_after_start(self):
        self.sd.crash_after_start = True
        code, res = self.post("/api/start")
        self.assertEqual(code, 500)
        self.assertIn("not running afterwards", res["error"])

    def test_active_without_a_main_process_is_not_success(self):
        self.sd.no_main_pid = True
        code, res = self.post("/api/start")
        self.assertEqual(code, 500)

    def test_stop_that_leaves_the_bot_running_is_not_success(self):
        self.running()
        self.sd.stop_noop = True
        code, res = self.post("/api/stop")
        self.assertEqual(code, 500)
        self.assertIn("still active", res["error"])

    def test_settings_with_state_unreadable(self):
        self.sd.show_fails = True
        with mock.patch("bot.strategy.reset"):
            code, res = self.post("/api/strategy/reset")
        self.assertEqual(code, 500)
        self.assertEqual((res["ok"], res["saved"], res["bot_restart"]), (False, True, "unknown"))
        self.assertEqual(self.sd.verbs(), [])

    def test_state_unreadable(self):
        self.sd.show_fails = True
        code, res = self.post("/api/start")
        self.assertEqual(code, 500)
        self.assertIn("unreadable", res["error"])
        self.assertEqual(self.sd.verbs(), [])

    def test_settings_restart_a_running_bot_through_systemd(self):
        self.running()
        with mock.patch("bot.strategy.save", return_value=[]):
            code, res = self.post("/api/strategy/save", {"x": 1})
        self.assertEqual(code, 200)
        self.assertEqual((res["ok"], res["saved"], res["bot_restart"]), (True, True, "restarted"))
        self.assertEqual(self.sd.verbs(), ["try-restart"])

    def test_settings_restart_failure_is_reported(self):
        self.running()
        self.sd.rc["try-restart"] = 1
        with mock.patch("bot.strategy.reset"):
            code, res = self.post("/api/strategy/reset")
        self.assertEqual(code, 500)
        self.assertEqual((res["ok"], res["saved"], res["bot_restart"]), (False, True, "failed"))

    def test_stop(self):
        self.running()
        code, res = self.post("/api/stop")
        self.assertEqual((code, res["ok"]), (200, True))
        self.assertEqual(self.sd.verbs(), ["stop"])

    def test_stop_is_not_gated_by_the_allow_file(self):
        self.running()
        self.allow.unlink()
        self.assertEqual(self.post("/api/stop")[0], 200)

    def test_stop_failure_and_timeout(self):
        self.running()
        self.sd.rc["stop"] = 1
        self.assertEqual(self.post("/api/stop")[0], 500)
        del self.sd.rc["stop"]
        self.sd.timeout.add("stop")
        code, res = self.post("/api/stop")
        self.assertEqual(code, 504)
        self.assertIn("outcome unknown", res["error"])


# ─── races ──────────────────────────────────────────────────────────────────
class Races(_Base):
    def setUp(self):
        super().setUp()
        self.allow.touch()

    def test_allow_file_removed_after_the_dashboard_check(self):
        # The dashboard saw the file; it is removed before systemd evaluates the
        # Gate A-0 guard. systemd refuses; the dashboard reports the refusal.
        self.sd.before_start = lambda: self.allow.unlink()
        code, res = self.post("/api/start")
        self.assertEqual(code, 409)
        self.assertIn("Gate A-0 guard", res["error"])
        self.assertEqual(self.sd.state["ActiveState"], "inactive")

    def test_one_operation_at_a_time(self):
        self.sd.gate = threading.Event()
        out = {}

        def first():
            out["first"] = self.c.post("/api/restart", headers=self.h).status_code
        t = threading.Thread(target=first)
        t.start()
        self.assertTrue(self.sd.entered.wait(5))
        code, res = self.post("/api/start")                     # while restart is in progress
        self.assertEqual(code, 409)
        self.assertIn("in progress", res["error"])
        with mock.patch("bot.strategy.save", return_value=[]):
            code, res = self.post("/api/strategy/save", {"x": 1})
        self.assertEqual((code, res["saved"], res["bot_restart"]), (409, True, "busy"))
        self.sd.gate.set()
        t.join(10)
        self.assertEqual(out["first"], 200)
        self.assertEqual(self.sd.verbs(), ["restart"])

    def test_lock_released_after_a_failure(self):
        self.sd.rc["start"] = 1
        self.assertEqual(self.post("/api/start")[0], 500)
        del self.sd.rc["start"]
        self.assertEqual(self.post("/api/start")[0], 200)


# ─── processes outside systemd: reported, never killed ─────────────────────
class OutsideSystemd(_Base):
    def setUp(self):
        super().setUp()
        self.allow.touch()
        main = str(self.app.BASE_DIR / "main.py")
        self.process(101, ["/opt/venv/bin/python3", main], "0::/system.slice/algo-trader.service\n")
        self.process(202, ["/usr/bin/python3", "other/main.py"], "0::/user.slice\n")   # unrelated
        self.process(303, ["python3", "main.py"], "0::/user.slice\n", cwd="/")         # elsewhere

    def test_scan_finds_only_this_checkouts_main_outside_the_unit(self):
        self.assertEqual(self.app._bot_processes_outside_unit(), [])
        self.process(404, ["/usr/bin/python3", str(self.app.BASE_DIR / "main.py")], "0::/user.slice\n")
        self.process(505, ["python3", "main.py"], "0::/user.slice\n", cwd=str(self.app.BASE_DIR))
        self.assertEqual(self.app._bot_processes_outside_unit(), [404, 505])

    def test_start_refused_while_one_runs(self):
        self.process(404, ["python3", str(self.app.BASE_DIR / "main.py")], "0::/user.slice\n")
        code, res = self.post("/api/start")
        self.assertEqual(code, 409)
        self.assertEqual(res["stray_pids"], [404])
        self.assertEqual(self.sd.verbs(), [])

    def test_stop_reports_it_and_does_not_kill(self):
        self.running()
        self.process(404, ["python3", str(self.app.BASE_DIR / "main.py")], "0::/user.slice\n")
        code, res = self.post("/api/stop")
        self.assertEqual(code, 409)
        self.assertEqual((res["unit_stopped"], res["stray_pids"]), (True, [404]))
        self.assertIn("does not kill", res["error"])
        self.assertEqual(self.sd.verbs(), ["stop"])


# ─── source ─────────────────────────────────────────────────────────────────
class Source(unittest.TestCase):
    SRC = (REPO / "dashboard" / "app.py").read_text()

    def test_no_launch_kill_or_signal(self):
        tree = ast.parse(self.SRC)
        attrs = [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
        for bad in ("Popen", "kill", "killpg", "send_signal", "terminate", "system", "spawnv"):
            self.assertNotIn(bad, attrs, bad)
        # every external command the dashboard runs, with the function it runs in
        cmds = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for n in ast.walk(fn):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in ("run", "call", "check_call", "check_output")
                        and getattr(n.func.value, "id", "") == "subprocess"):
                    elts = n.args[0].elts if n.args and isinstance(n.args[0], ast.List) else []
                    words = tuple(e.value if isinstance(e, ast.Constant) else "*" for e in elts[:2])
                    cmds.append((fn.name,) + words)
        # Control: systemctl only through _systemctl. Read-only probes that predate
        # PR-e: the M15.2 health view's `systemctl is-active/is-enabled` and the
        # /api/status pgrep "running" flag. Nothing else runs a command.
        self.assertEqual(sorted(cmds), [("_systemctl", "systemctl", "*"),
                                        ("_systemctl_state", "systemctl", "is-active"),
                                        ("_systemctl_state", "systemctl", "is-enabled"),
                                        ("status", "pgrep", "-f")])

    def test_systemctl_verbs_are_limited(self):
        from dashboard import app as dashapp
        tree = ast.parse(self.SRC)
        verbs = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_systemctl" and n.args:
                if isinstance(n.args[0], ast.Constant):
                    verbs.add(n.args[0].value)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_bot_start_or_restart":
                pass
        self.assertEqual(verbs, {"show", "stop"})               # plus the verb parameter below
        fn = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "_bot_start_or_restart"][0]
        self.assertIn("'start', 'restart' or 'try-restart'", ast.get_docstring(fn))
        callers = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and getattr(n.func, "id", "") == "_bot_start_or_restart"]
        self.assertEqual(sorted(c.args[0].value for c in callers), ["restart", "start", "try-restart"])
        self.assertEqual(dashapp._BOT_UNIT, "algo-trader.service")
        self.assertEqual(str(dashapp._BOT_START_ALLOW_FILE), "/var/lib/algo-trader/ALLOW_BOT_START")

    def test_every_bot_route_goes_through_one_lock(self):
        tree = ast.parse(self.SRC)
        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for name in ("start", "stop", "restart"):
            self.assertIn("_bot_operation", ast.dump(funcs[name]), name)
        for name in ("telegram_save", "strategy_save", "strategy_reset"):
            self.assertIn("_bot_after_settings_change", ast.dump(funcs[name]), name)
        self.assertIn("_bot_operation", ast.dump(funcs["_bot_after_settings_change"]))


if __name__ == "__main__":
    unittest.main()
