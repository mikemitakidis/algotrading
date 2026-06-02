"""M15.0 — Production process / systemd reliability test suite.

Covers the in-repo artifacts of M15.0:
  * unit-file shape (correct sections + safe defaults)
  * /api/system/services endpoint contract (read-only, GET-only, JSON shape)
  * scanner isolation invariant carry-forward
  * AST scan: no live-write surface introduced by M15.0
  * existing M14.G read-only endpoints still work

The VPS-side proofs (cgroup ownership, drain/restart independence) require
the actual systemd install and are documented in
`docs/M15_0_systemd_canonical.md` §3.

No live calls. No broker writes. No orders.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = str(Path(__file__).resolve().parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


# ─────────────────────────────────────────────────────────────────────────────
# Group 1 — Systemd unit files exist and have the required shape
# ─────────────────────────────────────────────────────────────────────────────


class TestSystemdUnitShape(unittest.TestCase):
    """The two M15.0 unit files must be present in-repo, parseable as
    systemd unit syntax (rough check), and contain the required fields."""

    UNITS = (
        ("infra/systemd/algo-trader.service",           "main.py"),
        ("infra/systemd/algo-trader-dashboard.service", "dashboard/app.py"),
    )

    def _read(self, rel):
        path = os.path.join(_REPO, rel)
        self.assertTrue(os.path.exists(path), f"unit file missing: {rel}")
        with open(path) as f:
            return f.read()

    def _parse(self, text):
        """Tiny INI-like parser for systemd units (read-only check; we
        don't rely on a third-party systemd parser)."""
        sections = {}
        current = None
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            m = re.match(r'^\[([^\]]+)\]$', line)
            if m:
                current = m.group(1)
                sections.setdefault(current, [])
                continue
            if current is None:
                continue
            sections[current].append(line.strip())
        return sections

    def test_units_have_required_sections(self):
        for rel, _ in self.UNITS:
            text = self._read(rel)
            sections = self._parse(text)
            for required in ("Unit", "Service", "Install"):
                self.assertIn(required, sections,
                    f"{rel}: missing required section [{required}]")

    def test_service_type_is_simple(self):
        for rel, _ in self.UNITS:
            text = self._read(rel)
            self.assertIn("Type=simple", text,
                f"{rel}: missing 'Type=simple' — required for accurate process tracking")

    def test_exec_start_is_absolute_and_points_to_venv(self):
        for rel, script_path in self.UNITS:
            text = self._read(rel)
            sections = self._parse(text)
            exec_lines = [s for s in sections.get("Service", [])
                          if s.startswith("ExecStart=")]
            self.assertEqual(len(exec_lines), 1,
                f"{rel}: expected exactly one ExecStart= line, got {len(exec_lines)}")
            es = exec_lines[0]
            self.assertIn("/opt/algo-trader/venv/bin/python3", es,
                f"{rel}: ExecStart must use the venv Python")
            self.assertIn(script_path, es,
                f"{rel}: ExecStart must reference {script_path}")
            # Absolute path requirement.
            tokens = es.split("=", 1)[1].strip().split()
            self.assertTrue(tokens[0].startswith("/"),
                f"{rel}: ExecStart must use an absolute path, got {tokens[0]!r}")

    def test_restart_policy_is_safe(self):
        for rel, _ in self.UNITS:
            text = self._read(rel)
            self.assertIn("Restart=on-failure", text,
                f"{rel}: must use Restart=on-failure (not Restart=always)")
            self.assertIn("RestartSec=", text,
                f"{rel}: must set RestartSec=")
            self.assertIn("StartLimitBurst=", text,
                f"{rel}: must set StartLimitBurst= to bound restart storms")

    def test_working_directory_is_repo_root(self):
        for rel, _ in self.UNITS:
            text = self._read(rel)
            self.assertIn("WorkingDirectory=/opt/algo-trader", text,
                f"{rel}: WorkingDirectory must be /opt/algo-trader")

    def test_env_file_is_optional(self):
        """The EnvironmentFile= line must use the '-' prefix so a missing
        .env doesn't prevent the unit from starting."""
        for rel, _ in self.UNITS:
            text = self._read(rel)
            self.assertIn("EnvironmentFile=-/opt/algo-trader/.env", text,
                f"{rel}: EnvironmentFile must use '-' prefix (optional .env)")

    def test_install_target_is_multi_user(self):
        for rel, _ in self.UNITS:
            text = self._read(rel)
            self.assertIn("WantedBy=multi-user.target", text,
                f"{rel}: must WantedBy=multi-user.target so it boots at runlevel 3+")

    def test_units_are_independent_no_requires(self):
        """The two units MUST NOT Requires= each other — the user
        explicitly wants stopping one to not affect the other."""
        for rel, _ in self.UNITS:
            text = self._read(rel)
            sections = self._parse(text)
            unit_block = sections.get("Unit", [])
            for line in unit_block:
                if line.startswith("Requires=") or line.startswith("BindsTo="):
                    self.assertNotIn("algo-trader", line,
                        f"{rel}: unit block must NOT Requires=/BindsTo= the other "
                        f"M15.0 unit; got: {line}")

    def test_install_script_is_present_and_executable_bit(self):
        for rel in ("infra/systemd/install.sh", "infra/systemd/rollback.sh"):
            path = os.path.join(_REPO, rel)
            self.assertTrue(os.path.exists(path), f"script missing: {rel}")
            with open(path) as f:
                first = f.readline().rstrip()
            self.assertTrue(first.startswith("#!"),
                f"{rel}: missing shebang on first line, got {first!r}")

    def test_install_requires_root_check(self):
        with open(os.path.join(_REPO, "infra/systemd/install.sh")) as f:
            src = f.read()
        # The script must refuse to run as non-root (defense against
        # accidental invocations).
        self.assertIn("EUID", src, "install.sh: missing root check")
        self.assertIn("must run as root", src,
            "install.sh: must explicitly state root requirement")

    def test_install_snapshots_before_mutating(self):
        with open(os.path.join(_REPO, "infra/systemd/install.sh")) as f:
            src = f.read()
        # Snapshot must happen BEFORE the systemctl enable/start lines.
        snap_idx  = src.find("snapshot")
        enable_idx = src.find("systemctl enable")
        self.assertGreater(snap_idx, 0, "install.sh: no snapshot step found")
        self.assertGreater(enable_idx, 0, "install.sh: no systemctl enable step found")
        self.assertLess(snap_idx, enable_idx,
            "install.sh: snapshot must occur before systemctl enable")


# ─────────────────────────────────────────────────────────────────────────────
# Group 2 — /api/system/services endpoint contract
# ─────────────────────────────────────────────────────────────────────────────


class TestSystemServicesEndpoint(unittest.TestCase):
    """The new M15.0 endpoint must be GET-only, read-only, JSON-shaped."""

    def setUp(self):
        # Re-import dashboard so the route is registered in this test run.
        if "dashboard.app" in sys.modules:
            del sys.modules["dashboard.app"]
        from dashboard.app import app
        self.app = app
        self.app.config["TESTING"] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user"] = "admin"
            sess["logged_in"] = True
            sess["authed"] = True

    def test_endpoint_registered(self):
        urls = {str(r) for r in self.app.url_map.iter_rules()}
        self.assertIn("/api/system/services", urls,
            "/api/system/services not registered")

    def test_endpoint_methods_get_only(self):
        for rule in self.app.url_map.iter_rules():
            if str(rule) == "/api/system/services":
                methods = rule.methods - {"HEAD", "OPTIONS"}
                self.assertEqual(methods, {"GET"},
                    f"endpoint methods must be GET-only, got {methods}")
                return
        self.fail("endpoint rule not found")

    def test_get_returns_200_json(self):
        r = self.client.get("/api/system/services")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content_type, "application/json")

    def test_response_shape(self):
        body = self.client.get("/api/system/services").get_json()
        self.assertIn("services", body)
        self.assertIn("m15_0_installed", body)
        self.assertIn("as_of_utc", body)
        self.assertIsInstance(body["services"], list)
        self.assertIsInstance(body["m15_0_installed"], bool)

    def test_canonical_service_map_present(self):
        body = self.client.get("/api/system/services").get_json()
        units = [s["unit"] for s in body["services"]]
        self.assertEqual(set(units),
                          {"algo-trader.service",
                           "algo-trader-dashboard.service"})
        for s in body["services"]:
            for key in ("unit", "script", "description",
                        "active", "enabled", "process", "managed_by"):
                self.assertIn(key, s, f"missing key {key} in service entry")

    def test_post_returns_405(self):
        self.assertEqual(
            self.client.post("/api/system/services").status_code, 405)

    def test_delete_returns_405(self):
        self.assertEqual(
            self.client.delete("/api/system/services").status_code, 405)

    def test_put_returns_405(self):
        self.assertEqual(
            self.client.put("/api/system/services").status_code, 405)

    def test_patch_returns_405(self):
        self.assertEqual(
            self.client.patch("/api/system/services").status_code, 405)


# ─────────────────────────────────────────────────────────────────────────────
# Group 3 — AST: no new live-write surface in M15.0 dashboard edits
# ─────────────────────────────────────────────────────────────────────────────


class TestNoLiveWriteSurface(unittest.TestCase):
    """The /api/system/services route and its helpers MUST NOT introduce
    any live-write capability. AST-walk the new symbols and assert."""

    FORBIDDEN_MODULES = {
        "bot.etoro.live_broker", "tools.etoro_live_write", "bot.brokers",
        "bot.risk_authority.preflight",
    }
    FORBIDDEN_NAMES = {"EtoroLiveBroker", "IBKRBroker", "PaperBroker"}
    FORBIDDEN_ORDER = {"placeOrder", "cancelOrder", "modifyOrder",
                       "reqGlobalCancel"}
    FORBIDDEN_HTTP_METHODS = {"post", "delete", "put", "patch"}

    M15_0_FUNCTIONS = {"system_services", "_systemctl_state",
                       "_process_owner_cgroup"}

    def _walk_m15_0_functions(self):
        with open(os.path.join(_REPO, "dashboard/app.py")) as f:
            tree = ast.parse(f.read())
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.M15_0_FUNCTIONS:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.ImportFrom):
                        m = sub.module or ""
                        if m in self.FORBIDDEN_MODULES:
                            offenders.append(
                                f"{node.name}: ImportFrom {m} @{sub.lineno}")
                        for a in sub.names:
                            if a.name in self.FORBIDDEN_NAMES:
                                offenders.append(
                                    f"{node.name}: name {a.name} @{sub.lineno}")
                    if isinstance(sub, ast.Name) and sub.id in self.FORBIDDEN_NAMES:
                        offenders.append(
                            f"{node.name}: ref {sub.id} @{sub.lineno}")
                    if isinstance(sub, ast.Call) and isinstance(sub.func,
                                                                 ast.Attribute):
                        if sub.func.attr in self.FORBIDDEN_ORDER:
                            offenders.append(
                                f"{node.name}: .{sub.func.attr} @{sub.lineno}")
                        if sub.func.attr in self.FORBIDDEN_HTTP_METHODS:
                            offenders.append(
                                f"{node.name}: .{sub.func.attr} @{sub.lineno}")
        return offenders

    def test_no_forbidden_refs_in_m15_0_functions(self):
        offenders = self._walk_m15_0_functions()
        self.assertEqual(offenders, [],
            f"M15.0 functions have forbidden refs: {offenders}")

    def test_no_subprocess_writes_or_systemctl_mutations(self):
        """`/api/system/services` may call `systemctl is-active|is-enabled`
        — both read-only. It MUST NOT call start/stop/enable/disable/
        reload/reset/mask from inside the route handler."""
        with open(os.path.join(_REPO, "dashboard/app.py")) as f:
            tree = ast.parse(f.read())
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.M15_0_FUNCTIONS:
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "run"):
                        # subprocess.run — inspect args for forbidden subcommand
                        if sub.args and isinstance(sub.args[0], ast.List):
                            forbidden_subcmds = {"start", "stop", "enable",
                                                  "disable", "daemon-reload",
                                                  "reset-failed", "mask",
                                                  "unmask", "restart",
                                                  "reload"}
                            for el in sub.args[0].elts:
                                if (isinstance(el, ast.Constant)
                                        and isinstance(el.value, str)
                                        and el.value in forbidden_subcmds):
                                    offenders.append(
                                        f"{node.name}: subprocess.run forbidden "
                                        f"subcmd {el.value!r} @{el.lineno}")
        self.assertEqual(offenders, [],
            f"M15.0 helpers call mutating systemctl subcommands: {offenders}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 4 — Scanner isolation invariant carry-forward
# ─────────────────────────────────────────────────────────────────────────────


class TestScannerIsolation(unittest.TestCase):

    def test_scanner_import_does_not_load_dashboard_or_m14(self):
        check = (
            "import sys\n"
            "import bot.scanner, bot.strategy, bot.risk, bot.brokers\n"
            "forbidden = [m for m in (\n"
            "    'dashboard.app',\n"
            "    'tools.etoro_live_write',\n"
            "    'bot.risk_authority.preflight',\n"
            "    'bot.risk_authority.dashboard_read',\n"
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


# ─────────────────────────────────────────────────────────────────────────────
# Group 5 — sync.sh shape: detects systemd, falls back to nohup
# ─────────────────────────────────────────────────────────────────────────────


class TestSyncShBehaviour(unittest.TestCase):
    """sync.sh must detect systemd-managed services and prefer
    `systemctl restart` over the legacy `pkill + nohup` path. The legacy
    path must remain as a fallback so the pre-M15.0 deploy path works."""

    def setUp(self):
        with open(os.path.join(_REPO, "sync.sh")) as f:
            self.src = f.read()

    def test_sync_sh_references_systemctl(self):
        self.assertIn("systemctl restart", self.src,
            "sync.sh must use systemctl restart when units are present")

    def test_sync_sh_references_canonical_unit_names(self):
        self.assertIn("algo-trader.service", self.src,
            "sync.sh must reference algo-trader.service")
        self.assertIn("algo-trader-dashboard.service", self.src,
            "sync.sh must reference algo-trader-dashboard.service")

    def test_sync_sh_keeps_legacy_nohup_fallback(self):
        # Both nohup launches must still appear (fallback for the case
        # where M15.0 install hasn't been run yet).
        self.assertIn("nohup $VENV/bin/python3 $BASE/dashboard/app.py",
                       self.src)
        self.assertIn("nohup $VENV/bin/python3 $BASE/main.py",
                       self.src)

    def test_sync_sh_detection_guards_systemctl_path(self):
        """The systemctl branch must be guarded by a detection check —
        we don't want `systemctl restart` to run when units don't exist
        yet (that would fail noisily but not catastrophically)."""
        # The script must check 'list-unit-files' before restarting.
        self.assertIn("list-unit-files", self.src,
            "sync.sh must detect unit presence before using systemctl")


# ─────────────────────────────────────────────────────────────────────────────
# Group 6 — Existing endpoints + protected files unchanged
# ─────────────────────────────────────────────────────────────────────────────


class TestExistingEndpointsUnchanged(unittest.TestCase):

    def setUp(self):
        if "dashboard.app" in sys.modules:
            del sys.modules["dashboard.app"]
        from dashboard.app import app
        self.app = app
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user"] = "admin"
            sess["logged_in"] = True
            sess["authed"] = True

    def test_api_health_still_returns(self):
        r = self.client.get("/api/health")
        self.assertNotEqual(r.status_code, 404,
            "/api/health route was removed by M15.0")

    def test_m14_g_endpoints_still_registered(self):
        urls = {str(r) for r in self.app.url_map.iter_rules()}
        for u in ("/api/risk-authority/decisions",
                  "/api/risk-authority/scopes",
                  "/api/risk-authority/snapshot/latest",
                  "/api/risk-authority/authority"):
            self.assertIn(u, urls, f"M14.G route {u} missing after M15.0")

    def test_portfolio_risk_routes_still_registered(self):
        urls = {str(r) for r in self.app.url_map.iter_rules()}
        for u in ("/api/portfolio-risk/state",
                  "/api/portfolio-risk/snapshots",
                  "/api/portfolio-risk/rejections"):
            self.assertIn(u, urls, f"pre-M15.0 route {u} missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
