"""test_quarantine_guard.py — keep the script-style operator tests quarantined.

Background (ISSUE-020)
----------------------
Five early-milestone test files are *script-style*: they run assertions in a
top-level / `__main__` body rather than via `unittest.TestCase`. Some of them
write synthetic rows to the real `signals.db` and/or exercise live/risk paths.
They are **operator/manual verification tests**, NOT CI-safe automatic tests.
See docs/TESTING.md.

This guard locks in that quarantine. If someone later adds a
`unittest.TestCase` (or a top-level `test_*` function) to any of these files,
they would suddenly become discoverable by `python -m unittest` and could run
automatically — potentially writing to `signals.db` or touching live/risk
code. This guard fails loudly in that case so the change is a conscious one
(and must be paired with making the file CI-safe, e.g. temp-DB).

Safety properties
-----------------
* AST / static parsing ONLY. This guard reads each quarantined file as text
  and parses it with `ast`. It NEVER imports or executes any of them, so
  importing this guard does not run their script bodies, never writes
  `signals.db`, and never touches live/broker code.
* Imports nothing from `bot/`.
* Pure standard library (`ast`, `pathlib`).
"""
import ast
import pathlib
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent

# The quarantined operator/manual scripts (ISSUE-020). Keep in sync with
# docs/TESTING.md.
QUARANTINED_SCRIPTS = (
    "test_m10.py",
    "test_m11.py",
    "test_m12.py",
    "test_m12_live_order.py",
    "test_m14_risk.py",
)


def _parse(name: str) -> ast.Module:
    return ast.parse((_REPO_ROOT / name).read_text(), filename=name)


def _defines_unittest_testcase(tree: ast.Module) -> bool:
    """True if the module defines a class that (by name) subclasses
    unittest.TestCase — checked statically, without importing."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                # matches `unittest.TestCase` and a bare `TestCase`
                if isinstance(base, ast.Attribute) and base.attr == "TestCase":
                    return True
                if isinstance(base, ast.Name) and base.id == "TestCase":
                    return True
    return False


def _has_top_level_test_callable(tree: ast.Module) -> bool:
    """True if the module has a top-level `test*`-named function or class,
    which `unittest`/`pytest`-style discovery could pick up."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                return True
        if isinstance(node, ast.ClassDef):
            if node.name.startswith("Test") or node.name.startswith("test"):
                return True
    return False


class QuarantineGuard(unittest.TestCase):
    """Static guards that the operator scripts stay non-discoverable."""

    def test_quarantined_files_exist(self):
        for name in QUARANTINED_SCRIPTS:
            self.assertTrue(
                (_REPO_ROOT / name).is_file(),
                f"expected quarantined operator script missing: {name}")

    def test_quarantined_files_define_no_unittest_testcase(self):
        offenders = [
            name for name in QUARANTINED_SCRIPTS
            if _defines_unittest_testcase(_parse(name))
        ]
        self.assertEqual(
            offenders, [],
            "Operator scripts must NOT define unittest.TestCase classes — "
            "that would make them auto-discoverable by `python -m unittest` "
            "and some write to signals.db / touch live paths. If you intend "
            "to convert one, first make it CI-safe (temp DB, no live calls) "
            f"and update docs/TESTING.md + this guard. Offenders: {offenders}")

    def test_quarantined_files_have_no_top_level_test_callables(self):
        offenders = [
            name for name in QUARANTINED_SCRIPTS
            if _has_top_level_test_callable(_parse(name))
        ]
        self.assertEqual(
            offenders, [],
            "Operator scripts must NOT expose top-level test*/Test* callables "
            f"(discoverable by test runners). Offenders: {offenders}")

    def test_quarantine_documented_in_testing_md(self):
        """docs/TESTING.md must name every quarantined script so the
        manual/operator status is discoverable by humans too."""
        doc = (_REPO_ROOT / "docs" / "TESTING.md").read_text()
        missing = [n for n in QUARANTINED_SCRIPTS if n not in doc]
        self.assertEqual(
            missing, [],
            f"docs/TESTING.md must document each quarantined script; "
            f"missing: {missing}")


if __name__ == "__main__":
    unittest.main()
