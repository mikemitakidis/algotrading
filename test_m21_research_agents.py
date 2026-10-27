"""test_m21_research_agents.py — lane E: research-agent roles, knowledge library and mock workflow (B21, B22, D06).

Offline and deterministic. The tests prove that:
- forbidden actions are denied to every role and logged;
- the knowledge library refuses unfalsifiable, duplicate and look-ahead
  hypotheses;
- the graveyard needs evidence and a re-test trigger;
- the mock workflow runs end to end without network access or executing
  any generated code.

Run via ``python _test_isolation.py test_m21_research_agents``.
"""
from __future__ import annotations

import builtins
import unittest
from dataclasses import replace

from research.agents import knowledge as K
from research.agents import mock as MK
from research.agents import roles as R
from research.integrity.ledger import Ledger, LedgerError, TrialRecord


def _lib():
    L = Ledger()
    g = R.Gate(L)
    return L, g, K.KnowledgeLibrary(L, g)


def _proposal(sids, **kw):
    base = dict(statement="Oversold large caps below the lower band revert within five sessions after costs",
                mechanism="liquidity-driven overreaction", prediction=K.Prediction("ret_diff", ">", 0.0),
                kill_criteria="no positive paired difference", source_ids=tuple(sids), template="mean_reversion",
                proposed_by="hypothesist", test_window_start="2018-01-02")
    base.update(kw)
    return K.Proposal(**base)


class E1_Roles(unittest.TestCase):
    def test_never_actions_are_denied_to_every_role_and_logged(self):
        L = Ledger()
        g = R.Gate(L)
        n = 0
        for role in R.ROLES:
            for action in sorted(R.NEVER):
                d = g.check(role, action)
                self.assertFalse(d.allowed, (role, action))
                n += 1
        self.assertEqual(len(L.entries("agent_decision")), n)
        self.assertTrue(L.verify().ok)

    def test_allow_lists_are_exact(self):
        g = R.Gate(Ledger())
        for role, spec in R.ROLES.items():
            for action in sorted(R.ACTIONS):
                self.assertEqual(g.check(role, action).allowed, action in spec.allowed, (role, action))
        self.assertFalse(g.check("intruder", "kb.read").allowed)
        self.assertFalse(g.check("scout", "teleport").allowed)
        with self.assertRaises(R.PermissionDenied):
            g.require("engineer", "code.execute")
        self.assertTrue(R.ROLES["auditor"].deterministic)
        self.assertNotIn("eval.synthetic", R.ROLES["hypothesist"].allowed)

    def test_never_list_holds_even_if_roles_are_altered_after_construction(self):
        g = R.Gate(Ledger())
        g.roles["scout"] = replace(R.ROLES["scout"], allowed=R.ROLES["scout"].allowed | {"network.fetch"})
        d = g.check("scout", "network.fetch")
        self.assertFalse(d.allowed)
        self.assertIn("never permitted", d.reason)

    def test_a_role_cannot_be_configured_with_a_forbidden_action(self):
        bad = dict(R.ROLES)
        bad["scout"] = replace(R.ROLES["scout"], allowed=R.ROLES["scout"].allowed | {"broker.order"})
        with self.assertRaises(ValueError):
            R.Gate(Ledger(), bad)


class E2_Knowledge(unittest.TestCase):
    def test_sources_are_validated_and_only_the_operator_verifies(self):
        L, g, kb = _lib()
        s = MK.FIXTURE_SOURCES[0]
        sid = kb.add_source("scout", s)
        with self.assertRaises(K.KnowledgeError):
            kb.add_source("scout", s)
        with self.assertRaises(K.KnowledgeError):
            kb.add_source("scout", replace(s, title="x", locator=""))
        with self.assertRaises(K.KnowledgeError):
            kb.add_source("scout", replace(s, title="y", published="2027-01-01", retrieved="2026-01-01"))
        with self.assertRaises(R.PermissionDenied):
            kb.add_source("hypothesist", replace(s, title="z"))
        with self.assertRaises(K.KnowledgeError):
            kb.verify_source(sid, operator="scout")
        kb.verify_source(sid, operator="operator:mike")
        self.assertEqual(len(L.entries("kb_source_verification")), 1)

    def test_proposals_must_be_falsifiable_sourced_and_point_in_time(self):
        L, g, kb = _lib()
        sids = [kb.add_source("scout", s) for s in MK.FIXTURE_SOURCES]
        cases = {
            "not falsifiable": _proposal(sids[:1], prediction=K.Prediction("ret", "~", 0.0)),
            "kill criteria missing": _proposal(sids[:1], kill_criteria=" "),
            "no sources cited": _proposal([]),
            "cites unknown source": _proposal(["SRC-nope"]),
            "look-ahead": _proposal([sids[4]]),
        }
        for why, p in cases.items():
            with self.assertRaises(K.KnowledgeError) as cm:
                kb.propose(p)
            self.assertIn(why, str(cm.exception))
        self.assertEqual(len(L.entries("kb_refusal")), len(cases))
        hid = kb.propose(_proposal(sids[:2]))
        h = [x for x in kb.hypotheses() if x["hypothesis_id"] == hid][0]
        self.assertEqual(h["knowledge_cutoff"], "1990-04-01")
        with self.assertRaises(R.PermissionDenied):
            kb.propose(_proposal(sids[:2], proposed_by="scout", statement="something else entirely new here"))

    def test_duplicates_refused_until_the_graveyard_trigger_fires(self):
        L, g, kb = _lib()
        sids = [kb.add_source("scout", s) for s in MK.FIXTURE_SOURCES[:2]]
        hid = kb.propose(_proposal(sids))
        reworded = _proposal(sids, statement="Oversold large-caps below the lower band revert within 5 sessions "
                                             "after the costs")
        self.assertGreater(K.similarity(reworded.statement, _proposal(sids).statement), K.DUPLICATE_THRESHOLD)
        with self.assertRaises(K.KnowledgeError):
            kb.propose(reworded)
        with self.assertRaises(K.KnowledgeError):
            kb.reject("librarian", hid, reason="x", evidence=[], retest_trigger={})
        with self.assertRaises(LedgerError):
            kb.reject("librarian", hid, reason="x", evidence=["T999999-deadbeef"], retest_trigger={"min_symbols": 50})
        tid = L.record_trial(TrialRecord(family_id="f", kind="research", spec_digest="s", data_digest="d",
                                         code_digest="c", seed=0, metrics={}))
        kb.reject("librarian", hid, reason="failed", evidence=[tid],
                  retest_trigger={"min_symbols": 50, "requires": ["real_data"]})
        self.assertEqual(kb.status(hid), "rejected")
        with self.assertRaises(K.KnowledgeError):
            kb.propose(reworded, context={"n_symbols": 100})
        with self.assertRaises(K.KnowledgeError):
            kb.propose(reworded, context={"n_symbols": 10, "flags": ["real_data"]})
        with self.assertRaises(K.KnowledgeError):
            kb.propose(reworded, distinct_from={hid: "it is really different"})     # no bypass of the graveyard
        new = kb.propose(reworded, context={"n_symbols": 100, "flags": ["real_data"]})
        self.assertNotEqual(new, hid)
        self.assertEqual(len(kb.graveyard()), 1)

    def test_distinct_ideas_need_a_ledgered_justification(self):
        L, g, kb = _lib()
        sids = [kb.add_source("scout", s) for s in MK.FIXTURE_SOURCES[:2]]
        hid = kb.propose(_proposal(sids))
        opposite = _proposal(sids, statement="Oversold large caps below the lower band continue within five "
                                             "sessions after costs")
        with self.assertRaises(K.KnowledgeError):
            kb.propose(opposite)
        with self.assertRaises(K.KnowledgeError):
            kb.propose(opposite, distinct_from={hid: "   "})
        new = kb.propose(opposite, distinct_from={hid: "continuation is the opposite prediction to reversion"})
        h = [x for x in kb.hypotheses() if x["hypothesis_id"] == new][0]
        self.assertEqual(h["distinct_from"][0]["near"], hid)
        self.assertEqual(K.similarity("Short horizon five sessions", "short-horizon 5 sessions"), 1.0)

    def test_lineage_needs_known_trials_and_the_right_role(self):
        L, g, kb = _lib()
        sids = [kb.add_source("scout", MK.FIXTURE_SOURCES[0])]
        hid = kb.propose(_proposal(sids))
        with self.assertRaises(LedgerError):
            kb.record_lineage("librarian", hypothesis_id=hid, genome_id="g", trial_ids=["T000000-x"])
        with self.assertRaises(R.PermissionDenied):
            kb.record_lineage("reporter", hypothesis_id=hid, genome_id="g")
        kb.record_lineage("librarian", hypothesis_id=hid, genome_id="g")
        self.assertEqual(kb.summary()["lineage"], 1)


class E3_MockWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        real_exec, real_eval = builtins.exec, builtins.eval

        def boom(*a, **k):
            raise AssertionError("the mock workflow must never execute code")
        builtins.exec, builtins.eval = boom, boom
        try:
            cls.r = MK.run(n_symbols=8, n_sessions=400)
        finally:
            builtins.exec, builtins.eval = real_exec, real_eval

    def test_guards_fire(self):
        joined = " | ".join(self.r.refusals)
        for s in ("network.fetch", "code.execute", "holdout.open", "duplicate of", "look-ahead"):
            self.assertIn(s, joined)
        self.assertEqual(len(self.r.refusals), 5)
        self.assertFalse(self.r.extra["lint"]["may_execute"])
        caps = {f["capability"] for f in self.r.extra["lint"]["capability_findings"]}
        self.assertIn("attr:system", caps)

    def test_governance_outcome(self):
        self.assertNotEqual(self.r.report_card["verdict"], "PASS")
        self.assertFalse(self.r.prereg_outcome["passed"])
        self.assertIn("not independently verified", self.r.prereg_outcome["reason"])
        self.assertEqual(list(self.r.kb["status"].values()), ["rejected"])
        self.assertTrue(self.r.ledger_ok)
        self.assertEqual(self.r.evaluation["n_markets"], 3)
        self.assertGreaterEqual(self.r.evaluation["positive_markets"], 2)
        self.assertIn("never executed", self.r.report)

    def test_deterministic(self):
        again = MK.run(n_symbols=8, n_sessions=400)
        self.assertEqual(again.digest, self.r.digest)


if __name__ == "__main__":
    unittest.main()
