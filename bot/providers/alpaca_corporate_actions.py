"""M21 WP5 — Alpaca corporate-actions fetch for split / price-scale evidence.

Endpoint (Alpaca market-data API, documented at
docs.alpaca.markets/us/reference/corporateactions-1; response shape as parsed
by the official ``alpaca-py`` SDK, ``alpaca/data/models/corporate_actions.py``):

    GET https://data.alpaca.markets/v1/corporate-actions
        ?symbols=A,B&types=forward_split,reverse_split,...&start=YYYY-MM-DD
        &end=YYYY-MM-DD&limit=1000&data_quality=all[&page_token=...]
    -> {"corporate_actions": {"forward_splits": [{"symbol", "new_rate",
        "old_rate", "process_date", "ex_date", ...}], "reverse_splits": [...],
        ...}, "next_page_token": str | null}

``data_quality=all`` includes actions the provider has not finished
processing (e.g. no ex-date yet); those are returned with ``ex_date=None`` so
the evidence builder treats them as inside the window (conservative).

The result is COMPLETE only if every page was fetched without error within
``max_pages``. Only a complete fetch may become a verified record; anything
else fails closed downstream (UNVERIFIED corporate-action state).

Secret-safe (ALPACA_KEY / ALPACA_SECRET, never logged or returned);
injectable ``http_get`` (tests never touch the network). This module is NOT
yet exercised against the live API: runtime verification needs approved
access (VPS VERIFICATION REQUIRED).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from bot.market_clock.alpaca_clock import (
    HttpError, HttpResponse, _default_http_get, _get_keys,
)

SOURCE = "alpaca:/v1/corporate-actions"
DATA_BASE_DEFAULT = "https://data.alpaca.markets"
# request type name -> response key (alpaca-py CorporateActionsSet)
TYPES = {"forward_split": "forward_splits", "reverse_split": "reverse_splits",
         "unit_split": "unit_splits", "stock_dividend": "stock_dividends",
         "spin_off": "spin_offs"}
_SYMBOL_FIELD = {"forward_splits": "symbol", "reverse_splits": "symbol",
                 "unit_splits": "old_symbol", "stock_dividends": "symbol",
                 "spin_offs": "source_symbol"}


@dataclass(frozen=True)
class ActionRow:
    symbol: str
    action_type: str            # request type name, e.g. "forward_split"
    ex_date: Optional[date]     # None when the provider gave none / unparseable
    process_date: Optional[date]
    detail: str = ""


@dataclass(frozen=True)
class CorporateActionsFetch:
    complete: bool
    rows: Tuple[ActionRow, ...]
    symbols: Tuple[str, ...]
    start: str
    end: str
    fetched_at_utc: str
    pages: int
    source: str = SOURCE
    note: str = ""

    def rows_for(self, symbol: str) -> List[ActionRow]:
        s = symbol.strip().upper()
        return [r for r in self.rows if r.symbol == s]


def _date_or_none(v: Any) -> Optional[date]:
    if isinstance(v, str) and v.strip():
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def fetch_price_scale_actions(symbols: Sequence[str], start: date, end: date, *,
                              now_utc: datetime,
                              http_get: Optional[Callable[..., HttpResponse]] = None,
                              base_url: Optional[str] = None, max_pages: int = 50,
                              timeout_sec: float = 10.0) -> CorporateActionsFetch:
    syms = tuple(sorted({str(s).strip().upper() for s in symbols if str(s).strip()}))
    s, e = start.isoformat(), end.isoformat()
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")

    def incomplete(note, rows=(), pages=0):
        return CorporateActionsFetch(False, tuple(rows), syms, s, e, now_utc.isoformat(),
                                     pages, note=note)

    if not syms:
        return incomplete("no symbols")
    if start > end:
        return incomplete("bad range")
    key, secret = _get_keys()
    if not key or not secret:
        return incomplete("ALPACA_KEY/ALPACA_SECRET not set")
    base = (base_url or os.getenv("ALPACA_DATA_URL") or DATA_BASE_DEFAULT).rstrip("/")
    get = http_get or _default_http_get
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params: Dict[str, Any] = {"symbols": ",".join(syms), "types": ",".join(TYPES),
                              "start": s, "end": e, "limit": 1000, "data_quality": "all"}
    rows: List[ActionRow] = []
    token, pages = None, 0
    while True:
        if pages >= max_pages:
            return incomplete("page limit %d reached" % max_pages, rows, pages)
        p = dict(params)
        if token:
            p["page_token"] = token
        try:
            r = get(base + "/v1/corporate-actions", headers=headers, params=p,
                    timeout=timeout_sec)
        except HttpError as exc:
            return incomplete("transport error: %s" % str(exc)[:40], rows, pages)
        pages += 1
        code = getattr(r, "status_code", None)
        if not isinstance(code, int) or not (200 <= code < 300):
            return incomplete("http %s" % code, rows, pages)
        try:
            body = r.json()
        except ValueError:
            return incomplete("body not JSON", rows, pages)
        if not isinstance(body, dict) or not isinstance(body.get("corporate_actions"), dict):
            return incomplete("missing corporate_actions object", rows, pages)
        for rtype, rkey in TYPES.items():
            items = body["corporate_actions"].get(rkey, [])
            if not isinstance(items, list):
                return incomplete("%s is not a list" % rkey, rows, pages)
            for it in items:
                if not isinstance(it, dict):
                    return incomplete("%s item is not an object" % rkey, rows, pages)
                sym = it.get(_SYMBOL_FIELD[rkey])
                if not isinstance(sym, str) or not sym.strip():
                    return incomplete("%s item lacks a symbol" % rkey, rows, pages)
                detail = ""
                if "old_rate" in it or "new_rate" in it:
                    detail = "%s->%s" % (it.get("old_rate"), it.get("new_rate"))
                rows.append(ActionRow(sym.strip().upper(), rtype,
                                      _date_or_none(it.get("ex_date")
                                                    or it.get("effective_date")),
                                      _date_or_none(it.get("process_date")), detail))
        token = body.get("next_page_token")
        if token is None or token == "":
            break
        if not isinstance(token, str):
            return incomplete("next_page_token not a string", rows, pages)
    rows = [r for r in rows if r.symbol in syms]
    return CorporateActionsFetch(True, tuple(rows), syms, s, e, now_utc.isoformat(), pages)
