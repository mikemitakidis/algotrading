# M21.UQ — Global Quality Collectors / Gates — Dry-Run Report

Generated: 2026-06-28 19:24:49Z

- run_environment: **local**
- generated_at_git_branch: `m21-uq-quality-collectors-gates`
- generated_at_git_head: `8523a6710474bd268937707a2a638f4fc95ded70`
- generated_at_git_status: **dirty**

> Read-only quality dry-run over EXISTING global candidates. No writes to global_expanded.json / source_registry.json, no scan_ready change, no runtime activation. Default run is offline (structural checks: provider-symbol, suffix, duplicate, liquidity); OHLCV checks run only when a provider is injected.

## Summary

- total_candidates: **193**
- region_breakdown: HK=93, UK=100
- passed (no fatal codes): **193**
- failed (>=1 fatal code): **0**
- overall: **quality_pass**

## Reason / warning code counts

| code | count |
|---|---|
| `liquidity_unknown` | 193 |

## Failing candidates (first 50)

(none)

## Warnings (non-fatal)

- `liquidity_unknown`: 193 candidates (non-fatal at this stage; inactive candidates have null liquidity by design)

## Safety confirmation

- read-only: no global_expanded.json / source_registry.json write
- no scan_ready change; no runtime activation; no scanner change
- this report evaluates existing candidates only; adds no symbols, no Europe/Japan/China/ADRs
