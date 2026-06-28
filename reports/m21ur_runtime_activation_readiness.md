# M21.UR — Runtime Registry Activation Readiness (read-only)

**Read-only readiness assessment. NO activation is performed in this branch.**
This document combines the pre-activation audit, the UK/HK eligibility
assessment, and a recommendation for the smallest safe future activation path.
It changes no runtime, no scan_ready, no default paths, no configs, no symbols.

- main HEAD at assessment: `2e826869e41b110dd922b4d391ec5456b5c2bd62`
- milestone: **M21.UR — Runtime Registry Activation Readiness**

## 1. Current runtime / default-path flow (grounded in code)

- `bot/universe/active_selection.py::get_scan_ready_symbols(paths=None)` loads
  `UniverseRegistry.load(_DEFAULT_PATHS)` and returns the de-duplicated, sorted
  bare tickers of every `scan_ready=true` record.
- `_DEFAULT_PATHS` = exactly two files:
  `configs/universe/us_seed.json` and `configs/universe/us_expanded.json`.
- `configs/universe/global_expanded.json` is **excluded** from `_DEFAULT_PATHS`,
  so global candidates are never loaded by the default runtime path.
- `_bare_ticker(record)` prefers `provider_symbols['yfinance']`. Consequence:
  if a global candidate were ever activated, it would surface in runtime format
  as its yfinance symbol — e.g. UK `HSBA.L`, HK `0001.HK` — i.e. WITH the
  exchange suffix. The current US scan-ready set is suffix-free.
- Current `scan_ready` count: **536** (US only).
- `global_in_default_paths`: **False**.

## 2. Current global candidate state

- `global_symbols`: **193**
- UK: **100**
- HK: **93**
- EU: **0**
- Existing candidates only. Every global record is `active=false`,
  `scan_ready=false`, `data_quality_status=unverified`, liquidity fields null.
- No runtime activation; nothing in `bot/` or `main.py` imports the global file
  for scanning.

## 3. Activation options (analysis only — NOT implemented)

### Option A — include `global_expanded.json` in `_DEFAULT_PATHS`
- Risks: immediately exposes ALL records that are `scan_ready=true` in that
  file to the scanner; suffix-format mismatch (`0001.HK`) may break runtime/
  broker assumptions; HK quality is currently unprovable (rate-limited). Coarse
  (all-or-nothing per file).
- Files likely touched: `bot/universe/active_selection.py` (the `_DEFAULT_PATHS`
  list).
- Rollback: revert the one-line list change; scan_ready returns to 536.

### Option B — staged regional registry
- A separate, explicitly-loaded registry path (e.g. activate per region by
  passing `paths=` rather than editing the default), so US default is untouched
  and a region is opt-in.
- Risks: more plumbing; must define who passes the regional path and when.
- Files likely touched: a new config path + the caller that selects it (NOT the
  default list).
- Rollback: stop passing the regional path.

### Option C — UK-only pilot
- Activate a small UK subset only (set `scan_ready=true` on a few verified UK
  records, or load a UK-only file), US untouched, HK excluded.
- Risks: lowest blast radius; still must confirm suffix/runtime compatibility
  for `.L` symbols and broker routing for LSE.
- Files likely touched: a UK-only config or targeted record edits + possibly a
  loader path; NOT the US default.
- Rollback: unset the pilot records / stop loading the UK file.

### Option D — delay activation
- Keep everything inactive until provider reliability (HK rate-limit) and the
  liquidity policy are resolved.
- Risks: none to the running system; slower universe expansion.
- Files likely touched: none.
- Rollback: n/a.

## 4. Eligibility / readiness assessment

- **UK is the better potential first pilot.** The provider-backed yfinance
  sample passed **5/5** for UK in the M21.UQ live smoke. UK `.L` symbols are the
  most likely to pass live quality gates today.
- **HK is blocked / deferred.** The HK live sample failed 5/5 due to
  `YFRateLimitError` (provider throttle), which M21.UQ correctly classifies as
  `provider_rate_limited` — a provider-availability problem, NOT a
  symbol-quality failure. HK readiness cannot be established until the provider
  reliability / rate-limit issue is solved.
- **No activation yet.** This assessment does not activate, schedule, or
  scan-ready any UK or HK symbol. No claim is made that any UK/HK symbol is
  active or scan_ready. UK-only pilot is a FUTURE OPTION, not a current state.

## 5. Minimum gates before ANY activation

A candidate must clear all of these before it could be considered for
activation (none are applied in this branch):

1. provider symbol present (`provider_symbols.yfinance`)
2. suffix valid for its exchange (canonical EXCHANGES map)
3. duplicate-free provider symbol across the universe
4. provider-backed OHLCV passes where the provider is available (enough bars,
   not stale, finite, non-zero volume)
5. provider errors separated from data failures (`provider_rate_limited` /
   `provider_fetch_error` are NOT treated as data quality passes or failures)
6. liquidity policy decided (today liquidity is null → `liquidity_unknown`
   warning; a real activation needs a liquidity threshold decision)
7. exchange / currency / market-hours compatibility checked for the runtime
   (timezone, trading calendar, suffix format)
8. runtime / scanner / risk / broker assumptions checked against suffixed,
   non-USD, non-US-hours symbols
9. broker / live execution remains **explicitly blocked** unless separately
   approved — readiness here never implies execution approval

## 6. Future activation patch (smallest safe path)

- **Recommended smallest safe path: a UK-only pilot behind an explicit,
  opt-in load path (a blend of Option C + Option B), NOT editing the US
  `_DEFAULT_PATHS`.** This keeps the 536 US set byte-for-byte unchanged and
  makes UK activation reversible by not-loading.
- Files a FUTURE activation branch would likely change (for reference only —
  none changed here):
  - `bot/universe/active_selection.py` (only if the chosen path edits
    `_DEFAULT_PATHS`; the recommended path AVOIDS this by passing `paths=`)
  - a new/targeted UK pilot config under `configs/universe/` OR targeted
    `scan_ready=true` edits on verified UK records in `global_expanded.json`
  - the caller that selects the pilot path
  - tests asserting the new scan_ready count and runtime-format compatibility
- Rollback plan: stop loading the UK pilot path (or revert the `scan_ready`
  edits); `get_scan_ready_symbols()` returns to 536; `global_in_default_paths`
  returns to False. Because the recommended path does not touch the US default,
  rollback cannot affect US scanning.

## 7. Explicit non-activation statement

- no scan_ready change (still 536)
- no default-path change (`_DEFAULT_PATHS` unchanged; global still excluded)
- no runtime activation
- no config mutation (`global_expanded.json` / `source_registry.json`
  untouched)
- no symbols added (still 193)
- no live reports committed
- **M21.UR activation is NOT performed in this branch.**
- **Explicit operator approval is REQUIRED before any activation**, and broker /
  live execution requires its own separate approval beyond activation.
