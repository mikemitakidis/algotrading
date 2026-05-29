"""
bot/risk_authority/ingest_audit.py — M14.C redacted ingestion audit log.

Reuses the redaction primitives from bot/etoro/audit.py and writes a
rotating JSONL file at `<repo>/data/risk_ingest.log`. Per ChatGPT
M14.C correction #5:
  * compact redacted summaries go into DB `lifecycle_json.latest_reading`
    (handled by the orchestrator),
  * this file carries richer (still redacted) entries for history,
  * no secrets, no full broker tokens, never raises on I/O failure.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from bot.etoro.audit import AuditLogger


_DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "risk_ingest.log"


def get_ingest_audit_logger(path: Optional[Path] = None) -> AuditLogger:
    """Return a process-local AuditLogger for risk-ingestion events.

    Identical redaction guarantees to the M13.5.B live-broker audit
    logger (api/user keys redacted, Bearer scrubbed, IDs truncated,
    never raises on I/O failure)."""
    return AuditLogger(path or _DEFAULT_PATH)


__all__ = ["get_ingest_audit_logger"]
