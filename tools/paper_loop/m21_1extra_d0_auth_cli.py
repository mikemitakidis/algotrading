#!/usr/bin/env python3
"""M21.1extra-D0 Commit 2 — operator authorization CLI + audit trail.

The AUTHORIZED, auditable way to turn on unattended paper auto-trading. It sets
the global + ibkr_paper time-boxed authorization that the reader
(determine_signal_only_reason) and is_auto_trading_allowed() already enforce.
This CLI is a WRITER only — enforcement lives in those readers, never here.

Hard safety rules (enforced in code, not by trust):
  - operator-only, behind an ugly confirmation flag;
  - enables the ibkr_paper LANE only — it NEVER enables ibkr_live and NEVER
    touches any eToro/live flag;
  - NEVER copies the legacy ibkr.auto_trading_enabled into a lane;
  - writes genuine UTC +00:00 expiry strings, bounded by a hard max duration;
  - persists through the existing broker_allocation save_policy (which
    validates — a malformed/non-UTC expiry would be rejected);
  - disable immediately clears BOTH global and ibkr_paper authorization;
  - every enable/disable/refused action is written to an append-only audit
    table (evidence only — nothing reads it to make an authorization decision).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from bot.broker_allocation import load_policy, save_policy

# Hard cap on how long an operator can authorize in one action (bounded window).
_MAX_DURATION_HOURS = 8.0
_LANE = "ibkr_paper"
_AUDIT_TABLE = "auto_trading_authorizations"


class AuthorizationRefused(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    # genuine UTC, +00:00 offset
    return dt.astimezone(timezone.utc).isoformat()


# ── audit trail (append-only, evidence only) ────────────────────────────────

def _audit_db_path(db_path: Optional[str]) -> str:
    if db_path:
        return db_path
    env = os.environ.get("AUTO_TRADING_AUDIT_DB_PATH")
    if env:
        return env
    base = Path(__file__).resolve().parents[2]
    return str(base / "data" / "auto_trading_authorizations.db")


def _audit_connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_audit_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS %s ("
        " audit_id TEXT PRIMARY KEY,"      # dedup key for one action
        " action TEXT,"                    # enable | disable | refused
        " lane TEXT,"
        " operator TEXT,"
        " expiry_utc TEXT,"                # for enable; null otherwise
        " duration_hours REAL,"
        " reason TEXT,"
        " created_at_utc TEXT"
        ")" % _AUDIT_TABLE)
    conn.commit()


def _audit_write(db_path: str, *, action: str, lane: str, operator: str,
                 expiry_utc: Optional[str], duration_hours: Optional[float],
                 reason: str, created_at_utc: str) -> Dict[str, Any]:
    """Append one audit row. Append-only (INSERT OR IGNORE on a content hash so
    an identical replay does not duplicate). Evidence only — never read to gate."""
    basis = "|".join([action, lane, operator, str(expiry_utc),
                      str(duration_hours), reason, created_at_utc])
    audit_id = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
    conn = _audit_connect(db_path)
    try:
        _ensure_audit_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO %s (audit_id, action, lane, operator, "
            "expiry_utc, duration_hours, reason, created_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?)" % _AUDIT_TABLE,
            (audit_id, action, lane, operator, expiry_utc, duration_hours,
             reason, created_at_utc))
        conn.commit()
    finally:
        conn.close()
    return {"audit_id": audit_id, "action": action, "lane": lane,
            "operator": operator, "expiry_utc": expiry_utc, "reason": reason,
            "created_at_utc": created_at_utc}


def read_audit(db_path: Optional[str] = None) -> list:
    path = _audit_db_path(db_path)
    if not Path(path).exists():
        return []
    conn = _audit_connect(path)
    try:
        _ensure_audit_table(conn)
        rows = conn.execute(
            "SELECT * FROM %s ORDER BY created_at_utc ASC" % _AUDIT_TABLE
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── enable / disable (writers) ──────────────────────────────────────────────

def enable_ibkr_paper(conn: sqlite3.Connection, *, duration_hours: float,
                      operator: str, confirm: bool,
                      audit_db_path: Optional[str] = None,
                      now: Optional[datetime] = None) -> Dict[str, Any]:
    """Authorize ibkr_paper auto-trading for a bounded window. Sets global +
    ibkr_paper enabled=True with a shared UTC expiry. Never touches live/eToro."""
    now = now or _utc_now()
    audit_path = _audit_db_path(audit_db_path)

    if not confirm:
        _audit_write(audit_path, action="refused", lane=_LANE,
                     operator=operator, expiry_utc=None,
                     duration_hours=duration_hours,
                     reason="confirmation flag not provided",
                     created_at_utc=_utc_iso(now))
        raise AuthorizationRefused(
            "explicit confirmation required to authorize unattended trading")

    if not (0 < duration_hours <= _MAX_DURATION_HOURS):
        _audit_write(audit_path, action="refused", lane=_LANE,
                     operator=operator, expiry_utc=None,
                     duration_hours=duration_hours,
                     reason="duration out of bounds (0 < h <= %s)"
                            % _MAX_DURATION_HOURS,
                     created_at_utc=_utc_iso(now))
        raise AuthorizationRefused(
            "duration must be > 0 and <= %s hours" % _MAX_DURATION_HOURS)

    expiry = _utc_iso(now + timedelta(hours=duration_hours))

    policy = load_policy(conn)
    # global authorization window
    policy["global"]["auto_trading_enabled"] = True
    policy["global"]["auto_trading_enabled_until_utc"] = expiry
    # ibkr_paper LANE only — never ibkr_live, never eToro, never legacy ibkr.
    policy["ibkr_paper"]["auto_trading_enabled"] = True
    policy["ibkr_paper"]["auto_trading_enabled_until_utc"] = expiry
    # Explicit safety: ensure this action never enables the live lane.
    if policy.get("ibkr_live", {}).get("auto_trading_enabled") is True:
        # We do not manage live here; refuse rather than silently proceed.
        _audit_write(audit_path, action="refused", lane=_LANE,
                     operator=operator, expiry_utc=None,
                     duration_hours=duration_hours,
                     reason="ibkr_live is enabled; D0 CLI manages paper only",
                     created_at_utc=_utc_iso(now))
        raise AuthorizationRefused(
            "ibkr_live is enabled — the D0 CLI only manages ibkr_paper")

    save_policy(conn, policy)   # validates (rejects non-UTC/malformed expiry)

    return _audit_write(audit_path, action="enable", lane=_LANE,
                        operator=operator, expiry_utc=expiry,
                        duration_hours=duration_hours,
                        reason="operator enabled ibkr_paper (bounded window)",
                        created_at_utc=_utc_iso(now))


def disable_ibkr_paper(conn: sqlite3.Connection, *, operator: str,
                       audit_db_path: Optional[str] = None,
                       now: Optional[datetime] = None) -> Dict[str, Any]:
    """Immediately clear ibkr_paper authorization (and the global window). Safe
    to call anytime; no confirmation needed to REVOKE."""
    now = now or _utc_now()
    audit_path = _audit_db_path(audit_db_path)

    policy = load_policy(conn)
    policy["global"]["auto_trading_enabled"] = False
    policy["global"]["auto_trading_enabled_until_utc"] = None
    policy["ibkr_paper"]["auto_trading_enabled"] = False
    policy["ibkr_paper"]["auto_trading_enabled_until_utc"] = None
    save_policy(conn, policy)

    return _audit_write(audit_path, action="disable", lane=_LANE,
                        operator=operator, expiry_utc=None,
                        duration_hours=None,
                        reason="operator disabled ibkr_paper",
                        created_at_utc=_utc_iso(now))


# ── CLI ─────────────────────────────────────────────────────────────────────

def _signals_db_path() -> str:
    env = os.environ.get("SIGNALS_DB_PATH")
    if env:
        return env
    base = Path(__file__).resolve().parents[2]
    return str(base / "data" / "signals.db")


def main():
    ap = argparse.ArgumentParser(
        description="D0 operator authorization for ibkr_paper (paper lane only).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    en = sub.add_parser("enable", help="authorize ibkr_paper for a window")
    en.add_argument("--duration-hours", type=float, required=True,
                    help="bounded window (0 < h <= %s)" % _MAX_DURATION_HOURS)
    en.add_argument("--operator", required=True)
    en.add_argument("--i-understand-this-authorizes-unattended-paper-trading",
                    dest="confirm", action="store_true")
    en.add_argument("--db", default=None, help="signals.db path")
    en.add_argument("--audit-db", default=None)

    di = sub.add_parser("disable", help="revoke ibkr_paper authorization")
    di.add_argument("--operator", required=True)
    di.add_argument("--db", default=None)
    di.add_argument("--audit-db", default=None)

    st = sub.add_parser("status", help="show current authorization + audit tail")
    st.add_argument("--db", default=None)
    st.add_argument("--audit-db", default=None)

    args = ap.parse_args()
    db = args.db or _signals_db_path()
    conn = sqlite3.connect(db)
    try:
        if args.cmd == "enable":
            try:
                res = enable_ibkr_paper(
                    conn, duration_hours=args.duration_hours,
                    operator=args.operator, confirm=args.confirm,
                    audit_db_path=args.audit_db)
                print("ENABLED ibkr_paper until %s (audit_id=%s)"
                      % (res["expiry_utc"], res["audit_id"]))
            except AuthorizationRefused as e:
                print("REFUSED: %s" % e)
                raise SystemExit(2)
        elif args.cmd == "disable":
            res = disable_ibkr_paper(conn, operator=args.operator,
                                     audit_db_path=args.audit_db)
            print("DISABLED ibkr_paper (audit_id=%s)" % res["audit_id"])
        elif args.cmd == "status":
            policy = load_policy(conn)
            g = policy.get("global", {})
            lane = policy.get("ibkr_paper", {})
            print("global.auto_trading_enabled: %s until %s"
                  % (g.get("auto_trading_enabled"),
                     g.get("auto_trading_enabled_until_utc")))
            print("ibkr_paper.auto_trading_enabled: %s until %s"
                  % (lane.get("auto_trading_enabled"),
                     lane.get("auto_trading_enabled_until_utc")))
            print("ibkr_live.auto_trading_enabled: %s (D0 CLI never sets this)"
                  % policy.get("ibkr_live", {}).get("auto_trading_enabled"))
            tail = read_audit(args.audit_db)[-5:]
            print("audit tail (%d rows shown):" % len(tail))
            for r in tail:
                print("  %s %s lane=%s operator=%s expiry=%s"
                      % (r["created_at_utc"], r["action"], r["lane"],
                         r["operator"], r["expiry_utc"]))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
