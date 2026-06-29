# Runtime Registry — FOCUS_SIZE Shadow-Run

- report_type: **real scan_cycle FOCUS_SIZE shadow-run (fixture-backed)**
- universe_source: **us_default** (US default scan-ready registry, capped)
- focus_size: **150**
- data_source: **simulated_fixture**
- network: **disabled**
- provider: **fixture-arbitrary (monkeypatched; yfinance NOT called)**
- not_live_yfinance: **true**
- symbols_selected: **150**
- focus_sample: `A`, `AAPL`, `ABBV`, `ABNB`, `ABT`, `ACGL`, `ACN`, `ADBE`, `ADI`, `ADM`
- fixture_fetch_calls: **4**
- unique_symbols_requested: **150**
- meta.symbols_scanned: **150**
- signals_returned: **0**
- elapsed_seconds: **5.0058**

> Read-only shadow run of the REAL bot.scanner.scan_cycle with focus sourced from get_scan_ready_symbols()[:FOCUS_SIZE] (US default registry) and a monkeypatched fixture provider. No Yahoo/yfinance, no network, no DB writes (conn=None), no Telegram, no broker / live / paper. The global 193 set and the UK pilot are NOT loaded (unless --source uk_pilot is explicit).

## Safety confirmation

- real scan_cycle invoked; focus = US scan-ready capped at FOCUS_SIZE=150
- fixture provider used; yfinance not called; no network
- conn=None -> no DB insert / no flywheel log_candidate
- no Telegram; no broker / live / paper; no orders
- no global 193 load; no UK pilot (default); no HK; no Europe
- no main.py / bot/ edit; no default-path change
