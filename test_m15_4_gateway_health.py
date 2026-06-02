"""M15.4 — IB Gateway reliability test suite.

Read-only point-in-time truth layer (`bot/gateway_health.py`) plus the
new `GET /api/gateway/health` endpoint.

Tests:
  * helper functions return correct shape under mocked sources
  * classification table is exhaustive across the closed-set statuses
  * endpoint is GET-only, JSON-shaped, auth-protected
  * AST scan: no IB API call, no broker construction, no systemctl
    mutation, no order method anywhere in bot/gateway_health.py or
    the new route handler
  * the existing /api/gateway/state route is unchanged
  * scanner isolation invariant carry-forward
  * the current-audit-state scenario classifies as not-ready
    (systemd active + port closed + login error -> service_active_login_error)

No live calls. No broker writes. No orders. No IB API call.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_REPO = str(Path(__file__).resolve().parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from bot.gateway_health import (
    GATEWAY_UNIT, PORT_LIVE, PORT_PAPER, STATUSES,
    _classify, assemble_health,
    detect_trading_mode, read_log_tail,
    read_recent_lifecycle_events, read_systemd_state,
    probe_tcp_listening,
    LOGIN_ERROR_PATTERNS,
)


# ─────────────────────────────────────────────────────────────────────────────
# Group 1 — Helper functions (read-only, mocked sources)
# ─────────────────────────────────────────────────────────────────────────────


class TestReadSystemdState(unittest.TestCase):
    """read_systemd_state must call only is-active / is-enabled / show
    and aggregate the results. Mocked subprocess responses."""

    @patch("bot.gateway_health.subprocess.run")
    def test_active_enabled_with_full_show(self, mock_run):
        # 1st call: is-active -> "active"
        # 2nd call: is-enabled -> "enabled"
        # 3rd call: show --property=... -> properties block
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="active\n", stderr=""),
            MagicMock(returncode=0, stdout="enabled\n", stderr=""),
            MagicMock(returncode=0, stdout=(
                "SubState=running\n"
                "MainPID=3637174\n"
                "ActiveEnterTimestamp=Sat 2026-05-30 06:56:35 UTC\n"
                "NRestarts=0\n"
                "FragmentPath=/etc/systemd/system/ibgateway.service\n"
            ), stderr=""),
        ]
        r = read_systemd_state()
        self.assertEqual(r["active"], "active")
        self.assertEqual(r["enabled"], "enabled")
        self.assertEqual(r["sub_state"], "running")
        self.assertEqual(r["main_pid"], 3637174)
        self.assertEqual(r["n_restarts"], 0)
        self.assertTrue(r["source_ok"])

    @patch("bot.gateway_health.subprocess.run")
    def test_systemd_subprocess_failure(self, mock_run):
        mock_run.side_effect = FileNotFoundError("systemctl missing")
        r = read_systemd_state()
        self.assertEqual(r["active"], "unknown")
        self.assertFalse(r["source_ok"])

    @patch("bot.gateway_health.subprocess.run")
    def test_systemd_returns_not_found_unit(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=3, stdout="inactive\n", stderr=""),
            MagicMock(returncode=1, stdout="not-found\n", stderr=""),
            MagicMock(returncode=0, stdout="SubState=dead\nMainPID=0\n",
                       stderr=""),
        ]
        r = read_systemd_state()
        self.assertEqual(r["active"], "inactive")
        self.assertEqual(r["enabled"], "not-found")
        self.assertIsNone(r["main_pid"])  # MainPID=0 means no process

    @patch("bot.gateway_health.subprocess.run")
    def test_only_readonly_systemctl_subcommands_used(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="active",
                                            stderr="")
        read_systemd_state()
        for call in mock_run.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[0], "systemctl")
            self.assertIn(argv[1], ("is-active", "is-enabled", "show"))


class TestProbeTcpListening(unittest.TestCase):
    """Probe must return True/False/None and MUST NOT send any bytes."""

    @patch("bot.gateway_health.socket.create_connection")
    def test_connection_accepted_means_true(self, mock_cc):
        mock_sock = MagicMock()
        mock_cc.return_value = mock_sock
        self.assertTrue(probe_tcp_listening("127.0.0.1", 4002))
        # MUST close the socket immediately. MUST NOT call send/sendall/recv.
        mock_sock.close.assert_called_once()
        for forbidden in ("send", "sendall", "recv", "write"):
            self.assertFalse(getattr(mock_sock, forbidden).called,
                f"probe_tcp_listening called {forbidden}() — that would be "
                f"an actual IB API write")

    @patch("bot.gateway_health.socket.create_connection")
    def test_connection_refused_means_false(self, mock_cc):
        mock_cc.side_effect = ConnectionRefusedError()
        self.assertFalse(probe_tcp_listening("127.0.0.1", 4002))

    @patch("bot.gateway_health.socket.create_connection")
    def test_socket_timeout_means_none(self, mock_cc):
        import socket as _s
        mock_cc.side_effect = _s.timeout()
        self.assertIsNone(probe_tcp_listening("127.0.0.1", 4002))

    @patch("bot.gateway_health.socket.create_connection")
    def test_unrelated_oserror_means_none(self, mock_cc):
        mock_cc.side_effect = OSError("network unreachable")
        self.assertIsNone(probe_tcp_listening("127.0.0.1", 4002))


class TestDetectTradingMode(unittest.TestCase):

    def test_no_files_unknown(self):
        # Point at directories that don't exist.
        r = detect_trading_mode(
            start_script=Path("/nonexistent/start.sh"),
            ibc_config_dir=Path("/nonexistent/ibc"),
        )
        self.assertEqual(r["mode"], "unknown")
        self.assertIsNone(r["expected_port"])

    def test_paper_from_start_script_env(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sh = Path(tmp) / "start.sh"
            sh.write_text("#!/bin/bash\nTRADING_MODE=paper\nexec ibcalpha\n")
            r = detect_trading_mode(
                start_script=sh,
                ibc_config_dir=Path("/nonexistent"),
            )
            self.assertEqual(r["mode"], "paper")
            self.assertEqual(r["expected_port"], PORT_PAPER)
            self.assertTrue(any("TRADING_MODE=paper" in e
                                  for e in r["evidence"]))

    def test_live_from_start_script_arg(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sh = Path(tmp) / "start.sh"
            sh.write_text("ibcalpha --mode=live --foo=bar\n")
            r = detect_trading_mode(
                start_script=sh,
                ibc_config_dir=Path("/nonexistent"),
            )
            self.assertEqual(r["mode"], "live")
            self.assertEqual(r["expected_port"], PORT_LIVE)

    def test_config_ini_paper(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.ini"
            cfg.write_text("[IBC]\nIbLoginId=fake\nTradingMode=paper\n")
            r = detect_trading_mode(
                start_script=Path("/nonexistent/start.sh"),
                ibc_config_dir=Path(tmp),
            )
            self.assertEqual(r["mode"], "paper")
            self.assertEqual(r["expected_port"], PORT_PAPER)


class TestReadLogTail(unittest.TestCase):

    def test_missing_log_returns_source_ok(self):
        r = read_log_tail(log_path=Path("/nonexistent/ibgateway.log"))
        self.assertFalse(r["present"])
        self.assertFalse(r["login_error_detected"])
        # absence is a known answer
        self.assertTrue(r["source_ok"])

    def test_login_error_match_unrecognized(self):
        import tempfile
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".log", delete=False) as fh:
            fh.write("starting up\n")
            fh.write("login attempt\n")
            fh.write("Dialog: Unrecognized Username or Password\n")
            fh.write("retrying\n")
            path = Path(fh.name)
        try:
            r = read_log_tail(log_path=path)
            self.assertTrue(r["present"])
            self.assertTrue(r["login_error_detected"])
            self.assertIn("Unrecognized", r["matched_pattern"])
        finally:
            os.unlink(path)

    def test_no_login_error_in_clean_log(self):
        import tempfile
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".log", delete=False) as fh:
            fh.write("Gateway started\n")
            fh.write("API server listening on 4002\n")
            path = Path(fh.name)
        try:
            r = read_log_tail(log_path=path)
            self.assertTrue(r["present"])
            self.assertFalse(r["login_error_detected"])
            self.assertIsNone(r["matched_pattern"])
        finally:
            os.unlink(path)

    def test_multiple_login_error_patterns_all_match(self):
        """Sanity: each pattern in LOGIN_ERROR_PATTERNS triggers
        detection on its own. Guards against accidental pattern
        breakage during future edits."""
        import tempfile
        for pattern in LOGIN_ERROR_PATTERNS:
            # The pattern is a regex; build a string that ought to match.
            # Strip optional whitespace markers.
            sample = (pattern
                      .replace(r"\s+", " ")
                      .replace(r"\s", " ")
                      .replace("(?:", "")
                      .replace(")", "")
                      .replace("|username", " username")
                      .replace("|password", " password")
                      .replace("|credentials", " credentials")
                      .replace("|error", " error")
                      .replace("|failed", " failed")
                      .replace("failed|error", "failed")
                      .strip())
            sample_line = f"INFO  Dialog: {sample}\n"
            with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".log", delete=False) as fh:
                fh.write(sample_line)
                path = Path(fh.name)
            try:
                r = read_log_tail(log_path=path)
                self.assertTrue(
                    r["login_error_detected"],
                    f"pattern {pattern!r} did not match its own sample "
                    f"{sample_line!r}",
                )
            finally:
                os.unlink(path)


class TestReadRecentLifecycleEvents(unittest.TestCase):

    @patch("bot.gateway_health.subprocess.run")
    def test_event_extraction_and_counts(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=(
            "2026-06-01T01:00:00 host systemd[1]: Started IB Gateway.\n"
            "2026-06-01T03:15:00 host ibc[123]: main process exited\n"
            "2026-06-01T03:15:30 host systemd[1]: Scheduled restart job\n"
            "2026-06-01T03:16:00 host systemd[1]: Started IB Gateway.\n"
            "2026-06-02T05:00:00 host systemd[1]: Failed with result.\n"
            "2026-06-02T05:00:30 host systemd[1]: Started IB Gateway.\n"
            "2026-06-02T05:00:31 host kernel: unrelated noise\n"
        ), stderr="")
        r = read_recent_lifecycle_events()
        self.assertTrue(r["source_ok"])
        self.assertGreaterEqual(r["n_restarts_30d"], 3)
        self.assertGreaterEqual(r["n_failures_30d"], 2)
        self.assertGreater(len(r["events"]), 0)
        for e in r["events"]:
            self.assertTrue(any(kw in e for kw in (
                "Started", "Stopped", "Failed", "exited",
                "Scheduled restart", "killed signal")))

    @patch("bot.gateway_health.subprocess.run")
    def test_subprocess_unreachable(self, mock_run):
        mock_run.side_effect = FileNotFoundError("journalctl missing")
        r = read_recent_lifecycle_events()
        self.assertFalse(r["source_ok"])
        self.assertEqual(r["events"], [])
        self.assertIsNone(r["n_restarts_30d"])


# ─────────────────────────────────────────────────────────────────────────────
# Group 2 — Classification table (covers all valid combos)
# ─────────────────────────────────────────────────────────────────────────────


class TestClassification(unittest.TestCase):

    def test_classify_systemd_inactive_means_service_down(self):
        s = _classify({"active": "inactive"},
                      tcp_reachable=False, login_error_detected=False)
        self.assertEqual(s, "service_down")

    def test_classify_systemd_failed_means_service_down(self):
        s = _classify({"active": "failed"},
                      tcp_reachable=None, login_error_detected=False)
        self.assertEqual(s, "service_down")

    def test_classify_active_port_open_is_ready(self):
        s = _classify({"active": "active"},
                      tcp_reachable=True, login_error_detected=False)
        self.assertEqual(s, "service_active_api_port_open")

    def test_classify_active_port_closed_with_login_error(self):
        """The current-audit-state scenario from the user's M15.4 audit."""
        s = _classify({"active": "active"},
                      tcp_reachable=False, login_error_detected=True)
        self.assertEqual(s, "service_active_login_error")

    def test_classify_active_port_closed_no_login_error(self):
        s = _classify({"active": "active"},
                      tcp_reachable=False, login_error_detected=False)
        self.assertEqual(s, "service_active_port_closed")

    def test_classify_active_tcp_probe_unknown(self):
        s = _classify({"active": "active"},
                      tcp_reachable=None, login_error_detected=False)
        self.assertEqual(s, "unknown")

    def test_classify_systemd_unknown_means_unknown(self):
        s = _classify({"active": "unknown"},
                      tcp_reachable=True, login_error_detected=False)
        self.assertEqual(s, "unknown")

    def test_login_error_does_not_falsely_promote_port_open(self):
        """Login-error patterns never matter when the port is actually
        open — the port being open wins."""
        s = _classify({"active": "active"},
                      tcp_reachable=True, login_error_detected=True)
        self.assertEqual(s, "service_active_api_port_open")

    def test_status_value_belongs_to_closed_set(self):
        """No combination of inputs may produce a status outside STATUSES."""
        for active in ("active", "inactive", "failed", "activating",
                        "deactivating", "unknown", "garbage"):
            for tcp in (True, False, None):
                for login in (True, False):
                    s = _classify({"active": active},
                                   tcp_reachable=tcp,
                                   login_error_detected=login)
                    self.assertIn(s, STATUSES,
                        f"_classify produced out-of-set status {s!r} for "
                        f"active={active} tcp={tcp} login={login}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 3 — assemble_health current-audit scenario reproduction
# ─────────────────────────────────────────────────────────────────────────────


class TestAssembleHealthCurrentAuditScenario(unittest.TestCase):
    """Reproduce the exact state the user reported in the M15.4 audit:
       * systemd active=active enabled=enabled
       * mode=paper from start_ibgateway.sh + config.ini
       * ports 4001 and 4002 NOT listening
       * log tail contains 'Unrecognized Username or Password'
       * journalctl returns some lifecycle events

    Expected: status=service_active_login_error, ready_for_ibkr_trading=False.
    """

    def test_current_audit_state_classified_as_login_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # Build a fake start script + config that say paper
            sh = Path(tmp) / "start_ibgateway.sh"
            sh.write_text("TRADING_MODE=paper\nexec ibcalpha --mode=paper\n")
            cfg_dir = Path(tmp) / "ibc"
            cfg_dir.mkdir()
            (cfg_dir / "config.ini").write_text("TradingMode=paper\n")
            # Build a fake log tail with the exact pattern the audit found
            log = Path(tmp) / "ibgateway.log"
            log.write_text(
                "Gateway started\n"
                "IBC command server starting on port 7462\n"
                "Login attempt\n"
                "Dialog: Unrecognized Username or Password\n"
                "Awaiting operator action\n"
            )

            with patch("bot.gateway_health.subprocess.run") as mock_run:
                # Order:
                #   read_systemd_state: is-active, is-enabled, show (3)
                #   read_recent_lifecycle_events: journalctl       (1)
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="active\n", stderr=""),
                    MagicMock(returncode=0, stdout="enabled\n", stderr=""),
                    MagicMock(returncode=0, stdout=(
                        "SubState=running\nMainPID=3637174\n"
                        "ActiveEnterTimestamp=Sat 2026-05-30 06:56:35 UTC\n"
                        "NRestarts=0\n"
                        "FragmentPath=/etc/systemd/system/ibgateway.service\n"
                    ), stderr=""),
                    MagicMock(returncode=0, stdout=(
                        "2026-05-30T06:56:35 host systemd[1]: Started IB Gateway.\n"
                    ), stderr=""),
                ]
                with patch("bot.gateway_health.socket.create_connection") as mock_cc:
                    mock_cc.side_effect = ConnectionRefusedError()
                    health = assemble_health(
                        start_script=sh,
                        ibc_config_dir=cfg_dir,
                        log_path=log,
                    )

        # Acceptance assertions matching the user's expected output.
        self.assertTrue(health["systemd_active"])
        self.assertEqual(health["mode"], "paper")
        self.assertEqual(health["expected_port"], PORT_PAPER)
        self.assertFalse(health["tcp_reachable"])
        self.assertTrue(health["login_error_detected"])
        self.assertEqual(health["status"], "service_active_login_error")
        self.assertFalse(health["ready_for_ibkr_trading"])


# ─────────────────────────────────────────────────────────────────────────────
# Group 4 — Endpoint contract
# ─────────────────────────────────────────────────────────────────────────────


class TestGatewayHealthEndpoint(unittest.TestCase):

    def setUp(self):
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
        self.assertIn("/api/gateway/health", urls,
            "/api/gateway/health not registered")

    def test_endpoint_methods_get_only(self):
        for rule in self.app.url_map.iter_rules():
            if str(rule) == "/api/gateway/health":
                methods = rule.methods - {"HEAD", "OPTIONS"}
                self.assertEqual(methods, {"GET"})
                return
        self.fail("rule not found")

    def test_get_returns_200_json(self):
        r = self.client.get("/api/gateway/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content_type, "application/json")

    def test_response_shape(self):
        body = self.client.get("/api/gateway/health").get_json()
        for key in ("as_of_utc", "unit", "systemd", "systemd_active",
                     "mode", "expected_port", "tcp", "tcp_reachable",
                     "log", "login_error_detected", "lifecycle",
                     "status", "ready_for_ibkr_trading"):
            self.assertIn(key, body, f"missing key in response: {key!r}")
        self.assertIn(body["status"], STATUSES)
        self.assertIsInstance(body["ready_for_ibkr_trading"], bool)

    def test_post_returns_405(self):
        self.assertEqual(self.client.post("/api/gateway/health").status_code, 405)

    def test_delete_returns_405(self):
        self.assertEqual(self.client.delete("/api/gateway/health").status_code, 405)

    def test_put_returns_405(self):
        self.assertEqual(self.client.put("/api/gateway/health").status_code, 405)

    def test_patch_returns_405(self):
        self.assertEqual(self.client.patch("/api/gateway/health").status_code, 405)


# ─────────────────────────────────────────────────────────────────────────────
# Group 5 — AST scan: no IB API call, no broker construction, no
# systemctl mutation, no order method
# ─────────────────────────────────────────────────────────────────────────────


class TestNoForbiddenSurface(unittest.TestCase):

    HEALTH_MOD = os.path.join(_REPO, "bot/gateway_health.py")
    DASHBOARD_APP = os.path.join(_REPO, "dashboard/app.py")

    FORBIDDEN_MODULES = {
        "ib_insync", "ibapi",
        "bot.etoro.live_broker", "tools.etoro_live_write",
        "bot.brokers", "bot.brokers.ibkr_broker",
        "bot.risk_authority.preflight",
    }
    FORBIDDEN_NAMES = {"EtoroLiveBroker", "IBKRBroker", "PaperBroker", "IB"}
    FORBIDDEN_IB_METHODS = {
        "connect", "reqCurrentTime", "reqMktData", "reqHistoricalData",
        "placeOrder", "cancelOrder", "modifyOrder", "reqGlobalCancel",
    }
    FORBIDDEN_SYSTEMCTL_VERBS = {
        "start", "stop", "restart", "enable", "disable", "mask",
        "unmask", "daemon-reload", "reload", "reset-failed",
    }

    def _load(self, path):
        with open(path) as f:
            return ast.parse(f.read(), filename=path), f

    def test_health_module_no_forbidden_module_imports(self):
        tree, _ = self._load(self.HEALTH_MOD)
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
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in self.FORBIDDEN_MODULES:
                        offenders.append(f"Import {a.name} @{node.lineno}")
        self.assertEqual(offenders, [],
            f"bot/gateway_health.py has forbidden imports: {offenders}")

    def test_health_module_no_forbidden_names_or_ib_methods(self):
        tree, _ = self._load(self.HEALTH_MOD)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in self.FORBIDDEN_NAMES:
                offenders.append(f"Name {node.id} @{node.lineno}")
            if isinstance(node, ast.Call) and isinstance(node.func,
                                                          ast.Attribute):
                if node.func.attr in self.FORBIDDEN_IB_METHODS:
                    offenders.append(
                        f"call .{node.func.attr} @{node.lineno}")
        self.assertEqual(offenders, [],
            f"bot/gateway_health.py has forbidden refs/calls: {offenders}")

    def test_health_module_no_mutating_systemctl_subcommand(self):
        """All subprocess.run calls must pass argv whose 2nd element
        is in {is-active, is-enabled, show} (for systemctl) or be
        journalctl. NEVER start/stop/restart/etc."""
        tree, _ = self._load(self.HEALTH_MOD)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func,
                                                          ast.Attribute):
                if node.func.attr != "run":
                    continue
                # subprocess.run([...]) — inspect arg 0
                if not node.args:
                    continue
                a0 = node.args[0]
                if isinstance(a0, ast.List):
                    elements = []
                    for el in a0.elts:
                        if isinstance(el, ast.Constant):
                            elements.append(el.value)
                    if elements and elements[0] == "systemctl":
                        sub = elements[1] if len(elements) > 1 else ""
                        if sub in self.FORBIDDEN_SYSTEMCTL_VERBS:
                            offenders.append(
                                f"systemctl {sub} @{node.lineno}")
                # subprocess.run(argv, ...) where argv is a Name —
                # check inbound argv assignments. We rely on the
                # function _run_readonly being the only entrypoint.
        self.assertEqual(offenders, [],
            f"bot/gateway_health.py calls mutating systemctl: {offenders}")

    def test_endpoint_handler_no_forbidden_surface(self):
        tree, _ = self._load(self.DASHBOARD_APP)
        offenders = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "gateway_health"):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and isinstance(sub.func,
                                                                 ast.Attribute):
                        if sub.func.attr in self.FORBIDDEN_IB_METHODS:
                            offenders.append(
                                f"gateway_health: .{sub.func.attr} "
                                f"@{sub.lineno}")
                    if isinstance(sub, ast.Name) and sub.id in self.FORBIDDEN_NAMES:
                        offenders.append(
                            f"gateway_health: Name {sub.id} @{sub.lineno}")
        self.assertEqual(offenders, [],
            f"gateway_health route has forbidden refs: {offenders}")

    def test_no_db_writes_in_helper(self):
        """The helper must NOT write to any SQLite DB."""
        tree, _ = self._load(self.HEALTH_MOD)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func,
                                                          ast.Attribute):
                if node.func.attr in ("commit", "executemany",
                                       "executescript"):
                    offenders.append(
                        f".{node.func.attr} @{node.lineno}")
                if node.func.attr == "execute" and node.args:
                    a = node.args[0]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        s = a.value.strip().upper()
                        for forbidden in ("INSERT", "UPDATE", "DELETE",
                                          "REPLACE", "DROP", "CREATE",
                                          "ALTER", "TRUNCATE"):
                            if s.startswith(forbidden):
                                offenders.append(
                                    f"write SQL @{node.lineno}: {s[:40]!r}")
        self.assertEqual(offenders, [],
            f"bot/gateway_health.py writes to DB: {offenders}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 6 — Existing /api/gateway/state preserved
# ─────────────────────────────────────────────────────────────────────────────


class TestGatewayStatePreserved(unittest.TestCase):

    def setUp(self):
        if "dashboard.app" in sys.modules:
            del sys.modules["dashboard.app"]
        from dashboard.app import app
        self.app = app

    def test_state_route_still_registered_get_only(self):
        for rule in self.app.url_map.iter_rules():
            if str(rule) == "/api/gateway/state":
                methods = rule.methods - {"HEAD", "OPTIONS"}
                self.assertEqual(methods, {"GET"})
                return
        self.fail("/api/gateway/state no longer registered")

    def test_both_endpoints_coexist(self):
        urls = {str(r) for r in self.app.url_map.iter_rules()}
        self.assertIn("/api/gateway/state", urls)
        self.assertIn("/api/gateway/health", urls)


# ─────────────────────────────────────────────────────────────────────────────
# Group 7 — Scanner isolation carry-forward
# ─────────────────────────────────────────────────────────────────────────────


class TestScannerIsolation(unittest.TestCase):

    def test_importing_health_does_not_load_brokers_or_m14(self):
        check = (
            "import sys\n"
            "import bot.gateway_health\n"
            "forbidden = [m for m in (\n"
            "    'ib_insync',\n"
            "    'bot.brokers.ibkr_broker',\n"
            "    'bot.etoro.live_broker',\n"
            "    'tools.etoro_live_write',\n"
            "    'bot.risk_authority.preflight',\n"
            ") if m in sys.modules]\n"
            "print('loaded_forbidden:', forbidden)\n"
            "sys.exit(0 if not forbidden else 1)\n"
        )
        r = subprocess.run([sys.executable, "-c", check],
                            capture_output=True, text=True, cwd=_REPO)
        self.assertEqual(r.returncode, 0,
            f"gateway_health import pulled in forbidden modules. "
            f"stdout={r.stdout!r}")

    def test_scanner_import_does_not_load_health(self):
        check = (
            "import sys\n"
            "import bot.scanner, bot.strategy, bot.risk\n"
            "forbidden = [m for m in (\n"
            "    'bot.gateway_health',\n"
            "    'dashboard.app',\n"
            ") if m in sys.modules]\n"
            "print('loaded_forbidden:', forbidden)\n"
            "sys.exit(0 if not forbidden else 1)\n"
        )
        r = subprocess.run([sys.executable, "-c", check],
                            capture_output=True, text=True, cwd=_REPO)
        self.assertEqual(r.returncode, 0,
            f"scanner-isolation broken. stdout={r.stdout!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 8 — Reference unit-file mirror sanity
# ─────────────────────────────────────────────────────────────────────────────


class TestReferenceMirror(unittest.TestCase):

    MIRROR = os.path.join(_REPO, "infra/systemd/ibgateway.service.documented")

    def test_mirror_present_and_marked_not_deployed(self):
        self.assertTrue(os.path.exists(self.MIRROR))
        with open(self.MIRROR) as f:
            src = f.read()
        self.assertIn("NOT INSTALLED", src,
            "reference mirror must clearly state it is not deployed")
        self.assertIn(".documented", self.MIRROR,
            "reference mirror filename must end in .documented to "
            "prevent it being mistaken for a deployable unit file")

    def test_mirror_reconciles_audit_findings(self):
        """The values captured by the M15.4 audit must appear in the
        mirror so a future drift check is meaningful."""
        with open(self.MIRROR) as f:
            src = f.read()
        for required in (
            "Restart=always",
            "RestartSec=30",
            "StartLimitBurst=3",
            "ExecStart=/opt/ibc/start_ibgateway.sh",
            "Environment=DISPLAY=:99",
            "append:/var/log/ibgateway/ibgateway.log",
        ):
            self.assertIn(required, src,
                f"reference mirror missing audit-reconciled field: {required!r}")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    unittest.main(verbosity=2)
