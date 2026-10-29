"""M21 E4 — score-versus-outcome report from the candidate ledger.

Ledgers are built with the production A02 writer (``migrate`` +
``CandidateLedgerWriter``) in a temporary directory; the report reads them
read-only. Outcomes are fixtures: these tests prove the accounting and the
evidence labels, never any market property.
"""
import contextlib
import hashlib
import io
import json
import math
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from bot.candidate_ledger import CandidateLedgerWriter, migrate
from tools.candidate_ledger import score_outcome as SO

NOW = "2026-10-29T15:00:00+00:00"
T0 = datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*a)


class Ledger:
    def __init__(self, tc):
        d = tempfile.mkdtemp(prefix="e4_")
        tc.addCleanup(shutil.rmtree, d, True)
        self.dir = d
        self.path = os.path.join(d, "ledger.db")
        with sqlite3.connect(self.path) as c:
            migrate(c, now_utc=NOW)
        self.w = CandidateLedgerWriter(lambda: sqlite3.connect(self.path), enabled=True)
        self.n = 0

    def add(self, score, *, kind="trade", src="ibkr_paper", outcome="WIN", r=1.0, fees_final=1,
            scored=True, broker_src=None, outcome_src=None, trade_id=None):
        """kind: trade | open | intent_only | not_selected | scoring_blocked | risk_rejected |
        broker_not_accepted | resolved_without_record"""
        self.n += 1
        at = (T0 + timedelta(minutes=self.n)).isoformat()
        cid = self.w.open_candidate(symbol="S%03d" % self.n, direction="long", strategy_id="scanner:v1",
                                    decision_ts_utc=at, cycle_key="scan:%s" % at, scanner_stage="final_signal",
                                    at_utc=at, payload={} if scored else {"score": score})
        assert cid
        if scored:
            assert self.w.record(cid, "scored", at_utc=at, payload={
                "research_rank": score, "selectable": kind != "scoring_blocked"})
        if kind == "scoring_blocked":
            assert self.w.close(cid, "scoring_blocked", at_utc=at)
            return cid
        if kind == "not_selected":
            assert self.w.close(cid, "not_selected", at_utc=at)
            return cid
        verdict = "reject" if kind == "risk_rejected" else "pass"
        assert self.w.record(cid, "risk_evaluated", at_utc=at, payload={"verdict": verdict, "evidence": "M14_LIVE"})
        if kind == "risk_rejected":
            assert self.w.close(cid, "risk_rejected", at_utc=at)
            return cid
        assert self.w.record(cid, "intent_logged", at_utc=at, payload={"data_source": src})
        if kind == "intent_only":
            return cid
        status = "rejected" if kind == "broker_not_accepted" else "accepted"
        assert self.w.record(cid, "broker_result", at_utc=at,
                             payload={"status": status, "data_source": broker_src or src})
        if kind == "broker_not_accepted":
            assert self.w.close(cid, "broker_not_accepted", at_utc=at)
            return cid
        if kind == "open":
            return cid
        if kind == "resolved_without_record":
            assert self.w.close(cid, "outcome_resolved", at_utc=at)
            return cid
        assert self.w.record(cid, "outcome_resolved", at_utc=at, payload={
            "outcome": outcome, "trade_id": trade_id or "T-%03d" % self.n, "r_multiple": r,
            "realized_pnl": 10.0 * r, "fees_final": fees_final, "data_source": outcome_src or src})
        assert self.w.close(cid, "outcome_resolved", at_utc=at)
        return cid

    def rows(self, matched=()):
        c = SO._open_ro(self.path)
        try:
            return SO.load_candidates(c, matched_trade_ids=matched)
        finally:
            c.close()

    def trades(self, n, *, start=40.0, slope=1.0, src="ibkr_paper"):
        """n resolved trades; R rises with score when slope > 0, falls when < 0, constant pattern if 0."""
        ids = []
        for i in range(n):
            score = start + i * (55.0 / max(n - 1, 1))
            r = slope * (i - n / 2) / n if slope else (0.3 if i % 2 else -0.3)
            ids.append("T-%03d" % (self.n + 1))
            self.add(score, src=src, outcome="WIN" if r > 0 else "LOSS", r=r)
        return ids


class ClassesAndVerdicts(unittest.TestCase):
    def test_classes_are_kept_apart_and_only_statement_matched_trades_get_a_verdict(self):
        L = Ledger(self)
        ids = L.trades(40, slope=1.0, src="ibkr_paper")
        L.trades(40, slope=1.0, src="simulated")
        L.trades(5, slope=1.0, src="ibkr_paper")                     # journal-only
        rep = SO.report(L.rows(matched=ids), min_n=30)
        C = rep["classes"]
        self.assertEqual(C[SO.IBKR_MATCHED]["resolved_trades"], 40)
        self.assertEqual(C[SO.IBKR_PAPER]["resolved_trades"], 5)
        self.assertEqual(C[SO.SIMULATED]["resolved_trades"], 40)
        self.assertEqual(C[SO.IBKR_MATCHED]["verdict"], SO.POS_ASSOC)
        self.assertEqual(C[SO.SIMULATED]["verdict"], SO.NOT_MARKET)          # same strong pattern, still not evidence
        self.assertEqual(C[SO.IBKR_PAPER]["verdict"], SO.NOT_CONFIRMED)
        self.assertNotIn(SO.NO_ORDER, C)
        self.assertIn("not predictive", rep["caveat"])

    def test_journal_only_trades_never_get_an_association_verdict(self):
        L = Ledger(self)
        L.trades(60, slope=1.0)
        rep = SO.report(L.rows(), min_n=30)
        self.assertEqual(rep["classes"][SO.IBKR_PAPER]["verdict"], SO.NOT_CONFIRMED)
        self.assertEqual(rep["classes"][SO.IBKR_MATCHED]["resolved_trades"], 0)
        self.assertIn("none supplied", rep["statement_reconciliation"])

    def test_insufficient_below_min_n(self):
        L = Ledger(self)
        ids = L.trades(29, slope=1.0)
        rep = SO.report(L.rows(matched=ids), min_n=30)
        self.assertEqual(rep["classes"][SO.IBKR_MATCHED]["trades_with_score_and_r"], 29)
        self.assertEqual(rep["classes"][SO.IBKR_MATCHED]["verdict"], SO.INSUFFICIENT)

    def test_exactly_min_n_is_enough(self):
        L = Ledger(self)
        ids = L.trades(30, slope=1.0)
        self.assertEqual(SO.report(L.rows(matched=ids), min_n=30)["classes"][SO.IBKR_MATCHED]["verdict"],
                         SO.POS_ASSOC)

    def test_negative_and_no_association(self):
        L = Ledger(self)
        ids = L.trades(40, slope=-1.0)
        self.assertEqual(SO.report(L.rows(matched=ids))["classes"][SO.IBKR_MATCHED]["verdict"], SO.NEG_ASSOC)
        L2 = Ledger(self)
        ids2 = L2.trades(40, slope=0.0)                                  # alternating R, no rank relation
        c = SO.report(L2.rows(matched=ids2))["classes"][SO.IBKR_MATCHED]
        self.assertEqual(c["verdict"], SO.NO_ASSOC)
        self.assertGreaterEqual(c["association"]["permutation_p"], 0.05)

    def test_weak_in_sample_pattern_is_not_an_association(self):
        """rho about 0.25 on 40 trades, permutation p about 0.11: above 0.05, so no association is stated."""
        L = Ledger(self)
        ids = []
        for i in range(40):
            r = math.sin(i * 1.7) + 0.015 * i
            ids.append("T-%03d" % (L.n + 1))
            L.add(40 + i * 55 / 39, outcome="WIN" if r > 0 else "LOSS", r=r)
        c = SO.report(L.rows(matched=ids))["classes"][SO.IBKR_MATCHED]
        self.assertGreater(c["association"]["spearman_rho_score_vs_r"], 0.2)
        self.assertTrue(0.05 < c["association"]["permutation_p"] < 0.2)
        self.assertEqual(c["verdict"], SO.NO_ASSOC)

    def test_unrecognised_outcome_is_not_a_resolved_trade(self):
        L = Ledger(self)
        L.add(70, outcome="PENDING")
        c = SO.report(L.rows())["classes"][SO.IBKR_PAPER]
        self.assertEqual(c["resolved_trades"], 0)
        self.assertEqual(c["missing_outcomes"]["resolved_terminal_without_outcome_record"], 1)

    def test_conflicting_or_missing_sources_are_unknown_never_pooled(self):
        L = Ledger(self)
        L.add(70, src="ibkr_paper", outcome_src="simulated")
        L.add(70, src="ibkr_paper", broker_src="simulated", kind="open")
        L.add(70, src="mystery")
        rows = L.rows()
        self.assertEqual([r["execution_class"] for r in rows], [SO.UNKNOWN_SOURCE] * 3)
        self.assertEqual(rows[0]["data_sources"], ["ibkr_paper", "simulated"])

    def test_a_matched_id_on_a_simulated_trade_does_not_make_it_genuine(self):
        L = Ledger(self)
        L.add(70, src="simulated", trade_id="T-X")
        self.assertEqual(L.rows(matched=["T-X"])[0]["execution_class"], SO.SIMULATED)


class FunnelAndMissing(unittest.TestCase):
    def test_funnel_rejected_and_missing_outcomes(self):
        L = Ledger(self)
        L.add(80)                                           # resolved
        L.add(75, outcome="LOSS", r=-1.0, fees_final=0)     # resolved, fees not final
        L.add(72, kind="open")                              # accepted, awaiting / missing outcome
        L.add(71, kind="intent_only")                       # intent, no broker result
        L.add(69, kind="resolved_without_record")           # contradiction
        L.add(68, kind="broker_not_accepted")
        L.add(55, kind="not_selected")
        L.add(45, kind="scoring_blocked")
        L.add(65, kind="risk_rejected")
        rep = SO.report(L.rows())
        f = rep["funnel_by_execution_class"]
        self.assertEqual(f[SO.IBKR_PAPER], {"outcome_resolved": 3, "open": 2, "broker_not_accepted": 1})
        self.assertEqual(f[SO.NO_ORDER], {"not_selected": 1, "scoring_blocked": 1, "risk_rejected": 1})
        c = rep["classes"][SO.IBKR_PAPER]
        m = c["missing_outcomes"]
        self.assertEqual(c["resolved_trades"], 2)
        self.assertEqual(c["outcomes"], {"WIN": 1, "LOSS": 1})
        self.assertEqual(m["accepted_without_resolved_outcome"], 1)
        self.assertEqual(m["intent_without_broker_result"], 1)
        self.assertEqual(m["resolved_terminal_without_outcome_record"], 1)
        self.assertEqual(c["fees_unconfirmed_in_resolved"], 1)
        nt = rep["not_traded"]
        self.assertEqual(nt["count"], 4)
        self.assertIn({"terminal_kind": "scoring_blocked", "score_bin": "40-50", "count": 1},
                      nt["by_terminal_and_score_bin"])
        self.assertIn({"terminal_kind": "broker_not_accepted", "score_bin": "60-70", "count": 1},
                      nt["by_terminal_and_score_bin"])
        self.assertIn("not observed", nt["note"])

    def test_score_source_and_bins(self):
        L = Ledger(self)
        L.add(61.0, scored=False)                    # loop-opened: score from the scanned payload
        L.add(None, scored=False)
        rows = L.rows()
        self.assertEqual((rows[0]["score"], rows[0]["score_source"]), (61.0, "scanned.score"))
        self.assertEqual((rows[1]["score"], rows[1]["score_source"]), (None, None))
        self.assertEqual([SO._bin(x) for x in (None, 0, 39.99, 40, 59.999, 60, 89.9, 90, 100, 101, -1)],
                         ["no_score", "0-40", "0-40", "40-50", "50-60", "60-70", "80-90", "90-100", "90-100",
                          "out_of_range", "out_of_range"])
        rep = SO.report(rows)
        bins = [b["score_bin"] for b in rep["classes"][SO.IBKR_PAPER]["score_bins"]]
        self.assertEqual(bins, ["60-70", "no_score"])


class Statistics(unittest.TestCase):
    def test_wilson(self):
        self.assertEqual(SO.wilson(0, 0), (None, None))
        lo, hi = SO.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=4)
        self.assertAlmostEqual(hi, 0.7634, places=4)
        lo, hi = SO.wilson(10, 10)
        self.assertAlmostEqual(lo, 0.7225, places=4)
        self.assertAlmostEqual(hi, 1.0)

    def test_bin_table_numbers(self):
        L = Ledger(self)
        for r in (2.0, 1.0, -1.0, -1.0):
            L.add(65, outcome="WIN" if r > 0 else "LOSS", r=r)
        b = SO.report(L.rows())["classes"][SO.IBKR_PAPER]["score_bins"][0]
        self.assertEqual((b["trades"], b["wins"], b["n_with_r"]), (4, 2, 4))
        self.assertAlmostEqual(b["win_rate"], 0.5)
        self.assertAlmostEqual(b["mean_r"], 0.25)
        half = 1.96 * 1.5 / 2.0                      # sample sd 1.5, n 4
        self.assertAlmostEqual(b["mean_r_ci95"][0], 0.25 - half)
        self.assertAlmostEqual(b["mean_r_ci95"][1], 0.25 + half)

    def test_spearman(self):
        rho, p = SO.spearman(range(20), [x * x for x in range(20)])
        self.assertAlmostEqual(rho, 1.0)
        self.assertLess(p, 0.01)
        rho, _ = SO.spearman([1, 2, 2, 3], [4, 3, 3, 1])
        self.assertAlmostEqual(rho, -1.0)                          # ties averaged
        self.assertEqual(SO.spearman([1, 1, 1, 1], [1, 2, 3, 4]), (None, None))
        self.assertEqual(SO.spearman([1, 2], [1, 2]), (None, None))
        self.assertEqual(SO.spearman(range(10), range(10)), SO.spearman(range(10), range(10)))   # seeded


class ReadOnlyCli(unittest.TestCase):
    def test_cli_reads_without_writing_and_uses_statement_matches(self):
        L = Ledger(self)
        ids = L.trades(30, slope=1.0)
        before = sha(L.path)
        m = os.path.join(L.dir, "matches.json")
        with open(m, "w") as fh:
            json.dump({"schema": "m21-e5-reconciliation/1", "matched_trade_ids": ids}, fh)
        out_j, out_m = os.path.join(L.dir, "r.json"), os.path.join(L.dir, "r.md")
        rc = quiet(SO.main, ["--ledger-db", L.path, "--statement-matches", m, "--out-json", out_j,
                             "--out-md", out_m])
        self.assertEqual(rc, 0)
        self.assertEqual(sha(L.path), before)
        with open(out_j) as fh:
            rep = json.load(fh)
        self.assertEqual(rep["classes"][SO.IBKR_MATCHED]["verdict"], SO.POS_ASSOC)
        self.assertIn("30 matched trades", rep["statement_reconciliation"])
        with open(out_m) as fh:
            md = fh.read()
        self.assertIn("IBKR_PAPER_STATEMENT_MATCHED — verdict **POSITIVE_ASSOCIATION_IN_SAMPLE**", md)
        self.assertIn("not predictive", md)

    def test_unreadable_statement_matches_refused(self):
        L = Ledger(self)
        bad = os.path.join(L.dir, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{\"schema\": 1}")
        self.assertEqual(quiet(SO.main, ["--ledger-db", L.path, "--statement-matches", bad]), 2)

    def test_connection_is_read_only(self):
        L = Ledger(self)
        c = SO._open_ro(L.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                c.execute("DELETE FROM candidate_ledger")
        finally:
            c.close()

    def test_missing_database_is_not_created(self):
        d = tempfile.mkdtemp(prefix="e4_")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "absent.db")
        self.assertEqual(quiet(SO.main, ["--ledger-db", p]), 2)
        self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
