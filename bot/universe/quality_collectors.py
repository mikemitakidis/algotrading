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
        "batch_size": 100,
        "throttle_seconds": 0.0,
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


def _default_alpaca_fetch(symbols: List[str], *, lookback_days: int
                          ) -> Dict[str, Dict[str, Any]]:
    from bot.providers.alpaca_provider import AlpacaProvider  # lazy
    prov = AlpacaProvider()
    end = date.today()
    start = end - timedelta(days=lookback_days)
    out: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        try:
            df, status = prov.fetch_bars_range(sym, "1d", start, end)
            out[sym] = (_metrics_from_df(df) if status == "ok" or df is not None
                        else {"status": status or "error"})
        except Exception as e:  # noqa: BLE001 — record, never fatal
            msg = str(e)[:120]
            out[sym] = {"status": "rate_limit" if "rate" in msg.lower()
                        else "error", "error": msg}
    return out


def _default_yahoo_fetch(symbols: List[str], *, lookback_days: int
                         ) -> Dict[str, Dict[str, Any]]:
    from bot.historical.providers_yfinance import YFinanceProvider  # lazy reuse
    prov = YFinanceProvider()
    out: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        try:
            res = prov.fetch_bars(sym, "1d")
            df = getattr(res, "bars", res)
            out[sym] = _metrics_from_df(df)
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:120]
            out[sym] = {"status": "rate_limit" if "rate" in msg.lower()
                        else "error", "error": msg}
    return out


def _probe(fetch: Callable, *, lookback_days: int) -> bool:
    """Single tiny read-only reachability probe (one symbol)."""
    try:
        r = fetch(["AAPL"], lookback_days=lookback_days)
        return bool(r) and r.get("AAPL", {}).get("status") == "ok"
    except Exception:  # noqa: BLE001
        return False


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
    alpaca_reachable = False
    yahoo_reachable = False
    errors: List[str] = []
    summaries: List[SourceSummary] = []
    # A real (default) Alpaca fetch needs creds; an injected fetcher (tests) is
    # probed directly. creds gate only the default network path.
    alpaca_injected = alpaca_fetch is not _default_alpaca_fetch

    if "alpaca" in sources:
        can_probe_alpaca = creds or alpaca_injected
        alpaca_reachable = can_probe_alpaca and _probe(
            alpaca_fetch, lookback_days=cfg["lookback_days"])
        summaries.append(SourceSummary(source="alpaca", creds_present=creds,
                                       reachable=alpaca_reachable))
        if not creds and not alpaca_injected:
            errors.append("alpaca_creds_missing")
    if "yahoo" in sources:
        yahoo_reachable = _probe(yahoo_fetch,
                                 lookback_days=cfg["lookback_days"])
        summaries.append(SourceSummary(source="yahoo", reachable=yahoo_reachable))

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
    if "alpaca" in sources and to_fetch:
        alpaca_data = alpaca_fetch(to_fetch, lookback_days=cfg["lookback_days"])
    if "yahoo" in sources and to_fetch:
        yahoo_data = yahoo_fetch(to_fetch, lookback_days=cfg["lookback_days"])

    symbols_out: Dict[str, Any] = dict(existing)
    a_ok = y_ok = both_ok = miss_a = miss_y = 0
    for (internal, yf) in universe:
        if not yf or internal in symbols_out:
            continue
        a = alpaca_data.get(yf, {"status": "missing"})
        y = yahoo_data.get(yf, {"status": "missing"})
        if a.get("status") == "rate_limit" or y.get("status") == "rate_limit":
            rate_limit_count += 1
        a_good = a.get("status") == "ok"
        y_good = y.get("status") == "ok"
        a_ok += int(a_good)
        y_ok += int(y_good)
        both_ok += int(a_good and y_good)
        miss_a += int(not a_good)
        miss_y += int(not y_good)
        symbols_out[internal] = {"provider_symbol": yf, "alpaca": a, "yahoo": y}

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
    status = "success" if (a_ok and y_ok and rate_limit_count == 0
                           and not errors) else "partial"
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
              f"snapshot={d['snapshot_path']}")
    return 0 if report.status in ("success", "partial") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
