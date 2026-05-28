"""
tools/etoro_reconcile.py — M13.5.B controlled reconciliation CLI.

Purpose:
  After an `unverified` outcome (post-POST polling could not confirm
  state), the operator manually verifies the order in the eToro web UI
  and uses this CLI to attach the verification result to the existing
  execution_intents row.

Hard guarantees (M13.5.A §6):
  * Does NOT place an order.
  * Does NOT call any eToro write endpoint.
  * Updates lifecycle through bot/etoro/lifecycle.py ONLY — no raw SQL.
  * Requires explicit intent_id; refuses bulk operations.
  * Refuses to act on already-terminal rows unless --allow-terminal-override
    is passed (operator must opt-in explicitly).

Import-time guard:
  This module REFUSES to import if bot.etoro.live_broker is already
  in sys.modules. The intent: a single process never both submits and
  reconciles. The reconciler is run separately, after the operator
  CLI exits.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

# Hard guard: refuse if live broker module already imported. This is
# defensive; in normal usage the reconciler runs in its own process.
if "bot.etoro.live_broker" in sys.modules:   # pragma: no cover
    raise ImportError(
        "tools.etoro_reconcile must not be loaded in a process that has "
        "imported bot.etoro.live_broker. Run the reconciler separately."
    )

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=os.environ.get("ETORO_CLI_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [etoro_reconcile] %(message)s",
)
log = logging.getLogger(__name__)


def _resolve_db_path(arg_db: Optional[str]) -> str:
    if arg_db:
        return arg_db
    return os.environ.get("SIGNALS_DB_PATH") or \
        str(_REPO_ROOT / "data" / "signals.db")


def _load_json_arg(value: str) -> dict:
    """Accept either a JSON literal string or a path to a JSON file."""
    p = Path(value)
    if p.exists():
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = json.loads(value)
    if not isinstance(data, dict):
        raise SystemExit("evidence must be a JSON object")
    return data


def cmd_show(args) -> int:
    from bot.etoro.lifecycle import get_lifecycle
    conn = sqlite3.connect(_resolve_db_path(args.db))
    try:
        row = conn.execute(
            "SELECT id, status, symbol, broker, broker_order_id, "
            "       submitted_at, filled_at, cancelled_at "
            "FROM execution_intents WHERE id=?", (args.intent_id,)
        ).fetchone()
        if not row:
            print(f"ERROR: intent {args.intent_id} not found")
            return 2
        cols = ["id", "status", "symbol", "broker", "broker_order_id",
                "submitted_at", "filled_at", "cancelled_at"]
        for c, v in zip(cols, row):
            print(f"  {c:18s} = {v!r}")
        lifecycle = get_lifecycle(conn, args.intent_id)
        print("  lifecycle_json     =")
        print(json.dumps(lifecycle, indent=4, sort_keys=True))
    finally:
        conn.close()
    return 0


def cmd_mark_filled(args) -> int:
    """Attach an operator-verified fill to an unverified row.

    Required evidence: --evidence FILE_OR_JSON containing at minimum
    {"position_id": int, "fill_price": float, "fill_qty": float}.
    """
    from bot.etoro.lifecycle import apply_transition, attach_evidence
    evidence = _load_json_arg(args.evidence)
    pid = evidence.get("position_id")
    fp = evidence.get("fill_price")
    fq = evidence.get("fill_qty")
    if pid is None or fp is None or fq is None:
        print("ERROR: evidence must include position_id, fill_price, fill_qty")
        return 2

    conn = sqlite3.connect(_resolve_db_path(args.db))
    try:
        attach_evidence(conn, args.intent_id, key="reconcile_evidence",
                        value=evidence)
        apply_transition(
            conn, args.intent_id, "filled",
            fill_price=float(fp),
            fill_qty=float(fq),
            broker_order_id=str(evidence.get("order_id", "")) or None,
            event=f"operator_reconciled_filled:{args.note}" if args.note
                  else "operator_reconciled_filled",
            extra_lifecycle={
                "position_id": pid,
                "reconciled_by": "tools/etoro_reconcile.py",
            },
            allow_terminal_override=bool(args.allow_terminal_override),
        )
        print(f"OK: intent {args.intent_id} -> filled "
              f"(position_id={pid}, fill_price={fp}, fill_qty={fq})")
    finally:
        conn.close()
    return 0


def cmd_mark_rejected(args) -> int:
    """Attach an operator-verified rejection to an unverified row."""
    from bot.etoro.lifecycle import apply_transition, attach_evidence
    conn = sqlite3.connect(_resolve_db_path(args.db))
    try:
        if args.evidence:
            attach_evidence(conn, args.intent_id, key="reconcile_evidence",
                            value=_load_json_arg(args.evidence))
        apply_transition(
            conn, args.intent_id, "broker_rejected",
            event=f"operator_reconciled_rejected:{args.note}" if args.note
                  else "operator_reconciled_rejected",
            extra_lifecycle={"reconciled_by": "tools/etoro_reconcile.py"},
            allow_terminal_override=bool(args.allow_terminal_override),
        )
        print(f"OK: intent {args.intent_id} -> broker_rejected")
    finally:
        conn.close()
    return 0


def cmd_mark_closed_manual(args) -> int:
    """Record an operator manual close (via eToro web UI)."""
    from bot.etoro.lifecycle import apply_transition, attach_evidence
    conn = sqlite3.connect(_resolve_db_path(args.db))
    try:
        if args.evidence:
            attach_evidence(conn, args.intent_id, key="close_evidence",
                            value=_load_json_arg(args.evidence))
        apply_transition(
            conn, args.intent_id, "closed_manual",
            event=f"operator_closed_manually:{args.note}" if args.note
                  else "operator_closed_manually",
            extra_lifecycle={"closed_by": "tools/etoro_reconcile.py"},
            allow_terminal_override=bool(args.allow_terminal_override),
        )
        print(f"OK: intent {args.intent_id} -> closed_manual")
    finally:
        conn.close()
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="etoro_reconcile",
        description="M13.5.B controlled reconciliation. Updates lifecycle "
                    "via bot.etoro.lifecycle only — never raw SQL, never "
                    "calls eToro write endpoints.",
    )
    parser.add_argument("--db", default=None)
    parser.add_argument("--allow-terminal-override", action="store_true",
                        help="Allow transitioning out of an already-terminal "
                             "status. Use with care.")

    sub = parser.add_subparsers(dest="cmd", required=True)

    s_show = sub.add_parser("show", help="Show one intent row.")
    s_show.add_argument("intent_id", type=int)

    s_fill = sub.add_parser("mark-filled",
                            help="Mark an unverified row as filled.")
    s_fill.add_argument("intent_id", type=int)
    s_fill.add_argument("--evidence", required=True,
                        help="Path to JSON file or inline JSON. Must contain "
                             "position_id, fill_price, fill_qty.")
    s_fill.add_argument("--note", type=str, default=None)

    s_rej = sub.add_parser("mark-rejected",
                           help="Mark an unverified row as broker_rejected.")
    s_rej.add_argument("intent_id", type=int)
    s_rej.add_argument("--evidence", required=False, default=None)
    s_rej.add_argument("--note", type=str, default=None)

    s_close = sub.add_parser("mark-closed-manual",
                             help="Record an operator manual close.")
    s_close.add_argument("intent_id", type=int)
    s_close.add_argument("--evidence", required=False, default=None)
    s_close.add_argument("--note", type=str, default=None)

    args = parser.parse_args(argv)
    if args.cmd == "show":
        return cmd_show(args)
    if args.cmd == "mark-filled":
        return cmd_mark_filled(args)
    if args.cmd == "mark-rejected":
        return cmd_mark_rejected(args)
    if args.cmd == "mark-closed-manual":
        return cmd_mark_closed_manual(args)
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
