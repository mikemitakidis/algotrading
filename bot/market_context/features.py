"""M21 WP15 — point-in-time market-context features.

Every feature answers "what was knowable at time t" and says how it knows:
status OK, STALE (knowable but older than its freshness limit) or
UNAVAILABLE (no source, too little data, or the source is undecided). A
feature is never filled with a default value.

* ``vix_level``, ``vix_change_5d`` and ``vix_regime`` come from the
  point-in-time store, so a later revision or a not-yet-published value is
  invisible.
* ``spy_trend``, ``qqq_trend`` and ``sector_uptrend_share`` come from
  CLOSE-stamped daily bars. Only rows with ``ts_utc <= t`` are used, so a
  forming or future bar can never leak in. Callers pass completed bars (WP6)
  or WP11-converted history. Bars stamped at or before the 09:30 New York
  open cannot be closes (M16 stores bar STARTS); they are refused and the
  feature is UNAVAILABLE (review of PR #20, fail closed).
* ``breadth_above_sma50`` is the share of the universe above its 50-day SMA,
  computed only with enough coverage.
* ``event_<KIND>_hours`` and ``event_window`` use only events whose schedule
  was announced by t.
* ``earnings_proximity`` is UNAVAILABLE until G4 decides the source.
* ``news_risk`` is the existing sentiment layer's score when its status is ok.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional, Sequence

import pandas as pd

from bot.market_context.sources import EVENT_KINDS, SECTOR_ETFS, ScheduledEvent
from bot.market_context.store import ContextStore

OK, STALE, UNAVAILABLE = "OK", "STALE", "UNAVAILABLE"
VIX_REGIMES = ((15.0, "low"), (25.0, "normal"), (35.0, "high"), (math.inf, "extreme"))
VIX_MAX_AGE = timedelta(days=4)
BARS_MAX_AGE = timedelta(days=5)
EVENT_BEFORE, EVENT_AFTER = timedelta(hours=2), timedelta(hours=1)


@dataclass(frozen=True)
class FeatureValue:
    name: str
    value: Any
    status: str
    as_of: Optional[str]
    source: str
    detail: str = ""


def _unavailable(name, source, why):
    return FeatureValue(name, None, UNAVAILABLE, None, source, why)


def vix_features(store: ContextStore, t: datetime) -> Dict[str, FeatureValue]:
    hist = store.history_asof("VIXCLS", t, 6)
    if not hist:
        return {n: _unavailable(n, "VIXCLS", "no_vix_published_by_t") for n in
                ("vix_level", "vix_change_5d", "vix_regime")}
    last = hist[-1]
    status = OK if t - last.published_at_utc <= VIX_MAX_AGE else STALE
    asof = last.published_at_utc.isoformat()
    regime = next(name for hi, name in VIX_REGIMES if last.value < hi)
    out = {"vix_level": FeatureValue("vix_level", last.value, status, asof, last.source, str(last.obs_date)),
           "vix_regime": FeatureValue("vix_regime", regime, status, asof, last.source, str(last.obs_date))}
    out["vix_change_5d"] = (FeatureValue("vix_change_5d", last.value - hist[0].value, status, asof, last.source)
                            if len(hist) == 6 else _unavailable("vix_change_5d", "VIXCLS", "fewer_than_6_values"))
    return out


class _StartStamped(ValueError):
    pass


def _closed(bars: Optional[pd.DataFrame], t: datetime) -> Optional[pd.DataFrame]:
    if bars is None or len(bars) == 0:
        return None
    ts = pd.to_datetime(bars["ts_utc"], utc=True)
    ny = pd.DatetimeIndex(ts).tz_convert("America/New_York")
    if ((ny.hour * 60 + ny.minute) <= 9 * 60 + 30).any():
        raise _StartStamped("bars stamped at or before the 09:30 New York open are not closes")
    b = bars.loc[(ts <= pd.Timestamp(t)).to_numpy()]
    return b if len(b) else None


def trend_feature(name: str, bars: Optional[pd.DataFrame], t: datetime, *, min_bars: int = 200) -> FeatureValue:
    try:
        b = _closed(bars, t)
    except _StartStamped:
        return _unavailable(name, "INDEX_ETF", "start_stamped_bars_refused")
    if b is None or len(b) < min_bars:
        return _unavailable(name, "INDEX_ETF", "fewer_than_%d_closed_bars" % min_bars)
    c = b["close"].astype(float)
    s50, s200, last = c.rolling(50).mean().iloc[-1], c.rolling(200).mean().iloc[-1], c.iloc[-1]
    v = "up" if last > s50 > s200 else "down" if last < s50 < s200 else "mixed"
    lt = pd.Timestamp(b["ts_utc"].iloc[-1])
    status = OK if pd.Timestamp(t) - lt <= BARS_MAX_AGE else STALE
    return FeatureValue(name, v, status, lt.isoformat(), "INDEX_ETF")


def sector_feature(bars_by_etf: Mapping[str, pd.DataFrame], t: datetime, *, min_sectors: int = 8) -> FeatureValue:
    trends = [trend_feature(e, bars_by_etf.get(e), t) for e in SECTOR_ETFS]
    ok = [f for f in trends if f.status == OK]
    if len(ok) < min_sectors:
        return _unavailable("sector_uptrend_share", "INDEX_ETF", "only_%d_of_%d_sectors" % (len(ok), len(SECTOR_ETFS)))
    return FeatureValue("sector_uptrend_share", sum(f.value == "up" for f in ok) / len(ok), OK,
                        max(f.as_of for f in ok), "INDEX_ETF", "%d_sectors" % len(ok))


def breadth_feature(bars_by_symbol: Mapping[str, pd.DataFrame], t: datetime, *,
                    min_coverage: float = 0.8) -> FeatureValue:
    n, above, used = len(bars_by_symbol), 0, 0
    for b in bars_by_symbol.values():
        try:
            c = _closed(b, t)
        except _StartStamped:
            continue                                       # not counted: coverage falls, never a leak
        if c is None or len(c) < 50 or pd.Timestamp(t) - pd.Timestamp(c["ts_utc"].iloc[-1]) > BARS_MAX_AGE:
            continue
        x = c["close"].astype(float)
        used += 1
        above += int(x.iloc[-1] > x.iloc[-50:].mean())
    if n == 0 or used / n < min_coverage:
        return _unavailable("breadth_above_sma50", "BREADTH", "coverage_%d_of_%d" % (used, n))
    return FeatureValue("breadth_above_sma50", above / used, OK, pd.Timestamp(t).isoformat(), "BREADTH",
                        "%d_symbols" % used)


def event_features(events: Optional[Sequence[ScheduledEvent]], t: datetime) -> Dict[str, FeatureValue]:
    if events is None:
        out = {"event_%s_hours" % k: _unavailable("event_%s_hours" % k, "EVENTS", "no_calendar") for k in EVENT_KINDS}
        out["event_window"] = _unavailable("event_window", "EVENTS", "no_calendar")
        return out
    known = [e for e in events if e.announced_at_utc <= t]
    out, window = {}, []
    for k in EVENT_KINDS:
        ks = [e for e in known if e.kind == k]
        nxt = [e for e in ks if e.at_utc >= t - EVENT_AFTER]
        if not ks:
            out["event_%s_hours" % k] = _unavailable("event_%s_hours" % k, "EVENTS", "no_announced_%s" % k)
            continue
        if nxt:
            e = nxt[0]
            out["event_%s_hours" % k] = FeatureValue("event_%s_hours" % k, (e.at_utc - t).total_seconds() / 3600,
                                                     OK, e.announced_at_utc.isoformat(), e.source)
            if e.at_utc - EVENT_BEFORE <= t <= e.at_utc + EVENT_AFTER:
                window.append(k)
        else:
            out["event_%s_hours" % k] = _unavailable("event_%s_hours" % k, "EVENTS", "no_future_%s_announced" % k)
    out["event_window"] = FeatureValue("event_window", tuple(window), OK, None, "EVENTS")
    return out


def earnings_feature() -> FeatureValue:
    return _unavailable("earnings_proximity", "EARNINGS", "source_and_licence_undecided_G4")


def news_feature(result: Any) -> FeatureValue:
    """``result``: a ``bot.sentiment.base.SentimentResult`` (duck-typed)."""
    if result is None or getattr(result, "status", None) != "ok" or getattr(result, "score", None) is None:
        return _unavailable("news_risk", "NEWS_RISK", "sentiment_%s" % getattr(result, "status", "absent"))
    return FeatureValue("news_risk", float(result.score), OK, None, "NEWS_RISK:%s" % getattr(result, "source", ""))


def build_context(store: ContextStore, t: datetime, *, index_bars: Mapping[str, pd.DataFrame],
                  sector_bars: Mapping[str, pd.DataFrame], universe_bars: Mapping[str, pd.DataFrame],
                  events: Optional[Sequence[ScheduledEvent]], news: Any = None) -> Dict[str, FeatureValue]:
    if t.tzinfo is None:
        raise ValueError("t must be timezone-aware")
    ctx = dict(vix_features(store, t))
    ctx["spy_trend"] = trend_feature("spy_trend", index_bars.get("SPY"), t)
    ctx["qqq_trend"] = trend_feature("qqq_trend", index_bars.get("QQQ"), t)
    ctx["sector_uptrend_share"] = sector_feature(sector_bars, t)
    ctx["breadth_above_sma50"] = breadth_feature(universe_bars, t)
    ctx.update(event_features(events, t))
    ctx["earnings_proximity"] = earnings_feature()
    ctx["news_risk"] = news_feature(news)
    return ctx


__all__ = ["OK", "STALE", "UNAVAILABLE", "VIX_REGIMES", "VIX_MAX_AGE", "BARS_MAX_AGE", "EVENT_BEFORE", "EVENT_AFTER",
           "FeatureValue", "vix_features", "trend_feature", "sector_feature", "breadth_feature", "event_features",
           "earnings_feature", "news_feature", "build_context"]
