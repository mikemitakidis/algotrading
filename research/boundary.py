"""research.boundary — STRUCTURAL import lint for the offline research package.

SECURITY SCOPE (read this first): this module is a static lint, NOT a sandbox.
It parses source with ``ast`` and reports imports of forbidden modules. It
cannot stop what running Python can do without importing anything new:
``open()``, ``pathlib`` reads/writes, ``os.system`` / ``os.environ`` (``os``
itself is not forbidden), ``eval`` / ``exec`` / ``compile``, attribute access
built at runtime with ``getattr``, introspection escapes, and so on.

Therefore:
* Passing ``scan_source`` is NEVER evidence that code is safe to execute.
  ``may_execute_untrusted`` returns False for every input by contract, and
  ``vet_for_execution`` always raises ``NotASandboxError``.
* Untrusted or AI-generated code (lane E, canonical B22) may only ever run
  inside an OS/container-level sandbox with no secrets, no network, read-only
  inputs, scoped write paths and process limits — a separate, operator-approved
  design. A growing AST denylist is not, and will not become, that boundary.
* ``capability_findings`` lists risky capabilities (file, process,
  environment, dynamic evaluation, introspection) to help a HUMAN reviewer;
  an empty list is likewise not a safety guarantee.

What the lint is for: keeping the research package's own imports inside the
read-only libraries (M16 store readers, M17 simulator, M18 ML, M19 scoring)
and away from broker, order, notifier, kill-switch, runtime, dashboard,
network, secret-loading and process-spawning modules.

Policy (deny by default for ``bot.*``):
* ``bot.*`` imports must match ``ALLOWED_BOT_MODULES`` (exact module or a
  sub-module of an allowed package).
* ``FORBIDDEN_MODULES`` are rejected anywhere under ``research/``.
* ``dashboard``, ``main``, ``tools`` and ``server_scripts`` are rejected.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple

# Read-only libraries research code may reuse.
ALLOWED_BOT_MODULES: Tuple[str, ...] = (
    "bot.ml",                    # M18 features, labels, splits, models, evaluation
    "bot.backtesting",           # M17 indicators, strategies, single-symbol simulator
    "bot.signal_scoring",        # M19 deterministic scoring (pure functions)
    "bot.historical.store",      # M16 read path (parquet readers)
    "bot.historical.schema",
    "bot.historical.timeframes",
    "bot.historical.quality",
    # M21 WP11: the real exchange calendar (pure: stdlib + zoneinfo only). Real
    # bars must be judged and re-stamped against holidays, early closes and DST.
    "bot.scoring_truth.sessions",
)

# Never importable from research code, at any depth.
FORBIDDEN_MODULES: Tuple[str, ...] = (
    # network / remote access
    "socket", "ssl", "http", "urllib.request", "urllib3", "requests", "httpx",
    "aiohttp", "websocket", "websockets", "ftplib", "smtplib", "poplib",
    "imaplib", "telnetlib", "paramiko", "xmlrpc",
    # broker / market-data SDKs and messaging
    "ib_insync", "ibapi", "alpaca", "alpaca_trade_api", "yfinance", "telegram",
    # secrets and process control
    "dotenv", "subprocess", "multiprocessing.connection", "pty",
)

# Top-level production entry points research must not import.
FORBIDDEN_TOP_LEVEL: Tuple[str, ...] = ("dashboard", "main", "tools", "server_scripts")


@dataclass(frozen=True)
class ImportViolation:
    path: str
    line: int
    module: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return "%s:%d imports %s (%s)" % (self.path, self.line, self.module, self.reason)


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def classify(module: str) -> str:
    """Return '' if `module` is allowed for research code, else a reason."""
    if not module:
        return ""
    for bad in FORBIDDEN_MODULES:
        if _matches(module, bad):
            return "forbidden module %s" % bad
    top = module.split(".", 1)[0]
    if top in FORBIDDEN_TOP_LEVEL:
        return "production entry point %s" % top
    if top == "bot":
        if module == "bot":       # package root (empty __init__); submodules checked
            return ""
        if any(_matches(module, ok) for ok in ALLOWED_BOT_MODULES):
            return ""
        return "bot module outside ALLOWED_BOT_MODULES"
    return ""


def _imported_modules(tree: ast.AST) -> Iterable[Tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative import inside research/
                continue
            mod = node.module or ""
            yield node.lineno, mod
            # `from bot import brokers` imports bot.brokers
            for alias in node.names:
                yield node.lineno, "%s.%s" % (mod, alias.name) if mod else alias.name
        elif isinstance(node, ast.Call):
            # importlib.import_module("x") / __import__("x") with a literal name
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else fn.id if isinstance(fn, ast.Name) else "")
            if name in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    yield node.lineno, arg.value
                else:
                    yield node.lineno, "<dynamic import>"


def scan_source(source: str, path: str = "<string>") -> List[ImportViolation]:
    """AST-scan Python source for boundary violations (never executes it)."""
    tree = ast.parse(source, filename=path)
    out: List[ImportViolation] = []
    for line, mod in _imported_modules(tree):
        if mod == "<dynamic import>":
            out.append(ImportViolation(path, line, mod, "non-literal dynamic import"))
            continue
        why = classify(mod)
        if why:
            out.append(ImportViolation(path, line, mod, why))
    return out


def scan_file(path: Path) -> List[ImportViolation]:
    return scan_source(Path(path).read_text(), str(path))


def scan_tree(root: Path) -> List[ImportViolation]:
    """Scan every .py file below `root`."""
    out: List[ImportViolation] = []
    for p in sorted(Path(root).rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        out.extend(scan_file(p))
    return out


# ── capability findings (reviewer aid, NOT a security control) ─────────

SECURITY_SCOPE = "structural_import_lint_only_not_a_sandbox"

RISKY_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "open", "getattr", "setattr", "delattr", "globals",
    "locals", "vars", "input", "breakpoint", "memoryview",
})
RISKY_ATTRS = frozenset({
    # process / OS
    "system", "popen", "fork", "forkpty", "kill", "killpg", "execv", "execve", "execl", "execle",
    "execlp", "execvp", "execvpe", "spawnl", "spawnv", "spawnve", "posix_spawn", "startfile",
    # environment
    "environ", "getenv", "putenv", "unsetenv",
    # filesystem
    "remove", "unlink", "rmdir", "removedirs", "rmtree", "rename", "renames", "replace", "chmod",
    "chown", "symlink", "symlink_to", "link", "mkdir", "makedirs", "write_text", "write_bytes",
    "read_text", "read_bytes", "open", "touch", "copyfile", "copytree", "move",
    # introspection escapes
    "__subclasses__", "__globals__", "__builtins__", "__dict__", "__code__", "__class__", "__bases__",
    "__mro__", "f_globals", "f_locals", "gi_frame",
})


@dataclass(frozen=True)
class CapabilityFinding:
    path: str
    line: int
    capability: str


def capability_findings(source: str, path: str = "<string>") -> List[CapabilityFinding]:
    """Risky capabilities a human reviewer should look at. Incomplete by
    design: an empty result does NOT mean the code is safe."""
    tree = ast.parse(source, filename=path)
    out: List[CapabilityFinding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in RISKY_CALLS:
            out.append(CapabilityFinding(path, node.lineno, "call:%s" % node.func.id))
        elif isinstance(node, ast.Attribute) and node.attr in RISKY_ATTRS:
            out.append(CapabilityFinding(path, node.lineno, "attr:%s" % node.attr))
        elif isinstance(node, ast.Name) and node.id == "__builtins__":
            out.append(CapabilityFinding(path, node.lineno, "name:__builtins__"))
    return out


class NotASandboxError(PermissionError):
    """Raised whenever a caller asks this lint to authorise code execution."""


def may_execute_untrusted(source: str) -> bool:
    """Always False: no static check here can make untrusted code safe to run."""
    return False


def vet_for_execution(source: str, path: str = "<string>") -> None:
    """Refuse, always. Execution of untrusted code needs an OS/container sandbox."""
    raise NotASandboxError(
        "research.boundary is a structural import lint, not a sandbox (%s): untrusted or generated "
        "code must only run inside an approved OS/container sandbox with no secrets or network" % path)


__all__ = [
    "ALLOWED_BOT_MODULES", "FORBIDDEN_MODULES", "FORBIDDEN_TOP_LEVEL", "SECURITY_SCOPE",
    "ImportViolation", "classify", "scan_source", "scan_file", "scan_tree",
    "RISKY_CALLS", "RISKY_ATTRS", "CapabilityFinding", "capability_findings",
    "NotASandboxError", "may_execute_untrusted", "vet_for_execution",
]
