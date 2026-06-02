"""M14.F — eToro Live-Write Preflight Integration test suite.

Proves the 9 user-specified proof points:
  1. eToro live-write preflight calls the M14.E Risk Authority before
     any live-write attempt.
  2. If Risk Authority returns block, the operator path stops before
     transport/write.
  3. If global automation is disabled, eToro preflight blocks.
  4. If exposure is unknown, eToro preflight blocks.
  5. If PnL is unknown, eToro preflight blocks.
  6. If eToro live is disabled in broker allocation policy, preflight blocks.
  7. The scanner still cannot trigger eToro live writes.
  8. Tests prove no eToro write call occurs during blocked preflight.
  9. Audit records the RiskDecision / block reason.

No live calls, no eToro write endpoint contacted, no order placed.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

_REPO = str(Path(__file__).resolve().parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from bot.broker_allocation import DEFAULT_POLICY, save_policy
from bot.flywheel import init_flywheel_tables
from bot.risk_authority import Authority, PreflightResult, run_risk_preflight
from bot.risk_authority.engine import TradeRequest

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


class _DB:
    """Temp SQLite fixture with flywheel + audit tables ready."""

    def __init__(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        f.close()
        self.path = f.name
        with self.conn() as c:
            init_flywheel_tables(c)

    def conn(self):
        return sqlite3.connect(self.path)

    def cleanup(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _valid_policy(**overrides) -> dict:
    """Build a valid M13.4A policy. Override via dotted keys."""
    policy = {
        "version": 1,
        "global": {
            "auto_trading_enabled": True,
            "max_auto_trading_capital": 100000.0,
            "kill_switch": False,
        },
        "ibkr": {
            "auto_trading_enabled": True,
            "max_auto_trading_capital": 5000.0,
            "max_single_trade_amount": 500.0,
            "max_daily_loss": 1000.0,
            "max_open_positions": 5,
            "kill_switch": False,
        },
        "etoro": {
            "auto_trading_enabled": True,
            "max_auto_trading_capital": 3000.0,
            "max_single_trade_amount": 300.0,
            "max_daily_loss": 500.0,
            "max_open_positions": 3,
            "kill_switch": False,
        },
        "routing": {
            "default_broker": "etoro_paper",
            "route_overrides": {"IBKR": "ibkr_live", "ETORO": "etoro_paper"},
            "allowed_brokers": ["ibkr_paper", "ibkr_live",
                                 "etoro_paper", "etoro_real"],
            "etoro_live_enabled": True,    # ⇐ permissive default for M14.F tests
        },
    }
    for k, v in overrides.items():
        if "." in k:
            top, sub = k.split(".", 1)
            policy[top][sub] = v
        else:
            policy[k] = v
    return policy


def _today_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _seed_fresh_state(conn: sqlite3.Connection, *,
                       scope: str = "etoro_real",
                       pnl_known: bool = True,
                       exposure_known: bool = True,
                       trading_day: Optional[str] = None) -> None:
    """Seed daily_state_per_broker with FRESH state for one scope.
    Defaults `trading_day` to today_utc() so the in-process
    assemble_snapshot (which also uses today_utc) finds the row."""
    from datetime import datetime, timezone
    if trading_day is None:
        trading_day = _today_utc()
    lifecycle = {
        "status": "fresh" if pnl_known else "unknown",
        "exposure_status": ("exposure_fresh" if exposure_known
                            else "exposure_unknown"),
        "exposure_fresh_reads_count": 3 if exposure_known else 0,
    }
    conn.execute(
        "INSERT OR REPLACE INTO daily_state_per_broker "
        "(date, broker_scope, realised_pnl_usd, realised_daily_loss, "
        " daily_pnl_available, daily_loss_block_active, "
        " open_positions, capital_deployed, peak_equity, "
        " drawdown_from_peak, fresh_reads_count, "
        " source, last_ingested_at, lifecycle_json, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trading_day, scope, 0.0, 0.0,
         1 if pnl_known else 0, 0,
         0, 0.0, None, 0.0,
         3 if pnl_known else 0,
         "ingested", datetime.now(timezone.utc).isoformat(),
         json.dumps(lifecycle),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _seed_fresh_all_scopes(conn: sqlite3.Connection,
                            trading_day: Optional[str] = None) -> None:
    if trading_day is None:
        trading_day = _today_utc()
    for s in ("ibkr_live", "ibkr_paper", "etoro_real", "etoro_paper"):
        _seed_fresh_state(conn, scope=s, trading_day=trading_day)


def _request(**overrides) -> TradeRequest:
    base = {"symbol": "AAPL", "amount_usd": 50.0, "side": "long", "leverage": 1}
    base.update(overrides)
    return TradeRequest(**base)


# ─────────────────────────────────────────────────────────────────────────────
# Group 1 — Preflight pure orchestration
# ─────────────────────────────────────────────────────────────────────────────


class _InProcessEnv:
    """Mixin: set ETORO_LIVE_ENABLED=true for the duration of tests that
    exercise the in-process preflight directly. This is needed because
    the engine's gate #8 (etoro_live_env_disabled) reads
    ETORO_LIVE_ENABLED from os.environ; if it's missing, that gate
    fires first and pre-empts the gates we're trying to test
    (exposure/PnL unknown, kill switch, etc.).

    Subprocess CLI tests (TestNoEtoroWriteOnBlock, TestAllowPreservesM13_5)
    deliberately do NOT set this env var — those tests prove that even
    when ETORO_LIVE_ENABLED is absent, the Risk Authority blocks
    correctly (the user's hard rule: 'Risk Authority should be testable
    even when ETORO_LIVE_ENABLED is false/absent')."""

    def _set_env_live(self):
        self._prev_etoro_live = os.environ.get("ETORO_LIVE_ENABLED")
        os.environ["ETORO_LIVE_ENABLED"] = "true"

    def _restore_env_live(self):
        if getattr(self, "_prev_etoro_live", None) is None:
            os.environ.pop("ETORO_LIVE_ENABLED", None)
        else:
            os.environ["ETORO_LIVE_ENABLED"] = self._prev_etoro_live


# ─────────────────────────────────────────────────────────────────────────────
# Group 1 — Preflight pure orchestration
# ─────────────────────────────────────────────────────────────────────────────


class TestPreflightOrchestration(unittest.TestCase, _InProcessEnv):

    def setUp(self):
        self._set_env_live()
        self.fx = _DB()
        with self.fx.conn() as c:
            save_policy(c, _valid_policy())
            _seed_fresh_all_scopes(c)

    def tearDown(self):
        self.fx.cleanup()
        self._restore_env_live()

    def test_returns_preflight_result_on_allow(self):
        with self.fx.conn() as c:
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.ONE_SHOT_MANUAL,
            )
        self.assertIsInstance(r, PreflightResult)
        self.assertTrue(r.allowed)
        self.assertEqual(r.decision.result, "allow")

    def test_returns_preflight_result_on_block(self):
        with self.fx.conn() as c:
            save_policy(c, _valid_policy(**{"global.kill_switch": True}))
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.ONE_SHOT_MANUAL,
            )
        self.assertFalse(r.allowed)
        self.assertIn("global_kill", r.reason_codes)
        self.assertIn("global_kill", r.recovery_paths)

    def test_calls_decide_and_audit_exactly_once(self):
        with self.fx.conn() as c:
            n_dec_before = c.execute(
                "SELECT COUNT(*) FROM risk_decisions").fetchone()[0]
            n_snap_before = c.execute(
                "SELECT COUNT(*) FROM risk_snapshots").fetchone()[0]
            run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.ONE_SHOT_MANUAL,
            )
            n_dec_after = c.execute(
                "SELECT COUNT(*) FROM risk_decisions").fetchone()[0]
            n_snap_after = c.execute(
                "SELECT COUNT(*) FROM risk_snapshots").fetchone()[0]
        self.assertEqual(n_dec_after - n_dec_before, 1)
        self.assertEqual(n_snap_after - n_snap_before, 1)

    def test_rejects_non_Authority_type(self):
        with self.fx.conn() as c:
            with self.assertRaises(TypeError):
                run_risk_preflight(
                    c, broker_scope="etoro_real", request=_request(),
                    current_authority="ONE_SHOT_MANUAL",   # string, not Authority
                )

    def test_rejects_manual_reset_audit_source(self):
        """manual_reset is reserved for M14.G; M14.F must not accept it."""
        with self.fx.conn() as c:
            with self.assertRaises(ValueError):
                run_risk_preflight(
                    c, broker_scope="etoro_real", request=_request(),
                    current_authority=Authority.ONE_SHOT_MANUAL,
                    audit_source="manual_reset",
                )


# ─────────────────────────────────────────────────────────────────────────────
# Group 2 + 3 — Risk Authority runs BEFORE transport (proofs 1 & 2)
# ─────────────────────────────────────────────────────────────────────────────


class TestRiskBeforeTransport(unittest.TestCase):
    """The CLI must call Risk Authority preflight BEFORE any
    EtoroLiveBroker construction, credential loading, or transport
    contact. Verified by AST analysis of the CLI source."""

    def _find_call_linenos(self, source: str):
        """Return {name: lineno} mapping for every Call site we care about.
        Excludes ast.Name occurrences in non-Call contexts (e.g. dict
        keys in the runtime-imports alias mapping)."""
        tree = ast.parse(source)
        # Locate cmd_oneshot first.
        oneshot = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "cmd_oneshot":
                oneshot = node
                break
        self.assertIsNotNone(oneshot, "cmd_oneshot not found")
        found = {}
        targets = {
            "run_risk_preflight", "_read_keys", "EtoroLiveBroker",
        }
        for node in ast.walk(oneshot):
            if isinstance(node, ast.Call):
                fn = node.func
                # Direct name: f(...)
                if isinstance(fn, ast.Name) and fn.id in targets:
                    found.setdefault(fn.id, node.lineno)
        # Also locate the env-flag *message* (it's a Constant string).
        for node in ast.walk(oneshot):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and "ETORO_LIVE_ENABLED is not" in node.value):
                found.setdefault("env_flag_msg", node.lineno)
                break
        # Locate nonce_store.issue(...) call.
        for node in ast.walk(oneshot):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "issue"):
                found.setdefault("nonce_issue", node.lineno)
                break
        return found

    def _read_oneshot_source(self) -> str:
        """Return just the cmd_oneshot function body."""
        path = os.path.join(_REPO, "tools/etoro_live_write.py")
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "cmd_oneshot":
                # Return the body as a sequential list of (line, source) tuples.
                with open(path) as f:
                    lines = f.read().splitlines()
                return "\n".join(lines[node.lineno - 1: node.end_lineno])
        raise RuntimeError("cmd_oneshot not found")

    def test_preflight_call_appears_before_read_keys(self):
        with open(os.path.join(_REPO, "tools/etoro_live_write.py")) as f:
            src = f.read()
        pos = self._find_call_linenos(src)
        self.assertIn("run_risk_preflight", pos,
            "cmd_oneshot does NOT call run_risk_preflight")
        self.assertIn("_read_keys", pos,
            "cmd_oneshot does NOT call _read_keys")
        self.assertLess(pos["run_risk_preflight"], pos["_read_keys"],
            f"Risk Authority preflight (line {pos['run_risk_preflight']}) "
            f"MUST run before _read_keys() (line {pos['_read_keys']})")

    def test_preflight_call_appears_before_env_flag_check(self):
        with open(os.path.join(_REPO, "tools/etoro_live_write.py")) as f:
            src = f.read()
        pos = self._find_call_linenos(src)
        self.assertIn("env_flag_msg", pos)
        self.assertLess(pos["run_risk_preflight"], pos["env_flag_msg"],
            f"preflight (line {pos['run_risk_preflight']}) must run "
            f"before env-flag check (line {pos['env_flag_msg']})")

    def test_preflight_call_appears_before_broker_construction(self):
        with open(os.path.join(_REPO, "tools/etoro_live_write.py")) as f:
            src = f.read()
        pos = self._find_call_linenos(src)
        self.assertIn("EtoroLiveBroker", pos,
            "cmd_oneshot does NOT call EtoroLiveBroker")
        self.assertLess(pos["run_risk_preflight"], pos["EtoroLiveBroker"],
            "preflight must run before EtoroLiveBroker(...)")

    def test_preflight_call_appears_before_nonce_issuance(self):
        with open(os.path.join(_REPO, "tools/etoro_live_write.py")) as f:
            src = f.read()
        pos = self._find_call_linenos(src)
        self.assertIn("nonce_issue", pos,
            "cmd_oneshot does NOT call nonce_store.issue(...)")
        self.assertLess(pos["run_risk_preflight"], pos["nonce_issue"],
            "preflight must run before nonce issuance")

    def test_block_returns_exit_4_in_source(self):
        """The CLI must return 4 specifically when preflight blocks."""
        src = self._read_oneshot_source()
        # The 'return 4' must appear inside a 'not preflight.allowed' branch.
        self.assertIn("if not preflight.allowed", src)
        # Find the branch and confirm 'return 4' follows within 30 lines.
        idx = src.find("if not preflight.allowed")
        snippet = src[idx: idx + 1200]
        self.assertIn("return 4", snippet)


# ─────────────────────────────────────────────────────────────────────────────
# Group 4 — global_auto_disabled blocks (proof 3)
# ─────────────────────────────────────────────────────────────────────────────


class TestGlobalAutoDisabledBlocks(unittest.TestCase, _InProcessEnv):

    def setUp(self):
        self._set_env_live()
        self.fx = _DB()
        with self.fx.conn() as c:
            _seed_fresh_all_scopes(c)

    def tearDown(self):
        self.fx.cleanup()
        self._restore_env_live()

    def test_global_auto_disabled_blocks_preflight(self):
        with self.fx.conn() as c:
            save_policy(c, _valid_policy(
                **{"global.auto_trading_enabled": False}))
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.ONE_SHOT_MANUAL,
            )
        self.assertFalse(r.allowed)
        self.assertIn("global_auto_disabled", r.reason_codes)

    def test_global_kill_switch_blocks_preflight(self):
        with self.fx.conn() as c:
            save_policy(c, _valid_policy(**{"global.kill_switch": True}))
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.ONE_SHOT_MANUAL,
            )
        self.assertFalse(r.allowed)
        self.assertIn("global_kill", r.reason_codes)


# ─────────────────────────────────────────────────────────────────────────────
# Group 5 — Unknown exposure blocks (proof 4)
# ─────────────────────────────────────────────────────────────────────────────


class TestUnknownExposureBlocks(unittest.TestCase, _InProcessEnv):

    def setUp(self):
        self._set_env_live()
        self.fx = _DB()
        with self.fx.conn() as c:
            save_policy(c, _valid_policy())

    def tearDown(self):
        self.fx.cleanup()
        self._restore_env_live()

    def test_unknown_exposure_on_etoro_real_blocks(self):
        with self.fx.conn() as c:
            _seed_fresh_state(c, scope="ibkr_live")
            _seed_fresh_state(c, scope="ibkr_paper")
            _seed_fresh_state(c, scope="etoro_paper")
            _seed_fresh_state(c, scope="etoro_real",
                               exposure_known=False)
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.ONE_SHOT_MANUAL,
            )
        self.assertFalse(r.allowed)
        # Either scope-level exposure_unknown or combined-level fires.
        self.assertTrue(
            "exposure_unknown" in r.reason_codes
            or "combined_exposure_unknown" in r.reason_codes,
            f"expected exposure-unknown reason, got {r.reason_codes}"
        )

    def test_any_scope_unknown_blocks_combined(self):
        """Hard rule from M14.E correction #4 carried through M14.F."""
        with self.fx.conn() as c:
            _seed_fresh_state(c, scope="ibkr_live")
            _seed_fresh_state(c, scope="ibkr_paper",
                               exposure_known=False)  # ⇐ unknown
            _seed_fresh_state(c, scope="etoro_paper")
            _seed_fresh_state(c, scope="etoro_real")
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.ONE_SHOT_MANUAL,
            )
        self.assertFalse(r.allowed)

    def test_known_zero_exposure_does_not_block(self):
        """Known-zero must be distinguished from unknown-zero."""
        with self.fx.conn() as c:
            # All four scopes: exposure_known=True, capital=0, positions=0.
            _seed_fresh_all_scopes(c)
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.ONE_SHOT_MANUAL,
            )
        self.assertTrue(r.allowed, f"known-zero should allow, got "
                         f"reasons={r.reason_codes}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 6 — Unknown PnL blocks (proof 5)
# ─────────────────────────────────────────────────────────────────────────────


class TestUnknownPnLBlocks(unittest.TestCase, _InProcessEnv):

    def setUp(self):
        self._set_env_live()
        self.fx = _DB()
        with self.fx.conn() as c:
            save_policy(c, _valid_policy())

    def tearDown(self):
        self.fx.cleanup()
        self._restore_env_live()

    def test_unknown_pnl_on_etoro_real_blocks(self):
        with self.fx.conn() as c:
            _seed_fresh_state(c, scope="ibkr_live")
            _seed_fresh_state(c, scope="ibkr_paper")
            _seed_fresh_state(c, scope="etoro_paper")
            _seed_fresh_state(c, scope="etoro_real",
                               pnl_known=False)
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.ONE_SHOT_MANUAL,
            )
        self.assertFalse(r.allowed)
        self.assertTrue(
            "daily_pnl_unknown" in r.reason_codes
            or "global_daily_loss_unknown" in r.reason_codes,
            f"expected pnl-unknown reason, got {r.reason_codes}"
        )

    def test_known_zero_pnl_does_not_block(self):
        with self.fx.conn() as c:
            _seed_fresh_all_scopes(c)
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.ONE_SHOT_MANUAL,
            )
        # Known-zero PnL: realised_daily_loss=0, daily_pnl_available=1.
        # MUST NOT trigger any 'unknown' reason.
        self.assertNotIn("daily_pnl_unknown", r.reason_codes)
        self.assertNotIn("global_daily_loss_unknown", r.reason_codes)


# ─────────────────────────────────────────────────────────────────────────────
# Group 7 — eToro live disabled in M13.4A policy blocks (proof 6)
# ─────────────────────────────────────────────────────────────────────────────


class TestEtoroLiveDisabledBlocks(unittest.TestCase, _InProcessEnv):

    def setUp(self):
        self._set_env_live()
        self.fx = _DB()
        with self.fx.conn() as c:
            _seed_fresh_all_scopes(c)

    def tearDown(self):
        self.fx.cleanup()
        self._restore_env_live()

    def test_etoro_live_disabled_in_routing_blocks(self):
        with self.fx.conn() as c:
            save_policy(c, _valid_policy(
                **{"routing.etoro_live_enabled": False}))
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.ONE_SHOT_MANUAL,
            )
        self.assertFalse(r.allowed)
        self.assertIn("etoro_live_flag_disabled", r.reason_codes)

    def test_etoro_not_in_allowed_brokers_blocks(self):
        with self.fx.conn() as c:
            p = _valid_policy()
            p["routing"]["allowed_brokers"] = ["ibkr_paper", "ibkr_live",
                                                "etoro_paper"]  # no etoro_real
            save_policy(c, p)
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.ONE_SHOT_MANUAL,
            )
        self.assertFalse(r.allowed)
        self.assertIn("broker_not_allowed", r.reason_codes)


# ─────────────────────────────────────────────────────────────────────────────
# Group 8 — Scanner still cannot trigger eToro live writes (proof 7)
# ─────────────────────────────────────────────────────────────────────────────


class TestScannerIsolation(unittest.TestCase):
    """The standing M13.5/M14 invariant: importing the scanner must
    not load the live-write CLI or the M14.F preflight bridge."""

    def test_scanner_import_does_not_load_etoro_live_write(self):
        check = (
            "import sys\n"
            "import bot.scanner, bot.strategy, bot.risk, bot.brokers\n"
            "forbidden = [m for m in (\n"
            "    'tools.etoro_live_write',\n"
            "    'bot.risk_authority.preflight',\n"
            "    'bot.etoro.live_broker',\n"
            ") if m in sys.modules]\n"
            "print('loaded:', forbidden)\n"
            "sys.exit(0 if not forbidden else 1)\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", check],
            capture_output=True, text=True, cwd=_REPO,
        )
        self.assertEqual(r.returncode, 0,
            f"scanner-isolation violated. "
            f"stdout={r.stdout!r} stderr={r.stderr!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 9 — Tests prove no eToro write call occurs during blocked preflight
#           (proof 8)
# ─────────────────────────────────────────────────────────────────────────────


class TestNoEtoroWriteOnBlock(unittest.TestCase):
    """End-to-end subprocess: run the CLI with a policy that forces
    a Risk Authority block; assert exit code 4 AND no eToro write
    happened (no live_post audit-log line)."""

    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def _run_cli_blocked(self, env_extra=None):
        """Set up a guaranteed-block policy + run the CLI subprocess."""
        with self.fx.conn() as c:
            # Block via global kill — earliest gate that fires.
            save_policy(c, _valid_policy(**{"global.kill_switch": True}))
            _seed_fresh_all_scopes(c)
            audit_path = os.path.join(_REPO, "data",
                                       "etoro_live_audit_test.jsonl")
        # Ensure a clean audit log location for this test run.
        env = dict(os.environ)
        env["SIGNALS_DB_PATH"] = self.fx.path
        env["ETORO_LIVE_AUDIT_LOG"] = audit_path
        # Do NOT set ETORO_LIVE_ENABLED — we want to prove the Risk
        # Authority blocks even when env_live is false/absent.
        env.pop("ETORO_LIVE_ENABLED", None)
        if env_extra:
            env.update(env_extra)
        # Remove any prior audit log so we can assert "no new lines".
        try:
            os.unlink(audit_path)
        except OSError:
            pass

        r = subprocess.run(
            [sys.executable, "tools/etoro_live_write.py",
             "oneshot",
             "--instrument-id", "1000",
             "--amount", "50.0",
             "--symbol", "AAPL",
             "--close-plan", "manual",
             "--authority", "ONE_SHOT_MANUAL"],
            capture_output=True, text=True, env=env, cwd=_REPO,
            timeout=30,
        )
        return r, audit_path

    def test_cli_returns_exit_4_on_risk_block(self):
        r, _audit = self._run_cli_blocked()
        self.assertEqual(r.returncode, 4,
            f"expected exit 4 (Risk Authority block). "
            f"stdout={r.stdout!r} stderr={r.stderr!r}")

    def test_cli_block_prints_decision_id_and_reasons(self):
        r, _audit = self._run_cli_blocked()
        combined = (r.stdout or "") + (r.stderr or "")
        self.assertIn("Risk Authority blocked", combined)
        self.assertIn("decision_id", combined)
        self.assertIn("reason_codes", combined)
        self.assertIn("global_kill", combined)

    def test_no_live_post_audit_line_on_block(self):
        r, audit_path = self._run_cli_blocked()
        # The audit path may or may not exist; if it does, it must
        # contain ZERO 'live_post' lines (M13.5.B audit vocabulary).
        live_post_count = 0
        if os.path.exists(audit_path):
            with open(audit_path) as f:
                for ln in f:
                    if "live_post" in ln:
                        live_post_count += 1
        self.assertEqual(live_post_count, 0,
            f"a live_post audit line was written despite Risk Authority "
            f"block. exit_code={r.returncode}")

    def test_block_does_not_touch_etoro_live_audit_log_at_all(self):
        """Even non-live_post lines should not appear, since we exit
        before any broker construction (which is what creates the
        AuditLogger). The audit log file should not exist at all."""
        r, audit_path = self._run_cli_blocked()
        self.assertFalse(
            os.path.exists(audit_path),
            f"etoro live audit log was created despite Risk Authority "
            f"block — implies broker construction happened. "
            f"exit_code={r.returncode}"
        )

    def test_block_writes_audit_row_to_risk_decisions(self):
        """Proof 9: even on block, the RiskDecision IS written to the
        audit table — this is the explainable trail."""
        with self.fx.conn() as c:
            n_dec_before = c.execute(
                "SELECT COUNT(*) FROM risk_decisions").fetchone()[0]
        self._run_cli_blocked()
        with self.fx.conn() as c:
            n_dec_after = c.execute(
                "SELECT COUNT(*) FROM risk_decisions").fetchone()[0]
            rows = c.execute(
                "SELECT broker_scope, requested_action, result, source, "
                "       actor, reason_codes "
                "FROM risk_decisions ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(n_dec_after - n_dec_before, 1)
        self.assertEqual(rows[0], "etoro_real")
        self.assertEqual(rows[1], "trade_open")
        self.assertEqual(rows[2], "block")
        self.assertEqual(rows[3], "manual")
        self.assertEqual(rows[4], "operator")
        parsed = json.loads(rows[5])
        self.assertIn("global_kill", parsed)


# ─────────────────────────────────────────────────────────────────────────────
# Group 10 — AST: preflight.py contains no write/order surface (proof 8 cont.)
# ─────────────────────────────────────────────────────────────────────────────


class TestPreflightNoWriteSurface(unittest.TestCase):
    """The preflight module itself MUST NOT contain HTTP write verbs,
    order methods, or live-broker imports. Direct grep + AST scan."""

    PATH = os.path.join(_REPO, "bot/risk_authority/preflight.py")

    FORBIDDEN_MODULES = {
        "bot.etoro.live_broker",
        "tools.etoro_live_write",
        "bot.brokers",
        "bot.risk_authority.ingest",
        "bot.risk_authority.ingest_etoro",
        "bot.risk_authority.ingest_ibkr",
        "bot.risk_authority.ingest_exposure",
        "bot.risk_authority.ingest_etoro_exposure",
        "bot.risk_authority.ingest_ibkr_exposure",
    }
    FORBIDDEN_NAMES = {"EtoroLiveBroker", "IBKRBroker", "PaperBroker"}
    FORBIDDEN_HTTP = {"POST", "DELETE", "PUT", "PATCH"}
    FORBIDDEN_HTTP_METHODS = {"post", "delete", "put", "patch"}
    FORBIDDEN_ORDER = {"placeOrder", "cancelOrder", "modifyOrder",
                       "reqGlobalCancel"}

    def _scan(self, path):
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
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
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in self.FORBIDDEN_MODULES:
                        offenders.append(f"Import {a.name} @{node.lineno}")
            if isinstance(node, ast.Name) and node.id in self.FORBIDDEN_NAMES:
                offenders.append(f"Name {node.id} @{node.lineno}")
            if isinstance(node, ast.Call):
                for a in node.args:
                    if (isinstance(a, ast.Constant)
                            and isinstance(a.value, str)
                            and a.value.upper() in self.FORBIDDEN_HTTP):
                        offenders.append(f"call arg {a.value!r} @{a.lineno}")
                for kw in node.keywords:
                    if (kw.arg == "method"
                            and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)
                            and kw.value.value.upper() in self.FORBIDDEN_HTTP):
                        offenders.append(
                            f"method={kw.value.value} @{kw.value.lineno}")
                fn = node.func
                if isinstance(fn, ast.Attribute):
                    if fn.attr in self.FORBIDDEN_HTTP_METHODS:
                        offenders.append(f".{fn.attr} @{fn.lineno}")
                    if fn.attr in self.FORBIDDEN_ORDER:
                        offenders.append(f".{fn.attr} (order) @{fn.lineno}")
        return offenders

    def test_no_forbidden_in_preflight(self):
        offenders = self._scan(self.PATH)
        self.assertEqual(offenders, [],
            f"preflight.py has forbidden references: {offenders}")

    def test_preflight_makes_no_direct_db_writes(self):
        """The only DB-writing surface in M14.F preflight is the
        imported `decide_and_audit`. The module itself must not contain
        INSERT/UPDATE/conn.execute outside that import. AST-based to
        ignore comments and docstrings (which legitimately reference
        the words descriptively)."""
        with open(self.PATH) as f:
            tree = ast.parse(f.read(), filename=self.PATH)
        offenders = []
        for node in ast.walk(tree):
            # Direct .execute(...) / .commit() / .cursor() calls.
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute):
                    if fn.attr in ("execute", "commit", "cursor",
                                   "executemany", "executescript"):
                        offenders.append(f".{fn.attr} @{node.lineno}")
            # String literal containing 'INSERT '/'UPDATE ' as SQL
            # (only meaningful if it's a Call arg of execute/etc; we
            # already catch those above). Plain Constants in docstrings
            # are fine.
        self.assertEqual(offenders, [],
            f"preflight.py contains direct DB writes: {offenders}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 11 — Allow path preserves M13.5 invariants
# ─────────────────────────────────────────────────────────────────────────────


class TestAllowPreservesM13_5(unittest.TestCase):
    """When Risk Authority allows, the existing M13.5 envelope (env
    flag, nonce, schema, operator confirmation) must still run. We
    verify by running the CLI with allow-shaped state (ETORO_LIVE_ENABLED
    set so the engine's gate #8 passes) BUT no real eToro credentials
    in env: M13.5's _read_keys / env-flag layer still blocks downstream
    of the Risk Authority allow. This proves M14.F does not weaken any
    M13.5 gate.

    The two layers overlap intentionally: gate #8 (etoro_live_env_disabled)
    is the engine's mirror; the M13.5 envelope's env-flag check is the
    second, downstream verification. M14.F leaves both intact."""

    def setUp(self):
        self.fx = _DB()
        with self.fx.conn() as c:
            save_policy(c, _valid_policy())
            _seed_fresh_all_scopes(c)

    def tearDown(self):
        self.fx.cleanup()

    def test_allow_then_m13_5_envelope_still_runs(self):
        """Risk Authority allows; subsequent M13.5 envelope still has a
        chance to block. We force the M13.5 envelope to error out by
        omitting eToro credentials but setting ETORO_LIVE_ENABLED=true.
        The CLI must exit with a non-zero code OTHER than 4 (which is
        Risk Authority's). Any of {1, 2, 3} is acceptable as proof the
        M13.5 envelope ran."""
        env = dict(os.environ)
        env["SIGNALS_DB_PATH"] = self.fx.path
        env["ETORO_LIVE_ENABLED"] = "true"   # ⇐ pass engine gate #8
        # Remove eToro credentials so M13.5 _read_keys fails downstream.
        env.pop("ETORO_REAL_API_KEY", None)
        env.pop("ETORO_REAL_USER_KEY", None)
        env.pop("ETORO_API_KEY", None)
        env.pop("ETORO_USER_KEY", None)
        env["ETORO_LIVE_AUDIT_LOG"] = os.path.join(
            _REPO, "data", "etoro_live_audit_test_allow.jsonl")
        try:
            os.unlink(env["ETORO_LIVE_AUDIT_LOG"])
        except OSError:
            pass
        r = subprocess.run(
            [sys.executable, "tools/etoro_live_write.py",
             "oneshot",
             "--instrument-id", "1000",
             "--amount", "50.0",
             "--symbol", "AAPL",
             "--close-plan", "manual",
             "--authority", "ONE_SHOT_MANUAL",
             "--market-open"],
            capture_output=True, text=True, env=env, cwd=_REPO,
            timeout=30,
        )
        # Risk Authority allowed (exit != 4); M13.5 envelope ran and
        # produced its own non-zero exit (typically 2 for missing
        # credentials / env flag).
        self.assertNotEqual(r.returncode, 0,
            f"M13.5 envelope failed to enforce its own gates. "
            f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertNotEqual(r.returncode, 4,
            f"Risk Authority unexpectedly blocked when it should "
            f"have allowed. stdout={r.stdout!r} stderr={r.stderr!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 12 — Argparse hygiene
# ─────────────────────────────────────────────────────────────────────────────


class TestArgparseHygiene(unittest.TestCase):

    def _run(self, *flags):
        env = dict(os.environ)
        env.pop("ETORO_LIVE_ENABLED", None)
        return subprocess.run(
            [sys.executable, "tools/etoro_live_write.py", *flags],
            capture_output=True, text=True, env=env, cwd=_REPO,
            timeout=30,
        )

    def test_authority_argument_accepts_all_five_levels(self):
        for level in ("OFF", "SIGNAL_ONLY", "PAPER_ONLY",
                      "ONE_SHOT_MANUAL", "AUTO_ALLOWED"):
            r = self._run("oneshot", "--help")
            # The --help text must enumerate the level.
            self.assertIn(level, r.stdout)

    def test_authority_rejects_invalid_level(self):
        r = self._run("oneshot",
                      "--instrument-id", "1000",
                      "--amount", "50.0",
                      "--symbol", "AAPL",
                      "--close-plan", "manual",
                      "--authority", "GOD_MODE")
        self.assertEqual(r.returncode, 2)   # argparse exit
        self.assertIn("invalid choice", r.stderr)

    def test_base_url_flag_still_rejected(self):
        """M13.5.B carry-forward: --base-url must not exist."""
        r = self._run("oneshot",
                      "--instrument-id", "1000",
                      "--amount", "50.0",
                      "--symbol", "AAPL",
                      "--close-plan", "manual",
                      "--base-url", "https://evil.example")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unrecognized arguments", r.stderr)

    def test_help_does_not_mention_forbidden_flags(self):
        r = self._run("oneshot", "--help")
        for forbidden in ("--base-url", "--override-realised",
                          "--override-pnl", "--assume-yes"):
            self.assertNotIn(forbidden, r.stdout,
                f"--help must not mention forbidden flag {forbidden!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 13 — AUTO_ALLOWED does not bypass Risk Authority
# ─────────────────────────────────────────────────────────────────────────────


class TestAutoAllowedDoesNotBypass(unittest.TestCase, _InProcessEnv):
    """The user's spec: even if --authority AUTO_ALLOWED is passed,
    it must NOT bypass Risk Authority. The engine still consults
    every gate."""

    def setUp(self):
        self._set_env_live()
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()
        self._restore_env_live()

    def test_auto_allowed_still_blocked_by_kill_switch(self):
        with self.fx.conn() as c:
            save_policy(c, _valid_policy(**{"global.kill_switch": True}))
            _seed_fresh_all_scopes(c)
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.AUTO_ALLOWED,
            )
        self.assertFalse(r.allowed,
            "AUTO_ALLOWED must not bypass global_kill")
        self.assertIn("global_kill", r.reason_codes)

    def test_auto_allowed_still_blocked_by_unknown_pnl(self):
        with self.fx.conn() as c:
            save_policy(c, _valid_policy())
            _seed_fresh_state(c, scope="ibkr_live")
            _seed_fresh_state(c, scope="ibkr_paper")
            _seed_fresh_state(c, scope="etoro_paper")
            _seed_fresh_state(c, scope="etoro_real", pnl_known=False)
            r = run_risk_preflight(
                c, broker_scope="etoro_real", request=_request(),
                current_authority=Authority.AUTO_ALLOWED,
            )
        self.assertFalse(r.allowed,
            "AUTO_ALLOWED must not bypass unknown-pnl fail-closed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
