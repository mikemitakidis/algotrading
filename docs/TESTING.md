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

A second static guard (`test_quarantine_guard.py::QuarantineGuard`) keeps the
five script-style operator tests listed above **quarantined**. It parses each
of them with `ast` (never importing or executing them, so it never runs their
script bodies, never writes `signals.db`, and never touches live/broker code)
and fails if any of them gains a `unittest.TestCase` subclass or a top-level
`test*`/`Test*` callable — i.e. if a change would make them auto-discoverable
by `python -m unittest`. If you intend to convert one of these scripts into a
real unittest, you must first make it CI-safe (temp DB, no live calls), then
update both `docs/TESTING.md` and that guard in the same change.

## 3. Isolation from production state, secrets and brokers (M21 C5a)

Run from the production checkout, many modules touched real runtime files:
`test_m14_risk.py` rewrote `data/kill_switch.json` on import (and other modules
silently relied on that real file being inactive), a dozen dashboard/broker
tests opened `data/signals.db` (one inserted synthetic `auth_events`),
`test_m13_3_etoro_paper.py` appended to `data/paper_etoro_orders.jsonl`,
`test_m14_d_exposure.py` ran the real exposure CLI over every broker scope,
`test_m17_backtesting.py` created files under `data/backtests/`,
`test_m14_f_preflight.py` deleted/created audit files in `data/`, 24
modules loaded the real `.env` (via `bot.config`, `dashboard.app` or a
`tools/` CLI), and `test_m21_1extra_b2b_single_submit.py` launched the B2b CLI
with the real-paper-order confirmation flag.

**How to run tests:** always in a separate clone (never `/opt/algo-trader`),
through the isolating runner:

    python _test_isolation.py test_m21_c5a_test_isolation test_m14_e_engine ...
    python _test_isolation.py --all        # every module, one isolated process each

Not `unittest discover` (the quarantined script-style modules are not
discoverable tests).

What isolation does (`_test_isolation.py`, test-only; production code unchanged):

- **Redirects** known runtime state into a private temp dir for the test
  process and its Python children: sqlite opens of `<repo>/data/*`, the
  kill-switch file (starts in the production default state, inactive), the
  eToro paper audit JSONL, the risk-ingest audit logs, and every python-dotenv
  load of `<repo>/.env` (so importing `bot.config`/`dashboard.app` never loads
  real secrets). Patches are applied lazily on import.
- **Blocks, fail-closed** (audit hook, raises `ProductionAccessBlocked`): any
  access to `<repo>/.env*`; any write under `<repo>/data/` (reads of the
  tracked reference CSVs are fine); any connection to an IB Gateway/TWS port
  (4001, 4002, 7496, 7497) or to a non-loopback host, and non-loopback DNS
  lookups. A test that hits the guard fails loudly instead of touching
  production.
- The runner gives every module its own fresh temp state and refuses to run in
  `/opt/algo-trader` or to run the operator scripts. It is **fail-closed**:
  before launching any module it starts a verification child exactly like a
  module child and requires isolation and the guard to be active there and in
  the Python processes that child starts (each module's own environment is
  re-verified just before launch). If not — for example because another
  `sitecustomize` is loaded before the generated one, or the interpreter
  ignores `PYTHONPATH` — the whole run is refused with exit code 3 and no test
  executes. `--self-check` prints the same verification without running tests.
- Modules that are risky when run directly also call `install()` at the top.
  `test_m21_c5a_test_isolation.py` enforces this statically (any module that
  imports `dashboard.app`, `bot.config`, `bot.etoro.paper_broker` or a
  `.env`-loading `tools` CLI, spawns a `tools/` CLI, or writes the kill switch
  must install before any project import), forbids the real-order flag in
  tests, and requires pyotp/bcrypt/qrcode (a missing dependency fails instead
  of silently skipping the 2FA tests, unless `ALGO_ALLOW_MISSING_AUTH_DEPS=1`).
- The operator scripts `test_m10.py`, `test_m11.py`, `test_m12.py --live` and
  `test_m12_live_order.py` are isolated whenever imported, never load `.env`
  on import, and refuse to run unless `ALGO_OPERATOR_SCRIPT_CONFIRM=<script
  file name>` is set for that single, explicitly approved run.

Not covered by the child-process propagation: children started with
`python -I`/`-S`, through a shell string, or non-Python programs.
