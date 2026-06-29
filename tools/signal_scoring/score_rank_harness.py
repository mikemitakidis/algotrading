#!/usr/bin/env python3
"""M21.1 — Score & Rank harness (read-only, research-grade).

Runs scanner signals through the scoring bridge, ranks them by composite score
under the RESEARCH profile, and renders an explainable report: composite score,
11-component breakdown, confidence + decision buckets, gate status, reason codes,
and a "why A ranks above B" comparison.

Honesty contract (stated in every report):
  - scores are RESEARCH-GRADE rankings, not calibrated live probabilities
  - not execution approval; execution_eligible is False on every candidate
  - ML readiness is NOT passed (no trained model / no outcome data yet)
  - STRICT/live scoring stays hard-blocked until M21.1extra produces outcomes

Tool only: nothing in bot/ imports it. No orders, no broker / live / paper / DB /
Telegram. No runtime wiring.
"""
import argparse
import json
from pathlib import Path

from bot.signal_scoring import ScoringProfile
from tools.signal_scoring.scanner_bridge import score_signal

_COMPONENT_ORDER = [
    "ml", "scanner", "technical_confluence", "trend", "momentum",
    "volume_liquidity", "volatility", "market_regime", "risk_adjusted",
    "data_quality", "calibration_uncertainty",
]


def _bucket(b):
    return getattr(b, "value", b)


def score_rows(signals, profile=ScoringProfile.RESEARCH):
    rows = []
    for sig in signals:
        sc = score_signal(sig, profile=profile)
        if sc.execution_eligible is not False:
            raise AssertionError(
                "execution_eligible must be False (got %r for %s)"
                % (sc.execution_eligible, sc.symbol))
        rows.append({
            "symbol": sc.symbol,
            "side": _bucket(sc.side),
            "final_score_100": float(sc.final_score_100),
            "decision_bucket": _bucket(sc.decision_bucket),
            "confidence_bucket": _bucket(sc.confidence_bucket),
            "hard_gate_passed": bool(sc.hard_gate_passed),
            "execution_eligible": bool(sc.execution_eligible),
            "reason_codes": list(sc.reason_codes),
            "components": {k: float(v) for k, v in sc.component_scores.items()},
        })
    return rows


def rank_rows(rows):
    return sorted(rows, key=lambda r: (-r["final_score_100"], r["symbol"], r["side"]))


def explain_pair(better, worse):
    deltas = []
    for name in _COMPONENT_ORDER:
        b = better["components"].get(name)
        w = worse["components"].get(name)
        if b is None or w is None:
            continue
        d = round(b - w, 2)
        if d != 0:
            deltas.append((name, d))
    deltas.sort(key=lambda x: -x[1])
    return {
        "better": better["symbol"], "worse": worse["symbol"],
        "score_delta": round(better["final_score_100"] - worse["final_score_100"], 2),
        "top_component_advantages": deltas[:5],
    }


def build_result(signals, profile=ScoringProfile.RESEARCH):
    rows = rank_rows(score_rows(signals, profile=profile))
    pair = explain_pair(rows[0], rows[-1]) if len(rows) >= 2 else None
    return {
        "profile": _bucket(profile),
        "n_signals": len(signals),
        "n_scored": len(rows),
        "execution_eligible_any": any(r["execution_eligible"] for r in rows),
        "any_hard_gate_passed": any(r["hard_gate_passed"] for r in rows),
        "ranked": rows,
        "why_top_over_bottom": pair,
    }


def render(result, data_source="simulated_fixture"):
    L = []
    L.append("# M21.1 — Score & Rank (research-grade, read-only)")
    L.append("")
    L.append("- report_type: **M19 score + rank over scanner signals (via the "
             "M21.1 scoring bridge)**")
    L.append("- data_source: **%s**" % data_source)
    L.append("- scoring_profile: **RESEARCH**")
    L.append("- engine: **bot.signal_scoring (M19 public API; gates.py "
             "model_readiness downgraded to REVIEW under RESEARCH only)**")
    L.append("- signals_scored: **%d**" % result["n_scored"])
    L.append("- execution_eligible_any: **%s** (must be false)"
             % str(result["execution_eligible_any"]).lower())
    L.append("")
    L.append("> **Honesty statement (read this).** These are RESEARCH-GRADE "
             "rankings, NOT calibrated live probabilities and NOT execution "
             "approval. ML readiness is NOT passed — no model has been trained "
             "on real outcome data yet (that is M21.1extra). Under the RESEARCH "
             "profile, 'model not ready' and 'calibration unavailable' are "
             "MANUAL_REVIEW, so candidates can be ranked by component quality. "
             "Under the STRICT (live) profile these same candidates remain "
             "hard-BLOCKED. execution_eligible is False on every candidate. No "
             "runtime / broker / live / paper / Telegram path is touched.")
    L.append("")
    L.append("## Ranked candidates (by composite score)")
    L.append("")
    L.append("| rank | symbol | side | score | decision | confidence | "
             "gate | exec_eligible |")
    L.append("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(result["ranked"], 1):
        L.append("| %d | `%s` | %s | %.2f | %s | %s | %s | %s |"
                 % (i, r["symbol"], r["side"], r["final_score_100"],
                    r["decision_bucket"], r["confidence_bucket"],
                    "pass" if r["hard_gate_passed"] else "review/block",
                    str(r["execution_eligible"]).lower()))
    L.append("")
    L.append("## Component breakdown (0–100 each)")
    L.append("")
    L.append("| symbol | " + " | ".join(_COMPONENT_ORDER) + " |")
    L.append("|" + "---|" * (len(_COMPONENT_ORDER) + 1))
    for r in result["ranked"]:
        cells = " | ".join("%.1f" % r["components"].get(n, float("nan"))
                           for n in _COMPONENT_ORDER)
        L.append("| `%s` | %s |" % (r["symbol"], cells))
    L.append("")
    if result["why_top_over_bottom"]:
        p = result["why_top_over_bottom"]
        L.append("## Why `%s` ranks above `%s`" % (p["better"], p["worse"]))
        L.append("")
        L.append("- composite score delta: **%.2f**" % p["score_delta"])
        L.append("- top component advantages:")
        for name, d in p["top_component_advantages"]:
            L.append("  - `%s`: **+%.2f**" % (name, d))
        L.append("")
    L.append("## Safety confirmation")
    L.append("")
    L.append("- research-grade only; STRICT/live remains hard-blocked until a "
             "real trained model exists")
    L.append("- ML readiness NOT passed; no calibrated probability invented; "
             "prediction_calibrated stays null")
    L.append("- execution_eligible = False on every candidate")
    L.append("- no runtime wiring; no main.py change; no IBKR paper order; no "
             "eToro; no broker / live / paper; no Telegram")
    L.append("- M19 public API unchanged (44 names); only gates.py behaviour "
             "for RESEARCH model_readiness changed")
    L.append("")
    return "\n".join(L)


def fixture_signals():
    ts = "2026-06-26T15:00:00+00:00"
    base = lambda **kw: dict(timestamp=ts, available_tfs=4,
                             avg_volume_20d=500000, **kw)  # noqa: E731
    return [
        base(symbol="AAA", direction="long", entry_price=100.0, stop_loss=95.0,
             target_price=115.0, rsi=62.0, macd_hist=0.9, vol_ratio=1.4,
             valid_count=4, atr=2.0),
        base(symbol="BBB", direction="long", entry_price=50.0, stop_loss=48.0,
             target_price=56.0, rsi=55.0, macd_hist=0.3, vol_ratio=1.1,
             valid_count=3, atr=1.2),
        base(symbol="CCC", direction="long", entry_price=200.0, stop_loss=190.0,
             target_price=230.0, rsi=70.0, macd_hist=1.2, vol_ratio=1.5,
             valid_count=4, atr=3.0),
        base(symbol="DDD", direction="long", entry_price=20.0, stop_loss=19.5,
             target_price=21.0, rsi=48.0, macd_hist=0.0, vol_ratio=0.8,
             valid_count=1, atr=0.4),
    ]


def run_live(focus_size=150):
    """Score+rank the REAL scan_cycle output (e.g. DATA_PROVIDER=alpaca set by
    the caller). conn=None, no side effects. For VPS /tmp use only."""
    from bot.scanner import scan_cycle
    from bot.universe.active_selection import get_scan_ready_symbols
    focus = get_scan_ready_symbols()[:focus_size]
    config = {"strategy": "default",
              "routing": {"etoro_min_tfs": 4, "ibkr_min_tfs": 2, "min_valid_tfs": 1}}
    signals, _meta = scan_cycle(focus, config, conn=None, cycle_id=0)
    return build_result(signals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    ap.add_argument("--focus-size", type=int, default=150)
    ap.add_argument("--report", default="reports/m21_1_scoring_bridge_readonly.md")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()
    if args.mode == "live":
        result = run_live(focus_size=args.focus_size)
        data_source = "live_alpaca_scan_cycle"
    else:
        result = build_result(fixture_signals())
        data_source = "simulated_fixture"
    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(render(result, data_source=data_source), encoding="utf-8")
    print("wrote %s" % rp)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2))
        print("wrote %s" % args.json_out)
    print("mode=%s profile=%s scored=%d exec_any=%s gate_passed_any=%s"
          % (args.mode, result["profile"], result["n_scored"],
             result["execution_eligible_any"], result["any_hard_gate_passed"]))


if __name__ == "__main__":
    main()
