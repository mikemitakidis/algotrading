"""_test_isolation.py — keep tests off production state, secrets and brokers (M21 C5a).

TEST-ONLY helper. Importing this module changes nothing. ``install()`` acts on
the current test process (and the Python child processes it starts) only.

1. REDIRECT known runtime state into a private per-process temp dir:
  * ``sqlite3.connect`` on any file directly under ``<repo>/data/`` (plain path,
    relative path, ``pathlib.Path`` or ``file:`` URI). Example:
    ``data/signals.db`` -> ``<tmp>/signals.db``.
  * ``bot.kill_switch.KILL_SWITCH_PATH`` -> ``<tmp>/kill_switch.json``,
    initialised to the production default state (inactive), as ``main.py``
    does via ``ensure_default_state()``. No test depends on the real file;
    a test that needs an ACTIVE switch activates the temp one.
  * ``bot.etoro.paper_broker._DEFAULT_AUDIT_FILE`` -> ``<tmp>/paper_etoro_orders.jsonl``.
  * ``bot.risk_authority.ingest_audit.get_ingest_audit_logger`` -> log paths
    under ``<repo>/data/`` are rewritten into the temp dir.
  * python-dotenv (``load_dotenv``, ``dotenv_values``, ``get_key``, ``set_key``,
    ``unset_key``): a path to ``<repo>/.env*`` (or no path at all) is replaced by
    ``<tmp>/.env``, which does not exist unless a test writes it. So importing
    ``bot.config``, ``dashboard.app`` or a ``tools/`` CLI never loads the real
    secrets into a test process.
  These module patches are applied lazily, when (and only if) the code under
  test imports the module, so installing isolation loads no project module.

2. BLOCK (fail closed) anything else, via a ``sys.addaudithook`` guard that
   raises ``ProductionAccessBlocked`` (a ``PermissionError``) on:
  * any access to ``<repo>/.env*`` (read, write, copy, rename, delete);
  * any write under ``<repo>/data/`` (open for writing, create, delete, rename,
    truncate, chmod/utime, rmtree, a non-redirected sqlite connection, or
    mkdir of a directory that does not exist yet). Reads are allowed;
  * a network connection to an IB Gateway / TWS port (4001, 4002, 7496, 7497)
    on any host, or to any non-loopback address, DNS lookups of non-loopback
    hosts, and listeners bound to a non-loopback interface (e.g. 0.0.0.0).
    Tests never reach a broker or the internet, nor expose a port.

3. CHILD PROCESSES: Python children started by a test inherit 1+2 through a
   generated ``sitecustomize`` placed first on PYTHONPATH (fails closed with
   exit 97 if isolation cannot be applied); children started with a hand-built
   ``env=`` get it re-injected by a ``subprocess.Popen`` wrapper. Not covered:
   ``python -I``/``-S`` children, shell-string commands, non-Python programs.

4. RUNNER: ``python _test_isolation.py test_a test_b ...`` (or ``--all``) runs
   each module in its own isolated child process, so every module is covered
   whether or not it calls ``install()`` itself. Fail closed: before launching
   ANY module it starts a verification child exactly like a module child and
   requires isolation + guard to be active there and in the Python processes
   that child starts; each module's own environment is re-verified just before
   launch. If not, the whole run is refused (exit 3) and no test executes —
   e.g. when another ``sitecustomize`` is loaded before the generated one.

Why: test modules opened ``data/signals.db``, appended to
``data/paper_etoro_orders.jsonl``, rewrote ``data/kill_switch.json``, read the
real kill-switch state, created files under ``data/`` and loaded ``.env``
(findings N-1, C5a). On the VPS those are production files and secrets.

Production code is never modified. This helper never writes, deletes or reads
production files itself, and opens no network connection. The temp dir is
removed at interpreter exit.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote

REPO_ROOT = Path(__file__).resolve().parent
PROD_DATA_DIR = (REPO_ROOT / "data").resolve()
_REPO_S = str(REPO_ROOT)
_DATA_S = str(PROD_DATA_DIR)

_state: dict = {}
_real_connect = sqlite3.connect
_CHILD_ENV = "ALGO_TEST_ISOLATION_DIR"


def temp_dir() -> Path:
    """The active per-process temp directory (install() must have run)."""
    return _state["tmp"]


def is_installed() -> bool:
    return bool(_state)


# ── path classification ─────────────────────────────────────────────────────

def _abs(path, dir_fd=None) -> str | None:
    """Absolute, symlink-resolved form of ``path`` (relative to ``dir_fd`` when
    given, else to the cwd), or None for fds / non-paths / special names."""
    if path is None or isinstance(path, int):
        return None
    try:
        p = os.fsdecode(path)
    except TypeError:
        return None
    if p in ("", ":memory:"):
        return None
    try:
        if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(p):
            p = os.path.join(os.readlink("/proc/self/fd/%d" % dir_fd), p)
        return os.path.realpath(p)
    except (OSError, ValueError):
        return None


def is_prod_env_path(path, dir_fd=None) -> bool:
    """True for ``<repo>/.env`` and its siblings (``.env.bak*`` etc.)."""
    a = _abs(path, dir_fd)
    return bool(a) and os.path.dirname(a) == _REPO_S \
        and os.path.basename(a).startswith(".env")


def is_prod_data_path(path, dir_fd=None) -> bool:
    """True for ``<repo>/data`` itself and anything below it."""
    a = _abs(path, dir_fd)
    return bool(a) and (a == _DATA_S or a.startswith(_DATA_S + os.sep))


# ── sqlite redirect ─────────────────────────────────────────────────────────

def redirect_path(path) -> str | None:
    """Return the temp-dir replacement for a file directly under <repo>/data/,
    or None if ``path`` is not a production data file."""
    if not _state:
        return None
    a = _abs(path)
    if a and os.path.dirname(a) == _DATA_S:
        return str(_state["tmp"] / os.path.basename(a))
    return None


def _redirect_uri(uri: str) -> str:
    body = uri[len("file:"):]
    path, sep, query = body.partition("?")
    if path.startswith("//"):                    # file://host/path or file:///path
        rest = path[2:]
        slash = rest.find("/")
        path = rest[slash:] if slash >= 0 else rest
    new = redirect_path(unquote(path))
    if new is None:
        return uri
    return "file:" + quote(new) + (sep + query if sep else "")


def _isolated_connect(database, *args, **kwargs):
    if isinstance(database, (str, bytes, os.PathLike)):
        db = os.fsdecode(database)
        if db.startswith("file:") and kwargs.get("uri"):
            database = _redirect_uri(db)
        elif db != ":memory:" and not db.startswith("file:"):
            new = redirect_path(db)
            if new is not None:
                database = new
    return _real_connect(database, *args, **kwargs)


# ── lazy patches for project / library modules ──────────────────────────────

def _patch_kill_switch(mod) -> None:
    mod.KILL_SWITCH_PATH = _state["tmp"] / "kill_switch.json"
    if not mod.KILL_SWITCH_PATH.exists():
        mod.ensure_default_state()          # production default: inactive


def _patch_etoro_paper(mod) -> None:
    mod._DEFAULT_AUDIT_FILE = _state["tmp"] / "paper_etoro_orders.jsonl"


def _patch_ingest_audit(mod) -> None:
    if getattr(mod.get_ingest_audit_logger, "_algo_isolated", False):
        return
    real_factory = mod.get_ingest_audit_logger

    def _isolated_audit_logger(path=None, *args, **kwargs):
        if path is None:
            path = mod._DEFAULT_PATH
        new = redirect_path(path)
        return real_factory(Path(new) if new is not None else path, *args, **kwargs)

    _isolated_audit_logger._algo_isolated = True
    mod.get_ingest_audit_logger = _isolated_audit_logger


def isolated_env_file() -> Path:
    """The stand-in for <repo>/.env inside this process's temp dir."""
    return _state["tmp"] / ".env"


def _redirect_env(path):
    if path is None or is_prod_env_path(path):
        return str(isolated_env_file())
    return path


def _patch_dotenv(pkg) -> None:
    import dotenv.main as dm
    for ns in (pkg, dm):
        for name in ("load_dotenv", "dotenv_values", "get_key", "set_key", "unset_key"):
            real = getattr(ns, name, None)
            if real is None or getattr(real, "_algo_isolated", False):
                continue

            def wrapper(dotenv_path=None, *args, _real=real, **kwargs):
                if dotenv_path is None and (args or kwargs.get("stream") is not None):
                    return _real(dotenv_path, *args, **kwargs)   # explicit stream, no file
                # an explicit <repo>/.env* path, or None (which would search
                # upwards and find <repo>/.env) -> the isolated stand-in
                return _real(_redirect_env(dotenv_path), *args, **kwargs)

            wrapper._algo_isolated = True
            wrapper.__wrapped__ = real
            setattr(ns, name, wrapper)


_PATCHERS = {
    "bot.kill_switch": _patch_kill_switch,
    "bot.etoro.paper_broker": _patch_etoro_paper,
    "bot.risk_authority.ingest_audit": _patch_ingest_audit,
    "dotenv": _patch_dotenv,
}


class _PostImportPatcher:
    """sys.meta_path finder: lets the normal import machinery load the target
    modules, then applies the matching patch (also on importlib.reload)."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _PATCHERS:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                break
        else:
            return None
        loader = spec.loader
        if loader is None or getattr(loader, "_algo_isolated", False):
            return spec
        real_exec = loader.exec_module

        def exec_module(module, _real=real_exec, _name=fullname):
            _real(module)
            _PATCHERS[_name](module)

        loader.exec_module = exec_module
        loader._algo_isolated = True
        return spec


# ── fail-closed guard (audit hook) ──────────────────────────────────────────

class ProductionAccessBlocked(PermissionError):
    """Raised when a test process tries to touch production secrets, write
    production data, or reach a broker / the network."""


BROKER_PORTS = frozenset({4001, 4002, 7496, 7497})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_FS_EVENTS = {
    # event: (indexes of path args, index of their dir_fd or None) per CPython
    "open": ((0,), None),
    "os.remove": ((0,), 1), "os.rmdir": ((0,), 1), "shutil.rmtree": ((0,), 1),
    "os.mkdir": ((0,), 2), "os.chmod": ((0,), 2), "os.utime": ((0,), 3),
    "os.truncate": ((0,), None),
    "os.rename": ((0, 1), 2), "os.replace": ((0, 1), 2),
    "os.link": ((0, 1), 2), "os.symlink": ((1,), 2),
    "shutil.copyfile": ((0, 1), None), "shutil.move": ((0, 1), None),
    "shutil.copytree": ((0, 1), None),
    "sqlite3.connect": ((0,), None),
}
_guard_busy = [False]


def _block(what: str, detail: str) -> None:
    raise ProductionAccessBlocked(
        "[_test_isolation] BLOCKED %s: %s (tests must not touch <repo>/.env*, "
        "write under <repo>/data/, or reach a broker/the network; "
        "see docs/TESTING.md section 3)" % (what, detail))


def _is_loopback(host) -> bool:
    h = str(host).strip("[]").lower()
    return h in ("localhost", "::1", "") or h.startswith("127.")


_WILDCARD = ("0.0.0.0", "::")


def _check_fs(event, args) -> None:
    idx, fd_idx = _FS_EVENTS[event]
    for i in idx:
        if i >= len(args) or not isinstance(args[i], (str, bytes, os.PathLike)):
            continue
        dir_fd = args[fd_idx] if fd_idx is not None and fd_idx < len(args) else None
        raw = os.fsdecode(args[i])
        if os.path.isabs(raw):
            if "data" not in raw and ".env" not in raw:
                continue                      # fast path (e.g. module files on import)
        elif event == "open":
            # os.open's audit event carries no dir_fd: shutil's fd-based tree
            # walk opens names relative to a directory fd -> cannot resolve.
            try:
                caller = sys._getframe(2)     # 0 _check_fs, 1 _guard, 2 trigger
            except ValueError:
                caller = None
            if caller is not None and caller.f_code.co_name.startswith("_rmtree"):
                continue
        a = _abs(args[i], dir_fd)
        if not a:
            continue
        rel = os.path.relpath(a, _REPO_S)
        if os.path.dirname(a) == _REPO_S and os.path.basename(a).startswith(".env"):
            _block("access to production secrets file", "%s %s" % (event, rel))
        if not (a == _DATA_S or a.startswith(_DATA_S + os.sep)):
            continue
        if event == "open":
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                     (isinstance(flags, int) and bool(flags & _WRITE_FLAGS))
            if not writes:
                continue                      # reads of data/ are allowed
        elif event == "os.mkdir" and os.path.isdir(a):
            continue                          # mkdir(exist_ok=True) on an existing dir
        elif event in ("shutil.copyfile", "shutil.copytree") and i == 0:
            continue                          # copying FROM data/ is a read
        _block("write to production data", "%s %s" % (event, rel))


def _check_net(event, args) -> None:
    if event == "socket.bind":
        addr = args[1] if len(args) > 1 else None
        if isinstance(addr, tuple) and len(addr) >= 2 and not _is_loopback(addr[0]):
            _block("listener on a non-loopback interface", "%s:%s" % (addr[0], addr[1]))
        return
    if event == "socket.connect":
        addr = args[1] if len(args) > 1 else None
        if isinstance(addr, tuple) and len(addr) >= 2:
            host, port = addr[0], addr[1]
            if port in BROKER_PORTS:
                _block("broker connection", "%s:%s" % (host, port))
            if not _is_loopback(host):
                _block("network connection", "%s:%s" % (host, port))
    elif event == "socket.getaddrinfo":
        host = args[0] if args else None
        if isinstance(host, bytes):
            host = host.decode(errors="replace")
        port = args[1] if len(args) > 1 else None
        if port in BROKER_PORTS or (isinstance(port, str) and port.isdigit()
                                    and int(port) in BROKER_PORTS):
            _block("broker address lookup", "%s:%s" % (host, port))
        if host is not None and not _is_loopback(host) and host not in _WILDCARD:
            _block("DNS lookup", str(host))


def _guard(event, args) -> None:
    if _guard_busy[0]:
        return
    if event in _FS_EVENTS:
        check = _check_fs
    elif event in ("socket.connect", "socket.getaddrinfo", "socket.bind"):
        check = _check_net
    else:
        return
    _guard_busy[0] = True
    try:
        check(event, args)
    finally:
        _guard_busy[0] = False


# ── child processes ─────────────────────────────────────────────────────────

_SITECUSTOMIZE = '''\
# Generated by _test_isolation.py (M21 C5a). Applies the same isolation and
# guard to Python child processes started by a test. Fails closed.
import os, sys
if os.environ.get("ALGO_TEST_ISOLATION_DIR"):
    try:
        import _test_isolation
        _test_isolation.install(os.environ["ALGO_TEST_ISOLATION_DIR"])
    except Exception as exc:
        sys.stderr.write("[_test_isolation] child isolation failed: %r\\n" % (exc,))
        raise SystemExit(97)
'''


def _apply_child_env(env) -> None:
    """Make Python children started with ``env`` re-apply this isolation."""
    parts = [str(_state["shim"]), str(REPO_ROOT)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
    env[_CHILD_ENV] = str(_state["tmp"])


def _env_is_isolated(env) -> bool:
    """True if ``env`` already makes a Python child install isolation: it names
    an isolation dir AND puts that dir's generated sitecustomize on PYTHONPATH
    (e.g. the runner's per-module environments)."""
    d = env.get(_CHILD_ENV)
    if not d:
        return False
    shim = os.path.join(d, "_child_site")
    return shim in (env.get("PYTHONPATH") or "").split(os.pathsep) \
        and os.path.isfile(os.path.join(shim, "sitecustomize.py"))


def _is_python_command(args) -> bool:
    if isinstance(args, (list, tuple)) and args:
        exe = str(args[0])
        if os.path.basename(exe).startswith("python"):
            return True
        try:
            return os.path.realpath(exe) == os.path.realpath(sys.executable)
        except OSError:
            return False
    return False


def _wrap_popen() -> None:
    """Tests sometimes start Python children with a hand-built ``env=``
    (e.g. PYTHONPATH reset to the repo). Re-inject the isolation into such
    environments so those children are isolated too."""
    import subprocess
    if getattr(subprocess.Popen.__init__, "_algo_isolated", False):
        return
    real_init = subprocess.Popen.__init__

    def _init(self, args, *a, **kw):
        env = kw.get("env")
        if env is not None and _is_python_command(args) and not _env_is_isolated(env):
            env = dict(env)
            _apply_child_env(env)
            kw["env"] = env
        real_init(self, args, *a, **kw)

    _init._algo_isolated = True
    subprocess.Popen.__init__ = _init


# ── entry point ─────────────────────────────────────────────────────────────

def install(shared_dir: str | None = None) -> Path:
    """Idempotently activate the isolation for this process. Returns the temp dir."""
    if _state:
        return _state["tmp"]
    if shared_dir:
        tmp = Path(shared_dir)
    else:
        tmp = Path(tempfile.mkdtemp(prefix="algo_test_isolation_"))
        atexit.register(shutil.rmtree, str(tmp), True)
    resolved = tmp.resolve()
    if resolved == PROD_DATA_DIR or PROD_DATA_DIR in resolved.parents:
        raise RuntimeError("isolation dir must not be inside the production data dir")
    tmp.mkdir(parents=True, exist_ok=True)
    _state["tmp"] = tmp

    shim = tmp / "_child_site"
    shim.mkdir(exist_ok=True)
    (shim / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    _state["shim"] = shim
    _apply_child_env(os.environ)
    _wrap_popen()

    sqlite3.connect = _isolated_connect

    sys.meta_path.insert(0, _PostImportPatcher())
    for name, patch in _PATCHERS.items():          # already imported -> patch now
        if name in sys.modules:
            patch(sys.modules[name])

    sys.addaudithook(_guard)                       # last: nothing above is blocked
    return tmp


# ── operator scripts ────────────────────────────────────────────────────────

OPERATOR_CONFIRM_ENV = "ALGO_OPERATOR_SCRIPT_CONFIRM"


def operator_run_confirmed(script_file: str) -> bool:
    """True only when the operator set ALGO_OPERATOR_SCRIPT_CONFIRM=<script name>."""
    return os.environ.get(OPERATOR_CONFIRM_ENV) == Path(script_file).name


def require_operator_confirmation(script_file: str) -> None:
    """Refuse to run an operator/manual script unless the operator set
    ALGO_OPERATOR_SCRIPT_CONFIRM=<script file name> for this one run.

    Used by the quarantined scripts that can reach a broker or write the
    production DB (test_m10.py, test_m11.py, test_m12.py --live,
    test_m12_live_order.py), so a loop such as
    ``for f in test_*.py; do python "$f"; done`` can never trigger them."""
    name = Path(script_file).name
    if not operator_run_confirmed(script_file):
        sys.stderr.write(
            "REFUSED: %s is an operator script that can reach a broker or the "
            "production DB. Run it only with explicit approval by setting "
            "%s=%s for that single run.\n" % (name, OPERATOR_CONFIRM_ENV, name))
        raise SystemExit(2)


# ── runner ──────────────────────────────────────────────────────────────────

# Operator scripts: never run by the runner (they define no tests and refuse
# without explicit confirmation anyway).
OPERATOR_SCRIPTS = ("test_m10.py", "test_m11.py", "test_m12_live_order.py")


def _module_child_env(d: Path) -> dict:
    """Environment for one isolated module run with fresh temp state in ``d``:
    the child's generated sitecustomize installs isolation + guard on startup."""
    (d / "_child_site").mkdir(parents=True, exist_ok=True)
    (d / "_child_site" / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    env = {k: v for k, v in os.environ.items() if k != _CHILD_ENV}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(d / "_child_site"), _REPO_S] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env[_CHILD_ENV] = str(d)
    return env


# Runs inside a freshly started child with the exact environment a test module
# gets. It must NOT call install(): it reports what the child's startup did.
# Every probe is non-destructive: with the guard missing, none of them reads
# a secret, writes a file or opens a connection.
_VERIFY_CHILD = r"""
import json, os, socket, subprocess, sys
res = {"installed": False}
try:
    import _test_isolation as t
    res["installed"] = t.is_installed()
except Exception as exc:
    res["import_error"] = repr(exc)
if res["installed"]:
    res["tmp"] = str(t.temp_dir())
    def blocked(fn):
        try:
            fn()
        except t.ProductionAccessBlocked:
            return True
        except Exception:
            return False
        return False
    res["env_blocked"] = blocked(lambda: open(os.path.join(t._REPO_S, ".env.isolation_probe_absent")))
    res["data_blocked"] = blocked(lambda: open(os.path.join(t._DATA_S, "isolation_probe_absent_dir", "x"), "w"))
    res["broker_blocked"] = blocked(lambda: socket.getaddrinfo("127.0.0.1", 4002))
    res["net_blocked"] = blocked(lambda: socket.getaddrinfo("192.0.2.1", 443))
    if CHECK_CHILDREN:
        code = "import _test_isolation as t; print(t.is_installed() and str(t.temp_dir()))"
        for name, env in (("inherited", None),
                          ("hand_built", {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": t._REPO_S}),
                          ("copied_reset", dict(os.environ, PYTHONPATH=t._REPO_S))):
            r = subprocess.run([sys.executable, "-c", code], env=env, cwd=t._REPO_S,
                               capture_output=True, text=True, timeout=120)
            res["child_" + name] = r.stdout.strip() if r.returncode == 0 else "rc=%d" % r.returncode
print("ISOLATION_VERIFY " + json.dumps(res))
"""

_GUARD_PROBES = ("env_blocked", "data_blocked", "broker_blocked", "net_blocked")
_CHILD_PROBES = ("inherited", "hand_built", "copied_reset")


def _verify_child_isolation(d: Path, check_children: bool) -> list:
    """Start a child exactly as a test module would be started (same
    interpreter, environment builder and cwd) and return a list of problems;
    empty means isolation and the guard are effective in that child (and, with
    ``check_children``, in the Python processes it starts)."""
    import json
    import subprocess
    code = _VERIFY_CHILD.replace("CHECK_CHILDREN", "True" if check_children else "False")
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=_REPO_S, env=_module_child_env(d),
                           capture_output=True, text=True, timeout=300)
    except Exception as exc:                       # fail closed
        return ["verification child could not run: %r" % (exc,)]
    res = None
    for ln in r.stdout.splitlines():
        if ln.startswith("ISOLATION_VERIFY "):
            try:
                res = json.loads(ln[len("ISOLATION_VERIFY "):])
            except ValueError:
                res = None
    if r.returncode != 0 or not isinstance(res, dict):
        return ["verification child failed (rc=%d): %s" % (r.returncode, (r.stdout + r.stderr).strip()[-300:])]
    if not res.get("installed"):
        return ["isolation is NOT active in a freshly started test child: its generated "
                "sitecustomize did not run (e.g. another sitecustomize was loaded first, or the "
                "interpreter ignores PYTHONPATH)" + (" [%s]" % res["import_error"] if res.get("import_error") else "")]
    problems = []
    if res.get("tmp") != str(d):
        problems.append("child isolated with an unexpected temp dir %r (expected %r)" % (res.get("tmp"), str(d)))
    problems += ["guard not active in child: %s is False" % k for k in _GUARD_PROBES if res.get(k) is not True]
    if check_children:
        problems += ["Python process started by a test child (%s env) is not isolated: %r"
                     % (k, res.get("child_" + k)) for k in _CHILD_PROBES if res.get("child_" + k) != str(d)]
    return problems


def _refuse_run(problems: list) -> int:
    sys.stderr.write("REFUSED: child-process isolation is not effective in this environment; "
                     "NO test module was run.\n")
    for p in problems:
        sys.stderr.write("  - %s\n" % p)
    return 3


def _run(argv) -> int:
    import subprocess
    import time
    if _REPO_S == "/opt/algo-trader":
        sys.stderr.write("REFUSED: never run tests inside the production checkout; "
                         "use a separate clone (docs/TESTING.md).\n")
        return 2
    names = [a for a in argv if not a.startswith("-")]
    if "--all" in argv:
        names = sorted(p.stem for p in REPO_ROOT.glob("test_*.py")
                       if p.name not in OPERATOR_SCRIPTS)
    names = [n[:-3] if n.endswith(".py") else n for n in names]
    self_check = "--self-check" in argv
    if not names and not self_check:
        sys.stderr.write("usage: python _test_isolation.py [-v] (--all | --self-check | test_module ...)\n")
        return 2
    bad = [n for n in names if n + ".py" in OPERATOR_SCRIPTS]
    if bad:
        sys.stderr.write("REFUSED: operator scripts are not run by the test runner: %s\n" % bad)
        return 2
    root = Path(tempfile.mkdtemp(prefix="algo_test_run_"))
    atexit.register(shutil.rmtree, str(root), True)

    # Fail closed: before ANY test module is launched, prove that a child started
    # exactly like a test module is isolated and guarded, and that the Python
    # processes it starts are too. Otherwise refuse the whole run.
    problems = _verify_child_isolation(root / "_preflight", check_children=True)
    if self_check:
        print("SELF_CHECK_OK" if not problems else "SELF_CHECK_FAILED")
        for p in problems:
            print("  - %s" % p)
        if problems or not names:
            return 0 if not problems else 1
    if problems:
        return _refuse_run(problems)

    verbose = "-v" in argv
    failed, no_tests = [], []
    for n in names:
        d = root / n                               # fresh state per module
        problems = _verify_child_isolation(d, check_children=False)
        if problems:                               # re-verified for this exact environment
            print("ISOLATED_RUN aborted before %s; already run: %d" % (n, names.index(n)))
            return _refuse_run(problems)
        t0 = time.time()
        r = subprocess.run([sys.executable, "-m", "unittest"] + (["-v"] if verbose else []) + [n],
                           cwd=_REPO_S, env=_module_child_env(d),
                           capture_output=True, text=True)
        tail = [ln for ln in (r.stdout + r.stderr).strip().splitlines() if ln.strip()][-1:]
        if r.returncode == 5 and "NO TESTS RAN" in r.stderr:
            status = "NO-TESTS"                     # script-style module (e.g. test_m12.py)
            no_tests.append(n)
        else:
            status = "OK" if r.returncode == 0 else "FAIL(rc=%d)" % r.returncode
        print("%-48s %-10s %6.1fs  %s" % (n, status, time.time() - t0, tail[0] if tail else ""))
        if verbose or status.startswith("FAIL"):
            sys.stdout.write(r.stdout + r.stderr)
        if status.startswith("FAIL"):
            failed.append(n)
    print("ISOLATED_RUN modules=%d failed=%d no_tests=%d %s" % (
        len(names), len(failed), len(no_tests), " ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run(sys.argv[1:]))
