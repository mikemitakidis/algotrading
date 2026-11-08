"""bot/paper_loop_d2/risk_refresh.py: M21 PR-c, fail-closed refresh of the M14 risk data.

Before the paper loop asks M14 for a new entry, the risk gate calls a refresher that
re-ingests the paper account's exposure and daily P&L into signals.db with the
existing read-only ingestion CLIs:

    tools/ingest_exposure_state.py --scope ibkr_paper --db <signals.db>   (M15.5 reader)
    tools/ingest_risk_state.py     --scope ibkr_paper --db <signals.db>   (PR-b reader)

Each runs as a child process with a hard timeout, so a hung gateway call, an event
loop clash with the loop's own IB connection, or a crash cannot stall or corrupt
the loop. Both CLIs exit 0 only when the reading is known; anything else (exit 1
UNKNOWN, exit 2 DB error, timeout, spawn failure, lock busy) is a refusal, and the
gate blocks without calling M14.

The refresher is the availability half. The safety half is M14 itself (PR-c,
engine #7): even after a "successful" refresh, M14 blocks unless every reading it
uses carries a recent fetch time. So a refresher that silently wrote nothing, wrote
to another DB or ran too long still cannot let stale data through.

Refreshes are serialised across processes with an exclusive file lock, because the
two readers use fixed client ids (15 and 16) and two concurrent runs would collide
at the gateway. Only the ibkr_paper scope is ever refreshed: paper trading never
polls the LIVE account.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

PAPER_SCOPE = "ibkr_paper"
INGESTERS: Tuple[Tuple[str, str], ...] = (
    ("exposure", "tools/ingest_exposure_state.py"),
    ("pnl", "tools/ingest_risk_state.py"),
)
DEFAULT_TIMEOUT_SEC = 90.0
DEFAULT_LOCK_WAIT_SEC = 30.0
_LOCK_POLL_SEC = 0.2

RefreshResult = Tuple[bool, str]


class PaperRiskRefresher:
    """``refresher() -> (ok, reason)``. ``ok`` is True only when both ingestions
    exited 0 within their timeouts. Never raises."""

    def __init__(self, *, repo: Path, db_path: str, lock_path: str,
                 python: str = sys.executable,
                 timeout_sec: float = DEFAULT_TIMEOUT_SEC,
                 lock_wait_sec: float = DEFAULT_LOCK_WAIT_SEC,
                 runner: Optional[Callable[..., "subprocess.CompletedProcess"]] = None,
                 ingesters: Sequence[Tuple[str, str]] = INGESTERS):
        if not db_path or not lock_path:
            raise ValueError("db_path and lock_path are required")
        if not (isinstance(timeout_sec, (int, float)) and timeout_sec > 0):
            raise ValueError("timeout_sec must be positive")
        self.repo = Path(repo)
        self.db_path = str(db_path)
        self.lock_path = str(lock_path)
        self.python = python
        self.timeout_sec = float(timeout_sec)
        self.lock_wait_sec = float(lock_wait_sec)
        self._run = runner or subprocess.run
        self.ingesters = tuple(ingesters)
        self.last: dict = {}

    def commands(self):
        return [(name, [self.python, "-I", "-B", str(self.repo / script), "--scope", PAPER_SCOPE,
                        "--db", self.db_path]) for name, script in self.ingesters]

    def __call__(self) -> RefreshResult:
        try:
            fd = self._lock()
        except Exception as e:
            return False, "refresh_lock_error:%s" % type(e).__name__
        if fd is None:
            return False, "refresh_lock_busy"
        try:
            return self._refresh()
        except Exception as e:                      # pragma: no cover - defensive
            return False, "refresh_error:%s" % type(e).__name__
        finally:
            self._unlock(fd)

    # ── internals ─────────────────────────────────────────────────────────────
    def _refresh(self) -> RefreshResult:
        t0 = time.monotonic()
        for name, argv in self.commands():
            try:
                r = self._run(argv, cwd=str(self.repo), capture_output=True, text=True,
                              timeout=self.timeout_sec, env=dict(os.environ))
            except subprocess.TimeoutExpired:
                self.last = {"failed": name, "why": "timeout"}
                return False, "%s_refresh_timeout" % name
            except Exception as e:
                self.last = {"failed": name, "why": type(e).__name__}
                return False, "%s_refresh_error:%s" % (name, type(e).__name__)
            rc = getattr(r, "returncode", None)
            if rc != 0:
                self.last = {"failed": name, "why": "rc=%r" % (rc,)}
                return False, "%s_refresh_rc_%s" % (name, rc)
        self.last = {"ok": True, "elapsed_sec": round(time.monotonic() - t0, 3)}
        return True, "refreshed"

    def _lock(self) -> Optional[int]:
        import fcntl
        Path(self.lock_path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + max(0.0, self.lock_wait_sec)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return None
                time.sleep(_LOCK_POLL_SEC)

    @staticmethod
    def _unlock(fd: int) -> None:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def make_paper_risk_refresher(*, repo: Path, db_path: str, lock_path: str,
                              **kwargs) -> PaperRiskRefresher:
    """The production refresher for the paper loop's risk gate."""
    return PaperRiskRefresher(repo=repo, db_path=db_path, lock_path=lock_path, **kwargs)


__all__ = ["PaperRiskRefresher", "make_paper_risk_refresher", "INGESTERS", "PAPER_SCOPE"]
