"""M21.U0 — source ingestion / raw vault tests.

Isolation is checked via AST (imports) and write-target analysis, NOT raw string
scanning, so docstrings/comments cannot cause false failures.
"""
import ast
import importlib
import json
import pathlib
import tempfile
import unittest

import bot.universe.source_ingest as si


class U0VaultIngest(unittest.TestCase):
    def setUp(self):
        # redirect the vault + ledger to a temp area so tests never touch the
        # real repo paths.
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self._orig_vault = si._VAULT_DIR
        self._orig_ledger = si._LEDGER
        si._VAULT_DIR = root / "data" / "universe" / "raw_sources"
        si._LEDGER = root / "configs" / "universe" / "source_registry.json"
        si._VAULT_DIR.mkdir(parents=True)
        si._LEDGER.parent.mkdir(parents=True)
        # a fixture source file
        self._src = root / "ftse100.csv"
        self._src.write_text("ticker,name\nHSBA,HSBC Holdings\n",
                             encoding="utf-8")

    def tearDown(self):
        si._VAULT_DIR = self._orig_vault
        si._LEDGER = self._orig_ledger
        self._tmp.cleanup()

    def _ingest(self, **kw):
        base = dict(file=str(self._src), region="UK", index_source="FTSE100",
                    source_name="test", source_type="etf_holdings",
                    source_asof="2026-06-30")
        base.update(kw)
        return si.ingest(**base)

    def test_ingest_vaults_and_records(self):
        res = self._ingest()
        self.assertTrue(res["ok"])
        self.assertFalse(res["noop"])
        led = json.loads(si._LEDGER.read_text())
        self.assertEqual(led["schema_version"], "m21u0_source_registry_v1")
        self.assertEqual(len(led["sources"]), 1)
        e = led["sources"][0]
        self.assertEqual(e["region"], "UK")
        self.assertEqual(len(e["sha256"]), 64)
        self.assertEqual(e["ingest_method"], "upload")
        # the vaulted file must actually exist on disk
        vaulted = si._vault_root() / e["vault_path"]
        self.assertTrue(vaulted.is_file(), f"vaulted file missing: {vaulted}")
        # and its SHA-256 must equal the ledger digest and the source digest
        self.assertEqual(si._sha256_file(vaulted), e["sha256"])
        self.assertEqual(si._sha256_file(self._src), e["sha256"])

    def test_idempotent_identical_bytes(self):
        self._ingest()
        res2 = self._ingest()
        self.assertTrue(res2["ok"])
        self.assertTrue(res2["noop"])
        led = json.loads(si._LEDGER.read_text())
        self.assertEqual(len(led["sources"]), 1)  # no new entry

    def test_content_changed_creates_new_entry_keeps_old(self):
        self._ingest()
        vault_files_before = list(si._VAULT_DIR.rglob("*.csv"))
        # different bytes, same logical source/date
        self._src.write_text("ticker,name\nHSBA,HSBC\nSHEL,Shell\n",
                             encoding="utf-8")
        res = self._ingest()
        self.assertTrue(res["ok"])
        self.assertFalse(res["noop"])
        self.assertTrue(res["content_changed"])
        led = json.loads(si._LEDGER.read_text())
        self.assertEqual(len(led["sources"]), 2)        # new entry
        vault_files_after = list(si._VAULT_DIR.rglob("*.csv"))
        self.assertEqual(len(vault_files_after), 2)      # old file kept
        for f in vault_files_before:
            self.assertTrue(f.exists())                  # prior file untouched

    def test_verify_detects_tamper(self):
        self._ingest()
        # corrupt a vaulted file
        vf = next(si._VAULT_DIR.rglob("*.csv"))
        vf.write_text("TAMPERED", encoding="utf-8")
        res = si.verify()
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["mismatches"]), 1)

    def test_bad_region_rejected(self):
        res = self._ingest(region="CN")  # out of scope in M21.U0
        self.assertFalse(res["ok"])
        self.assertIn("bad_region", res["reason"])

    # ── M21.U0.H input hardening ──
    def test_bad_source_asof_format_rejected(self):
        for bad in ("2026/06/30", "20260630", "2026-6-30", "2026-06-30 ",
                    " 2026-06-30", "2026-06-30T00:00:00", ""):
            res = self._ingest(source_asof=bad)
            self.assertFalse(res["ok"], f"{bad!r} should be rejected")
            self.assertIn("validation", res["reason"])

    def test_invalid_calendar_date_rejected(self):
        for bad in ("2026-02-31", "2026-13-01", "2026-00-10", "2026-06-00"):
            res = self._ingest(source_asof=bad)
            self.assertFalse(res["ok"], f"{bad!r} should be rejected")
            self.assertIn("validation", res["reason"])

    def test_source_asof_traversal_rejected(self):
        for bad in ("../etc", "2026-06-../", "..-..-..", "2026-06-30/.."):
            res = self._ingest(source_asof=bad)
            self.assertFalse(res["ok"])
            self.assertIn("validation", res["reason"])

    def test_bad_index_source_rejected(self):
        for bad in ("FTSE/100", "FTSE 100", "ftse100..", "../x", "FT\\SE",
                    "", "FTSE;100"):
            res = self._ingest(index_source=bad)
            self.assertFalse(res["ok"], f"{bad!r} should be rejected")
            self.assertIn("validation", res["reason"])

    def test_path_traversal_does_not_escape_vault(self):
        # even a crafted index_source must never produce a path outside the
        # vault dir (validation rejects it before any path is built).
        res = self._ingest(index_source="../../../../etc/passwd")
        self.assertFalse(res["ok"])
        self.assertIn("validation", res["reason"])
        # nothing was vaulted
        self.assertEqual(len(list(si._VAULT_DIR.rglob("*"))), 0)

    def test_valid_ingest_still_works_after_hardening(self):
        res = self._ingest(index_source="FTSE100", source_asof="2026-06-30")
        self.assertTrue(res["ok"])
        self.assertFalse(res["noop"])
        e = json.loads(si._LEDGER.read_text())["sources"][0]
        self.assertEqual(si._sha256_file(si._vault_root() / e["vault_path"]),
                         e["sha256"])

    def test_ledger_schema_version_validated(self):
        # a tampered ledger schema_version must be rejected on load.
        self._ingest()  # create a valid ledger first
        doc = json.loads(si._LEDGER.read_text())
        doc["schema_version"] = "bogus_v999"
        si._LEDGER.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(si.IngestValidationError):
            si._load_ledger()

    def test_filename_stamp_is_utc_z(self):
        res = self._ingest()
        e = json.loads(si._LEDGER.read_text())["sources"][0]
        fname = pathlib.Path(e["vault_path"]).name
        # stamp segment looks like YYYYMMDDThhmmssZ
        import re as _re
        self.assertTrue(_re.search(r"\d{8}T\d{6}Z", fname),
                        f"no UTC-Z stamp in {fname}")

    def test_no_deletion_path(self):
        # the module must expose no delete/remove/unlink behaviour
        src = pathlib.Path(si.__file__).read_text()
        tree = ast.parse(src)
        called = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute):
                called.add(n.attr)
        for forbidden in ("unlink", "rmtree", "remove", "rmdir"):
            self.assertNotIn(forbidden, called,
                             f"deletion call {forbidden} present")


class U0Isolation(unittest.TestCase):
    def _imports(self):
        tree = ast.parse(pathlib.Path(si.__file__).read_text())
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                imported.add(n.module)
                imported.add(n.module.split(".")[0])
        return imported

    def test_no_forbidden_imports(self):
        imported = self._imports()
        forbidden = {"requests", "urllib", "socket", "http", "aiohttp",
                     "yfinance", "alpaca", "ib_insync"}
        self.assertEqual(imported & forbidden, set())

    def test_no_repo_runtime_imports(self):
        imported = self._imports()
        for mod in ("bot.scanner", "bot.paper", "bot.live", "bot.brokers",
                    "bot.providers", "bot.universe.registry",
                    "bot.universe.active_selection"):
            self.assertNotIn(mod, imported)

    def test_only_stdlib_and_self(self):
        # every import is stdlib (no third-party, no bot.*)
        imported = self._imports()
        nonstd = {m for m in imported if m.startswith("bot")}
        self.assertEqual(nonstd, set(),
                         f"unexpected bot.* imports: {nonstd}")

    def test_scan_ready_unchanged(self):
        # the vault tool must not perturb the runtime universe selector.
        from bot.universe.active_selection import get_scan_ready_symbols
        self.assertEqual(len(get_scan_ready_symbols()), 536)


if __name__ == "__main__":
    unittest.main()
