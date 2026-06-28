#!/usr/bin/env python3
"""M21.UQ quality collectors/gates — read-only report runner.

Evaluates the EXISTING global candidates in global_expanded.json and writes a
markdown (and optional JSON) report of pass/fail/reason-codes. Read-only: never
writes global_expanded.json / source_registry.json, never sets scan_ready, never
touches runtime. By default runs WITHOUT a provider (structural checks only:
symbol/suffix/duplicate/liquidity), so it makes no network calls. A provider can
be injected programmatically for OHLCV checks; the CLI stays offline.

Usage:
  python3 -m tools.universe_quality.run_quality_report \
    [--global configs/universe/global_expanded.json] \
    [--report reports/m21uq_quality_collectors_plan_or_dryrun.md] \
    [--json-out reports/m21uq_quality_collectors_dryrun.json]
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from tools.universe_quality.evaluators import (
    evaluate_candidate, find_duplicate_provider_symbols)
from tools.universe_quality.quality_model import (
    PROVIDER_SYMBOL_DUPLICATE, QUALITY_FAIL, QUALITY_PASS)

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_GLOBAL = _REPO / "configs" / "universe" / "global_expanded.json"


def evaluate_all(records, provider=None, cfg=None, as_of=None):
    """Evaluate every record; inject PROVIDER_SYMBOL_DUPLICATE for symbols used
    more than once. Returns list[QualityResult]."""
    dups = find_duplicate_provider_symbols(records)
    results = []
    for r in records:
        res = evaluate_candidate(r, provider=provider, cfg=cfg, as_of=as_of)
        yf = res.provider_symbol
        if yf in dups and PROVIDER_SYMBOL_DUPLICATE not in res.reason_codes:
            res.reason_codes.append(PROVIDER_SYMBOL_DUPLICATE)
            res.passed = False
        results.append(res)
    return results


def select_records(records, region=None, symbols=None, limit=None):
    """Read-only selection/filtering of candidates for a provider run.

    region: 'UK'/'HK' (case-insensitive) -> filter by record region.
    symbols: iterable of internal_symbols or provider symbols to include.
    limit: cap the number returned (after region/symbol filtering).
    Returns a NEW list; never mutates the input records.
    """
    out = list(records)
    if region:
        rg = region.upper()
        out = [r for r in out if str(r.get("region", "")).upper() == rg]
    if symbols:
        want = set(symbols)
        out = [r for r in out
               if r.get("internal_symbol") in want
               or (r.get("provider_symbols") or {}).get("yfinance") in want]
    if limit is not None:
        out = out[:limit]
    return out


def render(records, results, provider_mode="none / structural-only",
           attempted=None):
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    by_region = Counter(r.get("region", "?") for r in records)
    code_counts = Counter()
    for r in results:
        for c in r.reason_codes:
            code_counts[c] += 1
        for w in r.warnings:
            code_counts[w] += 1
    network = "disabled" if provider_mode.startswith("none") else "enabled"
    L = []
    L.append("# M21.UQ — Global Quality Collectors / Gates — Dry-Run Report")
    L.append("")
    rtype = ("offline structural dry-run"
             if provider_mode.startswith("none")
             else "provider-backed dry-run")
    L.append("- report_type: **%s**" % rtype)
    L.append("- source_file: `configs/universe/global_expanded.json`")
    L.append("- scope: **existing global candidates only**")
    L.append("- network: **%s**" % network)
    L.append("- provider_mode: **%s**" % provider_mode)
    L.append("- attempted: **%d**" % (total if attempted is None
                                      else attempted))
    L.append("")
    L.append("> Read-only quality dry-run over EXISTING global candidates. No "
             "writes to global_expanded.json / source_registry.json, no "
             "scan_ready change, no runtime activation. Default run is offline "
             "(structural checks: provider-symbol, suffix, duplicate, "
             "liquidity); OHLCV checks run only when a provider is injected.")
    L.append("")
    L.append("## Summary")
    L.append("")
    L.append("- total_candidates: **%d**" % total)
    L.append("- region_breakdown: %s"
             % ", ".join("%s=%d" % (k, v) for k, v in sorted(
                 by_region.items())))
    L.append("- passed (no fatal codes): **%d**" % passed)
    L.append("- failed (>=1 fatal code): **%d**" % failed)
    L.append("- overall: **%s**"
             % (QUALITY_PASS if failed == 0 else QUALITY_FAIL))
    L.append("")
    L.append("## Reason / warning code counts")
    L.append("")
    if code_counts:
        L.append("| code | count |")
        L.append("|---|---|")
        for c, n in sorted(code_counts.items()):
            L.append("| `%s` | %d |" % (c, n))
    else:
        L.append("(no codes raised)")
    L.append("")
    L.append("## Failing candidates (first 50)")
    L.append("")
    fails = [r for r in results if not r.passed]
    if not fails:
        L.append("(none)")
    else:
        L.append("| internal_symbol | provider_symbol | reason_codes |")
        L.append("|---|---|---|")
        for r in fails[:50]:
            L.append("| `%s` | `%s` | %s |"
                     % (r.internal_symbol, r.provider_symbol,
                        ", ".join("`%s`" % c for c in r.reason_codes)))
    L.append("")
    L.append("## Warnings (non-fatal)")
    L.append("")
    warn_counts = Counter()
    for r in results:
        for w in r.warnings:
            warn_counts[w] += 1
    if warn_counts:
        for w, n in sorted(warn_counts.items()):
            L.append("- `%s`: %d candidates (non-fatal at this stage; "
                     "inactive candidates have null liquidity by design)" %
                     (w, n))
    else:
        L.append("(none)")
    L.append("")
    L.append("## OHLCV breakdown (provider-backed runs)")
    L.append("")
    ohlcv_codes = ("ohlcv_empty", "ohlcv_too_few_bars", "ohlcv_stale",
                   "ohlcv_non_finite", "volume_missing_or_zero")
    any_ohlcv = False
    for code in ohlcv_codes:
        hit = [r.internal_symbol for r in results if code in r.reason_codes]
        if hit:
            any_ohlcv = True
            shown = ", ".join("`%s`" % s for s in hit[:25])
            more = "" if len(hit) <= 25 else " (+%d more)" % (len(hit) - 25)
            L.append("- `%s`: %d — %s%s" % (code, len(hit), shown, more))
    if not any_ohlcv:
        L.append("(no OHLCV codes — structural-only run, or all OHLCV checks "
                 "passed)")
    L.append("")
    L.append("## Safety confirmation")
    L.append("")
    L.append("- read-only: no global_expanded.json / source_registry.json "
             "write")
    L.append("- no scan_ready change; no runtime activation; no scanner "
             "change")
    L.append("- this report evaluates existing candidates only; adds no "
             "symbols, no Europe/Japan/China/ADRs")
    L.append("")
    return "\n".join(L)


def build_provider(name, timeout=20, pace_seconds=0.0):
    """Construct a provider by name. 'none' -> None (offline). 'yfinance' ->
    YFinanceProvider. Unknown -> ValueError."""
    if name == "none":
        return None
    if name == "yfinance":
        from tools.universe_quality.yfinance_provider import YFinanceProvider
        return YFinanceProvider(timeout=timeout, pace_seconds=pace_seconds)
    raise ValueError("unknown provider: %s" % name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--global", dest="global_path",
                    default=str(_DEFAULT_GLOBAL))
    ap.add_argument("--report",
                    default="reports/m21uq_quality_collectors_plan_or_dryrun"
                            ".md")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--provider", choices=("none", "yfinance"),
                    default="none",
                    help="none = offline structural (default); yfinance = "
                         "provider-backed OHLCV (explicit, read-only, reports "
                         "only)")
    ap.add_argument("--region", default="", help="filter UK/HK")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--symbols", default="",
                    help="comma-separated internal or provider symbols")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--pace-seconds", type=float, default=0.0)
    args = ap.parse_args()

    all_records = json.loads(Path(args.global_path).read_text())["symbols"]
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] \
        or None
    records = select_records(all_records, region=args.region or None,
                             symbols=symbols, limit=args.limit)
    provider = build_provider(args.provider, timeout=args.timeout,
                              pace_seconds=args.pace_seconds)
    provider_mode = ("none / structural-only" if provider is None
                     else "yfinance")

    # duplicate detection is global (over the FULL set), so a filtered run
    # still flags a provider symbol duplicated elsewhere in the universe.
    results = evaluate_all(records, provider=provider)
    if provider is not None:
        # re-inject duplicates computed over the full universe
        from tools.universe_quality.evaluators import (
            find_duplicate_provider_symbols)
        from tools.universe_quality.quality_model import (
            PROVIDER_SYMBOL_DUPLICATE)
        dups = find_duplicate_provider_symbols(all_records)
        for r in results:
            if (r.provider_symbol in dups
                    and PROVIDER_SYMBOL_DUPLICATE not in r.reason_codes):
                r.reason_codes.append(PROVIDER_SYMBOL_DUPLICATE)
                r.passed = False

    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(render(records, results, provider_mode=provider_mode,
                         attempted=len(records)), encoding="utf-8")
    print("wrote %s" % rp)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([r.to_dict() for r in results], indent=2))
        print("wrote %s" % args.json_out)
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    print("provider=%s attempted=%d passed=%d failed=%d"
          % (provider_mode, total, passed, total - passed))


if __name__ == "__main__":
    main()
