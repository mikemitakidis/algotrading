#!/usr/bin/env python3
"""M21.1extra-D2.0 — single-cycle process lock (overlap prevention).

Prevents two loop cycles running at once. The lock PATH is injected (tests use
temp paths; the real /opt/algo-trader/data/d2_loop.lock is never touched in
D2.0).

Contract:
  - ATOMIC acquisition via os.open(O_CREAT | O_EXCL). If the create wins, the
    lock is held. If it loses (FileExistsError), read the existing lock: a LIVE
    owning pid (ANY pid, including our own) -> refuse (LockHeld); a stale/corrupt
    lock -> remove and retry the atomic create.
  - Always released on normal completion AND on exception (context manager),
    and only a lock WE actually acquired is removed.

No daemon, no cron, no systemd. D2.0 only defines/tests the lock contract.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional, Callable


class LockHeld(RuntimeError):
    """Raised when the lock is held by a live process (any pid, incl. ours)."""


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` exists. Uses signal 0 (no-op probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by another user -> conservatively alive
    except OSError:
        return False
    return True


class CycleLock:
    """Advisory single-holder lock backed by an atomically-created JSON file."""

    _MAX_RECLAIM_RETRIES = 5

    def __init__(self, path: str, *,
                 clock: Optional[Callable[[], datetime]] = None,
                 pid: Optional[int] = None,
                 pid_alive: Optional[Callable[[int], bool]] = None):
        self._path = path                       # injected; never the VPS path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._pid = pid if pid is not None else os.getpid()
        self._pid_alive = pid_alive or _pid_alive
        self._acquired = False

    @property
    def path(self) -> str:
        return self._path

    def _payload_bytes(self) -> bytes:
        payload = {"pid": self._pid,
                   "started_at_utc": self._clock().astimezone(
                       timezone.utc).isoformat()}
        return json.dumps(payload).encode("utf-8")

    def _read(self) -> Optional[dict]:
        try:
            with open(self._path) as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
            return {"pid": -1}                    # unexpected shape -> stale
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            return {"pid": -1, "corrupt": True}   # unreadable/corrupt -> stale

    def _owning_pid(self, data: dict) -> int:
        try:
            return int(data.get("pid", -1))
        except (TypeError, ValueError):
            return -1

    def acquire(self) -> "CycleLock":
        parent = os.path.dirname(self._path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

        for _ in range(self._MAX_RECLAIM_RETRIES):
            try:
                fd = os.open(self._path,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                # someone holds it (or a stale file remains). Inspect.
                data = self._read()
                if data is None:
                    # vanished between EXCL-fail and read -> retry create.
                    continue
                owner = self._owning_pid(data)
                # ANY live owning pid blocks -- including our own pid.
                if self._pid_alive(owner):
                    raise LockHeld(
                        "cycle lock held by live pid %s at %s"
                        % (owner, self._path))
                # stale or corrupt -> reclaim: remove then retry atomic create.
                try:
                    os.remove(self._path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise LockHeld("cannot reclaim stale lock at %s: %s"
                                   % (self._path, type(exc).__name__))
                continue
            else:
                # we won the atomic create -> write our payload and hold it.
                try:
                    os.write(fd, self._payload_bytes())
                finally:
                    os.close(fd)
                self._acquired = True
                return self

        # exhausted retries (persistent contention/racing reclaims).
        raise LockHeld("could not acquire cycle lock at %s (contention)"
                       % self._path)

    def release(self) -> None:
        """Remove the lock only if WE acquired it and it is still ours."""
        if not self._acquired:
            return
        data = self._read()
        try:
            if data is not None and self._owning_pid(data) == self._pid:
                os.remove(self._path)
        except (OSError, TypeError, ValueError):
            pass
        finally:
            self._acquired = False

    def __enter__(self) -> "CycleLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False   # never suppress the exception
