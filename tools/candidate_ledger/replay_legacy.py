#!/usr/bin/env python3
"""M21.1 A02 — offline reconstruction of the candidate ledger from legacy tables.

Reads ``candidate_snapshots``, ``signals``, ``execution_intents`` and
``signal_outcomes`` from a SOURCE database opened read-only (SQLite URI
``mode=ro`` + ``query_only``) and writes the ledger into a separate OUTPUT
database that must live under /tmp/. The source is never written.
Re-running is idempotent (deterministic ids, insert-or-ignore).

Best-effort for legacy data: legacy rows carry no candidate id and the cycle
counter restarts with the process, so candidates are keyed by their own
recorded timestamps (signals.timestamp for final signals, the snapshot
timestamp otherwise). Gaps are reported, never invented (e.g. a signal with no
intent row closes as ``untracked_after_signal``).

Running it against a copy of the production database is an operator decision.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote

from bot.candidate_ledger import (
    ACCEPTED_STATUSES, RESOLVED_OUTCOMES, CandidateLedgerWriter, account,
    migrate, risk_payload, scanner_strategy_id,
)

_IND = ("rsi", "macd_hist", "atr", "bb_pos", "vwap_dev", "vol_ratio")


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect("file:%s?mode=ro" % quote(str(path.resolve())), uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


_REPO = Path(__file__).resolve().parents[2]


def _check_paths(source: Path, output: Path) -> None:
    if not source.is_file():
        raise ValueError("source database not found: %s" % source)
    out = output.resolve()
    if not str(out).startswith("/tmp/"):
        raise ValueError("output must be under /tmp/ (got %s)" % out)
    if out == _REPO or _REPO in out.parents:
        raise ValueError("output must not be inside the repository")
    if not out.parent.is_dir():
        raise ValueError("output directory does not exist: %s" % out.parent)
    if out == source.resolve():
        raise ValueError("output must differ from the source database")


def _strategy(v) -> str:
    try:
        return scanner_strategy_id(int(v if v is not None else 1))
    except (TypeError, ValueError):
        return scanner_strategy_id(1)


def replay(source_db, output_db, *, now_utc: Optional[str] = None) -> dict:
    source, output = Path(source_db), Path(output_db)
    _check_paths(source, output)
    now_utc = now_utc or datetime.now(timezone.utc).isoformat()
    oc = sqlite3.connect(str(output))
    try:
        migrate(oc, now_utc=now_utc)
        oc.commit()
    finally:
        oc.close()
    w = CandidateLedgerWriter(lambda: sqlite3.connect(str(output)), enabled=True,
                              writer="legacy_replay")
    if not w.enabled:
        raise RuntimeError("ledger schema not present in output")
    src = _ro(source)
    try:
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        snaps = (src.execute("SELECT * FROM candidate_snapshots ORDER BY id").fetchall()
                 if "candidate_snapshots" in tables else [])
        signals = (src.execute("SELECT * FROM signals ORDER BY id").fetchall()
                   if "signals" in tables else [])
        intents: Dict[int, list] = {}
        if "execution_intents" in tables:
            for r in src.execute("SELECT * FROM execution_intents ORDER BY id"):
                intents.setdefault(r["signal_id"], []).append(r)
        outcomes: Dict[int, list] = {}
        if "signal_outcomes" in tables:
            for r in src.execute("SELECT * FROM signal_outcomes ORDER BY id"):
                outcomes.setdefault(r["signal_id"], []).append(r)
    finally:
        src.close()

    summary = {"snapshots_not_selected": 0, "signals": 0, "invalid_rows": 0,
               "multiple_intents": 0, "multiple_outcomes": 0}
    final_snap = {r["signal_id"]: r for r in snaps
                  if r["stage"] == "final_signal" and r["signal_id"] is not None}

    for r in snaps:
        if r["stage"] == "final_signal" and r["signal_id"] is not None:
            continue
        cid = w.open_candidate(
            symbol=r["symbol"], direction=r["direction"],
            strategy_id=_strategy(r["strategy_version"]), decision_ts_utc=r["timestamp"],
            cycle_key=r["timestamp"], cycle_id=r["cycle_id"], scanner_stage=r["stage"],
            at_utc=r["timestamp"],
            payload={"valid_count": r["valid_count"], "tfs_passing": r["tfs_passing"],
                     "available_tfs": r["available_tfs"], "legacy_snapshot_id": r["id"],
                     "indicators": {k: r[k] for k in _IND}},
            reason_codes=[r["rejection_reason"]] if r["rejection_reason"] else [])
        if cid is None:
            summary["invalid_rows"] += 1
            continue
        w.close(cid, "not_selected", at_utc=r["timestamp"])
        summary["snapshots_not_selected"] += 1

    for s in signals:
        snap = final_snap.get(s["id"])
        cid = w.open_candidate(
            symbol=s["symbol"], direction=s["direction"],
            strategy_id=_strategy(s["strategy_version"]), decision_ts_utc=s["timestamp"],
            cycle_key=s["timestamp"], cycle_id=snap["cycle_id"] if snap else None,
            scanner_stage="final_signal", at_utc=s["timestamp"],
            payload={"valid_count": s["valid_count"], "route": s["route"],
                     "legacy_snapshot_id": snap["id"] if snap else None,
                     "indicators": {k: s[k] for k in _IND if k in s.keys()}})
        if cid is None:
            summary["invalid_rows"] += 1
            continue
        summary["signals"] += 1
        w.link(cid, signal_id=s["id"])
        rows = intents.get(s["id"], [])
        if len(rows) > 1:
            summary["multiple_intents"] += 1
        if not rows:
            w.close(cid, "untracked_after_signal", at_utc=s["timestamp"])
            continue
        it = rows[0]
        try:
            checks = json.loads(it["risk_checks"] or "{}")
        except ValueError:
            checks = {}
        status = it["status"]
        at = it["timestamp"] or s["timestamp"]
        passed = status != "risk_rejected"
        w.record(cid, "risk_evaluated", at_utc=at,
                 payload=risk_payload(passed, it["rejection_reason"], checks),
                 reason_codes=[it["rejection_reason"]] if it["rejection_reason"] else [])
        w.record(cid, "intent_logged", at_utc=at,
                 payload={"intent_id": it["id"], "status": status, "broker": it["broker"]})
        w.link(cid, intent_id=it["id"])
        if status == "risk_rejected":
            w.close(cid, "risk_rejected", at_utc=at)
            continue
        w.record(cid, "broker_result", at_utc=it["submitted_at"] or at,
                 payload={"status": status, "broker_order_id": it["broker_order_id"],
                          "rejection_reason": it["rejection_reason"]})
        if status == "broker_unready":
            w.close(cid, "broker_unready", at_utc=at)
        elif status == "cancelled":
            w.close(cid, "cancelled", at_utc=it["cancelled_at"] or at)
        elif status in ACCEPTED_STATUSES:
            outs = [o for o in outcomes.get(s["id"], []) if o["outcome"] in RESOLVED_OUTCOMES]
            if len(outs) > 1:
                summary["multiple_outcomes"] += 1
            if outs:
                o = outs[0]
                w.record(cid, "outcome_resolved", at_utc=o["resolved_at"] or at,
                         payload={"outcome_id": o["id"], "outcome": o["outcome"],
                                  "return_pct": o["return_pct"],
                                  "resolution_method": o["resolution_method"]})
                w.link(cid, outcome_id=o["id"])
                w.close(cid, "outcome_resolved", at_utc=o["resolved_at"] or at)
            # else: in flight, awaiting its outcome
        elif status == "pending":
            pass            # unknown submit result: stays in flight, reported as incomplete
        else:
            w.close(cid, "broker_not_accepted", at_utc=at)

    oc = sqlite3.connect(str(output))
    try:
        report = account(oc).to_dict()
    finally:
        oc.close()
    return {"summary": summary, "writer_stats": dict(w.stats), "accounting": report}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="legacy signals DB (opened read-only)")
    ap.add_argument("--output", required=True, help="ledger DB to write (under /tmp/ only)")
    args = ap.parse_args(argv)
    try:
        res = replay(args.source, args.output)
    except ValueError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0 if res["accounting"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
