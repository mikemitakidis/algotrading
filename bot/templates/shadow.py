"""M21 WP8 — shadow record for template signals (no orders, no broker).

``ShadowStore`` appends each template verdict (entry or rejected setup) on a
completed bar to a SQLite table at a CALLER-SUPPLIED path. One row per
(strategy id, symbol, decision time): re-evaluating the same completed bar on
a later loop cycle is a no-op, so the shadow record never double counts.

``shadow_enabled_from_env`` is OFF unless ``TEMPLATE_SHADOW_ENABLED=1``.
Shadow outcomes (what the signal would have earned) are labelled later from
bars by the research evidence engine and by the WP14 shadow runner; this
module only records what each template said and when.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List, Optional

from bot.templates.core import TemplateSignal

_DDL = """CREATE TABLE IF NOT EXISTS template_shadow_signals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    template_id TEXT NOT NULL,
    template_version INTEGER NOT NULL,
    params_digest TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    decision_ts_utc TEXT NOT NULL,
    entry INTEGER NOT NULL,
    setup INTEGER NOT NULL,
    reject_reason TEXT NOT NULL,
    entry_ref_price REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    atr REAL,
    score REAL,
    digest TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    UNIQUE (strategy_id, symbol, decision_ts_utc))"""

_COLS = ("strategy_id", "template_id", "template_version", "params_digest", "symbol", "direction",
         "decision_ts_utc", "entry", "setup", "reject_reason", "entry_ref_price", "stop_loss", "take_profit",
         "atr", "score", "digest")


def shadow_enabled_from_env() -> bool:
    return os.getenv("TEMPLATE_SHADOW_ENABLED", "").strip() == "1"


class ShadowStore:
    def __init__(self, path: str):
        if not path:
            raise ValueError("a shadow store path is required")
        self.path = path
        c = sqlite3.connect(path, timeout=10)
        try:
            c.execute(_DDL)
            c.commit()
        finally:
            c.close()

    def record(self, sig: TemplateSignal, *, now: datetime) -> bool:
        """True when the row is new; False when this verdict was already recorded."""
        d = asdict(sig)
        vals = [int(d[k]) if k in ("entry", "setup") else d[k] for k in _COLS]
        c = sqlite3.connect(self.path, timeout=10)
        try:
            cur = c.execute("INSERT OR IGNORE INTO template_shadow_signals (%s, recorded_at_utc) VALUES (%s)"
                            % (", ".join(_COLS), ", ".join("?" * (len(_COLS) + 1))), vals + [now.isoformat()])
            c.commit()
            return cur.rowcount == 1
        finally:
            c.close()

    def rows(self, template_id: Optional[str] = None) -> List[Dict]:
        c = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=10)
        try:
            c.row_factory = sqlite3.Row
            q = "SELECT * FROM template_shadow_signals"
            rs = (c.execute(q + " WHERE template_id=? ORDER BY seq", (template_id,)) if template_id
                  else c.execute(q + " ORDER BY seq")).fetchall()
            return [dict(r) for r in rs]
        finally:
            c.close()


__all__ = ["shadow_enabled_from_env", "ShadowStore"]
