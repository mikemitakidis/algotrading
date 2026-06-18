"""M20.UA — universe registry infrastructure proof tests.

Schema/contract validation, suffix mapping, registry queries, provenance,
isolation/safety guards, and backward-compatibility with the existing
bot.focus.FOCUS_SYMBOLS (which must remain untouched).
"""
import ast
import os
import pathlib
import subprocess
import unittest

import bot.universe as bu
from bot.universe import (
    SymbolRecord, AssetClass, DataQualityStatus, UniverseRegistry, suffixes,
)
from bot.focus import FOCUS_SYMBOLS

_PKG_DIR = pathlib.Path(__file__).resolve().parent / "bot" / "universe"
_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_SEED = _REPO_ROOT / "configs" / "universe" / "us_seed.json"
_BASELINE = "e823fe6779deaccc7b8ff7859c17b4dab564b868"
_M20A_HEAD = "5cdd0839204a71436579f9ed9a8c4a7d69681e87"
_TS = "2026-06-18T00:00:00+00:00"
_DATE = "2026-06-18"


def _record(**over):
    internal = over.get("internal_symbol", "NASDAQ:AAPL")
    ticker = internal.split(":")[1]
    base = dict(
        internal_symbol="NASDAQ:AAPL", provider_symbols={"yfinance": ticker},
        asset_class="EQUITY", name=ticker, exchange="NASDAQ", country="US",
        region="US", currency="USD", timezone="America/New_York",
        trading_calendar="XNAS", universe_tags=["legacy_focus"], active=True,
        scan_ready=False, source="test", as_of_date=_DATE, first_seen_utc=_TS)
    base.update(over)
    # keep provider symbol consistent with internal_symbol unless explicitly set
    if "provider_symbols" not in over:
        base["provider_symbols"] = {"yfinance": ticker}
    return SymbolRecord(**base)


class M20UASchema(unittest.TestCase):

    def test_phase_marker(self):
        self.assertEqual(bu.M20U_PHASE, "M20.UA")

    def test_round_trip(self):
        r = _record()
        self.assertEqual(SymbolRecord.from_dict(r.to_dict()).to_dict(),
                         r.to_dict())

    def test_required_fields_enforced(self):
        with self.assertRaises(TypeError):
            SymbolRecord(internal_symbol="NASDAQ:AAPL")  # missing required

    def test_unknown_field_rejected(self):
        d = _record().to_dict()
        d["surprise"] = 1
        with self.assertRaises(ValueError):
            SymbolRecord.from_dict(d)

    def test_nullable_liquidity_accepted(self):
        r = _record(avg_volume_20d=None, avg_dollar_volume_20d=None,
                    median_spread_bps=None, sector=None, industry=None)
        self.assertIsNone(r.avg_dollar_volume_20d)
        self.assertEqual(r.data_quality_status, DataQualityStatus.UNVERIFIED)

    def test_negative_liquidity_rejected_when_present(self):
        for bad in (dict(avg_volume_20d=-1),
                    dict(avg_dollar_volume_20d=-5),
                    dict(median_spread_bps=-0.1)):
            with self.assertRaises(ValueError, msg=str(bad)):
                _record(**bad)

    def test_invalid_currency_rejected(self):
        # USD is required for NASDAQ; EUR is inconsistent
        with self.assertRaises(ValueError):
            _record(currency="EUR")

    def test_invalid_timezone_rejected(self):
        with self.assertRaises(ValueError):
            _record(timezone="Mars/Olympus")

    def test_invalid_exchange_rejected(self):
        with self.assertRaises(ValueError):
            _record(internal_symbol="BOGUS:AAPL", exchange="BOGUS")

    def test_invalid_asset_class_rejected(self):
        with self.assertRaises(ValueError):
            _record(asset_class="CRYPTOPUNK")

    def test_invalid_region_rejected(self):
        with self.assertRaises(ValueError):
            _record(region="MOON")

    def test_exchange_mismatch_with_internal_symbol_rejected(self):
        with self.assertRaises(ValueError):
            _record(internal_symbol="NYSE:AAPL", exchange="NASDAQ")

    def test_yfinance_provider_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            _record(provider_symbols={"yfinance": "WRONG"})


class M20UASuffixes(unittest.TestCase):

    def test_mapping_examples(self):
        cases = {
            "NASDAQ:AAPL": "AAPL", "LSE:VOD": "VOD.L", "TSE:7203": "7203.T",
            "HKEX:0700": "0700.HK", "XETRA:SAP": "SAP.DE", "EPA:AIR": "AIR.PA",
            "AEX:ASML": "ASML.AS", "BME:SAN": "SAN.MC", "SIX:NESN": "NESN.SW",
        }
        for internal, expected in cases.items():
            self.assertEqual(suffixes.to_yfinance_symbol(internal), expected)

    def test_hk_zero_padding(self):
        self.assertEqual(suffixes.to_yfinance_symbol("HKEX:700"), "0700.HK")

    def test_malformed_internal_symbol_rejected(self):
        for bad in ("AAPL", "NASDAQ:", ":AAPL", "A:B:C"):
            with self.assertRaises(ValueError):
                suffixes.split_internal_symbol(bad)

    def test_consistency_table_complete(self):
        for ex, info in suffixes.EXCHANGES.items():
            self.assertTrue(info.country and info.currency and info.timezone
                            and info.trading_calendar and info.region)


class M20UARegistry(unittest.TestCase):

    def _reg(self, records):
        return UniverseRegistry(records)

    def test_duplicate_internal_symbol_rejected(self):
        with self.assertRaises(ValueError):
            self._reg([_record(), _record()])

    def test_duplicate_provider_symbol_rejected(self):
        # The schema forces the yfinance symbol to match the internal symbol,
        # so a yfinance collision across two internal symbols cannot occur.
        # A non-derived provider (e.g. alpaca) CAN collide -> registry rejects.
        r1 = _record(internal_symbol="NASDAQ:AAPL",
                     provider_symbols={"yfinance": "AAPL", "alpaca": "DUP"})
        r2 = _record(internal_symbol="NASDAQ:MSFT", name="MSFT",
                     provider_symbols={"yfinance": "MSFT", "alpaca": "DUP"})
        with self.assertRaises(ValueError):
            self._reg([r1, r2])

    def test_active_and_scan_ready_filters(self):
        reg = self._reg([
            _record(internal_symbol="NASDAQ:AAPL", active=True,
                    scan_ready=False),
            _record(internal_symbol="NASDAQ:MSFT", name="MSFT", active=True,
                    scan_ready=True),
            _record(internal_symbol="NASDAQ:NVDA", name="NVDA", active=False,
                    scan_ready=False)])
        self.assertEqual(len(reg.active_symbols()), 2)
        self.assertEqual(len(reg.scan_ready_symbols()), 1)

    def test_symbols_by_tag(self):
        reg = self._reg([
            _record(internal_symbol="ARCA:SPY", name="SPY", exchange="ARCA",
                    trading_calendar="XNYS", asset_class="ETF",
                    provider_symbols={"yfinance": "SPY"},
                    universe_tags=["etf", "legacy_focus"])])
        self.assertEqual(len(reg.symbols_by_tag("etf")), 1)
        self.assertEqual(len(reg.symbols_by_tag("nonexistent")), 0)

    def test_provider_symbol_lookup(self):
        reg = self._reg([_record()])
        self.assertEqual(reg.provider_symbol("NASDAQ:AAPL"), "AAPL")

    def test_unknown_symbol_clean_error(self):
        reg = self._reg([_record()])
        self.assertIsNone(reg.get("NASDAQ:ZZZZ"))
        with self.assertRaises(KeyError):
            reg.provider_symbol("NASDAQ:ZZZZ")

    def test_inactive_symbols_retained(self):
        reg = self._reg([
            _record(internal_symbol="NASDAQ:AAPL", active=True),
            _record(internal_symbol="NASDAQ:DELISTED", name="DELISTED",
                    active=False)])
        self.assertEqual(len(reg.all_symbols()), 2)
        self.assertIsNotNone(reg.get("NASDAQ:DELISTED"))


class M20UAProvenance(unittest.TestCase):

    def test_source_required(self):
        with self.assertRaises(ValueError):
            _record(source="")

    def test_as_of_date_required_and_valid(self):
        with self.assertRaises(ValueError):
            _record(as_of_date="not-a-date")

    def test_universe_status_has_survivorship_warning(self):
        txt = (_PKG_DIR / "UNIVERSE_STATUS.md").read_text().lower()
        self.assertIn("survivorship", txt)
        self.assertIn("not", txt)


class M20UASeed(unittest.TestCase):

    def setUp(self):
        self.reg = UniverseRegistry.load(_SEED)

    def test_seed_loads_89(self):
        self.assertEqual(len(self.reg), 89)

    def test_all_focus_symbols_mirrored(self):
        internal_tickers = {r.internal_symbol.split(":")[1]
                            for r in self.reg.all_symbols()}
        for sym in FOCUS_SYMBOLS:
            self.assertIn(sym, internal_tickers, f"{sym} missing from seed")

    def test_seed_all_active_none_scan_ready(self):
        self.assertEqual(len(self.reg.active_symbols()), 89)
        self.assertEqual(len(self.reg.scan_ready_symbols()), 0)

    def test_seed_all_us_and_legacy_tagged(self):
        for r in self.reg.all_symbols():
            self.assertEqual(r.country, "US")
            self.assertIn("legacy_focus", r.universe_tags)

    def test_seed_liquidity_null(self):
        for r in self.reg.all_symbols():
            self.assertIsNone(r.avg_dollar_volume_20d)
            self.assertIsNone(r.avg_volume_20d)
            self.assertIsNone(r.median_spread_bps)
            self.assertEqual(r.data_quality_status,
                             DataQualityStatus.UNVERIFIED)

    def test_seed_etfs_present(self):
        etfs = self.reg.symbols_by_tag("etf")
        self.assertEqual(len(etfs), 8)


class M20UASafetyGuards(unittest.TestCase):

    FORBIDDEN_ROOTS = {"ib_insync", "requests", "urllib", "aiohttp", "socket",
                       "http", "main", "dashboard", "sqlite3", "yfinance"}
    FORBIDDEN_PREFIXES = ("bot.paper", "bot.brokers", "bot.live", "bot.risk",
                          "bot.risk_authority", "bot.flywheel", "bot.scanner",
                          "bot.strategy", "dashboard", "main")

    def _iter(self):
        return sorted(_PKG_DIR.glob("*.py"))

    def test_no_forbidden_imports(self):
        offenders = []
        for path in self._iter():
            tree = ast.parse(path.read_text())
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        root = a.name.split(".")[0]
                        if root in self.FORBIDDEN_ROOTS or \
                                a.name.startswith(self.FORBIDDEN_PREFIXES):
                            offenders.append(f"{path.name}:{a.name}")
                elif isinstance(n, ast.ImportFrom) and n.module:
                    root = n.module.split(".")[0]
                    if root in self.FORBIDDEN_ROOTS or \
                            n.module.startswith(self.FORBIDDEN_PREFIXES):
                        offenders.append(f"{path.name}:{n.module}")
        self.assertEqual(offenders, [], f"forbidden imports: {offenders}")

    def test_no_db_network_tokens(self):
        for path in self._iter():
            src = path.read_text()
            for tok in ("sqlite3", "signals.db", "requests.", "urlopen",
                        "socket.socket"):
                self.assertNotIn(tok, src, f"{path.name}:{tok}")

    def test_file_open_only_in_registry(self):
        # registry.py legitimately reads JSON; no other module opens files.
        for path in self._iter():
            src = path.read_text()
            if path.name != "registry.py":
                self.assertNotIn("open(", src, f"{path.name} opens files")

    def test_no_data_path_tokens(self):
        for path in self._iter():
            src = path.read_text()
            for tok in ("data/ml", "data/m19", "data/paper"):
                self.assertNotIn(tok, src, f"{path.name}:{tok}")

    def test_import_writes_nothing(self):
        import importlib
        for d in (_REPO_ROOT / "data" / "paper", _REPO_ROOT / "data" / "m19"):
            before = sorted(p.name for p in d.glob("*")) if d.exists() else []
            importlib.import_module("bot.universe")
            after = sorted(p.name for p in d.glob("*")) if d.exists() else []
            self.assertEqual(before, after)


class M20UABackwardCompat(unittest.TestCase):

    def test_focus_still_imports(self):
        from bot.focus import FOCUS_SYMBOLS as f
        self.assertEqual(len(f), 89)

    def test_focus_symbols_exists(self):
        import bot.focus
        self.assertTrue(hasattr(bot.focus, "FOCUS_SYMBOLS"))

    def _unchanged(self, *paths, baseline=_BASELINE):
        r = subprocess.run(["git", "diff", "--name-only", baseline, "HEAD",
                            "--", *paths], capture_output=True, text=True,
                           timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "", f"{paths} changed")

    def test_focus_unchanged(self):
        self._unchanged("bot/focus.py")

    def test_main_unchanged(self):
        self._unchanged("main.py")

    def test_ml_build_dataset_unchanged(self):
        self._unchanged("ml_build_dataset.py")

    def test_paper_only_authorised_routing_diff(self):
        # bot/paper was frozen at the M20.A head (5cdd083). Later authorised
        # phases may add to it: M20.B adds routing.py + the __init__ export.
        # This must still fail if ANY other bot/paper file changes.
        r = subprocess.run(
            ["git", "diff", "--name-only", _M20A_HEAD, "HEAD", "--",
             "bot/paper"], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        changed = set(r.stdout.split())
        allowed = {"bot/paper/__init__.py", "bot/paper/routing.py",
                   "bot/paper/sizing.py"}
        self.assertTrue(
            changed <= allowed,
            f"unauthorised bot/paper change: {sorted(changed - allowed)}")

    def test_signal_scoring_unchanged(self):
        self._unchanged("bot/signal_scoring", baseline=_M20A_HEAD)


if __name__ == "__main__":
    unittest.main()
