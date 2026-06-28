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


def _load_audit(path):
    """Return {venue: best_fallback_record} from an audit json, if provided."""
    if not path or not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text())
    out = {}
    for r in data:
        v = r.get("venue")
        best = None
        for a in r.get("attempts", []):
            if a.get("role", "").endswith("fallback") and a.get("inspection"):
                best = a
        if best:
            out[v] = best
    return out


def render(audit_path=""):
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ")
    audit = _load_audit(audit_path)
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
    live = "with LIVE audit results merged" if audit else \
           "PLAN ONLY (no live audit json supplied — run the audit to fill " \
           "fetched/exact/verdict)"
    L.append("Mode: **%s**" % live)
    L.append("")
    L.append("## Per-venue endpoint repair")
    L.append("")
    L.append("| Venue | Expected | Old endpoint (failed) | New endpoint(s) | "
             "source_role | Fetched? | Exact? | Verdict |")
    L.append("|---|---|---|---|---|---|---|---|")
    for v in _REPAIR_VENUES:
        meta = VENUES[v]
        exp = meta["expected"]
        new_fb = [u for (role, u, note) in meta["endpoints"]
                  if role == "reputable_etf_fallback"]
        old = "; ".join(OLD_ENDPOINTS.get(v, ["(see venues.py history)"]))
        new = "<br>".join("`%s`" % u for u in new_fb)
        if v in audit:
            rec = audit[v]
            n = len(rec["inspection"]["included"])
            fetched = "yes"
            exact = "yes" if n == exp else "no (%d/%d)" % (n, exp)
            verdict = ("ACCEPT_FALLBACK_EXACT" if n == exp
                       else "FALLBACK_INCOMPLETE")
        else:
            fetched = "(run audit)"
            exact = "(run audit)"
            verdict = "PENDING_AUDIT"
        L.append("| %s | %d | %s | %s | reputable_etf_fallback | %s | %s | "
                 "**%s** |" % (v.upper(), exp, old, new, fetched, exact,
                               verdict))
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
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report",
                    default="reports/m21u4_europe_endpoint_repair.md")
    ap.add_argument("--audit-json", default="")
    args = ap.parse_args()
    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(render(args.audit_json), encoding="utf-8")
    print("wrote %s" % rp)


if __name__ == "__main__":
    main()
