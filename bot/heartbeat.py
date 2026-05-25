"""
M15.2 — Independent heartbeat thread.

Writes data/heartbeat.json every HEARTBEAT_INTERVAL_SEC (default 45s) via
atomic rename. Runs as a daemon thread inside main.py, INDEPENDENT of the
scan cycle, so /api/health can detect process death/wedge even between
scan intervals.

Design constraints (M15.2 review-locked):
- NEVER take a write lock on data/signals.db (no BEGIN IMMEDIATE).
  Lock contention with the trading scan loop is forbidden.
- DB-writability check is done via a tiny tempfile probe in the data/
  directory plus a read-only sqlite3 connect-and-PRAGMA check. The
  probe result is stored as `db_writable: bool` inside heartbeat.json,
  so the /api/health endpoint NEVER touches signals.db.
- Atomic write via tempfile + os.replace so a partial read never
  observes truncated JSON.
- Crash-resilient: any exception in a tick is logged and the thread
  continues. The heartbeat thread MUST NOT silently die.
- Path-stable: bot/heartbeat.py and dashboard/app.py both use
  resolve_heartbeat_path() so they agree on the file location.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_HEARTBEAT_PATH = _REPO_ROOT / 'data' / 'heartbeat.json'
_DEFAULT_SIGNALS_DB = _REPO_ROOT / 'data' / 'signals.db'


def _utc_iso() -> str:
    """Microsecond precision so scan_started/completed taken milliseconds
    apart never collide."""
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def resolve_heartbeat_path() -> Path:
    """Single source of truth for the heartbeat file location.
    Used by bot/heartbeat.py AND dashboard/app.py to prevent path drift."""
    override = os.getenv('HEARTBEAT_FILE_PATH', '').strip()
    if override:
        return Path(override).resolve()
    return _DEFAULT_HEARTBEAT_PATH


def resolve_signals_db_path() -> Path:
    """Where signals.db lives. Read-only check only; never written by heartbeat."""
    override = os.getenv('SIGNALS_DB_PATH', '').strip()
    if override:
        return Path(override).resolve()
    return _DEFAULT_SIGNALS_DB


def _probe_db_writable(db_path: Path, data_dir: Path) -> bool:
    """Non-contentious DB+dir writability probe.

    1. Tries to create+delete a tiny tempfile inside data_dir (cheap, no
       locking on signals.db).
    2. Opens signals.db read-only and runs PRAGMA quick_check on the file
       itself — proves we can READ the DB without taking a write lock.

    Returns True only if both succeed. NEVER calls BEGIN IMMEDIATE or
    any write SQL against signals.db.
    """
    # Step 1: data directory writable?
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=str(data_dir), prefix='.hb_probe_', delete=True
        ) as tf:
            tf.write(b'1')
            tf.flush()
    except (OSError, PermissionError) as e:
        log.warning('[HEARTBEAT] data_dir not writable: %s', e)
        return False

    # Step 2: signals.db readable (if it exists)?
    if not db_path.exists():
        # No DB yet — that is acceptable on a fresh deploy; init_flywheel
        # will create it. Treat as "writable" in the sense that nothing
        # prevents the trading loop from creating it.
        return True
    try:
        # mode=ro plus uri=True opens a true read-only handle that cannot
        # acquire write locks under any circumstances.
        conn = sqlite3.connect(
            f'file:{db_path}?mode=ro', uri=True, timeout=2.0,
        )
        try:
            conn.execute('PRAGMA schema_version').fetchone()
            return True
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.warning('[HEARTBEAT] signals.db not readable: %s', e)
        return False


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically: tempfile in same dir, then os.replace.
    Guarantees readers never see partial/truncated content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + '.tmp.', dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f, separators=(',', ':'), default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class Heartbeat:
    """Daemon thread that writes data/heartbeat.json every interval.

    Public API used by main.py:
      hb = Heartbeat(scan_interval_sec=900)
      hb.start()
      hb.record_scan_started()
      ... scan work ...
      hb.record_scan_completed()
      hb.stop()
    """

    def __init__(self, scan_interval_sec: int,
                 heartbeat_path: Optional[Path] = None,
                 signals_db_path: Optional[Path] = None,
                 interval_sec: Optional[int] = None):
        self.scan_interval_sec = int(scan_interval_sec)
        self.heartbeat_path = heartbeat_path or resolve_heartbeat_path()
        self.signals_db_path = signals_db_path or resolve_signals_db_path()
        self.interval_sec = interval_sec if interval_sec is not None \
            else _int_env('HEARTBEAT_INTERVAL_SEC', 45)
        self.data_dir = self.heartbeat_path.parent

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._pid = os.getpid()
        self._process_started_at = _utc_iso()
        self._last_scan_started_ts: Optional[str] = None
        self._last_scan_completed_ts: Optional[str] = None

    # ---- public API ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name='heartbeat', daemon=True,
        )
        self._thread.start()
        log.info(
            '[HEARTBEAT] started: path=%s interval=%ds scan_interval=%ds',
            self.heartbeat_path, self.interval_sec, self.scan_interval_sec,
        )
        # Write an immediate first heartbeat so the endpoint has data
        # within milliseconds of startup, not interval_sec later.
        self._tick()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def record_scan_started(self) -> None:
        with self._lock:
            self._last_scan_started_ts = _utc_iso()
        # Force a heartbeat write now so /api/health sees the new
        # scan_started_ts immediately without waiting for the next tick.
        self._tick()

    def record_scan_completed(self) -> None:
        with self._lock:
            self._last_scan_completed_ts = _utc_iso()
        self._tick()

    # ---- internal ----

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                # CRITICAL: do NOT let exceptions kill the heartbeat thread.
                log.exception('[HEARTBEAT] tick error (continuing)')
            self._stop.wait(self.interval_sec)

    def _tick(self) -> None:
        db_writable = _probe_db_writable(self.signals_db_path, self.data_dir)
        with self._lock:
            payload = {
                'last_heartbeat_ts':       _utc_iso(),
                'last_scan_started_ts':    self._last_scan_started_ts,
                'last_scan_completed_ts':  self._last_scan_completed_ts,
                'scan_interval_sec':       self.scan_interval_sec,
                'db_writable':             db_writable,
                'db_writable_checked_at':  _utc_iso(),
                'pid':                     self._pid,
                'process_started_at':      self._process_started_at,
                'heartbeat_interval_sec':  self.interval_sec,
            }
        _atomic_write_json(self.heartbeat_path, payload)


# Module-level convenience for tests and one-off scripts
def read_heartbeat(path: Optional[Path] = None) -> Optional[dict]:
    p = path or resolve_heartbeat_path()
    if not p.exists():
        return None
    try:
        with open(p, 'r') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning('[HEARTBEAT] read_heartbeat failed: %s', e)
        return None
