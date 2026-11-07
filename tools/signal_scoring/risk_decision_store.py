#!/usr/bin/env python3
"""M21.1 A01 — trusted READ-ONLY lookup of persisted M14 risk decisions.

The scoring bridge accepts a Risk Authority approval only after reading the
decision back from the table M14's single audit surface writes
(``bot/risk_authority/audit_decisions.write_decision`` -> ``risk_decisions``).
A caller-built reference is a pointer, never evidence.

Read-only by construction:
  * the database is opened with an SQLite URI in ``mode=ro`` (no file is ever
    created; a missing file is "not found");
  * ``PRAGMA query_only = ON`` is set before the single SELECT;
  * the only SQL text in this module is that PRAGMA and one SELECT by primary
    key (statically tested).

It imports no broker, risk-authority, runtime or network module. It is a tool:
nothing in bot/ imports it. Pointing it at the production signals.db is an
operator decision (it never writes, but it reads production audit data).
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from bot.scoring_truth import PersistedRiskDecision

_SELECT = ("SELECT decision_id, taken_at, broker_scope, requested_action, "
           "request_json, result, authority_after, source "
           "FROM risk_decisions WHERE decision_id = ?")


class DecisionStoreError(RuntimeError):
    """The store could not be read (unreadable file, missing table, ...)."""


class ReadOnlyM14DecisionStore:
    """Fetch one persisted M14 decision by id, read-only."""

    STORE = "m14_risk_decisions(read_only)"

    def __init__(self, db_path):
        self._path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        uri = "file:%s?mode=ro" % quote(str(self._path.resolve()))
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only = ON")
        return conn

    def fetch(self, decision_id) -> Optional[PersistedRiskDecision]:
        """The persisted decision, or None if this id was never recorded.
        Raises DecisionStoreError if the store itself cannot be read."""
        if not isinstance(decision_id, str) or not decision_id.strip():
            return None
        if not self._path.is_file():
            return None
        try:
            conn = self._connect()
            try:
                rows = conn.execute(_SELECT, (decision_id,)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise DecisionStoreError(type(exc).__name__) from None
        if len(rows) != 1:
            return None
        (did, taken_at, scope, action, request_json, result, auth_after, src) = rows[0]
        symbol = side = amount = leverage = None
        try:
            req = json.loads(request_json) if request_json else None
        except (TypeError, ValueError):
            req = None
        if isinstance(req, dict):
            if isinstance(req.get("symbol"), str):
                symbol = req["symbol"]
            if isinstance(req.get("side"), str):
                side = req["side"]
            a = req.get("amount_usd")
            if isinstance(a, (int, float)) and not isinstance(a, bool) and math.isfinite(a):
                amount = float(a)
            lev = req.get("leverage")
            if isinstance(lev, int) and not isinstance(lev, bool):
                leverage = lev
        return PersistedRiskDecision(
            decision_id=str(did), taken_at_utc=str(taken_at), broker_scope=str(scope),
            requested_action=str(action), result=str(result),
            authority_after=str(auth_after), audit_source=str(src),
            symbol=symbol, side=side, amount_usd=amount, leverage=leverage,
            store=self.STORE)
