"""M20.UC2 — pure, offline quality-gate engine.

Reads a UC1 quality snapshot (m20_quality_snapshot_v1) + a thresholds config,
applies data-quality + liquidity gates and a PRICE+DATE cross-check, and decides
scan_ready / data_quality_status per symbol.

Key rules (locked):
- Both Alpaca AND Yahoo required for `verified` (single-source -> unverified).
- Cross-check is PRICE + DATE only. Alpaca IEX volume (single-venue) vs Yahoo
  volume (consolidated) is NOT comparable; volume divergence is reported but
  never causes a failure.
- Liquidity gates use ONE consolidated source (config: liquidity_source, yahoo).
- Default scan_ready=false. scan_ready=true requires all gates + cross-check.
- Configurable safety ceiling (max_scan_ready_per_run). report-only mode makes
  NO write-back.
- No fetch, no network, no RNG, no wall-clock (last_verified_utc derives from the
  snapshot asof). Deterministic + idempotent.

This module NEVER writes universe files in report-only mode. The write-back path
edits us_seed.json / us_expanded.json in place (only the quality/scan_ready
fields) and is invoked only via mode='write_back'.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bot.universe.quality_gate_report import (
    QualityGateReport, SymbolDecision)

_REPO = Path(__file__).resolve().parents[2]
_SNAP_SCHEMA = "m20_quality_snapshot_v1"
_THRESHOLDS = _REPO / "configs" / "universe" / "quality_thresholds.json"
_SEED = _REPO / "configs" / "universe" / "us_seed.json"
_EXPANDED = _REPO / "configs" / "universe" / "us_expanded.json"


# ── helpers ──
def _load_thresholds(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path) if path else _THRESHOLDS
    return json.loads(p.read_text(encoding="utf-8"))


def _thresholds_digest(cfg: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _pct_diff(x: float, y: float) -> float:
    base = max(abs(x), abs(y), 1e-9)
    return abs(x - y) / base * 100.0


def _is_etf_denied(name: str, ticker: str, etf_deny: Dict[str, Any]) -> bool:
    if ticker in set(etf_deny.get("tickers", [])):
        return True
    low = (name or "").lower()
    return any(kw in low for kw in etf_deny.get("name_keywords", []))


def _liquidity_tier(dollar_vol: float, tiers: Dict[str, Any]) -> Optional[str]:
    if dollar_vol >= tiers["tier_1"]:
        return "tier_1"
    if dollar_vol >= tiers["tier_2"]:
        return "tier_2"
    if dollar_vol >= tiers["tier_3"]:
        return "tier_3"
    return None


def evaluate_symbol(record: Dict[str, Any], snap_entry: Dict[str, Any],
                    cfg: Dict[str, Any], *, asof: str) -> SymbolDecision:
    """Pure per-symbol gate evaluation. Returns a SymbolDecision; no I/O."""
    internal = record["internal_symbol"]
    name = record.get("name", "")
    asset_class = record.get("asset_class", "EQUITY")
    ticker = record.get("provider_symbols", {}).get("alpaca") \
        or record.get("provider_symbols", {}).get("yahoo") \
        or internal.split(":")[-1]
    reasons: List[str] = []

    a = (snap_entry or {}).get("alpaca") or {}
    y = (snap_entry or {}).get("yahoo") or {}
    a_ok = a.get("status") == "ok"
    y_ok = y.get("status") == "ok"

    # both-sources requirement -> unverified if either missing
    if not a_ok and not y_ok:
        reasons.append("missing_both_sources")
        return SymbolDecision(internal, "unverified", False, reasons=reasons)
    if not a_ok:
        reasons.append("missing_alpaca")
        return SymbolDecision(internal, "unverified", False, reasons=reasons)
    if not y_ok:
        reasons.append("missing_yahoo")
        return SymbolDecision(internal, "unverified", False, reasons=reasons)

    # ETF deny-class (never scan_ready, even if liquid) -> failed
    if str(asset_class).upper() == "ETF" and _is_etf_denied(
            name, ticker, cfg["etf_deny"]):
        reasons.append("etf_denied_class")
        return SymbolDecision(internal, "failed", False, reasons=reasons)

    cc = cfg["cross_check"]
    # ── cross-check: PRICE + DATE only (volume intentionally excluded) ──
    a_close, y_close = a.get("latest_close"), y.get("latest_close")
    if isinstance(a_close, (int, float)) and isinstance(y_close, (int, float)):
        if _pct_diff(a_close, y_close) > float(cc["latest_close_pct"]):
            reasons.append("price_disagreement")
    else:
        reasons.append("missing_close")
    if cc.get("require_date_aligned", True):
        if a.get("last_bar_date") != y.get("last_bar_date"):
            reasons.append("bar_date_mismatch")

    # ── data-quality gates (use Alpaca bars_count; both share history) ──
    bars = max(int(a.get("bars_count") or 0), int(y.get("bars_count") or 0))
    if bars < int(cfg["min_history_days"]):
        reasons.append("insufficient_history")

    # staleness: last_bar_date within max_stale_trading_days of asof.
    # Compared per source against the snapshot asof (deterministic, no clock).
    from datetime import date
    try:
        asof_d = date.fromisoformat(asof)
        for src, blk in (("alpaca", a), ("yahoo", y)):
            lbd = blk.get("last_bar_date")
            d = date.fromisoformat(lbd) if lbd else None
            if d is None:
                reasons.append(f"missing_bar_date_{src}")
            elif (asof_d - d).days > int(cfg["max_stale_trading_days"]) + 3:
                # +3 calendar buffer to approximate trading-day window
                reasons.append("stale_data")
                break
    except (ValueError, TypeError):
        reasons.append("bad_date_format")

    # ── liquidity gates (single consolidated source) ──
    lsrc = cfg.get("liquidity_source", "yahoo")
    liq = y if lsrc == "yahoo" else a
    close = liq.get("latest_close")
    vol = liq.get("avg_volume_20d")
    dvol = liq.get("avg_dollar_volume_20d")
    spread = liq.get("median_spread_bps")

    if not isinstance(close, (int, float)) or close < float(cfg["min_latest_close"]):
        reasons.append("below_min_price")
    if not isinstance(vol, (int, float)) or vol < float(cfg["min_avg_volume_20d"]):
        reasons.append("below_min_volume")
    if not isinstance(dvol, (int, float)) or dvol < float(cfg["min_avg_dollar_volume_20d"]):
        reasons.append("below_min_dollar_volume")
    # spread: skip-with-note when absent; never fabricate
    if spread is None:
        reasons.append("spread_unavailable_skipped")
    elif spread > float(cfg["max_median_spread_bps"]):
        reasons.append("spread_too_wide")

    tier = _liquidity_tier(float(dvol), cfg["liquidity_tiers"]) \
        if isinstance(dvol, (int, float)) else None
    if tier is None and "below_min_dollar_volume" not in reasons:
        reasons.append("below_tier_3")

    # hard-fail reasons (spread_unavailable_skipped is NOT a failure)
    hard = [r for r in reasons if r != "spread_unavailable_skipped"]
    if hard:
        return SymbolDecision(
            internal, "failed", False, reasons=reasons,
            avg_volume_20d=vol if isinstance(vol, (int, float)) else None,
            avg_dollar_volume_20d=dvol if isinstance(dvol, (int, float)) else None,
            liquidity_source=lsrc)

    # passed everything -> verified + scan_ready
    last_verified = f"{asof}T00:00:00+00:00"
    pass_reasons = ["passed"]
    if "spread_unavailable_skipped" in reasons:
        pass_reasons.append("spread_unavailable_skipped")
    return SymbolDecision(
        internal, "verified", True, min_liquidity_tier=tier,
        avg_volume_20d=float(vol), avg_dollar_volume_20d=float(dvol),
        median_spread_bps=(float(spread) if isinstance(spread, (int, float))
                           else None),
        last_verified_utc=last_verified, liquidity_source=lsrc,
        reasons=pass_reasons)


def _volume_diverges(snap_entry: Dict[str, Any]) -> bool:
    a = (snap_entry or {}).get("alpaca") or {}
    y = (snap_entry or {}).get("yahoo") or {}
    if a.get("status") != "ok" or y.get("status") != "ok":
        return False
    av, yv = a.get("avg_volume_20d"), y.get("avg_volume_20d")
    if isinstance(av, (int, float)) and isinstance(yv, (int, float)):
        return _pct_diff(av, yv) > 25.0
    return False


def run_quality_gates(*, snapshot_path: str,
                      thresholds_path: Optional[str] = None,
                      mode: str = "report_only") -> QualityGateReport:
    """Evaluate all universe symbols against the snapshot + thresholds.

    mode='report_only' (default): NO write-back; returns the full report.
    mode='write_back': edits us_seed.json/us_expanded.json in place (only the
    quality/scan_ready fields). Caller must explicitly opt in.
    """
    from datetime import datetime, timezone
    started = datetime.now(timezone.utc).isoformat()
    cfg = _load_thresholds(thresholds_path)
    snap = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    if snap.get("schema_version") != _SNAP_SCHEMA:
        return QualityGateReport(
            mode=mode, snapshot_path=snapshot_path,
            errors=["snapshot_schema_mismatch"], started_at_utc=started,
            finished_at_utc=datetime.now(timezone.utc).isoformat())
    asof = snap.get("asof")
    snap_syms = snap.get("symbols", {})

    # load universe records (both files), keep file association for write-back
    seed_doc = json.loads(_SEED.read_text(encoding="utf-8"))
    exp_doc = json.loads(_EXPANDED.read_text(encoding="utf-8"))
    records: List[Tuple[Dict[str, Any], str]] = (
        [(r, "seed") for r in seed_doc["symbols"]]
        + [(r, "expanded") for r in exp_doc["symbols"]])

    decisions: List[SymbolDecision] = []
    tier_counts: Dict[str, int] = {}
    fail_reasons: Dict[str, int] = {}
    vol_div = 0
    for rec, _which in records:
        internal = rec["internal_symbol"]
        entry = snap_syms.get(internal, {})
        if _volume_diverges(entry):
            vol_div += 1
        dec = evaluate_symbol(rec, entry, cfg, asof=asof)
        decisions.append(dec)
        if dec.scan_ready and dec.min_liquidity_tier:
            tier_counts[dec.min_liquidity_tier] = \
                tier_counts.get(dec.min_liquidity_tier, 0) + 1
        if dec.data_quality_status == "failed":
            for r in dec.reasons:
                if r not in ("passed", "spread_unavailable_skipped"):
                    fail_reasons[r] = fail_reasons.get(r, 0) + 1

    verified = sum(1 for d in decisions if d.data_quality_status == "verified")
    failed = sum(1 for d in decisions if d.data_quality_status == "failed")
    unverified = sum(1 for d in decisions
                     if d.data_quality_status == "unverified")
    scan_ready = sum(1 for d in decisions if d.scan_ready)
    would_sr = sorted(d.internal_symbol for d in decisions if d.scan_ready)
    ceiling = int(cfg.get("max_scan_ready_per_run", 0))
    exceeded = scan_ready > ceiling

    report = QualityGateReport(
        mode=mode, asof=asof, snapshot_path=snapshot_path,
        thresholds_digest=_thresholds_digest(cfg),
        symbols_total=len(records), evaluated=len(decisions),
        verified_count=verified, failed_count=failed,
        unverified_count=unverified, scan_ready_count=scan_ready,
        volume_semantics_divergence_count=vol_div,
        max_scan_ready_per_run=ceiling, ceiling_exceeded=exceeded,
        tier_counts=tier_counts, fail_reason_counts=fail_reasons,
        would_scan_ready=would_sr, decisions=decisions,
        started_at_utc=started,
        finished_at_utc=datetime.now(timezone.utc).isoformat())

    if mode == "write_back":
        if exceeded:
            report.errors.append(
                f"ceiling_exceeded:{scan_ready}>{ceiling}_no_write")
            return report
        _write_back(decisions, seed_doc, exp_doc)

    return report


def _write_back(decisions: List[SymbolDecision], seed_doc: Dict[str, Any],
                exp_doc: Dict[str, Any]) -> None:
    """Edit us_seed.json/us_expanded.json IN PLACE, only the quality fields.
    Identity/membership fields and universe_tags are never touched."""
    by_internal = {d.internal_symbol: d for d in decisions}
    for doc, path in ((seed_doc, _SEED), (exp_doc, _EXPANDED)):
        for rec in doc["symbols"]:
            d = by_internal.get(rec["internal_symbol"])
            if d is None:
                continue
            rec["scan_ready"] = d.scan_ready
            rec["data_quality_status"] = d.data_quality_status
            if d.data_quality_status == "verified":
                rec["avg_volume_20d"] = d.avg_volume_20d
                rec["avg_dollar_volume_20d"] = d.avg_dollar_volume_20d
                rec["median_spread_bps"] = d.median_spread_bps
                rec["min_liquidity_tier"] = d.min_liquidity_tier
                rec["last_verified_utc"] = d.last_verified_utc
            rec["notes"] = ";".join(d.reasons) if d.reasons else None
        path.write_text(
            json.dumps(doc, indent=2, sort_keys=False) + "\n",
            encoding="utf-8")


# ── thin CLI ──
def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser("bot.universe.quality")
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--thresholds", default=None)
    ap.add_argument("--mode", choices=["report-only", "write-back"],
                    default="report-only")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    mode = "write_back" if args.mode == "write-back" else "report_only"
    report = run_quality_gates(
        snapshot_path=args.snapshot, thresholds_path=args.thresholds,
        mode=mode)
    d = report.to_dict()
    if args.json:
        # omit the very large per-symbol decisions list from CLI json by default
        slim = {k: v for k, v in d.items() if k != "decisions"}
        print(json.dumps(slim, indent=2, sort_keys=True))
    else:
        print(f"mode={d['mode']} verified={d['verified_count']} "
              f"failed={d['failed_count']} unverified={d['unverified_count']} "
              f"scan_ready={d['scan_ready_count']} "
              f"ceiling={d['max_scan_ready_per_run']} "
              f"exceeded={d['ceiling_exceeded']} tiers={d['tier_counts']}")
    return 2 if report.errors else 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
