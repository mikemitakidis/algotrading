#!/usr/bin/env python3
"""Generate reports/m21u4_europe_endpoint_repair.md.

Documents the endpoint-id repair for SMI/AEX/CAC/IBEX: old endpoint(s) -> new
candidate endpoint(s), source_role, expected count. The "fetched?/exact?/
verdict" columns are filled by RUNNING the audit (Action or VPS) against the
updated venues.py; this generator records the repair plan and leaves a clearly
marked placeholder for the live result so a committed report never carries a
fabricated fetch outcome.

Read-only. No curation, no global_expanded.json / source_registry.json /
runtime / scan_ready changes.

Usage:
  python3 -m tools.eu_source_audit.gen_endpoint_repair_report \
    [--report reports/m21u4_europe_endpoint_repair.md]
  # optionally merge a live audit json to fill verdicts:
  python3 -m tools.eu_source_audit.gen_endpoint_repair_report \
    --audit-json reports/m21u4_europe_source_audit.json
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

from tools.eu_source_audit.venues import VENUES

# Snapshot of the endpoints that FAILED in the pre-repair audit (for the
# old->new diff). Recorded here so the report can show what changed without
# needing the old venues.py revision at runtime.
OLD_ENDPOINTS = {
    "smi": [
        "ishares.com/ch/.../291893/ishares-smi-ch-chf-acc/"
        "1495092304805.ajax (returned global multi-asset fund / unreachable)",
    ],
    "aex": [
        "ishares.com/nl/.../251779/ishares-aex-ucits-etf/"
        "1478358465952.ajax (unreachable in audit run)",
    ],
    "cac": [
        "amundietf.fr/.../amundi-cac-40-ucits-etf-dist/"
        "fr0007052782?download=holdings (unreachable in audit run)",
    ],
    "ibex": [
        "ishares.com/es/.../251773/ishares-ibex-35-ucits-etf/"
        "1478358465952.ajax (unreachable in audit run)",
    ],
}

_REPAIR_VENUES = ["smi", "aex", "cac", "ibex"]


def _git(*a):
    try:
        p = subprocess.run(["git", *a], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else "(unknown)"
    except Exception:  # noqa: BLE001
        return "(unknown)"


def _run_env():
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "GitHub Actions"
    if Path("/opt/algo-trader").exists():
        return "VPS"
    return "local"


def _inspected_fallbacks(record):
    """All fallback attempts that have an inspection, for one venue record."""
    return [a for a in record.get("attempts", [])
            if a.get("role", "").endswith("fallback") and a.get("inspection")]


def _score(attempt, expected):
    """Selection score for a fallback attempt.

    Higher is better. An exact-count, no-dup attempt always outranks any
    inexact one; among inexact, prefer the highest included row count.
    """
    ins = attempt["inspection"]
    n = len(ins.get("included", []))
    dups = ins.get("duplicate_tickers") or []
    exact = (n == expected and not dups)
    # exact attempts get a large bonus so they always win; tie-break by count
    return (1 if exact else 0, n if not dups else -1)


def _load_audit(path):
    """Return (selected, audited_venues) from an audit json, if provided.

    selected: {venue: best_fallback_record}
      1. Prefer a fallback whose inspected row count == venue expected AND has
         no duplicate tickers (exact).
      2. Else choose the best inspected fallback (highest included row count,
         dup-laden attempts deprioritised).
      3. If no fallback was inspected, the venue is NOT in `selected`.
    audited_venues: set of venues that have a record in the json at all
      (regardless of whether any fallback was inspected). This lets render()
      distinguish "audited but no usable fallback" (BLOCKED_NEEDS_MANUAL_SOURCE)
      from "not in the json at all" (NOT_AUDITED).
    """
    if not path or not Path(path).is_file():
        return {}, set()
    data = json.loads(Path(path).read_text())
    selected = {}
    audited = set()
    for r in data:
        v = r.get("venue")
        if v is None:
            continue
        audited.add(v)
        expected = VENUES[v]["expected"] if v in VENUES else None
        candidates = _inspected_fallbacks(r)
        if not candidates or expected is None:
            continue  # audited but no usable fallback
        best = max(candidates, key=lambda a: _score(a, expected))
        selected[v] = best
    return selected, audited


def render(audit_path=""):
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ")
    audit, audited_venues = _load_audit(audit_path)
    audit_supplied = bool(audit_path and Path(audit_path).is_file())
    L = []
    L.append("# M21.U4 Europe — Endpoint Repair Report")
    L.append("")
    L.append("Generated: %s" % now)
    L.append("")
    L.append("- run_environment: **%s**" % _run_env())
    L.append("- generated_at_git_branch: `%s`"
             % _git("rev-parse", "--abbrev-ref", "HEAD"))
    L.append("- generated_at_git_head: `%s`" % _git("rev-parse", "HEAD"))
    L.append("- generated_at_git_status: **%s**"
             % ("dirty" if _git("status", "--porcelain") else "clean"))
    L.append("")
    L.append("> `generated_at_git_*` are report-generation provenance, not the "
             "final committed-tree state.")
    L.append("")
    L.append("Read-only. No curation, no `global_expanded.json` / "
             "`source_registry.json` / runtime / scan_ready changes. Endpoint "
             "ids below are corrected candidates; **fetch/exact verdicts are "
             "only valid when produced by running the audit (Action or VPS) "
             "against the updated `venues.py`** — this generator does not probe "
             "the network.")
    L.append("")
    live = "with LIVE audit results merged" if audit_supplied else \
           "PLAN ONLY (no live audit json supplied — run the audit to fill " \
           "fetched/exact/verdict)"
    L.append("Mode: **%s**" % live)
    L.append("")
    L.append("## Per-venue endpoint repair")
    L.append("")
    L.append("| Venue | Expected | Old endpoint (failed) | New candidate "
             "source(s) | Selected endpoint | Included | Dups? | Fetched? | "
             "Exact? | Verdict |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for v in _REPAIR_VENUES:
        meta = VENUES[v]
        exp = meta["expected"]
        direct = [u for (role, u, note) in meta.get("endpoints", [])
                  if role == "reputable_etf_fallback"]
        pages = [u for (role, u, note) in meta.get("product_pages", [])
                 if role == "reputable_etf_fallback"]
        old = "; ".join(OLD_ENDPOINTS.get(v, ["(see venues.py history)"]))
        new_parts = []
        for u in pages:
            new_parts.append("PAGE (extract link): `%s`" % u)
        for u in direct:
            new_parts.append("DIRECT CSV: `%s`" % u)
        new = "<br>".join(new_parts) if new_parts else "(none)"
        if v in audit:
            rec = audit[v]
            ins = rec["inspection"]
            n = len(ins.get("included", []))
            dups = ins.get("duplicate_tickers") or []
            sel = rec.get("url") or rec.get("file") or "(selected fallback)"
            fetched = "yes"
            has_dups = "yes (%s)" % ",".join(dups) if dups else "no"
            is_exact = (n == exp and not dups)
            exact = "yes" if is_exact else "no (%d/%d)" % (n, exp)
            verdict = ("ACCEPT_FALLBACK_EXACT" if is_exact
                       else "FALLBACK_INCOMPLETE")
            sel_cell = "`%s`" % sel
            inc_cell = str(n)
        else:
            if not audit_supplied:
                # no audit json supplied at all
                sel_cell = "(run audit)"
                inc_cell = "(run audit)"
                has_dups = "(run audit)"
                fetched = "(run audit)"
                exact = "(run audit)"
                verdict = "PENDING_AUDIT"
            elif v in audited_venues:
                # audited (venue record present) but no usable inspected
                # fallback -> genuinely blocked
                sel_cell = "(none usable)"
                inc_cell = "0"
                has_dups = "n/a"
                fetched = "no"
                exact = "no"
                verdict = "BLOCKED_NEEDS_MANUAL_SOURCE"
            else:
                # audit json supplied but this venue was not in it: do NOT
                # silently call it blocked.
                sel_cell = "(not in audit json)"
                inc_cell = "n/a"
                has_dups = "n/a"
                fetched = "n/a"
                exact = "n/a"
                verdict = "NOT_AUDITED"
        L.append("| %s | %d | %s | %s | %s | %s | %s | %s | %s | **%s** |"
                 % (v.upper(), exp, old, new, sel_cell, inc_cell, has_dups,
                    fetched, exact, verdict))
    L.append("")
    L.append("> Verdict per venue: **ACCEPT_FALLBACK_EXACT** if the selected "
             "fallback's included count equals Expected with no duplicate "
             "tickers; **FALLBACK_INCOMPLETE** if a fallback was inspected but "
             "is not exact; **BLOCKED_NEEDS_MANUAL_SOURCE** if the venue WAS "
             "audited but no fallback yielded an inspectable file; "
             "**NOT_AUDITED** if the venue is absent from the supplied audit "
             "json (not silently treated as blocked); **PENDING_AUDIT** if no "
             "audit json was supplied. The selected endpoint is the best "
             "fallback per venue (exact preferred, else highest included "
             "count), not merely the last attempt.")
    L.append("")
    # coverage warning: every repair venue should be present in the audit json
    if audit_supplied:
        missing = [v.upper() for v in _REPAIR_VENUES
                   if v not in audited_venues]
        if missing:
            L.append("> ⚠️ **COVERAGE WARNING:** the supplied audit json does "
                     "not cover all repair venues. Missing: %s. These are "
                     "marked NOT_AUDITED (not BLOCKED). Re-run the audit with "
                     "`--venues smi,aex,cac,ibex` so every repair venue is "
                     "evaluated before any curation decision." %
                     ", ".join(missing))
        else:
            L.append("> ✅ Coverage: all repair venues (SMI, AEX, CAC, IBEX) "
                     "are present in the audit json.")
        L.append("")
    L.append("## How to fill verdicts (one command)")
    L.append("")
    L.append("Run the audit against the updated `venues.py`, then regenerate "
             "this report merging the live json:")
    L.append("")
    L.append("```")
    L.append("python3 -m tools.eu_source_audit.run_audit \\")
    L.append("  --venues smi,aex,cac,ibex \\")
    L.append("  --report reports/m21u4_europe_source_audit.md \\")
    L.append("  --json-out reports/m21u4_europe_source_audit.json")
    L.append("python3 -m tools.eu_source_audit.gen_endpoint_repair_report \\")
    L.append("  --audit-json reports/m21u4_europe_source_audit.json")
    L.append("```")
    L.append("")
    L.append("DAX is intentionally unchanged (its only reachable source is the "
             "iShares ETF at 38/40 = FALLBACK_INCOMPLETE; no official machine-"
             "fetchable 40-name source was found).")
    L.append("")
    L.append("## Possible outcomes per venue")
    L.append("")
    L.append("- **ACCEPT_FALLBACK_EXACT** — corrected endpoint returns exactly "
             "the expected count (SMI 20 / AEX 25 / CAC 40 / IBEX 35), no dup "
             "tickers. Eligible to curate as a labelled ETF-fallback batch "
             "(pending operator policy approval).")
    L.append("- **FALLBACK_INCOMPLETE** — endpoint returns a holdings file but "
             "the count != expected (ETF samples).")
    L.append("- **BLOCKED_NEEDS_MANUAL_SOURCE** — all corrected endpoints "
             "still unreachable/not-a-file; an official file must be supplied "
             "once.")
    L.append("- **NOT_AUDITED** — venue missing from the supplied audit json; "
             "re-run the audit covering it before deciding.")
    L.append("")
    return "\n".join(L)


def coverage_report(audit_path):
    """Return (ok, missing_venues) for the repair venues vs the audit json.

    ok is True only if an audit json was supplied AND every repair venue
    (SMI/AEX/CAC/IBEX) has a record in it. Used to warn/fail the production
    command so a partial audit is never mistaken for a complete one.
    """
    if not audit_path or not Path(audit_path).is_file():
        return False, list(_REPAIR_VENUES)  # nothing audited
    _, audited = _load_audit(audit_path)
    missing = [v for v in _REPAIR_VENUES if v not in audited]
    return (not missing), missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report",
                    default="reports/m21u4_europe_endpoint_repair.md")
    ap.add_argument("--audit-json", default="")
    ap.add_argument("--require-all-venues", action="store_true",
                    help="exit non-zero if the audit json does not cover all "
                         "repair venues (SMI/AEX/CAC/IBEX)")
    args = ap.parse_args()
    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(render(args.audit_json), encoding="utf-8")
    print("wrote %s" % rp)
    if args.audit_json:
        ok, missing = coverage_report(args.audit_json)
        if ok:
            print("coverage: all repair venues present")
        else:
            print("coverage WARNING: repair venues missing from audit json: %s"
                  % ", ".join(missing))
            if args.require_all_venues:
                print("FAIL: --require-all-venues set and coverage incomplete")
                sys.exit(3)


if __name__ == "__main__":
    main()
