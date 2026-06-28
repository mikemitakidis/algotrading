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


def render(records, results):
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
    L = []
    L.append("# M21.UQ — Global Quality Collectors / Gates — Dry-Run Report")
    L.append("")
    L.append("- report_type: **offline structural dry-run**")
    L.append("- source_file: `configs/universe/global_expanded.json`")
    L.append("- scope: **existing global candidates only**")
    L.append("- network: **disabled**")
    L.append("- provider_mode: **none / structural-only**")
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--global", dest="global_path",
                    default=str(_DEFAULT_GLOBAL))
    ap.add_argument("--report",
                    default="reports/m21uq_quality_collectors_plan_or_dryrun"
                            ".md")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()
    records = json.loads(Path(args.global_path).read_text())["symbols"]
    results = evaluate_all(records)  # offline structural run
    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(render(records, results), encoding="utf-8")
    print("wrote %s" % rp)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([r.to_dict() for r in results], indent=2))
        print("wrote %s" % args.json_out)
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    print("evaluated=%d passed=%d failed=%d" % (total, passed, total - passed))


if __name__ == "__main__":
    main()
