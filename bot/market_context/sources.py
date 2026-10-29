"""M21 WP15 — context sources: registry, parsers for documented formats, event calendars.

No network code lives here. Data reaches the store only through these
parsers, from files the operator obtained under each source's terms (OP / G4
decisions). Nothing is fetched, bought or activated by this module.

Registry (``SOURCES``) — status reflects what is decided today:
  VIXCLS        CBOE VIX daily close as published by FRED (free download,
                FRED/CBOE terms apply)                       -> parser ready
  SPY, QQQ,     index and SPDR sector ETFs, daily bars from the existing
  XL*           market-data provider (completed bars, WP6)   -> derived
  BREADTH       computed from our own universe's daily bars  -> derived
  FOMC/CPI/NFP  official schedules (federalreserve.gov, bls.gov), kept by
                the operator as a JSON file; each event carries when its
                schedule was announced                       -> parser ready
  EARNINGS      source and licence undecided (G4)            -> UNAVAILABLE
  NEWS_RISK     the existing sentiment layer (bot/sentiment) -> adapter
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from bot.market_context.store import Observation

UTC = timezone.utc
SECTOR_ETFS = ("XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY")
EVENT_KINDS = ("FOMC", "CPI", "NFP")
# The VIX close for date D is set at 16:15 New York time, i.e. no earlier than
# 20:15 UTC (EDT) and 21:15 UTC (EST). An assumed publication lag is counted
# from D 00:00 UTC, so anything shorter than 22 hours would make the close
# knowable before it existed.
MIN_ASSUMED_LAG = timedelta(hours=22)


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    publisher: str
    license: str
    status: str            # parser_ready | derived | adapter | unavailable


SOURCES: Dict[str, SourceSpec] = {
    "VIXCLS": SourceSpec("VIXCLS", "FRED (St. Louis Fed), series VIXCLS from CBOE", "FRED / CBOE terms", "parser_ready"),
    "INDEX_ETF": SourceSpec("INDEX_ETF", "existing market-data provider (SPY, QQQ, sector ETFs)",
                            "provider terms", "derived"),
    "BREADTH": SourceSpec("BREADTH", "own universe daily bars", "internal", "derived"),
    "EVENTS": SourceSpec("EVENTS", "federalreserve.gov FOMC calendar; bls.gov CPI and Employment Situation "
                         "schedules (operator-maintained file)", "public schedules", "parser_ready"),
    "EARNINGS": SourceSpec("EARNINGS", "undecided (G4)", "undecided", "unavailable"),
    "NEWS_RISK": SourceSpec("NEWS_RISK", "bot/sentiment provider in use", "provider terms", "adapter"),
}


def parse_fred_csv(text: str, series_id: str, *, retrieved_at: datetime,
                   assumed_lag: Optional[timedelta] = None) -> Tuple[List[Observation], Dict[str, int]]:
    """FRED's CSV download: header ``observation_date,<SERIES>`` (older files:
    ``DATE,<SERIES>``); a value of ``.`` or empty means missing.

    Publication time: without ``assumed_lag`` every row is stamped with the
    retrieval time (``observed_retrieval``: the only thing we actually know).
    With ``assumed_lag`` (historical backfill) a row dated D is stamped
    D 00:00 UTC + lag (``assumed_lag``), never later than the retrieval."""
    if retrieved_at.tzinfo is None:
        raise ValueError("retrieved_at must be timezone-aware")
    if assumed_lag is not None and assumed_lag < MIN_ASSUMED_LAG:
        raise ValueError("assumed_lag %s is shorter than %s: the close would be knowable before it existed"
                         % (assumed_lag, MIN_ASSUMED_LAG))
    rows = list(csv.reader(io.StringIO(text.strip())))
    if not rows or [h.strip() for h in rows[0]] not in (["observation_date", series_id], ["DATE", series_id]):
        raise ValueError("not a FRED CSV for %s (header %r)" % (series_id, rows[0] if rows else None))
    out, missing, bad = [], 0, 0
    for r in rows[1:]:
        if len(r) != 2:
            bad += 1
            continue
        try:
            d = date.fromisoformat(r[0].strip())
        except ValueError:
            bad += 1
            continue
        v = r[1].strip()
        if v in ("", "."):
            missing += 1
            continue
        try:
            x = float(v)
        except ValueError:
            bad += 1
            continue
        if d > retrieved_at.date():
            bad += 1                              # an observation dated in the future cannot be real
            continue
        if assumed_lag is None:
            pub, basis = retrieved_at, "observed_retrieval"
        else:
            pub = min(datetime.combine(d, time(0), UTC) + assumed_lag, retrieved_at)
            basis = "assumed_lag"
        out.append(Observation(series_id, d, x, pub, retrieved_at, "FRED:%s" % series_id,
                               SOURCES["VIXCLS"].license, basis))
    if bad:
        raise ValueError("%d malformed rows in FRED CSV for %s" % (bad, series_id))
    return out, {"rows": len(out), "missing": missing}


@dataclass(frozen=True)
class ScheduledEvent:
    kind: str
    at_utc: datetime
    announced_at_utc: datetime
    source: str


def parse_event_calendar(text: str) -> List[ScheduledEvent]:
    """Operator-maintained JSON: ``[{"kind": "FOMC", "at_utc": ISO, "announced_at_utc": ISO,
    "source": "<official page>"}]``. Timestamps must be timezone-aware; an
    event cannot be announced after it happens."""
    raw = json.loads(text)
    out = []
    for e in raw:
        k = e.get("kind")
        if k not in EVENT_KINDS:
            raise ValueError("unknown event kind %r" % k)
        at, ann = datetime.fromisoformat(e["at_utc"].replace("Z", "+00:00")), \
            datetime.fromisoformat(e["announced_at_utc"].replace("Z", "+00:00"))
        if at.tzinfo is None or ann.tzinfo is None:
            raise ValueError("event timestamps must be timezone-aware")
        if ann > at:
            raise ValueError("event %s %s announced after it happens" % (k, at))
        if not str(e.get("source", "")).strip():
            raise ValueError("every event needs its official source")
        out.append(ScheduledEvent(k, at.astimezone(UTC), ann.astimezone(UTC), e["source"]))
    return sorted(out, key=lambda x: (x.at_utc, x.kind))


__all__ = ["SECTOR_ETFS", "EVENT_KINDS", "MIN_ASSUMED_LAG", "SourceSpec", "SOURCES", "parse_fred_csv", "ScheduledEvent",
           "parse_event_calendar"]
