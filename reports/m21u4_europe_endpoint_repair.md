# M21.U4 Europe — Endpoint Repair Report

Generated: 2026-06-28 15:36:09Z

- run_environment: **local**
- generated_at_git_branch: `m21-u4-europe-endpoint-repair`
- generated_at_git_head: `f5090678e1a4a62390aa6b7011488515c167dc56`
- generated_at_git_status: **dirty**

> `generated_at_git_*` are report-generation provenance, not the final committed-tree state.

Read-only. No curation, no `global_expanded.json` / `source_registry.json` / runtime / scan_ready changes. Endpoint ids below are corrected candidates; **fetch/exact verdicts are only valid when produced by running the audit (Action or VPS) against the updated `venues.py`** — this generator does not probe the network.

Mode: **PLAN ONLY (no live audit json supplied — run the audit to fill fetched/exact/verdict)**

## Per-venue endpoint repair

| Venue | Expected | Old endpoint (failed) | New endpoint(s) | source_role | Fetched? | Exact? | Verdict |
|---|---|---|---|---|---|---|---|
| SMI | 20 | ishares.com/ch/.../291893/ishares-smi-ch-chf-acc/1495092304805.ajax (returned global multi-asset fund / unreachable) | `https://www.ubs.com/etf-tools/api/etf/holdings/csv?isin=CH0017142719`<br>`https://www.ishares.com/ch/individual/en/products/270048/fund/1495092304805.ajax?fileType=csv&fileName=CSSMI_holdings&dataType=fund`<br>`https://www.ishares.com/de/privatanleger/de/produkte/270048/fund/1478358465952.ajax?fileType=csv&fileName=CSSMI_holdings&dataType=fund` | reputable_etf_fallback | (run audit) | (run audit) | **PENDING_AUDIT** |
| AEX | 25 | ishares.com/nl/.../251779/ishares-aex-ucits-etf/1478358465952.ajax (unreachable in audit run) | `https://www.ishares.com/nl/particuliere-belegger/nl/producten/251779/fund/1478358465952.ajax?fileType=csv&fileName=IAEX_holdings&dataType=fund`<br>`https://www.ishares.com/uk/individual/en/products/251779/fund/1478358465952.ajax?fileType=csv&fileName=IAEX_holdings&dataType=fund` | reputable_etf_fallback | (run audit) | (run audit) | **PENDING_AUDIT** |
| CAC | 40 | amundietf.fr/.../amundi-cac-40-ucits-etf-dist/fr0007052782?download=holdings (unreachable in audit run) | `https://www.ishares.com/fr/particuliers/fr/produits/251786/fund/1478358465952.ajax?fileType=csv&fileName=CAC_holdings&dataType=fund`<br>`https://www.amundietf.fr/fr/professionnels/api/funds/holdings/FR0007052782/csv` | reputable_etf_fallback | (run audit) | (run audit) | **PENDING_AUDIT** |
| IBEX | 35 | ishares.com/es/.../251773/ishares-ibex-35-ucits-etf/1478358465952.ajax (unreachable in audit run) | `https://www.ishares.com/es/inversor-particular/es/productos/251773/fund/1478358465952.ajax?fileType=csv&fileName=IBEX_holdings&dataType=fund`<br>`https://www.ishares.com/uk/individual/en/products/251773/fund/1478358465952.ajax?fileType=csv&fileName=IBEX_holdings&dataType=fund` | reputable_etf_fallback | (run audit) | (run audit) | **PENDING_AUDIT** |

## How to fill verdicts (one command)

Run the audit against the updated `venues.py`, then regenerate this report merging the live json:

```
python3 -m tools.eu_source_audit.run_audit \
  --venues smi,aex,cac,ibex \
  --report reports/m21u4_europe_source_audit.md \
  --json-out reports/m21u4_europe_source_audit.json
python3 -m tools.eu_source_audit.gen_endpoint_repair_report \
  --audit-json reports/m21u4_europe_source_audit.json
```

DAX is intentionally unchanged (its only reachable source is the iShares ETF at 38/40 = FALLBACK_INCOMPLETE; no official machine-fetchable 40-name source was found).

## Possible outcomes per venue

- **ACCEPT_FALLBACK_EXACT** — corrected endpoint returns exactly the expected count (SMI 20 / AEX 25 / CAC 40 / IBEX 35), no dup tickers. Eligible to curate as a labelled ETF-fallback batch (pending operator policy approval).
- **FALLBACK_INCOMPLETE** — endpoint returns a holdings file but the count != expected (ETF samples).
- **BLOCKED_NEEDS_MANUAL_SOURCE** — all corrected endpoints still unreachable/not-a-file; an official file must be supplied once.
