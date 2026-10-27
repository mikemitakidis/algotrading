"""test_m21_research_foundation.py — shared research foundation (Issues #2 / #3).

Covers the offline research package boundary and the shared building blocks
every lane depends on: canonical hashing, named RNG streams, OHLCV
conventions/resampling and the ground-truth synthetic market generator.

Offline only: no network, no broker, no production data, no writes outside
temp dirs. Run via ``python _test_isolation.py test_m21_research_foundation``.
"""
from __future__ import annotations

import ast
import time
import unittest
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from research import boundary
from research.common import bars as B
from research.common import hashing as H
from research.common import rng as R
from research.common.synthetic import MarketSpec, PlantedEffect, RegimeSpec, generate

REPO = Path(__file__).resolve().parent


class F1_ImportBoundary(unittest.TestCase):
    def test_research_tree_respects_boundary(self):
        violations = boundary.scan_tree(REPO / "research")
        self.assertEqual([str(v) for v in violations], [])

    def test_classify_rejects_dangerous_modules(self):
        for mod in ("bot.brokers", "bot.brokers.ibkr", "bot.paper_loop_d2.scheduler",
                    "bot.paper", "bot.etoro.paper_broker", "bot.notifier", "bot.kill_switch",
                    "bot.config", "bot.scanner", "bot.data", "bot.market_clock",
                    "bot.risk_authority.engine", "bot.historical.providers_yfinance",
                    "bot.historical.refresh", "ib_insync", "alpaca", "yfinance", "requests",
                    "socket", "urllib.request", "http.client", "subprocess", "dotenv",
                    "dashboard.app", "main", "tools.paper_loop", "telegram", "smtplib"):
            self.assertTrue(boundary.classify(mod), mod)

    def test_classify_allows_research_libraries(self):
        for mod in ("bot", "bot.ml.features", "bot.ml.labels.triple_barrier",
                    "bot.backtesting.indicators", "bot.backtesting.execution",
                    "bot.signal_scoring.provenance", "bot.historical.store",
                    "numpy", "pandas", "scipy.stats", "sklearn.ensemble", "xgboost",
                    "research.common.synthetic", "json", "sqlite3", "urllib.parse"):
            self.assertEqual(boundary.classify(mod), "", mod)

    def test_scan_catches_from_imports_and_dynamic_imports(self):
        src = ("from bot import brokers\n"
               "import importlib\n"
               "importlib.import_module('bot.paper_loop_d2')\n"
               "__import__(name)\n"
               "from bot.ml import features\n")
        got = [(v.line, v.module) for v in boundary.scan_source(src)]
        self.assertIn((1, "bot.brokers"), got)
        self.assertIn((3, "bot.paper_loop_d2"), got)
        self.assertIn((4, "<dynamic import>"), got)
        self.assertFalse(any(line == 5 for line, _ in got))

    def test_lint_is_not_a_sandbox_known_bypasses(self):
        """Known escapes need no forbidden import: the import lint does not
        see them, capability_findings flags them for a human, and nothing
        here ever authorises execution (review of PR #4, P1)."""
        bypasses = {
            "os_system": "import os\nos.system('id')\n",
            "file_read": "open('/etc/passwd').read()\n",
            "pathlib": "from pathlib import Path\nPath('x').read_text()\n",
            "environ": "import os\nsecret = os.environ['X']\n",
            "eval": "eval('1+1')\n",
            "exec": "exec('x = 1')\n",
            "getattr": "import os\ngetattr(os, 'sys' + 'tem')('id')\n",
            "introspection": "().__class__.__bases__[0].__subclasses__()\n",
        }
        for name, src in bypasses.items():
            self.assertEqual(boundary.scan_source(src), [], name)          # the lint does not catch it
            self.assertTrue(boundary.capability_findings(src), name)       # reviewer aid flags it
            self.assertFalse(boundary.may_execute_untrusted(src), name)
            with self.assertRaises(boundary.NotASandboxError):
                boundary.vet_for_execution(src)
        self.assertFalse(boundary.may_execute_untrusted("x = 1\n"))       # even trivially clean code
        self.assertEqual(boundary.SECURITY_SCOPE, "structural_import_lint_only_not_a_sandbox")
        doc = boundary.__doc__.lower()
        self.assertIn("not a sandbox", doc)
        self.assertNotIn("vet generated code before it can be run", doc)

    def test_no_production_code_imports_research(self):
        roots = [REPO / "bot", REPO / "dashboard", REPO / "tools", REPO / "server_scripts"]
        files = [p for r in roots if r.exists() for p in r.rglob("*.py") if "__pycache__" not in p.parts]
        files += [REPO / "main.py"] if (REPO / "main.py").exists() else []
        offenders = []
        for p in files:
            with warnings.catch_warnings():          # pre-existing escape warnings
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(p.read_text(), filename=str(p))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    mods = [node.module or ""]
                if any(m == "research" or m.startswith("research.") for m in mods):
                    offenders.append("%s:%d" % (p.relative_to(REPO), node.lineno))
        self.assertEqual(offenders, [])

    def test_research_has_no_repo_data_path_literals(self):
        """Research code never hard-codes the production data directory."""
        hits = []
        for p in (REPO / "research").rglob("*.py"):
            for node in ast.walk(ast.parse(p.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and (node.value.startswith("data/") or "/data/" in node.value
                             or node.value.startswith(".env")):
                    hits.append("%s:%d %r" % (p.relative_to(REPO), node.lineno, node.value))
        self.assertEqual(hits, [])

    def test_g10_allow_lists_admit_research_paths(self):
        for f in ("test_m17_backtesting.py", "test_m18_ml.py"):
            src = (REPO / f).read_text()
            for prefix in ('"research/"', '"docs/research/"', '"test_m21_research_"'):
                self.assertIn(prefix, src, "%s lacks %s" % (f, prefix))


class F2_Hashing(unittest.TestCase):
    def test_digest_is_key_order_invariant_and_type_normalised(self):
        a = {"b": np.float64(1.5), "a": [np.int64(2), (3, 4)], "c": {"z": True}}
        b = {"c": {"z": np.bool_(True)}, "a": [2, [3, 4]], "b": 1.5}
        self.assertEqual(H.digest(a), H.digest(b))
        self.assertNotEqual(H.digest(a), H.digest({**b, "b": 1.5000001}))

    def test_non_finite_values_hash_deterministically(self):
        x = {"v": float("nan"), "w": np.inf}
        self.assertEqual(H.digest(x), H.digest({"w": float("inf"), "v": np.nan}))
        self.assertEqual(H.to_plain(x), {"v": "nan", "w": "inf"})

    def test_frame_digest_sensitive_to_content_not_index(self):
        df = pd.DataFrame({"a": [1.0, 2.0], "b": ["x", "y"]})
        d0 = H.frame_digest(df)
        self.assertEqual(d0, H.frame_digest(df.set_axis([10, 11])))
        self.assertNotEqual(d0, H.frame_digest(df.assign(a=[1.0, 2.0000001])))
        self.assertNotEqual(d0, H.frame_digest(df.iloc[::-1]))
        self.assertNotEqual(d0, H.frame_digest(df.rename(columns={"a": "c"})))


class F3_RngStreams(unittest.TestCase):
    def test_streams_reproducible_and_independent(self):
        a1 = R.stream(5, "x", "y").standard_normal(8)
        a2 = R.stream(5, "x", "y").standard_normal(8)
        b = R.stream(5, "x", "z").standard_normal(8)
        c = R.stream(6, "x", "y").standard_normal(8)
        np.testing.assert_array_equal(a1, a2)
        self.assertFalse(np.allclose(a1, b))
        self.assertFalse(np.allclose(a1, c))

    def test_child_seed_is_31_bit_and_stable(self):
        s = R.child_seed(9, "model", "xgb")
        self.assertEqual(s, R.child_seed(9, "model", "xgb"))
        self.assertTrue(0 <= s < 2 ** 31)
        with self.assertRaises(ValueError):
            R.stream(-1, "x")


def _bars(ts, o, h, l, c, v):
    return pd.DataFrame({"ts_utc": pd.to_datetime(ts, utc=True), "open": o, "high": h,
                         "low": l, "close": c, "volume": v, "quality_flags": 0})


class F4_Bars(unittest.TestCase):
    def test_validate_flags_each_violation(self):
        ok = _bars(["2024-01-02 21:00", "2024-01-03 21:00"], [10, 11], [12, 12], [9, 10], [11, 11.5], [100, 200])
        self.assertEqual(B.validate_ohlcv(ok), [])
        self.assertTrue(B.validate_ohlcv(ok.drop(columns=["volume"])))
        self.assertTrue(B.validate_ohlcv(ok.assign(high=[10.5, 12])))       # high < close
        self.assertTrue(B.validate_ohlcv(ok.assign(low=[11.5, 10])))        # low > open
        self.assertTrue(B.validate_ohlcv(ok.assign(close=[11, np.nan])))
        self.assertTrue(B.validate_ohlcv(ok.assign(volume=[-1, 2])))
        self.assertTrue(B.validate_ohlcv(ok.iloc[::-1]))
        naive = ok.assign(ts_utc=ok.ts_utc.dt.tz_localize(None))
        self.assertTrue(B.validate_ohlcv(naive))

    def test_resample_session_buckets_and_values(self):
        m = generate(MarketSpec(n_symbols=1, n_sessions=3, timeframe="15m", seed=1))
        b15 = m.bars["S000"]
        mt = B.multi_timeframe(b15)
        self.assertEqual(len(mt["1D"]), 3)
        self.assertEqual(len(mt["4H"]), 6)
        self.assertEqual(len(mt["1H"]), 21)
        day0 = b15[b15.ts_utc.dt.date == b15.ts_utc.dt.date.iloc[0]]
        d = mt["1D"].iloc[0]
        self.assertEqual(d.ts_utc, day0.ts_utc.max())
        self.assertAlmostEqual(d.high, day0.high.max())
        self.assertAlmostEqual(d.low, day0.low.min())
        self.assertAlmostEqual(d.open, day0.open.iloc[0])
        self.assertAlmostEqual(d.close, day0.close.iloc[-1])
        self.assertAlmostEqual(d.volume, day0.volume.sum())
        first_h = mt["1H"].iloc[0]
        self.assertEqual(first_h.ts_utc, pd.Timestamp("2018-01-02 15:30", tz="UTC"))
        self.assertAlmostEqual(first_h.high, day0.high.iloc[:4].max())
        for v in mt.values():
            self.assertEqual(B.validate_ohlcv(v), [])
        with self.assertRaises(ValueError):
            B.resample(mt["1H"], "15m", source_tf="1H")

    def test_resample_ors_quality_flag_bits(self):
        """Comprehensive audit (PR #4): quality_flags is a bitmask; max() lost bits (max(1, 8) = 8 drops
        zero-volume). Aggregation is a bitwise OR at every coarser timeframe, with the real M16 constants."""
        from bot.historical.quality import QF_BACKFILLED_GAP, QF_DUPLICATE_KEPT, QF_OUTLIER_WARNED, QF_ZERO_VOLUME
        m = generate(MarketSpec(n_symbols=1, n_sessions=2, timeframe="15m", seed=1))
        b = m.bars["S000"].copy()
        b["quality_flags"] = 0
        b.loc[0, "quality_flags"] = QF_ZERO_VOLUME                      # first 1H bucket of day 0
        b.loc[1, "quality_flags"] = QF_BACKFILLED_GAP                   # same 1H bucket
        b.loc[5, "quality_flags"] = QF_OUTLIER_WARNED | QF_DUPLICATE_KEPT   # second 1H bucket, first 4H bucket
        b.loc[20, "quality_flags"] = 1 << 12                            # unknown bit, second 4H bucket (26 bars per session)
        mt = B.multi_timeframe(b)
        self.assertEqual(int(mt["1H"]["quality_flags"].iloc[0]), QF_ZERO_VOLUME | QF_BACKFILLED_GAP)
        self.assertEqual(int(mt["1H"]["quality_flags"].iloc[1]), QF_OUTLIER_WARNED | QF_DUPLICATE_KEPT)
        self.assertEqual(int(mt["4H"]["quality_flags"].iloc[0]),
                         QF_ZERO_VOLUME | QF_BACKFILLED_GAP | QF_OUTLIER_WARNED | QF_DUPLICATE_KEPT)
        self.assertEqual(int(mt["4H"]["quality_flags"].iloc[1]), 1 << 12)
        self.assertEqual(int(mt["1D"]["quality_flags"].iloc[0]),
                         QF_ZERO_VOLUME | QF_BACKFILLED_GAP | QF_OUTLIER_WARNED | QF_DUPLICATE_KEPT | (1 << 12))
        self.assertEqual(int(mt["1D"]["quality_flags"].iloc[1]), 0)

    def test_resample_refuses_real_calendar_bars(self):
        """Synthetic-session helpers must not bucket real exchange data
        (review of PR #4, P2): US summer bars at 13:30-20:00 UTC are refused."""
        self.assertEqual(B.CALENDAR_SCOPE, "synthetic_only")
        ts = pd.date_range("2024-07-01 13:45", periods=26, freq="15min", tz="UTC")   # EDT session
        real = _bars(ts, [10.0] * 26, [10.5] * 26, [9.5] * 26, [10.0] * 26, [1.0] * 26)
        with self.assertRaisesRegex(ValueError, "exchange-calendar"):
            B.resample(real, "1H")
        odd = _bars(pd.date_range("2024-07-01 14:52", periods=3, freq="15min", tz="UTC"),
                    [10.0] * 3, [10.5] * 3, [9.5] * 3, [10.0] * 3, [1.0] * 3)
        with self.assertRaises(ValueError):
            B.resample(odd, "1D")


def _rich_spec(**kw):
    effects = (PlantedEffect("rev", "drop_reversal", strength=0.4, horizon=5, k=2.0),
               PlantedEffect("rsi_vol", "rsi_oversold", strength=0.3, horizon=4, regime="volatile"))
    base = dict(n_symbols=40, n_sessions=1500, effects=effects, seed=21)
    base.update(kw)
    return MarketSpec(**base)


class F5_SyntheticMarket(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = generate(_rich_spec())
        cls.null = generate(_rich_spec().null())

    def test_deterministic_and_seed_sensitive(self):
        a = generate(MarketSpec(n_symbols=3, n_sessions=50, seed=4))
        b = generate(MarketSpec(n_symbols=3, n_sessions=50, seed=4))
        c = generate(MarketSpec(n_symbols=3, n_sessions=50, seed=5))
        self.assertEqual(a.spec_digest, b.spec_digest)
        for s in a.bars:
            self.assertEqual(H.frame_digest(a.bars[s]), H.frame_digest(b.bars[s]))
        self.assertNotEqual(H.frame_digest(a.bars["S000"]), H.frame_digest(c.bars["S000"]))
        self.assertNotEqual(a.spec_digest, c.spec_digest)

    def test_bars_valid_for_every_option(self):
        spec = MarketSpec(n_symbols=6, n_sessions=40, timeframe="15m", delist_prob=0.5,
                          split_prob=0.5, missing_bar_prob=0.02, seed=8)
        m = generate(spec)
        for s, df in m.bars.items():
            self.assertEqual(B.validate_ohlcv(df, name=s), [])
        for s, df in self.m.bars.items():
            self.assertEqual(B.validate_ohlcv(df, name=s), [])

    def test_timestamps_mark_bar_close(self):
        d = generate(MarketSpec(n_symbols=1, n_sessions=2, seed=1)).bars["S000"]
        self.assertTrue((d.ts_utc.dt.hour == 21).all() and (d.ts_utc.dt.minute == 0).all())
        q = generate(MarketSpec(n_symbols=1, n_sessions=1, timeframe="15m", seed=1)).bars["S000"]
        self.assertEqual(q.ts_utc.iloc[0], pd.Timestamp("2018-01-02 14:45", tz="UTC"))
        self.assertEqual(q.ts_utc.iloc[-1], pd.Timestamp("2018-01-02 21:00", tz="UTC"))
        self.assertEqual(len(q), 26)

    def test_null_market_has_no_planted_drift(self):
        for tr in self.null.truth.values():
            self.assertTrue((tr["planted_drift"] == 0).all())
            self.assertFalse(any(c.startswith("trigger_") for c in tr.columns))

    def test_drift_is_causal_and_bounded_by_horizon(self):
        """Planted drift at bar t exists only if a trigger fired in [t-h, t-1]."""
        for tr in list(self.m.truth.values())[:10]:
            fired = np.zeros(len(tr), dtype=bool)
            for name, h in (("rev", 5), ("rsi_vol", 4)):
                trig = tr["trigger_" + name].to_numpy().astype(bool)
                for lag in range(1, h + 1):
                    fired[lag:] |= trig[:-lag]
            drift = tr["planted_drift"].to_numpy() != 0
            self.assertFalse((drift & ~fired).any())
            self.assertEqual(drift[0], False)

    def test_triggers_are_reproducible_from_observed_past_data(self):
        """A planted trigger is a deterministic function of OBSERVED bars up to
        and including its own close (plus the regime). If the generator leaked
        the effect into the trigger bar, recomputation would disagree."""
        meta = self.m.symbols.set_index("symbol")
        for s in list(self.m.bars)[:12]:
            b, tr = self.m.bars[s], self.m.truth[s]
            sig = meta.loc[s, "sigma_bar"]
            close = b["close"].to_numpy()
            r = np.diff(np.log(close), prepend=np.nan)
            rev = (r < -2.0 * sig)
            rev[:21] = False
            np.testing.assert_array_equal(rev.astype(int), tr["trigger_rev"].to_numpy())
            d = np.diff(close)
            rsi = np.full(len(close), np.nan)
            for t in range(15, len(close)):
                w = d[t - 14:t]
                g, l_ = w[w > 0].sum() / 14, -w[w < 0].sum() / 14
                rsi[t] = 100.0 if l_ == 0 else 100.0 - 100.0 / (1.0 + g / l_)
            exp = (rsi < 30.0) & (tr["regime"].to_numpy() == "volatile")
            np.testing.assert_array_equal(exp.astype(int), tr["trigger_rsi_vol"].to_numpy())

    def test_regime_gating(self):
        for tr in self.m.truth.values():
            fired = tr.loc[tr["trigger_rsi_vol"] == 1, "regime"]
            self.assertTrue((fired == "volatile").all())

    def test_planted_effect_is_measurable_and_absent_in_null(self):
        """Mean intraday return over the 5 bars after a 'rev' trigger is
        positive with t > 4, and the same trigger rule on the null market is
        indistinguishable from zero."""
        def post_trigger_returns(m, use_truth_trigger):
            out = []
            for s, b in m.bars.items():
                sig = m.symbols.set_index("symbol").loc[s, "sigma_bar"]
                lo_ = np.log(b["open"].to_numpy())
                lc_ = np.log(b["close"].to_numpy())
                if use_truth_trigger:
                    trig = m.truth[s]["trigger_rev"].to_numpy()
                else:
                    r = np.diff(lc_, prepend=np.nan)
                    trig = (r < -2.0 * sig).astype(int)
                    trig[:21] = 0
                idx = np.flatnonzero(trig[:-6])
                out += [(lc_[i + 5] - lo_[i + 1]) / sig for i in idx]
            return np.asarray(out)
        pos = post_trigger_returns(self.m, True)
        nul = post_trigger_returns(self.null, False)
        t_pos = pos.mean() / (pos.std(ddof=1) / np.sqrt(len(pos)))
        t_nul = nul.mean() / (nul.std(ddof=1) / np.sqrt(len(nul)))
        self.assertGreater(len(pos), 300)
        self.assertGreater(t_pos, 4.0)
        self.assertLess(abs(t_nul), 3.0)
        # Size is close to the planted 5 x 0.4 sigma = 2 sigma (noise ~ sqrt(5))
        self.assertAlmostEqual(pos.mean(), 2.0, delta=0.6)

    def test_volatile_regime_is_more_volatile(self):
        b = self.m.bars["S000"]
        r = np.diff(np.log(b["close"].to_numpy()))
        reg = self.m.truth["S000"]["regime"].to_numpy()[1:]
        self.assertGreater(r[reg == "volatile"].std(), 1.5 * r[reg == "calm"].std())

    def test_sector_correlation_structure(self):
        rets = pd.DataFrame({s: np.diff(np.log(b["close"].to_numpy())) for s, b in self.m.bars.items()})
        c = rets.corr().to_numpy()
        sec = self.m.symbols["sector"].to_numpy()
        same = sec[:, None] == sec[None, :]
        off = ~np.eye(len(sec), dtype=bool)
        self.assertGreater(c[same & off].mean(), c[~same].mean() + 0.03)
        self.assertGreater(c[~same].mean(), 0.0)          # common market factor

    def test_corruptions_match_truth(self):
        spec = MarketSpec(n_symbols=8, n_sessions=300, delist_prob=0.5, split_prob=0.5,
                          missing_bar_prob=0.02, seed=13)
        m = generate(spec)
        meta = m.symbols.set_index("symbol")
        for s, b in m.bars.items():
            tr = m.truth[s]
            if pd.notna(meta.loc[s, "delisted_at"]):
                self.assertTrue((b.ts_utc <= meta.loc[s, "delisted_at"]).all())
            self.assertEqual(len(b), int(tr["observed"].sum()))
            if pd.notna(meta.loc[s, "split_at"]):
                sa, ratio = meta.loc[s, "split_at"], meta.loc[s, "split_ratio"]
                obs = tr[tr.observed].reset_index(drop=True)
                np.testing.assert_allclose(b["close"].to_numpy(),
                                           (obs.true_close * obs.split_factor).to_numpy())
                self.assertTrue(((obs.split_factor == 1.0) == (obs.ts_utc < sa)).all())
                self.assertAlmostEqual(1.0 / obs.split_factor.min(), ratio)

    def test_intrabar_paths_define_high_low(self):
        m = generate(MarketSpec(n_symbols=2, n_sessions=30, keep_paths=True, seed=2))
        for s, b in m.bars.items():
            p = np.exp(m.paths[s])
            np.testing.assert_allclose(p.max(axis=1), b["high"].to_numpy(), rtol=1e-12)
            np.testing.assert_allclose(p.min(axis=1), b["low"].to_numpy(), rtol=1e-12)
            np.testing.assert_allclose(p[:, 0], b["open"].to_numpy(), rtol=1e-12)
            np.testing.assert_allclose(p[:, -1], b["close"].to_numpy(), rtol=1e-12)

    def test_spec_validation(self):
        with self.assertRaises(ValueError):
            PlantedEffect("x", "not_a_kind", 0.1)
        with self.assertRaises(ValueError):
            MarketSpec(effects=(PlantedEffect("x", "drop_reversal", 0.1, regime="bull"),))
        with self.assertRaises(ValueError):
            MarketSpec(effects=(PlantedEffect("x", "drop_reversal", 0.1),
                                PlantedEffect("x", "breakout", 0.1)))
        with self.assertRaises(ValueError):
            RegimeSpec(names=("a", "b"), vol_mult=(1.0,))
        with self.assertRaises(ValueError):
            MarketSpec(timeframe="1H")

    def test_generation_speed_budget(self):
        t0 = time.time()
        generate(MarketSpec(n_symbols=50, n_sessions=1000, effects=_rich_spec().effects, seed=3))
        self.assertLess(time.time() - t0, 20.0)


if __name__ == "__main__":
    unittest.main()
