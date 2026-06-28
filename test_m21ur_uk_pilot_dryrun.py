"""M21.UR UK pilot dry-run tests (offline, deterministic, no network).

Proves the dry-run touches ONLY the 5 UK pilot symbols, reuses the M21.UQ
evaluator so rate-limits classify as provider_rate_limited (not ohlcv_empty),
records elapsed time, commits only simulated/structural provenance, and imports
no broker/live/paper/Telegram/scanner/main runtime code.
"""
import ast
import datetime
import pathlib
import unittest

from tools.universe_quality import uk_pilot_dryrun as D
from tools.universe_quality.yfinance_provider import YFinanceProvider
from tools.universe_quality.quality_model import OHLCVConfig

_REPO = pathlib.Path(__file__).resolve().parent
_RUNNER = _REPO / "tools" / "universe_quality" / "uk_pilot_dryrun.py"
_REPORT = _REPO / "reports" / "m21ur_uk_pilot_dryrun.md"
_EXPECTED = ["AAF.L", "AAL.L", "ABDN.L", "ABF.L", "ADM.L"]
_AS_OF = "2026-06-26"


def _bars(n=25, last=_AS_OF, vol=1000.0, close=10.0):
    base = datetime.date.fromisoformat(last)
    return [{"date": (base - datetime.timedelta(days=(n - 1 - i))).isoformat(),
             "open": close, "high": close, "low": close, "close": close,
             "volume": vol} for i in range(n)]


class Scope(unittest.TestCase):
    def test_touches_exactly_five_pilot_symbols(self):
        r = D.run_dryrun()  # offline
        self.assertEqual(sorted(r["symbols_checked"]), sorted(_EXPECTED))
        self.assertEqual(r["n_symbols"], 5)

    def test_no_global_or_us_symbols(self):
        r = D.run_dryrun()
        for s in r["symbols_checked"]:
            self.assertTrue(s.endswith(".L"))
            self.assertFalse(s.endswith(".HK"))
        # not 193, not 536
        self.assertNotEqual(r["n_symbols"], 193)
        self.assertNotEqual(r["n_symbols"], 536)

    def test_load_pilot_records_returns_five(self):
        recs = D.load_pilot_records()
        self.assertEqual(len(recs), 5)


class Evaluation(unittest.TestCase):
    def test_mocked_success_passes(self):
        prov = YFinanceProvider(_fetch_fn=lambda s: _bars(25))
        r = D.run_dryrun(provider=prov, as_of=_AS_OF, cfg=OHLCVConfig())
        self.assertTrue(all(p["passed"] for p in r["per_symbol"]))

    def test_mocked_rate_limit_maps_to_provider_rate_limited(self):
        class YFRateLimitError(Exception):
            pass

        def fake(sym):
            raise YFRateLimitError("Too Many Requests. Rate limited.")
        prov = YFinanceProvider(_fetch_fn=fake)
        r = D.run_dryrun(provider=prov, as_of=_AS_OF)
        for p in r["per_symbol"]:
            self.assertIn("provider_rate_limited", p["reason_codes"])
            self.assertNotIn("ohlcv_empty", p["reason_codes"])
            self.assertNotIn("volume_missing_or_zero", p["reason_codes"])

    def test_errors_separated_from_data_failures(self):
        # one rate-limited, rest succeed -> report separates them
        class YFRateLimitError(Exception):
            pass

        def fake(sym):
            if sym == "AAF.L":
                raise YFRateLimitError("Rate limited")
            return _bars(25)
        prov = YFinanceProvider(_fetch_fn=fake)
        r = D.run_dryrun(provider=prov, as_of=_AS_OF, cfg=OHLCVConfig())
        md = D.render(r, data_source="simulated_fixture")
        self.assertIn("provider_rate_limited (could not evaluate", md)
        self.assertIn("`AAF.L`", md)
        # the rate-limited symbol must not be counted as a data-quality failure
        data_line = [ln for ln in md.splitlines()
                     if ln.startswith("- data-quality failures")][0]
        self.assertIn("none", data_line)

    def test_elapsed_time_present(self):
        r = D.run_dryrun()
        self.assertIn("total_elapsed_seconds", r)
        self.assertTrue(all("elapsed_seconds" in p for p in r["per_symbol"]))


class ReportProvenance(unittest.TestCase):
    def test_committed_report_is_simulated_not_live(self):
        self.assertTrue(_REPORT.is_file(), "committed report missing")
        text = _REPORT.read_text()
        self.assertIn("data_source: **simulated_fixture**", text)
        self.assertIn("network: **disabled**", text)
        self.assertIn("not_live_yfinance: **true**", text)
        self.assertNotIn("data_source: **live_yfinance**", text)
        self.assertNotIn("network: **enabled**", text)

    def test_report_shows_required_fields(self):
        text = _REPORT.read_text()
        for token in ("symbols_checked", "provider_mode",
                      "total_elapsed_seconds", "Per-symbol result",
                      "Provider availability vs data quality",
                      "no broker / live / paper routing"):
            self.assertIn(token, text)


class ImportSafety(unittest.TestCase):
    """AST guard: the runner must not import broker/live/paper/Telegram/scanner/
    main runtime code."""

    def test_no_forbidden_runtime_imports(self):
        tree = ast.parse(_RUNNER.read_text())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        joined = " ".join(imported)
        for bad in ("scanner", "broker", "etoro", "risk", "telegram",
                    "paper", "live", "main"):
            self.assertNotIn(bad, joined.lower(),
                             "runner must not import %r (got %s)"
                             % (bad, imported))

    def test_only_expected_bot_import_is_uk_pilot(self):
        tree = ast.parse(_RUNNER.read_text())
        bot_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or
                                                     "").startswith("bot."):
                bot_imports.append(node.module)
        # the ONLY bot.* import is the pilot accessor
        self.assertEqual(bot_imports, ["bot.universe.uk_pilot"])


if __name__ == "__main__":
    unittest.main()
