"""test_m21_paper_d_services.py: M21 PR-d (BLK-5), guarded services installed disabled.

1. Unit files: the paper loop and the monitor are oneshot services started only by
   their timers, each guarded by its own allow-file; IBKR PAPER only; no [Install]
   on the services; timers on UTC calendars.
2. install_paper_units.sh: installs files only (never enables or starts), refuses while
   an allow-file exists or a unit is enabled/active, verifies the result, and has an
   --uninstall rollback. Exercised in a sandbox with a stub systemctl.
3. tools/monitoring/run_checks.py: read-only inputs, alarm DB and log as the only
   outputs, LogSink only, honest journal-only duplicate check, risk-data-block check,
   exit codes 0 / 1 / 2.

Nothing here touches the real systemd, the VPS, a gateway or a broker.
"""
from __future__ import annotations

import ast
import configparser
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# C5a: this module runs subprocesses next to tools/ paths; isolate first.
import _test_isolation  # noqa: E402
_test_isolation.install()

SYSTEMD = REPO / "infra" / "systemd"
UNITS = ("algo-paper-loop.service", "algo-paper-loop.timer",
         "algo-paper-monitor.service", "algo-paper-monitor.timer")
INSTALL = SYSTEMD / "install_paper_units.sh"


def unit(name):
    cp = configparser.RawConfigParser(strict=True)
    cp.optionxform = str
    cp.read_string((SYSTEMD / name).read_text())
    return cp


# ─── 1. unit files ──────────────────────────────────────────────────────────
class UnitFiles(unittest.TestCase):
    def test_loop_service(self):
        u = unit("algo-paper-loop.service")
        from tools.paper_loop.m21_1extra_d2_3_scheduler import REAL_LOOP_FLAG
        self.assertEqual(u["Unit"]["ConditionPathExists"], "/var/lib/algo-trader/ALLOW_PAPER_LOOP")
        s = u["Service"]
        self.assertEqual(s["Type"], "oneshot")
        argv = s["ExecStart"].split()
        self.assertEqual(argv[:4], ["/opt/algo-trader/venv/bin/python3", "-B", "-m",
                                    "tools.paper_loop.m21_1extra_d2_3_scheduler"])
        self.assertEqual(argv[4:], ["--broker", "ibkr-paper", REAL_LOOP_FLAG, "--once"])
        self.assertTrue((REPO / "tools/paper_loop/m21_1extra_d2_3_scheduler.py").is_file())
        self.assertLess(int(s["TimeoutStartSec"]), 900)                # before the next slot
        for k, v in (("NoNewPrivileges", "yes"), ("PrivateTmp", "yes"),
                     ("ProtectSystem", "full"), ("ProtectHome", "read-only")):
            self.assertEqual(s[k], v, k)
        self.assertFalse(u.has_section("Install"))

    def test_monitor_service(self):
        u = unit("algo-paper-monitor.service")
        self.assertEqual(u["Unit"]["ConditionPathExists"],
                         "/var/lib/algo-trader/ALLOW_PAPER_MONITOR")
        s = u["Service"]
        self.assertEqual(s["Type"], "oneshot")
        self.assertEqual(s["ExecStart"], "/opt/algo-trader/venv/bin/python3 -I -B "
                                         "/opt/algo-trader/tools/monitoring/run_checks.py")
        self.assertEqual(s["SuccessExitStatus"], "1")
        self.assertFalse(u.has_section("Install"))

    def test_timers(self):
        lt, mt = unit("algo-paper-loop.timer"), unit("algo-paper-monitor.timer")
        self.assertEqual(lt["Timer"]["Unit"], "algo-paper-loop.service")
        self.assertEqual(lt["Timer"]["OnCalendar"], "Mon..Fri *-*-* 13..21:00/15:00 UTC")
        self.assertEqual(mt["Timer"]["Unit"], "algo-paper-monitor.service")
        self.assertEqual(mt["Timer"]["OnCalendar"], "*-*-* *:00/5:00 UTC")
        for t in (lt, mt):
            self.assertEqual(t["Timer"]["Persistent"], "false")
            self.assertEqual(t["Install"]["WantedBy"], "timers.target")

    def test_paper_only_and_own_allow_files(self):
        text = "\n".join(l for n in UNITS for l in (SYSTEMD / n).read_text().splitlines()
                         if not l.lstrip().startswith("#"))
        for bad in ("ibkr-live", "ibkr_live", "4001", "7496", "ALLOW_BOT_START", "etoro"):
            self.assertNotIn(bad, text)

    def test_existing_units_untouched(self):
        rc = subprocess.run(["git", "diff", "--quiet", "963bb89", "HEAD", "--",
                             "infra/systemd/algo-trader.service",
                             "infra/systemd/algo-trader-dashboard.service",
                             "infra/systemd/install.sh", "infra/systemd/rollback.sh"],
                            cwd=str(REPO), capture_output=True)
        if rc.returncode not in (0, 1):
            self.skipTest("git history unavailable")
        self.assertEqual(rc.returncode, 0)

    def test_systemd_analyze(self):
        if not shutil.which("systemd-analyze"):
            self.skipTest("systemd-analyze not installed")
        for n in UNITS:
            r = subprocess.run(["systemd-analyze", "verify", str(SYSTEMD / n)],
                               capture_output=True, text=True, timeout=60)
            noise = [l for l in r.stderr.splitlines()
                     if l.strip() and "/opt/algo-trader" not in l and "not found" not in l
                     and "is not executable" not in l]
            self.assertEqual(noise, [], n)
        r = subprocess.run(["systemd-analyze", "calendar", "--iterations=40",
                            "Mon..Fri *-*-* 13..21:00/15:00 UTC"],
                           capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, TZ="UTC"))
        if r.returncode != 0:
            self.skipTest("calendar evaluation unavailable")
        utc = re.findall(r"(?:Next elapse|Iteration #\d+): (\w{3}) \d{4}-\d\d-\d\d "
                         r"(\d\d):(\d\d):(\d\d) UTC", r.stdout)
        self.assertEqual(len(utc), 40)
        for dow, hh, mm, ss in utc:
            self.assertIn(dow, ("Mon", "Tue", "Wed", "Thu", "Fri"))
            self.assertTrue(13 <= int(hh) <= 21 and int(mm) % 15 == 0 and ss == "00")


# ─── 2. install script ──────────────────────────────────────────────────────
STUB = r"""#!/bin/bash
echo "systemctl $*" >> "$SANDBOX/systemctl.log"
case "$1" in
  is-enabled) cat "$SANDBOX/enabled_$2" 2>/dev/null || echo disabled ;;
  is-active)  cat "$SANDBOX/active_$2" 2>/dev/null || echo inactive ;;
  show)       echo loaded ;;
esac
exit 0
"""


class InstallScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.base, self.dst, self.state, self.bin = t / "opt", t / "etc", t / "state", t / "bin"
        for d in (self.base / "infra/systemd", self.base / "venv/bin", self.dst, self.state,
                  self.bin):
            d.mkdir(parents=True)
        for n in UNITS:
            shutil.copy(SYSTEMD / n, self.base / "infra/systemd" / n)
        py = self.base / "venv/bin/python3"
        py.write_text("#!/bin/sh\n")
        py.chmod(0o755)
        (self.bin / "systemctl").write_text(STUB)
        (self.bin / "systemctl").chmod(0o755)
        src = INSTALL.read_text()
        src = src.replace('[ "$EUID" -eq 0 ] || refuse "must run as root"', "true")
        src = src.replace("-o root -g root ", "")
        src = src.replace("BASE=/opt/algo-trader", "BASE=%s" % self.base)
        src = src.replace("DST=/etc/systemd/system", "DST=%s" % self.dst)
        src = src.replace("STATE=/var/lib/algo-trader", "STATE=%s" % self.state)
        src = src.replace("'ConditionPathExists=/var/lib/algo-trader/", "'ConditionPathExists=/var/lib/algo-trader/")
        self.script = t / "install.sh"
        self.script.write_text(src)
        self.env = dict(os.environ, PATH="%s:/usr/bin:/bin" % self.bin, SANDBOX=str(t))
        self.t = t

    def run_script(self, *args):
        return subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True,
                              env=self.env, timeout=60)

    def log(self):
        p = self.t / "systemctl.log"
        return p.read_text().splitlines() if p.exists() else []

    def test_syntax(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(INSTALL)]).returncode, 0)

    def test_source_never_enables_or_starts(self):
        code = [l.strip() for l in INSTALL.read_text().splitlines()
                if l.strip() and not l.strip().startswith("#")]
        for l in code:
            self.assertNotRegex(l, r"systemctl\s+(enable|start|restart|reenable|unmask)\b", l)
            self.assertNotIn("touch", l)
            if "--now" in l:
                self.assertIn("systemctl disable --now", l)

    def test_install_copies_files_only(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        for n in UNITS:
            self.assertEqual((self.dst / n).read_text(), (SYSTEMD / n).read_text())
        calls = self.log()
        self.assertIn("systemctl daemon-reload", calls)
        self.assertFalse([c for c in calls if re.search(r"\b(enable|start|restart)\b", c)
                          and "is-enabled" not in c])
        self.assertFalse(any((self.state / a).exists()
                             for a in ("ALLOW_PAPER_LOOP", "ALLOW_PAPER_MONITOR")))
        self.assertIn("verified: installed, disabled, inactive", r.stdout)

    def test_refuses_with_allow_file(self):
        for a in ("ALLOW_PAPER_LOOP", "ALLOW_PAPER_MONITOR"):
            (self.state / a).touch()
            r = self.run_script()
            self.assertEqual(r.returncode, 1)
            self.assertIn("allow-file present", r.stderr)
            self.assertFalse(any((self.dst / n).exists() for n in UNITS))
            (self.state / a).unlink()

    def test_refuses_when_enabled_or_active(self):
        for kind, val in (("enabled", "enabled"), ("active", "active")):
            f = self.t / ("%s_algo-paper-loop.timer" % kind)
            f.write_text(val + "\n")
            r = self.run_script()
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertFalse(any((self.dst / n).exists() for n in UNITS))
            f.unlink()

    def test_unknown_argument_refused(self):
        self.assertEqual(self.run_script("--enable").returncode, 1)

    def test_verification_failure_exit_3(self):
        (self.base / "infra/systemd/algo-paper-loop.service").write_text(
            (SYSTEMD / "algo-paper-loop.service").read_text().replace(
                "ConditionPathExists=/var/lib/algo-trader/ALLOW_PAPER_LOOP", ""))
        self.assertEqual(self.run_script().returncode, 3)

    def test_uninstall(self):
        self.assertEqual(self.run_script().returncode, 0)
        (self.state / "ALLOW_PAPER_LOOP").touch()
        r = self.run_script("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(any((self.dst / n).exists() for n in UNITS))
        self.assertFalse((self.state / "ALLOW_PAPER_LOOP").exists())
        calls = self.log()
        self.assertIn("systemctl disable --now algo-paper-loop.timer", calls)
        self.assertIn("systemctl disable --now algo-paper-monitor.timer", calls)


# ─── 3. monitoring runner ──────────────────────────────────────────────────
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)


def healthy(now=NOW):
    return {"as_of_utc": now.isoformat(), "status": "service_active_api_port_open",
            "expected_port": 4002, "mode": "paper"}


class Runner(unittest.TestCase):
    def setUp(self):
        from bot.paper_loop_d2.journal import LoopJournal
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.journal = str(t / "journal.db")
        LoopJournal(self.journal)
        self.alarms = str(t / "alarms.db")
        self.logf = str(t / "monitoring.log")

    def cycle(self, key, minutes_ago, reason=None, state="DONE"):
        with sqlite3.connect(self.journal) as c:
            c.execute("INSERT INTO loop_cycles (cycle_key, started_at_utc, finished_at_utc, state, "
                      "outcome, reason) VALUES (?,?,?,?,?,?)",
                      (key, (NOW - timedelta(minutes=minutes_ago)).isoformat(),
                       (NOW - timedelta(minutes=minutes_ago - 1)).isoformat(), state,
                       "blocked" if reason else "no_entry", reason))

    def main(self, market_open, health=healthy, extra=()):
        from tools.monitoring import run_checks as RC
        argv = ["--journal", self.journal, "--alarms-db", self.alarms, "--log", self.logf, *extra]
        with mock.patch.object(RC, "market_open_now", lambda now, ms=None: market_open):
            return RC.main(argv, now=NOW, health=health)

    def open_alarms(self):
        with sqlite3.connect(self.alarms) as c:
            return {r[0] for r in c.execute("SELECT check_name FROM monitor_alarms WHERE state "
                                            "IN ('ACTIVE','CLEARED_PENDING_ACK')")}

    def test_quiet_closed_market_exit_0(self):
        before = Path(self.journal).read_bytes()
        self.assertEqual(self.main(False), 0)
        self.assertEqual(self.open_alarms(), set())
        self.assertEqual(Path(self.journal).read_bytes(), before)       # journal read-only
        self.assertIn("run done", Path(self.logf).read_text())

    def test_missing_heartbeat_when_open_alarms_exit_1(self):
        self.assertEqual(self.main(True), 1)
        self.assertIn("loop_heartbeat", self.open_alarms())

    def test_healthy_open_market(self):
        self.cycle("c1", 20)
        self.cycle("c2", 5)
        self.assertEqual(self.main(True), 0, self.open_alarms())

    def test_unknown_market_state_is_not_healthy(self):
        self.cycle("c1", 5)
        self.assertEqual(self.main(None), 1)
        self.assertIn("scheduler_liveness", self.open_alarms())

    def test_gateway_unavailable_or_failing(self):
        def boom():
            raise OSError("x")
        self.assertEqual(self.main(False, health=boom), 1)
        self.assertIn("gateway", self.open_alarms())

    def test_risk_data_blocks_alarm(self):
        # the loop records gate refusals as "risk:<gate reason>"
        self.cycle("c1", 20, "risk:risk_refresh_block:pnl_refresh_rc_1")
        self.cycle("c2", 5, "risk:risk_authority_block:exposure_stale")
        self.assertEqual(self.main(True), 1)
        self.assertEqual(self.open_alarms(), {"risk_data_blocks"})

    def test_one_data_block_then_allow_is_quiet(self):
        self.cycle("c1", 20, "risk:risk_authority_block:exposure_stale")
        self.cycle("c2", 5, None)
        self.assertEqual(self.main(True), 0)

    def test_a_single_latest_data_block_is_not_yet_an_alarm(self):
        self.cycle("c1", 20, None)
        self.cycle("c2", 5, "risk:risk_refresh_block:pnl_refresh_timeout")
        self.assertEqual(self.main(True), 0)
        self.assertNotIn("risk_data_blocks", self.open_alarms())

    def test_duplicate_check_is_journal_only_and_says_so(self):
        from tools.monitoring.run_checks import collect
        res = collect(journal_db=self.journal, now=NOW, interval=timedelta(minutes=15),
                      market_open=False, gateway_health=healthy(), observations={})
        names = {r.check: r for r in res}
        self.assertNotIn("duplicate_orders", names)
        self.assertIn("broker side NOT checked", names["duplicate_orders_journal"].detail)

    def test_journal_duplicates_still_alarm_under_the_full_name(self):
        from bot.monitoring.checks import check_duplicate_orders
        from tools.monitoring.run_checks import journal_only_duplicates
        dup = [{"state": "OPEN", "symbol": "AAA", "client_ref": "a"},
               {"state": "OPEN", "symbol": "AAA", "client_ref": "b"}]
        r = journal_only_duplicates(check_duplicate_orders(dup, None))
        self.assertEqual((r.check, r.status), ("duplicate_orders", "ALARM"))
        r = journal_only_duplicates(check_duplicate_orders([], [{"order_id": 1}]))
        self.assertEqual((r.check, r.status), ("duplicate_orders", "ALARM"))
        from bot.monitoring.checks import CheckResult
        other = CheckResult("duplicate_orders", "UNKNOWN", "CRITICAL", "something else")
        self.assertIs(journal_only_duplicates(other), other)

    def test_data_freshness_evaluated_only_with_observations(self):
        self.cycle("c1", 5)
        p = os.path.join(self.tmp.name, "obs.json")
        Path(p).write_text(json.dumps({"AAA": {"1h": "STALE"}, "BBB": {"1h": "MISSING"}}))
        self.assertEqual(self.main(True, extra=("--observations", p)), 1)
        self.assertIn("data_freshness", self.open_alarms())

    def test_unreadable_journal_is_critical_unknown(self):
        Path(self.journal).write_bytes(b"not a database" * 50)
        self.assertEqual(self.main(False), 1)
        self.assertIn("journal_readable", self.open_alarms())

    def test_run_failure_exit_2(self):
        os.mkdir(self.alarms)                                           # cannot open as a DB
        self.assertEqual(self.main(False), 2)

    def test_market_open_now_mapping(self):
        from tools.monitoring.run_checks import market_open_now
        from test_m21_1extra_d2_3_learning_loop import MS_NOW, market_state
        with mock.patch.dict(os.environ, {"ALPACA_KEY": "k", "ALPACA_SECRET": "s"}):
            self.assertIs(market_open_now(MS_NOW, market_state()), True)
            self.assertIs(market_open_now(MS_NOW, market_state(is_open=False)), False)
            self.assertIsNone(market_open_now(MS_NOW, market_state(clock_code=500)))

        def boom():
            raise RuntimeError
        self.assertIsNone(market_open_now(MS_NOW, boom))


class RiskDataBlocks(unittest.TestCase):
    def test_classification(self):
        from bot.monitoring.risk_data import is_data_block
        for r in ("risk_refresh_block:exposure_refresh_rc_1", "risk_refresh_error:OSError",
                  "risk:risk_refresh_block:pnl_refresh_rc_2", "risk:risk_authority_block:exposure_stale",
                  "risk_authority_block:exposure_stale", "risk_authority_block:daily_pnl_unknown",
                  "risk_authority_block:broker_auto_disabled,global_daily_loss_unknown"):
            self.assertTrue(is_data_block(r), r)
        for r in (None, "", "risk_authority_block:global_kill", "market_state_block:x",
                  "risk:risk_authority_block:global_kill", "risk:market_state_block:x",
                  "risk_authority_allow", "kill_switch_active"):
            self.assertFalse(is_data_block(r), r)


class Boundaries(unittest.TestCase):
    def _mods(self, rel):
        tree = ast.parse((REPO / rel).read_text())
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        return mods, tree

    def test_runner_and_check_have_no_broker_messaging_or_order_surface(self):
        for rel in ("tools/monitoring/run_checks.py", "bot/monitoring/risk_data.py"):
            mods, tree = self._mods(rel)
            bad = [m for m in mods if m.startswith(("ib_insync", "bot.brokers", "bot.etoro",
                                                    "bot.notifier", "requests", "socket",
                                                    "urllib", "telegram", "smtplib"))]
            self.assertEqual(bad, [], rel)
            names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            self.assertFalse({"placeOrder", "cancelOrder", "MemorySink"} & names, rel)

    def test_probe_still_read_only_and_reads_reason(self):
        from bot.monitoring.probe import read_journal
        from bot.paper_loop_d2.journal import LoopJournal
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "j.db")
            LoopJournal(p)
            with sqlite3.connect(p) as c:
                c.execute("INSERT INTO loop_cycles (cycle_key, started_at_utc, state, reason) "
                          "VALUES ('k', ?, 'DONE', 'r')", (NOW.isoformat(),))
            before = Path(p).read_bytes()
            j = read_journal(p)
            self.assertEqual(Path(p).read_bytes(), before)
        self.assertEqual(j["cycles"][0]["reason"], "r")


if __name__ == "__main__":
    unittest.main()
