#!/usr/bin/env python3
"""C5b (M21 safety-test repair) — stale protected-file guards, re-baselined
without weakening them.

25 protected-file / frozen-file guards in 17 modules compared protected
production files against milestone baselines that later, approved milestones
legitimately changed (bot/scanner.py, bot/signal_scoring/gates.py,
bot/brokers/ibkr_broker.py). The guards keep their original baselines and
full protected lists; _protected_baselines excuses a changed file ONLY when the
change is exactly a recorded approved transition (from_blob at the baseline,
to_blob now).

What this module proves:
  1. The approved-change table is true to git history: each entry's commit is
     an ancestor of HEAD and produced exactly to_blob, every baseline the
     guards use holds exactly from_blob, and the checkout holds to_blob.
  2. The helper's semantics, in a throwaway git repo: exact transition only;
     any other content, any other baseline content, any path outside the
     table, and a mismatch on the side the guard diffs (working tree vs HEAD)
     are all NOT excused.
  3. Static guard integrity (AST/text only): the 17 modules still use their
     original baselines, still protect the three files, and route their
     result through the helper.

No project module imported, no network, no production file touched.
"""
from __future__ import annotations

import ast
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

import _test_isolation  # noqa: E402
_test_isolation.install()
import _protected_baselines as PB  # noqa: E402

REPO = Path(__file__).resolve().parent

# Every baseline revision a guard compares a drifted file against.
_GUARD_BASELINES = {
    "bot/scanner.py": ("648682c", "60281c4", "ae8fb0d", "384e484",
                       "d73a04a", "ceb8cd5"),
    "bot/signal_scoring/gates.py": ("e823fe6", "5cdd083"),
    "bot/brokers/ibkr_broker.py": ("e823fe6",),
}

# module -> (baseline literals that must still be present, protected path
# literals that must still be present)
_GUARD_MODULES = {
    "test_m15_3_a_2_totp.py": (("648682c",), ("bot/scanner.py",)),
    "test_m15_3_a_dashboard_auth.py": (("60281c4",), ("bot/scanner.py",)),
    "test_m15_3_b_manual_reset.py": (("ae8fb0d",), ("bot/scanner.py",)),
    "test_m15_3_c_audit_export.py": (("384e484",), ("bot/scanner.py",)),
    "test_m15_5_ibkr_exposure.py": (("d73a04a",), ("bot/scanner.py",)),
    "test_m16_historical_data.py": (("ceb8cd5",), ("bot/scanner.py",)),
    "test_m20_paper.py": (
        ("e823fe6779deaccc7b8ff7859c17b4dab564b868",),
        ("bot/signal_scoring", "bot/scanner.py", "bot/brokers")),
    "test_m20_universe.py": (
        ("e823fe6779deaccc7b8ff7859c17b4dab564b868",
         "5cdd0839204a71436579f9ed9a8c4a7d69681e87"),
        ("bot/signal_scoring", "bot/focus.py", "ml_build_dataset.py")),
}
for _m in ("routing", "sizing", "orders_fills", "positions_pnl", "closing",
           "account_ledger", "storage"):
    _GUARD_MODULES[f"test_m20_paper_{_m}.py"] = (
        ("e823fe6779deaccc7b8ff7859c17b4dab564b868",),
        ("bot/signal_scoring", "bot/scanner.py", "bot/brokers"))
for _m in ("universe_expansion", "universe_quality_collector"):
    _GUARD_MODULES[f"test_m20_{_m}.py"] = (
        ("e823fe6779deaccc7b8ff7859c17b4dab564b868",),
        ("bot/signal_scoring", "bot/scanner.py", "bot/brokers"))


def _git(*args, cwd=REPO):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, timeout=20)
    return r.returncode, r.stdout.strip()


class TestApprovedTableMatchesHistory(unittest.TestCase):

    def test_table_covers_exactly_the_three_drifted_files(self):
        # Adding an entry must be a reviewed change to this test as well.
        self.assertEqual(sorted(PB.APPROVED_CHANGES), sorted(_GUARD_BASELINES))

    def test_approving_commit_is_ancestor_and_produced_to_blob(self):
        for rel, e in PB.APPROVED_CHANGES.items():
            with self.subTest(rel=rel):
                rc, _ = _git("merge-base", "--is-ancestor", e.commit, "HEAD")
                self.assertEqual(rc, 0, f"{e.commit} not an ancestor of HEAD")
                self.assertEqual(_git("rev-parse", f"{e.commit}:{rel}")[1],
                                 e.to_blob)

    def test_every_guard_baseline_holds_from_blob(self):
        for rel, baselines in _GUARD_BASELINES.items():
            for b in baselines:
                with self.subTest(rel=rel, baseline=b):
                    self.assertEqual(PB.rev_blob(b, rel),
                                     PB.APPROVED_CHANGES[rel].from_blob)

    def test_checkout_holds_to_blob(self):
        for rel, e in PB.APPROVED_CHANGES.items():
            with self.subTest(rel=rel):
                self.assertEqual(PB.rev_blob("HEAD", rel), e.to_blob)
                self.assertEqual(PB.worktree_blob(rel), e.to_blob)

    def test_worktree_blob_matches_git_hash_object(self):
        for rel in PB.APPROVED_CHANGES:
            with self.subTest(rel=rel):
                self.assertEqual(PB.worktree_blob(rel),
                                 _git("hash-object", "--", rel)[1])

    def test_currently_excused_against_every_guard_baseline(self):
        for rel, baselines in _GUARD_BASELINES.items():
            for b in baselines:
                with self.subTest(rel=rel, baseline=b):
                    self.assertTrue(PB.is_approved_change(b, rel))
                    self.assertTrue(PB.is_approved_change(b, rel,
                                                          target="HEAD"))


class TestHelperSemantics(unittest.TestCase):
    """Behaviour in a throwaway repo, independent of this repo's history."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="c5b_pb_")
        self.repo = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._g("init", "-q")
        self.base = self._commit("v1\n")
        self.approved = self._commit("v2\n")
        self.table = {"f.py": PB.ApprovedChange(
            from_blob=self._blob("v1\n"), to_blob=self._blob("v2\n"),
            commit=self.approved, approval="test")}

    def _g(self, *args):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                            "-c", "commit.gpgsign=false", *args],
                           cwd=str(self.repo), capture_output=True, text=True,
                           timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def _commit(self, text):
        (self.repo / "f.py").write_text(text)
        (self.repo / "other.py").write_text("o\n")
        self._g("add", "-A")
        self._g("commit", "-q", "-m", text.strip())
        return self._g("rev-parse", "HEAD")

    @staticmethod
    def _blob(text):
        data = text.encode()
        return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

    def _ok(self, baseline=None, rel="f.py", target=None):
        return PB.is_approved_change(baseline or self.base, rel, self.repo,
                                     self.table, target)

    def test_exact_transition_is_excused(self):
        self.assertTrue(self._ok())
        self.assertTrue(self._ok(target="HEAD"))
        self.assertEqual(PB.unapproved(self.base, ["f.py"], self.repo,
                                       self.table), [])

    def test_any_other_working_tree_content_is_not_excused(self):
        (self.repo / "f.py").write_text("v2\n# one extra line\n")
        self.assertFalse(self._ok())
        self.assertEqual(PB.unapproved(self.base, ["f.py"], self.repo,
                                       self.table), ["f.py"])

    def test_target_head_checks_committed_content_not_worktree(self):
        # Committed content is approved but the working tree is dirty:
        # a working-tree guard fails, a HEAD guard passes.
        (self.repo / "f.py").write_text("dirty\n")
        self.assertFalse(self._ok())
        self.assertTrue(self._ok(target="HEAD"))
        # Committed content is NOT approved but the working tree is restored
        # to the approved bytes: a HEAD guard must still fail.
        self._commit("v3\n")
        (self.repo / "f.py").write_text("v2\n")
        self.assertTrue(self._ok())
        self.assertFalse(self._ok(target="HEAD"))

    def test_baseline_with_other_content_is_not_excused(self):
        # From the approved commit itself the file is not at from_blob.
        self.assertFalse(self._ok(baseline=self.approved))

    def test_path_outside_table_is_never_excused(self):
        (self.repo / "other.py").write_text("changed\n")
        self.assertFalse(self._ok(rel="other.py"))
        self.assertEqual(PB.unapproved(self.base, ["other.py", "f.py"],
                                       self.repo, self.table), ["other.py"])

    def test_missing_file_is_not_excused(self):
        (self.repo / "f.py").unlink()
        self.assertFalse(self._ok())


class TestGuardIntegrity(unittest.TestCase):
    """Static only: nothing is imported or executed."""

    def _src(self, name):
        return (REPO / name).read_text()

    def test_modules_keep_baselines_and_protected_paths(self):
        for name, (baselines, paths) in _GUARD_MODULES.items():
            src = self._src(name)
            strings = {n.value for n in ast.walk(ast.parse(src))
                       if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            with self.subTest(module=name):
                for b in baselines:
                    self.assertIn(b, strings, f"baseline {b} removed")
                for p in paths:
                    self.assertIn(p, strings, f"protected path {p} removed")

    def test_modules_route_results_through_helper(self):
        for name in _GUARD_MODULES:
            src = self._src(name)
            with self.subTest(module=name):
                self.assertIn("import _protected_baselines", src)
                self.assertRegex(src, r"_protected_baselines\.(is_approved_change|unapproved)\(")

    def test_head_based_guards_check_head_not_worktree(self):
        # The M20 guards diff <baseline> HEAD, so they must pass target="HEAD".
        for name in _GUARD_MODULES:
            if not name.startswith("test_m20_"):
                continue
            src = self._src(name)
            with self.subTest(module=name):
                n_calls = src.count("_protected_baselines.unapproved(")
                self.assertGreater(n_calls, 0)
                self.assertEqual(src.count('target="HEAD")'), n_calls)

    def test_helper_imports_no_project_module(self):
        tree = ast.parse((REPO / "_protected_baselines.py").read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        self.assertEqual(mods - {"__future__", "hashlib", "subprocess",
                                 "pathlib", "typing"}, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
