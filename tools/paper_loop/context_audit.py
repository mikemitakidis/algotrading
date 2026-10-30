"""M21 E2 — market-context shadow record for every WS2 paper cycle.

Connects the WP15 point-in-time market context to the WS2 cycle: for one
cycle key (the same ``scan:<decision time>`` key the candidate ledger uses for
the scan and scoring records), it builds the context knowable at the decision
time, runs the reduce-only shadow throttle, and appends ONE record to a shadow
table in the WP15 context store.

* Shadow only. The record states ``can_grant_authority: false`` and
  ``enforced: false``. Nothing here returns anything the loop could act on;
  the WS2 tool calls it after the candidates are fixed and ignores any failure.
* Every feature carries its status. UNAVAILABLE and STALE features are listed
  explicitly with their reason; no feature is defaulted.
* Point-in-time: the context store only shows values published by the
  decision time, and daily bars enter only as COMPLETED sessions re-stamped at
  their real close by the WP6 completed-bar policy (a forming bar, a bar
  outside the regular session or a bar the policy cannot classify is dropped
  and counted, never used).
* Idempotent: one record per cycle key (``INSERT OR IGNORE``); a restarted
  tick never rewrites what was knowable the first time.
* OFF by default: the WS2 tool builds the audit only when
  ``MARKET_CONTEXT_SHADOW_ENABLED=1``.

No broker, order, network or production-database code lives here. The only
database is the shadow context store passed in (``data/market_context.db`` in
the WS2 tool).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

import pandas as pd

from bot.market_context.features import STALE, UNAVAILABLE, build_context
from bot.market_context.sources import SECTOR_ETFS, parse_event_calendar
from bot.market_context.store import ContextStore
from bot.market_context.throttle import decide

FLAG = "MARKET_CONTEXT_SHADOW_ENABLED"
SCHEMA = "m21-e2-cycle-context/1"
INDEX_SYMBOLS = ("SPY", "QQQ")
CONTEXT_SYMBOLS = INDEX_SYMBOLS + tuple(SECTOR_ETFS)

_DDL = """CREATE TABLE IF NOT EXISTS context_cycle_audit (
    cycle_key TEXT PRIMARY KEY, decision_time_utc TEXT NOT NULL, record_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL)"""


def enabled_from_env(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(FLAG, "")).strip() == "1"


def close_stamped_daily(df: Any, classify: Callable[[str, Any], Tuple[str, Any]]
                        ) -> Tuple[Optional[pd.DataFrame], Dict[str, int]]:
    """Provider daily bars (index = bar START) -> COMPLETED bars stamped at their
    real session close: ``ts_utc`` = close, ``bar_open_ts`` = start, ``close``.
    Rows the policy calls forming, outside or unknown are dropped and counted."""
    counts = {"complete": 0, "forming": 0, "outside": 0, "unknown": 0}
    if df is None or len(df) == 0:
        return None, counts
    col = "close" if "close" in df.columns else "Close" if "Close" in df.columns else None
    if col is None:
        counts["unknown"] = int(len(df))
        return None, counts
    rows = []
    for start, close_px in zip(df.index, df[col]):
        try:
            verdict, close = classify("1D", start)
        except Exception:
            verdict, close = "unknown", None
        if verdict != "complete" or close is None:
            counts[verdict if verdict in counts else "unknown"] += 1
            continue
        counts["complete"] += 1
        rows.append((pd.Timestamp(close).tz_convert("UTC") if pd.Timestamp(close).tzinfo else
                     pd.Timestamp(close, tz="UTC"), pd.Timestamp(start), float(close_px)))
    if not rows:
        return None, counts
    out = pd.DataFrame(rows, columns=["ts_utc", "bar_open_ts", "close"]).sort_values("ts_utc")
    return out.drop_duplicates("ts_utc", keep="first").reset_index(drop=True), counts


class CycleContextAudit:
    def __init__(self, store_path: str, *, events_path: Optional[str] = None):
        if not store_path:
            raise ValueError("a context store path is required")
        self.store = ContextStore(store_path)
        self.path = store_path
        self.events_path = events_path
        c = sqlite3.connect(store_path, timeout=10)
        try:
            c.execute(_DDL)
            c.commit()
        finally:
            c.close()

    def _events(self):
        """(events or None, source note). A missing or unreadable file makes the
        event calendar UNAVAILABLE (explicit), never an empty calendar."""
        if not self.events_path:
            return None, "no_event_calendar_configured"
        p = Path(self.events_path)
        if not p.exists():
            return None, "event_calendar_file_missing"
        try:
            return parse_event_calendar(p.read_text()), "operator_file:%s" % p.name
        except (ValueError, KeyError, TypeError) as exc:
            return None, "event_calendar_unreadable:%s" % type(exc).__name__

    def record(self, *, cycle_key: str, now: datetime, index_bars: Mapping[str, pd.DataFrame],
               sector_bars: Mapping[str, pd.DataFrame], universe_bars: Mapping[str, pd.DataFrame],
               candidates: Iterable[Tuple[str, str]] = (), news: Any = None,
               bar_counts: Optional[Mapping[str, Mapping[str, int]]] = None,
               calendar_verified: bool = True) -> Dict[str, Any]:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if not str(cycle_key or "").strip():
            raise ValueError("cycle_key is required")
        events, events_note = self._events()
        ctx = build_context(self.store, now, index_bars=index_bars, sector_bars=sector_bars,
                            universe_bars=universe_bars, events=events, news=news)
        dec = decide(ctx)
        feats = {k: {"value": v.value, "status": v.status, "as_of": v.as_of, "source": v.source,
                     "detail": v.detail} for k, v in sorted(ctx.items())}
        rec = {
            "schema": SCHEMA, "cycle_key": cycle_key, "decision_time_utc": now.isoformat(),
            "shadow_only": True, "enforced": False, "can_grant_authority": dec.can_grant_authority,
            "features": feats,
            "unavailable": {k: v.detail for k, v in sorted(ctx.items()) if v.status == UNAVAILABLE},
            "stale": sorted(k for k, v in ctx.items() if v.status == STALE),
            "shadow_decision": {"multiplier": dec.multiplier, "abstain": dec.abstain,
                                "reasons": list(dec.reasons)},
            "would_apply_to": [{"symbol": s, "direction": d, "would_block": dec.abstain,
                                "would_size_multiplier": 0.0 if dec.abstain else dec.multiplier}
                               for s, d in candidates],
            "events_source": events_note, "calendar_verified": bool(calendar_verified),
            "bar_counts": {k: dict(v) for k, v in sorted((bar_counts or {}).items())},
        }
        body = json.dumps(rec, sort_keys=True, default=str)
        sha = hashlib.sha256(body.encode()).hexdigest()
        c = sqlite3.connect(self.path, timeout=10)
        try:
            cur = c.execute("INSERT OR IGNORE INTO context_cycle_audit (cycle_key, decision_time_utc, record_json, "
                            "record_sha256) VALUES (?,?,?,?)", (cycle_key, now.isoformat(), body, sha))
            c.commit()
            recorded = cur.rowcount == 1
        finally:
            c.close()
        return {"recorded": recorded, "abstain": dec.abstain, "multiplier": dec.multiplier,
                "unavailable": len(rec["unavailable"]), "stale": len(rec["stale"]), "sha256": sha}

    def get(self, cycle_key: str) -> Optional[Dict[str, Any]]:
        c = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True)
        try:
            r = c.execute("SELECT record_json, record_sha256 FROM context_cycle_audit WHERE cycle_key=?",
                          (cycle_key,)).fetchone()
        finally:
            c.close()
        if r is None:
            return None
        if hashlib.sha256(r[0].encode()).hexdigest() != r[1]:
            raise ValueError("context record for %s does not match its digest" % cycle_key)
        return json.loads(r[0])


def record_cycle(audit: CycleContextAudit, *, cycle_key: str, now: datetime,
                 daily_by_symbol: Mapping[str, Any], classify: Optional[Callable], focus: Iterable[str],
                 candidates: Iterable[Tuple[str, str]] = ()) -> Dict[str, Any]:
    """WS2 tool entry point: re-stamp the fetched daily bars to completed closes,
    then record the cycle's context. ``daily_by_symbol`` holds provider frames
    for the index and sector ETFs and the focus universe."""
    stamped, counts = {}, {}
    for sym, df in daily_by_symbol.items():
        if classify is None:            # no verified session calendar: no bar can be proven complete
            stamped[sym] = None
            counts[sym] = {"complete": 0, "forming": 0, "outside": 0,
                           "unknown": int(len(df)) if df is not None else 0}
        else:
            stamped[sym], counts[sym] = close_stamped_daily(df, classify)
    focus = list(focus)
    return audit.record(
        cycle_key=cycle_key, now=now,
        index_bars={s: stamped.get(s) for s in INDEX_SYMBOLS},
        sector_bars={s: stamped.get(s) for s in SECTOR_ETFS},
        universe_bars={s: stamped.get(s) for s in focus},
        candidates=list(candidates),
        bar_counts={s: counts[s] for s in sorted(counts) if s in CONTEXT_SYMBOLS},
        calendar_verified=classify is not None)


__all__ = ["FLAG", "SCHEMA", "INDEX_SYMBOLS", "CONTEXT_SYMBOLS", "enabled_from_env", "close_stamped_daily",
           "CycleContextAudit", "record_cycle"]
