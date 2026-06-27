"""M21.U1 — global normaliser framework tests. Fixtures only; no real or
synthetic tickers committed (fixtures live here in the test file)."""
import ast
import json
import pathlib
import tempfile
import unittest

import bot.universe.global_expansion as gx
from bot.universe.schema import SymbolRecord

_REPO = pathlib.Path(__file__).resolve().parent
_GLOBAL = _REPO / "configs" / "universe" / "global_expanded.json"

_HEADER = ("region,index_source,exchange_prefix,local_ticker,yfinance_symbol,"
           "company_name,source_name,source_asof,verification_status")


def _csv(rows, header=_HEADER):
    return header + "\n" + "\n".join(rows) + ("\n" if rows else "")


def _write_csv(text):
    d = tempfile.mkdtemp()
    p = pathlib.Path(d) / "curated.csv"
    p.write_text(text, encoding="utf-8")
    return p


# a set of valid VERIFIED fixture rows across all first-cut regions
_VALID_ROWS = [
    "UK,FTSE100,LSE,HSBA,HSBA.L,HSBC Holdings,FTSE fixture,2026-06-30,VERIFIED",
    "HK,HSI,HKEX,0700,0700.HK,Tencent Holdings,HSI fixture,2026-06-30,VERIFIED",
    "JP,NIKKEI225,TSE,7203,7203.T,Toyota Motor,Nikkei fixture,2026-06-30,VERIFIED",
    "EU,DAX,XETRA,SAP,SAP.DE,SAP SE,DAX fixture,2026-06-30,VERIFIED",
    "EU,CAC,EPA,AIR,AIR.PA,Airbus,CAC fixture,2026-06-30,VERIFIED",
    "EU,AEX,AEX,ASML,ASML.AS,ASML Holding,AEX fixture,2026-06-30,VERIFIED",
    "EU,IBEX,BME,SAN,SAN.MC,Banco Santander,IBEX fixture,2026-06-30,VERIFIED",
    "EU,SMI,SIX,NESN,NESN.SW,Nestle,SMI fixture,2026-06-30,VERIFIED",
]


class U1EmptyCommittedFile(unittest.TestCase):
    def test_committed_global_expanded_is_empty(self):
        doc = json.loads(_GLOBAL.read_text())
        self.assertEqual(doc["schema_version"], "m21u_global_candidates_v1")
        self.assertEqual(doc["symbols"], [])

    def test_empty_envelope_helper(self):
        env = gx.empty_envelope()
        self.assertEqual(env["symbols"], [])


class U1Parsing(unittest.TestCase):
    def test_valid_csv_parses(self):
        rows = gx.parse_curated_csv(_write_csv(_csv(_VALID_ROWS)))
        self.assertEqual(len(rows), len(_VALID_ROWS))

    def test_missing_required_column_rejected(self):
        bad = _csv(["UK,FTSE100,LSE,HSBA,HSBA.L,HSBC,src,2026-06-30,VERIFIED"],
                   header=_HEADER.replace(",verification_status", ""))
        with self.assertRaises(gx.NormaliserError):
            gx.parse_curated_csv(_write_csv(bad))

    def test_unknown_column_rejected(self):
        bad = _csv(
            ["UK,FTSE100,LSE,HSBA,HSBA.L,HSBC,src,2026-06-30,VERIFIED,oops"],
            header=_HEADER + ",mystery")
        with self.assertRaises(gx.NormaliserError):
            gx.parse_curated_csv(_write_csv(bad))

    def test_documented_optionals_accepted(self):
        hdr = _HEADER + ",isin,weight,sector,notes"
        row = ("UK,FTSE100,LSE,HSBA,HSBA.L,HSBC,src,2026-06-30,VERIFIED,"
               "GB0005405286,5.1,Financials,big bank")
        rows = gx.parse_curated_csv(_write_csv(_csv([row], header=hdr)))
        self.assertEqual(len(rows), 1)

    def test_formula_injection_rejected(self):
        for cell in ("=cmd", "+1", "-bad", "@x"):
            row = (f"UK,FTSE100,LSE,HSBA,HSBA.L,{cell},src,2026-06-30,VERIFIED")
            with self.assertRaises(gx.NormaliserError):
                gx.parse_curated_csv(_write_csv(_csv([row])))


class U1BuildAndStatus(unittest.TestCase):
    def _build(self, rows):
        return gx.build_records(gx.parse_curated_csv(_write_csv(_csv(rows))))

    def test_only_verified_become_records(self):
        rows = list(_VALID_ROWS) + [
            "UK,FTSE100,LSE,RRX,RRX.L,Rolls Royce,src,2026-06-30,NEEDS_REVIEW",
            "UK,FTSE100,LSE,BPX,BPX.L,BP,src,2026-06-30,EXCLUDE",
        ]
        res = self._build(rows)
        self.assertEqual(res["count"], len(_VALID_ROWS))
        self.assertEqual(res["skipped"].get("NEEDS_REVIEW"), 1)
        self.assertEqual(res["skipped"].get("EXCLUDE"), 1)

    def test_unknown_status_rejected(self):
        rows = ["UK,FTSE100,LSE,HSBA,HSBA.L,HSBC,src,2026-06-30,MAYBE"]
        with self.assertRaises(gx.NormaliserError):
            self._build(rows)

    def test_all_records_validate_and_are_inactive(self):
        res = self._build(_VALID_ROWS)
        for rec in res["envelope"]["symbols"]:
            SymbolRecord.from_dict(rec)  # must not raise
            self.assertFalse(rec["active"])
            self.assertFalse(rec["scan_ready"])
            self.assertEqual(rec["data_quality_status"], "unverified")
            # liquidity fields null / absent
            for f in ("avg_volume_20d", "avg_dollar_volume_20d",
                      "median_spread_bps", "min_liquidity_tier"):
                self.assertIsNone(rec.get(f))
            self.assertIn("global_candidate", rec["universe_tags"])

    def test_no_non_schema_execution_fields(self):
        res = self._build(_VALID_ROWS)
        for rec in res["envelope"]["symbols"]:
            self.assertNotIn("execution_eligible", rec)
            self.assertNotIn("paper_routing_eligible", rec)

    def test_yfinance_suffix_mapping(self):
        res = self._build(_VALID_ROWS)
        by = {r["internal_symbol"]: r["provider_symbols"]["yfinance"]
              for r in res["envelope"]["symbols"]}
        self.assertEqual(by["LSE:HSBA"], "HSBA.L")
        self.assertEqual(by["TSE:7203"], "7203.T")
        self.assertEqual(by["XETRA:SAP"], "SAP.DE")
        self.assertEqual(by["EPA:AIR"], "AIR.PA")
        self.assertEqual(by["AEX:ASML"], "ASML.AS")
        self.assertEqual(by["BME:SAN"], "SAN.MC")
        self.assertEqual(by["SIX:NESN"], "NESN.SW")

    def test_hk_zero_padding(self):
        res = self._build([
            "HK,HSI,HKEX,5,0005.HK,HSBC HK,src,2026-06-30,VERIFIED"])
        rec = res["envelope"]["symbols"][0]
        self.assertEqual(rec["provider_symbols"]["yfinance"], "0005.HK")


class U1Rejections(unittest.TestCase):
    def _build(self, rows):
        return gx.build_records(gx.parse_curated_csv(_write_csv(_csv(rows))))

    def test_duplicate_internal_symbol_rejected(self):
        rows = [
            "UK,FTSE100,LSE,HSBA,HSBA.L,HSBC,src,2026-06-30,VERIFIED",
            "UK,FTSE100,LSE,HSBA,HSBA.L,HSBC dup,src,2026-06-30,VERIFIED"]
        with self.assertRaises(gx.NormaliserError):
            self._build(rows)

    def test_unsupported_exchange_rejected(self):
        rows = ["CN,CSI,SSE,600000,600000.SS,SPDB,src,2026-06-30,VERIFIED"]
        with self.assertRaises(gx.NormaliserError):
            self._build(rows)

    def test_mismatched_yfinance_rejected(self):
        rows = ["UK,FTSE100,LSE,HSBA,HSBA.WRONG,HSBC,src,2026-06-30,VERIFIED"]
        with self.assertRaises(gx.NormaliserError):
            self._build(rows)

    def test_whitespace_in_ticker_rejected(self):
        rows = ["UK,FTSE100,LSE,HS BA,HS BA.L,HSBC,src,2026-06-30,VERIFIED"]
        with self.assertRaises(gx.NormaliserError):
            self._build(rows)

    def test_bad_source_asof_rejected(self):
        for bad in ("2026/06/30", "2026-13-01", "2026-02-31", "20260630"):
            rows = [f"UK,FTSE100,LSE,HSBA,HSBA.L,HSBC,src,{bad},VERIFIED"]
            with self.assertRaises(gx.NormaliserError):
                self._build(rows)

    def test_real_us_collision_rejected(self):
        # pull a real US internal symbol from the committed US registry and
        # craft a fixture row that collides on it -> must hard-fail.
        us_internals, us_yfs = gx._load_us_identifiers()
        self.assertTrue(us_internals, "US registry should be non-empty")
        # find a US symbol on a supported global-able exchange? US prefixes are
        # NASDAQ/NYSE/ARCA which are valid in suffixes; craft a colliding row.
        sample = sorted(us_internals)[0]  # e.g. "NASDAQ:AAPL"
        ex, tic = sample.split(":")
        yf = gx._suffixes.to_yfinance_symbol(sample)
        row = (f"ADR,US,{ex},{tic},{yf},Collider,src,2026-06-30,VERIFIED")
        rows = gx.parse_curated_csv(_write_csv(_csv([row])))
        with self.assertRaises(gx.NormaliserError):
            gx.build_records(rows)


class U1Determinism(unittest.TestCase):
    def test_byte_identical_across_two_builds(self):
        import hashlib
        p = _write_csv(_csv(list(reversed(_VALID_ROWS))))  # unsorted input
        e1 = gx.build_records(gx.parse_curated_csv(p))["envelope"]
        e2 = gx.build_records(gx.parse_curated_csv(p))["envelope"]
        b1 = gx._canonical_json(e1).encode()
        b2 = gx._canonical_json(e2).encode()
        self.assertEqual(hashlib.sha256(b1).hexdigest(),
                         hashlib.sha256(b2).hexdigest())
        # sorted by internal_symbol
        syms = [r["internal_symbol"] for r in e1["symbols"]]
        self.assertEqual(syms, sorted(syms))


class U1Diff(unittest.TestCase):
    def test_diff_reports_and_does_not_apply(self):
        rows = _VALID_ROWS[:3]
        env = gx.build_records(gx.parse_curated_csv(_write_csv(_csv(rows))))[
            "envelope"]
        # existing fixture file with a different set (one overlapping, one to
        # be 'removed')
        d = tempfile.mkdtemp()
        existing = pathlib.Path(d) / "global_expanded.json"
        prev = gx.build_records(gx.parse_curated_csv(_write_csv(_csv(
            [_VALID_ROWS[0], _VALID_ROWS[4]]))))["envelope"]
        existing.write_text(gx._canonical_json(prev), encoding="utf-8")
        diff = gx.diff_against_existing(env, existing)
        self.assertFalse(diff["auto_apply"])
        self.assertIn("removed_advisory_only", diff)
        # the file is untouched by diff
        self.assertEqual(json.loads(existing.read_text()), prev)


class U1Isolation(unittest.TestCase):
    def _imports(self):
        tree = ast.parse(pathlib.Path(gx.__file__).read_text())
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                imported.add(n.module)
        return imported

    def test_no_forbidden_imports(self):
        imp = self._imports()
        forbidden = {"requests", "urllib", "socket", "http", "aiohttp",
                     "yfinance", "alpaca", "ib_insync"}
        roots = {m.split(".")[0] for m in imp}
        self.assertEqual(roots & forbidden, set())

    def test_production_import_whitelist(self):
        # production module may import stdlib + bot.universe.schema +
        # bot.universe.suffixes ONLY (no bot.universe.registry, no runtime).
        imp = self._imports()
        bot_imports = {m for m in imp if m.startswith("bot")}
        self.assertEqual(bot_imports,
                         {"bot.universe", "bot.universe.schema",
                          "bot.universe.suffixes"} & bot_imports
                         or bot_imports)
        # explicit: registry / active_selection / scanner / paper must be absent
        for forbidden in ("bot.universe.registry",
                          "bot.universe.active_selection", "bot.scanner",
                          "bot.paper", "bot.live", "bot.brokers",
                          "bot.providers"):
            self.assertNotIn(forbidden, imp)


class U1RuntimeSafety(unittest.TestCase):
    def test_global_file_not_in_default_paths(self):
        from bot.universe import active_selection as a
        paths = [str(p) for p in a._DEFAULT_PATHS]
        self.assertTrue(all("global_expanded.json" not in p for p in paths),
                        "global_expanded.json must NOT be in _DEFAULT_PATHS")

    def test_scan_ready_remains_536(self):
        from bot.universe.active_selection import get_scan_ready_symbols
        self.assertEqual(len(get_scan_ready_symbols()), 536)


if __name__ == "__main__":
    unittest.main()
