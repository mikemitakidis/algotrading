"""M20.UC1 admin/offline universe quality collector.

Explicit, read-only, admin-callable backend that fetches daily bars for the
expanded US universe from Alpaca (primary) and Yahoo/yfinance (cross-check) and
writes a reviewable quality SNAPSHOT. It computes per-symbol, per-source raw
metrics only (last_bar_date, bars_count, latest_close, avg_volume_20d,
avg_dollar_volume_20d, optional median_spread_bps). It makes NO gate decisions,
NEVER sets scan_ready / data_quality_status, and NEVER modifies us_seed.json or
us_expanded.json. UC2 consumes the snapshot and makes those decisions.

Admin-callable backend (stable, for the future admin panel + a thin CLI):
    universe_quality_check(*, sources, dry_run=True)      -> QualityCollectionReport
    universe_quality_collect(*, asof, sources, out_path)  -> QualityCollectionReport
    universe_quality_validate(*, snapshot_path)           -> QualityCollectionReport

Provider imports (alpaca-py / yfinance) are LAZY and collector-only; they are
never imported by gate code, scanner, runtime, or trading paths. Network happens
only inside collect/dry-run when invoked on a host with creds + connectivity
(the VPS) — not at import time. Fetchers are dependency-injectable so tests run
fully offline with mocks. No secrets are printed, logged, or written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from bot.universe.registry import UniverseRegistry
from bot.universe.quality_report import (
    QualityCollectionReport, SourceSummary, SCHEMA_VERSION as REPORT_VERSION,
)

SNAPSHOT_SCHEMA_VERSION = "m20_quality_snapshot_v1"
_REPO = Path(__file__).resolve().parent.parent.parent
_SEED = _REPO / "configs" / "universe" / "us_seed.json"
_EXPANDED = _REPO / "configs" / "universe" / "us_expanded.json"
_COLLECTOR_CONFIG = (_REPO / "configs" / "universe" /
                     "quality_collector_config.json")
_DEFAULT_OUT_DIR = _REPO / "configs" / "universe" / "quality_input"

_VALID_SOURCES = ("alpaca", "yahoo")


# ── helpers ──
def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _load_config() -> Dict[str, Any]:
    defaults = {
        "lookback_days": 400,          # ~252 trading days + buffer
        "alpaca_feed": "iex",          # free-plan feed; SIP requires subscription
        "batch_size": 50,
        "throttle_seconds": 0.4,
        "max_retries": 3,
        "tolerances": {
            "latest_close_pct": 2.0,
            "avg_volume_20d_pct": 25.0,
            "avg_dollar_volume_20d_pct": 25.0,
        },
    }
    if _COLLECTOR_CONFIG.exists():
        cfg = json.loads(_COLLECTOR_CONFIG.read_text(encoding="utf-8"))
        defaults.update(cfg)
    return defaults


def _config_digest(cfg: Dict[str, Any]) -> str:
    payload = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_universe_symbols() -> List[Tuple[str, str]]:
    """Return [(internal_symbol, yfinance_provider_symbol)] for the full
    universe (seed + expanded). READ-ONLY — does not modify the files."""
    out: List[Tuple[str, str]] = []
    for path in (_SEED, _EXPANDED):
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for r in doc["symbols"]:
            out.append((r["internal_symbol"],
                        r["provider_symbols"].get("yfinance")))
    return out


def _creds_present() -> bool:
    """True iff Alpaca creds exist in env — NEVER returns/echoes the values."""
    return bool(os.getenv("ALPACA_KEY", "").strip()
                and os.getenv("ALPACA_SECRET", "").strip())


# ── default (real) fetchers — lazy provider imports, collector-only ──
def _metrics_from_df(df) -> Dict[str, Any]:
    """Compute per-source raw metrics from a daily OHLCV DataFrame."""
    import pandas as pd  # local
    if df is None or len(df) == 0:
        return {"status": "empty"}
    closes = df["close"] if "close" in df else df.iloc[:, 3]
    vols = df["volume"] if "volume" in df else df.iloc[:, 4]
    last20_v = vols.tail(20)
    last20_dollar = (closes.tail(20) * vols.tail(20))
    idx = df.index
    last_bar = idx[-1]
    last_bar_date = (last_bar.date().isoformat()
                     if hasattr(last_bar, "date") else str(last_bar)[:10])
    return {
        "status": "ok",
        "last_bar_date": last_bar_date,
        "bars_count": int(len(df)),
        "latest_close": float(closes.iloc[-1]),
        "avg_volume_20d": float(last20_v.mean()),
        "avg_dollar_volume_20d": float(last20_dollar.mean()),
        "median_spread_bps": None,   # not available from daily bars; UC2 skips
    }


def _classify_provider_error(msg: str, *, source: str) -> str:
    """Map a provider error message to a stable, non-secret reason code."""
    low = (msg or "").lower()
    if "sip" in low or "subscription does not permit" in low:
        return "alpaca_subscription_not_permitted"
    if "rate" in low or "429" in low or "too many" in low:
        return f"{source}_rate_limited"
    if not low:
        return "source_unreachable"
    return f"{source}_fetch_error"


def _is_rate_limit(rec: Dict[str, Any]) -> bool:
    return (rec.get("status") == "rate_limit"
            or str(rec.get("reason", "")).endswith("_rate_limited"))


def _fetch_with_policy(
    symbols: List[str],
    fetch_one: Callable[[str], Dict[str, Any]],
    *,
    throttle_seconds: float = 0.0,
    max_retries: int = 3,
    batch_size: int = 100,
    sleep: Callable[[float], None] = None,
) -> Dict[str, Dict[str, Any]]:
    """Apply batching + throttling + exponential backoff on rate limits to a
    per-symbol fetch. `sleep` is injectable so tests pass a mock (no real wait).

    - throttle_seconds: pause between individual symbol fetches.
    - batch_size: pause (a longer throttle) between batches of this size.
    - max_retries: on a rate-limit result, retry up to this many times with
      exponential backoff (throttle_seconds * 2**attempt), then record the
      rate-limit result.
    Never raises; always returns a per-symbol result dict.
    """
    if sleep is None:
        import time as _time
        sleep = _time.sleep
    throttle = max(0.0, float(throttle_seconds))
    retries = max(0, int(max_retries))
    bsize = max(1, int(batch_size))
    out: Dict[str, Dict[str, Any]] = {}
    for i, sym in enumerate(symbols):
        rec = fetch_one(sym)
        attempt = 0
        while _is_rate_limit(rec) and attempt < retries:
            backoff = (throttle * (2 ** attempt)) if throttle > 0 else float(
                2 ** attempt)
            sleep(backoff)
            attempt += 1
            rec = fetch_one(sym)
        out[sym] = rec
        # throttle between symbols, and a longer pause between batches
        if throttle > 0 and i + 1 < len(symbols):
            if (i + 1) % bsize == 0:
                sleep(throttle * 2)
            else:
                sleep(throttle)
    return out


def _default_alpaca_fetch(symbols: List[str], *, lookback_days: int,
                          feed: str = "iex", throttle_seconds: float = 0.0,
                          max_retries: int = 3, batch_size: int = 100,
                          sleep: Callable[[float], None] = None
                          ) -> Dict[str, Dict[str, Any]]:
    """Collector-owned read-only Alpaca daily-bar fetch that explicitly
    requests the configured feed (default IEX). Builds its own StockBarsRequest
    via the lazy alpaca-py import so bot/providers/alpaca_provider.py is reused
    for client/creds but NOT modified. Honours throttle/batch/backoff. Never
    raises; records reasons."""
    from datetime import date as _date, timedelta as _td  # local
    from alpaca.data.requests import StockBarsRequest       # lazy
    from alpaca.data.timeframe import TimeFrame             # lazy
    try:
        from alpaca.data.enums import DataFeed               # lazy
        feed_enum = {"iex": DataFeed.IEX, "sip": DataFeed.SIP}.get(
            (feed or "iex").lower(), DataFeed.IEX)
    except Exception:  # noqa: BLE001 — older alpaca-py
        feed_enum = None
    from bot.providers.alpaca_provider import AlpacaProvider  # lazy, unmodified
    prov = AlpacaProvider()
    end = _date.today()
    start = end - _td(days=lookback_days)

    def _one(sym: str) -> Dict[str, Any]:
        try:
            client = prov._client()
            kwargs = dict(symbol_or_symbols=sym, timeframe=TimeFrame.Day,
                          start=start.isoformat(),
                          end=(end + _td(days=1)).isoformat())
            if feed_enum is not None:
                kwargs["feed"] = feed_enum
            df = client.get_stock_bars(StockBarsRequest(**kwargs)).df
            if df is None or len(df) == 0:
                return {"status": "no_data", "reason": "source_unreachable"}
            return _metrics_from_df(df)
        except Exception as e:  # noqa: BLE001 — record, never fatal
            reason = _classify_provider_error(str(e)[:160], source="alpaca")
            status = ("rate_limit" if reason == "alpaca_rate_limited"
                      else "error")
            return {"status": status, "reason": reason}

    return _fetch_with_policy(symbols, _one, throttle_seconds=throttle_seconds,
                              max_retries=max_retries, batch_size=batch_size,
                              sleep=sleep)


def _default_yahoo_fetch(symbols: List[str], *, lookback_days: int,
                         throttle_seconds: float = 0.0, max_retries: int = 3,
                         batch_size: int = 100,
                         sleep: Callable[[float], None] = None
                         ) -> Dict[str, Dict[str, Any]]:
    """Collector-owned read-only Yahoo daily-bar fetch using the REAL
    YFinanceProvider.fetch_bars(symbol, timeframe, start_utc, end_utc)
    signature and its FetchResult(outcome, df, ...) return. Honours
    throttle/batch/backoff. Never raises."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from bot.historical.providers_yfinance import YFinanceProvider  # lazy reuse
    from bot.historical.providers import (
        FETCH_OK, FETCH_RATE_LIMITED, FETCH_PROVIDER_ERROR, FETCH_NO_DATA)
    prov = YFinanceProvider()
    end_utc = _dt.now(_tz.utc)
    start_utc = end_utc - _td(days=lookback_days)

    def _one(sym: str) -> Dict[str, Any]:
        try:
            res = prov.fetch_bars(sym, "1D", start_utc, end_utc)
            outcome = getattr(res, "outcome", None)
            if outcome == FETCH_OK and getattr(res, "df", None) is not None:
                return _metrics_from_df(res.df)
            if outcome == FETCH_RATE_LIMITED:
                return {"status": "rate_limit", "reason": "yahoo_rate_limited"}
            if outcome == FETCH_NO_DATA:
                return {"status": "no_data", "reason": "source_unreachable"}
            return {"status": "error", "reason": "yahoo_fetch_error"}
        except Exception as e:  # noqa: BLE001 — record, never fatal
            reason = _classify_provider_error(str(e)[:160], source="yahoo")
            status = ("rate_limit" if reason == "yahoo_rate_limited"
                      else "error")
            return {"status": status, "reason": reason}

    return _fetch_with_policy(symbols, _one, throttle_seconds=throttle_seconds,
                              max_retries=max_retries, batch_size=batch_size,
                              sleep=sleep)


def _probe(fetch: Callable, *, lookback_days: int, **kwargs
           ) -> Tuple[bool, Optional[str]]:
    """Single tiny read-only reachability probe (one symbol). Returns
    (reachable, reason) — reason is a non-secret code when unreachable."""
    try:
        r = fetch(["AAPL"], lookback_days=lookback_days, **kwargs)
        rec = (r or {}).get("AAPL", {})
        if rec.get("status") == "ok":
            return True, None
        return False, rec.get("reason") or "source_unreachable"
    except Exception as e:  # noqa: BLE001 — never crash a probe
        return False, _classify_provider_error(str(e)[:160], source="source")


# ── backend: dry-run ──
def universe_quality_check(
    *,
    sources: Sequence[str] = _VALID_SOURCES,
    dry_run: bool = True,
    alpaca_fetch: Optional[Callable] = None,
    yahoo_fetch: Optional[Callable] = None,
) -> QualityCollectionReport:
    """Check credentials + connectivity only. No snapshot written, no bulk
    fetch, no scan_ready touch."""
    started = _now_utc()
    cfg = _load_config()
    sources = [s for s in sources if s in _VALID_SOURCES]
    alpaca_fetch = alpaca_fetch or _default_alpaca_fetch
    yahoo_fetch = yahoo_fetch or _default_yahoo_fetch

    creds = _creds_present()
    feed = cfg.get("alpaca_feed", "iex")
    alpaca_reachable = False
    yahoo_reachable = False
    errors: List[str] = []
    summaries: List[SourceSummary] = []
    # A real (default) Alpaca fetch needs creds; an injected fetcher (tests) is
    # probed directly. creds gate only the default network path.
    alpaca_injected = alpaca_fetch is not _default_alpaca_fetch

    if "alpaca" in sources:
        a_reason: Optional[str] = None
        if not creds and not alpaca_injected:
            a_reason = "alpaca_creds_missing"
        else:
            probe_kwargs = {} if alpaca_injected else {"feed": feed}
            alpaca_reachable, a_reason = _probe(
                alpaca_fetch, lookback_days=cfg["lookback_days"],
                **probe_kwargs)
        summaries.append(SourceSummary(source="alpaca", creds_present=creds,
                                       reachable=alpaca_reachable,
                                       reason=None if alpaca_reachable
                                       else a_reason))
        if not alpaca_reachable and a_reason:
            errors.append(a_reason)
    if "yahoo" in sources:
        yahoo_reachable, y_reason = _probe(
            yahoo_fetch, lookback_days=cfg["lookback_days"])
        summaries.append(SourceSummary(source="yahoo",
                                       reachable=yahoo_reachable,
                                       reason=None if yahoo_reachable
                                       else y_reason))
        if not yahoo_reachable and y_reason:
            errors.append(y_reason)

    ok = ((("alpaca" not in sources) or alpaca_reachable)
          and (("yahoo" not in sources) or yahoo_reachable))
    status = "success" if ok else ("partial" if (alpaca_reachable
                                                  or yahoo_reachable)
                                    else "failed")
    return QualityCollectionReport(
        status=status, mode="dry-run", asof=None, sources=list(sources),
        symbols_total=len(_load_universe_symbols()),
        alpaca_creds_present=creds, alpaca_reachable=alpaca_reachable,
        yahoo_reachable=yahoo_reachable, source_summaries=summaries,
        errors=errors, started_at_utc=started, finished_at_utc=_now_utc())


# ── backend: collect ──
def universe_quality_collect(
    *,
    asof: str,
    sources: Sequence[str] = _VALID_SOURCES,
    out_path: Optional[str] = None,
    resume: bool = True,
    alpaca_fetch: Optional[Callable] = None,
    yahoo_fetch: Optional[Callable] = None,
) -> QualityCollectionReport:
    """Fetch bars from the requested sources and write a quality snapshot.
    Writes ONLY the snapshot file — never universe records, never scan_ready."""
    started = _now_utc()
    cfg = _load_config()
    sources = [s for s in sources if s in _VALID_SOURCES]
    alpaca_fetch = alpaca_fetch or _default_alpaca_fetch
    yahoo_fetch = yahoo_fetch or _default_yahoo_fetch

    universe = _load_universe_symbols()
    yf_syms = [yf for (_internal, yf) in universe if yf]
    target = (str(out_path) if out_path
              else str(_DEFAULT_OUT_DIR / f"us_quality_{asof.replace('-', '')}.json"))

    # resume: load any existing snapshot symbols to skip re-fetch
    existing: Dict[str, Any] = {}
    if resume and Path(target).exists():
        try:
            existing = json.loads(
                Path(target).read_text(encoding="utf-8")).get("symbols", {})
        except (ValueError, KeyError):
            existing = {}

    by_yf = {yf: internal for (internal, yf) in universe if yf}
    to_fetch = [yf for yf in yf_syms
                if by_yf[yf] not in existing] if resume else yf_syms

    alpaca_data: Dict[str, Dict[str, Any]] = {}
    yahoo_data: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    rate_limit_count = 0
    feed = cfg.get("alpaca_feed", "iex")
    policy = dict(throttle_seconds=cfg.get("throttle_seconds", 0.0),
                  max_retries=cfg.get("max_retries", 3),
                  batch_size=cfg.get("batch_size", 100))
    alpaca_injected = alpaca_fetch is not _default_alpaca_fetch
    yahoo_injected = yahoo_fetch is not _default_yahoo_fetch
    if "alpaca" in sources and to_fetch:
        a_kwargs = dict(lookback_days=cfg["lookback_days"])
        if not alpaca_injected:
            a_kwargs.update(feed=feed, **policy)
        alpaca_data = alpaca_fetch(to_fetch, **a_kwargs)
    if "yahoo" in sources and to_fetch:
        y_kwargs = dict(lookback_days=cfg["lookback_days"])
        if not yahoo_injected:
            y_kwargs.update(**policy)
        yahoo_data = yahoo_fetch(to_fetch, **y_kwargs)

    symbols_out: Dict[str, Any] = dict(existing)
    a_ok = y_ok = both_ok = miss_a = miss_y = 0
    reason_set: set = set()
    for (internal, yf) in universe:
        if not yf or internal in symbols_out:
            continue
        a = alpaca_data.get(yf, {"status": "missing",
                                 "reason": "missing_alpaca"})
        y = yahoo_data.get(yf, {"status": "missing",
                                "reason": "missing_yahoo"})
        if a.get("status") == "rate_limit" or y.get("status") == "rate_limit":
            rate_limit_count += 1
        a_good = a.get("status") == "ok"
        y_good = y.get("status") == "ok"
        if not a_good and a.get("reason"):
            reason_set.add(a["reason"])
        if not y_good and y.get("reason"):
            reason_set.add(y["reason"])
        a_ok += int(a_good)
        y_ok += int(y_good)
        both_ok += int(a_good and y_good)
        miss_a += int(not a_good)
        miss_y += int(not y_good)
        symbols_out[internal] = {"provider_symbol": yf, "alpaca": a, "yahoo": y}
    errors.extend(sorted(reason_set))

    doc = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "asof": asof,
        "generated_at_utc": _now_utc(),
        "sources": list(sources),
        "collector_config_digest": _config_digest(cfg),
        "symbols": symbols_out,
    }
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text(
        json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")

    checked = len([1 for v in symbols_out.values()])
    if a_ok == 0 and y_ok == 0:
        status = "failed"
    elif (a_ok and y_ok and rate_limit_count == 0 and not errors):
        status = "success"
    else:
        status = "partial"
    return QualityCollectionReport(
        status=status, mode="collect", asof=asof, sources=list(sources),
        symbols_total=len(universe), symbols_checked=checked,
        alpaca_success_count=a_ok, yahoo_success_count=y_ok,
        both_sources_success_count=both_ok, missing_alpaca_count=miss_a,
        missing_yahoo_count=miss_y, rate_limit_count=rate_limit_count,
        alpaca_creds_present=_creds_present(), errors=errors,
        snapshot_path=target, started_at_utc=started,
        finished_at_utc=_now_utc())


# ── backend: validate ──
def universe_quality_validate(*, snapshot_path: str) -> QualityCollectionReport:
    """Validate snapshot structure + compute a source-agreement summary. No
    fetch, no writes, no scan_ready touch."""
    started = _now_utc()
    p = Path(snapshot_path)
    if not p.exists():
        return QualityCollectionReport(
            status="failed", mode="validate",
            errors=["snapshot_not_found"], snapshot_path=snapshot_path,
            started_at_utc=started, finished_at_utc=_now_utc())
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        return QualityCollectionReport(
            status="failed", mode="validate",
            errors=[f"corrupt_snapshot:{str(e)[:60]}"],
            snapshot_path=snapshot_path, started_at_utc=started,
            finished_at_utc=_now_utc())

    errors: List[str] = []
    if doc.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    symbols = doc.get("symbols", {})
    cfg = _load_config()
    tol = cfg["tolerances"]
    a_ok = y_ok = both = miss_a = miss_y = disagree = 0
    for internal, rec in symbols.items():
        a = rec.get("alpaca", {})
        y = rec.get("yahoo", {})
        ag = a.get("status") == "ok"
        yg = y.get("status") == "ok"
        a_ok += int(ag)
        y_ok += int(yg)
        miss_a += int(not ag)
        miss_y += int(not yg)
        if ag and yg:
            both += 1
            if _disagrees(a, y, tol):
                disagree += 1
    return QualityCollectionReport(
        status="success" if not errors else "failed", mode="validate",
        asof=doc.get("asof"), sources=doc.get("sources", []),
        symbols_total=len(symbols), symbols_checked=len(symbols),
        alpaca_success_count=a_ok, yahoo_success_count=y_ok,
        both_sources_success_count=both, missing_alpaca_count=miss_a,
        missing_yahoo_count=miss_y, source_disagreement_count=disagree,
        errors=errors, snapshot_path=snapshot_path, started_at_utc=started,
        finished_at_utc=_now_utc())


def _pct_diff(x: float, y: float) -> float:
    base = max(abs(x), abs(y), 1e-9)
    return abs(x - y) / base * 100.0


def _disagrees(a: Dict[str, Any], y: Dict[str, Any], tol: Dict[str, Any]
               ) -> bool:
    for key, tkey in (("latest_close", "latest_close_pct"),
                      ("avg_volume_20d", "avg_volume_20d_pct"),
                      ("avg_dollar_volume_20d", "avg_dollar_volume_20d_pct")):
        av, yv = a.get(key), y.get(key)
        if isinstance(av, (int, float)) and isinstance(yv, (int, float)):
            if _pct_diff(av, yv) > float(tol[tkey]):
                return True
    return False


# ── thin CLI wrapper ──
def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="bot.universe.quality_collectors",
        description="M20.UC1 admin/offline universe quality collector "
                    "(read-only; never modifies scan_ready or universe records)")
    ap.add_argument("--mode", required=True,
                    choices=["dry-run", "collect", "validate"])
    ap.add_argument("--sources", default="alpaca,yahoo")
    ap.add_argument("--asof", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--json", action="store_true",
                    help="print structured report as JSON")
    args = ap.parse_args(argv)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    if args.mode == "dry-run":
        report = universe_quality_check(sources=sources, dry_run=True)
    elif args.mode == "collect":
        if not args.asof:
            ap.error("--asof is required for collect")
        report = universe_quality_collect(asof=args.asof, sources=sources,
                                          out_path=args.out,
                                          resume=not args.no_resume)
    else:
        if not args.snapshot:
            ap.error("--snapshot is required for validate")
        report = universe_quality_validate(snapshot_path=args.snapshot)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        d = report.to_dict()
        print(f"status={d['status']} mode={d['mode']} "
              f"symbols_total={d['symbols_total']} "
              f"alpaca_ok={d['alpaca_success_count']} "
              f"yahoo_ok={d['yahoo_success_count']} "
              f"both={d['both_sources_success_count']} "
              f"rate_limited={d['rate_limit_count']} "
              f"reasons={','.join(d['errors']) or '-'} "
              f"snapshot={d['snapshot_path']}")
    # Exit-code design:
    #   0 = success/partial (>=1 source reachable, no implementation crash)
    #   2 = expected provider/config failure where all sources failed
    #       (creds missing, subscription denied, endpoint blocked, rate-limited)
    #   1 = unexpected/implementation error
    if report.status in ("success", "partial"):
        return 0
    _EXPECTED = {"alpaca_creds_missing", "alpaca_subscription_not_permitted",
                 "alpaca_rate_limited", "alpaca_fetch_error",
                 "yahoo_rate_limited", "yahoo_fetch_error", "source_unreachable",
                 "missing_alpaca", "missing_yahoo", "snapshot_not_found",
                 "schema_version_mismatch"}
    if report.errors and all(
            e in _EXPECTED or e.startswith("corrupt_snapshot")
            for e in report.errors):
        return 2
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
