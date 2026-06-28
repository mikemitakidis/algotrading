# M21.U4 Europe — Source Repair Report

Generated: 2026-06-28 15:36:17Z

- run_environment: **local**
- generated_at_git_branch: `m21-u4-europe-endpoint-repair`
- generated_at_git_head: `f5090678e1a4a62390aa6b7011488515c167dc56`
- generated_at_git_status: **dirty**

> The `generated_at_git_*` fields above are the git state when this report was generated (typically before the commit that includes it), NOT the final commit state of this file. They are run provenance, not a claim about the committed tree.

Read-only analysis. No curation; no `global_expanded.json` / `source_registry.json` / runtime / scan_ready changes. The findings derive from the merged audit tool's VPS run plus documented endpoint behaviour; no new live probing was done to author this report. Endpoint re-verification must go through the audit Action/VPS.

## Classification legend

- **ACCEPT_OFFICIAL** — official index/exchange source, exact count, machine-usable.
- **ACCEPT_FALLBACK_EXACT** — reputable ETF holdings, exact official count, membership unverified.
- **FALLBACK_INCOMPLETE** — reputable ETF reachable but sample ≠ official count.
- **BLOCKED_NEEDS_MANUAL_SOURCE** — no machine-usable source; an official file must be supplied once.

## Summary

| Venue | Index | Suffix | Expected | Audit | Fallback | Classification |
|---|---|---|---|---|---|---|
| DAX | DAX | .DE | 40 | REVIEW_NEEDED | INCOMPLETE (38/40) | **FALLBACK_INCOMPLETE** |
| SMI | SMI | .SW | 20 | BLOCKED | UNKNOWN (no holdings file obtained yet; corrected endpoint unreachable) | **BLOCKED_NEEDS_MANUAL_SOURCE** |
| AEX | AEX | .AS | 25 | BLOCKED | UNKNOWN (no holdings file obtained) | **BLOCKED_NEEDS_MANUAL_SOURCE** |
| CAC | CAC | .PA | 40 | BLOCKED | UNKNOWN (no holdings file obtained) | **BLOCKED_NEEDS_MANUAL_SOURCE** |
| IBEX | IBEX | .MC | 35 | BLOCKED | UNKNOWN (no holdings file obtained) | **BLOCKED_NEEDS_MANUAL_SOURCE** |

## DAX (DAX, XETRA, expected 40)

1. **Current audit result:** REVIEW_NEEDED
2. **Current source & why it failed:** iShares Core DAX UCITS ETF (DE) holdings CSV (reputable_etf_fallback) — ETF holdings sample the index: 38 equity Xetra/EUR lines vs official 40. AIRBUS excluded (held on Boerse Berlin line), QIAGEN excluded (Deutsche Boerse AG line); plus cash/FX/futures rows. So the ETF is not a faithful 40-name membership snapshot.
3. **Better sources considered:**
   - `official_index` — STOXX / dax-indices.com DAX composition export: Authoritative 40-name membership. Page is dynamic/JS; no stable machine-downloadable CSV/XLSX endpoint observed. Likely needs a one-time manual export or login.
   - `official_exchange` — Deutsche Boerse / Xetra DAX factsheet (PDF): Authoritative; PDF is downloadable but layout-variable and was not auto-parsed by the audit (PDFs flagged MANUAL_REVIEW).
   - `reputable_etf_fallback` — iShares Core DAX ETF holdings CSV: Machine-reachable from VPS (HTTP 200), but 38/40 -> incomplete.
4. **Exact count obtainable?** Only from an official index/exchange file (STOXX composition or Xetra factsheet). No reachable ETF gives exactly 40.
5. **Fallback exact or incomplete?** INCOMPLETE (38/40)
6. **Classification:** **FALLBACK_INCOMPLETE**
   - manual file that would unblock: Official DAX 40 composition (STOXX/dax-indices export CSV/XLSX) or the Xetra DAX factsheet PDF, uploaded once to /tmp/m21u4_sources/.

## SMI (SMI, SIX, expected 20)

1. **Current audit result:** BLOCKED
2. **Current source & why it failed:** iShares SMI ETF holdings CSV (reputable_etf_fallback) — First endpoint id returned a global multi-asset fund (429 rows, only 1 SIX/CHF equity) -> wrong product. Corrected SMI product-id endpoint was UNREACHABLE from the VPS audit run (404/▒). So no SMI holdings file was obtained.
3. **Better sources considered:**
   - `official_index` — SIX Swiss Exchange SMI index page / factsheet: Authoritative 20-name membership; page dynamic, no stable CSV endpoint observed.
   - `reputable_etf_fallback` — iShares SMI (CH) / UBS ETF SMI / Amundi SMI holdings CSV: SMI is fully replicated (20 names), so a CORRECT ETF endpoint should yield exactly 20. The challenge is the correct, reachable holdings URL; the ids tried so far 404'd. Worth one more endpoint-id correction via the audit tool.
4. **Exact count obtainable?** Plausibly YES via a correct iShares/UBS SMI holdings CSV (full replication -> 20), once the right reachable endpoint id is found; otherwise via SIX official file.
5. **Fallback exact or incomplete?** UNKNOWN (no holdings file obtained yet; corrected endpoint unreachable)
6. **Classification:** **BLOCKED_NEEDS_MANUAL_SOURCE**
   - manual file that would unblock: Either a corrected reachable iShares/UBS SMI holdings CSV (preferred, likely exact 20), or the SIX SMI official constituents file, uploaded once.

## AEX (AEX, AEX, expected 25)

1. **Current audit result:** BLOCKED
2. **Current source & why it failed:** iShares AEX UCITS ETF holdings CSV (reputable_etf_fallback) — Endpoint UNREACHABLE in the audit run (no holdings file obtained).
3. **Better sources considered:**
   - `official_index` — Euronext AEX composition (live.euronext.com): Authoritative 25-name membership; Euronext pages are dynamic and key on ISIN, no stable CSV endpoint observed.
   - `reputable_etf_fallback` — iShares AEX UCITS ETF holdings CSV: AEX (25 names) is small/replicated; a correct reachable iShares AEX endpoint should yield ~25. Endpoint id needs correction.
4. **Exact count obtainable?** Plausibly YES via a correct iShares AEX holdings CSV; otherwise via Euronext official file.
5. **Fallback exact or incomplete?** UNKNOWN (no holdings file obtained)
6. **Classification:** **BLOCKED_NEEDS_MANUAL_SOURCE**
   - manual file that would unblock: Corrected reachable iShares AEX holdings CSV, or the Euronext AEX official composition export.

## CAC (CAC, EPA, expected 40)

1. **Current audit result:** BLOCKED
2. **Current source & why it failed:** Amundi CAC 40 UCITS ETF holdings (reputable_etf_fallback) — Endpoint UNREACHABLE in the audit run (no holdings file obtained).
3. **Better sources considered:**
   - `official_index` — Euronext CAC 40 composition (live.euronext.com): Authoritative 40-name membership; dynamic page, ISIN-keyed, no stable CSV endpoint observed. Watch multi-class tickers.
   - `reputable_etf_fallback` — Amundi / Lyxor / iShares CAC 40 ETF holdings CSV: CAC 40 is replicated; a correct reachable ETF endpoint should yield ~40. Endpoint id/host needs correction (Amundi URL 404'd pattern in prior DAX probe).
4. **Exact count obtainable?** Plausibly YES via a correct CAC 40 ETF holdings CSV; otherwise via Euronext official file.
5. **Fallback exact or incomplete?** UNKNOWN (no holdings file obtained)
6. **Classification:** **BLOCKED_NEEDS_MANUAL_SOURCE**
   - manual file that would unblock: Corrected reachable CAC 40 ETF holdings CSV, or the Euronext CAC 40 official composition export.

## IBEX (IBEX, BME, expected 35)

1. **Current audit result:** BLOCKED
2. **Current source & why it failed:** iShares IBEX 35 UCITS ETF holdings CSV (reputable_etf_fallback) — Endpoint UNREACHABLE in the audit run (no holdings file obtained).
3. **Better sources considered:**
   - `official_index` — BME / Bolsa de Madrid IBEX 35 composition: Authoritative 35-name membership; dynamic page, no stable CSV endpoint observed.
   - `reputable_etf_fallback` — iShares IBEX 35 UCITS ETF holdings CSV: IBEX 35 is replicated; a correct reachable iShares IBEX endpoint should yield ~35. Endpoint id needs correction.
4. **Exact count obtainable?** Plausibly YES via a correct iShares IBEX holdings CSV; otherwise via BME official file.
5. **Fallback exact or incomplete?** UNKNOWN (no holdings file obtained)
6. **Classification:** **BLOCKED_NEEDS_MANUAL_SOURCE**
   - manual file that would unblock: Corrected reachable iShares IBEX 35 holdings CSV, or the BME IBEX 35 official composition.

## Decision required

- ACCEPT_OFFICIAL: none
- ACCEPT_FALLBACK_EXACT: none
- FALLBACK_INCOMPLETE: DAX
- BLOCKED_NEEDS_MANUAL_SOURCE: SMI, AEX, CAC, IBEX

No venue currently qualifies as ACCEPT_OFFICIAL or ACCEPT_FALLBACK_EXACT from a machine-reachable source. Three ways forward (not mutually exclusive):

**A) Endpoint-id repair (engineering, GitHub-first).** SMI/AEX/CAC/IBEX failed on wrong/unreachable ETF endpoint *ids*, not on a proven absence of an exact source. For fully-replicated indices (SMI 20, AEX 25, IBEX 35, CAC 40) a correct iShares/UBS/Amundi holdings CSV would likely yield the exact count. Next step: correct the endpoint ids in `venues.py`, re-run the audit Action, and any venue that returns the exact count becomes ACCEPT_FALLBACK_EXACT. This needs no manual files.

**B) Operator approves labelled ETF fallback.** If a `reputable_etf_fallback` set is acceptable as the spine for inactive/unverified candidates (the HK TraHK→HSIL posture), then ACCEPT_FALLBACK_EXACT venues curate immediately, and DAX could proceed as a labelled 38-name subset (explicitly NOT the official 40) if you accept incompleteness. Membership is reconciled later at a quality gate.

**C) Supply official manual files.** For guaranteed official membership, provide one file per venue (see each venue's "manual file that would unblock"). These are dynamic / not server-fetchable, so a one-time download+upload is the only authoritative route. DAX is the most likely to *need* this (its ETF is structurally 38/40).

Recommended sequence: **A first** (cheap, automated, may resolve SMI/AEX/CAC/IBEX outright) → then **B or C** for whatever remains (notably DAX). Europe stays active throughout; no venue is abandoned.
