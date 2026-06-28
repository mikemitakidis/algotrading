# M21.U4 Europe — Endpoint Repair Report

Generated: 2026-06-28 16:18:58Z

- run_environment: **local**
- generated_at_git_branch: `m21-u4-europe-endpoint-repair`
- generated_at_git_head: `a5895880b378f5d88ec0850930cb4612bce05d9f`
- generated_at_git_status: **dirty**

> `generated_at_git_*` are report-generation provenance, not the final committed-tree state.

Read-only. No curation, no `global_expanded.json` / `source_registry.json` / runtime / scan_ready changes. Endpoint ids below are corrected candidates; **fetch/exact verdicts are only valid when produced by running the audit (Action or VPS) against the updated `venues.py`** — this generator does not probe the network.

Mode: **PLAN ONLY (no live audit json supplied — run the audit to fill fetched/exact/verdict)**

## Per-venue endpoint repair

| Venue | Expected | Old endpoint (failed) | New candidate source(s) | Selected endpoint | Included | Dups? | Fetched? | Exact? | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| SMI | 20 | ishares.com/ch/.../291893/ishares-smi-ch-chf-acc/1495092304805.ajax (returned global multi-asset fund / unreachable) | PAGE (extract link): `https://www.ishares.com/ch/individual/en/products/251882/ishares-smi-ch`<br>DIRECT CSV: `https://www.ubs.com/etf-tools/api/etf/holdings/csv?isin=CH0017142719`<br>DIRECT CSV: `https://www.ishares.com/ch/individual/en/products/270048/fund/1495092304805.ajax?fileType=csv&fileName=CSSMI_holdings&dataType=fund`<br>DIRECT CSV: `https://www.ishares.com/de/privatanleger/de/produkte/270048/fund/1478358465952.ajax?fileType=csv&fileName=CSSMI_holdings&dataType=fund` | (run audit) | (run audit) | (run audit) | (run audit) | (run audit) | **PENDING_AUDIT** |
| AEX | 25 | ishares.com/nl/.../251779/ishares-aex-ucits-etf/1478358465952.ajax (unreachable in audit run) | PAGE (extract link): `https://www.ishares.com/nl/particuliere-belegger/nl/producten/251779/ishares-aex-ucits-etf`<br>PAGE (extract link): `https://www.ishares.com/uk/individual/en/products/251779/ishares-aex-ucits-etf`<br>DIRECT CSV: `https://www.ishares.com/nl/particuliere-belegger/nl/producten/251779/fund/1478358465952.ajax?fileType=csv&fileName=IAEX_holdings&dataType=fund`<br>DIRECT CSV: `https://www.ishares.com/uk/individual/en/products/251779/fund/1478358465952.ajax?fileType=csv&fileName=IAEX_holdings&dataType=fund` | (run audit) | (run audit) | (run audit) | (run audit) | (run audit) | **PENDING_AUDIT** |
| CAC | 40 | amundietf.fr/.../amundi-cac-40-ucits-etf-dist/fr0007052782?download=holdings (unreachable in audit run) | PAGE (extract link): `https://www.ishares.com/fr/particuliers/fr/produits/251786/ishares-cac-40-ucits-etf`<br>PAGE (extract link): `https://www.ishares.com/uk/individual/en/products/251786/ishares-cac-40-ucits-etf`<br>DIRECT CSV: `https://www.ishares.com/fr/particuliers/fr/produits/251786/fund/1478358465952.ajax?fileType=csv&fileName=CAC_holdings&dataType=fund`<br>DIRECT CSV: `https://www.amundietf.fr/fr/professionnels/api/funds/holdings/FR0007052782/csv` | (run audit) | (run audit) | (run audit) | (run audit) | (run audit) | **PENDING_AUDIT** |
| IBEX | 35 | ishares.com/es/.../251773/ishares-ibex-35-ucits-etf/1478358465952.ajax (unreachable in audit run) | PAGE (extract link): `https://www.ishares.com/es/inversor-particular/es/productos/251773/ishares-ibex-35-ucits-etf`<br>PAGE (extract link): `https://www.ishares.com/uk/individual/en/products/251773/ishares-ibex-35-ucits-etf`<br>DIRECT CSV: `https://www.ishares.com/es/inversor-particular/es/productos/251773/fund/1478358465952.ajax?fileType=csv&fileName=IBEX_holdings&dataType=fund`<br>DIRECT CSV: `https://www.ishares.com/uk/individual/en/products/251773/fund/1478358465952.ajax?fileType=csv&fileName=IBEX_holdings&dataType=fund` | (run audit) | (run audit) | (run audit) | (run audit) | (run audit) | **PENDING_AUDIT** |

> Verdict per venue: **ACCEPT_FALLBACK_EXACT** if the selected fallback's included count equals Expected with no duplicate tickers; **FALLBACK_INCOMPLETE** if a fallback was inspected but is not exact; **BLOCKED_NEEDS_MANUAL_SOURCE** if the venue WAS audited but no fallback yielded an inspectable file; **NOT_AUDITED** if the venue is absent from the supplied audit json (not silently treated as blocked); **PENDING_AUDIT** if no audit json was supplied. The selected endpoint is the best fallback per venue (exact preferred, else highest included count), not merely the last attempt.

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
- **NOT_AUDITED** — venue missing from the supplied audit json; re-run the audit covering it before deciding.
