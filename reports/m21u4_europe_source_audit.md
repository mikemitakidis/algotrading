# M21.U4 Europe — Source Audit Report

**This file is a placeholder.** The audit must be run where outbound network
access to source hosts (iShares, index owners) is available — i.e. via the
GitHub Actions workflow `.github/workflows/m21u4_europe_source_audit.yml`
(Actions -> "M21.U4 Europe Source Audit" -> Run workflow) or on the VPS.

The sandbox used to author these tools has a restricted egress allow-list
(GitHub/PyPI only) and cannot reach the source hosts, so it would report every
endpoint as `UNREACHABLE`, which is not the true result.

## How to produce the real report

Option A - GitHub Actions (recommended, no terminal):
1. Open the repo on GitHub -> **Actions** tab.
2. Select **M21.U4 Europe Source Audit** -> **Run workflow** (optionally set the
   venue list) -> Run.
3. When it finishes, download the **m21u4-europe-source-audit** artifact, or
   read the report printed in the job log. The artifact contains both
   `m21u4_europe_source_audit.md` and `.json`.

Option B - VPS (single command):
```
cd /opt/algo-trader && PYTHONPATH=/opt/algo-trader /opt/algo-trader/venv/bin/python3 \
  -m tools.eu_source_audit.run_audit \
  --venues dax,smi,aex,cac,ibex \
  --outdir /tmp/m21u4_sources \
  --report reports/m21u4_europe_source_audit.md \
  --json-out reports/m21u4_europe_source_audit.json
```
(The VPS run writes the report into the working tree; commit it to this branch
to review in GitHub/VS Code. It performs no curation and does not touch
`global_expanded.json` or `source_registry.json`.)

## What the report will contain (per venue: DAX, SMI, AEX, CAC, IBEX)

- each source attempted, with `source_role` (official_index / official_exchange
  / reputable_etf_fallback) and URL
- HTTP status, whether a file was saved, sha256, byte size
- as-of date, detected constituent rows, included tickers
- excluded-rows summary (cash / derivatives / FX / wrong-exchange / wrong-ccy)
- exact expected-count pass/fail, and a per-source recommendation
- a venue verdict: **ACCEPT** / **REVIEW_NEEDED** / **BLOCKED**

## Known expectations (from prior manual probes)

- **DAX**: the only machine-downloadable source is the iShares Core DAX ETF
  holdings (`reputable_etf_fallback`), which yields 38 equity Xetra/EUR lines,
  not the official 40 -> expected **REVIEW_NEEDED / BLOCKED** for an official
  spine.
- **SMI**: the previously-tried iShares endpoint returned a global multi-asset
  fund (429 rows), i.e. a wrong product id. `venues.py` now uses a corrected
  SMI product id; the Action will confirm whether it yields the clean 20.
- **AEX / CAC / IBEX**: ETF-holdings endpoints listed; the Action will report
  whether any yields the exact official count. Official index-owner pages are
  dynamic and expected to come back `NOT_A_FILE`.

No curation, no `global_expanded.json` / `source_registry.json` changes, and no
runtime activation happen at this stage. Curation proceeds only for venues an
accepted source is found for, in their own later sub-milestones.
