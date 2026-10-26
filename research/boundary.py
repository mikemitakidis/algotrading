"""research.boundary — the import boundary of the offline research package.

The research package may read market data and reuse the deterministic research
libraries that already exist (M16 store readers, M17 simulator, M18 ML, M19
scoring). It must never reach a broker, send a message, touch runtime state,
load secrets or open a network connection.

Policy (deny by default for ``bot.*``):

* ``bot.*`` imports must match ``ALLOWED_BOT_MODULES`` (exact module or a
  sub-module of an allowed package).
* ``FORBIDDEN_MODULES`` (network, broker SDKs, secrets, process spawning) are
  rejected anywhere under ``research/``.
* ``dashboard``, ``main``, ``tools`` and ``server_scripts`` are rejected.

``scan_file`` / ``scan_tree`` are pure AST helpers (no import, no execution).
They are reused by the foundation guard test and by the agent sandbox (lane E)
to vet generated code before it can be run.
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


__all__ = [
    "ALLOWED_BOT_MODULES", "FORBIDDEN_MODULES", "FORBIDDEN_TOP_LEVEL",
    "ImportViolation", "classify", "scan_source", "scan_file", "scan_tree",
]
