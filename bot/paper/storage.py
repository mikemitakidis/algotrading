"""M20.H paper storage (append-only JSONL).

Explicit-call-only persistence for the proven paper records. Three homogeneous
streams under a caller-supplied path (default data/paper/ only when an explicit
write is requested):
  events.jsonl         -> PaperEvent       (id: paper_event_id)
  snapshots.jsonl      -> PaperPnLSnapshot (id: timestamp_utc)
  account_state.jsonl  -> PaperAccountState(id: as_of_utc)

Canonical line format: json.dumps(rec.to_dict(), sort_keys=True,
separators=(",", ":")) + "\n". Append-only (never truncate/rewrite). Idempotent
on id (duplicates skipped). Loaders parse via the frozen from_dict (unknown-field
rejection inherited), ignore empty lines, close handles, and safe-reject on
missing/corrupt input. replay_events_summary folds PaperEvent.detail into a
simple validation summary only (no price-based PnL recompute, no duplication of
the M20.G accounting engine).

IMPORTING THIS MODULE PERFORMS NO I/O: no file creation, no data/paper creation,
no open handles. Directory creation happens only inside explicit write calls.
No SQLite, no runtime wiring, no dashboard, no broker/live/risk imports, no RNG,
no wall-clock.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bot.paper.schema import PaperEvent, PaperPnLSnapshot
from bot.paper.account import PaperAccountState

_DEFAULT_DIR = os.path.join("data", "paper")
_EVENTS_FILE = "events.jsonl"
_SNAPSHOTS_FILE = "snapshots.jsonl"
_ACCOUNT_FILE = "account_state.jsonl"


@dataclass(frozen=True)
class PaperStorageResult:
    ok: bool
    records: List[Any] = field(default_factory=list)
    path: Optional[str] = None
    written: int = 0
    loaded: int = 0
    duplicate_skipped: int = 0
    rejection_reason: Optional[str] = None
    reason_codes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    derived_metrics: Dict[str, Any] = field(default_factory=dict)


def _reject(reason: str, **dm) -> PaperStorageResult:
    return PaperStorageResult(ok=False, rejection_reason=reason,
                              reason_codes=[reason], derived_metrics=dm)


def _canonical(record_dict: Dict[str, Any]) -> str:
    return json.dumps(record_dict, sort_keys=True, separators=(",", ":"))


def _resolve_path(stream_file: str, *, path: Optional[str],
                  directory: Optional[str]) -> str:
    if path is not None:
        return path
    if directory is not None:
        return os.path.join(directory, stream_file)
    return os.path.join(_DEFAULT_DIR, stream_file)


def _existing_ids(target: str, id_key: str) -> set:
    """Read ids already present in the target file (empty set if absent)."""
    ids: set = set()
    if not os.path.exists(target):
        return ids
    with open(target, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)[id_key])
            except (ValueError, KeyError, TypeError):
                continue
    return ids


def _append(records, *, expected_type, id_attr: str, stream_file: str,
            path: Optional[str], directory: Optional[str]) -> PaperStorageResult:
    if not isinstance(records, (list, tuple)):
        return _reject("invalid_records")
    for r in records:
        if not isinstance(r, expected_type):
            return _reject("invalid_record_type")

    target = _resolve_path(stream_file, path=path, directory=directory)
    parent = os.path.dirname(target)
    seen = _existing_ids(target, id_attr)

    written = 0
    duplicate_skipped = 0
    lines: List[str] = []
    for r in records:
        rid = getattr(r, id_attr)
        if rid in seen:
            duplicate_skipped += 1
            continue
        seen.add(rid)
        lines.append(_canonical(r.to_dict()))
        written += 1

    if written:
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln + "\n")

    return PaperStorageResult(ok=True, path=target, written=written,
                              duplicate_skipped=duplicate_skipped,
                              derived_metrics={"written": written,
                                               "duplicate_skipped":
                                               duplicate_skipped})


def _load(path: str, *, parse) -> PaperStorageResult:
    if not isinstance(path, str) or not path:
        return _reject("invalid_path")
    if not os.path.exists(path):
        return _reject("file_not_found", path=path)
    records: List[Any] = []
    with open(path, "r", encoding="utf-8") as fh:
        for i, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                records.append(parse(obj))
            except (ValueError, KeyError, TypeError) as e:
                return PaperStorageResult(
                    ok=False, path=path, rejection_reason="corrupt_record",
                    reason_codes=["corrupt_record"],
                    derived_metrics={"line_number": i, "error": str(e)})
    return PaperStorageResult(ok=True, records=records, path=path,
                              loaded=len(records),
                              derived_metrics={"loaded": len(records)})


# ── events ──
def append_events(events, *, path=None, directory=None) -> PaperStorageResult:
    return _append(events, expected_type=PaperEvent, id_attr="paper_event_id",
                   stream_file=_EVENTS_FILE, path=path, directory=directory)


def load_events(path) -> PaperStorageResult:
    return _load(path, parse=PaperEvent.from_dict)


# ── snapshots ──
def append_snapshots(snapshots, *, path=None,
                     directory=None) -> PaperStorageResult:
    return _append(snapshots, expected_type=PaperPnLSnapshot,
                   id_attr="timestamp_utc", stream_file=_SNAPSHOTS_FILE,
                   path=path, directory=directory)


def load_snapshots(path) -> PaperStorageResult:
    return _load(path, parse=PaperPnLSnapshot.from_dict)


# ── account state ──
def append_account_states(states, *, path=None,
                          directory=None) -> PaperStorageResult:
    return _append(states, expected_type=PaperAccountState, id_attr="as_of_utc",
                   stream_file=_ACCOUNT_FILE, path=path, directory=directory)


def load_account_states(path) -> PaperStorageResult:
    return _load(path, parse=PaperAccountState.from_dict)


# ── lightweight replay validator (event detail only) ──
def replay_events_summary(events) -> PaperStorageResult:
    """Fold PaperEvent.detail into a simple validation summary. Does NOT recompute
    realised PnL from prices and does NOT duplicate the M20.G accounting engine."""
    if not isinstance(events, (list, tuple)):
        return _reject("invalid_records")
    for e in events:
        if not isinstance(e, PaperEvent):
            return _reject("invalid_record_type")

    cash_delta_total = 0.0
    realized_pnl_total = 0.0
    open_position_ids: List[str] = []
    closed_position_ids: List[str] = []
    for e in events:
        detail = e.detail or {}
        if "cash_delta" in detail:
            try:
                cash_delta_total += float(detail["cash_delta"])
            except (TypeError, ValueError):
                return _reject("corrupt_record")
        if "net_realized_pnl" in detail:
            try:
                realized_pnl_total += float(detail["net_realized_pnl"])
            except (TypeError, ValueError):
                return _reject("corrupt_record")
        etype = e.event_type.value
        pid = detail.get("paper_position_id")
        if pid is not None:
            if etype == "POSITION_OPENED":
                open_position_ids.append(pid)
            elif etype == "POSITION_CLOSED":
                closed_position_ids.append(pid)

    still_open = sorted(set(open_position_ids) - set(closed_position_ids))
    summary = {
        "cash_delta_total": cash_delta_total,
        "realized_pnl_total": realized_pnl_total,
        "open_position_ids": still_open,
        "closed_position_ids": sorted(set(closed_position_ids)),
        "event_count": len(events),
    }
    return PaperStorageResult(ok=True, loaded=len(events),
                              derived_metrics=summary)
