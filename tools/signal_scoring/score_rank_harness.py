#!/usr/bin/env python3
"""M21.1 — Score & Rank harness (read-only, research-grade).

Runs scanner signals through the scoring bridge, ranks them by M19's
transparent composite (``research_rank_score``, computed even when a hard gate
blocks) under the RESEARCH profile, and renders an explainable report:
research score, M19 final score + buckets, 11-component breakdown, gate
failures, and the A01 truth report (which inputs were measured and which are
UNKNOWN), plus a "why A ranks above B" comparison.

Honesty contract (stated in every report):
  - scores are RESEARCH-GRADE rankings, not calibrated live probabilities
  - not execution approval; execution_eligible is False on every candidate
  - ML readiness is NOT passed (no trained model / no outcome data yet)
  - A01: gate inputs are measured or UNKNOWN; an UNKNOWN input blocks in M19
  - STRICT/live scoring stays hard-blocked until M21.1extra produces outcomes

Tool only: nothing in bot/ imports it. No orders, no broker / live / paper / DB /
Telegram. No runtime wiring.
"""
import argparse
import json
from datetime import date, datetime, time, timezone
from pathlib import Path

from bot.signal_scoring import ScoringProfile
from bot.scoring_truth import SessionCalendar
from tools.signal_scoring.scanner_bridge import score_signal_with_truth

_COMPONENT_ORDER = [
    "ml", "scanner", "technical_confluence", "trend", "momentum",
    "volume_liquidity", "volatility", "market_regime", "risk_adjusted",
    "data_quality", "calibration_uncertainty",
]


def _bucket(b):
    return getattr(b, "value", b)


def _row_from_scored(ts):
    sc, tr = ts.scored, ts.truth
    if ts.execution_eligible is not False:      # raises if M19 ever says True
        raise AssertionError("execution_eligible must be False")
    return {
        "symbol": sc.symbol,
        "side": _bucket(sc.side),
        "research_rank_score": round(ts.research_rank_score, 6),
        "final_score_100": float(sc.final_score_100),
        "decision_bucket": _bucket(sc.decision_bucket),
        "confidence_bucket": _bucket(sc.confidence_bucket),
        "hard_gate_passed": bool(sc.hard_gate_passed),
        "hard_gate_failures": list(sc.hard_gate_failures),
        "execution_eligible": bool(sc.execution_eligible),
        "reason_codes": list(sc.reason_codes),
        "components": {k: float(v) for k, v in sc.component_scores.items()},
        "unknown_inputs": list(tr.unknown_fields),
        "observations": tr.observation_states(),
        "risk_authority": tr.risk.state.value,
        "corporate_action": tr.corporate_action.state.value,
    }


def score_rows(signals, profile=ScoringProfile.RESEARCH, truth_for=None):
    """``truth_for(signal) -> dict`` supplies the evidence kwargs per signal
    (bar_asof, calendar, evaluated_at_utc, risk_decision, ...). Without it
    every gate input is UNKNOWN and every candidate is M19-BLOCKED."""
    rows = []
    for sig in signals:
        kw = truth_for(sig) if truth_for else {}
        rows.append(_row_from_scored(
            score_signal_with_truth(sig, profile=profile, **kw)))
    return rows


def rank_rows(rows):
    return sorted(rows, key=lambda r: (-r["research_rank_score"], r["symbol"], r["side"]))


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
        "score_delta": round(better["research_rank_score"]
                             - worse["research_rank_score"], 2),
        "top_component_advantages": deltas[:5],
    }


def build_result(signals, profile=ScoringProfile.RESEARCH, truth_for=None):
    rows = rank_rows(score_rows(signals, profile=profile, truth_for=truth_for))
    pair = explain_pair(rows[0], rows[-1]) if len(rows) >= 2 else None
    return {
        "profile": _bucket(profile),
        "n_signals": len(signals),
        "n_scored": len(rows),
        "execution_eligible_any": any(r["execution_eligible"] for r in rows),
        "any_hard_gate_passed": any(r["hard_gate_passed"] for r in rows),
        "n_m19_blocked": sum(r["decision_bucket"] == "BLOCKED" for r in rows),
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
    L.append("> **A01 truth contract.** Every M19 gate input is MEASURED from "
             "evidence or UNKNOWN; UNKNOWN inputs are removed so M19 fails "
             "closed. Risk Authority is never 'ok' without an actual matching "
             "decision. Candidates are ordered by `research_rank_score` (the "
             "composite M19 computes even when a gate blocks); the M19 final "
             "score of a BLOCKED candidate is 0. M19-blocked: **%d/%d**."
             % (result.get("n_m19_blocked", 0), result["n_scored"]))
    L.append("")
    L.append("## Ranked candidates (by research_rank_score)")
    L.append("")
    L.append("| rank | symbol | side | research score | M19 final | decision | "
             "gate failures | exec_eligible |")
    L.append("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(result["ranked"], 1):
        L.append("| %d | `%s` | %s | %.2f | %.2f | %s | %s | %s |"
                 % (i, r["symbol"], r["side"], r["research_rank_score"],
                    r["final_score_100"], r["decision_bucket"],
                    ", ".join(r["hard_gate_failures"]) or "—",
                    str(r["execution_eligible"]).lower()))
    L.append("")
    L.append("## Input truth (A01)")
    L.append("")
    L.append("| symbol | observations | risk authority | corporate action | "
             "UNKNOWN inputs |")
    L.append("|---|---|---|---|---|")
    for r in result["ranked"]:
        obs = ", ".join("%s=%s" % kv for kv in r["observations"].items()) or "not supplied"
        L.append("| `%s` | %s | %s | %s | %s |"
                 % (r["symbol"], obs, r["risk_authority"], r["corporate_action"],
                    ", ".join("`%s`" % u for u in r["unknown_inputs"]) or "none"))
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
        L.append("- research score delta: **%.2f**" % p["score_delta"])
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
    L.append("- A01: no gate input defaulted; Risk Authority never 'ok' without "
             "a decision; corporate-action safety never inferred from a "
             "detector that found nothing")
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


# Simulated fixture calendar for the committed fixture report ONLY. It is not
# a verified exchange calendar and must never be used for real data.
FIXTURE_CALENDAR = SessionCalendar(
    name="simulated-fixture-us-2026", tz="America/New_York",
    regular_open=time(9, 30), regular_close=time(16, 0),
    coverage_start=date(2026, 6, 1), coverage_end=date(2026, 7, 31),
    holidays=frozenset({date(2026, 6, 19), date(2026, 7, 3)}),
    source="simulated fixture (not verified)")


def fixture_truth(signal):
    """Evidence for the fixture signals: bars as the scanner would record
    them at 2026-06-26 15:00Z (11:00 New York), raw prices with a clean
    split-detector run but NO verified corporate-action record, and NO risk
    decision. The resulting UNKNOWN inputs are the honest outcome."""
    return {
        "evaluated_at_utc": "2026-06-26T15:00:00+00:00",
        "calendar": FIXTURE_CALENDAR,
        "bar_asof": {"1D": "2026-06-26T04:00:00+00:00",
                     "4H": "2026-06-26T12:00:00+00:00",
                     "1H": "2026-06-26T14:30:00+00:00",
                     "15m": "2026-06-26T14:45:00+00:00"},
        "price_basis": "raw",
        "detector_suspected": False,
    }


def _live_liquidity_map(symbols, period="6mo", interval="1d"):
    """Honestly derive avg_dollar_volume_20d per symbol from REAL daily bars,
    using the same read-only fetch the scanner uses. Returns {sym: adv or None}.

    avg_dollar_volume_20d = last_close * mean(volume[-20:]). No constants, no
    fabrication: a symbol with insufficient/absent bars maps to None, and the
    bridge then marks its liquidity unavailable rather than inventing a value.
    This reads bars only; it places no orders and writes nothing.
    """
    from bot.data import fetch_bars
    out = {}
    bars_by_sym = fetch_bars(list(symbols), period, interval) or {}
    for sym in symbols:
        df = bars_by_sym.get(sym)
        try:
            if df is None or len(df) < 20:
                out[sym] = None
                continue
            vol = df["volume"].astype(float)
            close = df["close"].astype(float)
            adv_vol = float(vol.iloc[-20:].mean())
            last_close = float(close.iloc[-1])
            out[sym] = last_close * adv_vol if adv_vol > 0 else None
        except (KeyError, ValueError, TypeError, IndexError):
            out[sym] = None
    return out


def run_live(focus_size=150):
    """Score+rank the REAL scan_cycle output (e.g. DATA_PROVIDER=alpaca set by
    the caller). conn=None, no side effects. For VPS /tmp use only.

    Real scan_cycle signals do NOT carry avg_volume; liquidity is derived here
    from real daily bars (see _live_liquidity_map) and passed per-signal into
    the bridge. A symbol whose bars are missing is honestly left without
    liquidity (the M19 gate then treats it as missing, not faked-present)."""
    from bot.scanner import scan_cycle
    from bot.universe.active_selection import get_scan_ready_symbols
    focus = get_scan_ready_symbols()[:focus_size]
    config = {"strategy": "default",
              "routing": {"etoro_min_tfs": 4, "ibkr_min_tfs": 2, "min_valid_tfs": 1}}
    signals, meta = scan_cycle(focus, config, conn=None, cycle_id=0)
    evaluated_at = datetime.now(timezone.utc)
    asof = (meta or {}).get("bar_asof") or {}
    liq = _live_liquidity_map(sorted({s["symbol"] for s in signals}))
    rows = []
    for sig in signals:
        adv = liq.get(sig["symbol"])
        # A01: the scanner's own as-of record + the evaluation time. No
        # verified calendar, risk decision or corporate-action record is
        # available to this tool, so those inputs are honestly UNKNOWN.
        ts = score_signal_with_truth(
            sig, profile=ScoringProfile.RESEARCH, avg_dollar_volume=adv,
            evaluated_at_utc=evaluated_at, bar_asof=asof.get(sig["symbol"], {}))
        rows.append(_row_from_scored(ts))
    rows = rank_rows(rows)
    pair = explain_pair(rows[0], rows[-1]) if len(rows) >= 2 else None
    return {
        "profile": _bucket(ScoringProfile.RESEARCH),
        "n_signals": len(signals), "n_scored": len(rows),
        "execution_eligible_any": any(r["execution_eligible"] for r in rows),
        "any_hard_gate_passed": any(r["hard_gate_passed"] for r in rows),
        "n_m19_blocked": sum(r["decision_bucket"] == "BLOCKED" for r in rows),
        "ranked": rows, "why_top_over_bottom": pair,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    ap.add_argument("--focus-size", type=int, default=150)
    ap.add_argument("--report", default="reports/m21_1_scoring_bridge_readonly.md")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()
    if args.mode == "live":
        # Live output is NEVER committed: enforce /tmp-only so a live run can
        # never overwrite the committed simulated report or land under reports/.
        rp = Path(args.report)
        jp = Path(args.json_out) if args.json_out else None
        if rp.resolve() == Path("reports/m21_1_scoring_bridge_readonly.md").resolve() \
                or not str(rp.resolve()).startswith("/tmp/"):
            raise SystemExit(
                "live mode must write the report under /tmp/ "
                "(got %r); pass e.g. --report /tmp/m21_1_live.md" % str(rp))
        if jp is not None and not str(jp.resolve()).startswith("/tmp/"):
            raise SystemExit(
                "live mode must write --json-out under /tmp/ (got %r)"
                % str(jp))
        result = run_live(focus_size=args.focus_size)
        data_source = "live_alpaca_scan_cycle"
    else:
        result = build_result(fixture_signals(), truth_for=fixture_truth)
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
