"""dashboard/auth/audit_export.py — M15.3.C compliance audit + export.

Pure-logic primitives for the operator-initiated compliance export of
the M15.3 audit trail. Endpoint glue lives in dashboard/app.py.

What this module exports:
  * Date validation                — validate_date_range()
  * Row counting                   — count_export_rows()
  * Row reading                    — read_auth_events_range(),
                                     read_risk_decisions_manual_reset_range()
  * Body builders                  — build_jsonl_export(),
                                     build_csv_zip_export()
  * Redaction scanning             — scan_for_secrets()
  * Rate limiter factory           — make_export_limiter()
  * Constants                      — MAX_EXPORT_ROWS, EXPORT_SCHEMA_VERSION,
                                     ALLOWED_AUTH_EVENT_KINDS_AT_EXPORT_TIME

Design intent (M15.3.C pre-code checklist, approved 2026-06-04):
  Q-C.1 — narrow scope: auth_events (all kinds) + risk_decisions
          where source='manual_reset'. Nothing else.
  Q-C.2 — closed kind set built from the live ALLOWED_KINDS at runtime
          (not from a hard-coded list).
  Q-C.3 — both jsonl + csv-zip; default jsonl. SHA-256 of body
          requires spool-first (NOT pure streaming); operator-approved
          trade-off given the 100k row cap.
  Q-C.4 — UTC inclusive day windows; 100k row cap.
  Q-C.5 — fail-fast on secret-substring match (do NOT silent-strip).
  Q-C.6 — manifest includes schema version, export_id, dates,
          row_counts, sha256_payload; export_id links to audit row.
  Q-C.7 — read-only with respect to trading state; only audit-row write.
  Q-C.8 — GET endpoint, no CSRF, no step-up TOTP (read-only of
          already-visible data over HTTPS).
  Q-C.11 — no broker imports, no scanner/strategy imports, no engine
           imports. Stdlib only (json, csv, zipfile, hashlib, io, etc.)
           plus dashboard.auth.rate_limit (existing M15.3.A primitive).

This module imports NOTHING from bot.broker_*, bot.gateway_*,
bot.scanner, bot.strategy, bot.risk_authority.{engine,governor,
snapshot,preflight,ibkr_paper_reader}, bot.etoro.*, ib_insync, ibapi,
or any other broker / order-path code. AST-asserted in
test_m15_3_c_audit_export.TestNoBrokerImports.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

EXPORT_SCHEMA_VERSION = 1
MAX_EXPORT_ROWS = 100_000
SUPPORTED_FORMATS = ("jsonl", "csv")
DEFAULT_FORMAT = "jsonl"

# Rate-limit policy per Q-C.8: 10 exports / hour / IP. Cheaper than
# manual_reset (3/hour) because exports are non-destructive.
EXPORT_RATE_LIMIT_THRESHOLD = 10
EXPORT_RATE_LIMIT_WINDOW_SEC = 3600
EXPORT_RATE_LIMIT_LOCKOUT_SEC = 3600


def make_export_limiter():
    """Build a fresh RateLimiter for audit-export. Tests build their own."""
    from dashboard.auth.rate_limit import RateLimiter
    return RateLimiter(
        threshold=EXPORT_RATE_LIMIT_THRESHOLD,
        window_sec=EXPORT_RATE_LIMIT_WINDOW_SEC,
        lockout_sec=EXPORT_RATE_LIMIT_LOCKOUT_SEC,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Allowed kinds — read from live ALLOWED_KINDS at module load time
# ─────────────────────────────────────────────────────────────────────────────
#
# Per Q-C.2 correction: build the closed set from the actual
# dashboard.auth.audit.ALLOWED_KINDS, NOT a hard-coded list. This means
# if a future milestone adds a new kind to ALLOWED_KINDS, the export
# will automatically include it without an update here.
#
# We snapshot the set at module import time. The test asserts the
# snapshot matches what's currently in ALLOWED_KINDS (catches drift).

def _read_allowed_kinds_snapshot() -> frozenset:
    from dashboard.auth.audit import ALLOWED_KINDS
    return frozenset(ALLOWED_KINDS)


# Public alias used by the endpoint + tests.
ALLOWED_AUTH_EVENT_KINDS_AT_EXPORT_TIME = _read_allowed_kinds_snapshot()


# ─────────────────────────────────────────────────────────────────────────────
# Date range validation (Q-C.4)
# ─────────────────────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_date_range(
    from_str: Optional[str], to_str: Optional[str],
    *, now_utc: Optional[datetime] = None,
) -> Tuple[bool, str, Optional[str], Optional[str]]:
    """Validate operator-supplied from/to date strings.

    Returns (ok, error_code, from_iso, to_iso) where:
      * from_iso is the ISO-8601 timestamp at 00:00:00.000000 UTC of
        the from-date (inclusive lower bound).
      * to_iso is the ISO-8601 timestamp at 23:59:59.999999 UTC of the
        to-date (inclusive upper bound — operator-approved inclusive
        full-day window semantics).

    Defaults (when either is None or empty):
      * from = '1970-01-01' (earliest plausible)
      * to   = today UTC

    Validation:
      * Each MUST match ^\\d{4}-\\d{2}-\\d{2}$ if provided
      * Each MUST be a parseable real date (rejects 2026-02-30)
      * to_date MUST be >= from_date (else 'date_range_invalid')

    error_code is '' on success; otherwise one of:
      'date_format_invalid'  — malformed string
      'date_range_invalid'   — to < from
    """
    today = (now_utc or datetime.now(timezone.utc)).date()

    def _parse(s, *, default_date):
        if s is None or s == "":
            return default_date, ""
        if not isinstance(s, str) or not _DATE_RE.match(s):
            return None, "date_format_invalid"
        try:
            dt = datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None, "date_format_invalid"
        return dt, ""

    from datetime import date as _date
    from_date, err = _parse(from_str, default_date=_date(1970, 1, 1))
    if err:
        return False, err, None, None
    to_date, err = _parse(to_str, default_date=today)
    if err:
        return False, err, None, None

    if to_date < from_date:
        return False, "date_range_invalid", None, None

    from_iso = (datetime.combine(
        from_date, datetime.min.time(), tzinfo=timezone.utc).isoformat())
    to_iso = (datetime.combine(
        to_date, datetime.max.time(), tzinfo=timezone.utc).isoformat())
    return True, "", from_iso, to_iso


# ─────────────────────────────────────────────────────────────────────────────
# Row reading (Q-C.1 — strictly closed scope)
# ─────────────────────────────────────────────────────────────────────────────

def count_export_rows(
    conn: sqlite3.Connection, *, from_iso: str, to_iso: str,
) -> Tuple[int, int]:
    """Return (auth_events_count, risk_decisions_manual_reset_count).

    Used to enforce MAX_EXPORT_ROWS before allocating buffer space.
    """
    n_auth = conn.execute(
        "SELECT COUNT(*) FROM auth_events WHERE ts_utc >= ? AND ts_utc <= ?",
        (from_iso, to_iso),
    ).fetchone()[0]
    # risk_decisions doesn't have a ts_utc — use taken_at, with strict
    # source='manual_reset' filter per Q-C.1.
    n_rd = conn.execute(
        "SELECT COUNT(*) FROM risk_decisions "
        "WHERE source='manual_reset' AND taken_at >= ? AND taken_at <= ?",
        (from_iso, to_iso),
    ).fetchone()[0]
    return int(n_auth), int(n_rd)


def read_auth_events_range(
    conn: sqlite3.Connection, *, from_iso: str, to_iso: str,
) -> List[Dict[str, Any]]:
    """Read all auth_events rows in the date range, oldest first.

    Returns a list of dicts with keys: id, ts_utc, kind, client_ip,
    user_agent, session_id, success, extras_json (parsed back to a
    Python object if it was JSON; otherwise pass-through string).
    """
    cur = conn.execute(
        "SELECT id, ts_utc, kind, client_ip, user_agent, "
        "       session_id, success, extras_json "
        "FROM auth_events "
        "WHERE ts_utc >= ? AND ts_utc <= ? "
        "ORDER BY ts_utc ASC, id ASC",
        (from_iso, to_iso),
    )
    rows = []
    for r in cur.fetchall():
        extras_raw = r[7]
        extras_parsed: Any
        if extras_raw is None or extras_raw == "":
            extras_parsed = None
        else:
            try:
                extras_parsed = json.loads(extras_raw)
            except (json.JSONDecodeError, ValueError):
                # Defensive: keep as string if not valid JSON. Should
                # not occur given M15.3.A/.A.2/.B schema discipline.
                extras_parsed = extras_raw
        rows.append({
            "id": int(r[0]),
            "ts_utc": r[1],
            "kind": r[2],
            "client_ip": r[3],
            "user_agent": r[4],
            "session_id_hash": r[5],  # renamed for clarity in export
            "success": int(r[6]),
            "extras": extras_parsed,
        })
    return rows


def read_risk_decisions_manual_reset_range(
    conn: sqlite3.Connection, *, from_iso: str, to_iso: str,
) -> List[Dict[str, Any]]:
    """Read all risk_decisions rows with source='manual_reset' in range."""
    cur = conn.execute(
        "SELECT decision_id, taken_at, broker_scope, requested_action, "
        "       request_json, result, authority_before, authority_after, "
        "       reason_codes, recovery_paths, snapshot_id, source, "
        "       actor, explainer, created_at "
        "FROM risk_decisions "
        "WHERE source='manual_reset' "
        "  AND taken_at >= ? AND taken_at <= ? "
        "ORDER BY taken_at ASC, decision_id ASC",
        (from_iso, to_iso),
    )
    rows = []
    for r in cur.fetchall():
        def _parse_json(s):
            if s is None or s == "":
                return None
            try:
                return json.loads(s)
            except (json.JSONDecodeError, ValueError):
                return s
        rows.append({
            "decision_id":      r[0],
            "taken_at":         r[1],
            "broker_scope":     r[2],
            "requested_action": r[3],
            "request":          _parse_json(r[4]),
            "result":           r[5],
            "authority_before": r[6],
            "authority_after":  r[7],
            "reason_codes":     _parse_json(r[8]),
            "recovery_paths":   _parse_json(r[9]),
            "snapshot_id":      r[10],
            "source":           r[11],
            "actor":            r[12],
            "explainer":        r[13],
            "created_at":       r[14],
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Redaction (Q-C.5 — fail-fast, do NOT silent-strip)
# ─────────────────────────────────────────────────────────────────────────────
#
# Read these env vars AT EXPORT TIME and scan the body bytes for each
# non-empty value. If ANY match: fail the export with redaction_violation.
#
# The audit invariants in M15.3.A.2 / M15.3.B already guarantee these
# never appear in audit rows. So a match here means either:
#   * a bug in audit-row writing (defence-in-depth catches it)
#   * an env var with a very short value (false positive — we filter
#     candidates by minimum length to avoid that)

_SECRET_ENV_KEYS = (
    "DASHBOARD_TOTP_SECRET",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_PASSWORD_HASH",
    "DASHBOARD_SECRET_KEY",
    "IBKR_API_KEY",
    "IBKR_PASSWORD",
    "ETORO_API_KEY",
    "ETORO_USER_KEY",
    "ETORO_PASSWORD",
    "TELEGRAM_BOT_TOKEN",
)

# Additional literal substrings that should never appear in audit rows.
_SECRET_LITERAL_SUBSTRINGS = (
    "otpauth://",
    "-----BEGIN ",  # PEM key headers
)

# Minimum candidate length — env values shorter than this are skipped
# to avoid false positives on common short strings (e.g. a password
# of "abc" would false-positive on every legitimate row containing
# the substring "abc"). High-entropy real secrets are always longer.
_SECRET_MIN_LENGTH = 12


def _collect_secret_candidates(
    *, env_overrides: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Collect non-empty, sufficiently-long secret candidate strings.

    env_overrides is for tests — when None, reads os.environ.
    Returns a list of substrings to scan against the export body.
    """
    src = env_overrides if env_overrides is not None else os.environ
    out: List[str] = []
    for k in _SECRET_ENV_KEYS:
        v = src.get(k)
        if v and len(v) >= _SECRET_MIN_LENGTH:
            out.append(v)
    out.extend(_SECRET_LITERAL_SUBSTRINGS)
    return out


def scan_for_secrets(
    payload: bytes, *,
    env_overrides: Optional[Dict[str, str]] = None,
) -> Tuple[bool, List[str]]:
    """Scan export bytes for any known secret substring.

    Returns (clean, violation_labels). `clean` is True if no secret
    was found. `violation_labels` is a list of GENERIC labels
    identifying which secret class was matched — NEVER the actual
    secret value, NEVER the env-var value, NEVER any byte from the
    matched string. Suitable for inclusion in audit extras.

    The labels follow the env-key naming (e.g. 'DASHBOARD_TOTP_SECRET',
    'otpauth_uri'); they identify the CLASS of secret that leaked,
    not its content.
    """
    src = env_overrides if env_overrides is not None else os.environ
    violations: List[str] = []

    # Env-keyed secrets
    for k in _SECRET_ENV_KEYS:
        v = src.get(k)
        if v and len(v) >= _SECRET_MIN_LENGTH:
            if v.encode("utf-8") in payload:
                violations.append(k)

    # Literal substrings
    literal_label_map = {
        "otpauth://": "otpauth_uri",
        "-----BEGIN ": "pem_key_header",
    }
    for literal in _SECRET_LITERAL_SUBSTRINGS:
        if literal.encode("utf-8") in payload:
            violations.append(literal_label_map.get(literal, "literal_secret"))

    return (len(violations) == 0, sorted(set(violations)))


# ─────────────────────────────────────────────────────────────────────────────
# Manifest construction (Q-C.6)
# ─────────────────────────────────────────────────────────────────────────────

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_export_id() -> str:
    return f"exp-{uuid.uuid4().hex[:16]}"


def build_manifest(
    *, export_id: str, format_name: str,
    from_iso: str, to_iso: str,
    row_counts: Dict[str, int],
    sha256_payload: str,
    generated_at_utc: Optional[str] = None,
    generated_by_actor: str = "operator",
) -> Dict[str, Any]:
    """Build the manifest dict. All keys begin with `_` so they don't
    collide with audit-row field names in the JSONL stream."""
    return {
        "_schema_version":     EXPORT_SCHEMA_VERSION,
        "_export_id":          export_id,
        "_generated_at_utc":   generated_at_utc or _now_utc_iso(),
        "_generated_by_actor": generated_by_actor,
        "_date_range":         {"from_iso": from_iso, "to_iso": to_iso},
        "_row_counts":         dict(row_counts),
        "_sha256_payload":     sha256_payload,
        "_format":             format_name,
    }


# ─────────────────────────────────────────────────────────────────────────────
# JSONL body builder (Q-C.3)
# ─────────────────────────────────────────────────────────────────────────────

def build_jsonl_export(
    conn: sqlite3.Connection, *,
    from_iso: str, to_iso: str,
    export_id: Optional[str] = None,
    generated_at_utc: Optional[str] = None,
) -> Tuple[bytes, Dict[str, Any]]:
    """Build the full JSONL export (manifest + body) as bytes.

    Per Q-C.3 honest correction: this is NOT streaming. We spool the
    body in memory, compute SHA-256, then prepend the manifest. With
    the 100k row cap, peak memory is bounded (~50-100 MB worst case).

    Returns (full_bytes, manifest_dict). full_bytes is what the
    endpoint writes to the response.

    Layout:
      line 1   manifest (JSON object, single line)
      line 2+  audit rows (one JSON object per line)
    """
    auth_rows = read_auth_events_range(conn, from_iso=from_iso, to_iso=to_iso)
    rd_rows   = read_risk_decisions_manual_reset_range(
                  conn, from_iso=from_iso, to_iso=to_iso)

    body_buf = io.BytesIO()
    for r in auth_rows:
        line = json.dumps({"_source": "auth_events", **r},
                            separators=(",", ":"), sort_keys=True,
                            ensure_ascii=False, default=_json_default)
        body_buf.write(line.encode("utf-8"))
        body_buf.write(b"\n")
    for r in rd_rows:
        line = json.dumps({"_source": "risk_decisions_manual_reset", **r},
                            separators=(",", ":"), sort_keys=True,
                            ensure_ascii=False, default=_json_default)
        body_buf.write(line.encode("utf-8"))
        body_buf.write(b"\n")

    body_bytes = body_buf.getvalue()
    sha = hashlib.sha256(body_bytes).hexdigest()

    manifest = build_manifest(
        export_id=export_id or _new_export_id(),
        format_name="jsonl",
        from_iso=from_iso, to_iso=to_iso,
        row_counts={
            "auth_events":                  len(auth_rows),
            "risk_decisions_manual_reset":  len(rd_rows),
        },
        sha256_payload=sha,
        generated_at_utc=generated_at_utc,
    )
    manifest_line = json.dumps(manifest, separators=(",", ":"),
                                sort_keys=True, ensure_ascii=False).encode(
                                  "utf-8") + b"\n"
    return manifest_line + body_bytes, manifest


def _json_default(o):
    """Fallback JSON encoder for objects json doesn't know.

    We expect to never hit this in practice — all audit fields are
    SQLite TEXT/INTEGER. Defensive fallback returns repr() so the
    export never crashes; the test suite asserts a clean export uses
    no `default=` callbacks for known data shapes."""
    return repr(o)


# ─────────────────────────────────────────────────────────────────────────────
# CSV-in-ZIP body builder (Q-C.3)
# ─────────────────────────────────────────────────────────────────────────────

_CSV_AUTH_EVENTS_COLUMNS = (
    "id", "ts_utc", "kind", "client_ip", "user_agent",
    "session_id_hash", "success", "extras_json",
)
_CSV_RISK_DECISIONS_COLUMNS = (
    "decision_id", "taken_at", "broker_scope", "requested_action",
    "request_json", "result", "authority_before", "authority_after",
    "reason_codes_json", "recovery_paths_json", "snapshot_id",
    "source", "actor", "explainer", "created_at",
)


def _row_to_csv_auth_events(r: Dict[str, Any]) -> List[str]:
    extras = r.get("extras")
    if extras is None:
        extras_str = ""
    else:
        extras_str = json.dumps(extras, separators=(",", ":"),
                                  sort_keys=True, ensure_ascii=False)
    return [
        str(r["id"]),
        r.get("ts_utc") or "",
        r.get("kind") or "",
        r.get("client_ip") or "",
        r.get("user_agent") or "",
        r.get("session_id_hash") or "",
        str(r.get("success", 0)),
        extras_str,
    ]


def _row_to_csv_risk_decisions(r: Dict[str, Any]) -> List[str]:
    def _jdump(o):
        if o is None:
            return ""
        if isinstance(o, str):
            return o
        return json.dumps(o, separators=(",", ":"),
                            sort_keys=True, ensure_ascii=False)
    return [
        r.get("decision_id") or "",
        r.get("taken_at") or "",
        r.get("broker_scope") or "",
        r.get("requested_action") or "",
        _jdump(r.get("request")),
        r.get("result") or "",
        r.get("authority_before") or "",
        r.get("authority_after") or "",
        _jdump(r.get("reason_codes")),
        _jdump(r.get("recovery_paths")),
        "" if r.get("snapshot_id") is None else str(r.get("snapshot_id")),
        r.get("source") or "",
        r.get("actor") or "",
        r.get("explainer") or "",
        r.get("created_at") or "",
    ]


def build_csv_zip_export(
    conn: sqlite3.Connection, *,
    from_iso: str, to_iso: str,
    export_id: Optional[str] = None,
    generated_at_utc: Optional[str] = None,
) -> Tuple[bytes, Dict[str, Any]]:
    """Build the full CSV-in-ZIP export as bytes.

    The ZIP contains:
      * manifest.txt                       — manifest header in
                                              human-readable form
      * auth_events.csv                    — RFC-4180-quoted
      * risk_decisions_manual_reset.csv    — RFC-4180-quoted

    Returns (zip_bytes, manifest_dict).

    The SHA-256 hash covers the two CSV bodies concatenated (auth
    first, then risk_decisions), NOT manifest.txt — the manifest is
    metadata about the body, not part of the body.
    """
    auth_rows = read_auth_events_range(conn, from_iso=from_iso, to_iso=to_iso)
    rd_rows   = read_risk_decisions_manual_reset_range(
                  conn, from_iso=from_iso, to_iso=to_iso)

    # Build the auth CSV
    auth_buf = io.StringIO()
    auth_writer = csv.writer(auth_buf, quoting=csv.QUOTE_MINIMAL,
                                lineterminator="\n")
    auth_writer.writerow(_CSV_AUTH_EVENTS_COLUMNS)
    for r in auth_rows:
        auth_writer.writerow(_row_to_csv_auth_events(r))
    auth_bytes = auth_buf.getvalue().encode("utf-8")

    # Build the risk_decisions CSV
    rd_buf = io.StringIO()
    rd_writer = csv.writer(rd_buf, quoting=csv.QUOTE_MINIMAL,
                              lineterminator="\n")
    rd_writer.writerow(_CSV_RISK_DECISIONS_COLUMNS)
    for r in rd_rows:
        rd_writer.writerow(_row_to_csv_risk_decisions(r))
    rd_bytes = rd_buf.getvalue().encode("utf-8")

    # SHA-256 of (auth_bytes || rd_bytes)
    h = hashlib.sha256()
    h.update(auth_bytes)
    h.update(rd_bytes)
    sha = h.hexdigest()

    manifest = build_manifest(
        export_id=export_id or _new_export_id(),
        format_name="csv",
        from_iso=from_iso, to_iso=to_iso,
        row_counts={
            "auth_events":                  len(auth_rows),
            "risk_decisions_manual_reset":  len(rd_rows),
        },
        sha256_payload=sha,
        generated_at_utc=generated_at_utc,
    )

    # Build manifest.txt — line-per-field, human-readable.
    manifest_lines = []
    for k in ("_schema_version", "_export_id", "_generated_at_utc",
               "_generated_by_actor", "_format", "_sha256_payload"):
        manifest_lines.append(f"{k}: {manifest[k]}")
    manifest_lines.append(
        f"_date_range.from_iso: {manifest['_date_range']['from_iso']}")
    manifest_lines.append(
        f"_date_range.to_iso: {manifest['_date_range']['to_iso']}")
    for tbl, n in manifest["_row_counts"].items():
        manifest_lines.append(f"_row_counts.{tbl}: {n}")
    manifest_lines.append(
        "_files: manifest.txt, auth_events.csv, "
        "risk_decisions_manual_reset.csv")
    manifest_lines.append(
        "_sha256_scope: sha256 covers concat(auth_events.csv, "
        "risk_decisions_manual_reset.csv), NOT manifest.txt")
    manifest_txt = ("\n".join(manifest_lines) + "\n").encode("utf-8")

    # Pack ZIP. ZIP_DEFLATED for compactness.
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.txt", manifest_txt)
        z.writestr("auth_events.csv", auth_bytes)
        z.writestr("risk_decisions_manual_reset.csv", rd_bytes)
    return zip_buf.getvalue(), manifest


# ─────────────────────────────────────────────────────────────────────────────
# Filename construction (Q-C.5 — no secrets in filenames)
# ─────────────────────────────────────────────────────────────────────────────

def make_download_filename(format_name: str,
                             generated_at_utc: Optional[str] = None) -> str:
    """Return a Content-Disposition-safe filename.

    No secrets. No client-controllable strings. Only:
      audit_export_<YYYYMMDDTHHMMSSZ>.<ext>
    """
    ts = datetime.fromisoformat(
        (generated_at_utc or _now_utc_iso()).replace("Z", "+00:00")
    ).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if format_name == "jsonl":
        return f"audit_export_{ts}.jsonl"
    if format_name == "csv":
        return f"audit_export_{ts}.zip"
    # Defensive — caller should have validated already.
    raise ValueError(f"unsupported format {format_name!r}")
