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


if __name__ == "__main__":
    unittest.main()
