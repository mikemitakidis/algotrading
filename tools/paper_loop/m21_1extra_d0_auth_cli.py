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
  - persists via a direct atomic write (validated by validate_policy, which
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
    load_policy, validate_policy, DEFAULT_POLICY, POLICY_KEY,
    POLICY_VERSION, _ensure_table as _ensure_policy_table,
)

# Hard cap on how long an operator can authorize in one action (bounded window).
_MAX_DURATION_HOURS = 8.0
_LANE = "ibkr_paper"
_AUDIT_TABLE = "auto_trading_authorizations"


def _ensure_d0_fields(policy: Any) -> Dict[str, Any]:
    """Return a copy of `policy` with the D0 SCHEMA fields guaranteed to exist,
    WITHOUT modifying any existing value. This is add-missing-only: it never
    changes an existing enable/kill/expiry/eToro/routing value. It exists so an
    old pre-D0 signals.db row (missing ibkr_paper / ibkr_live /
    global.auto_trading_enabled_until_utc) can be mutated + validated safely.

    The enable/disable paths are responsible for refusing hostile states and for
    setting ONLY their target fields — normalisation here does not force any
    non-target flag, so nothing else is ever silently rewritten.
    """
    if not isinstance(policy, dict):
        return json.loads(json.dumps(DEFAULT_POLICY))
    p = json.loads(json.dumps(policy))  # deep copy, preserve everything

    p.setdefault("version", POLICY_VERSION)

    g = p.get("global")
    if not isinstance(g, dict):
        g = {}
        p["global"] = g
    g.setdefault("auto_trading_enabled", False)
    g.setdefault("auto_trading_enabled_until_utc", None)
    g.setdefault("max_auto_trading_capital", 0.0)
    g.setdefault("kill_switch", False)

    # lean D0 lane blocks — add if missing, all-disabled; never alter if present.
    for lane in ("ibkr_paper", "ibkr_live"):
        b = p.get(lane)
        if not isinstance(b, dict):
            b = {}
            p[lane] = b
        b.setdefault("auto_trading_enabled", False)
        b.setdefault("auto_trading_enabled_until_utc", None)
        b.setdefault("kill_switch", False)

    # legacy ibkr / etoro / routing: ensure the block+required keys EXIST so the
    # strict validator passes, but preserve every existing value untouched.
    for name in ("ibkr", "etoro"):
        b = p.get(name)
        if not isinstance(b, dict):
            b = {}
            p[name] = b
        d = DEFAULT_POLICY[name]
        for k, v in d.items():
            b.setdefault(k, v)

    r = p.get("routing")
    if not isinstance(r, dict):
        r = {}
        p["routing"] = r
    for k, v in DEFAULT_POLICY["routing"].items():
        r.setdefault(k, v)

    return p


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

    # D0 authorizes ibkr_paper ONLY. Before turning global auto-trading on,
    # REFUSE on any hostile/non-target state in the CURRENT (raw) policy, because
    # writing global=True + ibkr_paper=True could otherwise (a) be silently
    # blocked by a kill switch → fake ENABLED, or (b) activate a pre-existing
    # non-target lane. Refuse rather than silently modify anything.
    raw = load_policy(conn)

    def _flag(block, key):
        d = raw.get(block) if isinstance(raw.get(block), dict) else {}
        return d.get(key)

    refusals = []
    # (Blocker 1) kill switches would make a successful-looking enable a lie —
    # the reader would still block. Refuse; never silently clear a kill switch.
    if _flag("global", "kill_switch") is True:
        refusals.append("global.kill_switch")
    if _flag("ibkr_paper", "kill_switch") is True:
        refusals.append("ibkr_paper.kill_switch")
    # non-target authorizations that global=True could activate.
    if _flag("ibkr_live", "auto_trading_enabled") is True:
        refusals.append("ibkr_live.auto_trading_enabled")
    if _flag("etoro", "auto_trading_enabled") is True:
        refusals.append("etoro.auto_trading_enabled")
    if _flag("routing", "etoro_live_enabled") is True:
        refusals.append("routing.etoro_live_enabled")

    if refusals:
        reason = ("cannot enable ibkr_paper — hostile/non-target state active: "
                  "%s; resolve these first (D0 authorizes ibkr_paper only and "
                  "never modifies other lanes)" % ", ".join(refusals))
        _audit_write_standalone(
            conn, action="refused", lane=_LANE, operator=operator,
            expiry_utc=None, duration_hours=duration_hours,
            reason=reason, created_at_utc=created)
        raise AuthorizationRefused(reason)

    # Ensure D0 schema fields exist WITHOUT modifying any existing value.
    policy = _ensure_d0_fields(raw)

    # Set ONLY the target fields.
    policy["global"]["auto_trading_enabled"] = True
    policy["global"]["auto_trading_enabled_until_utc"] = expiry
    policy["ibkr_paper"]["auto_trading_enabled"] = True
    policy["ibkr_paper"]["auto_trading_enabled_until_utc"] = expiry

    # Guarantee ibkr_paper is actually allowed by the reader (no fake ENABLED),
    # keeping routing internally valid. This only ADDS ibkr_paper to
    # allowed_brokers; it does not enable/disable any lane.
    routing = policy["routing"]
    allowed = routing.get("allowed_brokers")
    if not isinstance(allowed, list):
        allowed = []
    if "ibkr_paper" not in allowed:
        allowed = list(allowed) + ["ibkr_paper"]
    routing["allowed_brokers"] = allowed
    # keep route_overrides / default_broker consistent so validation passes,
    # without touching any authorization flag.
    ov = routing.get("route_overrides")
    if isinstance(ov, dict):
        routing["route_overrides"] = {k: v for k, v in ov.items()
                                      if v in allowed}
    else:
        routing["route_overrides"] = {}
    if routing.get("default_broker") not in allowed:
        if "paper" not in allowed:
            allowed = list(allowed) + ["paper"]
            routing["allowed_brokers"] = allowed
        routing["default_broker"] = "paper"

    # (Blocker 3) explicit runtime safety checks — NOT asserts (assert is
    # stripped under python -O). Any violation refuses before writing.
    _guard = []
    if policy["ibkr_live"].get("auto_trading_enabled") is True:
        _guard.append("ibkr_live.auto_trading_enabled")
    if policy["etoro"].get("auto_trading_enabled") is True:
        _guard.append("etoro.auto_trading_enabled")
    if policy["routing"].get("etoro_live_enabled") is True:
        _guard.append("routing.etoro_live_enabled")
    if "ibkr_paper" not in policy["routing"].get("allowed_brokers", []):
        _guard.append("ibkr_paper not in allowed_brokers")
    if policy["global"].get("kill_switch") is True:
        _guard.append("global.kill_switch")
    if policy["ibkr_paper"].get("kill_switch") is True:
        _guard.append("ibkr_paper.kill_switch")
    if _guard:
        reason = ("internal safety guard tripped before write: %s"
                  % ", ".join(_guard))
        _audit_write_standalone(
            conn, action="refused", lane=_LANE, operator=operator,
            expiry_utc=None, duration_hours=duration_hours,
            reason=reason, created_at_utc=created)
        raise AuthorizationRefused(reason)

    # Validate BEFORE writing anything (rejects malformed/non-UTC expiry, or a
    # malformed non-target field e.g. a non-boolean kill_switch in an old row).
    vr = validate_policy(policy)
    if not vr.ok:
        _audit_write_standalone(
            conn, action="refused", lane=_LANE, operator=operator,
            expiry_utc=None, duration_hours=duration_hours,
            reason="enable refused: resulting policy invalid (will not write): "
                   "%s" % vr.errors, created_at_utc=created)
        raise AuthorizationRefused(
            "cannot enable: resulting policy invalid (%s)" % vr.errors)

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
    """Immediately clear D0's ibkr_paper authorization (and the global window).
    Safe anytime; no confirmation needed to REVOKE. Atomic policy+audit write.

    D0 disable clears ONLY its four target fields and PRESERVES every non-target
    flag (etoro.auto_trading_enabled, routing.etoro_live_enabled,
    ibkr_live.auto_trading_enabled, kill switches, capital/limits). It only ADDS
    missing D0 schema fields (for old DBs). If the resulting policy cannot be
    validated, it refuses rather than silently rewriting non-target state."""
    now = now or _utc_now()
    created = _utc_iso(now)

    # add-missing-only: never modifies an existing non-target value.
    policy = _ensure_d0_fields(load_policy(conn))
    # clear ONLY the four D0 target fields.
    policy["global"]["auto_trading_enabled"] = False
    policy["global"]["auto_trading_enabled_until_utc"] = None
    policy["ibkr_paper"]["auto_trading_enabled"] = False
    policy["ibkr_paper"]["auto_trading_enabled_until_utc"] = None

    vr = validate_policy(policy)
    if not vr.ok:
        # do NOT silently rewrite non-target fields to force validity.
        _audit_write_standalone(
            conn, action="refused", lane=_LANE, operator=operator,
            expiry_utc=None, duration_hours=None,
            reason="disable refused: policy invalid and cannot be revoked "
                   "without rewriting non-target fields: %s" % vr.errors,
            created_at_utc=created)
        raise AuthorizationRefused(
            "cannot safely disable: policy invalid (%s)" % vr.errors)

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
