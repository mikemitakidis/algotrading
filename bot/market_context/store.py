"""M21 WP15 (canonical B18) — point-in-time store for market-context series.

Every observation records:
- what it describes (``obs_date``);
- WHEN it became knowable (``published_at_utc``);
- when we retrieved it (``retrieved_at_utc``);
- the source and licence note;
- its vintage (revisions append a new vintage; nothing is overwritten);
- how the publication time was established (``publication_basis``): ``observed_retrieval``
  (we know only that it existed when we retrieved it) or ``assumed_lag`` (a
  declared, conservative publication lag for historical backfills).

``asof(series, t)`` returns what was KNOWABLE at ``t``: the newest
observation date whose vintage was published at or before ``t``, using the
newest such vintage. Data published after ``t`` is invisible, so a backtest
or a shadow decision at ``t`` never sees the future or a later revision.
The store writes only to a caller-supplied path.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

BASES = ("observed_retrieval", "assumed_lag")
UTC = timezone.utc

_DDL = (
    """CREATE TABLE IF NOT EXISTS context_observations (
        series_id TEXT NOT NULL, obs_date TEXT NOT NULL, vintage INTEGER NOT NULL, value REAL,
        published_at_utc TEXT NOT NULL, retrieved_at_utc TEXT NOT NULL, source TEXT NOT NULL,
        license TEXT NOT NULL, publication_basis TEXT NOT NULL,
        PRIMARY KEY (series_id, obs_date, vintage))""",
    """CREATE TABLE IF NOT EXISTS context_decisions (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, at_utc TEXT NOT NULL, decision_json TEXT NOT NULL)""",
)


@dataclass(frozen=True)
class Observation:
    series_id: str
    obs_date: date
    value: Optional[float]
    published_at_utc: datetime
    retrieved_at_utc: datetime
    source: str
    license: str
    publication_basis: str
    vintage: int = 1

    def __post_init__(self):
        for t in (self.published_at_utc, self.retrieved_at_utc):
            if t.tzinfo is None:
                raise ValueError("timestamps must be timezone-aware")
        # Stored as text and compared as text, so every instant is held in UTC:
        # a -05:00 stamp compared lexically with a UTC query time would make data
        # visible hours before it was published.
        object.__setattr__(self, "published_at_utc", self.published_at_utc.astimezone(UTC))
        object.__setattr__(self, "retrieved_at_utc", self.retrieved_at_utc.astimezone(UTC))
        if self.publication_basis not in BASES:
            raise ValueError("publication_basis must be one of %s" % (BASES,))
        if self.publication_basis == "observed_retrieval" and self.published_at_utc > self.retrieved_at_utc:
            raise ValueError("an observed publication cannot be later than its retrieval")
        if self.vintage < 1:
            raise ValueError("vintage must be >= 1")


class ContextStore:
    def __init__(self, path: str):
        if not path:
            raise ValueError("a context store path is required")
        self.path = path
        c = sqlite3.connect(path, timeout=10)
        try:
            for s in _DDL:
                c.execute(s)
            c.commit()
        finally:
            c.close()

    def _c(self):
        return sqlite3.connect(self.path, timeout=10)

    def record(self, o: Observation) -> None:
        """Append-only: re-recording a (series, date, vintage) raises; a
        revision must carry a higher vintage published later than the last."""
        c = self._c()
        try:
            last = c.execute("SELECT MAX(vintage), MAX(published_at_utc) FROM context_observations WHERE "
                             "series_id=? AND obs_date=?", (o.series_id, o.obs_date.isoformat())).fetchone()
            if last[0] is not None and (o.vintage <= last[0] or o.published_at_utc.isoformat() <= last[1]):
                raise ValueError("revision of %s %s must have a higher vintage published later"
                                 % (o.series_id, o.obs_date))
            c.execute("INSERT INTO context_observations VALUES (?,?,?,?,?,?,?,?,?)",
                      (o.series_id, o.obs_date.isoformat(), o.vintage, o.value, o.published_at_utc.isoformat(),
                       o.retrieved_at_utc.isoformat(), o.source, o.license, o.publication_basis))
            c.commit()
        finally:
            c.close()

    def asof(self, series_id: str, t: datetime) -> Optional[Observation]:
        if t.tzinfo is None:
            raise ValueError("t must be timezone-aware")
        t = t.astimezone(UTC)
        c = self._c()
        try:
            r = c.execute("SELECT * FROM context_observations WHERE series_id=? AND published_at_utc<=? "
                          "AND value IS NOT NULL ORDER BY obs_date DESC, vintage DESC LIMIT 1",
                          (series_id, t.isoformat())).fetchone()
        finally:
            c.close()
        if r is None:
            return None
        return Observation(r[0], date.fromisoformat(r[1]), r[3], datetime.fromisoformat(r[4]),
                           datetime.fromisoformat(r[5]), r[6], r[7], r[8], r[2])

    def history_asof(self, series_id: str, t: datetime, n: int) -> List[Observation]:
        """The last ``n`` observation dates knowable at ``t`` (newest vintage
        published by ``t`` for each), oldest first."""
        if t.tzinfo is None:
            raise ValueError("t must be timezone-aware")
        t = t.astimezone(UTC)
        c = self._c()
        try:
            rows = c.execute("SELECT * FROM context_observations o WHERE series_id=? AND published_at_utc<=? "
                             "AND value IS NOT NULL AND vintage = (SELECT MAX(vintage) FROM context_observations "
                             "i WHERE i.series_id=o.series_id AND i.obs_date=o.obs_date AND i.published_at_utc<=?) "
                             "ORDER BY obs_date DESC LIMIT ?", (series_id, t.isoformat(), t.isoformat(), n)).fetchall()
        finally:
            c.close()
        return [Observation(r[0], date.fromisoformat(r[1]), r[3], datetime.fromisoformat(r[4]),
                            datetime.fromisoformat(r[5]), r[6], r[7], r[8], r[2]) for r in reversed(rows)]

    def coverage(self, series_id: str) -> Dict:
        c = self._c()
        try:
            r = c.execute("SELECT COUNT(DISTINCT obs_date), MIN(obs_date), MAX(obs_date), MAX(vintage), "
                          "SUM(value IS NULL) FROM context_observations WHERE series_id=?", (series_id,)).fetchone()
        finally:
            c.close()
        return {"series_id": series_id, "dates": r[0], "first": r[1], "last": r[2], "max_vintage": r[3],
                "missing_values": r[4] or 0}

    def log_decision(self, decision_json: str, at: datetime) -> None:
        c = self._c()
        try:
            c.execute("INSERT INTO context_decisions (at_utc, decision_json) VALUES (?,?)", (at.isoformat(),
                                                                                            decision_json))
            c.commit()
        finally:
            c.close()


__all__ = ["BASES", "Observation", "ContextStore"]
