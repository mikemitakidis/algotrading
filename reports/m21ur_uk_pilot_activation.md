# M21.UR — UK Pilot Activation Path (controlled, config-based)

**M21.UR UK pilot activation path added.** This implements controlled,
config-based regional activation for a small UK-only pilot. It is **explicit
opt-in only** and does NOT wire anything into the default scanner/runtime.

- main HEAD at branch: `8ecf54b374ec926c9ed068b6927e721586a37431`
- milestone: **M21.UR — Runtime Registry Activation (UK pilot path)**

## What was added

- `configs/universe/uk_pilot.json` — a separate registry file with exactly 5 UK
  pilot records, each `scan_ready=true` **in this file only**.
- `bot/universe/uk_pilot.py` — an explicit opt-in accessor
  `get_uk_pilot_symbols()` that loads ONLY the pilot file via the existing
  `get_scan_ready_symbols(paths=[uk_pilot.json])`. It is NOT imported by any
  runtime entrypoint.
- `test_m21ur_uk_pilot.py` — isolation/validity/suffix/no-HK/default-unchanged
  tests.

## The 5 UK pilot symbols

| internal_symbol | yfinance | name | region | exchange | currency | calendar |
|---|---|---|---|---|---|---|
| LSE:AAF | AAF.L | Airtel Africa plc | UK | LSE | GBP | XLON |
| LSE:AAL | AAL.L | Anglo American Plc | UK | LSE | GBP | XLON |
| LSE:ABDN | ABDN.L | Aberdeen Group Plc | UK | LSE | GBP | XLON |
| LSE:ABF | ABF.L | Associated British Foods plc | UK | LSE | GBP | XLON |
| LSE:ADM | ADM.L | Admiral Group | UK | LSE | GBP | XLON |

The explicit UK pilot accessor returns exactly: `AAF.L`, `AAL.L`, `ABDN.L`,
`ABF.L`, `ADM.L` (sorted, suffixed).

## This satisfies the controlled config-based regional activation requirement

- Activation is achieved purely through a **separate config file** plus an
  **explicit accessor**, not by changing default runtime behaviour.
- `scan_ready=true` promotion exists ONLY in `uk_pilot.json`.
- The pilot is loaded only when a caller explicitly calls
  `get_uk_pilot_symbols()`.

## Default US runtime remains unchanged

- `get_scan_ready_symbols()` (no args) still returns **536** US symbols.
- `_DEFAULT_PATHS` is unchanged (`us_seed.json` + `us_expanded.json`); it
  references neither `global_expanded.json` nor `uk_pilot.json`.
- `global_in_default_paths=False`.
- The pilot set is disjoint from the US default set (proven by test).

## Global universe untouched

- `configs/universe/global_expanded.json` remains **193** records, all
  `active=false` / `scan_ready=false`. It was NOT edited.
- `configs/universe/source_registry.json` was NOT edited.

## Suffix note (`.L`)

The pilot symbols are LSE-suffixed (`AAF.L` ...), unlike the suffix-free US
set, because `_bare_ticker()` returns the yfinance provider symbol. Nothing is
wired to the scanner/broker here, so this format is exposed only via the
explicit accessor and asserted in tests; any future runtime/broker consumer
must handle the `.L` suffix, which is a precondition for the separately-approved
wiring step.

## HK remains blocked / deferred

HK is excluded entirely from this pilot. HK readiness remains blocked on the
`provider_rate_limited` (Yahoo throttle) issue. No HK symbol is touched, loaded,
or activated.

## Europe remains unavailable

Europe stays unavailable because EU candidates are **0** (M21.U4 source work was
paused / source-blocked). No EU symbol exists to activate.

## Not approved / not done here

- no default scanner/runtime wiring
- no `_DEFAULT_PATHS` change
- no `active_selection.py` edit
- no `global_expanded.json` edit
- no `source_registry.json` edit
- no broker / live / paper execution
- no HK; no Europe/Japan/China/ADR

## Rollback

Delete `configs/universe/uk_pilot.json` and `bot/universe/uk_pilot.py` (or stop
calling the accessor). The default runtime path never referenced them, so
`get_scan_ready_symbols()` stays 536 throughout and US scanning is unaffected at
every stage.
