"""M20.UC1 — admin/offline quality collector proof tests.

Fully offline: Alpaca/Yahoo fetchers are injected mocks; no real network. Proves
dry-run/collect/validate behaviour, structured report shape, snapshot structure +
config digest, resume, rate-limit handling, no-secret-leakage, and — critically —
that UC1 NEVER modifies scan_ready / data_quality_status / us_seed.json /
us_expanded.json. Reuses frozen schemas; no schema change.
"""
import ast
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

import pandas as pd

import bot.universe.quality_collectors as qc
from bot.universe.quality_collectors import (
    universe_quality_check, universe_quality_collect,
    universe_quality_validate, _metrics_from_df, _SEED, _EXPANDED,
    SNAPSHOT_SCHEMA_VERSION,
)
from bot.universe.quality_report import (
    QualityCollectionReport, SourceSummary,
)

_PKG_DIR = pathlib.Path(__file__).resolve().parent / "bot" / "universe"
_REPO = pathlib.Path(__file__).resolve().parent
_BASELINE = "e823fe6779deaccc7b8ff7859c17b4dab564b868"
_M20UA_HEAD = "97f02326e12d9e381d94544555524c2d87b2cf27"
_M20H_HEAD = "146759e4d454d0d851345eaf33bbd9f4dedcc50b"
_M20UB_HEAD = "df92d115dcfb101c5e1808e17d2d6e246b227507"
_SECRET = "SUPERSECRET_TEST_KEY_123"


def _df(close, vol, n=300, last="2026-06-20"):
    idx = pd.date_range(end=last, periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"open": [close] * n, "high": [close] * n,
                         "low": [close] * n, "close": [close] * n,
                         "volume": [vol] * n}, index=idx)


def _mock_alpaca(close=200.0, vol=1_000_000, n=300):
    def fetch(symbols, *, lookback_days):
        return {s: _metrics_from_df(_df(close, vol, n)) for s in symbols}
    return fetch


def _mock_yahoo(close=200.4, vol=1_010_000, n=300):
    def fetch(symbols, *, lookback_days):
        return {s: _metrics_from_df(_df(close, vol, n)) for s in symbols}
    return fetch


def _mock_rate_limited():
    def fetch(symbols, *, lookback_days):
        return {s: {"status": "rate_limit", "error": "rate exceeded"}
                for s in symbols}
    return fetch


def _mock_missing():
    def fetch(symbols, *, lookback_days):
        return {}
    return fetch


class M20UC1DryRun(unittest.TestCase):

    def test_dry_run_reachable_with_mocks(self):
        r = universe_quality_check(sources=["alpaca", "yahoo"],
                                   alpaca_fetch=_mock_alpaca(),
                                   yahoo_fetch=_mock_yahoo())
        self.assertEqual(r.status, "success")
        self.assertTrue(r.alpaca_reachable)
        self.assertTrue(r.yahoo_reachable)
        self.assertEqual(r.mode, "dry-run")

    def test_dry_run_writes_nothing(self):
        before = _EXPANDED.read_bytes(), _SEED.read_bytes()
        universe_quality_check(sources=["alpaca", "yahoo"],
                               alpaca_fetch=_mock_alpaca(),
                               yahoo_fetch=_mock_yahoo())
        self.assertEqual((_EXPANDED.read_bytes(), _SEED.read_bytes()), before)

    def test_dry_run_no_creds_real_path_fails_safely(self):
        # Deterministic regardless of host env: clear Alpaca creds for this
        # test's scope so the no-creds branch is exercised on sandbox AND VPS.
        # yahoo is mocked-missing so no real network is touched.
        saved_key = os.environ.pop("ALPACA_KEY", None)
        saved_secret = os.environ.pop("ALPACA_SECRET", None)
        try:
            r = universe_quality_check(sources=["alpaca"],
                                       yahoo_fetch=_mock_missing())
            self.assertEqual(r.status, "failed")
            self.assertFalse(r.alpaca_creds_present)
            self.assertIn("alpaca_creds_missing", r.errors)
        finally:
            if saved_key is not None:
                os.environ["ALPACA_KEY"] = saved_key
            if saved_secret is not None:
                os.environ["ALPACA_SECRET"] = saved_secret

    def test_failed_never_has_empty_errors(self):
        # SIP-denied on alpaca + a failing yahoo -> failed WITH reasons
        def sip(symbols, **k):
            return {s: {"status": "error",
                        "reason": "alpaca_subscription_not_permitted"}
                    for s in symbols}
        def yfail(symbols, **k):
            return {s: {"status": "error", "reason": "yahoo_fetch_error"}
                    for s in symbols}
        r = universe_quality_check(sources=["alpaca", "yahoo"],
                                   alpaca_fetch=sip, yahoo_fetch=yfail)
        self.assertEqual(r.status, "failed")
        self.assertTrue(r.errors, "failed status must carry reasons")
        self.assertIn("alpaca_subscription_not_permitted", r.errors)

    def test_sip_denied_reason_surfaced_in_summary(self):
        def sip(symbols, **k):
            return {s: {"status": "error",
                        "reason": "alpaca_subscription_not_permitted"}
                    for s in symbols}
        r = universe_quality_check(sources=["alpaca", "yahoo"],
                                   alpaca_fetch=sip,
                                   yahoo_fetch=_mock_yahoo())
        a = [s for s in r.source_summaries if s.source == "alpaca"][0]
        self.assertEqual(a.reason, "alpaca_subscription_not_permitted")
        self.assertFalse(a.reachable)
        # one source up -> partial, not failed
        self.assertEqual(r.status, "partial")


class M20UC1Collect(unittest.TestCase):

    def test_collect_writes_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            r = universe_quality_collect(asof="2026-06-22",
                                         sources=["alpaca", "yahoo"],
                                         out_path=out, resume=False,
                                         alpaca_fetch=_mock_alpaca(),
                                         yahoo_fetch=_mock_yahoo())
            self.assertTrue(r.status in ("success", "partial"))
            self.assertTrue(os.path.exists(out))
            snap = json.loads(pathlib.Path(out).read_text())
            self.assertEqual(snap["schema_version"], SNAPSHOT_SCHEMA_VERSION)
            self.assertTrue(snap["collector_config_digest"].startswith(
                "sha256:"))
            self.assertGreater(len(snap["symbols"]), 0)

    def test_snapshot_has_per_source_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            universe_quality_collect(asof="2026-06-22",
                                     sources=["alpaca", "yahoo"], out_path=out,
                                     resume=False, alpaca_fetch=_mock_alpaca(),
                                     yahoo_fetch=_mock_yahoo())
            snap = json.loads(pathlib.Path(out).read_text())
            rec = next(iter(snap["symbols"].values()))
            self.assertIn("alpaca", rec)
            self.assertIn("yahoo", rec)
            self.assertIn("provider_symbol", rec)
            self.assertEqual(rec["alpaca"]["status"], "ok")
            self.assertIn("avg_dollar_volume_20d", rec["alpaca"])

    def test_snapshot_has_no_scan_ready_or_verified(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            universe_quality_collect(asof="2026-06-22",
                                     sources=["alpaca", "yahoo"], out_path=out,
                                     resume=False, alpaca_fetch=_mock_alpaca(),
                                     yahoo_fetch=_mock_yahoo())
            blob = pathlib.Path(out).read_text()
            self.assertNotIn("scan_ready", blob)
            self.assertNotIn("data_quality_status", blob)

    def test_collect_does_not_modify_universe_files(self):
        before = _EXPANDED.read_bytes(), _SEED.read_bytes()
        with tempfile.TemporaryDirectory() as d:
            universe_quality_collect(asof="2026-06-22",
                                     sources=["alpaca", "yahoo"],
                                     out_path=os.path.join(d, "s.json"),
                                     resume=False, alpaca_fetch=_mock_alpaca(),
                                     yahoo_fetch=_mock_yahoo())
        self.assertEqual((_EXPANDED.read_bytes(), _SEED.read_bytes()), before)

    def test_resume_skips_existing_symbols(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            r1 = universe_quality_collect(asof="2026-06-22",
                                          sources=["alpaca", "yahoo"],
                                          out_path=out, resume=False,
                                          alpaca_fetch=_mock_alpaca(),
                                          yahoo_fetch=_mock_yahoo())
            n1 = json.loads(pathlib.Path(out).read_text())["symbols"]
            # second run with resume should not grow the file
            r2 = universe_quality_collect(asof="2026-06-22",
                                          sources=["alpaca", "yahoo"],
                                          out_path=out, resume=True,
                                          alpaca_fetch=_mock_alpaca(),
                                          yahoo_fetch=_mock_yahoo())
            n2 = json.loads(pathlib.Path(out).read_text())["symbols"]
            self.assertEqual(len(n1), len(n2))

    def test_rate_limit_recorded_partial(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            r = universe_quality_collect(asof="2026-06-22",
                                         sources=["alpaca", "yahoo"],
                                         out_path=out, resume=False,
                                         alpaca_fetch=_mock_rate_limited(),
                                         yahoo_fetch=_mock_yahoo())
            self.assertEqual(r.status, "partial")
            self.assertGreater(r.rate_limit_count, 0)

    def test_missing_source_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            r = universe_quality_collect(asof="2026-06-22",
                                         sources=["alpaca", "yahoo"],
                                         out_path=out, resume=False,
                                         alpaca_fetch=_mock_alpaca(),
                                         yahoo_fetch=_mock_missing())
            self.assertGreater(r.missing_yahoo_count, 0)
            self.assertEqual(r.status, "partial")


class M20UC1Validate(unittest.TestCase):

    def _write_snapshot(self, out, alpaca_close=200.0, yahoo_close=200.4):
        universe_quality_collect(asof="2026-06-22",
                                 sources=["alpaca", "yahoo"], out_path=out,
                                 resume=False,
                                 alpaca_fetch=_mock_alpaca(close=alpaca_close),
                                 yahoo_fetch=_mock_yahoo(close=yahoo_close))

    def test_validate_agreement_within_tolerance(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            self._write_snapshot(out, 200.0, 200.4)  # ~0.2% < 2%
            r = universe_quality_validate(snapshot_path=out)
            self.assertEqual(r.status, "success")
            self.assertEqual(r.source_disagreement_count, 0)

    def test_validate_detects_disagreement(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            self._write_snapshot(out, 200.0, 250.0)  # 25% > 2%
            r = universe_quality_validate(snapshot_path=out)
            self.assertGreater(r.source_disagreement_count, 0)

    def test_validate_missing_file(self):
        r = universe_quality_validate(snapshot_path="/tmp/does_not_exist.json")
        self.assertEqual(r.status, "failed")
        self.assertIn("snapshot_not_found", r.errors)

    def test_validate_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.json")
            pathlib.Path(p).write_text("{not json")
            r = universe_quality_validate(snapshot_path=p)
            self.assertEqual(r.status, "failed")
            self.assertTrue(any("corrupt_snapshot" in e for e in r.errors))


class M20UC1NoSecretLeak(unittest.TestCase):

    def test_secret_never_in_report_or_snapshot(self):
        os.environ["ALPACA_KEY"] = _SECRET
        os.environ["ALPACA_SECRET"] = _SECRET
        try:
            r = universe_quality_check(sources=["alpaca", "yahoo"],
                                       alpaca_fetch=_mock_alpaca(),
                                       yahoo_fetch=_mock_yahoo())
            blob = json.dumps(r.to_dict())
            self.assertNotIn(_SECRET, blob)
            self.assertTrue(r.alpaca_creds_present)  # detected presence only
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "s.json")
                universe_quality_collect(asof="2026-06-22",
                                         sources=["alpaca", "yahoo"],
                                         out_path=out, resume=False,
                                         alpaca_fetch=_mock_alpaca(),
                                         yahoo_fetch=_mock_yahoo())
                self.assertNotIn(_SECRET, pathlib.Path(out).read_text())
        finally:
            os.environ.pop("ALPACA_KEY", None)
            os.environ.pop("ALPACA_SECRET", None)


class M20UC1Report(unittest.TestCase):

    def test_report_to_dict_round_trip_fields(self):
        r = QualityCollectionReport(status="success", mode="dry-run",
                                    source_summaries=[SourceSummary(
                                        source="alpaca", reachable=True)])
        d = r.to_dict()
        for k in ("status", "mode", "symbols_total", "alpaca_success_count",
                  "yahoo_success_count", "both_sources_success_count",
                  "missing_alpaca_count", "missing_yahoo_count",
                  "source_disagreement_count", "rate_limit_count", "errors",
                  "snapshot_path", "log_path", "started_at_utc",
                  "finished_at_utc"):
            self.assertIn(k, d)
        self.assertEqual(d["source_summaries"][0]["source"], "alpaca")


class M20UC1SafetyGuards(unittest.TestCase):

    FORBIDDEN_PREFIXES = ("bot.paper", "bot.brokers", "bot.live", "bot.scanner",
                          "bot.risk", "bot.risk_authority", "bot.strategy",
                          "bot.flywheel", "bot.signal_scoring", "dashboard")

    def test_no_forbidden_imports(self):
        for mod in ("quality_collectors.py", "quality_report.py"):
            tree = ast.parse((_PKG_DIR / mod).read_text())
            for n in ast.walk(tree):
                names = []
                if isinstance(n, ast.Import):
                    names = [a.name for a in n.names]
                elif isinstance(n, ast.ImportFrom) and n.module:
                    names = [n.module]
                for nm in names:
                    self.assertFalse(nm.startswith(self.FORBIDDEN_PREFIXES),
                                     f"{mod}:{nm}")

    def test_provider_imports_are_lazy(self):
        # alpaca-py / yfinance / provider modules must NOT be top-level imports
        tree = ast.parse((_PKG_DIR / "quality_collectors.py").read_text())
        top_level = []
        for n in tree.body:
            if isinstance(n, ast.Import):
                top_level += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module:
                top_level.append(n.module)
        for banned in ("alpaca", "yfinance",
                       "bot.providers.alpaca_provider",
                       "bot.historical.providers_yfinance"):
            self.assertNotIn(banned, top_level)

    def test_no_trading_or_scan_ready_tokens(self):
        src = (_PKG_DIR / "quality_collectors.py").read_text()
        for tok in ("place_order", "submit_order", "execute",
                    "scan_ready=True", "scan_ready = True",
                    'data_quality_status="verified"',
                    "data_quality_status = "):
            self.assertNotIn(tok, src, tok)

    def test_config_exposes_alpaca_feed_iex_default(self):
        cfg = json.loads((_REPO / "configs" / "universe" /
                          "quality_collector_config.json").read_text())
        self.assertEqual(cfg.get("alpaca_feed"), "iex")

    def test_config_exposes_throttle_batch_retry(self):
        cfg = json.loads((_REPO / "configs" / "universe" /
                          "quality_collector_config.json").read_text())
        self.assertIn("throttle_seconds", cfg)
        self.assertIn("batch_size", cfg)
        self.assertIn("max_retries", cfg)
        self.assertIn("circuit_breaker", cfg)
        self.assertGreater(cfg["circuit_breaker"], 0)  # fail-fast on by default
        self.assertGreater(cfg["throttle_seconds"], 0.0)  # polite, not a burst

    def test_yahoo_fetch_calls_real_4arg_signature(self):
        # the real default yahoo fetch must call fetch_bars(symbol, timeframe,
        # start_utc, end_utc) and read FetchResult.df — never the old 2-arg
        # call that raised TypeError.
        import bot.universe.quality_collectors as mod
        calls = {}

        class _FakeRes:
            outcome = "ok"
            df = _df(100.0, 1_000_000)

        class _FakeProv:
            def fetch_bars(self, symbol, timeframe, start_utc, end_utc):
                calls["args"] = (symbol, timeframe, type(start_utc).__name__,
                                 type(end_utc).__name__)
                return _FakeRes()

        import bot.historical.providers_yfinance as yfmod
        orig = yfmod.YFinanceProvider
        yfmod.YFinanceProvider = _FakeProv
        try:
            out = mod._default_yahoo_fetch(["AAPL"], lookback_days=400)
        finally:
            yfmod.YFinanceProvider = orig
        self.assertEqual(out["AAPL"]["status"], "ok")
        self.assertEqual(calls["args"][1], "1D")
        self.assertEqual(calls["args"][2], "datetime")
        self.assertEqual(calls["args"][3], "datetime")


class M20UC1FrozenChecks(unittest.TestCase):

    def _unchanged(self, baseline, *paths):
        r = subprocess.run(["git", "diff", "--name-only", baseline, "HEAD",
                            "--", *paths], capture_output=True, text=True,
                           timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "", f"{paths} changed vs {baseline}")

    def test_paper_frozen_vs_m20h(self):
        self._unchanged(_M20H_HEAD, "bot/paper")

    def test_universe_code_frozen_vs_m20ua(self):
        self._unchanged(_M20UA_HEAD, "bot/universe/schema.py",
                        "bot/universe/registry.py", "bot/universe/suffixes.py")

    def test_universe_data_frozen_vs_m20ub(self):
        # UC1 must NOT modify the universe records
        self._unchanged(_M20UB_HEAD, "configs/universe/us_expanded.json",
                        "configs/universe/us_seed.json")

    def test_alpaca_provider_unchanged(self):
        self._unchanged(_BASELINE, "bot/providers/alpaca_provider.py")

    def test_protected_runtime_unchanged(self):
        self._unchanged(_BASELINE, "main.py", "bot/scanner.py", "bot/risk.py",
                        "bot/strategy.py", "dashboard/app.py", "bot/brokers",
                        "bot/flywheel.py", "bot/signal_scoring")


class M20UC1ThrottlePolicy(unittest.TestCase):
    """Throttle / batch / exponential-backoff policy — mock sleep, no real wait."""

    def test_throttle_between_symbols_and_batch_pause(self):
        from bot.universe.quality_collectors import _fetch_with_policy
        slept = []
        calls = []
        def ok(sym):
            calls.append(sym)
            return {"status": "ok"}
        out, _ = _fetch_with_policy([f"S{i}" for i in range(5)], ok,
                                    throttle_seconds=0.4, batch_size=2,
                                    sleep=slept.append)
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(v["status"] == "ok" for v in out.values()))
        # 4 inter-symbol gaps; every 2nd is a longer batch pause (0.8)
        self.assertEqual(slept, [0.4, 0.8, 0.4, 0.8])

    def test_no_real_sleep_used(self):
        # the policy must accept an injected sleep; tests never call time.sleep
        from bot.universe.quality_collectors import _fetch_with_policy
        marker = []
        _fetch_with_policy(["A", "B"], lambda s: {"status": "ok"},
                           throttle_seconds=99999.0, sleep=lambda s: marker.append(s))
        self.assertTrue(marker)  # injected sleep received the (large) value
        # if real time.sleep had been used with 99999s the test would hang;
        # reaching here proves the injected sleep was used instead.

    def test_backoff_retries_then_recovers(self):
        from bot.universe.quality_collectors import _fetch_with_policy
        slept = []
        state = {"n": 0}
        def flaky(sym):
            state["n"] += 1
            if state["n"] < 3:
                return {"status": "rate_limit", "reason": "yahoo_rate_limited"}
            return {"status": "ok"}
        out, _ = _fetch_with_policy(["X"], flaky, throttle_seconds=0.5,
                                    max_retries=3, sleep=slept.append)
        self.assertEqual(out["X"]["status"], "ok")
        self.assertEqual(slept, [0.5, 1.0])  # exponential backoff

    def test_backoff_exhausts_and_records_rate_limit(self):
        from bot.universe.quality_collectors import _fetch_with_policy
        slept = []
        out, _ = _fetch_with_policy(
            ["Y"], lambda s: {"status": "rate_limit",
                              "reason": "yahoo_rate_limited"},
            throttle_seconds=0.5, max_retries=2, sleep=slept.append)
        self.assertEqual(out["Y"]["status"], "rate_limit")
        self.assertEqual(slept, [0.5, 1.0])

    def test_collect_passes_policy_to_default_fetchers(self):
        # injected fetchers must still receive lookback_days and tolerate the
        # policy kwargs path (collect only forwards policy to DEFAULT fetchers).
        import bot.universe.quality_collectors as mod
        seen = {}
        def fake_alpaca(symbols, **kwargs):
            seen["alpaca_kwargs"] = set(kwargs.keys())
            return {s: _metrics_from_df(_df(200.0, 1_000_000)) for s in symbols}
        def fake_yahoo(symbols, **kwargs):
            seen["yahoo_kwargs"] = set(kwargs.keys())
            return {s: _metrics_from_df(_df(200.4, 1_010_000)) for s in symbols}
        with tempfile.TemporaryDirectory() as d:
            mod.universe_quality_collect(
                asof="2026-06-22", sources=["alpaca", "yahoo"],
                out_path=os.path.join(d, "s.json"), resume=False,
                alpaca_fetch=fake_alpaca, yahoo_fetch=fake_yahoo)
        # injected fetchers get lookback_days but NOT the policy kwargs
        self.assertIn("lookback_days", seen["alpaca_kwargs"])
        self.assertNotIn("throttle_seconds", seen["alpaca_kwargs"])


class M20UC1PerSourceResume(unittest.TestCase):
    """Per-source resume, slicing, overrides, reachable-in-collect-mode."""

    def test_alpaca_then_yahoo_topup_same_file(self):
        # Alpaca-only first, then Yahoo-only must top up the SAME file without
        # skipping symbols that have Alpaca but no Yahoo.
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            r1 = universe_quality_collect(
                asof="2026-06-22", sources=["alpaca"], out_path=out,
                alpaca_fetch=_mock_alpaca(), yahoo_fetch=_mock_yahoo())
            self.assertGreater(r1.alpaca_success_count, 0)
            self.assertEqual(r1.yahoo_success_count, 0)
            self.assertTrue(r1.alpaca_reachable)        # reachable fix
            self.assertFalse(r1.yahoo_reachable)
            snap1 = json.loads(pathlib.Path(out).read_text())["symbols"]
            rec1 = next(iter(snap1.values()))
            self.assertEqual(rec1["alpaca"]["status"], "ok")
            self.assertNotEqual(rec1["yahoo"].get("status"), "ok")
            # Yahoo top-up into the same file
            r2 = universe_quality_collect(
                asof="2026-06-22", sources=["yahoo"], out_path=out,
                alpaca_fetch=_mock_alpaca(), yahoo_fetch=_mock_yahoo())
            self.assertGreater(r2.yahoo_success_count, 0)
            self.assertGreater(r2.both_sources_success_count, 0)
            self.assertTrue(r2.yahoo_reachable)
            snap2 = json.loads(pathlib.Path(out).read_text())["symbols"]
            rec2 = next(iter(snap2.values()))
            self.assertEqual(rec2["alpaca"]["status"], "ok")  # not dropped
            self.assertEqual(rec2["yahoo"]["status"], "ok")   # merged in

    def test_per_source_resume_skips_only_usable_source(self):
        # after both sources ok, a 2nd yahoo run fetches nothing
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            universe_quality_collect(asof="2026-06-22", sources=["alpaca", "yahoo"],
                                     out_path=out, alpaca_fetch=_mock_alpaca(),
                                     yahoo_fetch=_mock_yahoo())
            fetched = []
            def y_count(symbols, **k):
                fetched.extend(symbols)
                return {s: _metrics_from_df(_df(200.4, 1_010_000)) for s in symbols}
            universe_quality_collect(asof="2026-06-22", sources=["yahoo"],
                                     out_path=out, alpaca_fetch=_mock_alpaca(),
                                     yahoo_fetch=y_count)
            self.assertEqual(fetched, [])  # nothing to fetch; all usable

    def test_yahoo_run_fetches_alpaca_only_symbols(self):
        # symbols with alpaca-ok but yahoo-missing MUST be fetched on yahoo run
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            universe_quality_collect(asof="2026-06-22", sources=["alpaca"],
                                     out_path=out, alpaca_fetch=_mock_alpaca(),
                                     yahoo_fetch=_mock_yahoo())
            fetched = []
            def y_count(symbols, **k):
                fetched.extend(symbols)
                return {s: _metrics_from_df(_df(200.4, 1_010_000)) for s in symbols}
            universe_quality_collect(asof="2026-06-22", sources=["yahoo"],
                                     out_path=out, alpaca_fetch=_mock_alpaca(),
                                     yahoo_fetch=y_count)
            self.assertGreater(len(fetched), 0)  # alpaca-only symbols fetched

    def test_limit_offset_slicing(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            r = universe_quality_collect(
                asof="2026-06-22", sources=["alpaca"], out_path=out,
                limit=10, offset=0, alpaca_fetch=_mock_alpaca(),
                yahoo_fetch=_mock_yahoo())
            self.assertEqual(r.symbols_checked, 10)

    def test_offset_slices_different_symbols(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            universe_quality_collect(asof="2026-06-22", sources=["alpaca"],
                                     out_path=out, limit=5, offset=0,
                                     alpaca_fetch=_mock_alpaca(),
                                     yahoo_fetch=_mock_yahoo())
            first5 = set(json.loads(pathlib.Path(out).read_text())["symbols"])
            universe_quality_collect(asof="2026-06-22", sources=["alpaca"],
                                     out_path=out, limit=5, offset=5,
                                     alpaca_fetch=_mock_alpaca(),
                                     yahoo_fetch=_mock_yahoo())
            after = set(json.loads(pathlib.Path(out).read_text())["symbols"])
            self.assertEqual(len(after), 10)  # 5 + 5 distinct merged

    def test_cli_overrides_parsed(self):
        # --throttle-seconds/--batch-size/--max-retries/--limit/--offset parse
        import bot.universe.quality_collectors as mod
        seen = {}
        orig = mod.universe_quality_collect
        def spy(**kwargs):
            seen.update(kwargs)
            return orig(asof=kwargs["asof"], sources=kwargs["sources"],
                        out_path=kwargs.get("out_path"), resume=kwargs.get("resume", True),
                        limit=kwargs.get("limit"), offset=kwargs.get("offset", 0),
                        alpaca_fetch=_mock_alpaca(), yahoo_fetch=_mock_yahoo())
        mod.universe_quality_collect = spy
        try:
            with tempfile.TemporaryDirectory() as d:
                mod._main(["--mode", "collect", "--asof", "2026-06-22",
                           "--sources", "alpaca", "--out", os.path.join(d, "s.json"),
                           "--throttle-seconds", "1.5", "--batch-size", "25",
                           "--max-retries", "4", "--limit", "10", "--offset", "5",
                           "--circuit-breaker", "7"])
        finally:
            mod.universe_quality_collect = orig
        self.assertEqual(seen.get("throttle_seconds"), 1.5)
        self.assertEqual(seen.get("batch_size"), 25)
        self.assertEqual(seen.get("max_retries"), 4)
        self.assertEqual(seen.get("circuit_breaker"), 7)
        self.assertEqual(seen.get("limit"), 10)
        self.assertEqual(seen.get("offset"), 5)


class M20UC1PatchReportingAndCircuit(unittest.TestCase):
    """Per-source rate-limit attribution, run-scope fields, circuit breaker."""

    def test_per_source_rate_limit_attribution(self):
        # yahoo-only run with yahoo rate-limited: rate-limit shows on YAHOO
        # summary, NOT alpaca summary.
        def y_rl(symbols, **k):
            return {s: {"status": "rate_limit", "reason": "yahoo_rate_limited"}
                    for s in symbols}
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            universe_quality_collect(asof="2026-06-22", sources=["alpaca"],
                                     out_path=out, alpaca_fetch=_mock_alpaca(),
                                     yahoo_fetch=y_rl)
            r = universe_quality_collect(asof="2026-06-22", sources=["yahoo"],
                                         out_path=out, limit=5,
                                         alpaca_fetch=_mock_alpaca(),
                                         yahoo_fetch=y_rl).to_dict()
            asum = [s for s in r["source_summaries"] if s["source"] == "alpaca"][0]
            ysum = [s for s in r["source_summaries"] if s["source"] == "yahoo"][0]
            self.assertEqual(asum["rate_limit_count"], 0)  # not alpaca's fault
            self.assertGreater(ysum["rate_limit_count"], 0)  # yahoo's own

    def test_run_scope_fields(self):
        def y_rl(symbols, **k):
            return {s: {"status": "rate_limit", "reason": "yahoo_rate_limited"}
                    for s in symbols}
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snap.json")
            universe_quality_collect(asof="2026-06-22", sources=["alpaca"],
                                     out_path=out, alpaca_fetch=_mock_alpaca(),
                                     yahoo_fetch=y_rl)
            r = universe_quality_collect(asof="2026-06-22", sources=["yahoo"],
                                         out_path=out, limit=5,
                                         alpaca_fetch=_mock_alpaca(),
                                         yahoo_fetch=y_rl).to_dict()
            self.assertEqual(r["run_sources"], ["yahoo"])
            self.assertEqual(r["run_symbols_attempted"], 5)   # the slice
            self.assertEqual(r["run_yahoo_ok"], 0)
            self.assertEqual(r["run_rate_limit_count"], 5)
            # whole-snapshot fields remain total coverage
            self.assertEqual(r["symbols_checked"], 573)
            self.assertGreater(r["alpaca_success_count"], 0)  # alpaca preserved

    def test_circuit_breaker_opens_and_skips_rest(self):
        from bot.universe.quality_collectors import _fetch_with_policy
        def always_rl(sym):
            return {"status": "rate_limit", "reason": "yahoo_rate_limited"}
        out, circuit_open = _fetch_with_policy(
            [f"S{i}" for i in range(10)], always_rl, throttle_seconds=0.0,
            max_retries=0, circuit_breaker=3, sleep=lambda s: None)
        self.assertTrue(circuit_open)
        statuses = [out[f"S{i}"]["status"] for i in range(10)]
        self.assertEqual(statuses.count("rate_limit"), 3)  # tripped after 3
        skipped = [k for k, v in out.items()
                   if v.get("reason") == "skipped_circuit_open"]
        self.assertEqual(len(skipped), 7)  # rest skipped, not hammered

    def test_circuit_breaker_resets_on_success(self):
        from bot.universe.quality_collectors import _fetch_with_policy
        state = {"n": 0}
        def alt(sym):
            state["n"] += 1
            if state["n"] % 2 == 0:
                return {"status": "ok"}
            return {"status": "rate_limit", "reason": "yahoo_rate_limited"}
        out, circuit_open = _fetch_with_policy(
            [f"S{i}" for i in range(10)], alt, throttle_seconds=0.0,
            max_retries=0, circuit_breaker=3, sleep=lambda s: None)
        self.assertFalse(circuit_open)  # successes reset the consecutive count

    def test_circuit_breaker_zero_disables(self):
        from bot.universe.quality_collectors import _fetch_with_policy
        def always_rl(sym):
            return {"status": "rate_limit", "reason": "yahoo_rate_limited"}
        out, circuit_open = _fetch_with_policy(
            [f"S{i}" for i in range(6)], always_rl, throttle_seconds=0.0,
            max_retries=0, circuit_breaker=0, sleep=lambda s: None)
        self.assertFalse(circuit_open)
        self.assertTrue(all(v["status"] == "rate_limit" for v in out.values()))


class M20UC1LastBarDateAndDisagreement(unittest.TestCase):
    """last_bar_date extraction (MultiIndex bug) + split price/volume reporting."""

    def test_last_bar_date_from_multiindex(self):
        import pandas as pd
        from bot.universe.quality_collectors import _extract_last_bar_date
        idx = pd.MultiIndex.from_tuples(
            [("AAPL", pd.Timestamp("2026-06-23", tz="UTC")),
             ("AAPL", pd.Timestamp("2026-06-24", tz="UTC"))],
            names=["symbol", "timestamp"])
        df = pd.DataFrame({"close": [1.0, 2.0], "volume": [1e6, 1e6]}, index=idx)
        self.assertEqual(_extract_last_bar_date(df), "2026-06-24")

    def test_last_bar_date_from_datetimeindex(self):
        import pandas as pd
        from bot.universe.quality_collectors import _extract_last_bar_date
        df = pd.DataFrame(
            {"close": [1.0, 2.0], "volume": [1e6, 1e6]},
            index=pd.date_range(end="2026-06-24", periods=2, freq="D", tz="UTC"))
        self.assertEqual(_extract_last_bar_date(df), "2026-06-24")

    def test_metrics_last_bar_date_not_tuple_repr(self):
        # regression: must never emit the "('AAPL', T" tuple-repr garbage
        import pandas as pd
        idx = pd.MultiIndex.from_tuples(
            [("AAPL", pd.Timestamp("2026-06-24", tz="UTC"))],
            names=["symbol", "timestamp"])
        df = pd.DataFrame({"close": [200.0], "volume": [1e6]}, index=idx)
        d = _metrics_from_df(df)
        self.assertEqual(d["last_bar_date"], "2026-06-24")
        self.assertNotIn("(", d["last_bar_date"])

    def test_price_disagrees_only_on_close(self):
        from bot.universe.quality_collectors import _price_disagrees
        tol = {"latest_close_pct": 2.0}
        # prices agree (IVV-like): not a disagreement despite huge volume gap
        self.assertFalse(_price_disagrees(
            {"latest_close": 735.30}, {"latest_close": 736.66}, tol))
        # prices genuinely differ
        self.assertTrue(_price_disagrees(
            {"latest_close": 100.0}, {"latest_close": 110.0}, tol))

    def test_volume_diverges_reported_separately(self):
        from bot.universe.quality_collectors import _volume_diverges
        tol = {"avg_volume_20d_pct": 25.0, "avg_dollar_volume_20d_pct": 25.0}
        # IEX vs consolidated volume: diverges (report-only, not a failure)
        self.assertTrue(_volume_diverges(
            {"avg_volume_20d": 68230.0, "avg_dollar_volume_20d": 5.1e7},
            {"avg_volume_20d": 13122994.0, "avg_dollar_volume_20d": 9.8e9}, tol))

    def test_validate_splits_price_and_volume(self):
        # build a tiny snapshot: prices agree, volume diverges -> price
        # disagreement 0, volume divergence 1, source_disagreement 0.
        import json, tempfile
        from bot.universe.quality_collectors import (
            universe_quality_validate, SNAPSHOT_SCHEMA_VERSION)
        snap = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION, "asof": "2026-06-24",
            "sources": ["alpaca", "yahoo"],
            "symbols": {
                "NASDAQ:AAA": {
                    "provider_symbol": "AAA",
                    "alpaca": {"status": "ok", "latest_close": 100.0,
                               "avg_volume_20d": 50000.0,
                               "avg_dollar_volume_20d": 5e6,
                               "last_bar_date": "2026-06-24"},
                    "yahoo": {"status": "ok", "latest_close": 100.5,
                              "avg_volume_20d": 9000000.0,
                              "avg_dollar_volume_20d": 9e8,
                              "last_bar_date": "2026-06-24"},
                }
            }
        }
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            pathlib.Path(p).write_text(json.dumps(snap))
            r = universe_quality_validate(snapshot_path=p).to_dict()
        self.assertEqual(r["both_sources_success_count"], 1)
        self.assertEqual(r["price_disagreement_count"], 0)       # prices agree
        self.assertEqual(r["source_disagreement_count"], 0)      # price-only now
        self.assertEqual(r["volume_semantics_divergence_count"], 1)  # reported
        self.assertEqual(r["bar_date_mismatch_count"], 0)


if __name__ == "__main__":
    unittest.main()
