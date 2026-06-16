# Testing Notes

This repository contains **two kinds** of `test_*.py` files. They are run
differently and have different safety properties. Read this before assembling
any "full regression" command.

## 1. Unittest-discoverable tests (the regression suite)

Most test files (M13 onward, plus M16, M17, M18) are proper
`unittest.TestCase` classes. They are discovered and run by
`python -m unittest <module>` and contribute to loader counts (e.g. the M18
`test_m18_ml` loader count).

These are safe to run automatically. They do not write to `signals.db` as a
side effect of normal collection, and the M18 suite is explicitly contracted
to never write `signals.db`.

## 2. Script-style verification tests (operator/manual — NOT in unittest)

The following files are **scripts**, not `unittest.TestCase` classes. They
define no test classes and no `test_`-prefixed top-level functions, so
`python -m unittest` collects **zero** tests from them:

- `test_m10.py`
- `test_m11.py`
- `test_m12.py`
- `test_m12_live_order.py`
- `test_m14_risk.py`

### Why this matters

1. **They are invisible to `python -m unittest`.** A regression command such
   as `python -m unittest test_m10` reports `Ran 0 tests` and exits OK. Do
   **not** assume these milestones are covered by a unittest regression sweep
   — they are not. Run them explicitly with `python test_m10.py` etc.

2. **Some write synthetic rows to `signals.db`.** For example `test_m10.py`
   injects a synthetic `final_signal` (cycle_id `99999`, a fake signal id)
   into the flywheel pipeline to prove the path
   `final_signal snapshot → risk check → paper broker → execution_intent`.
   These are **operator/manual verification scripts**, intended to be run
   deliberately on a machine where writing to `signals.db` is acceptable.
   They are **not** safe to include in an automated CI run, and they must
   **not** be mixed into the M18 workflow (which is contracted never to write
   `signals.db`).

3. **`test_m12_live_order.py` and `test_m14_risk.py` exercise the live/risk
   paths.** Treat them as deliberate operator actions, not background tests.

### Recommendation (not yet done)

Converting these scripts to unittest is **deferred** (it is not part of the
current docs/test-infra cleanup). If converted later, the synthetic
`signals.db` writes must be redirected to a temporary database so the tests
become side-effect-free and CI-safe. Until then, keep them out of automated
regression commands and run them manually when needed.

## Building a regression command

When you want a unittest regression sweep, target the unittest-style modules
only — for example the M16/M17/M18 suites and the M13–M15 suites. Do not add
the five script-style files above to a `python -m unittest` invocation; they
contribute nothing there and can mislead you into thinking they ran.

A duplicate-class / duplicate-method static guard
(`test_hygiene_suite.py::TestSuiteHygiene`) statically parses all `test_*.py`
files (without importing or executing them) and fails if any top-level test
class name is duplicated, or any test method name is duplicated within a
class. This guards against the silent unittest class-shadowing problem that
was fixed in the pre-M19 cleanup.
