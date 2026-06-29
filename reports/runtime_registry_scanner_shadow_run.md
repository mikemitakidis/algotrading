# Runtime Registry — Scanner Shadow-Run

- report_type: **real scan_cycle shadow-run (fixture-backed)**
- scope: **UK pilot 5 symbols only**
- data_source: **simulated_fixture**
- network: **disabled**
- provider: **fixture (monkeypatched; yfinance NOT called)**
- not_live_yfinance: **true**
- focus: **5** (AAF.L, AAL.L, ABDN.L, ABF.L, ADM.L)
- fixture_fetch_calls: **4**
- symbols actually requested: `AAF.L`, `AAL.L`, `ABDN.L`, `ABF.L`, `ADM.L`
- signals_returned: **0**
- elapsed_seconds: **0.2771**

> Read-only shadow run of the REAL bot.scanner.scan_cycle with a monkeypatched fixture data provider. No Yahoo/yfinance call, no network, no DB writes (conn=None), no Telegram, no broker / live / paper. Default runtime is unchanged; the US 536 set and the 193 global set are never loaded.

## Signals (deterministic)

(no actionable signals from the fixture series — the run still validates the full scan_cycle path end to end)

## Safety confirmation

- real scan_cycle invoked (not a copy); focus = 5 UK pilot only
- fixture provider used; yfinance provider not called; no network
- conn=None -> no DB insert / no flywheel log_candidate
- no Telegram; no broker / live / paper; no orders constructed
- no bot/scanner.py or bot/data.py edit; no default-path change
