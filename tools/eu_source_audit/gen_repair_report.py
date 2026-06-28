#!/usr/bin/env python3
"""Generate reports/m21u4_europe_source_repair.md from repair_findings.

Read-only. No curation, no global_expanded.json / source_registry.json /
runtime / scan_ready changes. Renders the structured findings into a reviewable
markdown report.

Usage:
  python3 -m tools.eu_source_audit.gen_repair_report \
    [--report reports/m21u4_europe_source_repair.md]
"""
import argparse
import datetime
import os
import subprocess
from pathlib import Path

from tools.eu_source_audit.repair_findings import FINDINGS

_ORDER = ["dax", "smi", "aex", "cac", "ibex"]


def _git(*args):
    try:
        p = subprocess.run(["git", *args], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else "(unknown)"
    except Exception:  # noqa: BLE001
        return "(unknown)"


def _run_env():
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "GitHub Actions"
    if Path("/opt/algo-trader").exists():
        return "VPS"
    return "local"


def render():
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    head = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain")
    L = []
    L.append("# M21.U4 Europe — Source Repair Report")
    L.append("")
    L.append("Generated: %s" % now)
    L.append("")
    L.append("- run_environment: **%s**" % _run_env())
    L.append("- generated_at_git_branch: `%s`" % branch)
    L.append("- generated_at_git_head: `%s`" % head)
    L.append("- generated_at_git_status: **%s**" % ("dirty" if dirty
                                                    else "clean"))
    L.append("")
    L.append("> The `generated_at_git_*` fields above are the git state when "
             "this report was generated (typically before the commit that "
             "includes it), NOT the final commit state of this file. They are "
             "run provenance, not a claim about the committed tree.")
    L.append("")
    L.append("Read-only analysis. No curation; no `global_expanded.json` / "
             "`source_registry.json` / runtime / scan_ready changes. The "
             "findings derive from the merged audit tool's VPS run plus "
             "documented endpoint behaviour; no new live probing was done to "
             "author this report. Endpoint re-verification must go through the "
             "audit Action/VPS.")
    L.append("")
    L.append("## Classification legend")
    L.append("")
    L.append("- **ACCEPT_OFFICIAL** — official index/exchange source, exact "
             "count, machine-usable.")
    L.append("- **ACCEPT_FALLBACK_EXACT** — reputable ETF holdings, exact "
             "official count, membership unverified.")
    L.append("- **FALLBACK_INCOMPLETE** — reputable ETF reachable but sample "
             "≠ official count.")
    L.append("- **BLOCKED_NEEDS_MANUAL_SOURCE** — no machine-usable source; an "
             "official file must be supplied once.")
    L.append("")
    # summary table
    L.append("## Summary")
    L.append("")
    L.append("| Venue | Index | Suffix | Expected | Audit | Fallback | "
             "Classification |")
    L.append("|---|---|---|---|---|---|---|")
    for v in _ORDER:
        f = FINDINGS[v]
        L.append("| %s | %s | %s | %d | %s | %s | **%s** |"
                 % (v.upper(), f["index"], f["suffix"], f["expected"],
                    f["audit_result"], f["fallback_exact_or_incomplete"],
                    f["classification"]))
    L.append("")
    # per-venue detail
    for v in _ORDER:
        f = FINDINGS[v]
        L.append("## %s (%s, %s, expected %d)"
                 % (v.upper(), f["index"], f["exchange"], f["expected"]))
        L.append("")
        L.append("1. **Current audit result:** %s" % f["audit_result"])
        L.append("2. **Current source & why it failed:** %s — %s"
                 % (f["current_source"], f["why_failed"]))
        L.append("3. **Better sources considered:**")
        for role, name, note in f["better_sources"]:
            L.append("   - `%s` — %s: %s" % (role, name, note))
        L.append("4. **Exact count obtainable?** %s"
                 % f["exact_count_possible"])
        L.append("5. **Fallback exact or incomplete?** %s"
                 % f["fallback_exact_or_incomplete"])
        L.append("6. **Classification:** **%s**" % f["classification"])
        L.append("   - manual file that would unblock: %s"
                 % f["manual_file_needed"])
        L.append("")
    # decision section
    L.append("## Decision required")
    L.append("")
    incomplete = [v.upper() for v in _ORDER
                  if FINDINGS[v]["classification"] == "FALLBACK_INCOMPLETE"]
    blocked = [v.upper() for v in _ORDER if FINDINGS[v]["classification"]
               == "BLOCKED_NEEDS_MANUAL_SOURCE"]
    official = [v.upper() for v in _ORDER if FINDINGS[v]["classification"]
                == "ACCEPT_OFFICIAL"]
    fb_exact = [v.upper() for v in _ORDER if FINDINGS[v]["classification"]
                == "ACCEPT_FALLBACK_EXACT"]
    L.append("- ACCEPT_OFFICIAL: %s" % (", ".join(official) or "none"))
    L.append("- ACCEPT_FALLBACK_EXACT: %s" % (", ".join(fb_exact) or "none"))
    L.append("- FALLBACK_INCOMPLETE: %s" % (", ".join(incomplete) or "none"))
    L.append("- BLOCKED_NEEDS_MANUAL_SOURCE: %s"
             % (", ".join(blocked) or "none"))
    L.append("")
    L.append("No venue currently qualifies as ACCEPT_OFFICIAL or "
             "ACCEPT_FALLBACK_EXACT from a machine-reachable source. Three "
             "ways forward (not mutually exclusive):")
    L.append("")
    L.append("**A) Endpoint-id repair (engineering, GitHub-first).** SMI/AEX/"
             "CAC/IBEX failed on wrong/unreachable ETF endpoint *ids*, not on "
             "a proven absence of an exact source. For fully-replicated "
             "indices (SMI 20, AEX 25, IBEX 35, CAC 40) a correct iShares/UBS/"
             "Amundi holdings CSV would likely yield the exact count. Next "
             "step: correct the endpoint ids in `venues.py`, re-run the audit "
             "Action, and any venue that returns the exact count becomes "
             "ACCEPT_FALLBACK_EXACT. This needs no manual files.")
    L.append("")
    L.append("**B) Operator approves labelled ETF fallback.** If a "
             "`reputable_etf_fallback` set is acceptable as the spine for "
             "inactive/unverified candidates (the HK TraHK→HSIL posture), then "
             "ACCEPT_FALLBACK_EXACT venues curate immediately, and DAX could "
             "proceed as a labelled 38-name subset (explicitly NOT the official "
             "40) if you accept incompleteness. Membership is reconciled later "
             "at a quality gate.")
    L.append("")
    L.append("**C) Supply official manual files.** For guaranteed official "
             "membership, provide one file per venue (see each venue's "
             "\"manual file that would unblock\"). These are dynamic / not "
             "server-fetchable, so a one-time download+upload is the only "
             "authoritative route. DAX is the most likely to *need* this (its "
             "ETF is structurally 38/40).")
    L.append("")
    L.append("Recommended sequence: **A first** (cheap, automated, may resolve "
             "SMI/AEX/CAC/IBEX outright) → then **B or C** for whatever remains "
             "(notably DAX). Europe stays active throughout; no venue is "
             "abandoned.")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report",
                    default="reports/m21u4_europe_source_repair.md")
    args = ap.parse_args()
    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(render(), encoding="utf-8")
    print("wrote %s" % rp)


if __name__ == "__main__":
    main()
