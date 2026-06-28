# M21.UQ — Global Quality Collectors / Gates — Dry-Run Report

- report_type: **provider-backed dry-run**
- source_file: `configs/universe/global_expanded.json`
- scope: **existing global candidates only**
- network: **enabled**
- provider_mode: **yfinance**
- attempted: **5**

> Read-only quality dry-run over EXISTING global candidates. No writes to global_expanded.json / source_registry.json, no scan_ready change, no runtime activation. Default run is offline (structural checks: provider-symbol, suffix, duplicate, liquidity); OHLCV checks run only when a provider is injected.

## Summary

- total_candidates: **5**
- region_breakdown: UK=5
- passed (no fatal codes): **4**
- failed (>=1 fatal code): **1**
- overall: **quality_fail**

## Reason / warning code counts

| code | count |
|---|---|
| `liquidity_unknown` | 5 |
| `ohlcv_empty` | 1 |
| `volume_missing_or_zero` | 1 |

## Failing candidates (first 50)

| internal_symbol | provider_symbol | reason_codes |
|---|---|---|
| `LSE:AAF` | `AAF.L` | `ohlcv_empty`, `volume_missing_or_zero` |

## Warnings (non-fatal)

- `liquidity_unknown`: 5 candidates (non-fatal at this stage; inactive candidates have null liquidity by design)

## OHLCV breakdown (provider-backed runs)

- `ohlcv_empty`: 1 — `LSE:AAF`
- `volume_missing_or_zero`: 1 — `LSE:AAF`

## Safety confirmation

- read-only: no global_expanded.json / source_registry.json write
- no scan_ready change; no runtime activation; no scanner change
- this report evaluates existing candidates only; adds no symbols, no Europe/Japan/China/ADRs
