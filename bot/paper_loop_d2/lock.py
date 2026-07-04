#!/usr/bin/env python3
"""M21.1extra-D2.0 — single-cycle process lock (overlap prevention).

Prevents two loop cycles running at once. The lock PATH is injected (tests use
temp paths; the real /opt/algo-trader/data/d2_loop.lock is never touched in
D2.0). A lock held by a LIVE process refuses acquisition; a STALE lock (owning
pid no longer alive) is reclaimed. The lock is always released — on normal
completion and on exception — via the context manager.

No daemon, no cron, no systemd. D2.0 only implements/tests the lock contract.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional, Callable


class LockHeld(RuntimeError):
    """Raised when the lock is held by a live process."""


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` exists. Uses signal 0 (no-op probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # exists but owned by another user -> treat as alive (conservative).
        return True
    except OSError:
        return False
    return True


class CycleLock:
    """Advisory single-holder lock backed by a small JSON file at `path`.

    Usage:
        with CycleLock(path) as lock:
            ...   # exclusive section
        # released here, even on exception
    """

    def __init__(self, path: str, *,
                 clock: Optional[Callable[[], datetime]] = None,
                 pid: Optional[int] = None,
                 pid_alive: Optional[Callable[[int], bool]] = None):
        # path is injected; D2.0 never defaults to the real VPS path.
        self._path = path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._pid = pid if pid is not None else os.getpid()
        self._pid_alive = pid_alive or _pid_alive
        self._acquired = False

    @property
    def path(self) -> str:
        return self._path

    def _read(self) -> Optional[dict]:
        if not os.path.exists(self._path):
            return None
        try:
            with open(self._path) as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (ValueError, OSError):
            # unreadable/corrupt lock file -> treat as stale (reclaimable).
            return {"pid": -1, "corrupt": True}
        return {"pid": -1}

    def acquire(self) -> "CycleLock":
        existing = self._read()
        if existing is not None:
            other_pid = existing.get("pid", -1)
            try:
                other_pid = int(other_pid)
            except (TypeError, ValueError):
                other_pid = -1
            # a live holder (that isn't us) blocks.
            if other_pid != self._pid and self._pid_alive(other_pid):
                raise LockHeld(
                    "cycle lock held by live pid %s at %s"
                    % (other_pid, self._path))
            # otherwise the lock is stale (dead pid / corrupt) -> reclaim.
        self._write()
        self._acquired = True
        return self

    def _write(self) -> None:
        parent = os.path.dirname(self._path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        payload = {"pid": self._pid,
                   "started_at_utc": self._clock().astimezone(
                       timezone.utc).isoformat()}
        tmp = self._path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, self._path)

    def release(self) -> None:
        # only remove a lock WE hold, and only if it's still ours.
        if not self._acquired:
            return
        data = self._read()
        try:
            if data and int(data.get("pid", -1)) == self._pid:
                os.remove(self._path)
        except (OSError, TypeError, ValueError):
            pass
        finally:
            self._acquired = False

    def __enter__(self) -> "CycleLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        # always release, even on exception; do not suppress the exception.
        self.release()
        return False
