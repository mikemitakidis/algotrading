"""test_hygiene_suite.py — repo-wide static test-suite hygiene guard.

Generalises the M18 duplicate-class guard
(`test_m18_ml.G10_Hygiene.test_no_duplicate_test_class_definitions`) to the
WHOLE test suite.

Why this exists
---------------
Python keeps only the LAST definition when a class is declared twice in a
module; earlier definitions are silently discarded, dropping or
misattributing their test methods with no error. The pre-M19 cleanup found
seven such duplicated `G7_*` classes in `test_m18_ml.py`. This guard prevents
recurrence across every `test_*.py` file.

Safety properties (important)
-----------------------------
* AST / static parsing ONLY. This guard reads each `test_*.py` file as text
  and parses it with `ast`. It NEVER imports or executes any test module, so
  the script-style operator tests (`test_m10.py`, `test_m11.py`,
  `test_m12.py`, `test_m12_live_order.py`, `test_m14_risk.py`) are NOT run
  and never touch `signals.db`. See docs/TESTING.md.
* Imports nothing from `bot/` — no live/broker/risk modules are loaded.
* Pure standard library (`ast`, `pathlib`).
"""
import ast
import pathlib
import unittest
from collections import Counter

_REPO_ROOT = pathlib.Path(__file__).resolve().parent


def _test_files():
    """All test_*.py files in the repo root (this file included).

    Static-only: returns paths, never imports them.
    """
    return sorted(_REPO_ROOT.glob("test_*.py"))


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _top_level_classes(tree: ast.Module):
    return [n for n in tree.body if isinstance(n, ast.ClassDef)]


def _methods(cls: ast.ClassDef):
    return [
        b.name for b in cls.body
        if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


class TestSuiteHygiene(unittest.TestCase):
    """Static guards over the entire test_*.py surface."""

    def test_no_duplicate_top_level_test_class_names(self):
        """No test_*.py file may define the same top-level class name twice.

        A duplicate name means the later definition silently shadows the
        earlier one (and any unique tests in the earlier copy are dropped or
        misattributed). Fail loudly with the offending file + names.
        """
        offenders = {}
        for path in _test_files():
            names = [c.name for c in _top_level_classes(_parse(path))]
            dups = {n: c for n, c in Counter(names).items() if c > 1}
            if dups:
                offenders[path.name] = dups
        self.assertEqual(
            offenders, {},
            "Duplicate top-level test class definitions found (later defs "
            "silently shadow earlier ones, dropping/misattributing tests): "
            f"{offenders}")

    def test_no_duplicate_method_names_within_a_class(self):
        """Within any class in any test_*.py, no method name may repeat.

        A duplicate method name silently shadows the earlier method, so a
        test can vanish without any error.
        """
        offenders = {}
        for path in _test_files():
            for cls in _top_level_classes(_parse(path)):
                methods = _methods(cls)
                dups = {m: c for m, c in Counter(methods).items() if c > 1}
                if dups:
                    offenders[f"{path.name}::{cls.name}"] = dups
        self.assertEqual(
            offenders, {},
            "Duplicate method names within a test class (later defs silently "
            f"shadow earlier ones): {offenders}")

    def test_guard_actually_inspected_files(self):
        """Sanity: the guard must have found test files to inspect, otherwise
        a globbing/path regression could make the guards vacuously pass."""
        self.assertGreater(
            len(_test_files()), 1,
            "expected to find multiple test_*.py files to inspect")


if __name__ == "__main__":
    unittest.main()
