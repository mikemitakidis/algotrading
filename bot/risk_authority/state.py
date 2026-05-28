"""
bot/risk_authority/state.py — M14.B read helpers.

Per ChatGPT M14.B correction #2: this module is for NEW Risk Authority
code / tests ONLY. Existing readers (bot.flywheel.get_daily_state) remain
the source of truth for old callers and MUST NOT be redirected here.

M14.B introduces daily_state_per_broker as an additive table that, today,
holds only the one-time backfill of historical daily_state rows under
broker_scope='GLOBAL'. It is not yet fed by ingestion (M14.C/D/E).
Therefore reads against it can be stale by design until those milestones
ship; callers must understand the staleness contract.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_daily_state_compat(
    conn: sqlite3.Connection,
    today: Optional[str] = None,
) -> Optional[dict]:
    """Return today's GLOBAL row from daily_state_per_broker, in the SAME
    dict shape that bot.flywheel.get_daily_state returns.

    Returns None if no GLOBAL row exists for today (which is the expected
    state until M14.C/D/E starts producing rollups). Callers in new
    Risk-Authority code that need a value should:

      1. Call this helper.
      2. If None, the engine treats the metric as 'unknown' and fails
         closed (per the M14.A staleness contract).
      3. They must NOT silently fall back to a value of 0.

    This helper is read-only. It never inserts, never updates, never
    creates today's row. Old callers that want write-on-read semantics
    must continue to use bot.flywheel.get_daily_state.
    """
    day = today or _today_utc()
    # Defensive: if the new table is absent (migration not yet run), return
    # None rather than raising. Avoids breaking the engine during partial
    # deployments.
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='daily_state_per_broker'"
    ).fetchone()
    if not exists:
        return None
    row = conn.execute(
        "SELECT date, realised_pnl_usd, realised_pnl_pct, "
        "       daily_pnl_source, daily_pnl_available, "
        "       daily_loss_block_active, daily_loss_alert_sent "
        "FROM daily_state_per_broker "
        "WHERE date=? AND broker_scope='GLOBAL'",
        (day,),
    ).fetchone()
    if not row:
        return None
    return {
        "date":                    row[0],
        "realised_pnl_usd":        row[1],
        "realised_pnl_pct":        row[2],
        "daily_pnl_source":        row[3],
        "daily_pnl_available":     row[4],
        "daily_loss_block_active": row[5],
        "daily_loss_alert_sent":   row[6],
    }


__all__ = ["get_daily_state_compat"]
