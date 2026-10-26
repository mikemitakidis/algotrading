#!/usr/bin/env python3
"""C5a (M21 safety-test repair) — tests stay off production state, secrets, brokers.

Closes finding N-1 (tests mutating production files / reading secrets /
reaching brokers) and R2-8 (dashboard auth/2FA tests silently skipping when
deps are missing).

What this module proves:
  1. _test_isolation.install() redirects sqlite opens of <repo>/data/* (path,
     relative path, Path and file: URI), the kill-switch file, the eToro paper
     audit JSONL and the risk-ingest audit logs into a private temp dir, and
     propagates the same isolation to Python child processes. The kill-switch
     temp file starts in the production default state (inactive), so no test
     depends on the real file; the bot.* patches are applied lazily, so
     installing isolation imports no project module. python-dotenv is
     redirected so the real .env is never loaded, and a fail-closed audit-hook
     guard blocks any .env access, any write under data/, any connection to a
     broker port or non-loopback host — in the test process and its children.
     The runner (python _test_isolation.py ...) isolates every module and
     refuses the whole run (no test executed) unless a verification child
     proves isolation is effective — e.g. not shadowed by another
     sitecustomize.
  2. Static guards (AST/text only — nothing is imported or executed):
       * every test module that imports dashboard.app, bot.config,
         bot.etoro.paper_broker or a .env-loading tools CLI, spawns a tools/
         CLI, or activates/deactivates the kill switch, calls
         _test_isolation.install() at module top level, before any project
         import;
       * no test passes the real-order confirmation flag to a CLI;
       * the quarantined operator scripts never load .env on import and refuse
         to run without ALGO_OPERATOR_SCRIPT_CONFIRM=<script name>.
  3. The operator scripts really refuse (subprocess, exit 2) before touching a
     DB or broker.
  4. R2-8: pyotp, bcrypt and qrcode must be installed; a missing dependency is
     a FAILURE here, not a silent skip, unless ALGO_ALLOW_MISSING_AUTH_DEPS=1.

No broker connection, no network, no real DB/kill-switch write.
"""
from __future__ import annotations

import ast
import contextlib
import glob
import hashlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from importlib.util import find_spec
from pathlib import Path

import _test_isolation  # noqa: E402
_test_isolation.install()

REPO = Path(__file__).resolve().parent
DATA = REPO / "data"
THIS = Path(__file__).name
REAL_ORDER_FLAG = "--i-understand-this-places-a-real-paper-order"


def _data_snapshot():
    snap = {}
    if DATA.is_dir():
        for p in sorted(DATA.rglob("*")):
            if p.is_file():
                snap[str(p.relative_to(DATA))] = hashlib.sha256(p.read_bytes()).hexdigest()
            elif p.is_dir():
                snap[str(p.relative_to(DATA)) + "/"] = "dir"
    return snap


_DATA_AT_IMPORT = _data_snapshot()


def _test_files():
    return sorted(Path(p).name for p in glob.glob(str(REPO / "test_*.py")))


def _tree(name):
    return ast.parse((REPO / name).read_text(encoding="utf-8"), filename=name)


def _imports_module(tree, module, attr=None):
    for n in ast.walk(tree):
        if isinstance(n, ast.Import) and any(a.name == module or a.name.startswith(module + ".")
                                             for a in n.names):
            return True
        if isinstance(n, ast.ImportFrom) and n.module:
            if n.module == module or n.module.startswith(module + "."):
                return True
            if attr and n.module == module.rsplit(".", 1)[0] and any(a.name == attr for a in n.names):
                return True
        if (isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "import_module"
                and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == module):
            return True
    return False


def _calls_kill_switch_writer(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if name in ("activate_kill_switch", "deactivate_kill_switch", "_write_state"):
                return True
    return False


def _patches_kill_switch_path(src):
    return "KILL_SWITCH_PATH =" in src


def _is_install_call(n):
    return (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and n.value.func.attr == "install"
            and getattr(n.value.func.value, "id", None) == "_test_isolation")


def _top_level_install_line(tree):
    """Line of a module-level install(); an install() guarded only by
    `if __name__ != '__main__':` (operator scripts, when imported) or
    `if not _OPERATOR_LIVE:` (test_m12.py) also counts."""
    for n in tree.body:
        if _is_install_call(n):
            return n.lineno
        if isinstance(n, ast.If) and any(_is_install_call(b) for b in n.body):
            test = ast.unparse(n.test)
            if test in ("__name__ != '__main__'", "not _OPERATOR_LIVE"):
                return n.lineno
    return None


# Production modules that load <repo>/.env when imported, or CLIs that do.
_ENV_LOADING_MODULES = ("bot.config", "dashboard.app", "tools.etoro_live_write",
                        "tools.ingest_risk_state", "tools.ingest_exposure_state")


def _spawns_project_cli(tree):
    """True if the module contains a tools/ CLI path or `-m tools.x` target
    and calls subprocess."""
    uses_subprocess = any(isinstance(n, ast.Attribute) and n.attr in ("run", "Popen", "check_output", "call")
                          and getattr(n.value, "id", None) == "subprocess" for n in ast.walk(tree))
    if not uses_subprocess:
        return False
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            v = n.value
            if (v.startswith("tools/") and v.endswith(".py")) or \
                    (v.startswith("tools.") and v.count(".") >= 1 and " " not in v):
                return True
    return False


def _first_project_import_line(tree):
    for n in tree.body:
        mods = []
        if isinstance(n, ast.Import):
            mods = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods = [n.module]
        if any(m.split(".")[0] in ("bot", "dashboard", "tools") for m in mods):
            return n.lineno
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. The helper itself
# ─────────────────────────────────────────────────────────────────────────────

class TestIsolationHelper(unittest.TestCase):
    def test_install_is_idempotent_and_outside_data(self):
        d1 = _test_isolation.install()
        d2 = _test_isolation.install()
        self.assertEqual(d1, d2)
        self.assertTrue(_test_isolation.is_installed())
        self.assertNotIn(DATA.resolve(), [d1.resolve(), *d1.resolve().parents])

    def test_sqlite_plain_relative_path_and_uri_are_redirected(self):
        tmp = _test_isolation.temp_dir()
        for target in (str(DATA / "c5a_probe_a.db"), "data/c5a_probe_b.db", DATA / "c5a_probe_c.db"):
            con = sqlite3.connect(target)
            con.execute("CREATE TABLE IF NOT EXISTS t (x)")
            con.close()
        con = sqlite3.connect("file:" + str(DATA / "c5a_probe_d.db") + "?mode=rwc", uri=True)
        con.execute("CREATE TABLE IF NOT EXISTS t (x)")
        con.close()
        for name in ("c5a_probe_a.db", "c5a_probe_b.db", "c5a_probe_c.db", "c5a_probe_d.db"):
            self.assertTrue((tmp / name).exists(), name)
            self.assertFalse((DATA / name).exists(), name)

    def test_non_production_paths_untouched(self):
        self.assertIsNone(_test_isolation.redirect_path(":memory:"))
        self.assertIsNone(_test_isolation.redirect_path(str(REPO / "signals.db")))
        self.assertIsNone(_test_isolation.redirect_path(str(DATA / "sub" / "x.db")))
        self.assertIsNone(_test_isolation.redirect_path("/tmp/elsewhere/signals.db"))
        con = sqlite3.connect(":memory:")
        con.execute("SELECT 1")
        con.close()

    def test_kill_switch_writes_go_to_temp(self):
        import bot.kill_switch as ks
        real = DATA / "kill_switch.json"
        before = real.read_bytes() if real.exists() else None
        self.assertEqual(Path(ks.KILL_SWITCH_PATH).parent, _test_isolation.temp_dir())
        self.assertTrue(ks.activate_kill_switch("c5a test")["active"])
        self.assertTrue(ks.is_kill_switch_active())
        self.assertFalse(ks.deactivate_kill_switch("c5a test")["active"])
        after = real.read_bytes() if real.exists() else None
        self.assertEqual(before, after)

    def test_etoro_paper_default_audit_redirected(self):
        import bot.etoro.paper_broker as pb
        self.assertEqual(Path(pb._DEFAULT_AUDIT_FILE).parent, _test_isolation.temp_dir())

    def test_ingest_audit_logger_paths_redirected(self):
        import bot.risk_authority.ingest_audit as ia
        tmp = _test_isolation.temp_dir()
        for arg in (None, DATA / "risk_exposure.log"):
            logger = ia.get_ingest_audit_logger(arg) if arg else ia.get_ingest_audit_logger()
            path = next(Path(v) for v in vars(logger).values()
                        if isinstance(v, (str, Path)) and str(v).endswith(".log"))
            self.assertEqual(path.parent, tmp)

    def test_python_children_inherit_isolation(self):
        code = ("import sqlite3, bot.kill_switch as k; "
                "c = sqlite3.connect('data/c5a_child_probe.db'); c.close(); "
                "print(k.KILL_SWITCH_PATH)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()).parent, _test_isolation.temp_dir())
        self.assertTrue((_test_isolation.temp_dir() / "c5a_child_probe.db").exists())
        self.assertFalse((DATA / "c5a_child_probe.db").exists())

    def test_children_with_hand_built_env_are_isolated(self):
        # Several suites start children with env={..., "PYTHONPATH": repo},
        # which drops the inherited isolation; the Popen wrapper re-injects it.
        minimal = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO)}
        copied = dict(os.environ, PYTHONPATH=str(REPO))   # keeps the marker, drops the shim
        for i, env in enumerate((minimal, copied)):
            name = "c5a_handenv_probe_%d.db" % i
            code = "import sqlite3; c = sqlite3.connect('data/%s'); c.close()" % name
            r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((_test_isolation.temp_dir() / name).exists(), name)
            self.assertFalse((DATA / name).exists(), name)

    def test_install_imports_no_project_modules(self):
        # Several suites assert which project modules a child process imports;
        # the isolation itself must not import any (bot.* patches are lazy).
        code = ("import sys, _test_isolation as t; t.install(); "
                "print(t.is_installed(), sorted(m for m in sys.modules "
                "if m.split('.')[0] in ('bot', 'dashboard', 'tools')))")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True []")

    def test_temp_kill_switch_starts_in_production_default_state(self):
        # Fresh, un-inherited isolation (-S: no sitecustomize; new temp dir):
        # the temp kill-switch file is created inactive, exactly as main.py's
        # ensure_default_state() does, and the real file is not consulted.
        code = ("import _test_isolation as t; d = t.install(); "
                "import bot.kill_switch as k; "
                "print(k.KILL_SWITCH_PATH.parent == d, k.KILL_SWITCH_PATH.exists(), "
                "k.is_kill_switch_active())")
        env = {k: v for k, v in os.environ.items()
               if k not in ("ALGO_TEST_ISOLATION_DIR", "PYTHONPATH")}
        r = subprocess.run([sys.executable, "-S", "-c", code], cwd=str(REPO), env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True True False")

    def test_patches_survive_module_reload(self):
        code = ("import importlib, _test_isolation as t, bot.kill_switch as k, "
                "bot.etoro.paper_broker as pb; importlib.reload(k); importlib.reload(pb); "
                "print(k.KILL_SWITCH_PATH.parent == t.temp_dir(), "
                "pb._DEFAULT_AUDIT_FILE.parent == t.temp_dir())")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True True")

    def test_real_env_file_is_never_read(self):
        import dotenv
        env_file = REPO / ".env"
        with self.assertRaises(_test_isolation.ProductionAccessBlocked):
            open(env_file)
        with self.assertRaises(_test_isolation.ProductionAccessBlocked):
            open(REPO / ".env.bak.c5a")
        # dotenv loads are redirected to the (absent) isolated stand-in
        self.assertTrue(getattr(dotenv.load_dotenv, "_algo_isolated", False))
        self.assertFalse(dotenv.load_dotenv(env_file))
        self.assertEqual(dotenv.dotenv_values(env_file), {})
        self.assertEqual(dotenv.dotenv_values(), {})
        # ...and they really read the stand-in, never <repo>/.env
        stand_in = _test_isolation.isolated_env_file()
        stand_in.write_text("C5A_STAND_IN=1\n")
        try:
            self.assertEqual(dotenv.dotenv_values(env_file), {"C5A_STAND_IN": "1"})
            self.assertEqual(dotenv.get_key(env_file, "C5A_STAND_IN"), "1")
        finally:
            stand_in.unlink()
        # and so in a child that imports dotenv itself
        code = ("import dotenv, _test_isolation as t; "
                "print(dotenv.load_dotenv(%r), t.isolated_env_file().parent == t.temp_dir())"
                % str(env_file))
        r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False True")

    def test_writes_under_data_are_blocked_reads_allowed(self):
        B = _test_isolation.ProductionAccessBlocked
        with self.assertRaises(B):
            open(DATA / "c5a_blocked.txt", "w")
        with self.assertRaises(B):
            open("data/c5a_blocked.jsonl", "a")
        with self.assertRaises(B):
            os.makedirs(DATA / "c5a_new_dir", exist_ok=True)
        with self.assertRaises(B):
            os.remove(DATA / "c5a_does_not_exist")
        with self.assertRaises(B):
            sqlite3.connect(str(DATA / "sub" / "c5a.db"))     # not redirected -> blocked
        DATA.mkdir(exist_ok=True)                             # existing dir: allowed
        for ref in sorted(DATA.glob("*.csv")):
            ref.read_bytes()                                  # reads: allowed
        self.assertFalse((DATA / "c5a_blocked.txt").exists())
        self.assertFalse((DATA / "c5a_new_dir").exists())
        code = "open('data/c5a_child_blocked.txt', 'w')"
        r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("BLOCKED write to production data", r.stderr)
        self.assertFalse((DATA / "c5a_child_blocked.txt").exists())

    def test_broker_ports_and_network_are_blocked(self):
        import socket
        B = _test_isolation.ProductionAccessBlocked
        for port in sorted(_test_isolation.BROKER_PORTS):
            s = socket.socket()
            try:
                with self.assertRaises(B, msg=port):
                    s.connect(("127.0.0.1", port))
            finally:
                s.close()
        with self.assertRaises(B):
            socket.create_connection(("192.0.2.1", 443), timeout=1)   # TEST-NET-1
        with self.assertRaises(B):
            socket.getaddrinfo("example.com", 443)
        pub = socket.socket()
        try:                                                  # public listeners: blocked
            with self.assertRaises(B):
                pub.bind(("0.0.0.0", 0))
        finally:
            pub.close()
        srv = socket.socket()
        try:                                                  # loopback test servers: allowed
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            socket.create_connection(srv.getsockname(), timeout=2).close()
        finally:
            srv.close()
        code = "import socket; socket.create_connection(('127.0.0.1', 4002), timeout=1)"
        r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("BLOCKED broker", r.stderr)     # lookup or connect

    def test_runner_runs_modules_isolated_and_refuses_operator_scripts(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ALGO_TEST_ISOLATION_DIR", "PYTHONPATH")}
        r = subprocess.run([sys.executable, "_test_isolation.py", "test_m21_c1_kill_switch"],
                           cwd=str(REPO), env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ISOLATED_RUN modules=1 failed=0 no_tests=0", r.stdout)
        # the runner's per-module children really are isolated and guarded
        r = subprocess.run([sys.executable, "_test_isolation.py", "--self-check"],
                           cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SELF_CHECK_OK", r.stdout)
        for script in ("test_m10", "test_m11.py", "test_m12_live_order"):
            r = subprocess.run([sys.executable, "_test_isolation.py", script],
                               cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2, script)
            self.assertIn("REFUSED", r.stderr, script)

    def test_refuses_isolation_dir_inside_data(self):
        code = ("import _test_isolation as t\n"
                "try:\n    t.install(%r)\nexcept RuntimeError:\n    print('REFUSED')\n"
                % str(DATA / "c5a_bad_dir"))
        env = {k: v for k, v in os.environ.items()
               if k not in ("ALGO_TEST_ISOLATION_DIR", "PYTHONPATH")}
        # -S: no sitecustomize, so this child starts without inherited isolation.
        r = subprocess.run([sys.executable, "-S", "-c", code], cwd=str(REPO), env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("REFUSED", r.stdout, r.stderr)

    def test_operator_confirmation_helper(self):
        os.environ.pop(_test_isolation.OPERATOR_CONFIRM_ENV, None)
        with self.assertRaises(SystemExit) as cm:
            _test_isolation.require_operator_confirmation("/x/test_m10.py")
        self.assertEqual(cm.exception.code, 2)
        os.environ[_test_isolation.OPERATOR_CONFIRM_ENV] = "test_m10.py"
        try:
            _test_isolation.require_operator_confirmation("/x/test_m10.py")
            with self.assertRaises(SystemExit):
                _test_isolation.require_operator_confirmation("/x/test_m11.py")
        finally:
            os.environ.pop(_test_isolation.OPERATOR_CONFIRM_ENV, None)


class TestRunnerFailsClosed(unittest.TestCase):
    """The runner must refuse the WHOLE run, before launching any test module,
    when isolation is not effective in its children. Reproduced by the
    independent review: another sitecustomize loaded before the generated one
    left children unisolated while the run still reported success."""

    MODULE = "test_m21_c1_kill_switch"

    def _decoy(self, body):
        d = Path(tempfile.mkdtemp(prefix="c5a_decoy_site_"))
        self.addCleanup(shutil.rmtree, str(d), True)
        (d / "sitecustomize.py").write_text(body)
        return d

    def _run_runner(self, argv, decoy, modules_only=False):
        """Run the real runner in-process, with a pre-existing sitecustomize
        placed ahead of the generated one in every (or only per-module) child
        environment; record every unittest launch."""
        real_env = _test_isolation._module_child_env
        real_run = subprocess.run
        launched = []

        def shadowed_env(d):
            env = real_env(d)
            if not (modules_only and d.name == "_preflight"):
                env["PYTHONPATH"] = str(decoy) + os.pathsep + env["PYTHONPATH"]
            return env

        def recording_run(cmd, *a, **kw):
            if "unittest" in cmd:
                launched.append(cmd)
            return real_run(cmd, *a, **kw)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(_test_isolation, "_module_child_env", shadowed_env), \
                mock.patch.object(subprocess, "run", recording_run), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = _test_isolation._run(argv)
        return rc, out.getvalue(), err.getvalue(), launched

    def test_refuses_whole_run_when_another_sitecustomize_loads_first(self):
        decoy = self._decoy("# a pre-existing sitecustomize; does not install isolation\nDECOY = True\n")
        rc, out, err, launched = self._run_runner([self.MODULE, "test_m14_risk"], decoy)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("REFUSED: child-process isolation is not effective", err)
        self.assertIn("NO test module was run", err)
        self.assertIn("isolation is NOT active in a freshly started test child", err)
        self.assertEqual(launched, [])                 # no test module launched at all
        self.assertNotIn("ISOLATED_RUN modules=", out)
        rc, out, err, launched = self._run_runner(["--self-check"], decoy)
        self.assertEqual(rc, 1)
        self.assertIn("SELF_CHECK_FAILED", out)

    def test_refuses_when_processes_started_by_children_lose_isolation(self):
        # Child itself isolated and guarded, but it breaks propagation to the
        # Python processes it starts (inherited environment).
        decoy = self._decoy(
            "import os, _test_isolation as t\n"
            "t.install(os.environ['ALGO_TEST_ISOLATION_DIR'])\n"
            "os.environ.pop('ALGO_TEST_ISOLATION_DIR', None)\n"
            "os.environ['PYTHONPATH'] = t._REPO_S\n")
        rc, out, err, launched = self._run_runner([self.MODULE], decoy)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("(inherited env) is not isolated", err)
        self.assertEqual(launched, [])

    def test_refuses_when_isolation_looks_installed_but_guard_is_missing(self):
        # A child that appears isolated (temp dir set) but has no audit-hook guard.
        decoy = self._decoy(
            "import os, _test_isolation as t\n"
            "from pathlib import Path\n"
            "t._state['tmp'] = Path(os.environ['ALGO_TEST_ISOLATION_DIR'])\n")
        rc, out, err, launched = self._run_runner([self.MODULE], decoy)
        self.assertEqual(rc, 3, out + err)
        for probe in ("env_blocked", "data_blocked", "broker_blocked", "net_blocked"):
            self.assertIn("guard not active in child: %s is False" % probe, err)
        self.assertEqual(launched, [])

    def test_each_module_environment_is_reverified_before_launch(self):
        decoy = self._decoy("DECOY = True\n")
        rc, out, err, launched = self._run_runner([self.MODULE], decoy, modules_only=True)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("ISOLATED_RUN aborted before %s; already run: 0" % self.MODULE, out)
        self.assertEqual(launched, [])

    def test_normal_run_verifies_then_launches(self):
        real_run = subprocess.run
        calls = []

        def recording_run(cmd, *a, **kw):
            calls.append("unittest" if "unittest" in cmd else "verify")
            return real_run(cmd, *a, **kw)

        out = io.StringIO()
        with mock.patch.object(subprocess, "run", recording_run), contextlib.redirect_stdout(out):
            rc = _test_isolation._run([self.MODULE])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("ISOLATED_RUN modules=1 failed=0 no_tests=0", out.getvalue())
        # preflight + per-module verification strictly before the module launch
        self.assertEqual(calls, ["verify", "verify", "unittest"])

    def test_script_style_module_is_reported_as_no_tests_not_failure(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = _test_isolation._run(["test_m12"])     # imported only: defines no tests
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("NO-TESTS", out.getvalue())
        self.assertIn("ISOLATED_RUN modules=1 failed=0 no_tests=1", out.getvalue())


# ─────────────────────────────────────────────────────────────────────────────
# 2. Static guards over every test module
# ─────────────────────────────────────────────────────────────────────────────

class TestStaticIsolationGuards(unittest.TestCase):
    def test_risky_test_modules_install_isolation_first(self):
        offenders = []
        for name in _test_files():
            if name == THIS:
                continue
            src = (REPO / name).read_text(encoding="utf-8")
            tree = ast.parse(src, filename=name)
            risky = (_imports_module(tree, "dashboard.app", attr="app")
                     or _imports_module(tree, "bot.etoro.paper_broker")
                     or any(_imports_module(tree, m) for m in _ENV_LOADING_MODULES)
                     or _spawns_project_cli(tree)
                     or (_calls_kill_switch_writer(tree) and not _patches_kill_switch_path(src)))
            if not risky:
                continue
            inst = _top_level_install_line(tree)
            first = _first_project_import_line(tree)
            if inst is None or (first is not None and first < inst):
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "these tests can touch production files; call "
                         "_test_isolation.install() at module top first")

    def test_known_writer_modules_install_isolation(self):
        writers = [
            "test_group_e_safety.py", "test_m12.py", "test_m13_3_etoro_paper.py",
            "test_m13_4a_allocation.py", "test_m13_4a_runtime_enforcement.py",
            "test_m14_d_exposure.py", "test_m14_g_dashboard.py", "test_m14_risk.py",
            "test_m15_0_service.py", "test_m15_2_health.py", "test_m15_3_a_2_totp.py",
            "test_m15_3_a_dashboard_auth.py", "test_m15_3_b_manual_reset.py",
            "test_m15_3_c_audit_export.py", "test_m15_4_gateway_health.py",
            "test_m16_historical_data.py",
            # read the real kill-switch state (failed when it was missing):
            "test_m21_1extra_b2flat_flatten.py",
            # found by the seeded access probe: read .env, read the real kill
            # switch, opened the real signals.db, or wrote under data/:
            "test_m13_5_cli_env.py", "test_m14_c_ingest.py", "test_m14_f_preflight.py",
            "test_m14_portfolio_ctx.py", "test_m17_backtesting.py",
            "test_m21_1extra_a_paper_order_contract.py",
            "test_m21_1extra_b1_ibkr_paper_contract_dryrun.py",
            "test_m21_1extra_b2a_gateway_readiness.py",
            "test_m21_1extra_b2b_single_submit.py",
            # operator scripts: isolated whenever imported
            "test_m10.py", "test_m11.py", "test_m12_live_order.py",
        ]
        missing = [w for w in writers if _top_level_install_line(_tree(w)) is None]
        self.assertEqual(missing, [])

    def test_only_reference_csvs_are_tracked_under_data(self):
        # The guard allows reads under data/; the only files the repo itself
        # ships there are reference CSVs. Anything else under data/ is
        # production runtime state.
        r = subprocess.run(["git", "ls-files", "data"], cwd=str(REPO),
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            self.skipTest("not a git checkout")
        tracked = [ln for ln in r.stdout.splitlines() if ln.strip()]
        self.assertTrue(all(t.endswith(".csv") and t.count("/") == 1 for t in tracked), tracked)

    def test_no_test_passes_the_real_order_flag(self):
        offenders = [n for n in _test_files() if n != THIS
                     and REAL_ORDER_FLAG in (REPO / n).read_text(encoding="utf-8")]
        self.assertEqual(offenders, [])

    def test_operator_scripts_gate_env_and_require_confirmation(self):
        for name in ("test_m10.py", "test_m11.py", "test_m12_live_order.py"):
            src = (REPO / name).read_text(encoding="utf-8")
            self.assertIn("if __name__ == '__main__' and _env.exists():", src, name)
            self.assertNotIn("\nif _env.exists():", src, name)
            self.assertIn("require_operator_confirmation(__file__)", src, name)
        # test_m12.py: only an operator-confirmed --live run loads .env and
        # skips isolation; everything else is isolated.
        src = (REPO / "test_m12.py").read_text(encoding="utf-8")
        self.assertIn("_OPERATOR_LIVE = (__name__ == '__main__' and '--live' in sys.argv\n"
                      "                  and _test_isolation.operator_run_confirmed(__file__))", src)
        self.assertIn("if _OPERATOR_LIVE and _env.exists():", src)
        self.assertIn("if not _OPERATOR_LIVE:\n    _test_isolation.install()", src)
        self.assertNotIn("\nif _env.exists():", src)
        self.assertIn("require_operator_confirmation(__file__)", src)

    def test_quarantined_scripts_still_have_no_testcases(self):
        # Keep test_quarantine_guard's contract: adding guards must not make
        # the operator scripts discoverable.
        for name in ("test_m10.py", "test_m11.py", "test_m12.py",
                     "test_m12_live_order.py", "test_m14_risk.py"):
            tree = _tree(name)
            names = [n.name for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            self.assertEqual([x for x in names if x.lower().startswith("test")], [], name)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Operator scripts refuse without explicit confirmation
# ─────────────────────────────────────────────────────────────────────────────

class TestOperatorScriptsRefuse(unittest.TestCase):
    def _run(self, *argv):
        env = {k: v for k, v in os.environ.items()
               if k != _test_isolation.OPERATOR_CONFIRM_ENV}
        env.update({"BROKER": "paper", "IBKR_PORT": "1"})
        return subprocess.run([sys.executable, *argv], cwd=str(REPO), env=env,
                              capture_output=True, text=True, timeout=120)

    def test_m10_refuses(self):
        r = self._run("test_m10.py")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("REFUSED", r.stderr)
        self.assertNotIn("M10 CONTROLLED VERIFICATION", r.stdout)

    def test_m11_refuses_even_dry_run(self):
        r = self._run("test_m11.py", "--dry-run")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("REFUSED", r.stderr)

    def test_m12_live_refuses(self):
        r = self._run("test_m12.py", "--live")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("REFUSED", r.stderr)

    def test_m12_live_order_refuses_real_run(self):
        r = self._run("test_m12_live_order.py", "--symbol", "F", "--qty", "1")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("REFUSED", r.stderr)
        self.assertNotIn("CONTROLLED LIVE ORDER TEST", r.stdout)


# ─────────────────────────────────────────────────────────────────────────────
# 4. R2-8: auth/2FA dependencies must be present (no silent skips)
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthDependenciesPresent(unittest.TestCase):
    def test_pyotp_bcrypt_qrcode_installed(self):
        missing = [m for m in ("pyotp", "bcrypt", "qrcode") if find_spec(m) is None]
        if missing and os.environ.get("ALGO_ALLOW_MISSING_AUTH_DEPS") == "1":
            self.skipTest("explicitly allowed missing auth deps: %s" % missing)
        self.assertEqual(missing, [],
                         "dashboard auth/2FA tests would silently skip without "
                         "these (requirements.txt pins them)")


# ─────────────────────────────────────────────────────────────────────────────
# 5. This module itself left the production data dir untouched
# ─────────────────────────────────────────────────────────────────────────────

class TestZZProductionDataUntouched(unittest.TestCase):
    def test_data_dir_unchanged_since_import(self):
        self.assertEqual(_data_snapshot(), _DATA_AT_IMPORT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
