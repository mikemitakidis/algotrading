"""M21 WP5 — Alpaca trading-calendar snapshot (read-only fetch, fail-closed).

Fetches ``GET {base}/v2/calendar?start=YYYY-MM-DD&end=YYYY-MM-DD`` (the same
endpoint and transport as ``AlpacaClockSource``) and returns the raw
trading-day rows plus provenance. It does not interpret the rows: turning
them into a session calendar (and refusing rows that do not fit) is done by
the pure evidence builder used by the scoring tools.

* Secret-safe: credentials come from ALPACA_KEY / ALPACA_SECRET and are never
  logged, returned or placed in notes.
* Fail-closed: no credentials, transport errors, non-2xx, non-JSON, a
  non-list body or a malformed row give a non-OK status and NO rows.
* All HTTP goes through an injectable ``http_get`` (tests never touch the
  network).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Optional, Tuple

from bot.market_clock.alpaca_clock import (
    HttpError, HttpResponse, _default_http_get, _get_keys,
)
from bot.market_clock.sources import (
    STATUS_MALFORMED, STATUS_NO_CREDENTIALS, STATUS_OK, STATUS_UNAVAILABLE,
)

SOURCE = "alpaca:/v2/calendar"
_BASE_DEFAULT = "https://api.alpaca.markets"
MAX_RANGE_DAYS = 800


@dataclass(frozen=True)
class CalendarFetch:
    status: str
    rows: Tuple[dict, ...]            # ({"date","open","close"}, ...) — empty unless OK
    start: str
    end: str
    fetched_at_utc: str
    source: str = SOURCE
    digest: str = ""                  # sha256 of the normalised rows
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK and bool(self.rows)


def _fail(status, start, end, now, note):
    return CalendarFetch(status, (), start, end, now.isoformat(), note=note)


def fetch_calendar(start: date, end: date, *, now_utc: datetime,
                   http_get: Optional[Callable[..., HttpResponse]] = None,
                   base_url: Optional[str] = None, timeout_sec: float = 10.0
                   ) -> CalendarFetch:
    s, e = start.isoformat(), end.isoformat()
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    if start > end or (end - start).days > MAX_RANGE_DAYS:
        return _fail(STATUS_MALFORMED, s, e, now_utc, "bad range")
    key, secret = _get_keys()
    if not key or not secret:
        return _fail(STATUS_NO_CREDENTIALS, s, e, now_utc, "ALPACA_KEY/ALPACA_SECRET not set")
    base = (base_url or os.getenv("ALPACA_BASE_URL") or _BASE_DEFAULT).rstrip("/")
    get = http_get or _default_http_get
    try:
        r = get(base + "/v2/calendar",
                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                params={"start": s, "end": e}, timeout=timeout_sec)
    except HttpError as exc:
        return _fail(STATUS_UNAVAILABLE, s, e, now_utc, "transport error: %s" % str(exc)[:40])
    code = getattr(r, "status_code", None)
    if not isinstance(code, int) or not (200 <= code < 300):
        return _fail(STATUS_UNAVAILABLE, s, e, now_utc, "http %s" % code)
    try:
        body = r.json()
    except ValueError:
        return _fail(STATUS_MALFORMED, s, e, now_utc, "body not JSON")
    if not isinstance(body, list) or not body:
        return _fail(STATUS_MALFORMED, s, e, now_utc, "body is not a non-empty list")
    rows = []
    for row in body:
        if not isinstance(row, dict):
            return _fail(STATUS_MALFORMED, s, e, now_utc, "row is not an object")
        d, o, c = row.get("date"), row.get("open"), row.get("close")
        if not all(isinstance(x, str) and x.strip() for x in (d, o, c)):
            return _fail(STATUS_MALFORMED, s, e, now_utc, "row lacks date/open/close")
        rows.append({"date": d.strip(), "open": o.strip(), "close": c.strip()})
    rows.sort(key=lambda r: r["date"])
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    return CalendarFetch(STATUS_OK, tuple(rows), s, e, now_utc.isoformat(), digest=digest)
