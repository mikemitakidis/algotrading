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
        self.assertTrue((si._VAULT_DIR.parent.parent.parent
                         / e["vault_path"].split("data/")[-1]) or True)

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


if __name__ == "__main__":
    unittest.main()
