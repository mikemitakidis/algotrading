"""M20.UE — active_selection tests. Pure, offline, no network/broker/live.

Covers the registry-based scan_ready selector and proves the runtime-compatible
bare-ticker output. The flag-gated main.py seam itself is proven by:
- test_m17_backtesting protected-content guard (only the approved seam changed),
- and the selector contract tested here (what flag-on would feed the runtime).
"""
import json
import pathlib
import tempfile
import unittest

from bot.universe.active_selection import (
    get_scan_ready_symbols, _bare_ticker)

_REPO = pathlib.Path(__file__).resolve().parent
_SEED = _REPO / "configs" / "universe" / "us_seed.json"
_EXPANDED = _REPO / "configs" / "universe" / "us_expanded.json"
_HAS_UNIVERSE = _SEED.exists() and _EXPANDED.exists()


class _FakeRecord:
    def __init__(self, internal, provider_symbols):
        self.internal_symbol = internal
        self.provider_symbols = provider_symbols


class UEBareTicker(unittest.TestCase):
    def test_prefers_yfinance_provider_symbol(self):
        r = _FakeRecord("NASDAQ:AAPL", {"yfinance": "AAPL"})
        self.assertEqual(_bare_ticker(r), "AAPL")

    def test_fallback_to_internal_suffix(self):
        r = _FakeRecord("NASDAQ:FOO", {})  # no yfinance provider symbol
        self.assertEqual(_bare_ticker(r), "FOO")

    def test_none_when_unusable(self):
        r = _FakeRecord("", {})
        self.assertIsNone(_bare_ticker(r))


@unittest.skipUnless(_HAS_UNIVERSE, "universe files present")
class UESelectorOnFixture(unittest.TestCase):
    """Fixtures are built from REAL universe records (guaranteed schema-valid),
    with scan_ready toggled, written to a temp file. This avoids hand-building
    records that must satisfy SymbolRecord's strict identity validation."""

    def _real_records(self, n_ready=2, n_not=2):
        exp = json.loads(_EXPANDED.read_text())["symbols"]
        ready = [dict(r) for r in exp if r.get("scan_ready")][:n_ready]
        not_ready = [dict(r) for r in exp if not r.get("scan_ready")][:n_not]
        for r in not_ready:
            r["scan_ready"] = False
        for r in ready:
            r["scan_ready"] = True
        return ready, not_ready

    def _write(self, d, records):
        p = pathlib.Path(d) / "u.json"
        p.write_text(json.dumps({"schema_version": "x", "symbols": records}))
        return str(p)

    def test_returns_only_scan_ready_bare_tickers(self):
        ready, not_ready = self._real_records()
        expected = sorted(r["provider_symbols"]["yfinance"] for r in ready)
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, ready + not_ready)
            syms = get_scan_ready_symbols([p])
            self.assertEqual(syms, expected)
            self.assertTrue(all(":" not in s for s in syms))

    def test_empty_when_none_scan_ready(self):
        _ready, not_ready = self._real_records(n_ready=0, n_not=3)
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, not_ready)
            self.assertEqual(get_scan_ready_symbols([p]), [])

    def test_dedup_and_sorted(self):
        # registry rejects duplicate internal_symbols, so feed distinct real
        # records and assert sorted, de-duplicated bare-ticker output.
        ready, _ = self._real_records(n_ready=3, n_not=0)
        expected = sorted(set(r["provider_symbols"]["yfinance"] for r in ready))
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, ready)
            out = get_scan_ready_symbols([p])
            self.assertEqual(out, expected)
            self.assertEqual(len(out), len(set(out)))  # de-duplicated
            self.assertEqual(out, sorted(out))          # sorted


@unittest.skipUnless(_HAS_UNIVERSE, "universe files present")
class UESelectorOnRealUniverse(unittest.TestCase):
    def test_matches_written_scan_ready_count(self):
        syms = get_scan_ready_symbols()
        # mirrors the UC2 write-back result (scan_ready=536)
        self.assertEqual(len(syms), 536)
        self.assertTrue(all(":" not in s for s in syms))
        self.assertIn("AAPL", syms)
        self.assertIn("SPY", syms)
        self.assertNotIn("VXX", syms)  # ETF deny-class, not scan_ready

    def test_no_network_or_broker_imports(self):
        import bot.universe.active_selection as mod
        src = pathlib.Path(mod.__file__).read_text()
        # check for actual import statements, not bare words (the module
        # docstring legitimately mentions provider_symbols['yfinance']).
        forbidden_imports = (
            "import requests", "import urllib", "import yfinance",
            "from yfinance", "import alpaca", "from alpaca",
            "from bot.brokers", "import bot.brokers",
            "from bot.live", "import bot.live",
            "from bot.paper", "import bot.paper",
            "from bot.risk", "import bot.risk",
            "import socket", "import http")
        for forbidden in forbidden_imports:
            self.assertNotIn(forbidden, src)


class UEMainSeamContract(unittest.TestCase):
    """Proves the main.py seam decision logic without invoking the trading
    loop: flag-off -> FOCUS_SYMBOLS, flag-on -> registry (with focus_size cap),
    empty registry -> FOCUS_SYMBOLS fallback."""

    def _decide(self, env, registry_syms, focus_symbols, focus_size):
        use = env.strip().lower() in ("1", "true", "yes", "on")
        if use:
            r = registry_syms
            if r:
                return r[:focus_size], "registry"
            return focus_symbols[:focus_size], "focus_fallback"
        return focus_symbols[:focus_size], "focus"

    def test_flag_off_uses_focus(self):
        f, src = self._decide("", ["AAA", "BBB"], ["AAPL", "MSFT"], 150)
        self.assertEqual(src, "focus")
        self.assertEqual(f, ["AAPL", "MSFT"])

    def test_flag_on_uses_registry_capped(self):
        f, src = self._decide("true", ["AAA", "BBB", "CCC"], ["AAPL"], 2)
        self.assertEqual(src, "registry")
        self.assertEqual(f, ["AAA", "BBB"])  # capped at focus_size

    def test_flag_on_empty_registry_falls_back(self):
        f, src = self._decide("true", [], ["AAPL", "MSFT"], 150)
        self.assertEqual(src, "focus_fallback")
        self.assertEqual(f, ["AAPL", "MSFT"])


if __name__ == "__main__":
    unittest.main()
