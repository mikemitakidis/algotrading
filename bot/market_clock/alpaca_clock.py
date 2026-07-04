#!/usr/bin/env python3
"""M21.1extra-D1 Commit 2 — Alpaca Clock/Calendar market-status source.

Fetches US equity market status from Alpaca's trading API and normalizes it
into a Commit-1 `SourceResult`. It NEVER decides `may_submit` — the guard does.
It is fail-closed: any credential/network/HTTP/parse problem yields a
SourceResult with the appropriate non-OK status (or, for an OK-but-unusable
reading, OK + is_open plus a missing calendar so the guard fails closed).

Endpoints (trading API):
  GET {base}/v2/clock
      -> {"timestamp","is_open","next_open","next_close"}
  GET {base}/v2/calendar?start=YYYY-MM-DD&end=YYYY-MM-DD
      -> [{"date","open","close","session_open","session_close",...}]

Secrets:
  Credentials come from ALPACA_KEY / ALPACA_SECRET (repo-consistent) and are
  sent as Alpaca auth headers. They are NEVER printed, logged, returned, or
  placed (even partially/masked) in raw_summary.

Testing:
  All HTTP goes through an injectable `http_get` transport, so unit tests use
  mocked responses and never touch the network.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone, date as _date
from typing import Optional, Callable, Dict, Any, Tuple
from zoneinfo import ZoneInfo

from bot.market_clock.sources import (
    SourceResult, STATUS_OK, STATUS_NO_CREDENTIALS, STATUS_UNAVAILABLE,
    STATUS_MALFORMED,
)

_ALPACA_BASE_DEFAULT = "https://api.alpaca.markets"
_US_EASTERN = ZoneInfo("America/New_York")
_DEFAULT_TIMEOUT_SEC = 5.0
# This adapter serves the US_EQ calendar only (this slice).
_SUPPORTED = "US_EQ"


def _get_keys() -> Tuple[str, str]:
    """Repo-consistent credential read. Empty when unset."""
    key = os.getenv("ALPACA_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET", "").strip()
    return key, secret


@dataclass(frozen=True)
class HttpResponse:
    """Minimal transport response the adapter understands. `json()` may raise
    ValueError for non-JSON bodies (the adapter treats that as malformed)."""
    status_code: int
    json_body: Any = None
    ok_json: bool = True   # False => body was not valid JSON

    def json(self) -> Any:
        if not self.ok_json:
            raise ValueError("response body is not valid JSON")
        return self.json_body


class HttpError(Exception):
    """Raised by a transport for a connection failure/timeout (no response)."""


def _default_http_get(url: str, *, headers: Dict[str, str], params: Dict[str, Any],
                      timeout: float) -> HttpResponse:
    """Real transport using `requests`, with an explicit timeout. Imported
    lazily so the module has no hard network dependency at import time and the
    hygiene tests see no top-level `requests` import."""
    import requests  # lazy, local
    try:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
    except Exception as exc:  # connection error, timeout, DNS, etc.
        raise HttpError(type(exc).__name__)
    try:
        body = r.json()
        return HttpResponse(status_code=r.status_code, json_body=body,
                            ok_json=True)
    except ValueError:
        return HttpResponse(status_code=r.status_code, json_body=None,
                            ok_json=False)


def _parse_iso_utc(s: Any) -> Optional[datetime]:
    """Parse an ISO8601 string to an aware UTC datetime, else None. Never
    assumes a naive value is UTC (returns None for naive/ambiguous)."""
    if not isinstance(s, str) or not s:
        return None
    txt = s.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _parse_session_dt(date_str: Any, hhmm: Any) -> Optional[datetime]:
    """Combine a calendar 'date' (YYYY-MM-DD) and a local ET 'HH:MM' into an
    aware UTC datetime, using America/New_York (DST-correct via zoneinfo).
    No hardcoded offset. Returns None on any malformed input."""
    if not isinstance(date_str, str) or not isinstance(hhmm, str):
        return None
    try:
        y, m, d = (int(x) for x in date_str.strip().split("-"))
        parts = hhmm.strip().split(":")
        if len(parts) != 2:
            return None
        hh, mm = int(parts[0]), int(parts[1])
    except (ValueError, AttributeError):
        return None
    try:
        naive_local = datetime(y, m, d, hh, mm)
        aware_local = naive_local.replace(tzinfo=_US_EASTERN)
        return aware_local.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


class AlpacaClockSource:
    """Alpaca Clock/Calendar market-status source (US_EQ). Injected into the
    guard as a MarketStatusSource. Fail-closed and secret-safe."""

    def __init__(self, *, base_url: Optional[str] = None,
                 timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
                 http_get: Optional[Callable[..., HttpResponse]] = None):
        # base_url overridable for tests; default is the real trading API.
        self._base = (base_url or os.getenv("ALPACA_BASE_URL")
                      or _ALPACA_BASE_DEFAULT).rstrip("/")
        self._timeout = float(timeout_sec)
        self._http_get = http_get or _default_http_get

    @property
    def name(self) -> str:
        return "alpaca"

    def _headers(self, key: str, secret: str) -> Dict[str, str]:
        # Auth headers are constructed here and NEVER logged/returned.
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def _unavailable(self, now_utc: datetime, note: str,
                     status: str = STATUS_UNAVAILABLE) -> SourceResult:
        # note is a SAFE, secret-free string.
        return SourceResult(status=status, is_open=None, fetched_at_utc=now_utc,
                            calendar_present=False, source_name=self.name,
                            raw_summary={"note": note})

    def get_status(self, *, market_calendar_id: str,
                   now_utc: datetime) -> SourceResult:
        # unsupported calendar -> the guard also guards this, but be explicit.
        if market_calendar_id != _SUPPORTED:
            return self._unavailable(
                now_utc, "alpaca source supports US_EQ only", STATUS_UNAVAILABLE)

        key, secret = _get_keys()
        if not key or not secret:
            return SourceResult(status=STATUS_NO_CREDENTIALS, is_open=None,
                                fetched_at_utc=now_utc, calendar_present=False,
                                source_name=self.name,
                                raw_summary={"note": "ALPACA_KEY/ALPACA_SECRET "
                                             "not set"})
        headers = self._headers(key, secret)

        # ── 1) CLOCK (primary open/closed) ────────────────────────────────
        try:
            cr = self._http_get(self._base + "/v2/clock", headers=headers,
                                params={}, timeout=self._timeout)
        except HttpError as exc:
            return self._unavailable(now_utc, "clock transport error: %s"
                                     % str(exc)[:40])
        clock_res = self._classify_http(cr, now_utc)
        if clock_res is not None:
            return clock_res  # a non-OK HTTP/parse result (fail-closed)

        try:
            cbody = cr.json()
        except ValueError:
            return self._unavailable(now_utc, "clock body not JSON",
                                     STATUS_MALFORMED)
        if not isinstance(cbody, dict) or "is_open" not in cbody:
            return self._unavailable(now_utc, "clock missing is_open",
                                     STATUS_MALFORMED)

        is_open = cbody.get("is_open")
        if is_open is not True and is_open is not False:
            return self._unavailable(now_utc, "clock is_open not boolean",
                                     STATUS_MALFORMED)

        as_of = _parse_iso_utc(cbody.get("timestamp"))
        next_open = _parse_iso_utc(cbody.get("next_open"))
        next_close = _parse_iso_utc(cbody.get("next_close"))
        # malformed timestamps present (non-empty but unparseable) -> malformed
        if cbody.get("timestamp") and as_of is None:
            return self._unavailable(now_utc, "clock timestamp malformed",
                                     STATUS_MALFORMED)
        if cbody.get("next_open") and next_open is None:
            return self._unavailable(now_utc, "next_open malformed",
                                     STATUS_MALFORMED)
        if cbody.get("next_close") and next_close is None:
            return self._unavailable(now_utc, "next_close malformed",
                                     STATUS_MALFORMED)

        safe_summary = {
            "is_open": is_open,
            "has_timestamp": bool(cbody.get("timestamp")),
            "endpoint": "clock+calendar",
        }

        # ── If CLOSED: we can return closed even if calendar is missing. ──
        if is_open is False:
            return SourceResult(
                status=STATUS_OK, is_open=False,
                session_open_utc=None, session_close_utc=None,
                next_open_utc=next_open, next_close_utc=next_close,
                as_of_utc=as_of, fetched_at_utc=now_utc,
                calendar_present=False, source_name=self.name,
                raw_summary=safe_summary)

        # ── If OPEN: the calendar is REQUIRED to confirm regular session. ──
        session_open, session_close, cal_ok, cal_status = \
            self._fetch_session_window(headers, now_utc)
        if not cal_ok:
            # clock says open but calendar missing/unavailable/malformed ->
            # return OK+open WITHOUT a calendar so the guard fails closed, OR a
            # non-OK status. We surface the calendar problem as its status.
            return SourceResult(
                status=cal_status, is_open=True,
                session_open_utc=None, session_close_utc=None,
                next_open_utc=next_open, next_close_utc=next_close,
                as_of_utc=as_of, fetched_at_utc=now_utc,
                calendar_present=False, source_name=self.name,
                raw_summary={**safe_summary, "calendar": "unavailable"})

        return SourceResult(
            status=STATUS_OK, is_open=True,
            session_open_utc=session_open, session_close_utc=session_close,
            next_open_utc=next_open, next_close_utc=next_close,
            as_of_utc=as_of, fetched_at_utc=now_utc,
            calendar_present=True, source_name=self.name,
            raw_summary={**safe_summary, "calendar": "present"})

    def _fetch_session_window(self, headers: Dict[str, str], now_utc: datetime
                              ) -> Tuple[Optional[datetime], Optional[datetime],
                                         bool, str]:
        """Return (session_open_utc, session_close_utc, ok, status) for TODAY's
        US_EQ regular session from the calendar. ok=False + a status on any
        problem. Early close is encoded by the calendar's own close time."""
        # today's ET date drives the calendar query (the session we're in).
        et_today = now_utc.astimezone(_US_EASTERN).date()
        ds = et_today.isoformat()
        try:
            rr = self._http_get(self._base + "/v2/calendar", headers=headers,
                                params={"start": ds, "end": ds},
                                timeout=self._timeout)
        except HttpError:
            return None, None, False, STATUS_UNAVAILABLE
        # HTTP-level failure?
        http_fail = self._classify_http(rr, now_utc)
        if http_fail is not None:
            return None, None, False, http_fail.status
        try:
            body = rr.json()
        except ValueError:
            return None, None, False, STATUS_MALFORMED
        if not isinstance(body, list) or not body:
            # no session row for today -> treat as unavailable for an OPEN
            # confirmation (guard will fail closed).
            return None, None, False, STATUS_UNAVAILABLE
        row = body[0]
        if not isinstance(row, dict):
            return None, None, False, STATUS_MALFORMED
        rdate = row.get("date") or ds
        # prefer explicit session_open/close; fall back to open/close.
        open_hhmm = row.get("session_open") or row.get("open")
        close_hhmm = row.get("session_close") or row.get("close")
        so = _parse_session_dt(rdate, open_hhmm)
        sc = _parse_session_dt(rdate, close_hhmm)
        if so is None or sc is None or not (so < sc):
            return None, None, False, STATUS_MALFORMED
        return so, sc, True, STATUS_OK

    def _classify_http(self, resp: HttpResponse, now_utc: datetime
                       ) -> Optional[SourceResult]:
        """Return a fail-closed SourceResult for a non-2xx response, else None.
        401/403/429/5xx/other-non-2xx -> unavailable. Secret-free notes only."""
        code = getattr(resp, "status_code", None)
        if code is None:
            return self._unavailable(now_utc, "no status code")
        if 200 <= code < 300:
            return None
        if code in (401, 403):
            return self._unavailable(now_utc, "auth rejected (%d)" % code)
        if code == 429:
            return self._unavailable(now_utc, "rate limited (429)")
        if 500 <= code < 600:
            return self._unavailable(now_utc, "server error (%d)" % code)
        return self._unavailable(now_utc, "unexpected http %d" % code)
