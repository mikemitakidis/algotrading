"""M20.J — read-only M20 status reporter.

Pure, offline reporting helper that summarizes M20 state from EXISTING
artifacts. No writes, no network, no trading, no runtime behaviour. Safe to run
any time; importing performs no I/O beyond the explicit build_status() call.

Surfaces:
  * universe counts (total / active / verified / failed / unverified / scan_ready)
  * quality snapshot reference (committed UC1 v3 snapshot + asof)
  * quality thresholds reference (key gate values)
  * paper loop enabled/disabled status (reads PAPER_LOOP_ENABLED, read-only)
  * paper storage summary (gracefully handles no data/paper/ artifacts yet)
  * frozen M20 commit summary (from docs/ROADMAP_M20.md key commits)

CLI:  python -m bot.runtime.m20_status --json
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO = Path(__file__).resolve().parents[2]
_SEED = _REPO / "configs" / "universe" / "us_seed.json"
_EXPANDED = _REPO / "configs" / "universe" / "us_expanded.json"
_THRESHOLDS = _REPO / "configs" / "universe" / "quality_thresholds.json"
_SNAPSHOT = (_REPO / "configs" / "universe" / "quality_input"
             / "us_quality_v3_20260624.json")
_PAPER_DIR = _REPO / "data" / "paper"

STATUS_SCHEMA_VERSION = "m20_status_report_v1"

# Frozen M20 commit references (kept in sync with docs/ROADMAP_M20.md).
_M20_COMMITS: Dict[str, str] = {
    "m19_main_baseline": "e823fe6779deaccc7b8ff7859c17b4dab564b868",
    "uc1_snapshot": "63b16ba0ea8418a4e9069dd536618adc9dd67766",
    "uc2_engine": "52ee00d093976b32a54769aa0a2cfb1fbc5b4611",
    "uc2_writeback": "501487ffb715e62bb4172c1bca55a173a3e492b1",
    "roadmap_doc": "3ec5d89766563a0347939a3572e653c31d2d65c9",
    "ue_registry_selector": "d077260d189a8fe6927b7c994f45872800df243a",
    "test_refresh": "6109650904b320b49806b21ce2ce7f6e3dca05c3",
    "m20i_paper_loop": "8421a2fae4cadfa003f0985e8a3fba93b5e4c838",
}


def _universe_counts() -> Dict[str, Any]:
    counts = {"total": 0, "active": 0, "verified": 0, "failed": 0,
              "unverified": 0, "scan_ready": 0}
    files_present = _SEED.exists() and _EXPANDED.exists()
    if not files_present:
        return {"files_present": False, **counts}
    for p in (_SEED, _EXPANDED):
        doc = json.loads(p.read_text(encoding="utf-8"))
        for r in doc.get("symbols", []):
            counts["total"] += 1
            if r.get("active"):
                counts["active"] += 1
            if r.get("scan_ready"):
                counts["scan_ready"] += 1
            dqs = r.get("data_quality_status")
            if dqs in counts:
                counts[dqs] += 1
    return {"files_present": True, **counts}


def _quality_snapshot_ref() -> Dict[str, Any]:
    if not _SNAPSHOT.exists():
        return {"present": False, "path": str(
            _SNAPSHOT.relative_to(_REPO))}
    doc = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    return {
        "present": True,
        "path": str(_SNAPSHOT.relative_to(_REPO)),
        "asof": doc.get("asof"),
        "schema_version": doc.get("schema_version"),
        "symbol_count": len(doc.get("symbols", {})),
    }


def _thresholds_ref() -> Dict[str, Any]:
    if not _THRESHOLDS.exists():
        return {"present": False}
    cfg = json.loads(_THRESHOLDS.read_text(encoding="utf-8"))
    return {
        "present": True,
        "schema_version": cfg.get("schema_version"),
        "min_history_days": cfg.get("min_history_days"),
        "max_stale_trading_days": cfg.get("max_stale_trading_days"),
        "min_latest_close": cfg.get("min_latest_close"),
        "min_avg_volume_20d": cfg.get("min_avg_volume_20d"),
        "min_avg_dollar_volume_20d": cfg.get("min_avg_dollar_volume_20d"),
        "liquidity_source": cfg.get("liquidity_source"),
        "liquidity_tiers": cfg.get("liquidity_tiers"),
        "max_scan_ready_per_run": cfg.get("max_scan_ready_per_run"),
        "cross_check": cfg.get("cross_check"),
    }


def _paper_loop_status() -> Dict[str, Any]:
    raw = os.getenv("PAPER_LOOP_ENABLED", "")
    enabled = raw.strip().lower() in ("1", "true", "yes", "on")
    return {"env_var": "PAPER_LOOP_ENABLED", "raw_value": raw or None,
            "enabled": enabled, "default": "off (simulation-only when on)"}


def _paper_storage_summary() -> Dict[str, Any]:
    """Summarize paper storage if present; gracefully report absence."""
    events = _PAPER_DIR / "events.jsonl"
    snapshots = _PAPER_DIR / "snapshots.jsonl"
    accounts = _PAPER_DIR / "account_state.jsonl"
    if not _PAPER_DIR.exists():
        return {"present": False,
                "note": "no data/paper/ artifacts yet (paper loop has not "
                        "been run with persistence)",
                "dir": str(_PAPER_DIR.relative_to(_REPO))}
    out: Dict[str, Any] = {"present": True,
                           "dir": str(_PAPER_DIR.relative_to(_REPO))}
    # use the existing M20.H read helpers; never recompute / never write
    try:
        from bot.paper import (load_events, load_snapshots,
                               load_account_states, replay_events_summary)
        if events.exists():
            er = load_events(str(events))
            out["events_loaded"] = getattr(er, "loaded", 0)
            if getattr(er, "ok", False) and er.records:
                rep = replay_events_summary(er.records)
                out["replay_ok"] = getattr(rep, "ok", False)
        else:
            out["events_loaded"] = 0
        out["snapshots_loaded"] = (
            getattr(load_snapshots(str(snapshots)), "loaded", 0)
            if snapshots.exists() else 0)
        out["account_states_loaded"] = (
            getattr(load_account_states(str(accounts)), "loaded", 0)
            if accounts.exists() else 0)
    except Exception as e:  # noqa: BLE001 — reporting must never raise
        out["error"] = f"{type(e).__name__}:{e}"
    return out


def build_status() -> Dict[str, Any]:
    """Assemble the full M20 status report. Pure read; returns a dict."""
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "universe": _universe_counts(),
        "quality_snapshot": _quality_snapshot_ref(),
        "quality_thresholds": _thresholds_ref(),
        "paper_loop": _paper_loop_status(),
        "paper_storage": _paper_storage_summary(),
        "frozen_m20_commits": dict(_M20_COMMITS),
        "main_merged": False,
        "next_required": "M20.UD (inactive global candidates) after M20 "
                         "close / main merge",
    }


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser("bot.runtime.m20_status")
    ap.add_argument("--json", action="store_true",
                    help="emit the full report as JSON")
    args = ap.parse_args(argv)
    report = build_status()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        u = report["universe"]
        print(f"M20 STATUS  total={u['total']} active={u['active']} "
              f"verified={u['verified']} failed={u['failed']} "
              f"unverified={u['unverified']} scan_ready={u['scan_ready']}")
        print(f"  snapshot={report['quality_snapshot'].get('asof')} "
              f"ceiling={report['quality_thresholds'].get('max_scan_ready_per_run')} "
              f"paper_loop_enabled={report['paper_loop']['enabled']} "
              f"paper_storage_present={report['paper_storage']['present']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
