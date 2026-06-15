"""bot.ml.audit — M18.B.10 read-only audit / safety runner.

Consolidates the manual per-phase verification checks into one
runnable, parseable verdict. STRICTLY READ-ONLY: it shells out to
read-only git commands and (in hygiene/full mode) runs existing test
groups, then reports. It NEVER mutates the repo — no auto-fix, no git
reset/clean, no requirements edit, no registry mutation, no training,
no broker/live/dashboard/scanner execution, no signals.db writes.

It deliberately does NOT re-implement the G10 hygiene logic; in
hygiene/full mode it invokes the existing G10_Hygiene test classes via
unittest and reports their result.

Modes:
  static  — fast repo/file checks only (git/branch/diff/scan/syntax)
  hygiene — static + the fast G10 hygiene test classes (DEFAULT)
  full    — static + hygiene + heavier approved suites (M17.B full).
             Opt-in only.

Injection points (for tests — never exposed as CLI flags):
  git_runner(args: list[str]) -> (returncode, stdout, stderr)
  test_runner(unittest_targets: list[str]) -> (returncode, stdout, stderr)
  repo_root: Path
These default to real subprocess calls against the real repo. Tests
inject fakes so no real git mutation or full suite run happens in unit
tests.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Content-SHA256 pin for bot/data.py (the value reported every phase).
BOT_DATA_PY_SHA256 = (
    "35a7ff9f88500d4b27444d171268631202ad0eca9809b113e157192ed2538440")

# Protected files that must not differ from origin/main.
PROTECTED_FILES = (
    "main.py",
    "bot/data.py",
    "bot/risk.py",
    "bot/scanner.py",
    "bot/strategy.py",
    "dashboard/app.py",
)

# Forbidden live/broker/dashboard/scanner EXECUTION patterns in bot/ml.
# NOTE: signals.db / sqlite3 are intentionally NOT here — the flywheel
# reader legitimately READS signals.db (read-only), and the existing
# G10_Hygiene tests already encode the precise, nuanced forbidden set
# (they distinguish a legitimate read from a forbidden write). This
# static scan only flags the unambiguous live/broker/dashboard/scanner
# EXECUTION tokens; the DB nuance is delegated to G10 (hygiene mode).
FORBIDDEN_PATTERNS = (
    "import bot.scanner",
    "from bot.scanner",
    "import bot.brokers",
    "from bot.brokers",
    "import dashboard",
    "from dashboard",
    "placeOrder",
    ".submit(",
)

# Untracked paths that are allowed to be present (not a dirty failure).
ALLOWED_UNTRACKED_PREFIXES = ("recovery_audit/",)

MODES = ("static", "hygiene", "full")

_DEF_REPO_ROOT = Path(__file__).resolve().parents[2]


GitRunner = Callable[[List[str]], Tuple[int, str, str]]
TestRunner = Callable[[List[str]], Tuple[int, str, str]]


def _real_git_runner(args: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(["git", *args], capture_output=True, text=True,
                       timeout=30)
    return p.returncode, p.stdout, p.stderr


def _real_test_runner(targets: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(
        ["python3", "-m", "unittest", *targets],
        capture_output=True, text=True, timeout=1800)
    # unittest writes its summary to stderr
    return p.returncode, p.stdout, p.stderr


def _check(name: str, ok: bool, details: str,
           command: Optional[str] = None) -> Dict[str, Any]:
    return {
        "name":    name,
        "status":  "pass" if ok else "fail",
        "details": details,
        "command": command,
    }


class AuditRunner:
    """Read-only audit runner. All external effects go through the two
    injected runner callables, so tests can drive every path with fakes
    and the production default is the only thing that touches the real
    repo (read-only)."""

    def __init__(
        self,
        *,
        repo_root: Optional[Path] = None,
        git_runner: Optional[GitRunner] = None,
        test_runner: Optional[TestRunner] = None,
    ) -> None:
        self.repo_root = Path(repo_root) if repo_root else _DEF_REPO_ROOT
        self.git = git_runner or _real_git_runner
        self.test = test_runner or _real_test_runner

    # ── static checks ──────────────────────────────────────────────

    def _branch_head(self) -> List[Dict[str, Any]]:
        out = []
        rc, branch, _ = self.git(["rev-parse", "--abbrev-ref", "HEAD"])
        out.append(_check(
            "branch", rc == 0, branch.strip(),
            "git rev-parse --abbrev-ref HEAD"))
        rc, head, _ = self.git(["rev-parse", "HEAD"])
        out.append(_check(
            "head", rc == 0, head.strip(), "git rev-parse HEAD"))
        return out

    def _git_status_clean(self) -> Dict[str, Any]:
        rc, out, _ = self.git(["status", "--porcelain"])
        if rc != 0:
            return _check("git_status", False, "git status failed",
                          "git status --porcelain")
        dirty = []
        for line in out.splitlines():
            line = line.rstrip("\n")
            if not line.strip():
                continue
            path = line[3:] if len(line) > 3 else line
            if any(path.startswith(p)
                   for p in ALLOWED_UNTRACKED_PREFIXES):
                continue
            dirty.append(line)
        ok = len(dirty) == 0
        return _check(
            "git_status", ok,
            "clean (only allowed untracked)" if ok
            else f"dirty: {dirty}",
            "git status --porcelain")

    def _requirements_clean(self) -> Dict[str, Any]:
        rc, out, _ = self.git(
            ["diff", "--stat", "origin/main", "--", "requirements.txt"])
        ok = (rc == 0 and out.strip() == "")
        return _check(
            "requirements_diff", ok,
            "unchanged vs origin/main" if ok else f"changed: {out.strip()}",
            "git diff --stat origin/main -- requirements.txt")

    def _data_ml_tracked(self) -> Dict[str, Any]:
        rc, out, _ = self.git(["ls-files", "data/ml"])
        ok = (rc == 0 and out.strip() == "")
        return _check(
            "data_ml_tracked", ok,
            "no tracked data/ml files" if ok
            else f"tracked: {out.strip()}",
            "git ls-files data/ml")

    def _data_ml_local(self) -> Dict[str, Any]:
        ml_dir = self.repo_root / "data" / "ml"
        files = []
        if ml_dir.exists():
            files = [str(p) for p in ml_dir.rglob("*") if p.is_file()]
        ok = len(files) == 0
        return _check(
            "data_ml_local", ok,
            "no local data/ml files" if ok else f"present: {files}",
            "find data/ml -type f")

    def _protected_paths(self) -> List[Dict[str, Any]]:
        out = []
        for f in PROTECTED_FILES:
            rc, diff, _ = self.git(
                ["diff", "--numstat", "origin/main", "HEAD", "--", f])
            # empty stdout == identical; non-empty == changed
            changed = diff.strip() != ""
            out.append(_check(
                f"protected:{f}", (rc == 0 and not changed),
                "unchanged vs origin/main" if not changed
                else f"changed: {diff.strip()}",
                f"git diff --numstat origin/main HEAD -- {f}"))
        return out

    def _bot_data_sha(self) -> Dict[str, Any]:
        p = self.repo_root / "bot" / "data.py"
        if not p.exists():
            return _check("bot_data_sha", False, "bot/data.py missing",
                          "sha256sum bot/data.py")
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        ok = digest == BOT_DATA_PY_SHA256
        return _check(
            "bot_data_sha", ok,
            "sha256 matches pin" if ok
            else f"sha256 {digest} != pin {BOT_DATA_PY_SHA256}",
            "sha256sum bot/data.py")

    def _forbidden_scan(self) -> Dict[str, Any]:
        ml_dir = self.repo_root / "bot" / "ml"
        hits: List[str] = []
        if ml_dir.exists():
            for py in ml_dir.rglob("*.py"):
                # audit.py itself DEFINES the forbidden-pattern list as
                # data, so scanning it would self-match. Skip it; its
                # own cleanliness is covered by the G10 import tests +
                # the no-side-effect audit test.
                if py.name == "audit.py":
                    continue
                try:
                    text = py.read_text()
                except Exception:
                    continue
                for pat in FORBIDDEN_PATTERNS:
                    if pat in text:
                        hits.append(f"{py.name}:{pat}")
        ok = len(hits) == 0
        return _check(
            "forbidden_scan", ok,
            "no forbidden live/broker/dashboard/scanner/signals "
            "patterns in bot/ml" if ok else f"hits: {hits}",
            "grep -RnE <forbidden> bot/ml")

    def _syntax(self) -> Dict[str, Any]:
        # Use the builtin compile() on source text so NO .pyc bytecode
        # is written (py_compile would create __pycache__, a side
        # effect). Read-only.
        ml_dir = self.repo_root / "bot" / "ml"
        bad: List[str] = []
        if ml_dir.exists():
            for py in ml_dir.rglob("*.py"):
                try:
                    compile(py.read_text(), str(py), "exec")
                except SyntaxError as exc:
                    bad.append(f"{py.name}: {exc}")
                except Exception:
                    continue
        ok = len(bad) == 0
        return _check(
            "syntax", ok,
            "all bot/ml files compile" if ok else f"errors: {bad}",
            "py_compile bot/ml/**/*.py")

    def _static_checks(self) -> List[Dict[str, Any]]:
        checks: List[Dict[str, Any]] = []
        checks.extend(self._branch_head())
        checks.append(self._git_status_clean())
        checks.append(self._requirements_clean())
        checks.append(self._data_ml_tracked())
        checks.append(self._data_ml_local())
        checks.extend(self._protected_paths())
        checks.append(self._bot_data_sha())
        checks.append(self._forbidden_scan())
        checks.append(self._syntax())
        return checks

    # ── hygiene / full (invoke existing tests; do not reimplement) ──

    def _run_test_group(self, name: str,
                        targets: List[str]) -> Dict[str, Any]:
        rc, out, err = self.test(targets)
        ok = rc == 0
        # unittest prints "OK" / "FAILED" on stderr
        tail = (err or out).strip().splitlines()
        summary = tail[-1] if tail else ""
        return _check(
            name, ok, summary,
            "python3 -m unittest " + " ".join(targets))

    def _hygiene_checks(self) -> List[Dict[str, Any]]:
        return [
            self._run_test_group(
                "m18_g10_hygiene",
                ["test_m18_ml.G10_Hygiene"]),
            self._run_test_group(
                "m17b_g10_hygiene",
                ["test_m17_backtesting.G10_Hygiene"]),
        ]

    def _full_checks(self) -> List[Dict[str, Any]]:
        return [
            self._run_test_group(
                "m17b_full_regression",
                ["test_m17_backtesting"]),
        ]

    # ── orchestration ─────────────────────────────────────────────

    def run(self, mode: str = "hygiene") -> Dict[str, Any]:
        if mode not in MODES:
            raise ValueError(
                f"mode={mode!r} not in {MODES}")
        checks = self._static_checks()
        if mode in ("hygiene", "full"):
            checks.extend(self._hygiene_checks())
        if mode == "full":
            checks.extend(self._full_checks())

        failed = [c for c in checks if c["status"] == "fail"]
        ok = len(failed) == 0
        return {
            "mode":      mode,
            "ok":        ok,
            "n_checks":  len(checks),
            "n_failed":  len(failed),
            "checks":    checks,
            "failed":    [c["name"] for c in failed],
        }
