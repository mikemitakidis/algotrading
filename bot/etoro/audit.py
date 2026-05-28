"""
bot/etoro/audit.py — M13.5.B redaction + audit logger.

Hard requirement (M13.4B §12, M13.5.A §2.2):
  - x-api-key / x-user-key NEVER appear in any log, audit record,
    Telegram payload, or lifecycle_json snapshot.
  - The functions in this module are the SOLE redaction primitives
    for the live writer + reconciliation tool.

The audit log is a rotating JSONL file at <BASE_DIR>/data/etoro_audit.log
(rotation handled lazily — size-bounded with a stem rename).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Headers that must NEVER appear in any audit record.
SENSITIVE_HEADERS = {
    "x-api-key",
    "x-user-key",
    "authorization",
    "cookie",
    "set-cookie",
}

# Response body fields that should be partly redacted.
# (Full account IDs are truncated to last 4 chars; tokens are masked.)
TRUNCATE_FIELDS = {
    "cid",          # customer id — keep last 4 only when emitting audit
    "CID",
    "GCID",
    "userId",
}
MASK_FIELDS = {
    "token",        # eToro tracking token in responses
    "x-request-id", # request id we sent — fine to log truncated, not full
}

_PAT_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE)


def _mask_string(s: str) -> str:
    """Best-effort scrub of an arbitrary text fragment."""
    if not isinstance(s, str):
        return s
    return _PAT_BEARER.sub("Bearer <REDACTED>", s)


def _truncate_id(value: Any) -> str:
    """Show only the last 4 chars of a long identifier; '<REDACTED>' if short."""
    s = str(value) if value is not None else ""
    if len(s) <= 4:
        return "<REDACTED>"
    return "***" + s[-4:]


def _mask_token(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    if len(s) <= 8:
        return "<REDACTED>"
    return s[:4] + "..." + s[-4:]


def redact_headers(headers: Optional[dict]) -> dict:
    """Return a copy of headers with sensitive keys removed/masked."""
    if not isinstance(headers, dict):
        return {}
    out = {}
    for k, v in headers.items():
        if not isinstance(k, str):
            continue
        kl = k.lower()
        if kl in SENSITIVE_HEADERS:
            out[k] = "<REDACTED>"
        elif kl == "x-request-id":
            out[k] = _mask_token(v)
        else:
            out[k] = v
    return out


def redact_body(body: Any) -> Any:
    """Recursively redact a JSON-shaped value (dict/list/str/num/bool/None).

    - Truncates known ID fields to last 4 chars.
    - Masks `token` fields.
    - Scrubs `Bearer ...` substrings in any string.
    - Never logs the raw `x-api-key` / `x-user-key` if they somehow
      ended up in the body.
    """
    if isinstance(body, dict):
        out = {}
        for k, v in body.items():
            if not isinstance(k, str):
                out[k] = redact_body(v)
                continue
            if k in TRUNCATE_FIELDS:
                out[k] = _truncate_id(v)
            elif k in MASK_FIELDS:
                out[k] = _mask_token(v)
            elif k.lower() in SENSITIVE_HEADERS:
                out[k] = "<REDACTED>"
            else:
                out[k] = redact_body(v)
        return out
    if isinstance(body, list):
        return [redact_body(item) for item in body]
    if isinstance(body, str):
        return _mask_string(body)
    return body


def redact_payload(payload: dict) -> dict:
    """Public helper used by callers to redact a payload before logging.

    Returns a deep-copied, redacted version. Original is not mutated."""
    return redact_body(copy.deepcopy(payload))


# ─────────────────────────────────────────────────────────────────────────────
# Rotating JSONL audit writer
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_MAX_BYTES = 5 * 1024 * 1024     # 5 MB per file
DEFAULT_KEEP_FILES = 5                  # rotate to .1, .2, ...


def _rotate_if_needed(path: Path, max_bytes: int, keep: int) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size < max_bytes:
        return
    # Shift .{n-1} -> .{n}, dropping the oldest.
    for i in range(keep - 1, 0, -1):
        src = path.with_suffix(path.suffix + f".{i}")
        dst = path.with_suffix(path.suffix + f".{i+1}")
        if src.exists():
            try:
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
            except OSError as e:
                log.warning("[audit] rotate: %s -> %s failed: %s", src, dst, e)
    # Move current to .1
    dst1 = path.with_suffix(path.suffix + ".1")
    try:
        if dst1.exists():
            dst1.unlink()
        path.rename(dst1)
    except OSError as e:
        log.warning("[audit] rotate current failed: %s", e)


class AuditLogger:
    """JSONL audit writer with redaction. One instance per CLI process.

    All `event(...)` records are redacted before serialisation; callers
    do not need to pre-redact.
    """

    def __init__(self, path: Path,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 keep: int = DEFAULT_KEEP_FILES):
        self.path = Path(path)
        # Never raise on construction — a bad audit path must not crash
        # the live writer. mkdir failures are logged and tolerated; the
        # subsequent event() write will also tolerate failure.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error("[audit] could not create log dir %s: %s",
                      self.path.parent, e)
        self.max_bytes = max_bytes
        self.keep = keep

    def event(self, kind: str, **fields: Any) -> None:
        """Write a single audit record. Never raises on I/O failure."""
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "kind": str(kind),
        }
        # Apply redaction to every field except the kind/ts already set.
        for k, v in fields.items():
            record[k] = redact_body(v)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        try:
            _rotate_if_needed(self.path, self.max_bytes, self.keep)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as e:
            # Never fail the live writer because the audit log can't be written.
            log.error("[audit] write failed: %s", e)


__all__ = [
    "SENSITIVE_HEADERS",
    "TRUNCATE_FIELDS",
    "MASK_FIELDS",
    "redact_headers",
    "redact_body",
    "redact_payload",
    "AuditLogger",
]
