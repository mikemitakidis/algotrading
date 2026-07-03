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
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from bot.broker_allocation import (
    load_policy, save_policy, validate_policy, DEFAULT_POLICY, POLICY_KEY,
    _ensure_table as _ensure_policy_table,
)

# Hard cap on how long an operator can authorize in one action (bounded window).
_MAX_DURATION_HOURS = 8.0
_LANE = "ibkr_paper"
_AUDIT_TABLE = "auto_trading_authorizations"


def _normalise_policy_for_d0(policy: Any) -> Dict[str, Any]:
    """Return a D0-shaped policy: start from DEFAULT_POLICY structure, overlay
    safe existing settings, and ensure the D0 fields exist WITHOUT ever copying
    a legacy enable into a lane or enabling ibkr_live.

    Handles old pre-D0 signals.db rows that lack global.auto_trading_enabled_
    until_utc / ibkr_paper / ibkr_live. Fail-closed: every authorization field
    the caller doesn't explicitly set ends up disabled/null.
    """
    base = json.loads(json.dumps(DEFAULT_POLICY))  # deep copy, all-disabled
    if not isinstance(policy, dict):
        return base

    # version
    base["version"] = policy.get("version", base["version"])

    # global: preserve capital + kill_switch; NEVER trust an old enable without
    # an expiry (D0 requires the time-box), so force disabled here — the enable
    # path sets both enable+expiry explicitly.
    g_in = policy.get("global") if isinstance(policy.get("global"), dict) else {}
    base["global"]["max_auto_trading_capital"] = g_in.get(
        "max_auto_trading_capital", base["global"]["max_auto_trading_capital"])
    base["global"]["kill_switch"] = bool(g_in.get("kill_switch", False))
    base["global"]["auto_trading_enabled"] = False
    base["global"]["auto_trading_enabled_until_utc"] = None

    # legacy ibkr block: preserve capital/limits + kill_switch, but DO NOT let
    # its auto_trading_enabled leak into lanes. Keep it as-is for compat.
    ibkr_in = policy.get("ibkr") if isinstance(policy.get("ibkr"), dict) else {}
    for k in ("max_auto_trading_capital", "max_single_trade_amount",
              "max_daily_loss", "max_open_positions", "kill_switch"):
        if k in ibkr_in:
            base["ibkr"][k] = ibkr_in[k]
    # legacy auto_trading_enabled is intentionally NOT propagated to lanes; keep
    # the legacy block's own flag value for backward-compat display only.
    base["ibkr"]["auto_trading_enabled"] = bool(
        ibkr_in.get("auto_trading_enabled", False))

    # etoro block: preserve, never enable anything here.
    etoro_in = policy.get("etoro") if isinstance(policy.get("etoro"), dict) else {}
    for k in ("max_auto_trading_capital", "max_single_trade_amount",
              "max_daily_loss", "max_open_positions", "kill_switch"):
        if k in etoro_in:
            base["etoro"][k] = etoro_in[k]
    base["etoro"]["auto_trading_enabled"] = bool(
        etoro_in.get("auto_trading_enabled", False))

    # lanes: if present, preserve ONLY a kill_switch; force auth disabled/null.
    # NEVER carry an enable/expiry forward implicitly — the enable path sets them.
    for lane in ("ibkr_paper", "ibkr_live"):
        lane_in = policy.get(lane) if isinstance(policy.get(lane), dict) else {}
        base[lane]["auto_trading_enabled"] = False
        base[lane]["auto_trading_enabled_until_utc"] = None
        base[lane]["kill_switch"] = bool(lane_in.get("kill_switch", False))

    # routing: preserve if present and shaped, else keep default.
    r_in = policy.get("routing")
    if isinstance(r_in, dict):
        for k in ("default_broker", "route_overrides", "allowed_brokers",
                  "etoro_live_enabled"):
            if k in r_in:
                base["routing"][k] = r_in[k]

    return base


class AuthorizationRefused(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    # genuine UTC, +00:00 offset
    return dt.astimezone(timezone.utc).isoformat()


# ── audit trail (append-only, evidence only) ────────────────────────────────

def _ensure_audit_table_conn(conn: sqlite3.Connection) -> None:
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


def _audit_id_for(action, lane, operator, expiry_utc, duration_hours, reason,
                  created_at_utc) -> str:
    basis = "|".join([action, lane, operator, str(expiry_utc),
                      str(duration_hours), reason, created_at_utc])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _audit_insert_conn(conn: sqlite3.Connection, *, action: str, lane: str,
                       operator: str, expiry_utc: Optional[str],
                       duration_hours: Optional[float], reason: str,
                       created_at_utc: str) -> Dict[str, Any]:
    """Append one audit row on the given connection (NO commit — caller commits).
    Append-only INSERT OR IGNORE on a content hash. Evidence only."""
    audit_id = _audit_id_for(action, lane, operator, expiry_utc,
                             duration_hours, reason, created_at_utc)
    _ensure_audit_table_conn(conn)
    conn.execute(
        "INSERT OR IGNORE INTO %s (audit_id, action, lane, operator, "
        "expiry_utc, duration_hours, reason, created_at_utc) "
        "VALUES (?,?,?,?,?,?,?,?)" % _AUDIT_TABLE,
        (audit_id, action, lane, operator, expiry_utc, duration_hours,
         reason, created_at_utc))
    return {"audit_id": audit_id, "action": action, "lane": lane,
            "operator": operator, "expiry_utc": expiry_utc, "reason": reason,
            "created_at_utc": created_at_utc}


def _audit_write_standalone(conn: sqlite3.Connection, **kw) -> Dict[str, Any]:
    """Write a single audit row and commit it on its own (for refusals, which
    make no policy change). Safe to commit independently."""
    res = _audit_insert_conn(conn, **kw)
    conn.commit()
    return res


def read_audit(conn: sqlite3.Connection) -> list:
    _ensure_audit_table_conn(conn)
    rows = conn.execute(
        "SELECT * FROM %s ORDER BY created_at_utc ASC" % _AUDIT_TABLE
    ).fetchall()
    return [dict(r) for r in rows]


# ── enable / disable (writers) ──────────────────────────────────────────────

def enable_ibkr_paper(conn: sqlite3.Connection, *, duration_hours: float,
                      operator: str, confirm: bool,
                      now: Optional[datetime] = None) -> Dict[str, Any]:
    """Authorize ibkr_paper auto-trading for a bounded window. Sets global +
    ibkr_paper enabled=True with a shared UTC expiry. Never touches live/eToro.

    ATOMIC: the policy row and the audit row are written in ONE transaction on
    the same connection and committed together — so a successful enable is
    auditable by construction (audit failure rolls back the policy, leaving
    authorization unchanged/disabled)."""
    now = now or _utc_now()
    created = _utc_iso(now)

    if not confirm:
        _audit_write_standalone(
            conn, action="refused", lane=_LANE, operator=operator,
            expiry_utc=None, duration_hours=duration_hours,
            reason="confirmation flag not provided", created_at_utc=created)
        raise AuthorizationRefused(
            "explicit confirmation required to authorize unattended trading")

    if not (0 < duration_hours <= _MAX_DURATION_HOURS):
        _audit_write_standalone(
            conn, action="refused", lane=_LANE, operator=operator,
            expiry_utc=None, duration_hours=duration_hours,
            reason="duration out of bounds (0 < h <= %s)" % _MAX_DURATION_HOURS,
            created_at_utc=created)
        raise AuthorizationRefused(
            "duration must be > 0 and <= %s hours" % _MAX_DURATION_HOURS)

    expiry = _utc_iso(now + timedelta(hours=duration_hours))

    # Refuse if ibkr_live is genuinely enabled in the CURRENT policy (check the
    # raw loaded policy BEFORE normalisation, which would reset lanes). The D0
    # CLI manages paper only and will not act alongside a live authorization.
    raw = load_policy(conn)
    raw_live = raw.get("ibkr_live") if isinstance(raw.get("ibkr_live"), dict) else {}
    if raw_live.get("auto_trading_enabled") is True:
        _audit_write_standalone(
            conn, action="refused", lane=_LANE, operator=operator,
            expiry_utc=None, duration_hours=duration_hours,
            reason="ibkr_live is enabled; D0 CLI manages paper only",
            created_at_utc=created)
        raise AuthorizationRefused(
            "ibkr_live is enabled — the D0 CLI only manages ibkr_paper")

    # D0-safe normalisation (handles old pre-D0 rows; never copies legacy enable
    # into a lane, never enables ibkr_live).
    policy = _normalise_policy_for_d0(raw)

    policy["global"]["auto_trading_enabled"] = True
    policy["global"]["auto_trading_enabled_until_utc"] = expiry
    policy["ibkr_paper"]["auto_trading_enabled"] = True
    policy["ibkr_paper"]["auto_trading_enabled_until_utc"] = expiry
    # ibkr_live stays disabled (normalisation guarantees it); assert defensively.
    assert policy["ibkr_live"]["auto_trading_enabled"] is False

    # Validate BEFORE writing anything (rejects malformed/non-UTC expiry).
    vr = validate_policy(policy)
    if not vr.ok:
        raise ValueError("normalised policy invalid: %s" % vr.errors)

    # ATOMIC write: audit row + policy row in one transaction, single commit.
    audit_result = None
    try:
        _ensure_policy_table(conn)
        audit_result = _audit_insert_conn(
            conn, action="enable", lane=_LANE, operator=operator,
            expiry_utc=expiry, duration_hours=duration_hours,
            reason="operator enabled ibkr_paper (bounded window)",
            created_at_utc=created)
        conn.execute(
            "INSERT OR REPLACE INTO portfolio_risk_state (key, value, updated_at)"
            " VALUES (?, ?, ?)",
            (POLICY_KEY, json.dumps(policy, sort_keys=True), created))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return audit_result


def disable_ibkr_paper(conn: sqlite3.Connection, *, operator: str,
                       now: Optional[datetime] = None) -> Dict[str, Any]:
    """Immediately clear ibkr_paper authorization (and the global window). Safe
    anytime; no confirmation needed to REVOKE. Atomic policy+audit write."""
    now = now or _utc_now()
    created = _utc_iso(now)

    policy = _normalise_policy_for_d0(load_policy(conn))
    policy["global"]["auto_trading_enabled"] = False
    policy["global"]["auto_trading_enabled_until_utc"] = None
    policy["ibkr_paper"]["auto_trading_enabled"] = False
    policy["ibkr_paper"]["auto_trading_enabled_until_utc"] = None

    vr = validate_policy(policy)
    if not vr.ok:
        raise ValueError("normalised policy invalid: %s" % vr.errors)

    audit_result = None
    try:
        _ensure_policy_table(conn)
        audit_result = _audit_insert_conn(
            conn, action="disable", lane=_LANE, operator=operator,
            expiry_utc=None, duration_hours=None,
            reason="operator disabled ibkr_paper", created_at_utc=created)
        conn.execute(
            "INSERT OR REPLACE INTO portfolio_risk_state (key, value, updated_at)"
            " VALUES (?, ?, ?)",
            (POLICY_KEY, json.dumps(policy, sort_keys=True), created))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return audit_result


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

    di = sub.add_parser("disable", help="revoke ibkr_paper authorization")
    di.add_argument("--operator", required=True)
    di.add_argument("--db", default=None)

    st = sub.add_parser("status", help="show current authorization + audit tail")
    st.add_argument("--db", default=None)

    args = ap.parse_args()
    db = args.db or _signals_db_path()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        if args.cmd == "enable":
            try:
                res = enable_ibkr_paper(
                    conn, duration_hours=args.duration_hours,
                    operator=args.operator, confirm=args.confirm)
                print("ENABLED ibkr_paper until %s (audit_id=%s)"
                      % (res["expiry_utc"], res["audit_id"]))
            except AuthorizationRefused as e:
                print("REFUSED: %s" % e)
                raise SystemExit(2)
        elif args.cmd == "disable":
            res = disable_ibkr_paper(conn, operator=args.operator)
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
            tail = read_audit(conn)[-5:]
            print("audit tail (%d rows shown):" % len(tail))
            for r in tail:
                print("  %s %s lane=%s operator=%s expiry=%s"
                      % (r["created_at_utc"], r["action"], r["lane"],
                         r["operator"], r["expiry_utc"]))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
