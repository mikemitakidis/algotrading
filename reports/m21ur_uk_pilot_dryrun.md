# M21.UR — UK Pilot Dry-Run / Check

- report_type: **UK pilot dry-run (scanner-behaviour proxy via M21.UQ evaluator)**
- scope: **UK pilot 5 symbols only**
- data_source: **simulated_fixture**
- network: **disabled**
- provider_mode: **yfinance**
- not_live_yfinance: **true**
- symbols_checked: **5** (AAF.L, AAL.L, ABDN.L, ABF.L, ADM.L)
- total_elapsed_seconds: **0.0005**

> Read-only. Uses the M21.UQ provider/evaluator path as the scanner-behaviour proxy; does NOT import or run bot/scanner.py, constructs no orders, sends no Telegram, touches no broker / live / paper code. Default US runtime is unchanged (the 536 US scan-ready set is never loaded here).

## Per-symbol result

| symbol | passed | reason_codes | bar_count | elapsed_s |
|---|---|---|---|---|
| `AAF.L` | no | `provider_rate_limited` | None | 0.0 |
| `AAL.L` | yes | — | 25 | 0.0001 |
| `ABDN.L` | yes | — | 25 | 0.0001 |
| `ABF.L` | yes | — | 25 | 0.0001 |
| `ADM.L` | yes | — | 25 | 0.0001 |

## Provider availability vs data quality (separated)

- provider_rate_limited (could not evaluate — throttle): `AAF.L`
- provider_fetch_error (could not evaluate — provider/network): none
- data-quality failures (real empty/stale/too-few/volume): none
- passed: `AAL.L`, `ABDN.L`, `ABF.L`, `ADM.L`

## Safety confirmation

- default runtime unchanged; US default scan-ready set not loaded here
- no `_DEFAULT_PATHS` change; no `global_expanded.json` / `source_registry.json` change
- no broker / live / paper routing; no orders; no Telegram
- explicit opt-in only; UK pilot 5 symbols only; no HK; no Europe/Japan/China/ADR
