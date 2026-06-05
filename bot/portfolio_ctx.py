"""bot/portfolio_ctx.py — gather PortfolioRiskContext fields without
duplicating broker round-trips (audit P0-4, 2026-06-05).

Background
──────────
Before P0-4, `main.py` constructed `PortfolioRiskContext` with only
`broker`, `mode`, `portfolio_value`, `sector_map`, `daily_state`,
`persistent_state` — leaving `positions`, `open_orders`,
`local_open_intents`, `kill_switch_active`, and `warnings` at their
empty defaults. `PortfolioRiskPolicy` gates that depend on those
fields (`_count_open_trades`, `_calc_symbol_exposure`,
`_calc_sector_exposure`) were running blind: they computed exposure
against empty position/order lists and consequently never blocked.

The audit P0-4 fix populates those fields. The audit's Correction
B forbids a second IBKR network round-trip per signal — so live
mode REUSES the reconciliation `RiskManager.evaluate()` already
performs and stashes into `intent.risk_checks['_recon']`.

Public API
──────────
`gather(broker_name, intent, conn)` returns a dict with the keys
`positions`, `open_orders`, `local_open_intents`,
`kill_switch_active`. Pure read-only; never raises.

Data source per broker
──────────────────────
* `ibkr_live` (and `ibkr_paper` if reconcile was performed):
    Reuse `intent.risk_checks['_recon']` if present (single live-mode
    reconcile already paid for by RiskManager.evaluate). If absent
    (paper mode where RiskManager did not reconcile), fall back to
    the local-DB path below.
* paper / etoro_paper / fallback:
    `positions` and `open_orders` derived from `execution_intents`
    table — rows with `status IN ('accepted', 'paper_logged')` are
    treated as paper "positions" (best effort; paper has no real
    broker state to read).

Hard constraints
────────────────
* Zero new network calls. The live-mode reconciliation is reused
  from RiskManager's stash; paper modes only query the local SQLite.
* No mutation of `intent`, `conn`, or any global.
* Returns conservative empty lists on any error — fail-soft for the
  scanner hot path (the existing RiskManager + bot.kill_switch
  defenses are still in effect; this helper only enriches the M14
  portfolio layer).

This module imports NOTHING from broker submit paths or live-write
code. It only reads from intent.risk_checks (already populated),
bot.kill_switch (file-based read), and the local signals.db.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)

# Synthetic test signal IDs that bot/risk.py also excludes — kept in
# sync intentionally; future P2 cleanup will replace these with an
# is_test column.
_TEST_SIGNAL_IDS = (888888, 999999)


# Recon-stash key written by RiskManager.evaluate() in live mode.
# Must match the literal used in bot/risk.py.
RECON_STASH_KEY = "_recon"


def _local_intents_from_db(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Read accepted/paper_logged execution_intents from SQLite,
    returning dicts shaped for PortfolioRiskPolicy._count_open_trades
    and ._calc_symbol_exposure / ._calc_sector_exposure.

    Required keys: symbol, position_size, entry_price.
    Returns [] on any error.
    """
    if conn is None:
        return []
    placeholders = ",".join("?" * len(_TEST_SIGNAL_IDS))
    sql = (
        f"SELECT symbol, direction, position_size, entry_price, signal_id "
        f"FROM execution_intents "
        f"WHERE status IN ('accepted', 'paper_logged') "
        f"AND signal_id NOT IN ({placeholders})"
    )
    try:
        rows = conn.execute(sql, _TEST_SIGNAL_IDS).fetchall()
    except sqlite3.Error as e:
        log.warning("[portfolio_ctx] local intent query failed: %s", e)
        return []
    return [
        {
            "symbol":         r[0] or "",
            "direction":      r[1] or "",
            "position_size":  float(r[2] or 0),
            "entry_price":    float(r[3] or 0),
            "signal_id":      r[4],
        }
        for r in rows
    ]


def _paper_positions_from_intents(intents: List[Dict[str, Any]]
                                    ) -> List[Dict[str, Any]]:
    """Derive a positions-list shape from local accepted intents
    when no real broker reconcile is available.

    PortfolioRiskPolicy._calc_symbol_exposure reads `symbol`,
    `position`, `market_value`, `avg_cost` from each entry. For
    paper there is no broker market_value; we leave it None so the
    `estimated=True` fallback path inside PortfolioRiskPolicy fires
    correctly (existing logic that already handles the no-MV case).
    """
    out: List[Dict[str, Any]] = []
    for it in intents:
        sym = it.get("symbol", "")
        if not sym:
            continue
        size = float(it.get("position_size") or 0)
        price = float(it.get("entry_price") or 0)
        if size == 0:
            continue
        out.append({
            "symbol":       sym,
            "position":     size,
            "avg_cost":     price,
            "market_value": None,
        })
    return out


def _read_kill_switch_active() -> bool:
    """Defensive read of the file-based kill switch.

    Failures fail-safe to True — same policy as bot.kill_switch
    itself. This field is NOT currently gated on by
    PortfolioRiskPolicy.evaluate, but the dataclass exposes it and
    future dashboard/audit consumers may read it. We populate it
    consistently with the same fail-safe semantics the rest of the
    system uses.
    """
    try:
        from bot.kill_switch import is_kill_switch_active
        return bool(is_kill_switch_active())
    except Exception as e:
        log.warning("[portfolio_ctx] kill_switch read failed "
                     "(treating as active): %s", e)
        return True


def gather(broker_name: str, intent: Any,
            conn: sqlite3.Connection) -> Dict[str, Any]:
    """Build the four ctx-enrichment fields without any extra
    network call.

    Returns a dict with keys: positions, open_orders,
    local_open_intents, kill_switch_active.

    Parameters
    ──────────
    broker_name : str
        From `broker.name` — e.g. 'paper', 'ibkr_live',
        'ibkr_paper', 'etoro_paper'. Used to select live-vs-paper
        data source.
    intent : OrderIntent
        The current intent. Read-only access to `risk_checks` for
        the live-mode reconcile stash.
    conn : sqlite3.Connection
        Existing scanner DB connection (signals.db). Read-only.
    """
    out: Dict[str, Any] = {
        "positions":          [],
        "open_orders":        [],
        "local_open_intents": [],
        "kill_switch_active": _read_kill_switch_active(),
    }

    # Always populate local_open_intents — applicable to both paper
    # and live paths (PortfolioRiskPolicy consumes it to avoid
    # double-counting cross-broker open intents).
    local_intents = _local_intents_from_db(conn)
    out["local_open_intents"] = local_intents

    # Live IBKR path: RiskManager.evaluate already paid for the
    # reconcile. Reuse the stash. Per Correction B, NEVER trigger a
    # second reconcile from here.
    risk_checks = getattr(intent, "risk_checks", None) or {}
    recon = risk_checks.get(RECON_STASH_KEY)
    if isinstance(recon, dict):
        out["positions"]   = list(recon.get("positions", []) or [])
        out["open_orders"] = list(recon.get("open_orders", []) or [])
        return out

    # Paper / eToro paper / IBKR-paper-without-recon-stash: derive
    # a synthetic position list from local accepted intents.
    out["positions"]   = _paper_positions_from_intents(local_intents)
    out["open_orders"] = []
    return out


__all__ = ["gather", "RECON_STASH_KEY"]
