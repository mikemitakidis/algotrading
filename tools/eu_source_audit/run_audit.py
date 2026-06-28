#!/usr/bin/env python3
"""Run the M21.U4 Europe source audit and write a markdown report.

Read-only with respect to the repo's universe data: it does NOT touch
global_expanded.json or source_registry.json, does NOT curate, does NOT change
runtime. It only writes the report file (and downloaded source files under the
--outdir for provenance).

Usage:
  python3 -m tools.eu_source_audit.run_audit \
    [--venues dax,smi,aex,cac,ibex] \
    [--outdir /tmp/m21u4_sources] \
    [--report reports/m21u4_europe_source_audit.md]
"""
import argparse
import datetime
import json
from pathlib import Path

from tools.eu_source_audit.venues import VENUES
from tools.eu_source_audit.audit import audit_venue


def _fmt_excluded(summary):
    if not summary:
        return "none"
    return ", ".join("%s×%d" % (k, v) for k, v in sorted(summary.items()))


def render(results):
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ")
    L = []
    L.append("# M21.U4 Europe — Source Audit Report")
    L.append("")
    L.append("Generated: %s" % now)
    L.append("")
    L.append("Read-only audit. No curation, no `global_expanded.json` or "
             "`source_registry.json` changes, no runtime activation.")
    L.append("")
    L.append("Source-role policy: `official_index` / `official_exchange` are "
             "preferred. `reputable_etf_fallback` (large physically-"
             "replicating ETF holdings) is acceptable ONLY when explicitly "
             "labelled — ETF holdings sample the index and need not equal "
             "official membership.")
    L.append("")
    # summary table
    L.append("## Summary")
    L.append("")
    L.append("| Venue | Index | Exch | Suffix | Expected | Best result | "
             "Verdict |")
    L.append("|---|---|---|---|---|---|---|")
    for r in results:
        m = r["meta"]
        best = "—"
        for a in r["attempts"]:
            if a["recommendation"] and (a["recommendation"].startswith(
                    "ACCEPT") or "REVIEW_NEEDED" in a["recommendation"]):
                inc = (len(a["inspection"]["included"])
                       if a["inspection"] else "—")
                best = "%s rows (%s)" % (inc, a["role"])
                if a["recommendation"].startswith("ACCEPT"):
                    break
        L.append("| %s | %s | %s | %s | %d | %s | **%s** |"
                 % (r["venue"].upper(), m["index"], m["exchange"],
                    m["suffix"], m["expected"], best, r["verdict"]))
    L.append("")
    # per-venue detail
    for r in results:
        m = r["meta"]
        L.append("## %s (%s, %s, %s) — expected %d"
                 % (r["venue"].upper(), m["index"], m["exchange"],
                    m["suffix"], m["expected"]))
        L.append("")
        L.append("Venue verdict: **%s**" % r["verdict"])
        L.append("")
        for i, a in enumerate(r["attempts"]):
            L.append("### Attempt %d — role: `%s`" % (i + 1, a["role"]))
            L.append("")
            L.append("- note: %s" % a.get("note", ""))
            L.append("- url: `%s`" % a["url"])
            L.append("- http_status: `%s`" % a["http_status"])
            L.append("- saved: %s" % ("yes" if a["saved"] else "no"))
            if a["saved"]:
                L.append("- file: `%s`" % a.get("file", ""))
                L.append("- sha256: `%s`" % a["sha256"])
                L.append("- bytes: %d" % a["bytes"])
            ins = a["inspection"]
            if ins:
                L.append("- as_of: `%s`" % ins["as_of"])
                L.append("- header_row: %s" % ins["header_row"])
                L.append("- detected_constituent_rows: **%d**"
                         % len(ins["included"]))
                L.append("- duplicate_tickers: %s"
                         % (", ".join(ins["duplicate_tickers"])
                            if ins["duplicate_tickers"] else "none"))
                L.append("- excluded_summary: %s"
                         % _fmt_excluded(ins["excluded_summary"]))
                if ins["included"]:
                    tks = ", ".join(t for t, _ in ins["included"])
                    L.append("- included_tickers (%d): %s"
                             % (len(ins["included"]), tks))
            L.append("- **recommendation: %s**" % a["recommendation"])
            L.append("")
    L.append("## Conclusion")
    L.append("")
    accepted = [r["venue"].upper() for r in results
                if r["verdict"] == "ACCEPT"]
    blocked = [r["venue"].upper() for r in results
               if r["verdict"] != "ACCEPT"]
    L.append("- ACCEPT (clean automated source): %s"
             % (", ".join(accepted) if accepted else "none"))
    L.append("- BLOCKED / REVIEW_NEEDED: %s"
             % (", ".join(blocked) if blocked else "none"))
    L.append("")
    L.append("Venues marked BLOCKED have no machine-downloadable source that "
             "yields the exact official constituent count. For those, the "
             "authoritative `official_index` file must be supplied once "
             "(it is dynamic / not server-fetchable), OR a "
             "`reputable_etf_fallback` set may be accepted as a labelled, "
             "unverified inactive-candidate batch pending a later membership "
             "cross-check (same posture as the HK TraHK→HSIL flow). No "
             "curation proceeds until a source is explicitly accepted.")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", default="dax,smi,aex,cac,ibex")
    ap.add_argument("--outdir", default="/tmp/m21u4_sources")
    ap.add_argument("--report",
                    default="reports/m21u4_europe_source_audit.md")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    venues = [v.strip().lower() for v in args.venues.split(",") if v.strip()]
    results = []
    for v in venues:
        if v not in VENUES:
            print("skip unknown venue: %s" % v)
            continue
        print("auditing %s ..." % v)
        results.append(audit_venue(v, VENUES[v], args.outdir))

    report = render(results)
    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(report, encoding="utf-8")
    print("wrote %s (%d bytes)" % (rp, len(report)))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2,
                                                  default=str))
        print("wrote %s" % args.json_out)


if __name__ == "__main__":
    main()
