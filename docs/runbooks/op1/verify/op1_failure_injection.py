"""OFFLINE ONLY (never on the VPS). Failure-injection tests for the Gate B stop rules of op1_readonly.sh, and for
op1_deploy_discovery.sh when a non-root account cannot see other users' processes.

usage: python3 -I op1_failure_injection.py <docs checkout>        (needs bash and the sqlite3 CLI)

A database copy that fails, or whose PRAGMA quick_check is not exactly "ok", must stop the whole session with a
nonzero exit code before any SQL report, summary or later step runs; a copy left by an earlier run must never be
read. Each case runs the real script on a throw-away replica, with the sqlite3 CLI and with the Python fallback,
and checks that the source databases are byte-identical afterwards.
"""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

DOCS = os.path.abspath(sys.argv.pop(1)) if len(sys.argv) > 1 else os.getcwd()
SCRIPT = os.path.join(DOCS, "docs", "runbooks", "op1", "op1_readonly.sh")
DBS = ("signals", "paper_lifecycles", "historical")
REPORTS = ("m16_inventory.txt", "old_paper_signals.txt", "old_paper_journal.txt")
LATER = ("parquet_basis_summary.txt", "env_metadata.txt", "runtime_status.txt", "deploy_discovery.txt",
         "MANIFEST.sha256")


def make_db(path, rows=3000):
    c = sqlite3.connect(path)
    c.execute("PRAGMA page_size=1024")
    c.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)")
    c.execute("CREATE INDEX ib ON t (b)")
    c.executemany("INSERT INTO t (b) VALUES (?)", [("v%05d" % i,) for i in range(rows)])
    c.commit()
    c.close()


def corrupt(path, page=10):
    """Overwrite cell bytes of one b-tree page: the file still opens and copies, quick_check fails."""
    with open(path, "rb") as fh:
        data = bytearray(fh.read())
    off = (page - 1) * 1024
    data[off + 200:off + 600] = b"A" * 400
    with open(path, "wb") as fh:
        fh.write(bytes(data))


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class StopRules(unittest.TestCase):
    py_fallback = False

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="op1fi_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "opt", "algo-trader")
        self.out = os.path.join(self.base, "tmp", "m21_op1")
        os.makedirs(os.path.join(self.root, "data"))
        for db in DBS:
            make_db(self.db(db))

    def db(self, name):
        return os.path.join(self.root, "data", name + ".db")

    def run_op1(self):
        before = {n: sha(self.db(n)) for n in DBS}
        env = dict(os.environ, OP1_ROOT=self.root, OP1_OUT=self.out, OP1_PY=sys.executable)
        env.pop("OP1_FORCE_PY_SQL", None)
        if self.py_fallback:
            env["OP1_FORCE_PY_SQL"] = "1"
        r = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual({n: sha(self.db(n)) for n in DBS}, before, "a source database changed")
        log = ""
        if os.path.exists(os.path.join(self.out, "op1.log")):
            with open(os.path.join(self.out, "op1.log")) as fh:
                log = fh.read()
        return r.returncode, log

    def assert_stopped_before_reports(self, log):
        for f in REPORTS + LATER:
            self.assertFalse(os.path.exists(os.path.join(self.out, f)), "%s was produced after a stop" % f)
        self.assertNotIn("OP1 done", log)
        self.assertNotIn("_copy.db ->", log.split("STOP:")[-1])          # no report line after the stop

    def test_healthy_databases_run_to_the_end(self):
        rc, log = self.run_op1()
        self.assertEqual(rc, 0, log)
        self.assertEqual(log.count("quick_check=ok"), 3)
        for f in REPORTS:
            self.assertTrue(os.path.exists(os.path.join(self.out, f)), f)
        self.assertIn("OP1 done", log)

    def test_a_failed_copy_stops_everything(self):
        with open(self.db("signals"), "wb") as fh:                      # not a database: the backup fails
            fh.write(os.urandom(8192))
        rc, log = self.run_op1()
        self.assertEqual(rc, 3, log)
        self.assertIn("STOP: COPY FAILED for signals.db", log)
        self.assert_stopped_before_reports(log)
        self.assertNotIn("paper_lifecycles_copy.db", log)              # later databases were not even copied

    def test_a_failed_copy_of_a_later_database_also_stops(self):
        with open(self.db("historical"), "wb") as fh:
            fh.write(os.urandom(8192))
        rc, log = self.run_op1()
        self.assertEqual(rc, 3, log)
        self.assertEqual(log.count("quick_check=ok"), 2)               # the first two were fine ...
        self.assert_stopped_before_reports(log)                         # ... and are still not reported on

    def test_a_copy_that_fails_quick_check_stops_everything(self):
        corrupt(self.db("paper_lifecycles"))
        rc, log = self.run_op1()
        self.assertEqual(rc, 4, log)
        self.assertIn("STOP: quick_check paper_lifecycles_copy.db:", log)
        self.assert_stopped_before_reports(log)

    def test_copies_from_an_earlier_run_are_never_used(self):
        os.makedirs(self.out)
        stale = os.path.join(self.out, "signals_copy.db")
        make_db(stale, rows=5)
        before = sha(stale)
        rc, log = self.run_op1()
        self.assertEqual(rc, 2, log)
        self.assertIn("already holds 1 *_copy.db", log)
        self.assert_stopped_before_reports(log)
        self.assertEqual(sha(stale), before)
        self.assertEqual(sorted(os.listdir(self.out)), ["op1.log", "signals_copy.db"])


@unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("unshare")
                     and shutil.which("runuser"), "needs root, unshare and runuser (private pid/mount namespace)")
class ProcessVisibility(unittest.TestCase):
    """A non-root session that cannot see other users' processes must say UNKNOWN, never a false "no"."""
    DISCOVERY = os.path.join(DOCS, "docs", "runbooks", "op1", "op1_deploy_discovery.sh")

    def run_case(self, *, hide, as_root):
        base = tempfile.mkdtemp(prefix="op1vis_")
        self.addCleanup(shutil.rmtree, base, True)
        os.chmod(base, 0o755)
        root, daemon, out = os.path.join(base, "root"), os.path.join(base, "daemon"), os.path.join(base, "out")
        os.makedirs(root)
        os.makedirs(daemon)
        with open(os.path.join(daemon, "sync.sh"), "w") as fh:
            fh.write("sleep 30\n")                                   # a root-owned stand-in for the daemon
        if not as_root:
            os.makedirs(out)
            os.chmod(out, 0o777)
        script = os.path.join(base, "op1_deploy_discovery.sh")       # readable by the unprivileged account
        shutil.copy(self.DISCOVERY, script)
        os.chmod(script, 0o644)
        run = "OP1_ROOT=%s bash %s %s >/dev/null" % (root, script, out)
        inner = ("setsid bash %s/sync.sh >/dev/null 2>&1 </dev/null & sleep 0.5; %s %s; "
                 "grep '^sync_sh_running=' %s/deploy_discovery.txt") % (
            daemon, "mount -o remount,hidepid=2 /proc;" if hide else "",
            run if as_root else "runuser -u nobody -- env " + run, out)
        r = subprocess.run(["unshare", "-m", "-p", "-f", "--mount-proc", "bash", "-c", inner],
                           capture_output=True, text=True, timeout=60)
        return r.stdout.strip()

    def test_visible_processes(self):
        self.assertEqual(self.run_case(hide=False, as_root=False), "sync_sh_running=yes count=1")

    def test_hidden_processes_are_unknown_for_a_non_root_account(self):
        self.assertTrue(self.run_case(hide=True, as_root=False).startswith("sync_sh_running=UNKNOWN"))

    def test_root_sees_through_hidepid(self):
        self.assertEqual(self.run_case(hide=True, as_root=True), "sync_sh_running=yes count=1")


class StopRulesPythonFallback(StopRules):
    """The same rules when the sqlite3 CLI is absent (op1_sql.py)."""
    py_fallback = True


if __name__ == "__main__":
    unittest.main(verbosity=2)
