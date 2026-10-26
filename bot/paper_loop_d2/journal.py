"""M21.1extra-D2.3/D2.4 + D5 — crash-safe journal for the paper learning loop.

One SQLite database (by default the paper-loop DB that M21.1extra-C already
uses and the D3 dashboard reads) holds:

  loop_cycles             one row per scheduler slot (cycle_key PK):
                          RUNNING -> DONE | INTERRUPTED
  loop_orders             one row per planned entry (client_ref PK), written
                          BEFORE the broker is called (write-ahead):
                          PENDING_SUBMIT -> SUBMITTED -> OPEN -> CLOSED
                                        \\-> REJECTED | NOT_SUBMITTED | NEEDS_REVIEW
  paper_trade_outcomes    closed trades with realised P&L (edge outcomes)
  paper_equity_snapshots  broker net-liquidation per cycle

Idempotency (finding R2-4 / D5):
  * a slot's cycle_key is a primary key, so the same slot never runs twice;
  * client_ref = "<cycle_key>|<symbol>" is a primary key, so an entry is
    planned at most once;
  * a partial UNIQUE index allows at most ONE active order per symbol
    (PENDING_SUBMIT / SUBMITTED / OPEN / NEEDS_REVIEW);
  * every state change is compare-and-set on the expected previous state;
  * closing a trade and writing its outcome happen in one transaction, and
    outcomes are keyed by trade_id (INSERT OR IGNORE).

Ownership (correction batch 1/2/5):
  * every planned entry gets a broker-visible ``order_ref`` (journal id +
    client_ref, UNIQUE) BEFORE submission; the broker legs' order / perm ids,
    contract id and account are stored as soon as they are known;
  * ``loop_exec_claims`` records which trade each broker execution was
    attributed to (exec_id PRIMARY KEY), written in the same transaction as
    the state change that used it — an execution can never count twice;
  * PENDING_SUBMIT rows track complete-lookup absence checks, so NOT_SUBMITTED
    is only ever concluded from repeated COMPLETE broker evidence.

The journal never talks to a broker; the scheduler does.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

ACTIVE_STATES = ("PENDING_SUBMIT", "SUBMITTED", "OPEN", "NEEDS_REVIEW")
TERMINAL_STATES = ("CLOSED", "REJECTED", "NOT_SUBMITTED")
EDGE_RECORD_KIND = "edge_paper_trade"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS loop_cycles (
  cycle_key TEXT PRIMARY KEY,
  started_at_utc TEXT NOT NULL,
  finished_at_utc TEXT,
  state TEXT NOT NULL,
  outcome TEXT,
  reason TEXT,
  pid INTEGER,
  details_json TEXT
);
CREATE TABLE IF NOT EXISTS loop_orders (
  client_ref TEXT PRIMARY KEY,
  cycle_key TEXT NOT NULL,
  strategy_id TEXT,
  symbol TEXT NOT NULL,
  direction TEXT NOT NULL,
  qty REAL NOT NULL,
  entry_ref_price REAL,
  stop_loss REAL,
  take_profit REAL,
  score REAL,
  state TEXT NOT NULL,
  data_source TEXT NOT NULL,
  broker_order_id TEXT,
  broker_perm_id TEXT,
  submit_status TEXT,
  risk_decision_id TEXT,
  equity_at_entry REAL,
  planned_at_utc TEXT NOT NULL,
  submitted_at_utc TEXT,
  entry_fill_price REAL,
  entry_filled_at_utc TEXT,
  entry_fees REAL,
  exit_fill_price REAL,
  exit_filled_at_utc TEXT,
  exit_reason TEXT,
  exit_fees REAL,
  time_stop_requested_at_utc TEXT,
  note TEXT,
  updated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loop_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loop_exec_claims (
  exec_id TEXT PRIMARY KEY,
  client_ref TEXT NOT NULL,
  leg TEXT NOT NULL,
  claimed_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_trade_outcomes (
  trade_id TEXT PRIMARY KEY,
  cycle_key TEXT,
  strategy_id TEXT,
  symbol TEXT,
  direction TEXT,
  qty REAL,
  entry_order_id TEXT,
  entry_perm_id TEXT,
  entry_price REAL,
  exit_price REAL,
  entry_filled_at_utc TEXT,
  exit_filled_at_utc TEXT,
  exit_reason TEXT,
  gross_pnl REAL,
  fees REAL,
  realized_pnl REAL,
  r_multiple REAL,
  data_source TEXT,
  record_kind TEXT,
  is_edge_outcome INTEGER,
  recorded_at_utc TEXT,
  raw_json TEXT
);
CREATE TABLE IF NOT EXISTS paper_equity_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  cycle_key TEXT,
  as_of_utc TEXT,
  account TEXT,
  net_liquidation REAL,
  cash REAL,
  currency TEXT,
  data_source TEXT
);
"""


# Columns added by the ownership correction (also migrated onto older DBs).
_ADDED_COLUMNS = (
    ("order_ref", "TEXT"), ("account", "TEXT"), ("con_id", "TEXT"), ("tp_order_id", "TEXT"),
    ("tp_perm_id", "TEXT"), ("sl_order_id", "TEXT"), ("sl_perm_id", "TEXT"),
    ("flatten_order_id", "TEXT"), ("flatten_perm_id", "TEXT"),
    ("absence_checks", "INTEGER NOT NULL DEFAULT 0"), ("last_lookup_at_utc", "TEXT"),
    ("last_lookup_note", "TEXT"),
)
_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS loop_orders_one_active_per_symbol
  ON loop_orders(symbol)
  WHERE state IN ('PENDING_SUBMIT','SUBMITTED','OPEN','NEEDS_REVIEW');
CREATE UNIQUE INDEX IF NOT EXISTS loop_orders_order_ref
  ON loop_orders(order_ref) WHERE order_ref IS NOT NULL;
"""


class JournalConflict(RuntimeError):
    """A compare-and-set transition did not match the expected state."""


def _utc(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(timezone.utc).isoformat()


def default_db_path() -> str:
    """Same file as M21.1extra-C's persister and the D3 reader."""
    import os
    env = os.environ.get("PAPER_LIFECYCLES_DB_PATH")
    if env:
        return env
    return str(Path(__file__).resolve().parents[2] / "data" / "paper_lifecycles.db")


class LoopJournal:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            have = {r["name"] for r in c.execute("PRAGMA table_info(loop_orders)")}
            for col, decl in _ADDED_COLUMNS:
                if col not in have:
                    c.execute("ALTER TABLE loop_orders ADD COLUMN %s %s" % (col, decl))
            c.executescript(_INDEXES)
            c.execute("INSERT OR IGNORE INTO loop_meta (key, value) VALUES ('journal_id', ?)",
                      (secrets.token_hex(4),))

    @property
    def journal_id(self) -> str:
        with self._conn() as c:
            return c.execute("SELECT value FROM loop_meta WHERE key='journal_id'").fetchone()[0]

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")

    # ── cycles ──
    def get_cycle(self, cycle_key: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM loop_cycles WHERE cycle_key=?", (cycle_key,)).fetchone()
            return dict(r) if r else None

    def begin_cycle(self, cycle_key: str, pid: int, now: datetime) -> bool:
        """True if this call created the cycle (the slot was never run)."""
        with self._tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO loop_cycles (cycle_key, started_at_utc, state, pid)"
                            " VALUES (?,?,?,?)", (cycle_key, _utc(now), "RUNNING", pid))
            return cur.rowcount == 1

    def mark_interrupted_cycles(self, is_alive, now: datetime) -> List[str]:
        """RUNNING cycles whose process is gone become INTERRUPTED."""
        out = []
        with self._tx() as c:
            for r in c.execute("SELECT cycle_key, pid FROM loop_cycles WHERE state='RUNNING'").fetchall():
                if not is_alive(int(r["pid"] or -1)):
                    c.execute("UPDATE loop_cycles SET state='INTERRUPTED', finished_at_utc=?,"
                              " reason='process_died_mid_cycle' WHERE cycle_key=? AND state='RUNNING'",
                              (_utc(now), r["cycle_key"]))
                    out.append(r["cycle_key"])
        return out

    def finish_cycle(self, cycle_key: str, outcome: str, reason: Optional[str],
                     details: Dict[str, Any], now: datetime) -> None:
        with self._tx() as c:
            cur = c.execute("UPDATE loop_cycles SET state='DONE', finished_at_utc=?, outcome=?,"
                            " reason=?, details_json=? WHERE cycle_key=? AND state='RUNNING'",
                            (_utc(now), outcome, reason, json.dumps(details, sort_keys=True, default=str),
                             cycle_key))
            if cur.rowcount != 1:
                raise JournalConflict("cycle %s not RUNNING" % cycle_key)

    def count_cycles_with_entries_on(self, day_prefix: str) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM loop_orders WHERE planned_at_utc LIKE ?"
                             " AND state NOT IN ('NOT_SUBMITTED','REJECTED')",
                             (day_prefix + "%",)).fetchone()[0]

    # ── orders ──
    def orders(self, states=None) -> List[Dict[str, Any]]:
        with self._conn() as c:
            if states:
                q = "SELECT * FROM loop_orders WHERE state IN (%s) ORDER BY planned_at_utc" % (
                    ",".join("?" * len(states)))
                rows = c.execute(q, tuple(states)).fetchall()
            else:
                rows = c.execute("SELECT * FROM loop_orders ORDER BY planned_at_utc").fetchall()
            return [dict(r) for r in rows]

    def get_order(self, client_ref: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM loop_orders WHERE client_ref=?", (client_ref,)).fetchone()
            return dict(r) if r else None

    def plan_order(self, *, client_ref: str, cycle_key: str, strategy_id: str, symbol: str,
                   direction: str, qty: float, entry_ref_price: float, stop_loss: float,
                   take_profit: float, score: float, data_source: str,
                   risk_decision_id: Optional[str], equity_at_entry: Optional[float],
                   now: datetime, order_ref: Optional[str] = None,
                   account: Optional[str] = None) -> None:
        """Write-ahead record BEFORE the broker is called, including the
        broker-visible order_ref. Raises sqlite3.IntegrityError if this
        client_ref / order_ref exists or the symbol already has an active order."""
        with self._tx() as c:
            c.execute("INSERT INTO loop_orders (client_ref, cycle_key, strategy_id, symbol, direction,"
                      " qty, entry_ref_price, stop_loss, take_profit, score, state, data_source,"
                      " risk_decision_id, equity_at_entry, planned_at_utc, updated_at_utc,"
                      " order_ref, account)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,'PENDING_SUBMIT',?,?,?,?,?,?,?)",
                      (client_ref, cycle_key, strategy_id, symbol, direction, qty, entry_ref_price,
                       stop_loss, take_profit, score, data_source, risk_decision_id,
                       equity_at_entry, _utc(now), _utc(now), order_ref, account))

    def transition(self, client_ref: str, expected: str, new: str, now: datetime,
                   claims: Optional[List[tuple]] = None, **fields: Any) -> None:
        """Compare-and-set state change. ``claims`` = [(exec_id, leg), ...]
        are recorded in the SAME transaction; an execution already claimed
        (by any trade) aborts the whole change."""
        sets = ["state=?", "updated_at_utc=?"] + ["%s=?" % k for k in fields]
        args = [new, _utc(now)] + list(fields.values()) + [client_ref, expected]
        with self._tx() as c:
            cur = c.execute("UPDATE loop_orders SET %s WHERE client_ref=? AND state=?" % ", ".join(sets),
                            args)
            if cur.rowcount != 1:
                raise JournalConflict("%s: expected state %s" % (client_ref, expected))
            self._claim(c, client_ref, claims, now)

    @staticmethod
    def _claim(c: sqlite3.Connection, client_ref: str, claims: Optional[List[tuple]],
               now: datetime) -> None:
        for exec_id, leg in claims or ():
            row = c.execute("SELECT client_ref FROM loop_exec_claims WHERE exec_id=?",
                            (exec_id,)).fetchone()
            if row is not None:
                if row["client_ref"] != client_ref:
                    raise JournalConflict("execution %s already attributed to %s"
                                          % (exec_id, row["client_ref"]))
                continue
            c.execute("INSERT INTO loop_exec_claims VALUES (?,?,?,?)",
                      (exec_id, client_ref, leg, _utc(now)))

    def exec_claims(self) -> Dict[str, str]:
        with self._conn() as c:
            return {r["exec_id"]: r["client_ref"]
                    for r in c.execute("SELECT exec_id, client_ref FROM loop_exec_claims")}

    def update_fields(self, client_ref: str, expected: str, now: datetime, **fields: Any) -> None:
        self.transition(client_ref, expected, expected, now, **fields)

    def close_with_outcome(self, client_ref: str, *, exit_price: float, exit_filled_at_utc: str,
                           exit_reason: str, exit_fees: float, now: datetime,
                           claims: Optional[List[tuple]] = None) -> Dict[str, Any]:
        """OPEN -> CLOSED, claim the exit executions and write the edge
        outcome, atomically."""
        with self._tx() as c:
            r = c.execute("SELECT * FROM loop_orders WHERE client_ref=?", (client_ref,)).fetchone()
            if r is None or r["state"] != "OPEN":
                raise JournalConflict("%s: expected OPEN" % client_ref)
            o = dict(r)
            sign = 1.0 if o["direction"] == "long" else -1.0
            gross = sign * (exit_price - o["entry_fill_price"]) * o["qty"]
            fees = (o["entry_fees"] or 0.0) + (exit_fees or 0.0)
            risk = abs(o["entry_fill_price"] - o["stop_loss"]) * o["qty"] if o["stop_loss"] else 0.0
            outcome = {
                "trade_id": client_ref, "cycle_key": o["cycle_key"], "strategy_id": o["strategy_id"],
                "symbol": o["symbol"], "direction": o["direction"], "qty": o["qty"],
                "entry_order_id": o["broker_order_id"], "entry_perm_id": o["broker_perm_id"],
                "entry_price": o["entry_fill_price"], "exit_price": exit_price,
                "entry_filled_at_utc": o["entry_filled_at_utc"],
                "exit_filled_at_utc": exit_filled_at_utc, "exit_reason": exit_reason,
                "gross_pnl": gross, "fees": fees, "realized_pnl": gross - fees,
                "r_multiple": (gross / risk) if risk > 0 else None,
                "data_source": o["data_source"], "record_kind": EDGE_RECORD_KIND,
                "is_edge_outcome": 1, "recorded_at_utc": _utc(now)}
            outcome["raw_json"] = json.dumps({"order": o, "outcome": dict(outcome)},
                                             sort_keys=True, default=str)
            c.execute("UPDATE loop_orders SET state='CLOSED', exit_fill_price=?, exit_filled_at_utc=?,"
                      " exit_reason=?, exit_fees=?, updated_at_utc=? WHERE client_ref=? AND state='OPEN'",
                      (exit_price, exit_filled_at_utc, exit_reason, exit_fees, _utc(now), client_ref))
            self._claim(c, client_ref, claims, now)
            c.execute("INSERT OR IGNORE INTO paper_trade_outcomes (%s) VALUES (%s)" % (
                ",".join(outcome), ",".join("?" * len(outcome))), list(outcome.values()))
            return outcome

    def outcomes(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM paper_trade_outcomes ORDER BY exit_filled_at_utc").fetchall()]

    # ── equity ──
    def record_equity(self, *, cycle_key: str, snap) -> None:
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO paper_equity_snapshots VALUES (?,?,?,?,?,?,?,?)",
                      ("%s|%s" % (cycle_key, snap.as_of_utc), cycle_key, snap.as_of_utc, snap.account,
                       snap.net_liquidation, snap.cash, snap.currency, snap.data_source))
