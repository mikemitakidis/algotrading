#!/usr/bin/env python3
"""M21.UR — UK pilot dry-run / check (explicit opt-in, read-only).

Verifies scanner-behaviour for the 5 UK pilot symbols ONLY, using the M21.UQ
provider/evaluator path as the scanner-behaviour proxy. It does NOT import or
run bot/scanner.py, constructs no orders, sends no Telegram, and touches no
broker/live/paper code. It does not change default runtime: the US default
scan-ready set (536) is never loaded here.

Scope guard (hard): the dry-run loads ONLY configs/universe/uk_pilot.json and
cross-checks its tickers against bot.universe.uk_pilot.get_uk_pilot_symbols().
It never reads global_expanded.json or the US default paths. If the pilot file
ever contained a non-UK / non-.L / HK symbol, the run aborts.

Default run is offline/simulated (no provider). Live yfinance is explicit-only
via --provider yfinance and writes its report only where the caller points it
(on the VPS: /tmp). No live report is committed to the repo.
"""
import argparse
import datetime
import json
import time
from pathlib import Path

from bot.universe.uk_pilot import get_uk_pilot_symbols, UK_PILOT_PATH
from tools.universe_quality.evaluators import evaluate_candidate
from tools.universe_quality.quality_model import OHLCVConfig

_EXPECTED = ["AAF.L", "AAL.L", "ABDN.L", "ABF.L", "ADM.L"]


def load_pilot_records():
    """Load the 5 pilot records from uk_pilot.json and verify the scope.

    Raises ValueError if the file's tickers do not match the accessor, or if
    any record is not a UK/.L symbol (defensive: never let a non-pilot or HK
    symbol through).
    """
    recs = json.loads(Path(UK_PILOT_PATH).read_text())["symbols"]
    accessor = set(get_uk_pilot_symbols())
    file_syms = set((r.get("provider_symbols") or {}).get("yfinance")
                    for r in recs)
    if file_syms != accessor:
        raise ValueError("pilot file tickers %s != accessor %s"
                         % (sorted(file_syms), sorted(accessor)))
    if accessor != set(_EXPECTED):
        raise ValueError("pilot set %s != expected %s"
                         % (sorted(accessor), _EXPECTED))
    for r in recs:
        yf = (r.get("provider_symbols") or {}).get("yfinance")
        if not yf or not yf.endswith(".L") or r.get("region") != "UK":
            raise ValueError("non-UK/.L symbol in pilot: %r" % r.get(
                "internal_symbol"))
        if yf.endswith(".HK"):
            raise ValueError("HK symbol must never appear in pilot: %s" % yf)
    return recs


def run_dryrun(provider=None, cfg=None, as_of=None):
    """Evaluate the 5 pilot records, timing each fetch. Returns a result dict.

    provider: None (offline structural) or a ProviderProtocol (e.g.
    YFinanceProvider) for a live/simulated fetch. Reuses the M21.UQ
    evaluate_candidate so provider errors are classified honestly
    (provider_rate_limited / provider_fetch_error) and never mislabelled as
    ohlcv_empty / volume_missing_or_zero.
    """
    cfg = cfg or OHLCVConfig()
    as_of = as_of or datetime.date.today().isoformat()
    recs = load_pilot_records()
    per_symbol = []
    t0 = time.monotonic()
    for r in recs:
        yf = r["provider_symbols"]["yfinance"]
        s0 = time.monotonic()
        res = evaluate_candidate(r, provider=provider, cfg=cfg, as_of=as_of)
        elapsed = time.monotonic() - s0
        per_symbol.append({
            "internal_symbol": r["internal_symbol"],
            "provider_symbol": yf,
            "passed": res.passed,
            "reason_codes": list(res.reason_codes),
            "warnings": list(res.warnings),
            "bar_count": res.details.get("bar_count"),
            "provider_error_text": res.details.get("provider_error_text", ""),
            "elapsed_seconds": round(elapsed, 4),
        })
    total_elapsed = time.monotonic() - t0
    return {
        "symbols_checked": [p["provider_symbol"] for p in per_symbol],
        "n_symbols": len(per_symbol),
        "provider_mode": "none / structural-only" if provider is None
        else "yfinance",
        "total_elapsed_seconds": round(total_elapsed, 4),
        "per_symbol": per_symbol,
    }


def render(result, data_source="simulated_fixture"):
    """Render a markdown dry-run report. data_source provenance, same discipline
    as M21.UQ: 'structural_only' / 'live_yfinance' / 'simulated_fixture'."""
    network = "enabled" if data_source == "live_yfinance" else "disabled"
    not_live = "false" if data_source == "live_yfinance" else "true"
    rate_limited = [p["provider_symbol"] for p in result["per_symbol"]
                    if "provider_rate_limited" in p["reason_codes"]]
    fetch_errors = [p["provider_symbol"] for p in result["per_symbol"]
                    if "provider_fetch_error" in p["reason_codes"]]
    data_fails = [p["provider_symbol"] for p in result["per_symbol"]
                  if any(c in p["reason_codes"] for c in
                         ("ohlcv_empty", "ohlcv_too_few_bars", "ohlcv_stale",
                          "ohlcv_non_finite", "volume_missing_or_zero"))]
    passed = [p["provider_symbol"] for p in result["per_symbol"]
              if p["passed"]]
    L = []
    L.append("# M21.UR — UK Pilot Dry-Run / Check")
    L.append("")
    L.append("- report_type: **UK pilot dry-run (scanner-behaviour proxy via "
             "M21.UQ evaluator)**")
    L.append("- scope: **UK pilot 5 symbols only**")
    L.append("- data_source: **%s**" % data_source)
    L.append("- network: **%s**" % network)
    L.append("- provider_mode: **%s**" % result["provider_mode"])
    L.append("- not_live_yfinance: **%s**" % not_live)
    L.append("- symbols_checked: **%d** (%s)"
             % (result["n_symbols"], ", ".join(result["symbols_checked"])))
    L.append("- total_elapsed_seconds: **%s**"
             % result["total_elapsed_seconds"])
    L.append("")
    L.append("> Read-only. Uses the M21.UQ provider/evaluator path as the "
             "scanner-behaviour proxy; does NOT import or run bot/scanner.py, "
             "constructs no orders, sends no Telegram, touches no broker / "
             "live / paper code. Default US runtime is unchanged (the 536 US "
             "scan-ready set is never loaded here).")
    L.append("")
    L.append("## Per-symbol result")
    L.append("")
    L.append("| symbol | passed | reason_codes | bar_count | elapsed_s |")
    L.append("|---|---|---|---|---|")
    for p in result["per_symbol"]:
        codes = ", ".join("`%s`" % c for c in p["reason_codes"]) or "—"
        L.append("| `%s` | %s | %s | %s | %s |"
                 % (p["provider_symbol"], "yes" if p["passed"] else "no",
                    codes, p["bar_count"], p["elapsed_seconds"]))
    L.append("")
    L.append("## Provider availability vs data quality (separated)")
    L.append("")
    L.append("- provider_rate_limited (could not evaluate — throttle): %s"
             % (", ".join("`%s`" % s for s in rate_limited) or "none"))
    L.append("- provider_fetch_error (could not evaluate — provider/network): "
             "%s" % (", ".join("`%s`" % s for s in fetch_errors) or "none"))
    L.append("- data-quality failures (real empty/stale/too-few/volume): %s"
             % (", ".join("`%s`" % s for s in data_fails) or "none"))
    L.append("- passed: %s"
             % (", ".join("`%s`" % s for s in passed) or "none"))
    L.append("")
    L.append("## Safety confirmation")
    L.append("")
    L.append("- default runtime unchanged; US default scan-ready set not "
             "loaded here")
    L.append("- no `_DEFAULT_PATHS` change; no `global_expanded.json` / "
             "`source_registry.json` change")
    L.append("- no broker / live / paper routing; no orders; no Telegram")
    L.append("- explicit opt-in only; UK pilot 5 symbols only; no HK; no "
             "Europe/Japan/China/ADR")
    L.append("")
    return "\n".join(L)


def build_provider(name, timeout=20, pace_seconds=0.0):
    if name == "none":
        return None
    if name == "yfinance":
        from tools.universe_quality.yfinance_provider import YFinanceProvider
        return YFinanceProvider(timeout=timeout, pace_seconds=pace_seconds)
    raise ValueError("unknown provider: %s" % name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("none", "yfinance"),
                    default="none")
    ap.add_argument("--report", default="reports/m21ur_uk_pilot_dryrun.md")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--pace-seconds", type=float, default=0.0)
    args = ap.parse_args()
    provider = build_provider(args.provider, timeout=args.timeout,
                              pace_seconds=args.pace_seconds)
    data_source = "structural_only" if provider is None else "live_yfinance"
    result = run_dryrun(provider=provider)
    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(render(result, data_source=data_source), encoding="utf-8")
    print("wrote %s" % rp)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2))
        print("wrote %s" % args.json_out)
    print("provider=%s symbols=%d elapsed=%ss"
          % (result["provider_mode"], result["n_symbols"],
             result["total_elapsed_seconds"]))


if __name__ == "__main__":
    main()
