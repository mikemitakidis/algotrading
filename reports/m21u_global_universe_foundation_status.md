# M21.U — Global Universe Foundation — Closeout / Status

**Read-only status reconciliation.** This report closes the *completed
source-backed foundation checkpoint* of M21.U. It does NOT claim Europe
succeeded and does NOT claim full M21 is complete. No symbols added, no runtime
activation, no scan_ready change.

- main HEAD: `18349c3ed5e5158b4621c08b82914f172af80e98`
- global_symbols: **193** (UK 100 + HK 93)
- uk_count: **100**
- hk_count: **93**
- eu_count: **0**
- scan_ready: **536** (unchanged)
- global_in_default_paths: **False**

## Naming (unchanged — M21 labels preserved exactly)

This roadmap uses the existing M21 labels. They are NOT renamed to numeric
labels. The "clean numeric" convention applies only from M22 onward
(M22.1, M22.2, ...; M23.1, M23.2, ...).

- M21.U — Global Universe Foundation
- M21.U0 — Source ingest / raw vault
- M21.U0.H — ingest hardening
- M21.U1 — normaliser framework
- M21.U2 — UK FTSE 100 batch
- M21.U3.HK — Hong Kong HSI / TraHK batch
- M21.U4 — Europe supported-venue source audit / repair
- M21.UQ — Global quality collectors / gates
- M21.UR — Regional universe activation
- Runtime Registry Activation — separate config task
- M21 — Score optimisation / self-learning (M21.1 .. M21.7 as already written)

## Sub-milestone status

| Sub-milestone | Status | Notes |
|---|---|---|
| M21.U0 | DONE | source_ingest raw vault tool |
| M21.U0.H | DONE | ingest input hardening |
| M21.U1 | DONE | global_expansion normaliser framework |
| M21.U2 | DONE | UK FTSE 100 — 100 inactive candidates |
| M21.U3.HK | DONE | Hong Kong HSI / TraHK — 93 inactive candidates; official HSIL exact-membership cross-check passed (93/93) |
| M21.U4 | PAUSED — SOURCE-BLOCKED | Europe audit + repair built; no venue reached ACCEPT_FALLBACK_EXACT; 0 symbols added |
| M21.UQ | NEXT | Global quality collectors / gates — not started |
| M21.UR | LATER | Regional universe activation — not started |
| Runtime Registry Activation | LATER | separate config task — not started |
| M21 score optimisation | LATER | M21.1 .. M21.7 — not started |

## Accepted source-backed foundation (final)

- **UK**: 100 inactive candidates (FTSE 100), source UK__FTSE100__2026-06-27__002.
- **HK**: 93 inactive candidates (HSI / TraHK), source HK__HSI__2026-06-26__002;
  official HSIL May-2026 review cross-check = 93/93 exact match.
- **Total**: 193 global candidates, all active=false, scan_ready=false,
  data_quality_status=unverified, liquidity null, no execution/paper keys.
- Scanner still sees exactly the 536 US scan-ready symbols; global_expanded.json
  is not in the active-selection default paths.

## Europe (M21.U4) — paused / source-blocked

Europe was audited and a source-repair effort (direct endpoints, then product-
page link extraction) was attempted. Final per-venue outcome:

- DAX: FALLBACK_INCOMPLETE (iShares ETF 38/40 — samples the index)
- SMI: FALLBACK_INCOMPLETE (extractor returned a WRONG fund — iShares MSCI
  World SWDA_holdings, 40 rows, not SMI 20; rejected by the exact-count gate)
- AEX / CAC / IBEX: BLOCKED_NEEDS_MANUAL_SOURCE (holdings link absent from
  static HTML / pages unreachable)

No venue reached ACCEPT_FALLBACK_EXACT. Per the agreed decision rule, Europe
source work is paused for M21. See reports/m21u4_europe_closeout.md for the full
closeout and the safety note that the link extractor must not be used for
curation acceptance from that run. **Europe count = 0. Europe did NOT succeed.**

## Deferred (not started, not abandoned)

- **Japan** (Nikkei 225) — DEFERRED. Same upload-a-file source route as HK.
- **China / ADRs / other global extras** — DEFERRED.

These are tracked for later regional batches; none are in progress.

## What this closeout closes (and what it does NOT)

CLOSES:
- the completed source-backed foundation checkpoint: M21.U0, M21.U0.H, M21.U1,
  M21.U2, M21.U3.HK are DONE.
- M21.U4 Europe is recorded as PAUSED / source-blocked (not a success).

DOES NOT CLOSE / NOT CLAIMED:
- M21.UQ (global quality collectors / gates) — remains **NEXT**.
- M21.UR (regional universe activation) — remains later.
- Runtime Registry Activation — remains later.
- M21 score optimisation (M21.1 .. M21.7) — remains later.
- Full M21 is NOT complete.

## State confirmation (nothing changed in the live system)

- no symbols added (global_symbols still 193)
- no configs/universe/global_expanded.json change
- no configs/universe/source_registry.json change
- no runtime activation
- no scanner activation
- no broker / live / paper routing change
- scan_ready unchanged (536)

## Next real build milestone

**M21.UQ — Global Quality Collectors / Gates.** Not started in this closeout.
This document is read-only status reconciliation only.
