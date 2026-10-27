"""test_m21_research_integrity.py — lane B research integrity stack (B05–B08, B20).

Offline, deterministic. Proves the statistical machinery is correct on known
cases, that leakage and tampering are caught, that pre-registrations and
holdouts are one-shot, and that the control batteries tell a miscalibrated
pipeline from a calibrated one. Run via
``python _test_isolation.py test_m21_research_integrity``.
"""
from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import unittest
from itertools import combinations
from math import comb
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as st

from research.common.rng import stream
from research.integrity import evidence as EV
from research.integrity import multiple_testing as MT
from research.integrity import splits as SP
from research.integrity import stats as S
from research.integrity.controls import (BatteryPlan, PipelineDecision, block_permute,
                                         circular_shift, placebo_events, run_battery)
from research.integrity.authority import Authority
from research.integrity.holdout import RELEASE_PURPOSE, HoldoutDenied, HoldoutVault, release_request
from research.integrity.ledger import Hypothesis, Ledger, LedgerError, TrialRecord, code_digest
from research.integrity.pbo import pbo
from research.integrity.prereg import (ATTESTATION_PURPOSE, PreRegistration, attest, attestation_payload,
                                       evaluate_once, register)
from research.integrity.reality_check import hansen_spa, white_reality_check
from research.integrity.report_card import GauntletConfig, evaluate, unsupported_numbers


def _clock():
    n = [0]

    def f():
        n[0] += 1
        return "2026-10-27T00:00:%02d+00:00" % (n[0] % 60)
    return f


def _hyp(**kw):
    base = dict(family_id="fam", statement="s", mechanism="m", falsifier="f", expected_effect="e", author="a")
    base.update(kw)
    return Hypothesis(**base)


def _trial(**kw):
    base = dict(family_id="fam", kind="research", spec_digest="spec", data_digest="data", code_digest="code",
                seed=1, metrics={"sr": 0.1})
    base.update(kw)
    return TrialRecord(**base)


class B1_Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.sqlite"
        self.led = Ledger(self.path, clock=_clock())

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def test_append_only_enforced_by_database(self):
        self.led.append("note", {"x": 1})
        for sql in ("UPDATE entries SET payload='{}'", "DELETE FROM entries",
                    "UPDATE ledger_meta SET value='x'", "DELETE FROM ledger_meta"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.led._db.execute(sql)

    def test_chain_verifies_and_detects_edit_delete_reorder(self):
        for i in range(5):
            self.led.append("note", {"i": i})
        self.assertTrue(self.led.verify().ok)
        self.led.close()
        for mutation, expect in (
                ("UPDATE entries SET payload='{\"i\":99}' WHERE seq=3", 3),
                ("DELETE FROM entries WHERE seq=2", 3),
                ("UPDATE entries SET created_utc='2030-01-01T00:00:00+00:00' WHERE seq=4", 4),
                ("UPDATE entries SET kind='trial' WHERE seq=5", 5)):
            p2 = Path(self.tmp.name) / "copy.sqlite"
            p2.write_bytes(self.path.read_bytes())
            con = sqlite3.connect(p2)
            con.execute("DROP TRIGGER entries_no_update")
            con.execute("DROP TRIGGER entries_no_delete")
            con.execute(mutation)
            con.commit()
            con.close()
            v = Ledger(p2).verify()
            self.assertFalse(v.ok, mutation)
            self.assertEqual(v.first_bad_seq, expect, mutation)
            p2.unlink()
        self.led = Ledger(self.path, clock=_clock())

    def _copy_with(self, fn):
        p2 = Path(self.tmp.name) / "copy2.sqlite"
        if p2.exists():
            p2.unlink()
        p2.write_bytes(self.path.read_bytes())
        con = sqlite3.connect(p2)
        con.execute("DROP TRIGGER entries_no_update")
        con.execute("DROP TRIGGER entries_no_delete")
        fn(con)
        con.commit()
        con.close()
        return Ledger(p2)

    def test_rechained_tampering_and_anchored_tip(self):
        """An attacker who knows the hash recipe re-hashes what they edit:
        the next entry's prev link, the sequence check and an anchored tip
        each catch a different variant of that."""
        from research.integrity import ledger as LG
        for i in range(5):
            self.led.append("note", {"i": i})
        tip = self.led.tip_hash
        self.led.close()

        def edit_and_rehash(con):                    # edit seq 3 and recompute ONLY its hash
            seq, kind, ref, pj, cr, ph, _ = con.execute("SELECT * FROM entries WHERE seq=3").fetchone()
            pj2 = '{"i":42}'
            con.execute("UPDATE entries SET payload=?, entry_hash=? WHERE seq=3",
                        (pj2, LG._entry_hash(ph, seq, kind, ref, pj2, cr)))
        v = self._copy_with(edit_and_rehash).verify()
        self.assertFalse(v.ok)
        self.assertEqual(v.first_bad_seq, 4)
        self.assertIn("prev_hash", v.reason)

        def delete_and_rechain(con):                 # drop seq 2, re-link all later entries
            con.execute("DELETE FROM entries WHERE seq=2")
            prev = con.execute("SELECT entry_hash FROM entries WHERE seq=1").fetchone()[0]
            for row in con.execute("SELECT * FROM entries WHERE seq>2 ORDER BY seq").fetchall():
                seq, kind, ref, pj, cr, _, _ = row
                h = LG._entry_hash(prev, seq, kind, ref, pj, cr)
                con.execute("UPDATE entries SET prev_hash=?, entry_hash=? WHERE seq=?", (prev, h, seq))
                prev = h
        v = self._copy_with(delete_and_rechain).verify()
        self.assertFalse(v.ok)
        self.assertIn("sequence gap", v.reason)

        truncated = self._copy_with(lambda con: con.execute("DELETE FROM entries WHERE seq=5"))
        self.assertTrue(truncated.verify().ok)                      # undetectable locally ...
        self.assertFalse(truncated.verify(expected_tip=tip).ok)     # ... caught against the anchor
        self.led = Ledger(self.path, clock=_clock())
        self.assertTrue(self.led.verify(expected_tip=tip).ok)

    def test_jsonl_mirror_roundtrip_and_tamper(self):
        hid = self.led.register_hypothesis(_hyp())
        self.led.record_trial(_trial(hypothesis_id=hid, metrics={"sr": float("nan"), "n": 3}))
        j = Path(self.tmp.name) / "l.jsonl"
        self.assertEqual(self.led.export_jsonl(j), 3)
        self.assertTrue(Ledger.verify_jsonl(j).ok)
        lines = j.read_text().splitlines()
        rec = json.loads(lines[1])
        rec["payload"]["status"] = "supported"
        lines[1] = json.dumps(rec, sort_keys=True)
        j.write_text("\n".join(lines) + "\n")
        self.assertFalse(Ledger.verify_jsonl(j).ok)

    def test_trial_accounting_counts_failed_and_abandoned(self):
        for st_ in ("completed", "failed", "abandoned", "completed"):
            self.led.record_trial(_trial(status=st_))
        self.led.record_trial(_trial(kind="control_null"))
        self.led.record_trial(_trial(family_id="other"))
        self.assertEqual(self.led.family_trial_count("fam"), 4)
        self.assertEqual(self.led.family_trial_count("fam", include_controls=True), 5)
        self.assertEqual(len(self.led.family_metric("fam", "sr")), 4)

    def test_hypothesis_lifecycle_and_references(self):
        hid = self.led.register_hypothesis(_hyp())
        self.assertEqual(self.led.hypothesis_status(hid), "open")
        self.led.set_hypothesis_status(hid, "rejected", "failed gauntlet")
        self.assertEqual(self.led.hypothesis_status(hid), "rejected")
        with self.assertRaises(LedgerError):
            self.led.register_hypothesis(_hyp())                  # duplicate
        with self.assertRaises(LedgerError):
            self.led.set_hypothesis_status(hid, "promoted", "x")  # unknown status
        with self.assertRaises(LedgerError):
            self.led.record_trial(_trial(hypothesis_id="H-unknown"))
        t1 = self.led.record_trial(_trial(hypothesis_id=hid))
        t2 = self.led.record_trial(_trial(parent_trial_id=t1))
        with self.assertRaises(LedgerError):
            self.led.record_trial(_trial(parent_trial_id="T999999-x"))
        self.led.assert_ledgered([t1, t2])
        with self.assertRaises(LedgerError):
            self.led.assert_ledgered([t1, "T000000-missing"])
        with self.assertRaises(ValueError):
            _trial(kind="promoted")
        with self.assertRaises(LedgerError):
            self.led.append("trade", {})

    def test_reopen_preserves_chain(self):
        self.led.append("note", {"a": 1})
        tip = self.led.tip_hash
        self.led.close()
        led2 = Ledger(self.path, clock=_clock())
        self.assertEqual(led2.tip_hash, tip)
        led2.append("note", {"a": 2})
        self.assertTrue(led2.verify().ok)
        led2.close()
        self.led = Ledger(self.path, clock=_clock())

    def test_code_digest_is_content_based(self):
        d = Path(self.tmp.name)
        (d / "a.py").write_text("x = 1\n")
        (d / "b.py").write_text("y = 2\n")
        d1 = code_digest([d / "a.py", d / "b.py"])
        self.assertEqual(d1, code_digest([d / "b.py", d / "a.py"]))
        (d / "b.py").write_text("y = 3\n")
        self.assertNotEqual(d1, code_digest([d / "a.py", d / "b.py"]))


AUTH = Authority("operator-authority", b"k" * 32)
OTHER = Authority("operator-authority", b"z" * 32)          # same name, different key: a forger


def _signed_attest(led, d, ts, auth=AUTH):
    pl = attestation_payload(d, ts, "github_comment.created_at", "https://example.invalid/c/1")
    return attest(led, d, server_timestamp_utc=ts, source=pl["source"], evidence_url=pl["evidence_url"],
                  signature=auth.sign(ATTESTATION_PURPOSE, pl), verifier=AUTH.verifier())


class B2_PreregAndHoldout(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(clock=_clock())
        self.hid = self.led.register_hypothesis(_hyp())
        self.p = PreRegistration(family_id="fam", hypothesis_id=self.hid, spec_digest="spec-1", metric="dsr",
                                 comparator=">=", threshold=0.95, min_effective_n=30,
                                 forward_window_start_utc="2027-01-01T00:00:00+00:00", analysis_plan="plan")

    def test_one_shot_and_trusted_attestation_out_of_sample(self):
        d = register(self.led, self.p)
        self.assertEqual(_signed_attest(self.led, d, "2026-12-01T00:00:00+00:00"), "authority_signed")
        out = evaluate_once(self.led, d, value=0.97, effective_n=40,
                            data_first_ts_utc="2027-01-05T21:00:00+00:00", data_digest="x")
        self.assertTrue(out.passed)
        with self.assertRaises(LedgerError):
            evaluate_once(self.led, d, value=0.97, effective_n=40,
                          data_first_ts_utc="2027-01-05T21:00:00+00:00", data_digest="x")

    def test_declared_or_forged_attestations_never_pass(self):
        """PR #5 review P1: a caller-typed timestamp is not proof of time."""
        d = register(self.led, self.p)
        trust = attest(self.led, d, server_timestamp_utc="2026-12-01T00:00:00+00:00", source="me",
                       evidence_url="u")
        self.assertEqual(trust, "untrusted_declared")
        out = evaluate_once(self.led, d, value=0.99, effective_n=40,
                            data_first_ts_utc="2027-01-05T21:00:00+00:00", data_digest="x")
        self.assertFalse(out.passed)
        self.assertIn("not independently verified", out.reason)
        p2 = PreRegistration(**{**self.p.__dict__, "spec_digest": "spec-forged"})
        d2 = register(self.led, p2)
        with self.assertRaises(LedgerError):                       # forged signature refused outright
            _signed_attest(self.led, d2, "2026-12-01T00:00:00+00:00", auth=OTHER)
        pl = attestation_payload(d2, "2026-12-01T00:00:00+00:00", "gh", "u")
        sig = AUTH.sign(ATTESTATION_PURPOSE, pl)
        with self.assertRaises(LedgerError):                       # valid signature, altered timestamp
            attest(self.led, d2, server_timestamp_utc="2026-11-01T00:00:00+00:00", source="gh", evidence_url="u",
                   signature=sig, verifier=AUTH.verifier())
        with self.assertRaises(LedgerError):                       # signature without a verifier
            attest(self.led, d2, server_timestamp_utc="2026-12-01T00:00:00+00:00", source="gh", evidence_url="u",
                   signature=sig)
        p3 = PreRegistration(**{**self.p.__dict__, "spec_digest": "spec-diag"})
        d3 = register(self.led, p3)
        attest(self.led, d3, server_timestamp_utc="2026-12-01T00:00:00+00:00", source="me", evidence_url="u")
        diag = evaluate_once(self.led, d3, value=0.99, effective_n=40, require_trusted=False,
                             data_first_ts_utc="2027-01-05T21:00:00+00:00", data_digest="x")
        self.assertFalse(diag.passed)

    def test_fail_closed_without_proof_of_time(self):
        cases = []
        d = register(self.led, self.p)                              # no attestation
        cases.append(evaluate_once(self.led, d, value=0.99, effective_n=40,
                                   data_first_ts_utc="2027-02-01T00:00:00+00:00", data_digest="x"))
        p2 = PreRegistration(**{**self.p.__dict__, "spec_digest": "spec-2"})
        d2 = register(self.led, p2)
        _signed_attest(self.led, d2, "2027-01-02T00:00:00+00:00")
        cases.append(evaluate_once(self.led, d2, value=0.99, effective_n=40,
                                   data_first_ts_utc="2027-02-01T00:00:00+00:00", data_digest="x"))
        p3 = PreRegistration(**{**self.p.__dict__, "spec_digest": "spec-3"})
        d3 = register(self.led, p3)
        _signed_attest(self.led, d3, "2026-12-01T00:00:00+00:00")
        cases.append(evaluate_once(self.led, d3, value=0.99, effective_n=40,
                                   data_first_ts_utc="2026-12-15T00:00:00+00:00", data_digest="x"))
        p4 = PreRegistration(**{**self.p.__dict__, "spec_digest": "spec-4"})
        d4 = register(self.led, p4)
        _signed_attest(self.led, d4, "2026-12-01T00:00:00+00:00")
        cases.append(evaluate_once(self.led, d4, value=0.99, effective_n=10,
                                   data_first_ts_utc="2027-02-01T00:00:00+00:00", data_digest="x"))
        self.assertEqual([c.passed for c in cases], [False] * 4)
        self.assertIn("attestation", cases[0].reason)
        self.assertIn("after the forward window", cases[1].reason)
        self.assertIn("before the forward window", cases[2].reason)
        self.assertIn("effective N", cases[3].reason)

    def test_prereg_digest_changes_with_any_field_and_rejects_reuse(self):
        d1 = self.p.digest
        self.assertNotEqual(d1, PreRegistration(**{**self.p.__dict__, "threshold": 0.9}).digest)
        register(self.led, self.p)
        with self.assertRaises(LedgerError):
            register(self.led, self.p)
        with self.assertRaises(ValueError):
            PreRegistration(**{**self.p.__dict__, "max_evaluations": 2})

    def _vault(self):
        df = pd.DataFrame({"ts_utc": pd.date_range("2026-01-01", periods=10, freq="D", tz="UTC"), "v": range(10)})
        return HoldoutVault(self.led, "h1", df, "2026-01-08T00:00:00+00:00", authority=AUTH.verifier(),
                            clock=lambda: "2027-01-01T00:00:00+00:00"), df

    @staticmethod
    def _token(family, spec, d, requester, auth=AUTH):
        return auth.sign(RELEASE_PURPOSE, release_request("h1", family, spec, d, requester))

    def test_holdout_release_requires_authority_family_match_and_trusted_prereg(self):
        vault, df = self._vault()
        self.assertEqual(len(vault.research_view()), 7)
        self.assertTrue((pd.to_datetime(vault.research_view().ts_utc) < pd.Timestamp("2026-01-08", tz="UTC")).all())
        d = register(self.led, self.p)
        rel = dict(family_id="fam", spec_digest="spec-1", prereg_digest=d, requester="alice")
        denied = []
        for kw in ({"approval_token": None}, {"approval_token": "deadbeef"},
                   {"approval_token": self._token("fam", "spec-1", d, "alice", auth=OTHER)},
                   {"approval_token": self._token("fam", "spec-1", d, "bob")}):              # token for someone else
            with self.assertRaises(HoldoutDenied):
                vault.release(**rel, **kw)
            denied.append(kw)
        with self.assertRaisesRegex(HoldoutDenied, "trusted attestation"):              # prereg not attested yet
            vault.release(**rel, approval_token=self._token("fam", "spec-1", d, "alice"))
        attest(self.led, d, server_timestamp_utc="2026-12-01T00:00:00+00:00", source="me", evidence_url="u")
        with self.assertRaisesRegex(HoldoutDenied, "trusted attestation"):              # declared only
            vault.release(**rel, approval_token=self._token("fam", "spec-1", d, "alice"))
        _signed_attest(self.led, d, "2026-12-01T00:00:00+00:00")
        with self.assertRaisesRegex(HoldoutDenied, "different spec"):
            vault.release(family_id="fam", spec_digest="other-spec", prereg_digest=d, requester="alice",
                          approval_token=self._token("fam", "other-spec", d, "alice"))
        got = vault.release(**rel, approval_token=self._token("fam", "spec-1", d, "alice"))
        self.assertEqual(list(got.v), [7, 8, 9])
        with self.assertRaisesRegex(HoldoutDenied, "already spent"):
            vault.release(**{**rel, "requester": "bob"}, approval_token=self._token("fam", "spec-1", d, "bob"))
        self.assertEqual(len(self.led.entries("holdout_release", "h1")), 1)
        self.assertEqual(len(self.led.entries("holdout_access_denied", "h1")), 8)
        with self.assertRaises(LedgerError):
            HoldoutVault(self.led, "h1", df, "2026-01-08T00:00:00+00:00", authority=AUTH.verifier())

    def test_cross_family_release_is_refused(self):
        """PR #5 review P1: family A's pre-registration (shared spec) cannot
        open the holdout under family B's name, even with a signed token."""
        vault, _ = self._vault()
        dA = register(self.led, self.p)                              # family 'fam', spec-1
        _signed_attest(self.led, dA, "2026-12-01T00:00:00+00:00")
        with self.assertRaisesRegex(HoldoutDenied, "belongs to family"):
            vault.release(family_id="famB", spec_digest="spec-1", prereg_digest=dA, requester="alice",
                          approval_token=self._token("famB", "spec-1", dA, "alice"))
        with self.assertRaisesRegex(HoldoutDenied, "authority cannot request"):
            vault.release(family_id="fam", spec_digest="spec-1", prereg_digest=dA,
                          requester="operator-authority",
                          approval_token=self._token("fam", "spec-1", dA, "operator-authority"))
        for bad_token in (None, "deadbeef", self._token("fam", "spec-1", dA, "alice", auth=OTHER),
                          self._token("fam", "spec-1", dA, "bob"), self._token("famB", "spec-1", dA, "alice")):
            with self.assertRaisesRegex(HoldoutDenied, "release-authority approval"):
                vault.release(family_id="fam", spec_digest="spec-1", prereg_digest=dA, requester="alice",
                              approval_token=bad_token)
        vault.release(family_id="fam", spec_digest="spec-1", prereg_digest=dA, requester="alice",
                      approval_token=self._token("fam", "spec-1", dA, "alice"))
        self.assertEqual(set(vault.releases()), {"fam"})


def _events(n=200, h=5, seed=0):
    rng = stream(seed, "events")
    s = np.sort(rng.choice(np.arange(5 * n), size=n, replace=False))
    return s, s + rng.integers(1, h + 1, size=n)


class B3_Splits(unittest.TestCase):
    def test_purged_kfold_has_no_leakage(self):
        s, e = _events()
        folds = SP.purged_kfold(s, e, n_splits=5, embargo=3)
        self.assertEqual(len(folds), 5)
        covered = np.sort(np.concatenate([f.test for f in folds]))
        np.testing.assert_array_equal(covered, np.arange(len(s)))
        for f in folds:
            self.assertTrue(SP.leakage_report(f.train, f.test, s, e, embargo=3).clean)
        self.assertGreater(sum(f.purged for f in folds), 0)

    def test_unpurged_split_is_caught_by_verifier(self):
        s = np.arange(100)
        e = s + 10
        train, test = np.arange(0, 50), np.arange(50, 100)
        self.assertFalse(SP.leakage_report(train, test, s, e).clean)

    def test_walk_forward_trains_only_on_resolved_past(self):
        s, e = _events(300, h=8, seed=3)
        for exp in (True, False):
            folds = SP.walk_forward(s, e, n_folds=4, embargo=2, expanding=exp, min_train_groups=2)
            self.assertEqual(len(folds), 4)
            for f in folds:
                self.assertTrue(len(f.train) > 0)
                self.assertTrue((e[f.train] < s[f.test].min()).all())
                self.assertTrue(SP.leakage_report(f.train, f.test, s, e, embargo=2).clean)
            if not exp:
                self.assertLess(len(folds[-1].train), len(np.arange(s.size)) // 2)

    def test_cpcv_counts_and_paths(self):
        s, e = _events(240, h=4, seed=4)
        res = SP.cpcv(s, e, n_groups=6, n_test_groups=2, embargo=2)
        self.assertEqual(len(res.folds), comb(6, 2))
        self.assertEqual(res.n_paths, 2 * comb(6, 2) // 6)
        appearances = {g: 0 for g in range(6)}
        for f in res.folds:
            for g in f.test_groups:
                appearances[g] += 1
            self.assertTrue(SP.leakage_report(f.train, f.test, s, e, embargo=2).clean)
        self.assertEqual(set(appearances.values()), {comb(5, 1)})
        for path in res.paths:
            self.assertEqual(sorted(path), list(range(6)))
            for g, fi in path.items():
                self.assertIn(g, res.folds[fi].test_groups)
        used = [(g, fi) for p in res.paths for g, fi in p.items()]
        self.assertEqual(len(used), len(set(used)))

    def test_datetime_events_and_validation(self):
        ts = pd.date_range("2024-01-01", periods=50, freq="D", tz="UTC").values
        folds = SP.purged_kfold(ts, ts + np.timedelta64(3, "D"), n_splits=5)
        self.assertEqual(len(folds), 5)
        with self.assertRaises(ValueError):
            SP.purged_kfold(np.array([3, 1, 2]), np.array([4, 5, 6]), n_splits=2)
        with self.assertRaises(ValueError):
            SP.purged_kfold(np.array([1, 2, 3]), np.array([0, 5, 6]), n_splits=2)
        with self.assertRaises(ValueError):
            SP.cpcv(np.arange(10), np.arange(10), n_groups=4, n_test_groups=4)


class B4_Stats(unittest.TestCase):
    def test_psr_closed_form(self):
        self.assertAlmostEqual(S.probabilistic_sharpe_ratio(0.0, 100), 0.5)
        v = S.probabilistic_sharpe_ratio(0.1, 101, 0.0)
        self.assertAlmostEqual(v, st.norm.cdf(0.1 * 10 / math.sqrt(1 + 0.5 * 0.01)), places=12)
        self.assertLess(S.probabilistic_sharpe_ratio(0.1, 101, skew=-1.0, kurt=6.0), v)

    def test_expected_max_sharpe_matches_monte_carlo(self):
        rng = stream(1, "emax")
        n_obs, n_tr = 252, 50
        sims = rng.standard_normal((400, n_obs, n_tr))
        sr = sims.mean(axis=1) / sims.std(axis=1, ddof=1)
        mc = sr.max(axis=1).mean()
        an = S.expected_max_sharpe(n_tr, 1.0 / (n_obs - 1))
        self.assertAlmostEqual(an, mc, delta=0.1 * mc)
        self.assertEqual(S.expected_max_sharpe(1, 0.01), 0.0)

    def test_dsr_penalises_trials(self):
        x = stream(2, "dsr").normal(0.05, 1.0, 500)
        vals = [S.deflated_sharpe_ratio(x, n).dsr for n in (1, 10, 100, 1000)]
        self.assertTrue(all(a > b for a, b in zip(vals, vals[1:])))
        self.assertAlmostEqual(vals[0], S.deflated_sharpe_ratio(x, 1).psr0)

    def test_effective_trials(self):
        rng = stream(3, "eff")
        base = rng.standard_normal((500, 1))
        same = np.repeat(base, 10, axis=1) + 1e-6 * rng.standard_normal((500, 10))
        indep = rng.standard_normal((500, 10))
        blocks = np.repeat(rng.standard_normal((500, 3)), [4, 3, 3], axis=1) + 0.1 * rng.standard_normal((500, 10))
        self.assertLess(S.effective_trials_eigen(same), 1.1)
        self.assertGreater(S.effective_trials_eigen(indep), 8.5)
        self.assertAlmostEqual(S.effective_trials_eigen(blocks), 3.0, delta=0.4)
        self.assertEqual(S.effective_trials_cluster(blocks), 3)
        self.assertEqual(S.effective_trials_cluster(same), 1)
        self.assertEqual(S.effective_trials_cluster(indep), 10)

    def test_clopper_pearson_known_values(self):
        self.assertAlmostEqual(S.clopper_pearson(0, 5, one_sided_upper=True)[1], 1 - 0.05 ** 0.2, places=12)
        self.assertLess(S.clopper_pearson(0, 59, one_sided_upper=True)[1], 0.05)
        self.assertGreater(S.clopper_pearson(0, 58, one_sided_upper=True)[1], 0.05)
        lo, hi = S.clopper_pearson(5, 10)
        self.assertAlmostEqual(lo, 0.187086, places=5)
        self.assertAlmostEqual(hi, 0.812914, places=5)
        self.assertEqual(S.clopper_pearson(10, 10)[1], 1.0)
        with self.assertRaises(ValueError):
            S.clopper_pearson(3, 2)

    def test_newey_west_and_bootstrap(self):
        rng = stream(4, "nw")
        e = rng.standard_normal(2000)
        ar = np.empty(2000)
        ar[0] = e[0]
        for t in range(1, 2000):
            ar[t] = 0.7 * ar[t - 1] + e[t]
        naive = ar.std(ddof=1) / math.sqrt(len(ar))
        self.assertGreater(S.newey_west_se(ar, lags=20), 2.0 * naive)
        self.assertAlmostEqual(S.newey_west_se(e, lags=0), e.std(ddof=0) / math.sqrt(len(e)), places=10)
        i1 = S.stationary_bootstrap_indices(50, 5.0, stream(5, "sb"), 3)
        i2 = S.stationary_bootstrap_indices(50, 5.0, stream(5, "sb"), 3)
        np.testing.assert_array_equal(i1, i2)
        runs = np.mean(np.diff(i1, axis=1) == 1)
        self.assertGreater(runs, 0.7)                                       # mostly contiguous blocks
        est, lo, hi = S.bootstrap_ci(e, np.mean, stream(6, "ci"), n_boot=500)
        self.assertLess(lo, est)
        self.assertLess(est, hi)
        self.assertLess(lo, 0.0 + 4 * e.std() / math.sqrt(len(e)))

    def test_mean_test_drawdown_es(self):
        m, t, p = S.mean_test(np.r_[np.ones(50), -np.ones(50)] + 0.5, lags=0)
        self.assertAlmostEqual(m, 0.5)
        self.assertLess(p, 0.01)
        self.assertAlmostEqual(S.max_drawdown([1, -2, -1, 3, -1]), 3.0)
        self.assertAlmostEqual(S.expected_shortfall(np.arange(100) - 50.0, 0.95), -48.0)
        with self.assertRaises(ValueError):
            S.sharpe([1.0, np.nan])


class B5_SnoopingAndOverfitting(unittest.TestCase):
    def test_pbo_high_for_noise_low_for_real_winner(self):
        # A single noise matrix's PBO is widely dispersed; its average is near
        # or above 0.5 (selection is uninformative).
        noise_pbos = [pbo(stream(7, "pbo", str(i)).standard_normal((1000, 20)) * 0.01, n_blocks=10).pbo
                      for i in range(10)]
        self.assertGreater(np.mean(noise_pbos), 0.4)
        noise = stream(7, "pbo", "0").standard_normal((1000, 20)) * 0.01
        real = noise.copy()
        real[:, 0] += 0.004
        r = pbo(real, n_blocks=10)
        self.assertLess(r.pbo, 0.05)
        self.assertEqual(r.n_splits, comb(10, 5))
        with self.assertRaises(ValueError):
            pbo(noise, n_blocks=7)

    def test_reality_check_and_spa_size_and_power(self):
        rejections_rc = rejections_spa = 0
        for i in range(40):
            d = stream(8, "rc", str(i)).standard_normal((400, 15))
            rejections_rc += white_reality_check(d, stream(9, "b", str(i)), n_boot=300).p_value <= 0.05
            rejections_spa += hansen_spa(d, stream(10, "b", str(i)), n_boot=300).p_value <= 0.05
        self.assertLessEqual(rejections_rc, 6)
        self.assertLessEqual(rejections_spa, 6)
        d = stream(11, "rc").standard_normal((400, 15))
        d[:, 3] += 0.25
        self.assertLess(white_reality_check(d, stream(12, "b"), n_boot=500).p_value, 0.05)
        sp = hansen_spa(d, stream(13, "b"), n_boot=500)
        self.assertLess(sp.p_value, 0.05)
        self.assertEqual(sp.best_index, 3)

    def test_spa_is_not_diluted_by_bad_strategies(self):
        d = stream(14, "spa").standard_normal((400, 60))
        d[:, 0] += 0.12
        d[:, 1:] -= 0.5                                                 # many terrible models
        rc = white_reality_check(d, stream(15, "b"), n_boot=500).p_value
        spa = hansen_spa(d, stream(16, "b"), n_boot=500).p_value
        self.assertLessEqual(spa, rc)
        # Recentring (SPA_c) is what removes the dilution versus SPA_u.
        d2 = stream(23, "spa").standard_normal((300, 40))
        d2[:, 0] += 0.1
        d2[:, 1:] -= 0.3
        pc = hansen_spa(d2, stream(24, "b"), n_boot=800, variant="c").p_value
        pu = hansen_spa(d2, stream(24, "b"), n_boot=800, variant="u").p_value
        pl = hansen_spa(d2, stream(24, "b"), n_boot=800, variant="l").p_value
        self.assertLess(pc, pu)
        self.assertLessEqual(pl, pc)
        with self.assertRaises(ValueError):
            hansen_spa(d2, stream(24, "b"), variant="x")

    def test_multiple_testing_known_values_and_fdr(self):
        p = [0.01, 0.04, 0.03, 0.005]
        np.testing.assert_allclose(MT.adjust(p, "bonferroni"), [0.04, 0.16, 0.12, 0.02])
        np.testing.assert_allclose(MT.adjust(p, "holm"), [0.03, 0.06, 0.06, 0.02])
        np.testing.assert_allclose(MT.adjust(p, "bh"), [0.02, 0.04, 0.04, 0.02])
        by = MT.adjust(p, "by")
        np.testing.assert_allclose(by, np.minimum(np.array([0.02, 0.04, 0.04, 0.02]) * (1 + 1 / 2 + 1 / 3 + 1 / 4), 1))
        self.assertEqual(MT.test(p, "holm", 0.05).n_rejected, 2)
        # Step-up monotonicity matters here: raw BH ratios are 0.04, 0.022, 0.016.
        np.testing.assert_allclose(MT.adjust([0.01, 0.011, 0.012, 0.9], "bh"), [0.016, 0.016, 0.016, 0.9])
        np.testing.assert_allclose(MT.adjust([0.04, 0.01, 0.02], "holm"), [0.04, 0.03, 0.04])
        # BH controls FDR at 10 % in a 90-null / 10-signal simulation.
        fdps = []
        for i in range(200):
            rng = stream(17, "fdr", str(i))
            z = np.r_[rng.standard_normal(90), rng.standard_normal(10) + 3.0]
            rej = np.asarray(MT.test(st.norm.sf(z), "bh", 0.1).rejected)
            fdps.append(rej[:90].sum() / max(rej.sum(), 1))
        self.assertLess(np.mean(fdps), 0.12)
        with self.assertRaises(ValueError):
            MT.adjust([0.1, 1.2])


class B6_ControlsAndReportCard(unittest.TestCase):
    def test_battery_verdicts(self):
        never = lambda d: PipelineDecision(False)               # noqa: E731
        p59 = BatteryPlan("never", "never", n_nulls=59)
        self.assertEqual(p59.min_nulls_for_target(), 59)
        self.assertEqual(run_battery(p59, never, lambda s: s).verdict, "CALIBRATED")
        p5 = BatteryPlan("never5", "never", n_nulls=5)
        r5 = run_battery(p5, never, lambda s: s)
        self.assertEqual(r5.verdict, "NOT_CALIBRATED")          # 0/5 cannot bound 5 %
        self.assertAlmostEqual(r5.false_pass_upper, 1 - 0.05 ** 0.2, places=10)
        self.assertIn("need >= 59", r5.notes)
        coin = lambda s: PipelineDecision(bool(stream(s, "coin").random() < 0.1))   # noqa: E731
        self.assertEqual(run_battery(BatteryPlan("coin", "coin", n_nulls=200), coin, lambda s: s).verdict,
                         "NOT_CALIBRATED")
        weak = BatteryPlan("weak", "w", n_nulls=60, effect_sizes=(1.0,), n_per_effect=20)
        r = run_battery(weak, never, lambda s: s, lambda e, s: s)
        self.assertEqual(r.verdict, "UNDERPOWERED")

    def test_power_uses_lower_bound_at_every_required_effect(self):
        """PR #5 review P2: power is not the last effect's point estimate."""
        never = lambda d: PipelineDecision(False)               # noqa: E731
        hit_big = lambda d: PipelineDecision(d >= 2.0)          # noqa: E731  detects only the large effect
        mk = lambda e, s: e                                     # noqa: E731
        fine = BatteryPlan("p1", "x", n_nulls=60, effect_sizes=(1.0, 2.0), n_per_effect=40)
        self.assertEqual(run_battery(fine, hit_big, lambda s: 0.0, mk).verdict, "CALIBRATED")
        both = BatteryPlan("p2", "x", n_nulls=60, effect_sizes=(1.0, 2.0), n_per_effect=40, required_effects=(1.0, 2.0))
        self.assertEqual(run_battery(both, hit_big, lambda s: 0.0, mk).verdict, "UNDERPOWERED")
        # 9/10 detections: point estimate 0.9 >= 0.8 but the exact lower bound is < 0.8.
        calls = []

        def mk_counted(e, s):
            calls.append(s)
            return len(calls)                                   # 1..10
        nine = lambda d: PipelineDecision(d != 0.0 and d % 10 != 0)   # noqa: E731  misses the 10th
        small = BatteryPlan("p3", "x", n_nulls=60, effect_sizes=(1.0,), n_per_effect=10)
        r = run_battery(small, nine, lambda s: 0.0, mk_counted)
        self.assertEqual((r.power[0].detections, r.power[0].rate), (9, 0.9))
        self.assertLess(r.power[0].lo, 0.8)
        self.assertEqual(r.verdict, "UNDERPOWERED")
        del never
        with self.assertRaises(ValueError):
            BatteryPlan("p4", "x", n_nulls=5, effect_sizes=(1.0,), n_per_effect=5, required_effects=(3.0,))

    def test_battery_is_ledgered_plan_first_and_one_use(self):
        led = Ledger(clock=_clock())
        plan = BatteryPlan("x", "pipe", n_nulls=3)
        run_battery(plan, lambda d: PipelineDecision(False), lambda s: s, ledger=led)
        kinds = [e.kind for e in led.entries()]
        self.assertEqual(kinds, ["control_battery_plan", "control_battery_result"])
        self.assertEqual(led.entries()[0].ref_id, plan.digest)
        with self.assertRaisesRegex(LedgerError, "already used"):          # PR #5 review P2
            run_battery(plan, lambda d: PipelineDecision(True), lambda s: s, ledger=led)
        self.assertEqual(len(led.entries("control_battery_plan")), 1)

    def test_naive_search_fails_battery_honest_search_does_not_pass_noise(self):
        """The integrity stack catches a pipeline that picks the best of 12
        variants without correction, and the corrected pipeline passes far
        fewer nulls while still detecting a strong planted effect."""
        n = 30
        naive = run_battery(BatteryPlan("naive", "naive", n_nulls=n, base_seed=5), EV.naive_pipeline, EV.make_null)
        honest = run_battery(BatteryPlan("honest", "honest", n_nulls=n, effect_sizes=(0.4,), n_per_effect=6,
                                         min_power=0.6, base_seed=5),
                             EV.honest_pipeline, EV.make_null, EV.make_positive)
        self.assertEqual(naive.verdict, "NOT_CALIBRATED")
        self.assertGreaterEqual(naive.null_passes, 4)
        self.assertLessEqual(honest.null_passes, 1)
        self.assertGreaterEqual(honest.power[0].detections, 5)

    def test_negative_control_helpers(self):
        rng = stream(18, "nc")
        y = np.arange(20)
        bp = block_permute(y, 5, rng)
        self.assertEqual(sorted(bp), list(y))
        self.assertTrue(all(np.all(np.diff(bp[i:i + 5]) == 1) for i in range(0, 20, 5)))
        ev = placebo_events(100, 10, rng, warmup=20)
        self.assertEqual(len(np.unique(ev)), 10)
        self.assertTrue((ev >= 20).all())
        np.testing.assert_array_equal(circular_shift([1, 2, 3], 1), [3, 1, 2])

    def _calibrated(self):
        never = lambda d: PipelineDecision(False)               # noqa: E731
        return run_battery(BatteryPlan("c", "c", n_nulls=60), never, lambda s: s)

    @staticmethod
    def _ledgered(n, family="f"):
        led = Ledger(clock=_clock())
        ids = [led.record_trial(_trial(family_id=family, spec_digest="s%d" % i)) for i in range(n)]
        return led, ids

    def test_report_card_verdicts_fail_closed(self):
        rng = stream(19, "rc")
        strong = rng.normal(0.6, 1.0, 400)
        fam = np.column_stack([strong] + [rng.normal(0, 1, 400) for _ in range(5)])
        cfg = GauntletConfig(n_boot=300)
        led, ids = self._ledgered(6)
        good = evaluate(candidate_id="c1", family_id="f", gross=strong, cost_per_obs=0.05, n_trials=6,
                        rng=stream(20, "a"), family_matrix=fam, canary=self._calibrated(), cfg=cfg,
                        ledger=led, trial_ids=ids)
        self.assertEqual(good.verdict, "PASS", good.reasons)
        self.assertEqual(good.numbers["provenance_verified"], 1.0)
        no_canary = evaluate(candidate_id="c2", family_id="f", gross=strong, cost_per_obs=0.05, n_trials=6,
                             rng=stream(20, "b"), family_matrix=fam, canary=None, cfg=cfg)
        self.assertEqual(no_canary.verdict, "INCONCLUSIVE")
        noise = rng.normal(0.0, 1.0, (400, 50))
        best = noise[:, int(np.argmax(noise.mean(axis=0)))]
        bad = evaluate(candidate_id="c3", family_id="f", gross=best, cost_per_obs=0.0, n_trials=50,
                       rng=stream(20, "c"), family_matrix=noise, canary=self._calibrated(), cfg=cfg)
        self.assertEqual(bad.verdict, "FAIL")
        self.assertTrue(any("deflated Sharpe" in r for r in bad.reasons))
        costly = evaluate(candidate_id="c4", family_id="f", gross=rng.normal(0.08, 1, 400), cost_per_obs=0.1,
                          n_trials=1, rng=stream(20, "d"), family_matrix=fam, canary=self._calibrated(), cfg=cfg)
        self.assertEqual(costly.verdict, "FAIL")
        x = rng.normal(0.5, 1.0, 300)
        c = evaluate(candidate_id="c6", family_id="f", gross=x, cost_per_obs=0.2, n_trials=1,
                     rng=stream(20, "f"), cfg=cfg)
        self.assertAlmostEqual(c.numbers["mean_net_1x"], x.mean() - 0.2, places=10)
        self.assertAlmostEqual(c.numbers["mean_net_2x"], x.mean() - 0.4, places=10)
        self.assertLess(c.numbers["ci_lo_net_2x"], c.numbers["ci_lo_net_1x"])
        tiny = evaluate(candidate_id="c5", family_id="f", gross=[1.0] * 5, cost_per_obs=0.0, n_trials=1,
                        rng=stream(20, "e"), cfg=cfg)
        self.assertEqual(tiny.verdict, "INCONCLUSIVE")
        self.assertIn("Report card", good.to_markdown())
        self.assertEqual(good.digest, good.digest)

    def test_report_card_requires_ledger_provenance_for_pass(self):
        """PR #5 review P2: unverified n_trials / effective N / family inputs
        can never yield PASS."""
        rng = stream(25, "prov")
        strong = rng.normal(0.6, 1.0, 400)
        fam = np.column_stack([strong] + [rng.normal(0, 1, 400) for _ in range(5)])
        cfg = GauntletConfig(n_boot=200)
        canary = self._calibrated()
        common = dict(candidate_id="c", family_id="f", gross=strong, cost_per_obs=0.05, rng=stream(26, "a"),
                      family_matrix=fam, canary=canary, cfg=cfg)
        led, ids = self._ledgered(6)
        cases = {
            "no ledger": evaluate(n_trials=6, **common),
            "no ids": evaluate(n_trials=6, ledger=led, **common),
            "unknown id": evaluate(n_trials=6, ledger=led, trial_ids=ids + ["T999999-x"], **common),
            "understated trials": evaluate(n_trials=6, ledger=self._ledgered(9)[0],
                                           trial_ids=self._ledgered(9)[1], **common),
        }
        other_led, other_ids = self._ledgered(6, family="g")
        cases["other family"] = evaluate(n_trials=6, ledger=other_led, trial_ids=other_ids, **common)
        for name, card in cases.items():
            self.assertEqual(card.verdict, "INCONCLUSIVE", name)
            self.assertEqual(card.numbers["provenance_verified"], 0.0, name)
            self.assertTrue(any("not reconciled" in r for r in card.reasons), name)
        hi = evaluate(n_trials=6, ledger=led, trial_ids=ids, effective_n=1e9, **common)
        lo = evaluate(n_trials=6, ledger=led, trial_ids=ids, effective_n=12.0, **common)
        self.assertLessEqual(hi.numbers["effective_n"], 400)            # a claimed N cannot inflate
        self.assertEqual(lo.numbers["effective_n"], 12.0)
        self.assertEqual(lo.verdict, "INCONCLUSIVE")
        for bad in (dict(n_trials=0), dict(n_trials=2.5), dict(n_trials=True),
                    dict(n_trials=6, effective_n=float("nan")), dict(n_trials=6, effective_n=-3.0)):
            with self.assertRaises(ValueError):
                evaluate(**{**common, **bad})

    def test_unsupported_numbers_checker(self):
        rng = stream(21, "u")
        card = evaluate(candidate_id="c", family_id="f", gross=rng.normal(0.5, 1, 200), cost_per_obs=0.05,
                        n_trials=3, rng=stream(22, "x"), cfg=GauntletConfig(n_boot=200), canary=None)
        dsr = card.numbers["dsr"]
        ok = "The deflated Sharpe is %.3f with 3 trials at 2x costs and 95 %% confidence." % dsr
        self.assertEqual(unsupported_numbers(ok, card), ())
        bad = "It earned 37.5 %% a year and a Sharpe of 4.21."
        self.assertEqual(set(unsupported_numbers(bad, card)), {"37.5", "4.21"})


class B7_EvidenceRun(unittest.TestCase):
    def test_evidence_cli_small_run(self):
        with tempfile.TemporaryDirectory() as d:
            s = EV.run(Path(d), n_nulls=6, n_per_effect=2, effects=(0.4,), n_snoop=2)
            self.assertTrue(s["ledger"]["verified"])
            self.assertTrue(s["ledger"]["jsonl_verified"])
            self.assertFalse(s["ledger"]["tampered_copy_verified"])
            self.assertTrue(s["prereg"]["second_evaluation"].startswith("refused"))
            md = (Path(d) / "lane_b_evidence.md").read_text()
            self.assertIn("not any market", md)
            self.assertNotIn("%%", md)


if __name__ == "__main__":
    unittest.main()
