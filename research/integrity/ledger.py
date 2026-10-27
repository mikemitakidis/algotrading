"""research.integrity.ledger — immutable, hash-chained research ledger.

Every hypothesis, trial (including failed and abandoned ones), control
battery, pre-registration, holdout release, data access and report card is
an ENTRY in one append-only SQLite table. Each entry's hash covers the
previous entry's hash, so editing, deleting or re-ordering any entry breaks
verification. SQLite triggers refuse UPDATE and DELETE outright; the chain
catches tampering that bypasses them (e.g. editing the file directly).

What the ledger proves and does not prove:
* It proves content integrity and order relative to other entries.
* Its ``created_utc`` is a CLIENT clock and is NOT proof of when something
  happened. Time proofs come from server-side attestations (see prereg.py).

Trial accounting: ``family_trial_count`` counts every trial in a family,
whatever its status, because a search that tried 40 variants and reported one
has 40 trials (this feeds the deflated Sharpe ratio).
"""
from __future__ import annotations

import re

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from research.common.hashing import canonical_json, digest

GENESIS = "0" * 64
LEDGER_FORMAT = "m21r-ledger-v1"
ENTRY_KINDS = (
    "hypothesis", "hypothesis_status", "trial", "prereg", "attestation",
    "prereg_evaluation", "holdout_seal", "holdout_release", "holdout_access_denied",
    "data_access", "control_battery_plan", "control_battery_result", "report_card",
    "note",
)
TRIAL_KINDS = ("research", "baseline", "control_null", "control_positive")
TRIAL_STATUSES = ("completed", "failed", "abandoned")
HYPOTHESIS_STATUSES = ("open", "supported", "rejected", "retired")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
    seq         INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    ref_id      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    entry_hash  TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS entries_kind ON entries(kind);
CREATE INDEX IF NOT EXISTS entries_ref ON entries(ref_id);
CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS meta_no_update BEFORE UPDATE ON ledger_meta
BEGIN SELECT RAISE(ABORT, 'ledger meta is immutable'); END;
CREATE TRIGGER IF NOT EXISTS meta_no_delete BEFORE DELETE ON ledger_meta
BEGIN SELECT RAISE(ABORT, 'ledger meta is immutable'); END;
"""


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    seq: int
    kind: str
    ref_id: str
    payload: Dict[str, Any]
    created_utc: str
    prev_hash: str
    entry_hash: str


def _entry_hash(prev_hash: str, seq: int, kind: str, ref_id: str, payload_json: str, created_utc: str) -> str:
    return digest({"prev": prev_hash, "seq": seq, "kind": kind, "ref": ref_id,
                   "payload": payload_json, "created_utc": created_utc, "format": LEDGER_FORMAT})


@dataclass(frozen=True)
class Hypothesis:
    family_id: str
    statement: str
    mechanism: str
    falsifier: str
    expected_effect: str
    author: str
    source_refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TrialRecord:
    family_id: str
    kind: str
    spec_digest: str
    data_digest: str
    code_digest: str
    seed: int
    metrics: Dict[str, Any]
    config: Dict[str, Any] = field(default_factory=dict)
    split: Dict[str, Any] = field(default_factory=dict)
    costs: Dict[str, Any] = field(default_factory=dict)
    status: str = "completed"
    hypothesis_id: Optional[str] = None
    parent_trial_id: Optional[str] = None
    notes: str = ""
    artifacts: Dict[str, str] = field(default_factory=dict)   # name -> outcome digest (research.integrity.provenance)

    def __post_init__(self):
        for k, v in self.artifacts.items():
            if not (isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)):
                raise ValueError("trial artifacts must map names to sha256 hex digests, got %r: %r" % (k, v))
        if self.kind not in TRIAL_KINDS:
            raise ValueError("trial kind must be one of %s" % (TRIAL_KINDS,))
        if self.status not in TRIAL_STATUSES:
            raise ValueError("trial status must be one of %s" % (TRIAL_STATUSES,))
        if not self.family_id:
            raise ValueError("family_id is required")


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    n_entries: int
    first_bad_seq: Optional[int] = None
    reason: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    """Append-only research ledger backed by SQLite (file path or ':memory:')."""

    def __init__(self, path: Union[str, Path] = ":memory:", *, clock: Callable[[], str] = _utc_now):
        self.path = str(path)
        self._clock = clock
        self._db = sqlite3.connect(self.path)
        self._db.executescript(_SCHEMA)
        cur = self._db.execute("SELECT value FROM ledger_meta WHERE key='format'")
        row = cur.fetchone()
        if row is None:
            self._db.execute("INSERT INTO ledger_meta VALUES ('format', ?)", (LEDGER_FORMAT,))
            self._db.commit()
        elif row[0] != LEDGER_FORMAT:
            raise LedgerError("unsupported ledger format %r" % row[0])

    def close(self) -> None:
        self._db.close()

    # ── core ──────────────────────────────────────────────────────────
    def _tip(self) -> Tuple[int, str]:
        row = self._db.execute("SELECT seq, entry_hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
        return (0, GENESIS) if row is None else (int(row[0]), row[1])

    def append(self, kind: str, payload: Dict[str, Any], ref_id: str = "") -> Entry:
        if kind not in ENTRY_KINDS:
            raise LedgerError("unknown entry kind %r" % kind)
        payload_json = canonical_json(payload)
        with self._db:                                    # one transaction
            last_seq, prev = self._tip()
            seq = last_seq + 1
            created = self._clock()
            ref = ref_id or "%s-%06d" % (kind[:3].upper(), seq)
            h = _entry_hash(prev, seq, kind, ref, payload_json, created)
            self._db.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?)",
                             (seq, kind, ref, payload_json, created, prev, h))
        return Entry(seq, kind, ref, json.loads(payload_json), created, prev, h)

    def entries(self, kind: Optional[str] = None, ref_id: Optional[str] = None) -> List[Entry]:
        q, args = "SELECT seq, kind, ref_id, payload, created_utc, prev_hash, entry_hash FROM entries", []
        cond = []
        if kind is not None:
            cond.append("kind = ?")
            args.append(kind)
        if ref_id is not None:
            cond.append("ref_id = ?")
            args.append(ref_id)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY seq"
        return [Entry(r[0], r[1], r[2], json.loads(r[3]), r[4], r[5], r[6]) for r in self._db.execute(q, args)]

    def verify(self, expected_tip: Optional[str] = None) -> VerifyResult:
        """Recompute the chain. Edits, deletions and re-ordering are caught
        locally; truncating the tail or rewriting the WHOLE chain consistently
        is only caught against a tip hash anchored elsewhere (e.g. posted in a
        GitHub comment or committed with the JSONL mirror): pass it as
        `expected_tip`."""
        prev, n, expect_seq = GENESIS, 0, 1
        for r in self._db.execute("SELECT seq, kind, ref_id, payload, created_utc, prev_hash, entry_hash "
                                  "FROM entries ORDER BY seq"):
            seq, kind, ref, pj, created, ph, h = r
            n += 1
            if seq != expect_seq:
                return VerifyResult(False, n, seq, "sequence gap (missing entry before %d)" % seq)
            if ph != prev:
                return VerifyResult(False, n, seq, "prev_hash does not match previous entry")
            if _entry_hash(ph, seq, kind, ref, pj, created) != h:
                return VerifyResult(False, n, seq, "entry content does not match its hash")
            prev, expect_seq = h, seq + 1
        if expected_tip is not None and prev != expected_tip:
            return VerifyResult(False, n, None, "tip hash differs from the anchored tip (truncated or rewritten)")
        return VerifyResult(True, n)

    @property
    def tip_hash(self) -> str:
        return self._tip()[1]

    # ── JSONL mirror (for Git) ─────────────────────────────────────────
    def export_jsonl(self, path: Union[str, Path]) -> int:
        rows = self.entries()
        with open(path, "w", encoding="utf-8") as f:
            for e in rows:
                f.write(json.dumps(asdict(e), sort_keys=True) + "\n")
        return len(rows)

    @staticmethod
    def verify_jsonl(path: Union[str, Path]) -> VerifyResult:
        prev, n = GENESIS, 0
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                e = json.loads(line)
                n += 1
                if e["seq"] != i or e["prev_hash"] != prev:
                    return VerifyResult(False, n, e.get("seq"), "chain broken")
                pj = canonical_json(e["payload"])
                if _entry_hash(prev, e["seq"], e["kind"], e["ref_id"], pj, e["created_utc"]) != e["entry_hash"]:
                    return VerifyResult(False, n, e["seq"], "entry content does not match its hash")
                prev = e["entry_hash"]
        return VerifyResult(True, n)

    # ── domain helpers ────────────────────────────────────────────────
    def register_hypothesis(self, h: Hypothesis) -> str:
        hid = "H-" + digest(asdict(h))[:12]
        if self.entries("hypothesis", hid):
            raise LedgerError("hypothesis %s already registered" % hid)
        self.append("hypothesis", asdict(h), ref_id=hid)
        self.append("hypothesis_status", {"status": "open", "reason": "registered"}, ref_id=hid)
        return hid

    def set_hypothesis_status(self, hypothesis_id: str, status: str, reason: str) -> None:
        if status not in HYPOTHESIS_STATUSES:
            raise LedgerError("status must be one of %s" % (HYPOTHESIS_STATUSES,))
        if not self.entries("hypothesis", hypothesis_id):
            raise LedgerError("unknown hypothesis %s" % hypothesis_id)
        self.append("hypothesis_status", {"status": status, "reason": reason}, ref_id=hypothesis_id)

    def hypothesis_status(self, hypothesis_id: str) -> str:
        rows = self.entries("hypothesis_status", hypothesis_id)
        if not rows:
            raise LedgerError("unknown hypothesis %s" % hypothesis_id)
        return rows[-1].payload["status"]

    def record_trial(self, t: TrialRecord) -> str:
        if t.hypothesis_id is not None and not self.entries("hypothesis", t.hypothesis_id):
            raise LedgerError("trial references unknown hypothesis %s" % t.hypothesis_id)
        if t.parent_trial_id is not None and not self.entries("trial", t.parent_trial_id):
            raise LedgerError("trial references unknown parent %s" % t.parent_trial_id)
        seq = self._tip()[0] + 1
        tid = "T%06d-%s" % (seq, digest(asdict(t))[:8])
        self.append("trial", asdict(t), ref_id=tid)
        return tid

    def trials(self, family_id: Optional[str] = None, kinds: Iterable[str] = TRIAL_KINDS) -> List[Entry]:
        kinds = set(kinds)
        return [e for e in self.entries("trial")
                if e.payload["kind"] in kinds and (family_id is None or e.payload["family_id"] == family_id)]

    def family_trial_count(self, family_id: str, include_controls: bool = False) -> int:
        kinds = TRIAL_KINDS if include_controls else ("research", "baseline")
        return len(self.trials(family_id, kinds))

    def family_metric(self, family_id: str, key: str) -> List[float]:
        out = []
        for e in self.trials(family_id, ("research", "baseline")):
            v = e.payload["metrics"].get(key)
            if isinstance(v, (int, float)):
                out.append(float(v))
        return out

    def record_access(self, dataset_digest: str, actor: str, purpose: str) -> Entry:
        return self.append("data_access", {"dataset_digest": dataset_digest, "actor": actor, "purpose": purpose})

    def assert_ledgered(self, trial_ids: Iterable[str]) -> None:
        known = {e.ref_id for e in self.entries("trial")}
        missing = sorted(set(trial_ids) - known)
        if missing:
            raise LedgerError("unledgered trials referenced: %s" % missing)


def code_digest(paths: Iterable[Union[str, Path]]) -> str:
    """Content digest of source files (sorted by path); no git needed."""
    items = []
    for p in sorted(str(x) for x in paths):
        items.append({"path": Path(p).as_posix(), "sha": digest(Path(p).read_text(encoding="utf-8"))})
    return digest(items)


__all__ = ["GENESIS", "LEDGER_FORMAT", "ENTRY_KINDS", "TRIAL_KINDS", "TRIAL_STATUSES",
           "HYPOTHESIS_STATUSES", "LedgerError", "Entry", "Hypothesis", "TrialRecord",
           "VerifyResult", "Ledger", "code_digest"]
