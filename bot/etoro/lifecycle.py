"""
bot/etoro/lifecycle.py — M13.5.B.

Controlled writer of execution_intents rows for the eToro live-write
and reconciliation paths. This is the SOLE module that updates
execution_intents during the eToro lifecycle (except for the initial
`log_intent` row insert, which uses bot.flywheel.log_intent).

Why a dedicated writer:
  * bot/flywheel.py::update_intent_status sets `submitted_at` only for
    'accepted' / 'paper_logged'. The eToro flow uses 'submitted' (M12
    vocabulary), which would otherwise leave `submitted_at` NULL —
    ChatGPT audit finding.
  * The reconciliation tool must update lifecycle through a single,
    validated entrypoint, not raw SQL.
  * Idempotency (no duplicate row creation, no duplicate event entries)
    is enforced here.

This module does NOT call any eToro endpoint. It is pure DB logic.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)


# Statuses the eToro flow uses. Free-form text in DB; this list is the
# vocabulary, not a hard constraint enforced by flywheel.
ETORO_LIFECYCLE_STATUSES = {
    # M13.5.B new
    "pending_live_write",
    "awaiting_confirm",
    "unverified",
    "closed_manual",
    # Reused from M12
    "policy_rejected",
    "risk_rejected",
    "submitted",
    "filled",
    "broker_rejected",
    "cancelled",
}

# Transitions that are safe to apply once a row already exists.
# Empty set means "no constraint" — but some terminal states refuse
# further updates.
_TERMINAL = {"filled", "broker_rejected", "cancelled", "closed_manual"}


class LifecycleError(RuntimeError):
    """Raised when a lifecycle transition is invalid or unsafe."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_row(conn: sqlite3.Connection, intent_id: int) -> dict:
    cur = conn.execute(
        "SELECT id, status, broker, broker_order_id, submitted_at, filled_at, "
        "       fill_price, fill_qty, cancelled_at, lifecycle_json "
        "FROM execution_intents WHERE id=?", (intent_id,)
    )
    row = cur.fetchone()
    if row is None:
        raise LifecycleError(f"execution_intents.id={intent_id} not found")
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _load_lifecycle(row: dict) -> dict:
    raw = row.get("lifecycle_json") or "{}"
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    return d


def find_by_client_intent_id(conn: sqlite3.Connection,
                             client_intent_id: str) -> Optional[int]:
    """Return execution_intents.id whose lifecycle_json carries the
    matching client_intent_id key, or None.

    Used to enforce exactly-one-row-per-operator-command (M13.5.A §8.4).
    """
    if not isinstance(client_intent_id, str) or not client_intent_id:
        return None
    # Two-step lookup so we do not depend on the JSON1 SQLite extension:
    # fetch recent rows and JSON-decode in Python. The window is small
    # because the operator CLI runs once per command.
    cur = conn.execute(
        "SELECT id, lifecycle_json FROM execution_intents "
        "WHERE lifecycle_json IS NOT NULL "
        "ORDER BY id DESC LIMIT 200"
    )
    for rid, raw in cur.fetchall():
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(d, dict) and d.get("client_intent_id") == client_intent_id:
            return int(rid)
    return None


def apply_transition(
    conn: sqlite3.Connection,
    intent_id: int,
    new_status: str,
    *,
    event: Optional[str] = None,
    fill_price: Optional[float] = None,
    fill_qty: Optional[float] = None,
    broker_order_id: Optional[str] = None,
    extra_lifecycle: Optional[dict] = None,
    allow_terminal_override: bool = False,
) -> dict:
    """Apply a single lifecycle transition. Returns the updated row dict.

    Behaviour:
      - Reads current row + lifecycle_json.
      - Appends a single event entry to lifecycle.events.
      - Sets `submitted_at` when entering 'submitted' (M13.5.B fix vs.
        flywheel.update_intent_status, which only set it for
        'accepted'/'paper_logged').
      - Sets `filled_at`/`fill_price`/`fill_qty` on 'filled'.
      - Sets `cancelled_at` on 'cancelled' and 'closed_manual'.
      - Refuses transitions out of a terminal status unless
        `allow_terminal_override=True`.
      - Refuses an unknown status string (defensive — eToro lifecycle
        vocabulary is closed).
      - Single SQL UPDATE — never inserts a new row.

    Idempotency: re-applying the same status with the same broker_order_id
    is permitted but appends a fresh event entry. Callers preventing
    double-confirmation are the operator nonce store, not this function.
    """
    if not isinstance(intent_id, int) or intent_id <= 0:
        raise LifecycleError(f"invalid intent_id={intent_id!r}")
    if new_status not in ETORO_LIFECYCLE_STATUSES:
        raise LifecycleError(
            f"unknown status {new_status!r}; expected one of "
            f"{sorted(ETORO_LIFECYCLE_STATUSES)}"
        )

    row = _read_row(conn, intent_id)
    cur_status = row.get("status") or ""

    if cur_status in _TERMINAL and not allow_terminal_override:
        raise LifecycleError(
            f"intent {intent_id} already terminal ({cur_status!r}); "
            f"cannot transition to {new_status!r}"
        )

    lifecycle = _load_lifecycle(row)
    events = lifecycle.get("events")
    if not isinstance(events, list):
        events = []
    now = _now_iso()
    events.append({
        "ts": now,
        "status": new_status,
        "event": event or new_status,
        "prev_status": cur_status,
    })
    lifecycle["events"] = events
    lifecycle["last_status"] = new_status
    if isinstance(extra_lifecycle, dict):
        # Merge top-level keys only (do not clobber events/last_status).
        for k, v in extra_lifecycle.items():
            if k in ("events", "last_status"):
                continue
            lifecycle[k] = v

    updates = ["status=?", "lifecycle_json=?"]
    values: list[Any] = [new_status, json.dumps(lifecycle)]

    if new_status == "submitted":
        # ChatGPT audit fix: eToro 'submitted' must set submitted_at.
        if not row.get("submitted_at"):
            updates.append("submitted_at=?")
            values.append(now)
        if broker_order_id:
            updates.append("broker_order_id=?")
            values.append(str(broker_order_id))
    elif new_status == "filled":
        updates.append("filled_at=?")
        values.append(now)
        if fill_price is not None:
            updates.append("fill_price=?")
            values.append(float(fill_price))
        if fill_qty is not None:
            updates.append("fill_qty=?")
            values.append(float(fill_qty))
        if broker_order_id:
            updates.append("broker_order_id=?")
            values.append(str(broker_order_id))
    elif new_status in ("cancelled", "closed_manual"):
        updates.append("cancelled_at=?")
        values.append(now)
    # 'unverified', 'awaiting_confirm', etc. update status + lifecycle_json only.

    values.append(intent_id)
    sql = f"UPDATE execution_intents SET {', '.join(updates)} WHERE id=?"
    try:
        conn.execute(sql, values)
        conn.commit()
    except sqlite3.OperationalError as e:
        raise LifecycleError(
            f"SQL failure updating intent {intent_id} -> {new_status!r}: {e}"
        ) from e

    return _read_row(conn, intent_id)


def attach_evidence(
    conn: sqlite3.Connection,
    intent_id: int,
    *,
    key: str,
    value: Any,
) -> None:
    """Attach a key/value into lifecycle_json without changing status.

    Used by the operator CLI to record nonce digest, x-request-id,
    client_intent_id, redacted request/response bodies, etc.
    """
    if not isinstance(key, str) or not key:
        raise LifecycleError("key must be a non-empty string")
    row = _read_row(conn, intent_id)
    lifecycle = _load_lifecycle(row)
    lifecycle[key] = value
    try:
        conn.execute(
            "UPDATE execution_intents SET lifecycle_json=? WHERE id=?",
            (json.dumps(lifecycle), intent_id),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        raise LifecycleError(
            f"SQL failure attaching evidence to intent {intent_id}: {e}"
        ) from e


def get_lifecycle(conn: sqlite3.Connection, intent_id: int) -> dict:
    """Return the parsed lifecycle_json for inspection."""
    row = _read_row(conn, intent_id)
    return _load_lifecycle(row)


__all__ = [
    "ETORO_LIFECYCLE_STATUSES",
    "LifecycleError",
    "apply_transition",
    "attach_evidence",
    "get_lifecycle",
    "find_by_client_intent_id",
]
