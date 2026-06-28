# M21.U4 Europe — Source-Repair Closeout (read-only)

**Status: source repair did NOT succeed. No venue accepted. No curation performed.**

This document closes out the M21.U4 Europe *source-repair* effort. It records the
final VPS verification exactly, the final per-venue outcomes, and the explicit
confirmation that nothing in the live universe / runtime was changed. It is a
read-only closeout, not a successful source repair.

## Final VPS verification (run from a clean temp worktree on branch HEAD 3229e0e)

- import paths resolved from the temp worktree (not /opt/algo-trader) — the
  earlier ImportError was a verification-command path bug, since fixed.
- tests: **28 OK**
- live audit ran for SMI / AEX / CAC / IBEX
- coverage: all repair venues present in the audit json
- `FINAL_LINK_EXTRACTION_VERIFY_RC=0`
- original repo (`/opt/algo-trader`) status: clean (worktree wrote only under
  /tmp)

## Final per-venue outcomes

| Venue | Verdict | Detail |
|---|---|---|
| DAX | FALLBACK_INCOMPLETE | iShares Core DAX ETF holdings = 38 equity Xetra/EUR vs official 40 (AIRBUS on Boerse Berlin line, QIAGEN on Deutsche Boerse AG line). ETF samples the index. |
| SMI | FALLBACK_INCOMPLETE | Product page fetched (200); extractor downloaded a holdings CSV, but it was the WRONG fund: `SWDA_holdings` / iShares MSCI World (40 rows), not SMI (20). 40 != 20 -> rejected. |
| AEX | BLOCKED_NEEDS_MANUAL_SOURCE | Product pages: NL page reachable but NO_HOLDINGS_LINK in static HTML; UK page PAGE_UNREACHABLE. Direct CSV endpoints not usable. |
| CAC | BLOCKED_NEEDS_MANUAL_SOURCE | FR page PAGE_UNREACHABLE; UK page reachable but NO_HOLDINGS_LINK. Direct CSV endpoints not usable. |
| IBEX | BLOCKED_NEEDS_MANUAL_SOURCE | ES page reachable but NO_HOLDINGS_LINK; UK page PAGE_UNREACHABLE. Direct CSV endpoints not usable. |

No venue reached **ACCEPT_FALLBACK_EXACT**.

## Why this is a closeout, not a repair

- The holdings links the iShares product pages expose in static HTML are either
  absent (NO_HOLDINGS_LINK — link is injected by JavaScript, not present in the
  served HTML) or point at the wrong fund (SMI page returned an MSCI World
  holdings link). Several locale pages are not reachable at all from the VPS.
- The only consistently machine-reachable Europe source is the iShares Core DAX
  ETF, which structurally samples the index (38/40).
- Per the agreed decision rule: since no venue reached ACCEPT_FALLBACK_EXACT,
  Europe source work pauses for M21.

## SAFETY NOTE — do not curate from this run

The SMI result is a concrete demonstration that automated link extraction can
return the WRONG product's holdings: the `251882/ishares-smi-ch` product page
yielded a `SWDA_holdings` (iShares MSCI World) CSV. The strict exact-count gate
rejected it (40 != 20). **The link extractor must NOT be used as a curation-
acceptance mechanism on the basis of this run.** Any future Europe curation must
start from an explicitly verified official/manual source, not an
extractor-discovered link, and must re-confirm the fund identity.

## State confirmation (nothing changed in the live system)

- no venue accepted
- no Europe symbols curated
- no `configs/universe/global_expanded.json` change (still UK 100 + HK 93 = 193)
- no `configs/universe/source_registry.json` change
- no runtime / scanner / broker / live / paper change
- `scan_ready` unchanged (536)

## Resumption conditions (future, not now)

Europe (M21.U4) may resume only when one of these is available:
- an official index-owner / primary-exchange constituent file per venue (SIX for
  SMI, Euronext for AEX/CAC, BME for IBEX, STOXX/Deutsche Boerse for DAX),
  supplied once (these pages are dynamic / not server-fetchable), OR
- explicit operator approval to curate a clearly-labelled `reputable_etf_
  fallback` set with the known incompleteness (e.g. DAX 38), with fund identity
  re-verified, as inactive / unverified / non-scan-ready candidates.

Until then, the M21.U4 audit + repair infrastructure (already on main / this
branch) stands as the standing gate for re-checking sources later.
