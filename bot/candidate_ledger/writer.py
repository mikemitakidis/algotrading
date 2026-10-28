"""M21.1 A02 — flag-gated, idempotent, never-raising ledger writer.

* Disabled by default. It writes only when constructed with ``enabled=True``
  AND the ledger schema is already present at the expected version. The
  writer never creates tables; ``schema.migrate`` is a separate, explicit
  step. ``ledger_enabled_from_env()`` reads ``CANDIDATE_LEDGER_ENABLED`` and
  is True only for the exact value "1".
* Own connection: every call opens a connection from ``conn_factory``,
  commits its own statements and closes it, so a caller's transaction is
  never committed or rolled back by the ledger.
* Idempotent: ids are deterministic; inserts use INSERT OR IGNORE. A repeated
  call with identical content is a no-op; different content for an existing
  (candidate, stage, attempt) is NOT overwritten and is counted as a
  conflict. Links and terminal kinds can be set once.
* Order-checked: a stage is accepted only after the stages it requires.
* Never raises into the caller: errors are counted in ``stats``.

Nothing in bot/ or main.py calls this writer yet (no runtime wiring).
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import Counter
from typing import Any, Callable, Iterable, Mapping, Optional

from bot.candidate_ledger.ids import make_candidate_id, make_event_id
from bot.candidate_ledger.schema import schema_present
from bot.candidate_ledger.stages import REQUIRES, STAGES, TERMINAL_KINDS

ENV_FLAG = "CANDIDATE_LEDGER_ENABLED"


def ledger_enabled_from_env(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENV_FLAG, "") == "1"


def _plain(v: Any) -> Any:
    if isinstance(v, Mapping):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        items = [_plain(x) for x in v]
        return sorted(items, key=repr) if isinstance(v, (set, frozenset)) else items
    if isinstance(v, bool) or v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else repr(v)
    if hasattr(v, "value") and isinstance(getattr(v, "value"), (str, int)):
        return v.value                           # enums
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def safe_json(obj: Any) -> str:
    """Deterministic JSON (sorted keys, compact); non-finite floats as text."""
    return json.dumps(_plain(obj), sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


class CandidateLedgerWriter:
    def __init__(self, conn_factory: Callable[[], sqlite3.Connection], *,
                 enabled: bool = False, writer: str = "scanner"):
        self._factory = conn_factory
        self.writer = str(writer)
        self.stats: Counter = Counter()
        self.enabled = False
        if enabled is not True:
            return
        try:
            conn = conn_factory()
            try:
                self.enabled = schema_present(conn)
            finally:
                conn.close()
        except Exception:
            self.stats["error:open"] += 1
            self.enabled = False
        if not self.enabled:
            self.stats["disabled:schema_absent"] += 1

    # ── internals ──
    def _run(self, fn):
        if not self.enabled:
            self.stats["skipped:disabled"] += 1
            return None
        try:
            conn = self._factory()
        except Exception as exc:
            self.stats["error:%s" % type(exc).__name__] += 1
            return None
        try:
            out = fn(conn)
            conn.commit()
            return out
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            self.stats["error:%s" % type(exc).__name__] += 1
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _stages_present(conn, candidate_id) -> set:
        return {r[0] for r in conn.execute(
            "SELECT stage FROM candidate_ledger_events WHERE candidate_id=?",
            (candidate_id,))}

    def _insert_event(self, conn, candidate_id, stage, attempt, at_utc,
                      reason_codes, payload) -> bool:
        eid = make_event_id(candidate_id, stage, attempt)
        rc, pj = safe_json(list(reason_codes)), safe_json(payload or {})
        cur = conn.execute(
            "INSERT OR IGNORE INTO candidate_ledger_events (event_id, candidate_id, "
            "stage, attempt, at_utc, reason_codes, payload_json, writer) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, candidate_id, stage, attempt, str(at_utc), rc, pj, self.writer))
        if cur.rowcount == 1:
            self.stats["event:%s" % stage] += 1
            return True
        old = conn.execute("SELECT reason_codes, payload_json FROM candidate_ledger_events "
                           "WHERE event_id=?", (eid,)).fetchone()
        if old is not None and (old[0], old[1]) == (rc, pj):
            self.stats["idempotent:%s" % stage] += 1
            return True
        self.stats["conflict:%s" % stage] += 1
        return False

    # ── public API ──
    def open_candidate(self, *, symbol: str, direction: str, strategy_id: str,
                       decision_ts_utc: Any, cycle_key: str, scanner_stage: str,
                       at_utc: str, cycle_id: Optional[int] = None,
                       bar_asof: Optional[Mapping[str, Any]] = None,
                       payload: Optional[Mapping[str, Any]] = None,
                       reason_codes: Iterable[str] = ()) -> Optional[str]:
        """Register a candidate and its ``scanned`` event. Returns the
        deterministic candidate id (also when disabled, so callers can carry
        it), or None if the inputs cannot form an id."""
        try:
            cid = make_candidate_id(symbol, decision_ts_utc, direction, strategy_id)
            from bot.candidate_ledger.ids import _iso_utc
            dts = _iso_utc(decision_ts_utc)
        except Exception as exc:
            self.stats["invalid:%s" % type(exc).__name__] += 1
            return None

        def fn(conn):
            conn.execute(
                "INSERT OR IGNORE INTO candidate_ledger (candidate_id, cycle_key, cycle_id, "
                "symbol, direction, strategy_id, decision_ts_utc, scanner_stage, "
                "bar_asof_json, first_seen_utc, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, str(cycle_key), cycle_id, symbol, direction, strategy_id, dts,
                 str(scanner_stage), safe_json(bar_asof) if bar_asof is not None else None,
                 str(at_utc), self.writer))
            body = dict(payload or {})
            body["scanner_stage"] = str(scanner_stage)
            self._insert_event(conn, cid, "scanned", 0, at_utc, reason_codes, body)
        self._run(fn)
        return cid

    def record(self, candidate_id: str, stage: str, *, at_utc: str,
               payload: Optional[Mapping[str, Any]] = None,
               reason_codes: Iterable[str] = (), attempt: int = 0) -> bool:
        if stage not in STAGES or stage in ("scanned", "closed"):
            self.stats["invalid:stage"] += 1
            return False

        def fn(conn):
            if conn.execute("SELECT 1 FROM candidate_ledger WHERE candidate_id=?",
                            (candidate_id,)).fetchone() is None:
                self.stats["rejected:unknown_candidate"] += 1
                return False
            have = self._stages_present(conn, candidate_id)
            if conn.execute("SELECT 1 FROM candidate_ledger_events WHERE event_id=?",
                            (make_event_id(candidate_id, stage, attempt),)).fetchone():
                # already recorded: identical -> idempotent, different -> conflict
                return self._insert_event(conn, candidate_id, stage, attempt, at_utc,
                                          reason_codes, payload)
            if "closed" in have:
                self.stats["rejected:after_close"] += 1
                return False
            if not all(req in have for req in REQUIRES[stage]):
                self.stats["rejected:out_of_order:%s" % stage] += 1
                return False
            return self._insert_event(conn, candidate_id, stage, attempt, at_utc,
                                      reason_codes, payload)
        return bool(self._run(fn))

    _LINKS = ("signal_id", "intent_id", "risk_decision_id", "outcome_id")

    def link(self, candidate_id: str, **links: Any) -> bool:
        bad = [k for k in links if k not in self._LINKS]
        if bad or not links:
            self.stats["invalid:link"] += 1
            return False

        def fn(conn):
            row = conn.execute("SELECT %s FROM candidate_ledger WHERE candidate_id=?"
                               % ",".join(self._LINKS), (candidate_id,)).fetchone()
            if row is None:
                self.stats["rejected:unknown_candidate"] += 1
                return False
            cur = dict(zip(self._LINKS, row))
            ok = True
            for k, v in links.items():
                if v is None:
                    continue
                if cur[k] is None:
                    conn.execute("UPDATE candidate_ledger SET %s=? WHERE candidate_id=? "
                                 "AND %s IS NULL" % (k, k), (v, candidate_id))
                    self.stats["link:%s" % k] += 1
                elif cur[k] != v:
                    self.stats["conflict:link:%s" % k] += 1
                    ok = False
            return ok
        return bool(self._run(fn))

    def close(self, candidate_id: str, terminal_kind: str, *, at_utc: str,
              reason_codes: Iterable[str] = (),
              payload: Optional[Mapping[str, Any]] = None) -> bool:
        if terminal_kind not in TERMINAL_KINDS:
            self.stats["invalid:terminal"] += 1
            return False

        def fn(conn):
            row = conn.execute("SELECT terminal_kind FROM candidate_ledger "
                               "WHERE candidate_id=?", (candidate_id,)).fetchone()
            if row is None:
                self.stats["rejected:unknown_candidate"] += 1
                return False
            if row[0] is not None:
                if row[0] == terminal_kind:
                    self.stats["idempotent:closed"] += 1
                    return True
                self.stats["conflict:terminal"] += 1
                return False
            body = dict(payload or {})
            body["terminal_kind"] = terminal_kind
            if not self._insert_event(conn, candidate_id, "closed", 0, at_utc,
                                      reason_codes, body):
                return False
            conn.execute("UPDATE candidate_ledger SET terminal_kind=?, terminal_at_utc=? "
                         "WHERE candidate_id=? AND terminal_kind IS NULL",
                         (terminal_kind, str(at_utc), candidate_id))
            return True
        return bool(self._run(fn))


# ── payload helpers (plain, bounded, no secrets) ──

_RISK_CHECK_KEYS = ("verdict", "market_hours", "position_size_usd", "position_size_ok",
                    "open_positions", "max_positions_ok", "duplicate_ok",
                    "daily_loss_ok", "open_trade_count", "max_open_trades_ok",
                    "symbol_exposure_pct", "symbol_exposure_ok", "sector",
                    "sector_exposure_ok", "streak_ok", "all_reasons",
                    "broker_conflict_ok", "combined_max_ok", "live_hard_cap_ok")


def risk_payload(passed: bool, reason: Optional[str], checks: Optional[Mapping]) -> dict:
    c = dict(checks or {})
    return {"verdict": "pass" if passed is True else "reject", "reason": reason,
            "checks": {k: c[k] for k in _RISK_CHECK_KEYS if k in c}}


def score_payload(truthful_score: Any) -> dict:
    """From an A01 ``TruthfulScore`` (duck-typed; no import of the bridge)."""
    sc, tr = truthful_score.scored, truthful_score.truth
    return {
        "m19_candidate_id": getattr(sc, "candidate_id", None),
        "decision_bucket": getattr(sc.decision_bucket, "value", str(sc.decision_bucket)),
        "final_score_100": sc.final_score_100,
        "research_rank_score": truthful_score.research_rank_score,
        "hard_gate_failures": list(sc.hard_gate_failures),
        "execution_eligible": sc.execution_eligible is True,
        "unknown_inputs": list(tr.unknown_fields),
        "observations": tr.observation_states(),
        "risk_authority": tr.risk.state.value,
        "risk_decision_id": tr.risk.decision_id,
        "corporate_action": tr.corporate_action.state.value,
    }
