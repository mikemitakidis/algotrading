"""bot/gateway_health.py — M15.4 read-only IB Gateway truth layer.

A point-in-time view of IB Gateway state. Distinct from M15.1's
`bot/gateway_watchdog.py`, which runs a background loop and writes
state-transition events to `gateway_state` / `gateway_events` DB tables.

M15.4 contract:
  * READ-ONLY end to end. No DB writes. No systemctl mutations
    (start/stop/restart/enable/disable/mask). No broker calls.
    No IB API call — explicitly per the M15.4 plan. We do NOT call
    api_probe / reqCurrentTime / ib.connect from this module.
  * No order paths. No live writes. No eToro contact.
  * The existing M15.1 `/api/gateway/state` historical view is left
    untouched; this module powers the new `/api/gateway/health`
    point-in-time view.

Truth sources combined:
  1. `systemctl is-active|is-enabled|show ibgateway.service` — service state
  2. `ss -ltn` / /proc/net/tcp — port listener state on 4001/4002
  3. Trading mode discovered from start_ibgateway.sh + config.ini (read-only)
  4. /var/log/ibgateway/ibgateway.log tail — login-error inference
  5. `journalctl -u ibgateway.service` — last N lifecycle events

Status classification (closed set, see STATUSES):
  * service_down                    — systemd inactive/failed/unknown
  * service_active_port_closed      — systemd active, expected port not listening
  * service_active_login_error      — systemd active, port closed, log shows
                                       login/credential failure
  * service_active_api_port_open    — systemd active, expected port listening
                                       (note: we do NOT probe the API to
                                        avoid an IB-side login; "open" means
                                        the TCP socket accepts connections)
  * unknown                         — any required source is unreadable

The boolean `ready_for_ibkr_trading` is True ONLY when status is
`service_active_api_port_open`. That field is the operator's headline.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

GATEWAY_UNIT = "ibgateway.service"
DEFAULT_HOST = "127.0.0.1"
PORT_PAPER = 4002
PORT_LIVE = 4001
DEFAULT_LOG_PATH = Path("/var/log/ibgateway/ibgateway.log")
DEFAULT_START_SCRIPT = Path("/opt/ibc/start_ibgateway.sh")
DEFAULT_IBC_CONFIG_DIR = Path("/opt/ibc")

STATUSES = (
    "service_down",
    "service_active_port_closed",
    "service_active_login_error",
    "service_active_api_port_open",
    "unknown",
)

# Patterns scanned in the gateway log to infer authentication failure.
# Conservative — we want true positives ("login error happened") with
# minimal false positives. The patterns are matched case-insensitive on
# the tail of the log; if any one matches, login_error_detected=True.
LOGIN_ERROR_PATTERNS = (
    r"Unrecognized\s+Username\s+or\s+Password",
    r"Login\s+Failed",
    r"Invalid\s+(?:username|password|credentials)",
    r"Authentication\s+(?:failed|error)",
    r"Username/Password\s+is\s+incorrect",
    r"Soft\s+lockout",
)
_LOGIN_ERROR_RE = re.compile("|".join(LOGIN_ERROR_PATTERNS), re.IGNORECASE)

# How many log bytes from the tail to scan for login-error inference.
# 64 KB is plenty for the last few session attempts without making the
# probe expensive.
LOG_TAIL_BYTES = 64 * 1024

# How many recent lifecycle events to surface in the response.
RECENT_LIFECYCLE_EVENTS = 10

# Lifecycle event keywords we care about in journalctl output.
_LIFECYCLE_RE = re.compile(
    r"Started\s+IB|Stopped\s+IB|Failed|main\s+process\s+exited|"
    r"Scheduled\s+restart|killed\s+signal",
    re.IGNORECASE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_readonly(argv: List[str], timeout: float = 3.0) -> Tuple[int, str, str]:
    """Run a command read-only. Returns (rc, stdout, stderr). On any
    subprocess failure returns (-1, '', error_str). The caller decides
    whether failure means 'unknown' or a hard error."""
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as e:
        return -1, "", f"{type(e).__name__}: {e}"[:200]


# ─────────────────────────────────────────────────────────────────────────────
# Source 1 — systemd state
# ─────────────────────────────────────────────────────────────────────────────


def read_systemd_state(unit: str = GATEWAY_UNIT) -> Dict[str, Any]:
    """Return {active, enabled, sub_state, main_pid, since_utc,
    n_restarts, fragment_path, source_ok}. Pure read; no mutations."""
    out: Dict[str, Any] = {
        "unit":           unit,
        "active":         "unknown",
        "enabled":        "unknown",
        "sub_state":      None,
        "main_pid":       None,
        "since_utc":      None,
        "n_restarts":     None,
        "fragment_path":  None,
        "source_ok":      False,
    }
    rc_a, so_a, _ = _run_readonly(["systemctl", "is-active", unit])
    if rc_a != -1:
        out["active"] = so_a.strip() or "unknown"
    rc_e, so_e, _ = _run_readonly(["systemctl", "is-enabled", unit])
    if rc_e != -1:
        out["enabled"] = so_e.strip() or "unknown"
    # Pull detail properties in one show call.
    rc, so, _ = _run_readonly([
        "systemctl", "show", unit, "--property=SubState",
        "--property=MainPID", "--property=ActiveEnterTimestamp",
        "--property=NRestarts", "--property=FragmentPath",
    ])
    if rc == 0:
        props = {}
        for line in so.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()
        out["sub_state"]     = props.get("SubState") or None
        mp = props.get("MainPID")
        out["main_pid"]      = int(mp) if (mp and mp.isdigit() and mp != "0") else None
        out["since_utc"]     = props.get("ActiveEnterTimestamp") or None
        nr = props.get("NRestarts")
        out["n_restarts"]    = int(nr) if (nr and nr.isdigit()) else None
        out["fragment_path"] = props.get("FragmentPath") or None
        out["source_ok"] = True
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Source 2 — port listener state (no API call)
# ─────────────────────────────────────────────────────────────────────────────


def probe_tcp_listening(host: str, port: int, timeout: float = 1.0) -> Optional[bool]:
    """Cheap connect-and-immediately-close probe. Returns:
      * True  — accepted the connection (port has a listener)
      * False — connection refused (no listener)
      * None  — neither: timeout / DNS failure / unrelated OSError
                (treated as 'unknown' upstream)

    We do NOT send any IB API bytes — the connection is closed
    immediately after acceptance. This is the established TCP-listener
    probe pattern in M15.1's tcp_probe; we use a separate function so
    the M15.4 surface is fully self-contained (no import dependency
    on bot.gateway_watchdog which pulls ib_insync transitively)."""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        try:
            s.close()
        except OSError:
            pass
        return True
    except ConnectionRefusedError:
        return False
    except (socket.timeout, OSError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Source 3 — trading mode discovery
# ─────────────────────────────────────────────────────────────────────────────


def detect_trading_mode(
    start_script: Optional[Path] = None,
    ibc_config_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Discover whether the running gateway is paper or live. Read-only
    inspection of:
      * start_ibgateway.sh — looks for `TRADING_MODE=`, `--mode=`
      * IBC config files — `config.live.ini`, `config.paper.ini`, etc.
      * /proc/<MainPID>/cmdline — if the systemd MainPID is in scope

    Returns {mode, expected_port, evidence, source_ok}. `mode` is
    "paper" / "live" / "unknown". `expected_port` is the corresponding
    port (4002/4001) or None when mode is unknown."""
    start_script = start_script or DEFAULT_START_SCRIPT
    ibc_config_dir = ibc_config_dir or DEFAULT_IBC_CONFIG_DIR
    evidence: List[str] = []
    mode: Optional[str] = None

    # 1. start_ibgateway.sh
    if start_script.is_file():
        try:
            text = start_script.read_text(errors="replace")
        except OSError:
            text = ""
        if text:
            m_env = re.search(r"TRADING_MODE\s*=\s*['\"]?(paper|live)",
                              text, re.IGNORECASE)
            if m_env:
                mode = m_env.group(1).lower()
                evidence.append(
                    f"{start_script}: TRADING_MODE={mode}")
            m_arg = re.search(r"--mode\s*[=\s]\s*['\"]?(paper|live)",
                              text, re.IGNORECASE)
            if m_arg and not mode:
                mode = m_arg.group(1).lower()
                evidence.append(f"{start_script}: --mode={mode}")

    # 2. IBC config dir
    if ibc_config_dir.is_dir():
        for fname in ("config.live.ini", "config.paper.ini", "config.ini"):
            cfg = ibc_config_dir / fname
            if cfg.is_file():
                try:
                    cfg_text = cfg.read_text(errors="replace")
                except OSError:
                    continue
                m_cfg = re.search(r"^\s*TradingMode\s*=\s*(paper|live)",
                                   cfg_text, re.IGNORECASE | re.MULTILINE)
                if m_cfg:
                    cfg_mode = m_cfg.group(1).lower()
                    evidence.append(f"{cfg}: TradingMode={cfg_mode}")
                    if mode is None:
                        mode = cfg_mode

    expected_port: Optional[int] = None
    if mode == "paper":
        expected_port = PORT_PAPER
    elif mode == "live":
        expected_port = PORT_LIVE

    return {
        "mode":           mode or "unknown",
        "expected_port":  expected_port,
        "evidence":       evidence,
        "source_ok":      mode is not None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Source 4 — login-error inference from /var/log/ibgateway/ibgateway.log
# ─────────────────────────────────────────────────────────────────────────────


def read_log_tail(
    log_path: Optional[Path] = None,
    n_bytes: int = LOG_TAIL_BYTES,
) -> Dict[str, Any]:
    """Tail the gateway log (no writes, no truncation). Returns
    {present, login_error_detected, matched_pattern, last_line_utc_hint,
    source_ok}."""
    log_path = log_path or DEFAULT_LOG_PATH
    out: Dict[str, Any] = {
        "path":                  str(log_path),
        "present":               False,
        "login_error_detected":  False,
        "matched_pattern":       None,
        "last_line_hint":        None,
        "source_ok":             False,
    }
    if not log_path.is_file():
        out["source_ok"] = True   # absence is a known answer
        return out
    out["present"] = True
    try:
        size = log_path.stat().st_size
        with open(log_path, "rb") as fh:
            if size > n_bytes:
                fh.seek(-n_bytes, 2)
            tail = fh.read()
    except OSError:
        return out
    out["source_ok"] = True
    try:
        text = tail.decode("utf-8", errors="replace")
    except Exception:
        return out
    m = _LOGIN_ERROR_RE.search(text)
    if m:
        out["login_error_detected"] = True
        out["matched_pattern"] = m.group(0)
    # last non-empty line, capped at 200 chars — no credentials in IBC
    # logs by default, but cap defensively
    last_line = ""
    for line in reversed(text.splitlines()):
        if line.strip():
            last_line = line.strip()[:200]
            break
    out["last_line_hint"] = last_line or None
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Source 5 — recent lifecycle events from journalctl
# ─────────────────────────────────────────────────────────────────────────────


def read_recent_lifecycle_events(
    unit: str = GATEWAY_UNIT,
    n: int = RECENT_LIFECYCLE_EVENTS,
    since: str = "30 days ago",
) -> Dict[str, Any]:
    """Pull the last N lifecycle events from journalctl for the gateway
    unit. Read-only. Returns {events, n_restarts_30d, n_failures_30d,
    source_ok}."""
    out: Dict[str, Any] = {
        "events":          [],
        "n_restarts_30d":  None,
        "n_failures_30d":  None,
        "source_ok":       False,
    }
    rc, so, _ = _run_readonly(
        ["journalctl", "-u", unit, "--since", since,
         "--no-pager", "--output=short-iso-precise"],
        timeout=5.0,
    )
    if rc == -1:
        return out
    out["source_ok"] = True
    matching: List[str] = []
    n_restarts = 0
    n_failures = 0
    for line in so.splitlines():
        if _LIFECYCLE_RE.search(line):
            matching.append(line[:240])
            ll = line.lower()
            if "started ib" in ll or "scheduled restart" in ll:
                n_restarts += 1
            if "failed" in ll or "main process exited" in ll:
                n_failures += 1
    out["events"] = matching[-n:]
    out["n_restarts_30d"] = n_restarts
    out["n_failures_30d"] = n_failures
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation — the single point-in-time truth
# ─────────────────────────────────────────────────────────────────────────────


def _classify(
    systemd: Dict[str, Any],
    tcp_reachable: Optional[bool],
    login_error_detected: bool,
) -> str:
    """Map (systemd, tcp, login) -> closed-set status. See STATUSES."""
    active = systemd.get("active")
    if active in ("inactive", "failed", "deactivating", "activating"):
        return "service_down"
    if active != "active":
        return "unknown"
    # active==active from here.
    if tcp_reachable is True:
        return "service_active_api_port_open"
    if tcp_reachable is False:
        if login_error_detected:
            return "service_active_login_error"
        return "service_active_port_closed"
    # tcp_reachable is None (probe failed unrelated to refusal).
    return "unknown"


def assemble_health(
    *,
    host: str = DEFAULT_HOST,
    unit: str = GATEWAY_UNIT,
    log_path: Optional[Path] = None,
    start_script: Optional[Path] = None,
    ibc_config_dir: Optional[Path] = None,
    tcp_timeout: float = 1.0,
) -> Dict[str, Any]:
    """Single read-only assembly. All five sources combined into the
    JSON shape consumed by `GET /api/gateway/health`.

    No DB write. No subprocess that mutates. No IB API call. No
    broker construction.
    """
    systemd = read_systemd_state(unit=unit)
    mode_info = detect_trading_mode(start_script=start_script,
                                      ibc_config_dir=ibc_config_dir)
    expected_port = mode_info["expected_port"]
    tcp_paper = probe_tcp_listening(host, PORT_PAPER, timeout=tcp_timeout)
    tcp_live  = probe_tcp_listening(host, PORT_LIVE,  timeout=tcp_timeout)
    # Reachable on the expected port for the discovered mode.
    if expected_port == PORT_PAPER:
        tcp_reachable = tcp_paper
    elif expected_port == PORT_LIVE:
        tcp_reachable = tcp_live
    else:
        tcp_reachable = None

    log_info = read_log_tail(log_path=log_path)
    lifecycle = read_recent_lifecycle_events(unit=unit)

    status = _classify(
        systemd=systemd,
        tcp_reachable=tcp_reachable,
        login_error_detected=log_info["login_error_detected"],
    )

    return {
        "as_of_utc":               _now_iso(),
        "unit":                    unit,
        "systemd": {
            "active":          systemd["active"],
            "enabled":         systemd["enabled"],
            "sub_state":       systemd["sub_state"],
            "main_pid":        systemd["main_pid"],
            "since_utc":       systemd["since_utc"],
            "n_restarts":      systemd["n_restarts"],
            "fragment_path":   systemd["fragment_path"],
            "source_ok":       systemd["source_ok"],
        },
        "systemd_active":          systemd["active"] == "active",
        "mode":                    mode_info["mode"],
        "expected_port":           expected_port,
        "mode_evidence":           mode_info["evidence"],
        "tcp": {
            "paper_4002":  tcp_paper,
            "live_4001":   tcp_live,
        },
        "tcp_reachable":           tcp_reachable,
        "log": {
            "path":                  log_info["path"],
            "present":               log_info["present"],
            "last_line_hint":        log_info["last_line_hint"],
            "source_ok":             log_info["source_ok"],
        },
        "login_error_detected":    log_info["login_error_detected"],
        "login_error_pattern":     log_info["matched_pattern"],
        "lifecycle": {
            "events":          lifecycle["events"],
            "n_restarts_30d":  lifecycle["n_restarts_30d"],
            "n_failures_30d":  lifecycle["n_failures_30d"],
            "source_ok":       lifecycle["source_ok"],
        },
        "status":                  status,
        "ready_for_ibkr_trading":  status == "service_active_api_port_open",
    }


__all__ = [
    "GATEWAY_UNIT", "PORT_PAPER", "PORT_LIVE", "STATUSES",
    "assemble_health",
    "read_systemd_state", "probe_tcp_listening",
    "detect_trading_mode", "read_log_tail",
    "read_recent_lifecycle_events",
]
