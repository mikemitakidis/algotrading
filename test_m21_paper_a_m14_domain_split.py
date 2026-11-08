"""M21 PR-a: M14 paper/live separation and lane-based IBKR authority.

Offline, pure-engine and temp-DB tests only (C5a isolation; no network, no broker,
no production data).

What is proved:
  A. Every cross-scope gate (global capital, combined exposure, global open
     positions, global daily loss, per-symbol concentration) evaluates a PAPER
     entry over that paper account alone. A real account's values or unknowns
     never gate a paper entry, and paper exposure never consumes a real cap.
  B. REAL-money entries (ibkr_live, etoro_real) keep the four-scope behaviour
     byte for byte: any unknown scope blocks; every scope's exposure counts.
  C. A paper entry still fails closed on its OWN unknown exposure / P&L.
  D. The real run_risk_preflight on a temp DB allows an ibkr_paper entry with
     ONLY ibkr_paper state present (no live, no eToro rows), and still blocks an
     ibkr_live entry in the same database.
  E. IBKR authority comes from the D0 lanes: the shared `ibkr` family flag
     alone enables neither scope; the paper lane never enables live; live needs
     the family flag AND its own lane; expired / naive / non-UTC / malformed
     time boxes and lane kill switches block.
  F. M14's time-box and lane rules agree with bot.broker_allocation (parity).
  G. Structure: no gate reads the four-scope view directly; the engine imports
     no broker or ingestion module (unchanged invariant).
"""
from __future__ import annotations

import ast
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import test_m14_e_engine as E
import test_m14_f_preflight as F
from bot.broker_allocation import _until_utc_active, is_auto_trading_allowed, save_policy
from bot.risk_authority import Authority, run_risk_preflight
from bot.risk_authority import engine as eng
from bot.risk_authority.engine import (
    PAPER_SCOPES,
    REAL_SCOPES,
    TradeRequest,
    decide,
    policy_view_from_allocation_policy,
)
from bot.risk_authority.snapshot import ALL_BROKER_SCOPES

REPO = Path(__file__).resolve().parent
ENGINE = REPO / "bot" / "risk_authority" / "engine.py"

CROSS_SCOPE_CODES = {
    "global_capital_cap_exceeded", "combined_exposure_cap_exceeded",
    "combined_exposure_unknown", "global_open_positions_exceeded",
    "global_open_positions_unknown", "global_daily_loss_exceeded",
    "global_daily_loss_unknown", "concentration_cap_exceeded",
}


def _future(hours=1):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past(hours=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _pos(symbol, usd):
    return {"symbol": symbol, "side": "long", "qty": 1, "exposure_usd": usd, "instrument_id": None}


def _decide(scope, scopes, req=None, **pol):
    return decide(E._ctx(scope=scope), E._snapshot(scopes_override=scopes), req or E._req(),
                  policy=E._baseline_policy(**pol))


def _all(**kw):
    return {s: E._scope(scope=s, **kw) for s in ALL_BROKER_SCOPES}


# ── A/B/C: every cross-scope gate, paper vs real ────────────────────────────────
class CrossScopeGatesByDomain(unittest.TestCase):

    def test_domains_are_disjoint_and_complete(self):
        self.assertEqual(set(PAPER_SCOPES) | set(REAL_SCOPES), set(ALL_BROKER_SCOPES))
        self.assertFalse(set(PAPER_SCOPES) & set(REAL_SCOPES))

    def test_global_capital(self):
        scopes = _all(capital_deployed=240.0)                   # 4 x 240 = 960
        live = _decide("ibkr_live", scopes, E._req(amount=50.0), global_capital_cap_usd=1000.0)
        self.assertIn("global_capital_cap_exceeded", live.reason_codes)        # real: unchanged
        paper = _decide("ibkr_paper", scopes, E._req(amount=50.0), global_capital_cap_usd=1000.0)
        self.assertEqual(paper.result, "allow", paper.reason_codes)             # 240 + 50 <= 1000
        own = _all(capital_deployed=0.0)
        own["ibkr_paper"] = E._scope(scope="ibkr_paper", capital_deployed=990.0)
        self.assertIn("global_capital_cap_exceeded",
                      _decide("ibkr_paper", own, E._req(amount=50.0), global_capital_cap_usd=1000.0).reason_codes)

    def test_combined_exposure(self):
        scopes = _all(capital_deployed=240.0)
        live = _decide("ibkr_live", scopes, E._req(amount=50.0), combined_exposure_cap_usd=500.0)
        self.assertIn("combined_exposure_cap_exceeded", live.reason_codes)
        paper = _decide("ibkr_paper", scopes, E._req(amount=50.0), combined_exposure_cap_usd=500.0)
        self.assertEqual(paper.result, "allow", paper.reason_codes)
        own = _all()
        own["ibkr_paper"] = E._scope(scope="ibkr_paper", capital_deployed=480.0)
        self.assertIn("combined_exposure_cap_exceeded",
                      _decide("ibkr_paper", own, E._req(amount=50.0), combined_exposure_cap_usd=500.0).reason_codes)

    def test_paper_exposure_still_counts_for_real_entries(self):
        scopes = _all(capital_deployed=0.0)
        scopes["ibkr_paper"] = E._scope(scope="ibkr_paper", capital_deployed=480.0)
        live = _decide("ibkr_live", scopes, E._req(amount=50.0), combined_exposure_cap_usd=500.0)
        self.assertIn("combined_exposure_cap_exceeded", live.reason_codes)     # unchanged, conservative

    def test_global_open_positions(self):
        scopes = _all(open_positions=3)                          # 12 across four
        self.assertIn("global_open_positions_exceeded",
                      _decide("ibkr_live", scopes, global_open_positions_cap=10).reason_codes)
        self.assertEqual(_decide("ibkr_paper", scopes, global_open_positions_cap=10).result, "allow")
        own = _all(open_positions=0)
        own["ibkr_paper"] = E._scope(scope="ibkr_paper", open_positions=10)
        self.assertIn("global_open_positions_exceeded",
                      _decide("ibkr_paper", own, global_open_positions_cap=10).reason_codes)

    def test_global_daily_loss(self):
        scopes = _all(realised_daily_loss=300.0)                 # 1200 across four
        self.assertIn("global_daily_loss_exceeded",
                      _decide("ibkr_live", scopes, global_daily_loss_cap_usd=1000.0).reason_codes)
        self.assertEqual(_decide("ibkr_paper", scopes, global_daily_loss_cap_usd=1000.0).result, "allow")
        own = _all(realised_daily_loss=0.0)
        own["ibkr_paper"] = E._scope(scope="ibkr_paper", realised_daily_loss=1001.0)
        self.assertIn("global_daily_loss_exceeded",
                      _decide("ibkr_paper", own, global_daily_loss_cap_usd=1000.0).reason_codes)

    def test_per_symbol_concentration(self):
        scopes = _all()
        scopes["ibkr_paper"] = E._scope(scope="ibkr_paper", positions=[_pos("AAPL", 4000.0)])
        scopes["ibkr_live"] = E._scope(scope="ibkr_live", positions=[_pos("AAPL", 5000.0)])
        req = E._req(symbol="AAPL", amount=200.0)
        self.assertIn("concentration_cap_exceeded",
                      _decide("ibkr_live", scopes, req, per_symbol_exposure_cap_usd=9000.0).reason_codes)
        paper = _decide("ibkr_paper", scopes, req, per_symbol_exposure_cap_usd=9000.0)
        self.assertEqual(paper.result, "allow", paper.reason_codes)              # 4000 + 200
        self.assertIn("concentration_cap_exceeded",
                      _decide("ibkr_paper", scopes, E._req(symbol="AAPL", amount=5001.0),
                              per_symbol_exposure_cap_usd=9000.0).reason_codes)  # its own 4000 + 5001

    def test_live_holding_never_gates_paper(self):
        """The operator's live holding (large, or entirely unknown) must not affect a paper entry."""
        for live in (E._scope(scope="ibkr_live", capital_deployed=1e9, open_positions=999,
                              realised_daily_loss=1e9, positions=[_pos("AAPL", 1e9)]),
                     E._scope(scope="ibkr_live", exposure_known=False, pnl_known=False)):
            scopes = _all()
            scopes["ibkr_live"] = live
            d = _decide("ibkr_paper", scopes, E._req(symbol="AAPL", amount=100.0),
                        global_capital_cap_usd=10_000.0, combined_exposure_cap_usd=10_000.0,
                        global_open_positions_cap=5, global_daily_loss_cap_usd=1000.0,
                        per_symbol_exposure_cap_usd=1000.0)
            self.assertEqual(d.result, "allow", d.reason_codes)

    def test_unknown_other_scopes_still_block_real_entries(self):
        for other in ("ibkr_paper", "etoro_paper", "etoro_real"):
            for kind in ("exposure_known", "pnl_known"):
                scopes = _all()
                scopes[other] = E._scope(scope=other, **{kind: False})
                d = _decide("ibkr_live", scopes, global_daily_loss_cap_usd=1000.0)
                self.assertEqual(d.result, "block", (other, kind))
                self.assertTrue(set(d.reason_codes) & CROSS_SCOPE_CODES, (other, kind, d.reason_codes))

    def test_unknown_other_scopes_never_block_paper_entries(self):
        for other in ("ibkr_live", "etoro_paper", "etoro_real"):
            for kind in ("exposure_known", "pnl_known"):
                scopes = _all()
                scopes[other] = E._scope(scope=other, **{kind: False})
                d = _decide("ibkr_paper", scopes, global_daily_loss_cap_usd=1000.0)
                self.assertEqual(d.result, "allow", (other, kind, d.reason_codes))

    def test_paper_own_unknown_fails_closed(self):
        for kind, codes in (("exposure_known", {"exposure_unknown", "combined_exposure_unknown"}),
                            ("pnl_known", {"daily_pnl_unknown", "global_daily_loss_unknown"})):
            scopes = _all()
            scopes["ibkr_paper"] = E._scope(scope="ibkr_paper", **{kind: False})
            d = _decide("ibkr_paper", scopes, global_daily_loss_cap_usd=1000.0)
            self.assertEqual(d.result, "block", kind)
            self.assertTrue(set(d.reason_codes) & codes, (kind, d.reason_codes))

    def test_paper_scope_missing_from_snapshot_fails_closed(self):
        view = eng._aggregate_view(E._snapshot(), "etoro_paper")
        self.assertIsNotNone(view.combined_capital_deployed)
        snap = E._snapshot()
        del snap.scopes["etoro_paper"]
        v = eng._aggregate_view(snap, "etoro_paper")
        self.assertIsNone(v.combined_capital_deployed)
        self.assertIsNone(v.combined_open_positions)
        self.assertIsNone(v.combined_realised_daily_loss)

    def test_etoro_paper_is_its_own_domain(self):
        scopes = _all(capital_deployed=400.0)
        d = _decide("etoro_paper", scopes, E._req(amount=50.0), combined_exposure_cap_usd=500.0)
        self.assertNotIn("combined_exposure_cap_exceeded", d.reason_codes)


# ── D: the real preflight on a temp database ───────────────────────────────────
class PreflightPaperNeedsNoLiveData(unittest.TestCase):

    def _run(self, scope, fresh, **policy_overrides):
        fx = F._DB()
        self.addCleanup(fx.cleanup)
        lane = {"ibkr_paper.auto_trading_enabled": True, "ibkr_paper.auto_trading_enabled_until_utc": _future()}
        with fx.conn() as c:
            save_policy(c, F._valid_policy(**{**lane, **policy_overrides}))
            for s in fresh:
                F._seed_fresh_state(c, scope=s)
        return run_risk_preflight(
            sqlite3.connect(fx.path), broker_scope=scope,
            request=TradeRequest(symbol="AAPL", amount_usd=100.0, side="long", sl=95.0, tp=110.0),
            current_authority=Authority.from_string("AUTO_ALLOWED"), actor="test",
            audit_source="auto", market_open=True)

    def test_paper_allowed_with_only_paper_state(self):
        r = self._run("ibkr_paper", ["ibkr_paper"])
        self.assertTrue(r.allowed, r.reason_codes)

    def test_paper_blocked_without_its_own_state(self):
        r = self._run("ibkr_paper", ["ibkr_live", "etoro_paper", "etoro_real"])
        self.assertFalse(r.allowed)
        self.assertTrue(set(r.reason_codes) & {"exposure_unknown", "daily_pnl_unknown", "combined_exposure_unknown"},
                        r.reason_codes)

    def test_live_still_needs_all_four(self):
        from unittest import mock
        lane = {"ibkr_live.auto_trading_enabled": True, "ibkr_live.auto_trading_enabled_until_utc": _future()}
        with mock.patch.dict("os.environ", {"IBKR_LIVE_CONFIRMED": "yes"}):    # reach the data gates
            r = self._run("ibkr_live", ["ibkr_live"], **lane)
        self.assertFalse(r.allowed)
        self.assertTrue(set(r.reason_codes) & {"combined_exposure_unknown", "global_open_positions_unknown",
                                               "global_daily_loss_unknown", "exposure_unknown"}, r.reason_codes)


# ── E: lane-based IBKR authority ───────────────────────────────────────────────
class LaneAuthority(unittest.TestCase):

    def _view(self, **overrides):
        return policy_view_from_allocation_policy(F._valid_policy(**overrides), env_overrides=False)

    def test_family_flag_alone_enables_neither_ibkr_scope(self):
        v = self._view(**{"ibkr.auto_trading_enabled": True})
        self.assertFalse(v.broker_auto_enabled["ibkr_paper"])
        self.assertFalse(v.broker_auto_enabled["ibkr_live"])

    def test_paper_lane_enables_paper_never_live(self):
        for family in (True, False):
            v = self._view(**{"ibkr.auto_trading_enabled": family,
                              "ibkr_paper.auto_trading_enabled": True,
                              "ibkr_paper.auto_trading_enabled_until_utc": _future()})
            self.assertTrue(v.broker_auto_enabled["ibkr_paper"], family)
            self.assertFalse(v.broker_auto_enabled["ibkr_live"], family)

    def _view_env(self, env, **overrides):
        from unittest import mock
        with mock.patch.dict("os.environ", env):
            if "IBKR_LIVE_CONFIRMED" not in env:
                import os
                os.environ.pop("IBKR_LIVE_CONFIRMED", None)
            return policy_view_from_allocation_policy(F._valid_policy(**overrides),
                                                      env_overrides=True)

    def test_live_needs_family_lane_and_confirmation(self):
        live_lane = {"ibkr_live.auto_trading_enabled": True, "ibkr_live.auto_trading_enabled_until_utc": _future()}
        yes = {"IBKR_LIVE_CONFIRMED": "yes"}
        fam = {"ibkr.auto_trading_enabled": True}
        live = lambda env, **o: self._view_env(env, **o).broker_auto_enabled["ibkr_live"]  # noqa: E731
        self.assertFalse(live(yes, **{"ibkr.auto_trading_enabled": False, **live_lane}))
        self.assertFalse(live(yes, **fam))
        self.assertFalse(live({}, **fam, **live_lane))                         # not confirmed
        for bad in ("", "no", "true", "1", "y"):
            self.assertFalse(live({"IBKR_LIVE_CONFIRMED": bad}, **fam, **live_lane), bad)
        self.assertTrue(live(yes, **fam, **live_lane))
        self.assertTrue(live({"IBKR_LIVE_CONFIRMED": " YES "}, **fam, **live_lane))
        # without env_overrides the confirmation is never read: unconfirmed
        from unittest import mock
        with mock.patch.dict("os.environ", yes):
            self.assertFalse(self._view(**fam, **live_lane).broker_auto_enabled["ibkr_live"])

    def test_paper_never_needs_live_confirmation(self):
        v = self._view_env({}, **{"ibkr_paper.auto_trading_enabled": True,
                                  "ibkr_paper.auto_trading_enabled_until_utc": _future()})
        self.assertTrue(v.broker_auto_enabled["ibkr_paper"])
        self.assertFalse(v.broker_auto_enabled["ibkr_live"])

    def test_bad_time_boxes_block(self):
        for until in (None, "", "garbage", _past(), "2099-01-01T00:00:00",            # naive
                      "2099-01-01T00:00:00+02:00", 1234567890):                     # non-UTC, not a string
            v = self._view(**{"ibkr_paper.auto_trading_enabled": True,
                              "ibkr_paper.auto_trading_enabled_until_utc": until})
            self.assertFalse(v.broker_auto_enabled["ibkr_paper"], until)
        v = self._view(**{"ibkr_paper.auto_trading_enabled": True,
                          "ibkr_paper.auto_trading_enabled_until_utc": "2099-01-01T00:00:00Z"})
        self.assertTrue(v.broker_auto_enabled["ibkr_paper"])

    def test_truthy_but_not_true_does_not_enable(self):
        for val in ("yes", 1, "true"):
            v = self._view(**{"ibkr_paper.auto_trading_enabled": val,
                              "ibkr_paper.auto_trading_enabled_until_utc": _future()})
            self.assertFalse(v.broker_auto_enabled["ibkr_paper"], val)

    def test_lane_kill_switch_kills_scope_and_family_kill_kills_both(self):
        v = self._view(**{"ibkr_paper.auto_trading_enabled": True,
                          "ibkr_paper.auto_trading_enabled_until_utc": _future(),
                          "ibkr_paper.kill_switch": True})
        self.assertTrue(v.broker_kill_switch["ibkr_paper"])
        self.assertFalse(v.broker_auto_enabled["ibkr_paper"])
        self.assertFalse(v.broker_kill_switch["ibkr_live"])
        v = self._view(**{"ibkr.kill_switch": True})
        self.assertTrue(v.broker_kill_switch["ibkr_paper"])
        self.assertTrue(v.broker_kill_switch["ibkr_live"])

    def test_global_auto_is_time_boxed(self):
        self.assertTrue(self._view().global_auto_enabled)
        for until in (None, _past(), "2099-01-01T00:00:00"):
            self.assertFalse(self._view(**{"global.auto_trading_enabled_until_utc": until}).global_auto_enabled, until)
        self.assertFalse(self._view(**{"global.auto_trading_enabled": "yes"}).global_auto_enabled)

    def test_decision_paper_lane_vs_live(self):
        pol = self._view(**{"ibkr_paper.auto_trading_enabled": True,
                            "ibkr_paper.auto_trading_enabled_until_utc": _future()})
        snap = E._snapshot()
        paper = decide(E._ctx(scope="ibkr_paper", authority=Authority.AUTO_ALLOWED), snap, E._req(amount=50.0), policy=pol)
        live = decide(E._ctx(scope="ibkr_live", authority=Authority.AUTO_ALLOWED), snap, E._req(amount=50.0), policy=pol)
        self.assertNotIn("broker_auto_disabled", paper.reason_codes)
        self.assertIn("broker_auto_disabled", live.reason_codes)


# ── F: parity with bot.broker_allocation ───────────────────────────────────────
class ParityWithD0(unittest.TestCase):
    CASES = (None, "", "x", 5, _future(), _past(), "2099-01-01T00:00:00Z", "2099-01-01T00:00:00+00:00",
             "2099-01-01T00:00:00", "2099-01-01T00:00:00+01:00", "2000-01-01T00:00:00Z",
             "2099-01-01T00:00:00.123456Z")

    def test_time_box_rule_identical(self):
        for raw in self.CASES:
            b = {"auto_trading_enabled_until_utc": raw}
            self.assertEqual(eng._d0_until_active(b), _until_utc_active(b), raw)

    def test_lane_rule_matches_is_auto_trading_allowed(self):
        base = F._valid_policy()
        base["routing"]["allowed_brokers"] = list(ALL_BROKER_SCOPES)
        for scope in ("ibkr_paper", "ibkr_live"):
            for enabled in (True, False, "yes", None):
                for until in self.CASES:
                    for kill in (False, True):
                        p = {**base, scope: {"auto_trading_enabled": enabled,
                                             "auto_trading_enabled_until_utc": until, "kill_switch": kill}}
                        d0_ok, _ = is_auto_trading_allowed(p, scope)
                        self.assertEqual(eng._d0_lane_authorised(p, scope), d0_ok, (scope, enabled, until, kill))


# ── G: structure ───────────────────────────────────────────────────────────────
class Structure(unittest.TestCase):

    def test_no_gate_reads_the_four_scope_view_directly(self):
        tree = ast.parse(ENGINE.read_text())
        offenders = []
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("_gate_")):
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and node.attr == "global_view":
                    offenders.append(fn.name)
        self.assertEqual(offenders, [])

    def test_aggregate_view_is_used_by_every_cross_scope_gate(self):
        src = ENGINE.read_text()
        for gate in ("_gate_global_capital", "_gate_combined_exposure", "_gate_global_open_positions",
                     "_gate_global_daily_loss", "_gate_concentration"):
            body = src[src.index("def %s(" % gate):]
            body = body[:body.index("\ndef ", 5)]
            self.assertIn("_aggregate_view(snap, ctx.broker_scope)", body, gate)

    def test_engine_imports_unchanged(self):
        tree = ast.parse(ENGINE.read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module)
            elif isinstance(n, ast.Import):
                mods.update(a.name for a in n.names)
        self.assertEqual({m for m in mods if m.startswith("bot")},
                         {"bot.risk_authority.authority", "bot.risk_authority.snapshot"})


if __name__ == "__main__":
    unittest.main()
