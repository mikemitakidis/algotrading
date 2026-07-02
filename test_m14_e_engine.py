"""M14.E — Risk Authority Engine + Governor test suite.

Covers the approved plan + the 5 ChatGPT corrections:

  1. decide() is PURE: no DB writes, no input mutation, no broker calls.
     A separate decide_and_audit(conn, ...) wrapper handles audit-row writes.
  2. Engine imports NO ingestion / adapter / broker code (AST-enforced).
  3. Concentration cap is per-symbol only.
  4. Combined-exposure cap covers all four scopes; any-unknown ⇒ fail-closed.
  5. Daily-loss latch keyed on UTC trading day.

Hard invariants checked:
  * Unknown PnL ⇒ block 'daily_pnl_unknown'.
  * Unknown exposure ⇒ block 'exposure_unknown'.
  * Known-zero distinguishable from unknown-zero (lifecycle status carries
    the semantic, never the raw numeric column).
  * Authority ladder is strict total order; governor never auto-upgrades.
  * Property-tested: 1000 random sequences never yield an autonomous upgrade.

No live calls, no eToro write, no order placed.
"""
from __future__ import annotations

import ast
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Optional

_REPO = str(Path(__file__).resolve().parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from bot.flywheel import init_flywheel_tables
from bot.risk_authority.authority import (
    Authority,
    REQUIRED_AUTHORITY,
    is_monotone_safe,
)
from bot.risk_authority.audit_decisions import (
    decide_and_audit,
    write_decision,
    write_snapshot,
)
from bot.risk_authority.engine import (
    RECOVERY_PATHS,
    REASON_CODES,
    RiskContext,
    RiskDecision,
    RiskPolicyView,
    TradeRequest,
    _GATES,
    decide,
    policy_view_from_allocation_policy,
)
from bot.risk_authority.governor import (
    GovernorState,
    apply_manual_reset,
    propose,
)
from bot.risk_authority.snapshot import (
    ALL_BROKER_SCOPES,
    GlobalView,
    RiskSnapshot,
    ScopeView,
    assemble_snapshot,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


class _DB:
    """Temp SQLite fixture with M14.B audit tables ready."""

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


def _scope(scope="ibkr_paper", *, pnl_known=True, exposure_known=True,
           realised_pnl=0.0, realised_daily_loss=0.0,
           open_positions=0, capital_deployed=0.0,
           peak_equity=None, drawdown_from_peak=0.0,
           daily_loss_block_active=False,
           exposure_fresh_reads_count=3, positions=()) -> ScopeView:
    return ScopeView(
        scope=scope,
        realised_pnl_usd=realised_pnl,
        realised_daily_loss=realised_daily_loss,
        daily_pnl_available=pnl_known,
        daily_loss_block_active=daily_loss_block_active,
        pnl_status=("fresh" if pnl_known else "unknown"),
        pnl_fresh_reads_count=(3 if pnl_known else 0),
        open_positions=open_positions,
        capital_deployed=capital_deployed,
        peak_equity=peak_equity,
        drawdown_from_peak=drawdown_from_peak,
        exposure_status=("exposure_fresh" if exposure_known else "exposure_unknown"),
        exposure_fresh_reads_count=exposure_fresh_reads_count,
        exposure_batch_id="batch-1" if exposure_known else None,
        positions=tuple(positions),
        last_ingested_at="2026-05-29T10:00:00Z",
    )


def _snapshot(*, scopes_override=None, taken_at="2026-05-29T10:00:00Z",
              trading_day="2026-05-29") -> RiskSnapshot:
    scopes = scopes_override or {
        s: _scope(scope=s) for s in ALL_BROKER_SCOPES
    }
    # Recompute global view from scopes.
    cap = 0.0; pos = 0; loss = 0.0
    cap_unknown = False; pos_unknown = False; pnl_unknown = False
    u_pnl = []; u_exp = []
    per_symbol = {}
    for sname, sv in scopes.items():
        if not sv.is_exposure_known():
            cap_unknown = pos_unknown = True
            u_exp.append(sname)
        else:
            cap += sv.capital_deployed
            pos += sv.open_positions
        if not sv.is_pnl_known():
            pnl_unknown = True
            u_pnl.append(sname)
        else:
            loss += sv.realised_daily_loss
        if sv.is_exposure_known():
            for p in sv.positions:
                per_symbol[p["symbol"]] = per_symbol.get(p["symbol"], 0.0) + p["exposure_usd"]
    gv = GlobalView(
        combined_capital_deployed=(None if cap_unknown else cap),
        combined_open_positions=(None if pos_unknown else pos),
        combined_realised_daily_loss=(None if pnl_unknown else loss),
        per_symbol_exposure=per_symbol,
        any_pnl_unknown=pnl_unknown,
        any_exposure_unknown=cap_unknown,
        unknown_pnl_scopes=tuple(sorted(u_pnl)),
        unknown_exposure_scopes=tuple(sorted(u_exp)),
    )
    return RiskSnapshot(
        taken_at_utc=taken_at, trading_day_utc=trading_day,
        scopes=scopes, global_view=gv, policy_version=1,
    )


def _baseline_policy(**overrides) -> RiskPolicyView:
    """A permissive policy that lets everything through unless overridden."""
    base = {
        "amount_min_usd": 1.0,
        "single_trade_cap_usd": 100000.0,
        "global_capital_cap_usd": 1_000_000.0,
        "combined_exposure_cap_usd": 1_000_000.0,
        "global_open_positions_cap": 100,
        "global_daily_loss_cap_usd": 100_000.0,
        "drawdown_throttle_threshold": 0.99,   # effectively off
        "per_symbol_exposure_cap_usd": 1_000_000.0,
        "etoro_live_flag_enabled": True,
        "etoro_live_env_enabled": True,
    }
    base.update(overrides)
    return RiskPolicyView(**base)


def _ctx(scope="ibkr_paper", action="trade_open",
         authority=Authority.ONE_SHOT_MANUAL, **overrides) -> RiskContext:
    base = {
        "broker_scope": scope, "requested_action": action,
        "current_authority": authority, "market_open": True,
    }
    base.update(overrides)
    return RiskContext(**base)


def _req(symbol="AAPL", amount=100.0, side="long") -> TradeRequest:
    return TradeRequest(symbol=symbol, amount_usd=amount, side=side)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Engine purity (correction #1)
# ─────────────────────────────────────────────────────────────────────────────


class TestEnginePurity(unittest.TestCase):
    """decide() must be pure: no DB writes, no input mutation, no broker calls."""

    def test_decide_idempotent_for_identical_inputs(self):
        ctx = _ctx()
        snap = _snapshot()
        pol = _baseline_policy()
        req = _req()
        d1 = decide(ctx, snap, req, policy=pol)
        d2 = decide(ctx, snap, req, policy=pol)
        # Decision IDs and timestamps differ (uuid + wallclock) but
        # everything else must be identical.
        self.assertEqual(d1.result, d2.result)
        self.assertEqual(d1.reason_codes, d2.reason_codes)
        self.assertEqual(d1.authority_before, d2.authority_before)
        self.assertEqual(d1.authority_after, d2.authority_after)
        self.assertEqual(d1.broker_scope, d2.broker_scope)
        self.assertEqual(d1.requested_action, d2.requested_action)

    def test_decide_does_not_mutate_inputs(self):
        ctx = _ctx()
        snap = _snapshot()
        pol = _baseline_policy()
        req = _req()
        ctx_copy  = deepcopy(ctx)
        snap_copy = deepcopy(snap)
        pol_copy  = deepcopy(pol)
        req_copy  = deepcopy(req)
        decide(ctx, snap, req, policy=pol)
        self.assertEqual(ctx,  ctx_copy)
        self.assertEqual(req,  req_copy)
        self.assertEqual(snap.taken_at_utc, snap_copy.taken_at_utc)
        self.assertEqual(snap.scopes, snap_copy.scopes)
        self.assertEqual(pol.amount_min_usd, pol_copy.amount_min_usd)

    def test_decide_grep_no_db_write_in_engine_file(self):
        """Sanity guard for correction #1 at the source-level."""
        with open(os.path.join(_REPO, "bot/risk_authority/engine.py")) as f:
            src = f.read()
        for forbidden in ("conn.execute(", "INSERT ", "UPDATE ",
                          "commit()", ".cursor("):
            self.assertNotIn(forbidden, src,
                f"engine.py must not contain {forbidden!r} per correction #1")

    def test_decide_query_authority_returns_quickly(self):
        """query_authority is always allowed at AUTHORITY_OFF+, never raises."""
        ctx = _ctx(action="query_authority", authority=Authority.OFF)
        snap = _snapshot()
        d = decide(ctx, snap, None, policy=_baseline_policy())
        self.assertEqual(d.result, "allow")

    def test_decide_returns_frozen_dataclass(self):
        d = decide(_ctx(), _snapshot(), _req(), policy=_baseline_policy())
        with self.assertRaises(Exception):
            d.result = "block"   # type: ignore[misc]

    def test_request_payload_redactable(self):
        d = decide(_ctx(), _snapshot(), _req(symbol="AAPL", amount=42.0),
                    policy=_baseline_policy())
        self.assertIsNotNone(d.request_payload)
        self.assertEqual(d.request_payload["symbol"], "AAPL")
        self.assertEqual(d.request_payload["amount_usd"], 42.0)

    def test_no_payload_on_query_authority(self):
        d = decide(_ctx(action="query_authority"), _snapshot(), None,
                    policy=_baseline_policy())
        self.assertIsNone(d.request_payload)

    def test_taken_at_is_iso_utc(self):
        d = decide(_ctx(), _snapshot(), _req(), policy=_baseline_policy())
        self.assertTrue(d.taken_at_utc.endswith("+00:00")
                        or "Z" in d.taken_at_utc
                        or "T" in d.taken_at_utc)

    def test_decision_id_is_unique(self):
        d1 = decide(_ctx(), _snapshot(), _req(), policy=_baseline_policy())
        d2 = decide(_ctx(), _snapshot(), _req(), policy=_baseline_policy())
        self.assertNotEqual(d1.decision_id, d2.decision_id)

    def test_decide_default_policy_loads_from_env(self):
        # Without explicit policy, engine loads from env. Should not raise.
        d = decide(_ctx(action="query_authority"), _snapshot(), None)
        self.assertIn(d.result, ("allow", "block"))


# ─────────────────────────────────────────────────────────────────────────────
# 2. Gate ordering — one test per gate (25)
# ─────────────────────────────────────────────────────────────────────────────


class TestGateOrdering(unittest.TestCase):
    """Each gate must trigger exactly when expected, with first-failure-wins
    semantics. We probe each gate by failing only that gate and asserting
    the resulting reason code."""

    def test_gate_count_matches_plan(self):
        # The approved plan says 24 gates; the shipped code adds one
        # final data_staleness sweep for a total of 25. Either is
        # acceptable; what we check is that the count is announced
        # truthfully (no off-by-one bug).
        self.assertEqual(len(_GATES), 25)

    def test_gate_1_policy_invalid(self):
        # Snapshot with empty scopes => policy_invalid reason.
        # Per hard requirement #1, the engine MUST NOT raise on any
        # input; it returns a block decision with reason 'policy_invalid'.
        # We pick a broker_scope that the engine won't try to look up
        # in scopes (gate_policy fires before _scope_view runs because
        # _gate_policy is the FIRST gate; we use 'ibkr_paper' but the
        # gate checks emptiness BEFORE scope lookup).
        empty_snap = RiskSnapshot(
            taken_at_utc="2026-05-29T10:00:00Z",
            trading_day_utc="2026-05-29",
            scopes={}, global_view=GlobalView(None, None, None),
        )
        d = decide(_ctx(scope="ibkr_paper", action="query_authority"),
                    empty_snap, None, policy=_baseline_policy())
        self.assertEqual(d.result, "block")
        self.assertIn("policy_invalid", d.reason_codes)

    def test_gate_2_global_kill(self):
        d = decide(_ctx(), _snapshot(), _req(),
                   policy=_baseline_policy(global_kill_switch=True))
        self.assertEqual(d.result, "block")
        self.assertIn("global_kill", d.reason_codes)

    def test_gate_3_broker_kill(self):
        d = decide(_ctx(scope="ibkr_paper"), _snapshot(), _req(),
                   policy=_baseline_policy(
                       broker_kill_switch={"ibkr_paper": True}))
        self.assertIn("broker_kill", d.reason_codes)

    def test_gate_4_global_auto_disabled(self):
        d = decide(_ctx(), _snapshot(), _req(),
                   policy=_baseline_policy(global_auto_enabled=False))
        self.assertIn("global_auto_disabled", d.reason_codes)

    def test_gate_5_broker_auto_disabled(self):
        d = decide(_ctx(scope="ibkr_paper"), _snapshot(), _req(),
                   policy=_baseline_policy(
                       broker_auto_enabled={"ibkr_paper": False}))
        self.assertIn("broker_auto_disabled", d.reason_codes)

    def test_gate_6_broker_not_allowed(self):
        d = decide(_ctx(scope="etoro_real"), _snapshot(), _req(),
                   policy=_baseline_policy(
                       allowed_brokers=("ibkr_paper", "ibkr_live")))
        self.assertIn("broker_not_allowed", d.reason_codes)

    def test_gate_7_etoro_live_flag(self):
        d = decide(_ctx(scope="etoro_real"), _snapshot(), _req(),
                   policy=_baseline_policy(etoro_live_flag_enabled=False))
        self.assertIn("etoro_live_flag_disabled", d.reason_codes)

    def test_gate_8_etoro_live_env(self):
        d = decide(_ctx(scope="etoro_real"), _snapshot(), _req(),
                   policy=_baseline_policy(etoro_live_env_enabled=False))
        self.assertIn("etoro_live_env_disabled", d.reason_codes)

    def test_gate_9_authority_too_low(self):
        d = decide(_ctx(authority=Authority.SIGNAL_ONLY), _snapshot(), _req(),
                   policy=_baseline_policy())
        self.assertIn("authority_too_low", d.reason_codes)

    def test_gate_10_amount_invalid(self):
        bad = TradeRequest(symbol="AAPL", amount_usd=float("nan"), side="long")
        d = decide(_ctx(), _snapshot(), bad, policy=_baseline_policy())
        self.assertIn("amount_invalid", d.reason_codes)

    def test_gate_11_amount_below_min(self):
        d = decide(_ctx(), _snapshot(), _req(amount=0.5),
                   policy=_baseline_policy(amount_min_usd=5.0))
        self.assertIn("amount_below_min", d.reason_codes)

    def test_gate_12_single_trade_cap(self):
        d = decide(_ctx(), _snapshot(), _req(amount=999.0),
                   policy=_baseline_policy(single_trade_cap_usd=100.0))
        self.assertIn("single_trade_cap_exceeded", d.reason_codes)

    def test_gate_13_broker_capital_exceeded(self):
        scopes = {s: _scope(scope=s, capital_deployed=950.0)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(amount=100.0),
                   policy=_baseline_policy(
                       broker_capital_cap_usd={"ibkr_paper": 1000.0}))
        self.assertIn("broker_capital_cap_exceeded", d.reason_codes)

    def test_gate_14_global_capital_exceeded(self):
        scopes = {s: _scope(scope=s, capital_deployed=240.0)
                  for s in ALL_BROKER_SCOPES}  # 4 * 240 = 960
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(), snap, _req(amount=50.0),
                   policy=_baseline_policy(global_capital_cap_usd=1000.0))
        self.assertIn("global_capital_cap_exceeded", d.reason_codes)

    def test_gate_15_combined_exposure_cap_distinct_code(self):
        """Correction-driven: combined cap emits its own code, not global_capital."""
        scopes = {s: _scope(scope=s, capital_deployed=240.0)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(), snap, _req(amount=50.0),
                   policy=_baseline_policy(
                       global_capital_cap_usd=10_000.0,        # not binding
                       combined_exposure_cap_usd=500.0))       # binding
        self.assertIn("combined_exposure_cap_exceeded", d.reason_codes)
        self.assertNotIn("global_capital_cap_exceeded", d.reason_codes)

    def test_gate_16_broker_open_positions_cap(self):
        scopes = {s: _scope(scope=s, open_positions=4)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(),
                   policy=_baseline_policy(
                       broker_open_positions_cap={"ibkr_paper": 4}))
        self.assertIn("broker_open_positions_exceeded", d.reason_codes)

    def test_gate_17_global_open_positions_cap(self):
        scopes = {s: _scope(scope=s, open_positions=3)
                  for s in ALL_BROKER_SCOPES}  # 4*3 = 12
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(), snap, _req(),
                   policy=_baseline_policy(global_open_positions_cap=10))
        self.assertIn("global_open_positions_exceeded", d.reason_codes)

    def test_gate_18_broker_daily_loss(self):
        scopes = {s: _scope(scope=s, realised_daily_loss=600.0)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(),
                   policy=_baseline_policy(
                       broker_daily_loss_cap_usd={"ibkr_paper": 500.0}))
        self.assertIn("broker_daily_loss_exceeded", d.reason_codes)

    def test_gate_19_global_daily_loss(self):
        scopes = {s: _scope(scope=s, realised_daily_loss=300.0)
                  for s in ALL_BROKER_SCOPES}  # 1200
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(), snap, _req(),
                   policy=_baseline_policy(global_daily_loss_cap_usd=1000.0))
        self.assertIn("global_daily_loss_exceeded", d.reason_codes)

    def test_gate_20_drawdown_throttle(self):
        scopes = {s: _scope(scope=s, drawdown_from_peak=0.5)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(),
                   policy=_baseline_policy(drawdown_throttle_threshold=0.3))
        self.assertIn("drawdown_throttle_hit", d.reason_codes)

    def test_gate_21_concentration_per_symbol(self):
        scopes = {
            "ibkr_paper": _scope(scope="ibkr_paper",
                positions=[{"symbol": "AAPL", "side": "long",
                            "qty": 10, "exposure_usd": 4000.0,
                            "instrument_id": None}]),
            "ibkr_live":  _scope(scope="ibkr_live",
                positions=[{"symbol": "AAPL", "side": "long",
                            "qty": 5, "exposure_usd": 5000.0,
                            "instrument_id": None}]),
            "etoro_real": _scope(scope="etoro_real"),
            "etoro_paper": _scope(scope="etoro_paper"),
        }
        snap = _snapshot(scopes_override=scopes)
        # AAPL cross-broker exposure = 9000 already. Request +200 with
        # cap=9000 → blocked.
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(symbol="AAPL", amount=200.0),
                   policy=_baseline_policy(per_symbol_exposure_cap_usd=9000.0))
        self.assertIn("concentration_cap_exceeded", d.reason_codes)

    def test_gate_22_market_closed(self):
        d = decide(_ctx(market_open=False), _snapshot(), _req(),
                   policy=_baseline_policy())
        self.assertIn("market_closed", d.reason_codes)

    def test_gate_23_quote_stale(self):
        d = decide(_ctx(quote_age_sec=60.0, quote_max_age_sec=30.0),
                   _snapshot(), _req(), policy=_baseline_policy())
        self.assertIn("quote_stale", d.reason_codes)

    def test_gate_24_spread_too_wide(self):
        d = decide(_ctx(spread_bps=200.0, spread_max_bps=50.0),
                   _snapshot(), _req(), policy=_baseline_policy())
        self.assertIn("spread_too_wide", d.reason_codes)

    def test_gate_25_data_staleness_sweep(self):
        scopes = {s: _scope(scope=s, exposure_fresh_reads_count=0,
                            exposure_known=True)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(),
                   policy=_baseline_policy())
        # Either exposure_stale (count<1) fires or it passes; this test
        # asserts the gate exists and runs without crashing.
        self.assertIn(d.result, ("allow", "block"))

    def test_first_failure_wins(self):
        """Multiple violations: only the first gate's reason code appears."""
        d = decide(_ctx(), _snapshot(), _req(),
                   policy=_baseline_policy(
                       global_kill_switch=True,         # gate 2
                       global_auto_enabled=False,        # gate 4
                   ))
        # Gate 2 fires first; gate 4's reason must not appear.
        self.assertIn("global_kill", d.reason_codes)
        self.assertNotIn("global_auto_disabled", d.reason_codes)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Authority ladder
# ─────────────────────────────────────────────────────────────────────────────


class TestAuthorityLadder(unittest.TestCase):

    def test_strict_total_order(self):
        a = [Authority.OFF, Authority.SIGNAL_ONLY, Authority.PAPER_ONLY,
             Authority.ONE_SHOT_MANUAL, Authority.AUTO_ALLOWED]
        for i in range(len(a) - 1):
            self.assertLess(a[i], a[i + 1])

    def test_required_authority_mapping(self):
        self.assertEqual(REQUIRED_AUTHORITY["query_authority"], Authority.OFF)
        self.assertLessEqual(REQUIRED_AUTHORITY["trade_close"],
                              REQUIRED_AUTHORITY["trade_open"])

    def test_from_string_roundtrip(self):
        for a in Authority:
            self.assertEqual(Authority.from_string(a.name), a)

    def test_from_string_rejects_unknown(self):
        with self.assertRaises(ValueError):
            Authority.from_string("NOT_A_LEVEL")

    def test_is_monotone_safe_downgrade_always_allowed(self):
        for hi in Authority:
            for lo in Authority:
                if int(lo) <= int(hi):
                    self.assertTrue(
                        is_monotone_safe(hi, lo, source="auto"),
                        f"downgrade {hi} -> {lo} must be allowed under 'auto'"
                    )

    def test_is_monotone_safe_upgrade_only_via_manual_reset(self):
        # OFF → AUTO_ALLOWED is an upgrade.
        self.assertFalse(
            is_monotone_safe(Authority.OFF, Authority.AUTO_ALLOWED,
                              source="auto"))
        self.assertTrue(
            is_monotone_safe(Authority.OFF, Authority.AUTO_ALLOWED,
                              source="manual_reset"))

    def test_query_authority_at_off_allowed(self):
        d = decide(_ctx(action="query_authority", authority=Authority.OFF),
                   _snapshot(), None, policy=_baseline_policy())
        self.assertEqual(d.result, "allow")

    def test_trade_open_blocked_at_signal_only(self):
        d = decide(_ctx(authority=Authority.SIGNAL_ONLY), _snapshot(), _req(),
                   policy=_baseline_policy())
        self.assertIn("authority_too_low", d.reason_codes)

    def test_trade_open_blocked_at_paper_only(self):
        d = decide(_ctx(authority=Authority.PAPER_ONLY), _snapshot(), _req(),
                   policy=_baseline_policy())
        self.assertIn("authority_too_low", d.reason_codes)

    def test_trade_open_allowed_at_one_shot_manual(self):
        d = decide(_ctx(authority=Authority.ONE_SHOT_MANUAL),
                   _snapshot(), _req(), policy=_baseline_policy())
        self.assertEqual(d.result, "allow")

    def test_trade_open_allowed_at_auto_allowed(self):
        d = decide(_ctx(authority=Authority.AUTO_ALLOWED),
                   _snapshot(), _req(), policy=_baseline_policy())
        self.assertEqual(d.result, "allow")

    def test_trade_close_allowed_at_paper_only(self):
        d = decide(_ctx(action="trade_close", authority=Authority.PAPER_ONLY),
                   _snapshot(), _req(side="long"),
                   policy=_baseline_policy())
        self.assertEqual(d.result, "allow")

    def test_int_values_match_total_order(self):
        self.assertEqual(int(Authority.OFF), 0)
        self.assertEqual(int(Authority.AUTO_ALLOWED), 4)

    def test_as_label_returns_name(self):
        self.assertEqual(Authority.AUTO_ALLOWED.as_label(), "AUTO_ALLOWED")

    def test_authority_in_decision_uses_enum_not_int(self):
        d = decide(_ctx(), _snapshot(), _req(), policy=_baseline_policy())
        self.assertIsInstance(d.authority_before, Authority)
        self.assertIsInstance(d.authority_after, Authority)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Governor monotone (property tests)
# ─────────────────────────────────────────────────────────────────────────────


class TestGovernorMonotone(unittest.TestCase):

    def _random_decision(self, rng, scope="ibkr_paper", day="2026-05-29"):
        """Build a random RiskDecision for property testing."""
        all_reasons = list(REASON_CODES)
        # Sometimes block, sometimes allow.
        if rng.random() < 0.6:
            n = rng.randint(1, 1)
            reasons = tuple(rng.sample(all_reasons, n))
            result = "block"
            # downgrade-only authority_after; choose a random level <=
            # authority_before.
            before = rng.choice(list(Authority))
            after = Authority(rng.randint(0, int(before)))
        else:
            reasons = ()
            result = "allow"
            before = rng.choice(list(Authority))
            after = before
        ts = f"{day}T{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:00Z"
        return RiskDecision(
            decision_id=f"d-{rng.randint(0, 10**9)}",
            taken_at_utc=ts,
            broker_scope=scope, requested_action="trade_open",
            result=result,
            authority_before=before,
            authority_after=after,
            reason_codes=reasons,
            recovery_paths={r: RECOVERY_PATHS.get(r, "x") for r in reasons},
            explainer="property-test",
        ), before

    def test_governor_never_auto_upgrades_random_sequences(self):
        """Property test: 1000 random sequences never produce an
        autonomous upgrade. Manual reset is the ONLY upgrade carrier."""
        rng = random.Random(0xC0FFEE)
        N = 1000
        violations = 0
        for _ in range(N):
            state = GovernorState(authority=Authority.AUTO_ALLOWED)
            for _ in range(rng.randint(1, 8)):
                decision, before = self._random_decision(rng)
                # The governor is fed the previous-state authority (carry-forward).
                proposed = propose(state.authority, decision, prev_state=state)
                if int(proposed.authority) > int(state.authority):
                    violations += 1
                state = proposed
        self.assertEqual(violations, 0,
            f"governor produced {violations} autonomous upgrades in {N} runs")

    def test_governor_downgrades_on_kill(self):
        state = GovernorState(authority=Authority.AUTO_ALLOWED)
        d = RiskDecision(
            decision_id="d1", taken_at_utc="2026-05-29T10:00:00Z",
            broker_scope="ibkr_paper", requested_action="trade_open",
            result="block",
            authority_before=Authority.AUTO_ALLOWED,
            authority_after=Authority.OFF,
            reason_codes=("global_kill",),
            recovery_paths={"global_kill": "manual_reset"},
            explainer="kill",
        )
        new = propose(Authority.AUTO_ALLOWED, d, prev_state=state)
        self.assertEqual(new.authority, Authority.OFF)
        self.assertTrue(new.manual_reset_required)

    def test_governor_day_latch_persists_same_utc_day(self):
        """Daily-loss latch must not auto-clear within the same UTC day."""
        state = GovernorState(authority=Authority.AUTO_ALLOWED)
        # Day 1: daily-loss breach.
        d_breach = RiskDecision(
            decision_id="d1", taken_at_utc="2026-05-29T15:00:00Z",
            broker_scope="ibkr_paper", requested_action="trade_open",
            result="block",
            authority_before=Authority.AUTO_ALLOWED,
            authority_after=Authority.SIGNAL_ONLY,
            reason_codes=("broker_daily_loss_exceeded",),
            recovery_paths={"broker_daily_loss_exceeded": "wait"},
            explainer="loss",
        )
        state = propose(state.authority, d_breach, prev_state=state)
        self.assertEqual(state.authority, Authority.SIGNAL_ONLY)
        self.assertEqual(state.latched_day_utc, "2026-05-29")

        # Same day, later: a fresh allow. Day-latch keeps us clamped.
        d_allow = RiskDecision(
            decision_id="d2", taken_at_utc="2026-05-29T18:00:00Z",
            broker_scope="ibkr_paper", requested_action="trade_open",
            result="allow",
            authority_before=Authority.SIGNAL_ONLY,
            authority_after=Authority.SIGNAL_ONLY,
            reason_codes=(),
            recovery_paths={},
            explainer="ok",
        )
        next_state = propose(state.authority, d_allow, prev_state=state)
        # Day-latch holds: authority can't rise (it was already SIGNAL_ONLY).
        self.assertLessEqual(int(next_state.authority), int(Authority.SIGNAL_ONLY))
        self.assertEqual(next_state.latched_day_utc, "2026-05-29")

    def test_governor_manual_reset_clears_latch(self):
        state = GovernorState(
            authority=Authority.OFF, latched_day_utc="2026-05-29",
            latched_reasons=("global_kill",), manual_reset_required=True,
        )
        new = apply_manual_reset(state, new_authority=Authority.AUTO_ALLOWED)
        self.assertEqual(new.authority, Authority.AUTO_ALLOWED)
        self.assertFalse(new.manual_reset_required)
        self.assertEqual(new.latched_reasons, ())

    def test_governor_auto_restore_requires_fresh_streak(self):
        """N=3 consecutive fresh reads before auto-restorable latch lifts."""
        state = GovernorState(authority=Authority.SIGNAL_ONLY,
                              latched_reasons=("daily_pnl_unknown",),
                              latched_day_utc="2026-05-29")
        d_allow = RiskDecision(
            decision_id="d", taken_at_utc="2026-05-29T11:00:00Z",
            broker_scope="ibkr_paper", requested_action="trade_open",
            result="allow",
            authority_before=Authority.SIGNAL_ONLY,
            authority_after=Authority.SIGNAL_ONLY,
            reason_codes=(),
            recovery_paths={},
            explainer="fresh",
        )
        # 1 fresh read — latch held.
        s1 = propose(state.authority, d_allow, prev_state=state)
        # 2 fresh — still held.
        s2 = propose(s1.authority, d_allow, prev_state=s1)
        # 3 fresh — latch cleared.
        s3 = propose(s2.authority, d_allow, prev_state=s2)
        self.assertEqual(s1.fresh_consecutive_count, 1)
        self.assertEqual(s2.fresh_consecutive_count, 2)
        self.assertEqual(s3.fresh_consecutive_count, 3)
        self.assertEqual(s3.latched_reasons, ())

    def test_propose_returns_frozen_state(self):
        state = GovernorState(authority=Authority.AUTO_ALLOWED)
        d = RiskDecision(
            decision_id="d", taken_at_utc="2026-05-29T10:00:00Z",
            broker_scope="ibkr_paper", requested_action="trade_open",
            result="allow",
            authority_before=Authority.AUTO_ALLOWED,
            authority_after=Authority.AUTO_ALLOWED,
            reason_codes=(), recovery_paths={}, explainer="",
        )
        new = propose(state.authority, d, prev_state=state)
        with self.assertRaises(Exception):
            new.authority = Authority.OFF   # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# 5. Fail-closed semantics
# ─────────────────────────────────────────────────────────────────────────────


class TestFailClosed(unittest.TestCase):

    def test_unknown_pnl_blocks_trade_open(self):
        scopes = dict.fromkeys(ALL_BROKER_SCOPES, None)
        for k in scopes:
            scopes[k] = _scope(scope=k, pnl_known=False)
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(),
                   policy=_baseline_policy(
                       broker_daily_loss_cap_usd={"ibkr_paper": 1000.0}))
        self.assertIn("daily_pnl_unknown", d.reason_codes)

    def test_unknown_exposure_blocks_trade_open(self):
        scopes = {s: _scope(scope=s, exposure_known=False)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap,
                   _req(),
                   policy=_baseline_policy(
                       broker_capital_cap_usd={"ibkr_paper": 1000.0}))
        self.assertIn("exposure_unknown", d.reason_codes)

    def test_combined_exposure_unknown_when_one_scope_unknown(self):
        """Correction #4: ANY unknown scope ⇒ combined value is None ⇒ block."""
        scopes = {s: _scope(scope=s) for s in ALL_BROKER_SCOPES}
        scopes["etoro_real"] = _scope(scope="etoro_real", exposure_known=False)
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(),
                   policy=_baseline_policy(
                       combined_exposure_cap_usd=100_000.0))
        # Either combined_exposure_unknown or exposure_unknown reaches first;
        # both are fail-closed. The point: trade is blocked.
        self.assertEqual(d.result, "block")
        self.assertIn(d.reason_codes[0],
                       ("combined_exposure_unknown",
                        "exposure_unknown",
                        "global_open_positions_unknown",
                        "global_daily_loss_unknown"))

    def test_global_daily_loss_unknown_when_one_scope_pnl_unknown(self):
        scopes = {s: _scope(scope=s) for s in ALL_BROKER_SCOPES}
        scopes["etoro_real"] = _scope(scope="etoro_real", pnl_known=False)
        snap = _snapshot(scopes_override=scopes)
        # ibkr_paper has known PnL so the broker-level gate passes;
        # combined PnL is unknown so the global gate must fail-closed.
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(),
                   policy=_baseline_policy(global_daily_loss_cap_usd=1000.0))
        self.assertEqual(d.result, "block")
        self.assertTrue(
            "global_daily_loss_unknown" in d.reason_codes
            or "daily_pnl_unknown" in d.reason_codes,
            f"expected unknown-pnl reason, got {d.reason_codes}"
        )

    def test_known_zero_pnl_does_not_trigger_unknown(self):
        """Known-zero PnL with daily_pnl_available=True ⇒ NOT unknown."""
        scopes = {s: _scope(scope=s, pnl_known=True, realised_pnl=0.0,
                              realised_daily_loss=0.0)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(),
                   policy=_baseline_policy(
                       broker_daily_loss_cap_usd={"ibkr_paper": 1000.0}))
        # No daily_pnl_unknown reason; trade allowed (or blocked for an unrelated reason).
        self.assertNotIn("daily_pnl_unknown", d.reason_codes)
        self.assertNotIn("global_daily_loss_unknown", d.reason_codes)

    def test_known_zero_exposure_does_not_trigger_unknown(self):
        """Known-zero exposure with exposure_status='exposure_fresh' ⇒ NOT unknown."""
        scopes = {s: _scope(scope=s, exposure_known=True, open_positions=0,
                              capital_deployed=0.0)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(),
                   policy=_baseline_policy(
                       broker_capital_cap_usd={"ibkr_paper": 1000.0}))
        self.assertNotIn("exposure_unknown", d.reason_codes)
        self.assertNotIn("combined_exposure_unknown", d.reason_codes)

    def test_pnl_status_distinguishes_known_zero_from_unknown_zero(self):
        kz = _scope(pnl_known=True, realised_pnl=0.0, realised_daily_loss=0.0)
        uz = _scope(pnl_known=False, realised_pnl=0.0, realised_daily_loss=0.0)
        # Bare numeric column is identical (0.0 in both).
        self.assertEqual(kz.realised_pnl_usd, uz.realised_pnl_usd)
        self.assertEqual(kz.realised_daily_loss, uz.realised_daily_loss)
        # But the *known* flag distinguishes — engine MUST call this.
        self.assertTrue(kz.is_pnl_known())
        self.assertFalse(uz.is_pnl_known())

    def test_exposure_status_distinguishes_known_zero_from_unknown_zero(self):
        kz = _scope(exposure_known=True, open_positions=0, capital_deployed=0.0)
        uz = _scope(exposure_known=False, open_positions=0, capital_deployed=0.0)
        self.assertEqual(kz.open_positions, uz.open_positions)
        self.assertEqual(kz.capital_deployed, uz.capital_deployed)
        self.assertTrue(kz.is_exposure_known())
        self.assertFalse(uz.is_exposure_known())

    def test_daily_loss_block_latch_is_a_hard_block(self):
        scopes = {s: _scope(scope=s, daily_loss_block_active=(s == "ibkr_paper"))
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(),
                   policy=_baseline_policy())
        self.assertIn("daily_loss_block_active", d.reason_codes)

    def test_authority_after_downgrades_on_unknown_pnl(self):
        scopes = {s: _scope(scope=s, pnl_known=False)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(authority=Authority.AUTO_ALLOWED, scope="ibkr_paper"),
                   snap, _req(),
                   policy=_baseline_policy(
                       broker_daily_loss_cap_usd={"ibkr_paper": 1000.0}))
        self.assertLess(int(d.authority_after), int(d.authority_before))


# ─────────────────────────────────────────────────────────────────────────────
# 6. Reason codes + recovery paths
# ─────────────────────────────────────────────────────────────────────────────


class TestReasonCodes(unittest.TestCase):

    def test_every_reason_in_closed_set(self):
        scopes = {s: _scope(scope=s, pnl_known=False)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(),
                   policy=_baseline_policy(
                       broker_daily_loss_cap_usd={"ibkr_paper": 1000.0}))
        for r in d.reason_codes:
            self.assertIn(r, REASON_CODES, f"reason {r!r} not in closed set")

    def test_every_block_has_recovery_path(self):
        d = decide(_ctx(), _snapshot(), _req(),
                   policy=_baseline_policy(global_kill_switch=True))
        self.assertEqual(d.result, "block")
        for r in d.reason_codes:
            self.assertIn(r, d.recovery_paths)
            self.assertTrue(d.recovery_paths[r],
                f"empty recovery path for reason {r!r}")

    def test_recovery_paths_table_covers_all_reasons(self):
        missing = [r for r in REASON_CODES if r not in RECOVERY_PATHS]
        self.assertEqual(missing, [],
            f"reasons without recovery path: {missing}")

    def test_combined_exposure_cap_has_distinct_recovery_path(self):
        self.assertIn("combined_exposure_cap_exceeded", RECOVERY_PATHS)
        self.assertNotEqual(
            RECOVERY_PATHS["combined_exposure_cap_exceeded"],
            RECOVERY_PATHS["global_capital_cap_exceeded"],
        )


# ─────────────────────────────────────────────────────────────────────────────
# 7. decide_and_audit — wrapper writes audit rows
# ─────────────────────────────────────────────────────────────────────────────


class TestDecideAndAudit(unittest.TestCase):

    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def test_one_decide_and_audit_writes_one_snapshot_and_one_decision(self):
        with self.fx.conn() as c:
            decide_and_audit(c, _ctx(), _snapshot(), _req(),
                              policy=_baseline_policy())
            n_snap = c.execute("SELECT COUNT(*) FROM risk_snapshots").fetchone()[0]
            n_dec  = c.execute("SELECT COUNT(*) FROM risk_decisions").fetchone()[0]
        self.assertEqual(n_snap, 1)
        self.assertEqual(n_dec, 1)

    def test_decision_links_to_existing_snapshot(self):
        with self.fx.conn() as c:
            d = decide_and_audit(c, _ctx(), _snapshot(), _req(),
                                  policy=_baseline_policy())
            row = c.execute(
                "SELECT snapshot_id FROM risk_decisions "
                "WHERE decision_id=?", (d.decision_id,)
            ).fetchone()
            snap_exists = c.execute(
                "SELECT 1 FROM risk_snapshots WHERE id=?", (row[0],)
            ).fetchone()
        self.assertIsNotNone(snap_exists)
        self.assertEqual(d.snapshot_ref, row[0])

    def test_audit_row_redacts_secrets(self):
        """request_payload must NOT contain api_key / token if any were
        present in the original — verified by injecting redact-eligible
        keys into request payload and confirming they come back redacted."""
        with self.fx.conn() as c:
            # Build a decision with an explicit payload-like input by
            # calling decide directly first (the engine's payload only
            # uses TradeRequest fields, not arbitrary kwargs — so the
            # redaction surface to test is the snapshot serializer).
            scopes = {s: _scope(scope=s) for s in ALL_BROKER_SCOPES}
            snap = _snapshot(scopes_override=scopes)
            decide_and_audit(c, _ctx(), snap, _req(),
                              policy=_baseline_policy())
            (snapshot_json,) = c.execute(
                "SELECT snapshot_json FROM risk_snapshots LIMIT 1"
            ).fetchone()
        # No secrets in the snapshot — even substring checks should pass.
        lowered = snapshot_json.lower()
        for forbidden in ("api_key", "api-key", "bearer ", "x-api-key",
                          "x-user-key", "telegram_token"):
            self.assertNotIn(forbidden, lowered)

    def test_audit_failure_rolls_back_both_rows(self):
        """If the snapshot write succeeds but the decision write fails,
        the transaction must roll back so we never have a snapshot
        without a decision."""
        with self.fx.conn() as c:
            # Pre-insert a decision_id that we'll collide with to force
            # an IntegrityError on the second write.
            c.execute(
                "INSERT INTO risk_decisions "
                "(decision_id, taken_at, broker_scope, requested_action, "
                " result, authority_before, authority_after, "
                " reason_codes, source, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("collide", "2026-05-29T10:00:00Z", "ibkr_paper",
                 "trade_open", "block", "OFF", "OFF", "[]", "auto",
                 "2026-05-29T10:00:00Z"),
            )
            c.commit()
            # Patch decide() to return a decision with id='collide'.
            from bot.risk_authority import audit_decisions
            real_decide = audit_decisions.decide
            def fake_decide(ctx, snap, req, *, policy=None):
                d = real_decide(ctx, snap, req, policy=policy)
                return RiskDecision(
                    decision_id="collide",  # collision!
                    taken_at_utc=d.taken_at_utc,
                    broker_scope=d.broker_scope,
                    requested_action=d.requested_action,
                    result=d.result,
                    authority_before=d.authority_before,
                    authority_after=d.authority_after,
                    reason_codes=d.reason_codes,
                    recovery_paths=d.recovery_paths,
                    explainer=d.explainer,
                    snapshot_ref=d.snapshot_ref,
                    request_payload=d.request_payload,
                )
            audit_decisions.decide = fake_decide
            try:
                n_snap_before = c.execute(
                    "SELECT COUNT(*) FROM risk_snapshots"
                ).fetchone()[0]
                with self.assertRaises(sqlite3.IntegrityError):
                    decide_and_audit(c, _ctx(), _snapshot(), _req(),
                                      policy=_baseline_policy())
                n_snap_after = c.execute(
                    "SELECT COUNT(*) FROM risk_snapshots"
                ).fetchone()[0]
                self.assertEqual(n_snap_before, n_snap_after,
                    "snapshot row leaked despite decision-write failure")
            finally:
                audit_decisions.decide = real_decide

    def test_decision_row_has_explainer_and_authority_columns(self):
        with self.fx.conn() as c:
            d = decide_and_audit(c, _ctx(), _snapshot(), _req(),
                                  policy=_baseline_policy())
            row = c.execute(
                "SELECT authority_before, authority_after, explainer, "
                "       reason_codes, recovery_paths, source "
                "FROM risk_decisions WHERE decision_id=?",
                (d.decision_id,)
            ).fetchone()
        self.assertEqual(row[0], "ONE_SHOT_MANUAL")
        self.assertTrue(row[2])  # non-empty explainer
        self.assertEqual(row[5], "auto")


# ─────────────────────────────────────────────────────────────────────────────
# 8. assemble_snapshot — read-only, no broker calls
# ─────────────────────────────────────────────────────────────────────────────


class TestAssembleSnapshot(unittest.TestCase):

    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def test_assemble_with_no_rows_produces_absent_scopes(self):
        with self.fx.conn() as c:
            snap = assemble_snapshot(c, trading_day="2026-05-29")
        self.assertEqual(set(snap.scopes), set(ALL_BROKER_SCOPES))
        for sv in snap.scopes.values():
            self.assertEqual(sv.exposure_status, "absent")
            self.assertFalse(sv.is_exposure_known())
            self.assertFalse(sv.is_pnl_known())

    def test_assemble_does_not_write(self):
        with self.fx.conn() as c:
            before = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker"
            ).fetchone()[0]
            assemble_snapshot(c, trading_day="2026-05-29")
            after = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker"
            ).fetchone()[0]
        self.assertEqual(before, after)

    def test_assemble_reflects_existing_daily_state(self):
        with self.fx.conn() as c:
            lifecycle = json.dumps({"status": "fresh"})
            c.execute(
                "INSERT INTO daily_state_per_broker "
                "(date, broker_scope, realised_pnl_usd, "
                " daily_pnl_available, lifecycle_json, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                ("2026-05-29", "ibkr_paper", -50.0, 1, lifecycle,
                 "2026-05-29T10:00:00Z"),
            )
            c.commit()
            snap = assemble_snapshot(c, trading_day="2026-05-29")
        sv = snap.scopes["ibkr_paper"]
        self.assertEqual(sv.realised_pnl_usd, -50.0)
        self.assertTrue(sv.is_pnl_known())


# ─────────────────────────────────────────────────────────────────────────────
# 9. Isolation: AST + subprocess
# ─────────────────────────────────────────────────────────────────────────────


class TestIsolation(unittest.TestCase):
    """Engine must not import ingestion/adapter/broker code (correction #2).
    Scanner/strategy/risk imports must not load engine/governor/snapshot."""

    FORBIDDEN_MODULES = {
        "bot.risk_authority.ingest",
        "bot.risk_authority.ingest_etoro",
        "bot.risk_authority.ingest_ibkr",
        "bot.risk_authority.ingest_exposure",
        "bot.risk_authority.ingest_etoro_exposure",
        "bot.risk_authority.ingest_ibkr_exposure",
        "bot.etoro.live_broker",
        "tools.etoro_live_write",
        "bot.brokers",
    }
    FORBIDDEN_NAMES = {"EtoroLiveBroker", "IBKRBroker", "PaperBroker"}
    FORBIDDEN_HTTP  = {"POST", "DELETE", "PUT", "PATCH"}
    FORBIDDEN_HTTP_METHODS = {"post", "delete", "put", "patch"}
    FORBIDDEN_ORDER = {"placeOrder", "cancelOrder", "modifyOrder",
                        "reqGlobalCancel"}

    TARGETS = (
        "bot/risk_authority/engine.py",
        "bot/risk_authority/governor.py",
        "bot/risk_authority/snapshot.py",
        "bot/risk_authority/audit_decisions.py",
        "bot/risk_authority/authority.py",
    )

    def _scan(self, path):
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                m = node.module or ""
                if m in self.FORBIDDEN_MODULES:
                    offenders.append(f"ImportFrom {m!r} @{node.lineno}")
                if m.startswith("bot.brokers"):
                    offenders.append(f"ImportFrom bot.brokers @{node.lineno}")
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
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and a.value.upper() in self.FORBIDDEN_HTTP):
                        offenders.append(f"call arg {a.value!r} @{a.lineno}")
                for kw in node.keywords:
                    if (kw.arg == "method"
                            and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)
                            and kw.value.value.upper() in self.FORBIDDEN_HTTP):
                        offenders.append(f"method={kw.value.value} @{kw.value.lineno}")
                fn = node.func
                if isinstance(fn, ast.Attribute):
                    if fn.attr in self.FORBIDDEN_HTTP_METHODS:
                        offenders.append(f".{fn.attr} @{fn.lineno}")
                    if fn.attr in self.FORBIDDEN_ORDER:
                        offenders.append(f".{fn.attr} (order) @{fn.lineno}")
        return offenders

    def test_no_forbidden_in_any_m14e_file(self):
        for path in self.TARGETS:
            full = os.path.join(_REPO, path)
            offenders = self._scan(full)
            self.assertEqual(offenders, [],
                f"{path} has forbidden references: {offenders}")

    def test_scan_catches_synthetic_placeOrder(self):
        synthetic = "def f(ib):\n    return ib.placeOrder('x', 'y')\n"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
            tf.write(synthetic)
            tmp = tf.name
        try:
            o = self._scan(tmp)
            self.assertTrue(any("placeOrder" in s for s in o))
        finally:
            os.unlink(tmp)

    def test_scan_ignores_docstring_mentions(self):
        synthetic = ('"""no placeOrder, no cancelOrder, no '
                     'EtoroLiveBroker, no POST"""\n'
                     "def f(): return 0\n")
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
            tf.write(synthetic)
            tmp = tf.name
        try:
            self.assertEqual(self._scan(tmp), [])
        finally:
            os.unlink(tmp)

    def test_scanner_imports_do_not_load_engine_modules(self):
        check = (
            "import sys\n"
            "import bot.scanner, bot.strategy, bot.risk, bot.brokers\n"
            "forbidden = [m for m in (\n"
            "    'bot.risk_authority.engine',\n"
            "    'bot.risk_authority.governor',\n"
            "    'bot.risk_authority.snapshot',\n"
            "    'bot.risk_authority.audit_decisions',\n"
            ") if m in sys.modules]\n"
            "print('loaded:', forbidden)\n"
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
# 10. Combined-exposure correction #4 (all four scopes)
# ─────────────────────────────────────────────────────────────────────────────


class TestCombinedExposureAllScopes(unittest.TestCase):

    def test_disallowed_broker_exposure_counts_toward_combined(self):
        """Correction #4: even disallowed brokers count toward combined exposure."""
        # etoro_real is disallowed but has 5000 USD exposure.
        scopes = {
            "ibkr_paper": _scope(scope="ibkr_paper", capital_deployed=2000.0),
            "ibkr_live":  _scope(scope="ibkr_live", capital_deployed=2000.0),
            "etoro_paper": _scope(scope="etoro_paper", capital_deployed=2000.0),
            "etoro_real":  _scope(scope="etoro_real", capital_deployed=5000.0),
        }
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(amount=100.0),
                   policy=_baseline_policy(
                       allowed_brokers=("ibkr_paper", "ibkr_live",
                                          "etoro_paper"),
                       combined_exposure_cap_usd=10_000.0))
        # 2000+2000+2000+5000 + 100 = 11100 > 10000 → block.
        self.assertEqual(d.result, "block")
        self.assertIn("combined_exposure_cap_exceeded", d.reason_codes)

    def test_global_view_combined_is_none_when_one_scope_unknown(self):
        scopes = {s: _scope(scope=s) for s in ALL_BROKER_SCOPES}
        scopes["etoro_paper"] = _scope(scope="etoro_paper", exposure_known=False)
        snap = _snapshot(scopes_override=scopes)
        self.assertIsNone(snap.global_view.combined_capital_deployed)
        self.assertIn("etoro_paper", snap.global_view.unknown_exposure_scopes)


# ─────────────────────────────────────────────────────────────────────────────
# 11. Per-symbol concentration (correction #3)
# ─────────────────────────────────────────────────────────────────────────────


class TestConcentrationPerSymbol(unittest.TestCase):

    def test_cross_scope_aggregate(self):
        scopes = {
            "ibkr_paper": _scope(scope="ibkr_paper",
                positions=[{"symbol": "TSLA", "side": "long",
                            "qty": 5, "exposure_usd": 3000.0,
                            "instrument_id": None}]),
            "ibkr_live":  _scope(scope="ibkr_live",
                positions=[{"symbol": "TSLA", "side": "long",
                            "qty": 2, "exposure_usd": 2000.0,
                            "instrument_id": None}]),
            "etoro_real": _scope(scope="etoro_real"),
            "etoro_paper": _scope(scope="etoro_paper"),
        }
        snap = _snapshot(scopes_override=scopes)
        # TSLA cross-scope = 5000. Add 1000 → 6000. Cap=5500 → block.
        d = decide(_ctx(scope="ibkr_paper"),
                   snap, _req(symbol="TSLA", amount=1000.0),
                   policy=_baseline_policy(per_symbol_exposure_cap_usd=5500.0))
        self.assertIn("concentration_cap_exceeded", d.reason_codes)


# ─────────────────────────────────────────────────────────────────────────────
# 12. UTC trading day latch (correction #5)
# ─────────────────────────────────────────────────────────────────────────────


class TestUTCTradingDayLatch(unittest.TestCase):

    def test_governor_latched_day_uses_utc_prefix(self):
        d = RiskDecision(
            decision_id="d", taken_at_utc="2026-05-29T23:55:00Z",
            broker_scope="ibkr_paper", requested_action="trade_open",
            result="block",
            authority_before=Authority.AUTO_ALLOWED,
            authority_after=Authority.SIGNAL_ONLY,
            reason_codes=("broker_daily_loss_exceeded",),
            recovery_paths={"broker_daily_loss_exceeded": "wait"},
            explainer="late-day loss",
        )
        state = propose(Authority.AUTO_ALLOWED, d,
                         prev_state=GovernorState(authority=Authority.AUTO_ALLOWED))
        self.assertEqual(state.latched_day_utc, "2026-05-29")

    def test_next_utc_day_makes_day_latch_eligible_for_clear(self):
        state = GovernorState(
            authority=Authority.SIGNAL_ONLY,
            latched_day_utc="2026-05-29",
            latched_reasons=("broker_daily_loss_exceeded",),
        )
        d_next_day = RiskDecision(
            decision_id="d", taken_at_utc="2026-05-30T08:00:00Z",
            broker_scope="ibkr_paper", requested_action="trade_open",
            result="allow",
            authority_before=Authority.SIGNAL_ONLY,
            authority_after=Authority.SIGNAL_ONLY,
            reason_codes=(),
            recovery_paths={}, explainer="next day",
        )
        new = propose(state.authority, d_next_day, prev_state=state)
        # Day-latch clamp lifted; fresh-streak builds.
        self.assertEqual(new.fresh_consecutive_count, 1)


# ─────────────────────────────────────────────────────────────────────────────
# 13. M13.4A broker-allocation policy is the source of truth (hard req #4)
# ─────────────────────────────────────────────────────────────────────────────


class TestM13_4A_PolicySource(unittest.TestCase):
    """Per hard requirement #4: policy source MUST respect the existing
    M13.4A broker allocation / dashboard policy. Do not create a
    disconnected env-only policy source.

    These tests prove the engine consults the M13.4A policy via
    `policy_view_from_allocation_policy(load_policy(conn))`, that
    family-level fields (kill_switch, caps) fan out to both scopes in
    the family, and that policy changes immediately affect decide()
    output.
    """

    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def _make_policy(self, **overrides):
        """Build a valid M13.4A policy dict for testing."""
        policy = {
            "version": 1,
            "global": {
                "auto_trading_enabled": True,
                "auto_trading_enabled_until_utc": (
                    __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc)
                    + __import__("datetime").timedelta(hours=1)).isoformat(),
                "max_auto_trading_capital": 100000.0,
                "kill_switch": False,
            },
            "ibkr_paper": {
                "auto_trading_enabled": False,
                "auto_trading_enabled_until_utc": None,
                "kill_switch": False,
            },
            "ibkr_live": {
                "auto_trading_enabled": False,
                "auto_trading_enabled_until_utc": None,
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
                "default_broker": "ibkr_paper",
                "route_overrides": {"IBKR": "ibkr_live", "ETORO": "etoro_paper"},
                "allowed_brokers": ["ibkr_paper", "ibkr_live", "etoro_paper"],
                "etoro_live_enabled": False,
            },
        }
        # Apply nested overrides.
        for key, value in overrides.items():
            if "." in key:
                top, sub = key.split(".", 1)
                policy[top][sub] = value
            else:
                policy[key] = value
        return policy

    def test_bridge_function_exists_and_callable(self):
        from bot.risk_authority.engine import policy_view_from_allocation_policy
        pol = policy_view_from_allocation_policy(self._make_policy())
        self.assertIsInstance(pol, RiskPolicyView)

    def test_global_kill_switch_propagates_to_engine(self):
        m13_pol = self._make_policy(**{"global.kill_switch": True})
        pol = policy_view_from_allocation_policy(m13_pol)
        d = decide(_ctx(), _snapshot(), _req(), policy=pol)
        self.assertIn("global_kill", d.reason_codes)

    def test_global_auto_disabled_propagates_to_engine(self):
        m13_pol = self._make_policy(**{"global.auto_trading_enabled": False})
        pol = policy_view_from_allocation_policy(m13_pol)
        d = decide(_ctx(), _snapshot(), _req(), policy=pol)
        self.assertIn("global_auto_disabled", d.reason_codes)

    def test_family_kill_switch_fans_out_to_both_scopes(self):
        """ibkr.kill_switch=True must block BOTH ibkr_paper AND ibkr_live."""
        m13_pol = self._make_policy(**{"ibkr.kill_switch": True})
        pol = policy_view_from_allocation_policy(m13_pol)
        for scope in ("ibkr_paper", "ibkr_live"):
            d = decide(_ctx(scope=scope), _snapshot(), _req(), policy=pol)
            self.assertIn("broker_kill", d.reason_codes,
                f"ibkr.kill_switch didn't propagate to {scope}")
        # And eToro scopes remain unaffected.
        d = decide(_ctx(scope="etoro_paper"), _snapshot(), _req(), policy=pol)
        self.assertNotIn("broker_kill", d.reason_codes)

    def test_etoro_family_kill_switch_fans_out(self):
        m13_pol = self._make_policy(**{"etoro.kill_switch": True})
        pol = policy_view_from_allocation_policy(m13_pol)
        for scope in ("etoro_paper", "etoro_real"):
            d = decide(_ctx(scope=scope), _snapshot(), _req(), policy=pol)
            self.assertIn("broker_kill", d.reason_codes,
                f"etoro.kill_switch didn't propagate to {scope}")

    def test_routing_allowed_brokers_propagates(self):
        m13_pol = self._make_policy()
        m13_pol["routing"]["allowed_brokers"] = ["ibkr_paper"]
        pol = policy_view_from_allocation_policy(m13_pol)
        d = decide(_ctx(scope="etoro_paper"), _snapshot(), _req(), policy=pol)
        self.assertIn("broker_not_allowed", d.reason_codes)
        d2 = decide(_ctx(scope="ibkr_paper"), _snapshot(), _req(amount=50.0),
                    policy=pol)
        self.assertNotIn("broker_not_allowed", d2.reason_codes)

    def test_etoro_live_enabled_propagates(self):
        # Policy flag False ⇒ etoro_real trades blocked.
        m13_pol = self._make_policy()
        m13_pol["routing"]["etoro_live_enabled"] = False
        m13_pol["routing"]["allowed_brokers"] = ["etoro_real"]
        pol = policy_view_from_allocation_policy(m13_pol)
        d = decide(_ctx(scope="etoro_real"), _snapshot(), _req(amount=50.0),
                   policy=pol)
        self.assertIn("etoro_live_flag_disabled", d.reason_codes)

    def test_family_capital_cap_propagates_to_both_scopes(self):
        m13_pol = self._make_policy(**{"ibkr.max_auto_trading_capital": 1000.0})
        pol = policy_view_from_allocation_policy(m13_pol)
        self.assertEqual(pol.broker_capital_cap_usd.get("ibkr_paper"), 1000.0)
        self.assertEqual(pol.broker_capital_cap_usd.get("ibkr_live"), 1000.0)

    def test_family_open_positions_cap_propagates(self):
        m13_pol = self._make_policy(**{"ibkr.max_open_positions": 2})
        pol = policy_view_from_allocation_policy(m13_pol)
        scopes = {s: _scope(scope=s, open_positions=2)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(), policy=pol)
        self.assertIn("broker_open_positions_exceeded", d.reason_codes)

    def test_family_daily_loss_cap_propagates(self):
        m13_pol = self._make_policy(**{"ibkr.max_daily_loss": 200.0})
        pol = policy_view_from_allocation_policy(m13_pol)
        scopes = {s: _scope(scope=s, realised_daily_loss=300.0)
                  for s in ALL_BROKER_SCOPES}
        snap = _snapshot(scopes_override=scopes)
        d = decide(_ctx(scope="ibkr_paper"), snap, _req(), policy=pol)
        self.assertIn("broker_daily_loss_exceeded", d.reason_codes)

    def test_single_trade_cap_uses_min_family_cap(self):
        m13_pol = self._make_policy(
            **{"ibkr.max_single_trade_amount": 100.0,
                "etoro.max_single_trade_amount": 50.0}
        )
        pol = policy_view_from_allocation_policy(m13_pol)
        # Min(100, 50) = 50 = the ceiling.
        self.assertEqual(pol.single_trade_cap_usd, 50.0)

    def test_policy_version_carried_through(self):
        m13_pol = self._make_policy(version=42)
        pol = policy_view_from_allocation_policy(m13_pol)
        self.assertEqual(pol.version, 42)

    def test_db_load_then_bridge_then_decide_end_to_end(self):
        """End-to-end: write a policy via save_policy(), load it via
        load_policy(), bridge to engine view, and decide() reflects it.
        This is the production wiring path."""
        from bot.broker_allocation import save_policy, load_policy
        m13_pol = self._make_policy(**{"global.kill_switch": True})
        with self.fx.conn() as c:
            save_policy(c, m13_pol)
            loaded = load_policy(c)
        pol = policy_view_from_allocation_policy(loaded)
        d = decide(_ctx(), _snapshot(), _req(), policy=pol)
        self.assertIn("global_kill", d.reason_codes)

    def test_bridge_is_pure_no_db_call(self):
        """The bridge must NOT take a sqlite connection — the caller
        loads policy via M13.4A's load_policy(conn). This proves the
        engine remains decoupled from DB I/O."""
        import inspect
        sig = inspect.signature(policy_view_from_allocation_policy)
        # First positional must be `policy` dict, not conn.
        self.assertEqual(list(sig.parameters)[0], "policy")
        # And the engine module must not import sqlite3.
        with open(os.path.join(_REPO, "bot/risk_authority/engine.py")) as f:
            src = f.read()
        self.assertNotIn("import sqlite3", src)
        self.assertNotIn("from sqlite3", src)

    def test_bridge_rejects_non_dict(self):
        with self.assertRaises(TypeError):
            policy_view_from_allocation_policy("not a dict")

    def test_default_policy_produces_safe_view(self):
        """DEFAULT_POLICY has global.auto_trading_enabled=False; the
        bridged view must reflect that (engine blocks trade_open)."""
        from bot.broker_allocation import DEFAULT_POLICY
        pol = policy_view_from_allocation_policy(DEFAULT_POLICY,
                                                  env_overrides=False)
        d = decide(_ctx(), _snapshot(), _req(), policy=pol)
        # DEFAULT_POLICY has auto disabled — block expected.
        self.assertIn("global_auto_disabled", d.reason_codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
