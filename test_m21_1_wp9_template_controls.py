"""test_m21_1_wp9_template_controls.py — M21 WP9 Phase-1 template controls (A16).

Pins the versioned control store for the paper lane: fail-closed validation
(bounds, champion, lane), the propose -> approve -> activate lifecycle with a
named human approver, audit rows for every action and refusal, rollback,
integrity fallback (tampered row, unreadable store) that never raises, and
paper/live separation (selection never makes templates 2-4 tradable).
Offline only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bot.templates import controls as K
from bot.templates import routing as RT
from bot.templates.core import validate_params

REPO = Path(__file__).resolve().parent
T0 = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


def cfg(selected=("scanner", "mean_reversion"), champion="scanner", params=None, **extra):
    d = {"lane": "paper", "selected": list(selected), "champion": champion,
         "params": params if params is not None else {"mean_reversion": {"rsi_entry": 25.0}}}
    d.update(extra)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "controls.db")
        self.k = K.TemplateControls(self.path, clock=Clock())

    def tearDown(self):
        self.tmp.cleanup()

    def live(self, c=None, author="claude", approver="mike"):
        v = self.k.propose(c or cfg(), author=author, reason="test change")
        self.k.approve(v, approver=approver, reason="reviewed")
        self.k.activate(v, actor=approver)
        return v

    def actions(self):
        return [a["action"] for a in self.k.audit()]


class K1_Validation(Base):
    def test_normalises_defaults_and_order(self):
        n = K.validate_config(cfg(selected=["mean_reversion", "scanner", "mean_reversion"]))
        self.assertEqual(n["selected"], ["mean_reversion", "scanner"])
        self.assertEqual(n["params"]["mean_reversion"], validate_params("mean_reversion", {"rsi_entry": 25.0}))
        both = K.validate_config(cfg(selected=["scanner", "pullback_in_trend"], params={}))
        self.assertEqual(both["params"]["pullback_in_trend"], validate_params("pullback_in_trend"))

    def test_every_invalid_change_is_refused(self):
        bad = [cfg(selected=[]), cfg(selected=["scanner", "martingale"]), cfg(champion="breakout_momentum"),
               cfg(params={"mean_reversion": {"rsi_entry": 80.0}}), cfg(params={"mean_reversion": {"x": 1}}),
               cfg(params={"pullback_in_trend": {}}), cfg(params={"scanner": {"min_valid_tfs": 1}}),
               cfg(lane="live"), cfg(extra_key=1), cfg(params=[1])]
        for b in bad:
            with self.assertRaises(K.ControlError, msg=b):
                K.validate_config(b)
            with self.assertRaises(K.ControlError):
                self.k.propose(b, author="claude", reason="r")
        self.assertEqual(self.actions(), ["PROPOSE_REFUSED"] * len(bad))
        self.assertEqual(self.k.versions(), [])

    def test_author_and_reason_required(self):
        for a, r in (("", "r"), ("claude", ""), (None, "r")):
            with self.assertRaises(K.ControlError):
                self.k.propose(cfg(), author=a, reason=r)


class K2_Lifecycle(Base):
    def test_propose_approve_activate(self):
        v = self.live()
        a = K.read_active(self.path)
        self.assertEqual((a.version, a.source, a.fallback_reasons), (v, K.SOURCE_STORE, ()))
        self.assertEqual(a.config["params"]["mean_reversion"]["rsi_entry"], 25.0)
        self.assertEqual(self.actions(), ["PROPOSED", "APPROVED", "ACTIVATED"])
        self.assertEqual(self.k.versions()[0]["status"], K.ACTIVE)

    def test_only_approved_versions_activate_and_approval_is_human_and_once(self):
        v = self.k.propose(cfg(), author="claude", reason="r")
        with self.assertRaises(K.ControlError):
            self.k.activate(v, actor="mike")
        for who in ("claude", "System", "", "scheduler"):
            with self.assertRaises(K.ControlError):
                self.k.approve(v, approver=who, reason="ok")
        with self.assertRaises(K.ControlError):
            self.k.approve(v, approver="mike", reason="")
        self.k.approve(v, approver="mike", reason="ok")
        with self.assertRaises(K.ControlError):
            self.k.approve(v, approver="mike", reason="again")
        with self.assertRaises(K.ControlError):
            self.k.approve(99, approver="mike", reason="missing")
        self.assertEqual(K.read_active(self.path).source, K.SOURCE_DEFAULT)
        self.assertGreaterEqual(self.actions().count("APPROVE_REFUSED"), 6)
        self.assertIn("ACTIVATE_REFUSED", self.actions())

    def test_withdraw(self):
        v = self.k.propose(cfg(), author="claude", reason="r")
        self.k.withdraw(v, actor="mike", reason="not now")
        with self.assertRaises(K.ControlError):
            self.k.approve(v, approver="mike", reason="ok")
        with self.assertRaises(K.ControlError):
            self.k.withdraw(v, actor="mike", reason="twice")

    def test_versions_are_immutable_and_linked(self):
        v1 = self.live()
        v2 = self.live(cfg(selected=["scanner", "breakout_momentum"], params={}))
        rows = {r["version"]: r for r in self.k.versions()}
        self.assertEqual((rows[v1]["status"], rows[v2]["status"]), (K.SUPERSEDED, K.ACTIVE))
        self.assertEqual(rows[v2]["parent_version"], v1)
        self.assertEqual(K.read_active(self.path).config["selected"], ["scanner", "breakout_momentum"])


class K3_Rollback(Base):
    def test_rollback_restores_the_previous_activation(self):
        v1 = self.live()
        v2 = self.live(cfg(params={"mean_reversion": {"rsi_entry": 35.0}}))
        self.assertEqual(self.k.rollback(actor="mike", reason="worse"), v1)
        a = K.read_active(self.path)
        self.assertEqual((a.version, a.config["params"]["mean_reversion"]["rsi_entry"]), (v1, 25.0))
        self.assertEqual(self.actions()[-1], "ROLLED_BACK")
        self.assertEqual(self.k.rollback(actor="mike", reason="undo"), v2)   # a second rollback undoes the first

    def test_rollback_refusals(self):
        with self.assertRaises(K.ControlError):
            self.k.rollback(actor="mike", reason="nothing")
        self.live()
        with self.assertRaises(K.ControlError):
            self.k.rollback(actor="mike", reason="only one")
        with self.assertRaises(K.ControlError):
            self.k.rollback(actor="", reason="x")
        self.assertIn("ROLLBACK_REFUSED", self.actions())


class K4_IntegrityFallback(Base):
    def _tamper(self, version, **cols):
        c = sqlite3.connect(self.path)
        for k_, v in cols.items():
            c.execute("UPDATE template_config_versions SET %s=? WHERE version=?" % k_, (v, version))
        c.commit()
        c.close()

    def test_tampered_active_row_falls_back_to_last_good(self):
        v1 = self.live()
        v2 = self.live(cfg(params={"mean_reversion": {"rsi_entry": 35.0}}))
        evil = cfg(params={"mean_reversion": {"rsi_entry": 44.0}})
        self._tamper(v2, config_json=json.dumps(evil))
        a = K.read_active(self.path)
        self.assertEqual((a.version, a.source), (v1, K.SOURCE_LAST_GOOD))
        self.assertIn("version_%d_failed_integrity" % v2, a.fallback_reasons)

    def test_out_of_bounds_row_with_matching_digest_still_refused(self):
        v = self.live()
        bad = cfg(params={"mean_reversion": {"rsi_entry": 99.0}})
        self._tamper(v, config_json=K.canonical(bad), config_sha256=K.sha256(bad))
        a = K.read_active(self.path)
        self.assertEqual((a.source, a.config), (K.SOURCE_DEFAULT, K.default_config()))
        with self.assertRaises(K.ControlError):
            self.k.rollback(actor="mike", reason="x")

    def test_unreadable_or_absent_store_gives_the_default(self):
        self.assertEqual(K.read_active(os.path.join(self.tmp.name, "absent.db")).source, K.SOURCE_DEFAULT)
        junk = Path(self.tmp.name, "junk.db")
        junk.write_bytes(b"not a database" * 100)
        a = K.read_active(str(junk))
        self.assertEqual(a.config, K.default_config())
        self.assertTrue(a.fallback_reasons[0].startswith("store_unreadable"))
        self.assertEqual(K.read_active(self.path).fallback_reasons, ("no_activation",))

    def test_read_path_never_writes(self):
        self.live()
        before = Path(self.path).read_bytes()
        K.read_active(self.path)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_activation_refused_for_a_tampered_approved_row(self):
        v = self.k.propose(cfg(), author="claude", reason="r")
        self.k.approve(v, approver="mike", reason="ok")
        self._tamper(v, config_json=K.canonical(cfg(params={"mean_reversion": {"rsi_entry": 40.0}})))
        with self.assertRaises(K.ControlError):
            self.k.activate(v, actor="mike")


class K5_PaperLiveSeparation(Base):
    def test_selection_never_makes_a_template_tradable(self):
        self.live(cfg(selected=["scanner", "mean_reversion", "breakout_momentum", "pullback_in_trend"],
                      champion="mean_reversion", params={}))
        a = K.read_active(self.path)
        self.assertEqual(a.config["champion"], "mean_reversion")
        self.assertEqual(RT.LIVE_TEMPLATES, frozenset({"scanner"}))
        self.assertFalse(any(RT.is_live(t) for t in ("mean_reversion", "breakout_momentum", "pullback_in_trend")))

    def test_shadow_params_follow_the_active_config(self):
        self.live()
        p = K.shadow_params(K.read_active(self.path))
        self.assertEqual(sorted(p), ["mean_reversion"])
        self.assertEqual(p["mean_reversion"]["rsi_entry"], 25.0)
        self.assertEqual(K.shadow_params(K.read_active(os.path.join(self.tmp.name, "none.db"))), {})

    def test_ws2_hook_reads_controls_read_only(self):
        src = (REPO / "tools/paper_loop/m21_1extra_d2_3_scheduler.py").read_text()
        block = src[src.index("cands, _notes = loop_candidates(ranked)"):src.index("    return provider")]
        self.assertIn("read_active(", block)
        self.assertNotIn("TemplateControls(", block)                  # the loop never writes controls

    def test_store_path_required(self):
        with self.assertRaises(ValueError):
            K.TemplateControls("", clock=Clock())


if __name__ == "__main__":
    unittest.main()
