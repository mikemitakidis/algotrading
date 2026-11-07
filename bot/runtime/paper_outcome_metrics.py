"""M21.1extra-D3 — read-only paper-trading outcome metrics.

Computes the dashboard metrics for ACTUAL trading outcomes of the IBKR paper
loop: trade count, win rate, average win / average loss, realised P&L and
drawdown — overall and per strategy — plus the mechanical lifecycle counts,
which are shown separately and never mixed into the edge metrics.

Honesty rules (enforced in code, covered by tests):
  * Only GENUINE edge outcomes count: rows of ``paper_trade_outcomes`` with
    ``is_edge_outcome = 1``, ``data_source`` in ``GENUINE_DATA_SOURCES`` (real
    broker paper fills), a closed exit and a numeric realised P&L. Simulated /
    fixture / unknown-source rows are counted as ``excluded_non_genuine`` and
    never enter a metric.
  * Mechanical immediate-flatten lifecycles (``paper_lifecycles``,
    record_kind='mechanical_paper_lifecycle', is_edge_outcome=0) are NOT trades
    with an edge outcome; they are reported in their own section.
  * Where no genuine data exists a metric is ``None`` and rendered ``"N/A"`` —
    never 0, never a fabricated number.

Read-only by construction: the database is opened with an SQLite ``mode=ro``
URI, so a write is impossible even by accident; a missing database or table is
reported as "no data" rather than created. This module imports no broker,
order, gateway or Telegram code and places no orders.

Data contract (written by the paper learning loop, read here):

  paper_trade_outcomes(trade_id TEXT PK, strategy_id TEXT, symbol TEXT,
      direction TEXT, qty REAL, entry_price REAL, exit_price REAL,
      entry_filled_at_utc TEXT, exit_filled_at_utc TEXT, exit_reason TEXT,
      gross_pnl REAL, fees REAL, realized_pnl REAL, data_source TEXT,
      record_kind TEXT, is_edge_outcome INTEGER, ...)
  paper_equity_snapshots(snapshot_id TEXT PK, as_of_utc TEXT,
      net_liquidation REAL, currency TEXT, data_source TEXT, ...)
  paper_lifecycles(...)  -- M21.1extra-C, mechanical records

Every column is optional from the reader's point of view: a missing table or
column yields N/A, not an error and not a guess.
"""
from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote

from bot.templates.ids import template_of

# Real broker-paper fills only. Simulated / fixture sources never count.
GENUINE_DATA_SOURCES = frozenset({"ibkr_paper"})
UNATTRIBUTED = "unattributed"
NA = "N/A"
LATEST_LIFECYCLES = 10

_OUTCOMES = "paper_trade_outcomes"
_EQUITY = "paper_equity_snapshots"
_LIFECYCLES = "paper_lifecycles"


def default_db_path() -> str:
    """Same resolution as M21.1extra-C's persister (one paper-loop database)."""
    env = os.environ.get("PAPER_LIFECYCLES_DB_PATH")
    if env:
        return env
    return str(Path(__file__).resolve().parents[2] / "data" / "paper_lifecycles.db")


# ── read-only access ────────────────────────────────────────────────────────

def _connect_ro(db_path: str) -> Optional[sqlite3.Connection]:
    """Open strictly read-only. Returns None if the file does not exist (a
    read-only open never creates it)."""
    if not Path(db_path).is_file():
        return None
    uri = "file:%s?mode=ro" % quote(str(Path(db_path).resolve()))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    return [r[1] for r in rows]


def _table_rows(conn: Optional[sqlite3.Connection], table: str) -> Optional[List[Dict[str, Any]]]:
    """All rows of ``table`` as dicts, or None when the table does not exist."""
    if conn is None:
        return None
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists:
        return None
    return [dict(r) for r in conn.execute("SELECT * FROM %s" % table).fetchall()]


# ── pure metric maths (no I/O) ──────────────────────────────────────────────

def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def is_genuine_outcome(row: Dict[str, Any]) -> bool:
    """A row counts only if it is a closed, real-broker edge outcome."""
    return (row.get("is_edge_outcome") in (1, True)
            and row.get("data_source") in GENUINE_DATA_SOURCES
            and bool(row.get("exit_filled_at_utc"))
            and _num(row.get("realized_pnl")) is not None)


def max_drawdown(pnls_in_order: Sequence[float]) -> Optional[float]:
    """Largest peak-to-trough fall of the cumulative realised-P&L curve, which
    starts at 0 before the first trade. Returned as a positive amount (0.0 if
    the curve never falls). None when there are no trades."""
    if not pnls_in_order:
        return None
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for p in pnls_in_order:
        equity += p
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return round(worst, 10)


def edge_metrics(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Metrics over genuine outcome rows (callers pass only genuine rows).
    Every value is None when it cannot be computed from real data."""
    ordered = sorted(rows, key=lambda r: (str(r.get("exit_filled_at_utc")), str(r.get("trade_id"))))
    pnls = [_num(r.get("realized_pnl")) for r in ordered]
    pnls = [p for p in pnls if p is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    total = sum(pnls) if n else None
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": n - len(wins) - len(losses),
        "win_rate": (len(wins) / n) if n else None,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "total_pnl": total,
        "expectancy": (total / n) if n else None,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses else None,
        "max_drawdown": max_drawdown(pnls),
        "first_exit_utc": ordered[0].get("exit_filled_at_utc") if n else None,
        "last_exit_utc": ordered[-1].get("exit_filled_at_utc") if n else None,
    }


def equity_metrics(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Broker-equity view from genuine account snapshots (net liquidation)."""
    pts = sorted(((str(r.get("as_of_utc")), _num(r.get("net_liquidation")), r.get("currency"))
                  for r in rows
                  if r.get("data_source") in GENUINE_DATA_SOURCES
                  and _num(r.get("net_liquidation")) is not None and r.get("as_of_utc")),
                 key=lambda t: t[0])
    if not pts:
        return {"snapshots": 0, "latest_equity": None, "currency": None,
                "peak_equity": None, "equity_drawdown": None,
                "equity_drawdown_pct": None, "as_of_utc": None}
    peak = pts[0][1]
    worst = 0.0
    worst_pct = 0.0
    for _, v, _ in pts:
        peak = max(peak, v)
        dd = peak - v
        if dd > worst:
            worst = dd
            worst_pct = (dd / peak) if peak > 0 else 0.0
    return {"snapshots": len(pts), "latest_equity": pts[-1][1], "currency": pts[-1][2],
            "peak_equity": max(v for _, v, _ in pts), "equity_drawdown": worst,
            "equity_drawdown_pct": worst_pct, "as_of_utc": pts[-1][0]}


def mechanical_summary(rows: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    rows = rows or []
    ordered = sorted(rows, key=lambda r: str(r.get("persisted_at_utc")))
    latest = [{"symbol": r.get("symbol"),
               "persisted_at_utc": r.get("persisted_at_utc"),
               "lifecycle_confirmed": bool(r.get("lifecycle_confirmed")),
               "flatten_confirmed": bool(r.get("flatten_confirmed"))}
              for r in reversed(ordered[-LATEST_LIFECYCLES:])]
    return {
        "record_kind": "mechanical_paper_lifecycle",
        "note": "immediate-flatten mechanics checks, not trades with a win/loss outcome",
        "total": len(rows),
        "lifecycle_confirmed": sum(1 for r in rows if r.get("lifecycle_confirmed")),
        "flatten_confirmed": sum(1 for r in rows if r.get("flatten_confirmed")),
        "not_confirmed": sum(1 for r in rows if not r.get("lifecycle_confirmed")),
        "latest": latest,
    }


# ── public entry point ──────────────────────────────────────────────────────

def compute_paper_metrics(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Read-only snapshot of all D3 metrics. Never raises on missing data;
    a corrupt/unreadable database is reported in ``error`` with N/A metrics."""
    db_path = db_path or default_db_path()
    result: Dict[str, Any] = {"db_present": False, "error": None,
                              "genuine_data_sources": sorted(GENUINE_DATA_SOURCES)}
    outcomes = equity = lifecycles = None
    try:
        conn = _connect_ro(db_path)
        if conn is not None:
            result["db_present"] = True
            try:
                outcomes = _table_rows(conn, _OUTCOMES)
                equity = _table_rows(conn, _EQUITY)
                lifecycles = _table_rows(conn, _LIFECYCLES)
            finally:
                conn.close()
    except sqlite3.Error as exc:                     # unreadable: report, do not guess
        result["error"] = "database_unreadable:%s" % type(exc).__name__
        outcomes = equity = lifecycles = None

    all_rows = outcomes or []
    genuine = [r for r in all_rows if is_genuine_outcome(r)]
    by_strategy: Dict[str, List[Dict[str, Any]]] = {}
    by_template: Dict[str, List[Dict[str, Any]]] = {}
    for r in genuine:
        by_strategy.setdefault(r.get("strategy_id") or UNATTRIBUTED, []).append(r)
        # M21 WP8: per template (A05/A06); parameter variants of one template pool here
        by_template.setdefault(template_of(r.get("strategy_id")) or UNATTRIBUTED, []).append(r)

    result.update({
        "outcomes_table_present": outcomes is not None,
        "edge_outcomes": {
            "overall": edge_metrics(genuine),
            "by_strategy": {k: edge_metrics(v) for k, v in sorted(by_strategy.items())},
            "by_template": {k: edge_metrics(v) for k, v in sorted(by_template.items())},
            "rows_total": len(all_rows),
            "rows_genuine": len(genuine),
            "excluded_non_genuine": len(all_rows) - len(genuine),
        },
        "broker_equity": equity_metrics(equity or []),
        "mechanical_lifecycles": mechanical_summary(lifecycles),
    })
    return result


def display(value: Any, kind: str = "num") -> str:
    """Render a metric for humans; None -> 'N/A' (never 0)."""
    if value is None:
        return NA
    if kind == "pct":
        return "%.1f%%" % (100.0 * value)
    if kind == "int":
        return "%d" % value
    if kind == "money":
        return "%+.2f" % value
    return "%.2f" % value
