"""test_m21_research_data.py — lane F: data inventory, quality audit and snapshot manifests (B01, B02, E05).

Offline and deterministic. The quality audit is scored against synthetic
markets with KNOWN defects: unadjusted splits, missing bars and delistings.
Snapshot manifests must catch every kind of tampering. Run via
``python _test_isolation.py test_m21_research_data``.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from research.common.synthetic import MarketSpec, generate
from research.dataqa import inventory as INV
from research.dataqa import quality as Q
from research.dataqa import snapshot as SN
from bot.historical.quality import QF_BACKFILLED_GAP, QF_ZERO_VOLUME


def _corrupt_market(seed=21):
    return generate(MarketSpec(n_symbols=24, n_sessions=600, seed=seed, split_prob=0.4, missing_bar_prob=0.01,
                               delist_prob=0.25))


class F1_QualityAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _corrupt_market()
        cls.rep = Q.audit_panel(cls.m.bars)

    def test_unadjusted_splits_found_with_no_false_alarms(self):
        sy = self.m.symbols.set_index("symbol")
        tp = fp = fn = 0
        for s, q in self.rep["per_symbol"].items():
            truth = sy.loc[s, "split_at"]
            found = {pd.Timestamp(x["ts_utc"]) for x in q["suspected_splits"]}
            has = pd.notna(truth) and (pd.isna(sy.loc[s, "delisted_at"]) or truth <= sy.loc[s, "delisted_at"])
            if has and pd.Timestamp(truth) in found:
                tp += 1
            elif has:
                fn += 1
            fp += len(found - ({pd.Timestamp(truth)} if has else set()))
        self.assertGreater(tp, 3)
        self.assertEqual((fn, fp), (0, 0))

    def test_missing_bars_counted_exactly(self):
        union = pd.DatetimeIndex(sorted(set().union(*[set(pd.to_datetime(b["ts_utc"], utc=True))
                                                       for b in self.m.bars.values()])))
        total = 0
        for s, q in self.rep["per_symbol"].items():
            tr = self.m.truth[s]
            obs = tr["observed"].to_numpy()
            first, last = np.flatnonzero(obs).min(), np.flatnonzero(obs).max()
            ts = pd.to_datetime(tr["ts_utc"], utc=True)
            expected_missing = int(sum((not obs[i]) and ts.iloc[i] in union for i in range(first, last + 1)))
            self.assertEqual(q["missing_bars"], expected_missing, s)
            total += expected_missing
        self.assertGreater(total, 20)

    def test_delistings_flagged_as_ended_early(self):
        sy = self.m.symbols.set_index("symbol")
        for s, q in self.rep["per_symbol"].items():
            self.assertEqual(q["ended_early"], bool(pd.notna(sy.loc[s, "delisted_at"])), s)

    def test_clean_market_is_clean(self):
        clean = generate(MarketSpec(n_symbols=8, n_sessions=600, seed=21))
        rep = Q.audit_panel(clean.bars)
        self.assertEqual(rep["grades"]["clean"], 8, {s: q["reasons"] for s, q in rep["per_symbol"].items()})

    def test_hard_errors_unsorted_duplicates_stale_and_outliers(self):
        base = generate(MarketSpec(n_symbols=1, n_sessions=300, seed=3)).bars["S000"]
        bad = base.copy()
        bad.loc[10, "high"] = bad.loc[10, "low"] - 1.0
        bad.loc[20, "close"] = np.nan
        q = Q.audit_symbol(bad, "X")
        self.assertEqual(q.grade, "unusable")
        self.assertGreaterEqual(sum(q.hard_errors.values()), 2)
        self.assertEqual(Q.audit_symbol(base.iloc[::-1].reset_index(drop=True), "X").grade, "unusable")
        dup = pd.concat([base, base.iloc[[50]]]).sort_values("ts_utc").reset_index(drop=True)
        qd = Q.audit_symbol(dup, "X")
        self.assertEqual(qd.duplicates, 1)
        stale = base.copy()
        stale.loc[100:106, ["open", "high", "low", "close"]] = stale.loc[100, "close"]
        qs = Q.audit_symbol(stale, "X")
        self.assertGreaterEqual(qs.stale_runs, 1)
        self.assertGreaterEqual(qs.zero_range_bars, 7)
        spike = base.copy()
        spike.loc[150, ["high", "close"]] = spike.loc[150, "close"] * 1.8
        qo = Q.audit_symbol(spike, "X")
        self.assertGreaterEqual(qo.outliers, 1)
        self.assertEqual(qo.grade, "usable_with_warnings")
        self.assertEqual(Q.audit_symbol(base.drop(columns=["volume"]), "X").grade, "unusable")


class F1b_FlagsAndKnowledgeTiming(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = generate(MarketSpec(n_symbols=1, n_sessions=300, seed=3)).bars["S000"]

    def test_a_single_flagged_bar_is_never_graded_clean(self):
        """Second review P1/P2: non-zero quality flags must affect the grade and admissibility."""
        self.assertEqual(Q.audit_symbol(self.base, "X").grade, "clean")
        for bit, kind, excluded in ((QF_ZERO_VOLUME, "zero_volume (warning bit)", 0),
                                    (QF_BACKFILLED_GAP, "backfilled_gap (BLOCKING bit", 1),
                                    (1 << 9, "unknown_bits (BLOCKING bit", 1)):
            b = self.base.copy()
            b.loc[100, "quality_flags"] = bit
            q = Q.audit_symbol(b, "X")
            self.assertEqual(q.grade, "usable_with_warnings", bit)
            self.assertTrue(any(kind in r for r in q.reasons), (bit, q.reasons))
            view, counts = Q.research_view(b)
            self.assertEqual(counts["excluded"], excluded, bit)
            self.assertEqual(len(view), len(b) - excluded)
            if excluded:
                self.assertNotIn(b["ts_utc"].iloc[100], set(view["ts_utc"]))

    def test_research_view_excludes_hard_invalid_rows(self):
        b = self.base.copy()
        b.loc[10, "high"] = b.loc[10, "low"] - 1.0
        b.loc[20, "close"] = np.nan
        b.loc[30, "volume"] = -5
        view, counts = Q.research_view(b)
        self.assertEqual(counts["excluded"], 3)
        self.assertTrue(Q.admissible_mask(view).all())

    def test_point_in_time_audit_and_split_flags_ignore_the_future(self):
        m = _corrupt_market(seed=21)
        sy = m.symbols.set_index("symbol")
        split_syms = [s for s in m.bars if pd.notna(sy.loc[s, "split_at"]) and pd.isna(sy.loc[s, "delisted_at"])]
        self.assertTrue(split_syms)
        hits = 0
        for s in split_syms:
            b = m.bars[s]
            t_split = pd.Timestamp(sy.loc[s, "split_at"])
            before = Q.audit_symbol_asof(b, s, t_split - pd.Timedelta(1, unit="D"))
            self.assertEqual(before.suspected_splits, [], s)
            self.assertTrue(before.knowledge.startswith("as_of:"))
            flags = Q.causal_split_flags(b)
            i = int(np.flatnonzero(pd.to_datetime(b["ts_utc"], utc=True) == t_split)[0])
            hits += bool(flags[i])
            fut = b.copy()
            fut.loc[i + 1:, ["open", "high", "low", "close"]] *= 0.37            # rewrite the future
            fut.loc[i + 1:, "volume"] *= 9.0
            np.testing.assert_array_equal(Q.causal_split_flags(fut)[:i + 1], flags[:i + 1])
        self.assertGreaterEqual(hits / len(split_syms), 0.75)          # causal recall is lower than retrospective
        others = [s for s in m.bars if pd.isna(sy.loc[s, "split_at"])]
        self.assertLessEqual(sum(int(Q.causal_split_flags(m.bars[s]).sum()) for s in others), 1)
        self.assertEqual(Q.audit_panel(m.bars)["knowledge"], "retrospective_full_sample")


class F1c_CausalSplitFlagUnit(unittest.TestCase):
    def test_flag_at_bar_i_uses_only_bars_up_to_i(self):
        ts = pd.date_range("2024-01-02", periods=8, freq="D", tz="UTC") + pd.Timedelta(21, unit="h")
        close = [100.0] * 5 + [50.0, 50.0, 50.0]
        open_ = [100.0] * 5 + [50.2, 50.0, 50.0]                       # 2:1 split at bar 5
        vol = [100.0] * 5 + [160.0, 10000.0, 10000.0]                   # bar 5 volume vs PRIOR median: 1.6x
        df = pd.DataFrame({"ts_utc": ts, "open": open_, "high": [max(a, b) for a, b in zip(open_, close)],
                           "low": [min(a, b) for a, b in zip(open_, close)], "close": close, "volume": vol})
        full = Q.causal_split_flags(df)
        self.assertTrue(full[5])
        for j in range(len(df)):
            np.testing.assert_array_equal(Q.causal_split_flags(df.iloc[:j + 1]), full[:j + 1])


class F2_Snapshots(unittest.TestCase):
    def _store(self, d):
        m = generate(MarketSpec(n_symbols=3, n_sessions=100, seed=4))
        for s, b in m.bars.items():
            p = Path(d) / "synthetic" / "1D" / ("%s.parquet" % s)
            p.parent.mkdir(parents=True, exist_ok=True)
            b.to_parquet(p, index=False)
        return m

    def test_manifest_detects_every_kind_of_tampering(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
            self._store(d)
            man = SN.build(d, name="test")
            self.assertEqual(len(man["files"]), 3)
            self.assertEqual(man["files"][0]["rows"], 100)
            self.assertEqual(SN.verify(man, d), [])
            mp = SN.write(man, Path(out) / "manifest.json")
            self.assertEqual(SN.verify(json.loads(mp.read_text()), d), [])
            f = Path(d) / "synthetic" / "1D" / "S001.parquet"
            raw = bytearray(f.read_bytes())
            raw[len(raw) // 2] ^= 0xFF
            f.write_bytes(bytes(raw))
            (Path(d) / "synthetic" / "1D" / "S002.parquet").unlink()
            (Path(d) / "extra.csv").write_text("a\n1\n")
            probs = SN.verify(man, d)
            self.assertIn("modified: synthetic/1D/S001.parquet", probs)
            self.assertIn("missing: synthetic/1D/S002.parquet", probs)
            self.assertIn("unexpected: extra.csv", probs)
            forged = dict(man, name="renamed")
            self.assertTrue(any("manifest edited" in p for p in SN.verify(forged, d)))

    def test_manifest_digest_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self._store(a)
            self._store(b)
            self.assertEqual(SN.build(a)["snapshot_digest"], SN.build(b)["snapshot_digest"])


class F3_Inventory(unittest.TestCase):
    def test_store_inventory_reads_m16_layout(self):
        with tempfile.TemporaryDirectory() as d:
            m = _corrupt_market(seed=5)
            for s, b in list(m.bars.items())[:6]:
                p = Path(d) / "synthetic" / "1D" / ("%s.parquet" % s)
                p.parent.mkdir(parents=True, exist_ok=True)
                b.to_parquet(p, index=False)
            before = sorted(Path(d).rglob("*"))
            inv = INV.inventory_store(d)
            self.assertEqual(sorted(Path(d).rglob("*")), before, "inventory must not write")
            self.assertEqual(inv["files"], 6)
            self.assertEqual(inv["groups"], [{"provider": "synthetic", "timeframe": "1D", "symbols": 6,
                                              "rows": int(sum(len(b) for b in list(m.bars.values())[:6]))}])
            self.assertEqual(inv["datasets"][0]["median_step_minutes"], 1440.0)
            self.assertEqual(inv["quality"]["synthetic/1D"]["symbols"], 6)
            (Path(d) / "synthetic" / "1D" / "BROKEN.parquet").write_bytes(b"x")
            inv2 = INV.inventory_store(d)
            self.assertEqual((inv2["files"], inv2["unreadable"]), (7, 1))
        self.assertFalse(INV.inventory_store("/nonexistent/path/for/test")["exists"])

    def test_repo_inventory(self):
        inv = INV.inventory_repo(Path(__file__).resolve().parent)
        self.assertTrue(inv["data_dir_exists"])
        self.assertIn("universe", inv)
        self.assertIn("finding", inv)
        self.assertEqual(inv["has_bars"], bool(inv["bar_files"]))


if __name__ == "__main__":
    unittest.main()
