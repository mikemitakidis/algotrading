# Milestone Status (Live State of Truth)

This file is the **single source of truth** for the current status of every
milestone. The narrative roadmap lives in [`ROADMAP.md`](ROADMAP.md); the
project-wide reconciliation narrative lives in
[`docs/PROJECT_STATUS_RECONCILIATION.md`](docs/PROJECT_STATUS_RECONCILIATION.md).

## Status legend

- **CLOSED** — implemented, tests/evidence captured, VPS-verified where applicable.
- **ACCEPTED ENOUGH** — implemented and working in production; known limitations
  exist but are tracked under another milestone, not blocking.
- **IMPLEMENTED, NOT VERIFIED** — code exists in repo, but no controlled
  verification has been run or evidence captured.
- **PARTIAL** — substantial implementation present, but the original goal is
  not fully met; remaining work is enumerated.
- **PENDING** — not started, but the plan is committed and the scope is bounded.
- **BLOCKED** — cannot proceed because of an external dependency.
- **SUPERSEDED / EXPANDED** — original scope replaced or absorbed into a
  different milestone; the redirect is documented.

> **Historical drift warning.** Earlier project notes described M13 as
> "externally blocked" and M14 as "not started." Those were true at their
> time of writing and are no longer the case. This file reflects the
> current state; see `docs/PROJECT_STATUS_RECONCILIATION.md` for the
> historical-vs-current diff.

---

## Quick status table

| # | Milestone | Status | Key evidence |
|---|---|---|---|
| 1 | Stable Shadow-Mode Scanner | CLOSED | `bot/scanner.py`; VPS heartbeat fresh on every M13.5.C–M14.D verification |
| 2 | Telegram from Dashboard | CLOSED | `dashboard/app.py`; M13.5.C/M14.B/C/D Telegram status messages sent |
| 3 | Dashboard Observability | CLOSED | Dashboard live on `:8080/api/health` → 200 (every VPS verification) |
| 4 | Strategy Engine | CLOSED | `bot/strategy.py` + `data/strategy.json` round-trip |
| 5 | Backtesting | ACCEPTED ENOUGH | `bot/backtest.py`, `backtest_cli.py`; Yahoo/cache limits tracked under M6/M15 |
| 6 | Modular Data-Provider Architecture | CLOSED | `bot/providers/{base,yfinance_provider,alpaca_provider}.py` |
| 7 | More Indicators / Richer Logging | CLOSED | `_ml_features` block per signal in `bot/scanner.py:~245` |
| 8 | News / Sentiment Module | IMPLEMENTED, NOT CLOSED-LOOP | `bot/sentiment/news_provider.py` (456 lines, real NewsAPI); single-provider; macro/multi-source → M18 |
| 9 | ML Pipeline | INFRASTRUCTURE COMPLETE, NOT CLOSED-LOOP | `ml_train.py` (541 lines XGBoost meta-labeling), `ml_build_dataset.py`; not wired as a live filter in `scanner.py`; closed-loop → M17 |
| 10 | Broker Execution Architecture | CLOSED | `bot/brokers/{base,ibkr_broker,paper_broker}.py`; flywheel schema |
| 11 | IBKR Paper Trading | CLOSED | `bot/brokers/ibkr_broker.py` + IBC 3.22.0; `test_m11.py` |
| 12 | IBKR Live Trading | CLOSED | Real broker acceptance proven; live `permId`; truthful `execution_intents`; no remaining F exposure |
| 13 | eToro Integration / Manual Bridge | CLOSED | `docs/M13_7_closeout.md` (chain → `1e2ced7`); zero real orders placed |
| 14 | Portfolio / Risk Layer | CLOSED | All sub-milestones A–H closed; see `docs/M14_FINAL_AUDIT.md` |
| 15 | Production Hardening | CLOSED (M15.0-pre/.0/.1/.2/.4/.5/.3.A/.3.A.2/.3.A.cutover/.3.B/.3.C all CLOSED 2026-06-05) | See M15 detail below |
| 16 | Historical Data + First Signal Engine | CLOSED 2026-06-05 (M16.A + M16.B + fixes 1-4) | See M16 detail below; `bot/historical/*`, `data/historical/yfinance/1D/AAPL.parquet` (real data); commit chain `c6e98b7` → `aef8335` |
| 17–23 | Future scope | PENDING | See `ROADMAP.md`. The M1–M16 audit-only pass is now CLOSED (P0 batch verified 2026-06-05, commit chain `655c955` → `268a50b`); the next concrete work item is operator-chosen. M17 has not started. |

---

## Per-milestone detail

### Milestone 1 — Stable Shadow-Mode Scanner (CLOSED)
- **Files:** `bot/scanner.py`, `bot/strategy.py`, `main.py`, `bot/providers/`.
- **Evidence:** Bot runs 24/7 on Hetzner VPS (`/opt/algo-trader`); every M13.5.C / M14.B / M14.C / M14.D VPS verification confirms `/api/health` 200 and a fresh heartbeat.
- **VPS proof:** scanner produces signals; SQLite `signals.db` grows daily.
- **Open gaps:** none for the goal; signal/universe diagnostic improvements live under M19.

### Milestone 2 — Telegram from Dashboard (CLOSED)
- **Files:** `dashboard/app.py`, `bot/notifier.py`.
- **Evidence:** Telegram token + chat_id configurable from UI; "Find My ID" works; Send Test succeeds. Every closeout milestone sends a Telegram status message via the existing notifier (M13.5.C, M14.B, M14.C, M14.D all delivered).
- **Open gaps:** none.

### Milestone 3 — Dashboard Observability (CLOSED)
- **Files:** `dashboard/app.py` + dashboard JS; `bot/scanner.py` writes `data/bot_state.json` (path is referenced in the live runtime; the writer lives inside scanner.py state-management).
- **Evidence:** dashboard renders phase / cycle / system panels; `:8080/api/health` returns 200 in every VPS verification.
- **History:** closed after a GitHub/server branch reconciliation and JS fixes.
- **Open gaps:** none for the goal.

### Milestone 4 — Strategy Engine (CLOSED)
- **Files:** `bot/strategy.py`, `data/strategy.json` (runtime), dashboard Strategy tab.
- **Evidence:** scanner reads `data/strategy.json` on every cycle; default-reset + validation present.
- **Open gaps:** none.

### Milestone 5 — Backtesting (ACCEPTED ENOUGH)
- **Files:** `bot/backtest.py`, `backtest_cli.py`.
- **Evidence:** walk-forward backtest using the live strategy code path (`compute()` / `score_timeframe()` / `load_strategy()`); 3-tier cache; status state machine; full stats including monthly breakdown and equity curve.
- **Known limitations:** Yahoo/yfinance cache limits and rate-pacing can cause partial runs. Tracked under M6 (provider) and M15 hardening, not reopened.
- **Open gaps:** alternative provider integration testing; deferred to M16 strategy/historical intelligence.

### Milestone 6 — Modular Data-Provider Architecture (CLOSED)
- **Files:** `bot/providers/base.py`, `bot/providers/yfinance_provider.py`, `bot/providers/alpaca_provider.py`.
- **Evidence:** `DATA_PROVIDER=` env-switchable; alternate provider implementation exists.
- **Open gaps:** none for the goal.

### Milestone 7 — More Indicators / Richer Logging (CLOSED)
- **Files:** `bot/scanner.py` attaches `_ml_features` block to every signal (visible at `bot/scanner.py:~245`); `bot/strategy.py`; signal payload feeds `ml_train.py`.
- **Evidence:** feature snapshot logged per-signal; ML dataset assembly works.
- **Open gaps:** none for the goal.

### Milestone 8 — News / Sentiment Module (IMPLEMENTED, NOT CLOSED-LOOP)
- **Files:**
  - `bot/sentiment/__init__.py` — pluggable provider factory + `apply_sentiment(signal, result, mode)`.
  - `bot/sentiment/news_provider.py` (~456 lines) — real NewsAPI integration with caching, error classification, fetch-success flag, headline extraction.
  - `bot/sentiment/disabled_provider.py` — opt-out path.
  - `bot/sentiment/base.py` — `SentimentResult` dataclass with `unavailable()` factory.
- **Evidence:** Sentiment integrated into the live cycle in `bot/scanner.py` (provider selection at line ~103; per-symbol `sent_provider.get_sentiment(sym)` at line ~250; signals are blocked by sentiment when mode requires alignment).
- **Honest status:** real, used in production. **Not closed** because:
  - Single-provider only (no aggregation across sources)
  - No macro overlay (rates, VIX, calendar events)
  - No confidence-weighted scoring across providers
- **Closure path:** these items are M18 scope, not M8.

### Milestone 9 — ML Pipeline (INFRASTRUCTURE COMPLETE, NOT CLOSED-LOOP)
- **Files:**
  - `ml_train.py` (~541 lines) — XGBoost meta-labeling with walk-forward TimeSeriesSplit, isotonic-calibrated probabilities, precision-recall curve, filter-comparison tables, per-group evaluation, honest verdict output.
  - `ml_build_dataset.py` — dataset assembly from `data/ml/training_dataset.parquet` or scattered `data/reports/*/results.json`.
  - Data flywheel tables (`candidate_snapshots`, `execution_intents`, `signal_outcomes`) feed the dataset.
- **Honest gap (this is the user-flagged correction):** `ml_train.py` trains and evaluates only. **No live filter** in `bot/scanner.py`: `grep -nE "model.predict|load_model|joblib|xgb" bot/scanner.py` returns empty.
- **Therefore:** M9 is not a "professional self-learning layer." It is XGBoost training infrastructure that needs the M17 closed-loop hookup (live shadow scoring → outcome capture → retraining cadence) to become self-learning.

### Milestone 10 — Broker Execution Architecture (CLOSED)
- **Files:** `bot/brokers/base.py` (`BrokerAdapter`, `OrderIntent`, `OrderResult`), `bot/brokers/paper_broker.py`, `bot/brokers/ibkr_broker.py`, registry in `bot/brokers/__init__.py`.
- **Evidence:** `BROKER=` env switching honoured; `test_m12.py` exercises the registry; flywheel schema in place.
- **Open gaps:** none for the architecture goal. Live execution sits in M11/M12.

### Milestone 11 — IBKR Paper Trading (CLOSED)
- **Files:** `bot/brokers/ibkr_broker.py` (paper port 4002 path; `_check_live_safety_config()` gate; `get_positions()`, `reconcile()`).
- **Infrastructure:** IBC 3.22.0 headless IB Gateway on `DISPLAY=:99`, systemd `Restart=always`, nightly `AutoRestartTime=23:45`.
- **Evidence:** `test_m11.py` records "Logged in to PAPER account DUP623346" + "API connections enabled (port 4002)". Paper login flow verified.

### Milestone 12 — IBKR Live Trading (CLOSED, capability proven)
- **Files:** `bot/brokers/ibkr_broker.py` live mode (port 4001 / `config.live.ini` / `start_ibgateway_live.sh` / `/var/lib/ibgateway-live`).
- **Evidence:**
  - Controlled live order (Ford / F, 1 share, delayed market data) accepted by the live broker with a confirmed `permId`.
  - `execution_intents` row reflects the truthful state (no fabrication, no fake IDs).
  - Position cancelled cleanly afterwards; no remaining F exposure on the account.
  - Bot returned to paper after the test.
- **Test artifacts:** `test_m12.py` (offline, 13 tests), `test_m12_live_order.py` (`--live` flag for Gateway connection + reconciliation).
- **Note on "sustained live trading":** M12 closes the *capability* milestone. Sustained automated live trading is a later phase (M22 semi-automated live trading), gated by M14.E governor + M14.F preflight integration + risk acceptance.

### Milestone 13 — eToro Integration / Manual Bridge (CLOSED)
- **Files:** 15 modules under `bot/etoro/`, `tools/etoro_live_write.py`, `tools/etoro_reconcile.py`, 9 test suites under `test_m13_5_*.py`, 6 docs under `docs/M13_*.md`.
- **Closeout artifact:** `docs/M13_7_closeout.md` (commit chain `61a` → `1e2ced7`, 13 accepted safety invariants with 173 proving tests in `test_m13_5_*.py` + M13.2 42 + M13.3 48 + M13.4A 61).
- **Safety stance:** demo disabled (fail-closed), `--base-url` removed, real mode pinned to `https://public-api.etoro.com`, double live-flag + per-payload nonce, full scanner-isolation, no dashboard live-write button.
- **Zero real eToro orders placed.** First funded eToro order is **outside M13 and M14**; tracked as **M21** (First Funded eToro Go-Live).
- **Status correction:** older notes called M13 "externally blocked." That is no longer true; M13 closed in the current chat thread.

### Milestone 14 — Portfolio / Risk Layer (CLOSED — A through H)

**Authoritative closeout:** [`docs/M14_FINAL_AUDIT.md`](docs/M14_FINAL_AUDIT.md).

| Sub-milestone | Status | Commit | Evidence |
|---|---|---|---|
| M14.A — Risk Intelligence Design | CLOSED | `3f4448e` | `docs/M14_A_design.md` |
| M14.B — Schema + migration | CLOSED | `42ee08c` | `test_m14_b_schema.py` 27/27; VPS verified |
| M14.C — Realised-PnL ingestion adapters | CLOSED | `d9c53eb` | `test_m14_c_ingest.py` 47/47; VPS dry-run verified |
| M14.D — Exposure ingestion + `broker_positions` | CLOSED | `729ad2d` | `test_m14_d_exposure.py` 60/60; VPS dry-run verified |
| M14.E — Risk Authority Engine + Governor | CLOSED | `ace0fda` | `test_m14_e_engine.py` 105/105; VPS verified |
| M14.F — eToro preflight integration | CLOSED | `2e20b52` | `test_m14_f_preflight.py` 34/34; VPS verified |
| M14.G — Dashboard read-only visibility | CLOSED | `71e893a` | `test_m14_g_dashboard.py` 51/51; VPS verified |
| M14.H — Closeout / audit doc | CLOSED | (this commit) | `docs/M14_FINAL_AUDIT.md` |

**M14 totals:** 9 commits on `main`; ~12,321 lines added; 17 new modules under `bot/risk_authority/`; 324 sub-milestone tests; 25 engine gates; 31 reason codes; 4 read-only dashboard endpoints; 10 layers of live-write defense in depth; **0** real-money orders placed; **0** scanner-to-live bypasses introduced.

**Carry-forward limitations (tracked in M15):**
- IBKR exposure reader is a `NotImplementedError` stub — engine returns `exposure_unknown` for IBKR scopes until wired to Gateway (M15.x).
- eToro keys absent on VPS (no `ETORO_LIVE_ENABLED`, no `ETORO_REAL_API_KEY`). First funded order is M21, not M14.
- Dashboard accessed via `http://138.199.196.95:8080/` — security hardening is M15.3.
- `manual_reset` is design-only; no UI or API path can issue one in M14.

These are acceptable for M14 closure because every "unknown" returns fail-closed; the engine refuses to fabricate zero, and the dashboard distinguishes known-zero from unknown-zero explicitly.

**Known coverage gap (open carry-forward, recorded 2026-06-05 at M1–M16 audit pass).** The M14 Risk Authority Engine (24 gates) is invoked **only** from the operator-CLI eToro live-write path (`tools/etoro_live_write.py` → `bot/risk_authority/preflight.py`). The scanner-driven IBKR submit path in `main.py` runs only `bot/risk.py` (`RiskManager` + `PortfolioRiskPolicy`) — a smaller gate set that does not include `broker_daily_loss_cap`, `global_capital`, `combined_exposure`, `drawdown_throttle`, per-symbol concentration, `quote_freshness`, `spread`, or `data_staleness`. This asymmetry is deliberate for M14 closure but is a **hard pre-requisite to address before M22 (Semi-Automated Live Trading)**: the gate set the scanner enforces must equal or exceed the M14 engine's gate set at M22 time. NOT in M17 scope. Full text in [`docs/M14_FINAL_AUDIT.md` §12](docs/M14_FINAL_AUDIT.md). Tracking: `docs/NEXT_WORK_REGISTER.md` entry `M14-extension-to-scanner-path`.

### Milestone 15 — Production Hardening (PARTIAL)

| Sub-milestone | Status | Evidence |
|---|---|---|
| M15.0-pre — Flywheel schema baseline (prerequisite for M14) | CLOSED | `bot/flywheel.py`; `test_m15_schema.py` 6/6. *Originally labelled M15.0; renumbered here to disambiguate from the production-process M15.0 that closed 2026-06-02.* |
| M15.1 — Gateway state + reconciliation | CLOSED | `test_m15_gateway.py` 33/33 |
| M15.2 — Health endpoint + external monitoring | CLOSED | `test_m15_2_health.py` 28/28; `docs/M15_2_external_monitoring.md` |
| M15.0 — Scanner / systemd reliability + production process clarity | CLOSED | `597635d` (chain `57dc200` → `597635d`); `test_m15_0_service.py` 40/40; VPS-verified 2026-06-02 |
| M15.4 — IB Gateway reliability + broker connectivity health (visibility/truth layer) | CLOSED | `073a8bd`; `test_m15_4_gateway_health.py` 47/47; VPS-verified 2026-06-02. Login-error precedence hardened in post-VPS patch `2446df6` (`test_m15_4_gateway_health.py` 50/50). |
| M15.5 — IBKR exposure reader wiring (paper mode) | CLOSED | `138df9e` → `2446df6` (cross-confirm + phased dry-run + login-error gate hardening); `test_m15_5_ibkr_exposure.py` 78/78; VPS-verified 2026-06-03 with real paper ingest succeeded. |
| M15.3.A — Dashboard auth/security hardening | CLOSED | `34fc157` → `c280a83` (script-mode sys.path bootstrap + `--stdin` for setpw tool) → `f26407f` (setpw sys.path bootstrap + M15.3.A.2 carry-forward); `test_m15_3_a_dashboard_auth.py` 97/97; VPS-verified 2026-06-04 with real operator browser login succeeded. |
| M15.3.A.2 — Dashboard TOTP / Google Authenticator 2FA | CLOSED | `723b963` (implementation) → `7ab7555` (test-fixture VPS fix); `test_m15_3_a_2_totp.py` 52/52; VPS-verified 2026-06-04 with real operator end-to-end login via password + Google Authenticator code; `auth_events` recorded `totp_setup`, `totp_success`, `login_success`; secret-material audit invariant verified (no secret/code/URI/password in `extras_json`). |
| M15.3.A.cutover — Caddy/TLS + 127.0.0.1 bind | CLOSED | Caddy install + Caddyfile + ACME issuance for `algotrading.marketwarrior.club` → `224e8a3` (production fix: `app.run(host=_m153a_bind_host)` replaces hardcoded `'0.0.0.0'`) → `383bec0` (test-fixture dotenv isolation against post-cutover VPS .env); `test_m15_3_a_dashboard_auth.py` 101/101; VPS-verified 2026-06-04: `ss` shows `127.0.0.1:8080`, HTTPS HTTP/2 via Caddy, HTTP→308→HTTPS redirect, browser login via password + Google Authenticator over `https://algotrading.marketwarrior.club`. |
| M15.3.B — `manual_reset` operator flow | CLOSED | `2f55f1d` (implementation + tests + runbook); `test_m15_3_b_manual_reset.py` 51/51; VPS-verified 2026-06-04 by operator browser test: cleared `ibkr.kill_switch` end-to-end via Recovery UI (password + step-up TOTP + 60s preview token + typed `RESET` + 10-500 char reason), `auth_event_id=38`, `decision_id=mr-3086a40a9b2f46e5`. |
| M15.3.C — Compliance audit + export | CLOSED | `0018c32` (implementation) → `02b5dcf` (ExportAttemptLimiter rate-limit fix per ChatGPT review); `test_m15_3_c_audit_export.py` 37/37; VPS-verified 2026-06-05: terminal regression sweep all green + operator browser end-to-end downloaded both JSONL (`audit_export_20260605T094145Z.jsonl`, 48 auth + 1 rd, SHA-256 verified) and CSV ZIP (`audit_export_20260605T094158Z.zip`, 50 auth + 1 rd — the +2 vs JSONL is the meta-audit chain working correctly: the JSONL export itself wrote 1 audit row, then the CSV export wrote another). |

**M15.5 closeout (IBKR paper exposure reader wired)** — VPS-verified 2026-06-03:
- The `NotImplementedError` stub at `tools/ingest_exposure_state.py::_build_ibkr_exposure_adapter` for `ibkr_paper` is replaced by a real read-only IB API positions reader at `bot/risk_authority/ibkr_paper_reader.py`. The reader connects to `127.0.0.1:4002` with `clientId=15`, `readonly=True`, waits for the account-update snapshot to be ready (bounded by `api_timeout`), reads both `ib.portfolio()` and `ib.positions()` for cross-confirmation, then disconnects in a `finally` block. The M14.D `IBKRExposureAdapter` is byte-identical — M15.5 only supplies a real `positions_reader` callable.
- **`ibkr_live` remains intentionally unwired** and continues to raise `NotImplementedError` from the CLI path. Live wiring requires a separately approved milestone.
- Live VPS evidence on closeout day: real ingest connected to IBKR paper, server version 176, synchronization complete, disconnected cleanly. Confirmed zero open positions: `open_positions=0`, `capital_deployed_usd=0.0`, `positions_written=0`. Exit code 0. No orders placed, no broker writes, no live mode exercised.
- Latest `daily_state_per_broker` row for `(2026-06-03, ibkr_paper)`: `exposure_status=exposure_partial`, `exposure_fresh_reads_count=1`, `source=ingested`, `exposure_missing_fields=["current_equity_usd", "peak_equity_usd"]`.
- **Risk Authority verification** all three surfaces report `ibkr_paper.exposure_known=True` (DB lookup, snapshot ScopeView, M14.G dashboard helper). The pre-M15.5 fail-closed behaviour on `exposure_unknown` for `ibkr_paper` is now resolved on real paper data.
- **`exposure_partial` is by design** and accepted as "known exposure" by both M14.E engine (`snapshot.py:77-80` `is_exposure_known()` returns True for both `exposure_fresh` and `exposure_partial`; every engine gate consults this predicate) and M14.G dashboard (`dashboard_read.py:211`). Missing fields are `current_equity_usd` and `peak_equity_usd` — both classified as `OPPORTUNISTIC_EXPOSURE` in `bot/risk_authority/exposure_reading.py:52-56`, not `REQUIRED_FOR_FRESH_EXPOSURE`. The `current_equity_usd` polish (via `ib.accountSummary()`) was offered as a path-B option and explicitly declined; M15.5 closes at the path-A boundary.
- **The `exposure_stale` warning remains expected** while `exposure_fresh_reads_count < 3` (current value 1). This is a UI-only badge in `bot/risk_authority/dashboard_read.py:148-150`; the engine gate threshold is `< 1` (`engine.py:678`), already cleared. The warning will resolve after two additional successful ingests.
- **`pnl_unknown` is separate and out of M15.5 scope.** It tracks PnL ingestion for `ibkr_paper` (M14.C surface) and is independent of exposure wiring. Resolving it is a future-work item.
- Hard-constraint evidence: M14.D adapter byte-identical vs `d73a04a`; M14 engine/governor/snapshot/audit/preflight modules untouched; AST scan rejects every order method (`placeOrder/cancelOrder/modifyOrder/reqGlobalCancel/reqMktData/reqHistoricalData/reqOpenOrders/reqExecutions`) on every commit; `readonly=True` AST-asserted on every `connect()` call; `ibkr_live` CLI path still raises `NotImplementedError`.
- Authoritative operator reference: [`docs/M15_5_ibkr_exposure_reader.md`](docs/M15_5_ibkr_exposure_reader.md). Dry-run-first workflow remains required before any real ingest (`run_paper_dryrun()` with phased observability: `error_phase`, `elapsed_ms`, per-step booleans).

**M15.3.A closeout (dashboard auth/security hardening)** — VPS-verified 2026-06-04:
- **Status:** CLOSED. Implementation chain `34fc157` → `c280a83` → `f26407f`. Operator manually logged into the dashboard in a browser with the new bcrypt-hashed password on closeout day.
- **What shipped:**
  - **bcrypt password verification** (cost factor 12, `DASHBOARD_PASSWORD_HASH`) preferred; plaintext `DASHBOARD_PASSWORD` retained as transitional fallback. The default `'changeme'` is REJECTED.
  - **Login rate-limit**: in-memory sliding window — 5 failures / 10 min → 15 min lockout per `client_ip`. The in-memory trade-off was accepted per Q-A.1; persistence is deferred under `M15.3.A.persist`.
  - **CSRF protection** on all 16 non-exempt state-changing POST endpoints (only `/api/login` exempt). Inline-JS `window.fetch` monkey-patched once at the top of the embedded HTML so every existing `fetch(...)` call site auto-attaches the `X-CSRF-Token` header — zero call-site changes.
  - **Session cookies**: `HttpOnly=True` always, `SameSite=Strict` always, `Secure=` env-gated via `DASHBOARD_HTTPS_MODE` or `DASHBOARD_COOKIE_SECURE` (not unconditional — would have broken login over plain HTTP during the Caddy transition window).
  - **Hybrid session timeout**: 30 min idle + 12 h absolute, both env-configurable. Legacy-session first-deploy grace.
  - **Stable `DASHBOARD_SECRET_KEY`** env var (no longer password-derived; auto-generated by `tools/set_dashboard_password.py` on first run).
  - **`auth_events` append-only audit log** (sha256-hashed session IDs per Q-A.8; closed `kind` set with SQLite CHECK constraints).
  - **Soft bind-host cutover** — default `0.0.0.0:8080` retained with explicit startup warning. `DASHBOARD_BIND_HOST=127.0.0.1` + Caddy/TLS final cutover is recorded under `M15.3.A.cutover` (operator action; not done in M15.3.A by design).
  - **`tools/set_dashboard_password.py`**: interactive bcrypt setter that backs up `.env`, preserves unrelated lines, sets 0600 perms, never prints the password. `--stdin` flag for non-interactive automation. Operates from any cwd without requiring `PYTHONPATH` (sys.path bootstrap fixed in `f26407f` after VPS verification of `c280a83` revealed the helper still needed `PYTHONPATH=/opt/algo-trader` as a workaround).
- **Two real bugs found and fixed during VPS verification** (test-suite gaps the sandbox masked):
  1. `34fc157` → `c280a83`: dashboard service crash-looped on the VPS (`NRestarts=68`, no listener) because systemd invokes `python3 /opt/algo-trader/dashboard/app.py` as a script and Python's script-mode `sys.path` only contains the script's directory, not the repo root — so `from dashboard.auth import ...` raised `ModuleNotFoundError` before any logging handler could capture the traceback. Fixed by prepending the repo root to `sys.path` at the top of `dashboard/app.py`. A new `TestScriptModeInvocation` test now invokes the script the same way systemd does and was negative-verified to catch the bug on the unfixed code.
  2. `c280a83` → `f26407f`: same root cause in `tools/set_dashboard_password.py` — operator had to run with `PYTHONPATH=/opt/algo-trader` as a workaround. Fixed with the same sys.path bootstrap; a new `test_subprocess_works_without_PYTHONPATH_from_non_repo_cwd` regression test runs the tool from `/tmp` with `PYTHONPATH` cleared.
- **Test evidence**: `test_m15_3_a_dashboard_auth.py` 97/97 (9 test groups covering password verify, rate-limit, session hardening, CSRF primitives, bind-host behaviour, audit DAO, login endpoint, CSRF enforcement, existing-endpoints regression, no-forbidden-surface AST scan, protected-files git-diff sweep, real-HTTP cookie flags, set-password subprocess including the script-mode regression). Regressions: `test_m13_4a_allocation` 61/61 (CSRF-aware test update was required — minimal `_csrf_headers()` helper, no production-code workaround), `test_m14_g_dashboard` 51/51, `test_m15_5_ibkr_exposure` 78/78, `test_m15_4_gateway_health` 50/50.
- **VPS verification facts (2026-06-04)**: HEAD = `f26407f`; dashboard `is-active = active`; `/` → HTTP 200; `/api/health` → HTTP 200; `auth_events` table present with 8 expected columns; `DASHBOARD_PASSWORD_HASH` valid bcrypt prefix `$2b$`, length 60; `DASHBOARD_SECRET_KEY` length 64; `.env` permissions `0o600`; operator successfully logged into the dashboard in a browser.
- **Hard-constraint evidence**: protected files modified vs `60281c4` (pre-M15.3.A baseline): 0 / 24. AST scan rejects every order method (`placeOrder/cancelOrder/modifyOrder/reqGlobalCancel/reqMktData/reqHistoricalData/reqOpenOrders/reqExecutions`) in the M15.3.A `dashboard/auth/` modules. No imports of `bot.scanner`, `bot.strategy`, `bot.brokers`, `bot.etoro`, `ib_insync`, or any `bot.risk_authority.*` engine module from any auth module.
- **Authoritative operator reference**: [`docs/M15_3_A_dashboard_auth.md`](docs/M15_3_A_dashboard_auth.md). Carry-forward items deferred from M15.3.A and tracked in [`docs/NEXT_WORK_REGISTER.md`](docs/NEXT_WORK_REGISTER.md): `M15.3.A.cutover` (Caddy/TLS operator action), `M15.3.A.persist` (DB-backed rate-limit), `M15.3.A.2` (TOTP 2FA, proposed before M15.3.B), and the newly-recorded `M15.3.D or later — multi-user/read-only dashboard roles`.

**M15.3.A.2 closeout (Dashboard TOTP / Google Authenticator 2FA)** — VPS-verified 2026-06-04:
- **Status:** CLOSED. Implementation chain `723b963` (initial) → `7ab7555` (test-fixture VPS fix). Pre-code checklist Q-A.1..Q-A.11 + Corrections 1–9 all honoured.
- **What shipped:**
  - **New module `dashboard/auth/totp.py`** — TOTP primitives (RFC 6238, 30-sec window, ±1 step tolerance), in-memory replay cache keyed by `(sha256(secret)[:16], time_step)` with 120-sec TTL. Per Q-A.10 correction: no raw codes or secrets stored in memory; cache uses sha256-truncated fingerprints only. Dependency-injectable clock + secret for testability.
  - **`/api/login` extended** in `dashboard/app.py` with a second-factor block between password verify and session rotation. **Hard guarantee**: when `DASHBOARD_TOTP_SECRET` is unset/empty, login behaviour is byte-identical to M15.3.A — password-only login is preserved.
  - **`/api/login` failure semantics** per Correction 3: wrong-password → generic 401; wrong-TOTP → generic 401 (does not leak whether code was wrong/expired/replay/format-invalid); right-password + missing-TOTP → 401 `{"error": "totp_required"}` (UX hint after password validates — acknowledged password-validity oracle, rate-limit-capped at 5 probes / 15 min). Missing-TOTP does NOT increment the failure counter (operator forgot the code); wrong-TOTP DOES increment the same per-IP bucket as wrong-password.
  - **`tools/set_dashboard_password.py` gained two flags**: `--enable-totp` (sanity-checks password is set; refuses overwrite; generates fresh secret; renders Unicode-block QR to operator's terminal; prompts for first code; **verifies before writing `.env`**; aborts cleanly on Ctrl-C or wrong code with `.env` untouched); `--disable-totp` (removes only `DASHBOARD_TOTP_SECRET`; preserves password hash + secret key; best-effort `totp_disabled` audit write — the recovery path must not block on broken DB).
  - **Login form** gained an always-visible TOTP input (per Q-A.8 — no probe endpoint to detect whether TOTP is enabled). JS handles the `totp_required` response by focusing the TOTP field with an orange outline.
  - **`auth_events` ALLOWED_KINDS** extended with 5 new closed values: `totp_success`, `totp_failure`, `totp_required_not_provided`, `totp_setup`, `totp_disabled`. No schema migration (kind enforcement is code-side). **`extras_json` invariant** (Correction 4): NEVER contains the code, the secret, the otpauth URI, or password material. Asserted by `test_extras_json_never_contains_secret_material`. VPS verification confirmed `SECRET_MATERIAL_DETECTED = False` against the live audit log.
  - **New deps pinned**: `pyotp==2.9.0`, `qrcode==7.4.2`. Clean-venv proof: `pip install -r requirements.txt` exit 0, `pip check` "No broken requirements found", `import pyotp, qrcode` exit 0.
- **Test evidence**: `test_m15_3_a_2_totp.py` 52/52 OK across 8 groups + 1 fixture-robustness regression. Test groups cover TOTP primitives, disabled-mode hard guarantee (password-only login when env unset), enabled-mode flows (missing/wrong/right code paths), setpw tool flags (write-after-verify, refuse-overwrite, idempotent disable, mutually-exclusive flags, no-secret-on-stderr), rate-limit integration (same per-IP bucket as wrong-password; missing-TOTP does not count), replay prevention (same time-step blocked within TTL, different secrets independent, cache key is fingerprint not raw secret), and `auth_events` kinds + extras_json invariant.
- **VPS-verification regression caught + fixed during closeout** (`723b963` → `7ab7555`): `test_extras_json_never_contains_secret_material` failed on VPS only. Root cause was a **test-setup bug, not production-code bug**: `dashboard.app` calls `load_dotenv()` at module-import time; the original `_make_test_app` cleaned `os.environ` BEFORE the dashboard.app import, so dotenv re-populated `DASHBOARD_PASSWORD_HASH` from the real `.env` AFTER the cleanup. `verify_password` then saw the real (operator) hash and rejected the test's plaintext password — login returned 401 at the password step, never reaching the TOTP block, so no `totp_*` audit rows were written. Sandbox did not reproduce because the sandbox had no `.env` file. Fix: import dashboard.app first (let dotenv run), then clean env, then set test values. New `test_fixture_overrides_preexisting_password_hash_from_env` regression test explicitly seeds a real bcrypt hash into `os.environ` before invoking the fixture and asserts the fixture cleanly overrides it. Negative-verified by reverting the fix and confirming the test catches the exact failure mode. **The "extras_json never leaks secrets" production invariant was unchanged by the fix — once login reaches the TOTP block, the assertion logic runs exactly as before.**
- **VPS verification facts (2026-06-04, HEAD `7ab7555`)**: M15.3.A.2 tests 52/52 OK; M15.3.A regression 97/97 OK; clean temp venv `CLEAN_INSTALL_EXIT=0`, `CLEAN_CHECK_EXIT=0`, `CLEAN_IMPORT_EXIT=0`; pyotp/qrcode imports OK in service venv; dashboard `is-active = active`; `/` → 200; `/api/health` → 200; `--enable-totp` succeeded interactively (`.env` backup created, `DASHBOARD_TOTP_SECRET` written with length 32 base32); **operator logged into the dashboard in a real browser session with password + Google Authenticator code**; `auth_events` recorded `totp_setup`, `totp_success`, `login_success`; redacted audit check `SECRET_MATERIAL_DETECTED = False`.
- **Hard-constraint evidence**: protected files modified vs `648682c` (pre-M15.3.A.2 baseline) across both commits `723b963` and `7ab7555`: **0 / 24**. AST scan of `dashboard/auth/totp.py` confirms no broker/scanner/strategy/engine imports and no order-method names. No systemd changes. No `sync.sh` or `deploy.sh` changes. No `manual_reset` code. No multi-user code. No live mode. No orders. No broker writes.
- **Authoritative operator reference**: [`docs/M15_3_A_dashboard_auth.md`](docs/M15_3_A_dashboard_auth.md) §12 (TOTP runbook covering enable/disable, login matrix, replay model, audit kinds, and honest threat-model trade-offs).
- **Honest trade-offs (documented in runbook §12.6)**:
  - **TOTP does NOT substitute for HTTPS.** Over plain HTTP an on-path attacker can still steal a valid session cookie after a successful 2FA login. `M15.3.A.cutover` (Caddy/TLS) remains an open carry-forward and a real prerequisite for state-changing operator actions like `M15.3.B` manual_reset.
  - **`totp_required` is a small password-validity oracle** — rate-limit-capped at 5 probes / 15 min. Approved trade-off for legitimate-operator UX clarity.
  - **In-memory replay cache resets on dashboard restart** — same trade-off as M15.3.A rate-limiter. A DB-backed variant is deferable to `M15.3.A.2.persist` if a real incident materializes.

**M15.3.A.cutover closeout (Caddy/TLS + 127.0.0.1 bind)** — VPS-verified 2026-06-04:
- **Status:** CLOSED. Operator runbook executed in three phases (Caddy install + Caddyfile + ACME cert issuance; production-code bind-host fix at `224e8a3`; test-fixture dotenv-isolation fix at `383bec0`). Domain `algotrading.marketwarrior.club` is now the canonical entrypoint.
- **What shipped (operator config + 2 test-only commits):**
  - **Caddy as HTTPS reverse-proxy** at `/etc/caddy/Caddyfile`: TLS via Let's Encrypt ACME (HTTP-01 challenge), automatic HTTP→HTTPS redirect, `X-Real-IP` + `X-Forwarded-For` propagation (the dashboard's M15.3.A `_m153a_client_ip()` honours these correctly, so audit rows and rate-limit buckets now reflect the real client IP behind Caddy). HTTP/2 enabled by default.
  - **Production-code fix `224e8a3`**: `dashboard/app.py`'s `if __name__ == '__main__':` block now passes `host=_m153a_bind_host` (the env-controlled variable from line 103) instead of a hardcoded `'0.0.0.0'`. The bug was discovered during Phase 2 operator verification: after writing `DASHBOARD_BIND_HOST=127.0.0.1` to `.env`, `ss` still showed the dashboard on `0.0.0.0:8080`. Root cause: the env var was correctly READ at module top but IGNORED at the actual `app.run()` call site. Fix is 1 functional line + 5 comment lines. Three regression tests added (AST scan of the `app.run()` call + two subprocess env→variable tests).
  - **Test-fixture fix `383bec0`**: the production cutover landed correctly, but `test_m15_3_a_dashboard_auth.py` failed 21/100 on the VPS afterwards. Same class of bug as M15.3.A.2 fix-1 (commit `7ab7555`): the operator's `/opt/algo-trader/.env` now carries `DASHBOARD_PASSWORD_HASH` + `DASHBOARD_TOTP_SECRET` + `DASHBOARD_BIND_HOST=127.0.0.1` + `DASHBOARD_HTTPS_MODE=true`, and dotenv re-populated all of these AFTER the test fixture's `_clean_auth_env()` had run. Password-only login tests returned `totp_required`; bind-host default test saw `127.0.0.1` instead of `0.0.0.0`. Fix-2 reorders `_make_test_app` to the same import-first-then-clean pattern as M15.3.A.2 fix-1, extends `_AUTH_ENV_KEYS` with `DASHBOARD_TOTP_SECRET` + `DASHBOARD_PORT`, seeds empty env vars in the two tests that reload dashboard.app (so dotenv's `override=False` leaves them empty), and adds a new regression test that explicitly simulates VPS dotenv pollution. Same fix applied proactively to `test_m13_4a_allocation.py` which had identical exposure (operator-verified clean on VPS post-fix). **Production code in `dashboard/app.py` not touched in fix-2; production VPS `.env` not touched.**
  - **`.env` changes (operator-side, persistent):** `DASHBOARD_BIND_HOST=127.0.0.1`, `DASHBOARD_HTTPS_MODE=true`. The dashboard now listens only on the loopback interface; Caddy is the only thing facing the public network on ports 80/443.
- **VPS verification facts (2026-06-04, HEAD `383bec0`):**
  - `test_m15_3_a_dashboard_auth.py` 101/101 OK; full regression sweep on VPS all green (`test_m13_4a_allocation` 61/61, `test_m15_3_a_2_totp` 52/52, `test_m15_5_ibkr_exposure` 78/78, `test_m15_4_gateway_health` 50/50, `test_m14_g_dashboard` 51/51, `test_m14_e_engine` 105/105).
  - `algo-trader-dashboard.service` and `caddy.service` both `active`.
  - `ss -ltnp 'sport = :8080'` shows `127.0.0.1:8080` (NOT `0.0.0.0:8080`). External `:8080` is now unreachable from the internet — confirmed during cutover.
  - `https://algotrading.marketwarrior.club/api/health` → HTTP/2 200 with `via: 1.1 Caddy` header.
  - `http://algotrading.marketwarrior.club` → `HTTP/1.1 308 Permanent Redirect` to HTTPS.
  - **Operator authenticated against the dashboard in a real browser session over HTTPS** with password + 6-digit Google Authenticator code — the full chain (HTTPS → Caddy → loopback → dashboard → password verify → TOTP verify → session rotation → CSRF token → state-changing POSTs accepted) works end-to-end.
- **Carry-forward recorded**: operator noted browser login felt slow (~7-10 seconds end-to-end). Not blocking the closeout, but recorded as a performance follow-up under `M15.3.A.cutover.perf` in `NEXT_WORK_REGISTER.md` to be measured/investigated when convenient.
- **Hard-constraint evidence**: protected files modified across the entire cutover chain (`224e8a3` + `383bec0`) vs `274f12e` (pre-cutover baseline): **0 / 24**. No trading code, scanner, strategy, M14 engine/governor/snapshot/preflight, eToro, IBKR-reader, broker, order-path, live-mode, sync.sh, deploy.sh, or systemd-unit changes. Caddy is a new systemd service installed via OS package (not a project-owned unit) and runs outside the algotrading repo.
- **Authoritative operator reference**: [`docs/M15_3_A_dashboard_auth.md`](docs/M15_3_A_dashboard_auth.md) §3 (operator runbook for the Caddy/TLS install procedure) + §13 (cutover closeout evidence). Caddyfile content lives at `/etc/caddy/Caddyfile` on the VPS; a documented mirror could be added to `infra/caddy/` as a future drift-reference (not done today).
- **Honest residual exposure (documented, not blocking):**
  - The dashboard's session-cookie `Secure` flag is now ON. Plain-HTTP access via the VPS IP would not receive the cookie — operator MUST use `https://algotrading.marketwarrior.club`. Bookmarks pointing at `http://138.199.196.95:8080` are dead by design.
  - Caddy auto-renews the TLS cert; manual renewal is never required. Cert expiry is monitored by Caddy internally.
  - The TOTP defence against credential theft is now stacked on TLS defence against on-path session-cookie theft. The two layers protect different attack surfaces.

**M15.3.B closeout (`manual_reset` operator flow)** — VPS-verified 2026-06-04:
- **Status:** CLOSED. Implementation chain: pre-code Q-style checklist Q-B.1..Q-B.10 + Corrections C1..C4 + Implementation corrections 1..10 all approved by operator → single implementation commit `2f55f1d` (no follow-up fixes required during VPS verification).
- **Purpose:** Operator-initiated mechanism to clear the M13.4A allocation-policy kill switches (`policy.global/<broker>.kill_switch`) — the safety locks that gate the M14 Risk Authority Engine. Until M15.3.B, the only recovery path was hand-editing the M13.4A allocation JSON; M15.3.B formalises it with the full M15.3 defensive stack: auth + CSRF + step-up TOTP + 60s preview-then-execute + 10-500 char operator reason + 3/hour rate limit + dual atomic audit. **This is different from `bot/kill_switch.py` (`data/kill_switch.json`)** — that file-based emergency-stop is unchanged by `manual_reset`; the two safety mechanisms are independent.
- **Design-intent disclosure (operator Correction C4, recorded in `docs/M15_3_B_manual_reset.md` §1):** `manual_reset` itself does NOT trade, call brokers, place/cancel/modify orders, or close positions. However, **the purpose of clearing the locks is exactly to allow the M14 engine to resume normal operation** under its existing gating logic. After a successful `manual_reset`, the engine's next decision cycle re-evaluates authority based on the new policy state. Live-trading risk is currently negligible (live IBKR account unfunded, scanner in shadow-mode) but this must be understood before the live path matures.
- **What shipped (1 commit, 10 files, +2902/-11 lines):**
  - **`dashboard/auth/manual_reset.py` (NEW, ~370 LOC)** — pure-logic primitives: `PreviewTokenStore` (session-bound, 60s TTL, single-use), `make_manual_reset_limiter` factory (3/3600s/3600s), `read_kill_switch_state`, `prepare_cleared_policy`, `verify_step_up_totp` (exposes ONLY `hint='recently_used'` per Correction C1), `validate_reason` (10-500 chars), `validate_confirm` ("RESET" exact), `execute_atomic_reset` (BEGIN IMMEDIATE / 3 writes / COMMIT or ROLLBACK), and 4 closed-schema audit-extras builders. No broker / scanner / strategy / engine imports (AST-asserted by G10).
  - **`dashboard/app.py` (extended, +486 LOC)** — `GET /api/manual-reset/preview` + `POST /api/manual-reset` endpoints + Recovery nav link + minimal Recovery page UI + `loadRecovery()` + `executeRecovery()` JS handlers. Session-binding for the preview token uses a stable per-session nonce stored INSIDE the Flask session (not the raw cookie bytes, which Flask re-signs between requests — a bug caught and fixed during sandbox smoke testing).
  - **`bot/risk_authority/audit_decisions.py` (extended additively, +133 LOC)** — one new function `write_manual_reset_decision()` that writes a single `risk_decisions` row with `source='manual_reset'`, `broker_scope='GLOBAL'`, `requested_action='query_authority'`, `result='allow'`, `authority_before='OFF'`, `authority_after='OFF'`, `snapshot_id=NULL` (operator action, not engine eval), `actor='operator'`, and a human-readable `explainer` including the operator's reason text. All 7 pre-existing functions (`decide_and_audit`, `write_snapshot`, `write_decision`, `_redact`, `_scope_view_to_dict`, `_serialize_snapshot`, `_freshness_summary`) byte-identical to baseline (asserted by G11 `test_audit_decisions_only_additive_change`).
  - **`dashboard/auth/audit.py` (extended, +5 lines)** — 4 new closed kinds added to `ALLOWED_KINDS`: `manual_reset_preview`, `manual_reset_attempt` (always written FIRST), `manual_reset_success`, `manual_reset_failure`.
  - **`test_m15_3_b_manual_reset.py` (NEW, 51 tests across 12 groups G1..G12)** — endpoint auth + preview + confirm-string + step-up TOTP + reason field + kill-switch clearing (incl. idempotent C2) + audit writes (auth_events + risk_decisions + secret-material invariant sweep) + atomicity (rollback) + rate limit + no-broker AST scan + protected-files diff + additive-only proof + ALLOWED_KINDS registration.
  - **`docs/M15_3_B_manual_reset.md` (NEW, ~290 LOC)** — full operator runbook: §1 purpose+design-intent (C4), §2 mutations, §3 explicit non-targets, §4 endpoint surface, §5 TOTP error UX (C1), §6 dual audit, §7 atomicity, §8 rate limit, §9 implementation files, §10 test suite mapping, §11 VPS deploy + verification command + 11-step browser walkthrough, §12 honest residual.
  - **`docs/NEXT_WORK_REGISTER.md`** — M15.3.B entry: `PENDING` → `IMPLEMENTATION LANDED` → `CLOSED`.
  - **Three older test files** (`test_m15_3_a_dashboard_auth.py`, `test_m15_3_a_2_totp.py`, `test_m15_5_ibkr_exposure.py`) — `bot/risk_authority/audit_decisions.py` removed from their PROTECTED tuple with a comment pointing to M15.3.B's additive-only check. Necessary because the operator-approved M15.3.B additive extension would otherwise fail those tests' "any change vs my baseline" diff. Each diff is exactly **one line removed + a docstring/comment note** — no test logic touched.
- **Operator-approved Q-B answers + Corrections honoured:**
  - **Q-B.1** Option A — kill-switch clear only; no cache invalidation, no `daily_state` mutations, no other side effects.
  - **Q-B.2** Explicit exclusions confirmed: `candidate_snapshots`, strategy params, positions, exposure rows, historical audit rows.
  - **Q-B.4** Option A — GET preview + POST execute + Recovery tab UI.
  - **Q-B.5** Three confirmations: typed `RESET` + 60s session-bound single-use preview token + step-up TOTP. No secrets in token (cryptographic random nonce).
  - **Q-B.6** Fresh step-up TOTP required at execution time.
  - **Q-B.7** Dual audit: `auth_events` (operator/security, 4 new closed kinds) + `risk_decisions` (M14 Risk Authority, `source='manual_reset'`). Secret-material blacklist asserted by G7. CSRF-rejected requests NOT audited (decorator rejects before endpoint body runs).
  - **Q-B.8** Single `BEGIN IMMEDIATE` transaction. Three writes (policy upsert + `risk_decisions` row + `manual_reset_success` row) succeed together or none do. The `manual_reset_attempt` and `manual_reset_failure` rows are OUTSIDE the transaction so failed-attempt evidence survives rollback.
  - **Q-B.9** Tight rate limit 3 attempts / 60min window / 60min lockout per client IP. Preview GET NOT counted.
  - **Q-B.10** AST scan: `dashboard/auth/manual_reset.py` + `write_manual_reset_decision` body + endpoint function bodies contain no broker imports + no broker method names.
  - **C1 — TOTP error UX**: API exposes ONLY `hint='recently_used'` for replay; wrong/malformed/expired/missing all return generic `{ok:false,error:'totp_invalid'}` with no hint.
  - **C2 — Idempotent**: empty `switches_cleared` still writes the `attempt` + `success` + `risk_decisions` rows (response carries `noop=true`).
  - **C3 — Reason 10-500 chars** with UI helper text deterring secret-pasting; no aggressive server-side regex.
  - **C4 — Design-intent disclosure** recorded prominently in §1 of the runbook.
  - **Impl Correction 9** — VPS verify uses `git fetch origin main + git reset --hard origin/main`, NOT `sudo ./sync.sh`.
  - **Impl Correction 10** — Strict protected-files check (G11): 0/24 modified vs `ae8fb0d` baseline.
- **VPS verification facts (2026-06-04, HEAD `2f55f1d`):**
  - **Terminal verification (operator):** `test_m15_3_b_manual_reset.py` 51/51 OK; full regression sweep on VPS all green (`test_m15_3_a_dashboard_auth` 101/101, `test_m15_3_a_2_totp` 52/52, `test_m13_4a_allocation` 61/61, `test_m14_e_engine` 105/105, `test_m14_g_dashboard` 51/51, `test_m15_4_gateway_health` 50/50, `test_m15_5_ibkr_exposure` 78/78); `algo-trader-dashboard.service` and `caddy.service` both `active`; `ss -ltnp 'sport = :8080'` still shows `127.0.0.1:8080` only (M15.3.A.cutover bind preserved); `https://algotrading.marketwarrior.club/api/health` returns 200; `git status` clean.
  - **Browser end-to-end verification (operator, real browser session over HTTPS):** Logged in at `https://algotrading.marketwarrior.club` with password + Google Authenticator code. Opened the Recovery / Operator manual_reset section. As a controlled test, set `ibkr.kill_switch=true` via the M13.4A Broker Allocation tab. Opened Recovery → "Load current state": preview showed `etoro.kill_switch=false`, `global.kill_switch=false`, `ibkr.kill_switch=true (locked)`. Entered operator reason "M15.3.B browser verification: clearing test ibkr kill switch after confirming no broker action is performed." Typed `RESET`. Entered fresh Google Authenticator code (distinct from login code — replay cache aged out naturally). Submitted. Browser returned success: `Cleared 1 kill switch(es): ibkr.`, `auth_event_id=38`, `decision_id=mr-3086a40a9b2f46e5`. Browser session over HTTPS → Caddy → loopback → dashboard preserved across the full request chain.
  - **End-to-end chain verified:** preview state read → preview token issuance → CSRF check → confirm-string validation → preview token consume → reason validation → step-up TOTP verification → atomic policy update + dual audit writes → response with before/after state + audit IDs → UI confirms.
- **Hard-constraint evidence:**
  - **Protected files modified vs `ae8fb0d` (pre-M15.3.B baseline): 0 / 23.** `main.py`, scanner, strategy, risk, M14 engine/governor/authority/snapshot/preflight, IBKR-reader, exposure-reading, gateway_health, gateway_watchdog, eToro live broker, all `tools/` write-paths, all `infra/systemd/` unit files, `sync.sh`, `deploy.sh` — every one byte-identical.
  - **`bot/risk_authority/audit_decisions.py` additive-only proven:** 7 baseline functions byte-identical + 1 new function (`write_manual_reset_decision`); 0 functions removed; G11 `test_audit_decisions_only_additive_change` asserts this every test run.
  - **AST scan (G10, 4 tests):** `dashboard/auth/manual_reset.py` imports zero broker libraries (`ib_insync`, `ibapi`, `bot.broker_*`, `bot.gateway_*`, `bot.scanner`, `bot.strategy`, `bot.risk_authority.engine/governor/snapshot/preflight/ibkr_paper_reader`, `bot.etoro.live_broker`); module string literals contain no broker order method names (`placeOrder`, `cancelOrder`, `modifyOrder`, `closePosition`, `submitOrder` + snake_case); the new audit-writer function has zero nested imports; the four manual_reset endpoint function bodies in `dashboard/app.py` contain no broker imports.
  - **Secret-material invariant (G7):** known TOTP secret + known TOTP codes + known password substring-searched across every `manual_reset_*` `extras_json` row + `risk_decisions` `explainer` + `request_json` + `recovery_paths` → zero matches.
  - **TOTP error UX (G4, 6 tests):** missing → 401 generic no hint; wrong code → 401 generic no hint; malformed → 401 generic no hint; empty → 401 generic no hint; replay → 401 + `hint='recently_used'`; valid fresh code → 200; no-secret-configured → hard refusal.
- **Authoritative operator reference**: [`docs/M15_3_B_manual_reset.md`](docs/M15_3_B_manual_reset.md). The runbook includes §11 VPS verification command (using `git fetch origin main + git reset --hard origin/main`, NOT `sudo ./sync.sh`) and an 11-step browser walkthrough.
- **Honest residual exposure (documented in runbook §12, not blocking):**
  - **Rate-limit storage is in-memory.** Same trade-off as M15.3.A's login limiter. A dashboard restart resets the limiter. Acceptable; revisit only on a real abuse incident.
  - **TOTP replay cache is in-memory.** A dashboard restart clears it. Same trade-off as M15.3.A.2.
  - **No multi-user roles.** Single-operator model preserved per the operator's "no multi-user work" constraint. All audit rows record `actor='operator'` rather than a specific user; if multiple operators ever co-administer, that field would need to expand.
  - **The endpoint does NOT cancel orders, close positions, or restart services.** It clears policy flags only. Anything else must be done via existing surfaces or at the broker directly.

**M15.3.C closeout (Compliance audit + export)** — VPS-verified 2026-06-05:
- **Status:** CLOSED. Implementation chain: pre-code Q-style checklist Q-C.1..Q-C.12 + ChatGPT formal review approved → initial implementation commit `0018c32` → rate-limit fix `02b5dcf` (ExportAttemptLimiter to honour the approved "every authenticated attempt counts" semantics, which the initial implementation had divergent from the checklist by deferring to the shared M15.3.A/B RateLimiter's failure-only counting). Pre-code corrections Q-C.1..Q-C.12 + the post-review C-α correction (strict "every attempt counts" rate-limit) all honoured.
- **Purpose:** Operator-initiated compliance-friendly export of the M15.3 audit trail. Reads two streams in one download:
  - all `auth_events` rows (the full M15.3 operator/security audit history: M15.3.A login/session/CSRF + M15.3.A.2 TOTP + M15.3.B `manual_reset_*` + M15.3.C's own `audit_export_request` meta-audit rows)
  - `risk_decisions` rows with **`source='manual_reset'` only** (the M14-side half of M15.3.B's dual-audit; other `risk_decisions` rows with `source IN ('auto','manual','reconciled')` are EXCLUDED per Q-C.1 as operational/risk-engine audit rather than security/operator audit).

  Two formats from one query: `jsonl` (default, high-fidelity, structured `extras_json` preserved as nested objects, manifest line first then audit rows one-per-line) and `csv` (ZIP containing `manifest.txt` + `auth_events.csv` + `risk_decisions_manual_reset.csv`, RFC-4180-quoted, opens in Excel/LibreOffice).
- **Design-intent disclosure (operator Correction Q-C.7, recorded in `docs/M15_3_C_audit_export.md` §1):** The endpoint is read-only with respect to all trading and account state. The **only** write it performs is a single `audit_export_request` row in `auth_events` — the meta-audit-of-the-audit. No broker calls, no order calls, no live-trading actions, no scanner/strategy changes, no M14 engine/governor/snapshot/preflight changes, no eToro/IBKR adapter changes, no M16 work. AST-asserted in the test suite (G10) + protected-files diff (G11).
- **What shipped (2 commits, 7 files total):**
  - **Commit 1 `0018c32`** — initial implementation:
    - `dashboard/auth/audit_export.py` (NEW, ~530 LOC) — pure-logic primitives: `validate_date_range` (UTC inclusive day windows, malformed → `date_format_invalid`, reversed → `date_range_invalid`), `count_export_rows` (with strict `source='manual_reset'` filter per Q-C.1), `read_auth_events_range`, `read_risk_decisions_manual_reset_range`, `build_jsonl_export` (spool-to-bytes-then-SHA-256 per Q-C.3 honest correction), `build_csv_zip_export` (ZIP_DEFLATED, `csv.QUOTE_MINIMAL`), `scan_for_secrets` (env-keyed + literal `otpauth://` + PEM headers; ≥12-char threshold to skip false positives), `build_manifest`, `make_download_filename` (`audit_export_<YYYYMMDDTHHMMSSZ>.{jsonl|zip}` — no secrets in filenames).
    - `dashboard/auth/audit.py` (extended, +7 lines) — `audit_export_request` added as the 18th `ALLOWED_KINDS` value.
    - `dashboard/app.py` (extended, +302 lines) — `m153c_audit_export()` endpoint + minimal Audit Export card on the Recovery page (date pickers + format selector + Download button, ~120 LOC HTML/JS).
    - `test_m15_3_c_audit_export.py` (NEW, 32 tests / 12 groups) — full coverage.
    - `docs/M15_3_C_audit_export.md` (NEW, ~280 LOC) — full operator runbook.
  - **Commit 2 `02b5dcf`** — ChatGPT-review rate-limit fix:
    - `dashboard/auth/audit_export.py` (extended, +148/-25) — new `ExportAttemptLimiter` class (sliding window, counts every attempt regardless of outcome, per-IP, thread-safe via `threading.Lock`). Old constants `EXPORT_RATE_LIMIT_THRESHOLD`/`_LOCKOUT_SEC` dropped; replaced with `EXPORT_RATE_LIMIT_MAX_PER_WINDOW=10` and `EXPORT_RATE_LIMIT_WINDOW_SEC=3600`. Factory `make_export_limiter()` now returns the new class instead of `dashboard.auth.rate_limit.RateLimiter`.
    - `dashboard/app.py` (rewrite of endpoint rate-limit block, +21/-32) — single `limiter.check_and_record(client_ip)` at the top replaces `check_locked()` + scattered `record_failure()` calls. Dead `record_failure()` calls removed from all four validation paths.
    - `test_m15_3_c_audit_export.py` (extended) — `TestExportRateLimit` expanded from 1 test to 6 covering all 5 acceptance criteria (10 successes allowed, 11th → 429, mixed-outcome attempts count too, 429 writes meta-audit row, no secrets in 429 response or extras) + unit test of the limiter class itself.
    - `docs/M15_3_C_audit_export.md` — §3 rate-limit semantics rewritten; §8 test count 32→37; §10 honest-residual entry about "only counts failures" removed; new entries added about rejected-attempt write amplification and per-process limiter scope.
- **Operator-approved Q-C answers + corrections honoured:**
  - **Q-C.1** — narrow scope: `auth_events` (all rows) + `risk_decisions` with `source='manual_reset'` only. Non-manual_reset risk_decisions EXCLUDED.
  - **Q-C.2** — closed kind set built from the LIVE `dashboard/auth/audit.ALLOWED_KINDS` at module-import time (not from a hard-coded list); operator correction "read the actual current closed set" honoured. After adding `audit_export_request`: 18 kinds.
  - **Q-C.3** — both `jsonl` (default) + `csv`-in-ZIP. Spool-then-SHA-256 (NOT pure streaming; operator-approved trade-off given the 100k row cap).
  - **Q-C.4** — `from`/`to` UTC inclusive full-day windows, default `from=1970-01-01` `to=today`, 100k row cap.
  - **Q-C.5** — fail-fast redaction (do NOT silent-strip); failed export still meta-audited with `success=0`, `reason='redaction_violation'`, labels-only `redaction_violations` array (NEVER the secret value).
  - **Q-C.6** — manifest carries `_schema_version`, `_export_id`, `_generated_at_utc`, `_generated_by_actor`, `_date_range`, `_row_counts`, `_sha256_payload`, `_format`. `_export_id` also written into `audit_export_request.extras_json` for bidirectional traceability.
  - **Q-C.7** — GET endpoint accepted with explicit documentation that the meta-audit row is the only write (recorded in runbook §1).
  - **Q-C.8** — `@require_auth` mandatory; GET so no CSRF; no step-up TOTP (conscious decision documented in runbook §3 — read-only of already-visible data over HTTPS); **rate limit 10 attempts/hour/IP counting EVERY authenticated attempt** (post-review correction; see C-α below).
  - **Q-C.9** — 37 tests across 12 groups; RSS memory-footprint test dropped (operator correction).
  - **Q-C.10** — same `git fetch + git reset --hard origin/main` pattern (NOT `sudo ./sync.sh`).
  - **Q-C.11** — hard constraints all honoured.
  - **Q-C.12** — full runbook + NEXT_WORK_REGISTER update + MILESTONE_STATUS closeout entry (this block).
  - **Post-review correction C-α (ChatGPT review 2026-06-05)** — the initial implementation `0018c32` used the shared M15.3.A/B `RateLimiter` (failure-only counting), which left successful exports effectively unlimited. Fix `02b5dcf` introduced the M15.3.C-local `ExportAttemptLimiter` that counts every authenticated attempt. The shared `RateLimiter` was NOT modified — M15.3.A login and M15.3.B manual_reset rate-limit semantics unchanged.
- **VPS verification facts (2026-06-05, HEAD `02b5dcf`):**
  - **Terminal verification (operator):** `git rev-parse --short HEAD` = `02b5dcf`. `test_m15_3_c_audit_export.py` 37/37 OK. Full regression sweep all green: `test_m15_3_b_manual_reset` 51/51, `test_m15_3_a_dashboard_auth` 101/101, `test_m15_3_a_2_totp` 52/52, `test_m13_4a_allocation` 61/61, `test_m14_e_engine` 105/105, `test_m14_g_dashboard` 51/51, `test_m15_4_gateway_health` 50/50, `test_m15_5_ibkr_exposure` 78/78. `algo-trader-dashboard.service` and `caddy.service` both `active`. `ss -ltnp 'sport = :8080'` shows `127.0.0.1:8080` only (M15.3.A.cutover bind preserved). Caddy listening on `*:80` + `*:443`. `https://algotrading.marketwarrior.club/api/health` returns HTTP 200. Unauthenticated `GET /api/audit-export?format=jsonl` returns HTTP 401 (expected — `@require_auth` enforced). `git status` clean.
  - **Browser end-to-end verification (operator, real session over HTTPS via Caddy → loopback → dashboard):** Logged in successfully with password + Google Authenticator. Opened Recovery → Audit Export (M15.3.C). **Downloaded JSONL** at `audit_export_20260605T094145Z.jsonl`: manifest parsed, `_schema_version=1`, `_format=jsonl`, `_row_counts={auth_events:48, risk_decisions_manual_reset:1}`, payload SHA-256 verified, only `auth_events` and `risk_decisions_manual_reset` appear as `_source` values (Q-C.1 scope holds at runtime). **Downloaded CSV ZIP** at `audit_export_20260605T094158Z.zip`: contains exactly `manifest.txt` + `auth_events.csv` + `risk_decisions_manual_reset.csv`; `auth_events.csv` has 50 data rows; `risk_decisions_manual_reset.csv` has 1 data row.
  - **Notable confirmation of meta-audit chain:** the CSV's auth_events.csv has 2 more rows than the JSONL's auth_events count (50 vs 48). The operator correctly identified this as the meta-audit chain working as designed: the JSONL export itself wrote one `audit_export_request` row, then a subsequent date-filter validation attempt (during testing) wrote another. The CSV download then captured both of those. This is exactly the bidirectional-traceability behaviour intended by Q-C.6 — every export attempt that reaches the endpoint creates a permanent audit footprint visible in the next export.
  - **End-to-end chain verified:** rate-limit check → format validation → date validation → row-count cap check → body build (spool + SHA-256) → redaction scan → meta-audit success row write → file download response with `X-Export-Id` + `X-Export-Sha256` headers + correct `Content-Disposition` filename.
- **Hard-constraint evidence (across the full M15.3.C commit chain `0018c32` + `02b5dcf`):**
  - **Protected files modified vs `384e484` (M15.3.B closeout baseline):** 0 / 24. `main.py`, scanner, strategy, risk, M14 engine/governor/authority/snapshot/preflight, IBKR-reader, exposure-reading, gateway_health, gateway_watchdog, eToro live broker, all `tools/` write-paths, all `infra/systemd/` unit files, `sync.sh`, `deploy.sh`, `dashboard/auth/manual_reset.py` (M15.3.B's helper module, now frozen) — every one byte-identical.
  - **AST scan (G10, 3 tests):** `dashboard/auth/audit_export.py` imports zero broker libraries (`ib_insync`, `ibapi`, `bot.broker_*`, `bot.gateway_*`, `bot.scanner`, `bot.strategy`, `bot.risk_authority.*`, `bot.etoro.*`); module imports are stdlib (`collections`, `csv`, `hashlib`, `io`, `json`, `logging`, `os`, `re`, `sqlite3`, `threading`, `time`, `typing`, `uuid`, `zipfile`, `datetime`) + `dashboard.auth.audit` only. Module string literals contain no broker order method names. The endpoint function body in `dashboard/app.py` contains no broker imports.
  - **Audit invariant (closed-kind set + secret-material sweep, G7 + G12):** `audit_export_request` registered as the 18th `ALLOWED_KINDS` value; runtime snapshot in `audit_export.py` matches the live set; redaction-violation paths verified not to leak the secret value into the 429 response body, audit row `extras_json`, or log lines.
  - **Rate-limit semantics asserted by G9 (6 tests):** 10 successful exports allowed; 11th valid attempt → 429 with `retry_after_sec ∈ [1, 3600]`; mixed-outcome (5 success + 3 format-invalid + 2 date-invalid) attempts also count toward cap; rate-limited attempt writes `audit_export_request` `success=0` `reason='rate_limited'` row; no env-keyed secret (`DASHBOARD_TOTP_SECRET`, `DASHBOARD_PASSWORD_HASH`, `IBKR_API_KEY`, `ETORO_USER_KEY` set with known long values) appears in 429 response or any `audit_export_request` extras; unit-level `ExportAttemptLimiter` sliding-window age-out + per-IP isolation verified.
- **Authoritative operator reference:** [`docs/M15_3_C_audit_export.md`](docs/M15_3_C_audit_export.md). The runbook includes §1 purpose+scope+design-intent, §2 mutations, §3 endpoint surface (auth, CSRF, TOTP, rate limit), §4 export format spec, §5 redaction rules, §6 self-audit, §7 implementation files, §8 test suite, §9 VPS deploy + verification, §10 honest residual, §11 closeout evidence (added in this docs-only commit).
- **Honest residual exposure (documented in runbook §10, not blocking):**
  - **No cryptographic signing of exports.** The SHA-256 in the manifest is integrity (was the file modified after export), not provenance (did the dashboard generate this). Adding HMAC/sig would require a server-side key plus a published verification step; out of scope.
  - **`_generated_by_actor` is hard-coded to `'operator'`** — single-user model preserved per the M15.3 single-operator constraint. M15.3.D-or-later would extend.
  - **Rate-limit + replay caches are in-memory and per-process.** A dashboard restart resets them. Same trade-off as M15.3.A/B. The dashboard runs single-worker; if multi-worker is introduced later, the `ExportAttemptLimiter` would need a shared store.
  - **Rejected-attempt audit-row amplification.** Every 429 response writes one `audit_export_request` row. An authenticated attacker bursting requests after hitting the cap would generate one audit row per request. This is *intentional* — every attempted access is logged — but it bounds the per-day audit-table growth at "attacker request rate × 24h". Acceptable: the attacker must already have valid password + TOTP to reach this endpoint.
  - **CSV is UTF-8 without BOM.** Excel on Windows in some locales defaults to CP1252; characters in `extras_json` may render badly without manual encoding setup. LibreOffice handles UTF-8 cleanly. Not changed speculatively.
  - **`extras_json` schema is open within each `kind`.** The closed-set test asserts `kind` values, but the JSON inside `extras_json` can contain arbitrary keys per kind. M15.3.A/A.2/B are disciplined about the shape (and the audit invariants prove no secrets leak there), but formal extras schemas were not adopted in M15.3.

**M15.3 FINAL CLOSEOUT (entire M15.3 sub-milestone tree complete, 2026-06-05):**
- All seven M15.3 sub-milestones CLOSED:
  - M15.3.A — Dashboard auth (CLOSED 2026-06-03)
  - M15.3.A.2 — Dashboard TOTP / Google Authenticator 2FA (CLOSED 2026-06-04)
  - M15.3.A.cutover — Caddy/TLS + 127.0.0.1 bind (CLOSED 2026-06-04)
  - M15.3.B — `manual_reset` operator flow (CLOSED 2026-06-04)
  - M15.3.C — Compliance audit + export (CLOSED 2026-06-05)
- Carry-forwards that remain DEFERRED (not blocking M15 closure):
  - `M15.3.A.cutover.perf` — dashboard login latency follow-up (~7-10s end-to-end; non-blocking; investigate when convenient)
  - `M15.3.A.persist` — DB-backed rate-limit persistence (in-memory variant is the approved trade-off; only revisit on real abuse incident)
  - `M15.3.D or later` — Multi-user / read-only dashboard roles (DEFERRED INDEFINITELY per the post-M15 strategic direction — not safety- or compliance-driven, single-operator model retained)

**M15 FINAL CLOSEOUT (entire Production Hardening milestone complete, 2026-06-05):**
- All M15 sub-milestones CLOSED:
  - M15.0-pre — Live IBKR account onboarding (CLOSED earlier)
  - M15.0 — Process-manager identification (CLOSED earlier)
  - M15.1 — Gateway watchdog + heartbeat thread + external health endpoint (CLOSED earlier)
  - M15.2 — Schema hardening (CLOSED earlier)
  - M15.4 — IB Gateway visibility/truth layer (CLOSED 2026-06-02)
  - M15.5 — IBKR paper exposure wiring (CLOSED earlier)
  - M15.3.A / .A.2 / .A.cutover / .B / .C — Dashboard auth + 2FA + TLS + manual_reset + audit export (all CLOSED, see above)
**M15 is now fully CLOSED.** **M16 is now fully CLOSED.** **The M1–M16 audit-only pass is now CLOSED** (P0 batch verified 2026-06-05, commit chain `655c955` → `268a50b`; see "P0 audit batch" block below). The next concrete work item is operator-chosen; **M17 has not started**. Dashboard work stops unless safety- or compliance-driven.

---

### Milestone 16 — Historical Data + First Signal Engine (CLOSED 2026-06-05)

**M16.A + M16.B + four small follow-up fixes, all VPS-verified.**

Real provider fetch → Parquet write → SQLite coverage update → local `get_bars()` read → SMA local-read proof, all working end-to-end with honest reporting at every failure mode.

**Commit chain on `origin/main`:**
- `c6e98b7` — M16.A: historical data engine + M16.B local-read proof
- `af96eda` — M16.A.fix-1: honest rate-limit classification (was silently `no_data`)
- `c5702f1` — M16.A.fix-2: `cmd_status` auto-migrates v1 DB + clean stale docstrings
- `cc979aa` — M16.A.fix-3: `/api/historical/status` auto-migrates v1 DB
- `aef8335` — M16.A.fix-4: freshness-aware incremental no-op + clean remaining docstrings

**Architecture (per approved pre-code checklist + 7 ChatGPT corrections):**
- Package `bot/historical/` (renamed mid-flight from `bot/data/` after collision with the existing M6 `bot/data.py` provider-delegation module; `bot/data.py` byte-identical to baseline, protected-files invariant held).
- Hybrid storage: SQLite metadata at `data/historical.db` (separate from `signals.db`) + Parquet bars at `data/historical/<provider>/<timeframe>/<symbol>.parquet`. Provider in path per Correction D-δ.
- 6 SQLite tables (`historical_schema_version`, `historical_symbols`, `historical_coverage`, `historical_refresh_runs`, `historical_quality_events`, `historical_refresh_lock`). Schema version 2 (v1 → v2 added `symbols_rate_limited` for honest rate-limit classification).
- Raw OHLC + `adj_close` + `adjustment_ratio` + `is_adjusted` columns (Correction 2) — uniform-ratio adjustment approximation documented honestly in `docs/M16_historical_data.md` §C.
- 4H timeframe resampled at write time from 1H, with explicit `source_timeframe`/`derivation_method`/`resample_rule_version` metadata in coverage (D-α).
- SQLite-table-based cross-process refresh lock with PID-aliveness probe + 30-minute lease (Correction 1).
- One public read façade `bot.historical.store.get_bars(...)`; one write orchestrator `bot.historical.refresh.run(...)`.
- 4 refresh modes (backfill / incremental / repair / force_rebuild). Incremental is provider-free when coverage is already fresh (fix-4).
- 5 hard-reject quality rules + 4 warn-tag rules + bitwise `quality_flags` Parquet column.
- 3 dashboard GET endpoints (`/api/historical/status`, `/coverage`, `/quality-events`). No POST refresh per Correction D-ε.
- pyarrow pinned `>=24,<25` in `requirements.txt`; clean-install verified.
- `data/symbol_universe.csv` (10-symbol V1 list AAPL,MSFT,GOOGL,AMZN,META,NVDA,TSLA,JPM,WMT,KO) git-tracked intentionally; all other `data/historical/*` runtime artifacts ignored.

**VPS evidence (2026-06-05, fix-4 acceptance):**
- HEAD = `aef8335` (= expected)
- `requirements.txt` install exit code 0; pyarrow = 24.0.0
- M16 suite: **70/70 OK, 1 skipped** (skipped is the live-yfinance smoke gated on `M16_LIVE=1`)
- Schema migration verified: `schema_version = 2`, `symbols_rate_limited` column present
- Live AAPL 1D backfill (fix-3 run): `status=ok`, `symbols_ok=1`, `bars_fetched=11462`, `bars_written=11462`, **real Parquet file on disk at `/opt/algo-trader/data/historical/yfinance/1D/AAPL.parquet` = 571,139 bytes**
- Local read proof: `get_bars` returns 11,462 rows, `freshness_status=fresh`, `last_ts_utc=2026-06-05`, SMA(20) returned 5 trailing values
- Fix-4 acceptance (fresh incremental no-op): `status=ok`, `symbols_attempted=1`, `symbols_ok=1`, `no_data=0`, `failed=0`, `rate_limited=0`, `bars_fetched=0`, `bars_written=0`, `bars_updated=0`, `duration_sec=0.01`, no provider call, no banner
- DB run log: `run_id 7 = incremental|ok|1|1|0|0|0|0|0` (fix-4 no-op), `run_id 4 = backfill|ok|1|1|0|0|0|0|11462` (fix-3 real fetch)
- Production: dashboard active, caddy active, HTTPS `/api/health` = 200, git status clean

**M16 proves end-to-end:**
1. Real yfinance provider fetch
2. Atomic Parquet write (temp → validate → rename)
3. SQLite coverage update
4. Local `get_bars()` read with no network
5. SMA local-read proof (M16.B capability gate)
6. Honest rate-limit classification (fix-1: no more silent `no_data` masquerade)
7. CLI status migration safety (fix-2)
8. Dashboard status migration safety (fix-3)
9. Freshness-aware incremental no-op (fix-4): back-to-back incremental never hits the provider when coverage is fresh
10. No generated runtime data ever committed to git

**Hard constraints upheld across all five commits:**
- Protected files vs `ceb8cd5`: 0/20 modified (every commit)
- `bot/data.py` sha256: byte-identical to baseline
- AST scan over `bot/historical/`: 0 broker / order / scanner / strategy imports
- Regression sweep: 1,183 tests across 30 suites, 0 failed
- Git-tracked under `data/`: only the 2 intentional CSVs (`symbol_metadata.csv` pre-existing + `symbol_universe.csv` for V1 universe)

**Authoritative operator reference:** [`docs/M16_historical_data.md`](docs/M16_historical_data.md).

**Known semantic trade-off (documented in M16 runbook §O):**
- Freshness-aware incremental no-op (fix-4) means a split that lands within the freshness window AND triggers yfinance to retroactively rewrite Adj Close in the same window will not be detected by an incremental within that window. The next incremental past the window catches it via the existing `split_detected` path. For US equities at 1D granularity, splits are rare enough and yfinance's history-rewrite latency is long enough (days) that this is acceptable. An operator who suspects a split can force a check via `force-rebuild`.

**Open known limitations carry-forward to future audit/M17+:**
- Live multi-symbol backfill at scale remains rate-limit-prone against Yahoo from the VPS IP; the engine reports rate-limits honestly but the operator may need to stagger/reduce/wait. A paid provider behind the same `BaseProvider` contract is a one-file future addition.
- 4H ↔ 1H consistency edge case: if 1H was just updated but 4H is "fresh" per its own threshold, 4H is not automatically re-resampled. Tracked as a future enhancement.

---

### Post-M16 next step — audit-only pass (CLOSED 2026-06-05)

Per operator instruction recorded at M16 closeout (2026-06-05):

**Before any M17 coding starts**, an audit-only pass over M1–M16 from the actual code was conducted. Two independent inspections (this assistant + ChatGPT) produced findings lists; the lists were compared; fix-priority decisions were made jointly. **No code changes during the audit-only phase itself.**

The audit-only pass produced 5 P0 (must-fix) findings + a P1/P2/P3 backlog. **All 5 P0 findings are now landed, regression-green, pushed, and VPS-verified 2026-06-05.** See the "P0 audit batch" block above for the 6-commit chain `655c955` → `6a04735` → `7e83415` → `a072032` → `0b4bf69` → `268a50b` and the full VPS evidence.

The P1 / P2 / P3 backlog **remains open** and is tracked entry-by-entry in [`docs/NEXT_WORK_REGISTER.md`](docs/NEXT_WORK_REGISTER.md) under the `audit-P1-*`, `audit-P2-*`, `audit-P3-*` entries.

This is recorded here to make the deviation from the original roadmap explicit. **M17 (originally "Outcome Learning Loop", and now per the M15-restructure "Backtesting + parameter rules") has not started.** The next concrete work item is operator-chosen.

**M15.3 deferred items** (carry-forwards, all explicitly NOT blocking M15 closure):
- **Dashboard login latency follow-up** — `M15.3.A.cutover.perf`, non-blocking. Operator observed ~7-10s end-to-end browser login; expected closer to 1-2s (~250ms bcrypt + minimal Caddy overhead). Investigate when convenient.
- **Multi-user / read-only dashboard roles** — `M15.3.D or later`, explicitly DEFERRED indefinitely under the post-M15 direction (not safety-critical; single-operator model retained).
- **DB-backed rate-limit persistence** — `M15.3.A.persist`, DEFERRED (in-memory variant is the approved trade-off; only revisit on real incident).

**Post-M15 strategic direction (recorded 2026-06-04 on closeout of M15.3.A.cutover; reaffirmed 2026-06-05 on closeout of M15.3.C and full M15):**
**M15 is fully CLOSED as of 2026-06-05.** Dashboard work now stops unless safety- or compliance-driven. The priority shifts to advanced trading-bot intelligence: historical data → strategy criteria & parameters → backtesting → signal scoring → paper-trade automation → optimisation → controlled live trading → fully autonomous. Concrete near-term timelines:
- **M16 — Historical data + first signal engine**: 3-7 days.
- **M17 — Backtesting + parameter rules**: 1-2 weeks.
- **M18 — Advanced signal scoring + paper-trade automation**: 2-4 weeks.
- **Controlled live trading readiness**: 2-3+ months minimum.
- **Fully autonomous advanced live bot**: 3-6+ months.

Detailed breakdown in [`ROADMAP.md`](ROADMAP.md) (M16+ section restructured 2026-06-04 to match this direction).

**M15.4 closeout (IB Gateway visibility/truth layer)** — VPS-verified 2026-06-02:
- New read-only helper `bot/gateway_health.py` combines five sources (`systemctl is-active/is-enabled/show`, TCP connect-and-close probe on 4001/4002, trading-mode discovery from `start_ibgateway.sh` + IBC config, `/var/log/ibgateway/ibgateway.log` tail, `journalctl -u ibgateway.service`) into a single point-in-time classification.
- New read-only endpoint `GET /api/gateway/health`. **Auth-protected** — unauthenticated requests return HTTP 401, exactly as expected for the dashboard's `@require_auth` model (confirmed on the VPS after dashboard restart). The existing M15.1 `/api/gateway/state` historical-events endpoint is preserved unchanged.
- Live VPS classification on the day of closeout: `ibgateway.service` reports active/enabled, but **no listener on either 4001 or 4002**, and the gateway log shows a `Unrecognized Username or Password` style entry. The truth layer therefore classifies the state as `status = service_active_login_error` and `ready_for_ibkr_trading = False`. This is the headline value of M15.4: systemd "active" is no longer mistaken for "IBKR trading is ready".
- **No IB API call was added.** M15.4 explicitly does not call `reqCurrentTime`, `ib.connect`, `placeOrder`, `cancelOrder`, or any other IB API method; AST-asserted on every commit. The pre-existing M15.1 `bot/gateway_watchdog.py` (which does run a background `reqCurrentTime` ping) is unchanged.
- Authoritative operator reference: [`docs/M15_4_ib_gateway_runbook.md`](docs/M15_4_ib_gateway_runbook.md) — includes status classification table, three known failure-mode recovery procedures, and a drift-detection checklist against the reference mirror at `infra/systemd/ibgateway.service.documented` (mirror is **not** installed by any script).
- Closing M15.4 did NOT itself close the carry-forward of automated IBKR exposure ingestion. That carry-forward was subsequently closed by M15.5 (see the M15.5 closeout block above). The runbook's failure-mode procedures remain authoritative for handling subsequent IB Gateway login outages.

**M15.0 closeout (production process clarity)** — VPS-verified 2026-06-02:
- Canonical systemd units installed and active: `algo-trader.service` (runs `main.py`) and `algo-trader-dashboard.service` (runs `dashboard/app.py`).
- VPS evidence: `main.py` PID owned by `/system.slice/algo-trader.service`; `dashboard/app.py` PID owned by `/system.slice/algo-trader-dashboard.service`; both active/enabled; exactly one of each; `/api/health` returns HTTP 200.
- Rollback snapshot path: `/var/lib/algo-trader/m15_0_snapshots/20260602T210527Z` — use `sudo bash /opt/algo-trader/infra/systemd/rollback.sh /var/lib/algo-trader/m15_0_snapshots/20260602T210527Z` to revert to the pre-install nohup-managed state. Trading state in `signals.db` survives both install and rollback.
- New read-only API endpoint `/api/system/services` reports the canonical service map and live systemd state. **Auth-protected** — unauthenticated requests return `{"error":"Unauthorized"}`; the dashboard's Risk Authority tab and any authenticated curl will see the JSON payload.
- `deploy.sh` and `sync.sh` are now systemd-aware: when canonical units exist + script runs as root, both prefer `systemctl restart` over the legacy `pkill + nohup` path; legacy fallback preserved for pre-install / post-rollback states.
- Authoritative operator reference: [`docs/M15_0_systemd_canonical.md`](docs/M15_0_systemd_canonical.md).

---

**P0 audit batch (M1–M16 audit) — VPS-verified 2026-06-05, CLOSED:**

The M1–M16 audit-only pass identified five P0 (must-fix) findings. All five are
now landed, regression-green, pushed, and VPS-verified. Six commits, in order
on `main`:

| # | Commit | Patch | Summary |
|---|---|---|---|
| 1 | `655c955` | P0-5 | Docs-only: M14 engine vs scanner-path coverage-gap carry-forward (HARD pre-requisite for M22, NOT in M17 scope). |
| 2 | `6a04735` | P0-1 | XFF trusted-proxy fix in `dashboard/auth/trusted_proxy.py`. Login rate-limiter + audit-IP corruption via rotating `X-Forwarded-For` now mitigated; XFF honoured only when `remote_addr` ∈ `DASHBOARD_TRUSTED_PROXIES` (default loopback), and the LAST entry is taken. 26 new tests. |
| 3 | `7e83415` | P0-2 | `IBKRBroker.cancel()` now supports the canonical `IB-PERM-{permId}` format that `submit()` writes; legacy `IB-{orderId}-{tp}-{sl}` preserved. Unknown / malformed / non-string IDs fail-fast with no I/O. 9 new tests. |
| 4 | `a072032` | P0-4 | `PortfolioRiskContext` now populated with `positions`, `open_orders`, `local_open_intents`, `kill_switch_active` via new `bot/portfolio_ctx.py`. Audit Correction B preserved: live path reuses RiskManager's existing reconcile via a single `checks['_recon']` stash — **zero new IBKR round-trips per signal**, AST-asserted. 16 new tests. Touches 2 protected files (`main.py` +23, `bot/risk.py` +6) per the explicitly approved P0 implementation plan. |
| 5 | `0b4bf69` | P0-3 | Runtime M13.4A kill-switch enforcement via new `bot/runtime_policy.py`. All three broker `submit()` paths re-check the broker-allocation policy through a 5s-TTL cache (env-overridable). Audit Correction A preserved: DB read failure with cached policy → use cached + warn; DB read failure with no cache → `REASON_POLICY_UNAVAILABLE` (fail-SAFE, never fail-OPEN). Operator dashboard toggles of global / per-broker kill_switch now take effect without scanner restart. 14 new tests including the headline `test_mid_run_kill_switch_activation_blocks_next_submit`. |
| 6 | `268a50b` | P0-4 fixup | Test-fixture-only follow-up: removes `main.py` and `bot/risk.py` from the `TestProtectedFilesUntouched` PROTECTED tuples in 6 milestone test files (`test_m15_3_a_dashboard_auth`, `test_m15_3_a_2_totp`, `test_m15_3_b_manual_reset`, `test_m15_3_c_audit_export`, `test_m15_5_ibkr_exposure`, `test_m16_historical_data`) and documents the P0-4 exception following the M15.3.B `audit_decisions.py` precedent. No production code touched. |

**Hard-constraint evidence (cumulative across the P0 batch vs `ceb8cd5`):**
- Protected files modified: **2 / 20** (`main.py` +22 lines, `bot/risk.py` +6 lines — both from `a072032` only, both operator-pre-approved).
- `bot/data.py` sha256: byte-identical to baseline.
- AST scan of new modules (`bot/runtime_policy.py`, `bot/portfolio_ctx.py`, `dashboard/auth/trusted_proxy.py`): zero forbidden broker imports.
- New tests: **65 / 65 OK**. Cumulative regression at `268a50b`: **563 tests in 132.984s, OK (skipped=1)** on the VPS.
- No `.env` changes (only the public `.env.example` template gained the `DASHBOARD_TRUSTED_PROXIES` documentation entry in P0-1).
- No service unit changes. No generated data committed (`data/` tracking unchanged: only `symbol_metadata.csv` + `symbol_universe.csv`).

**VPS evidence (operator-verified 2026-06-05):**
- HEAD = `268a50b` (expected = `268a50b`); commit chain since M16 closeout = 6 commits, all present.
- Landed-presence checks pass for all 5 patches plus the fixup.
- Regression sweep on VPS: `Ran 563 tests in 132.984s` / `OK (skipped=1)` / exit code 0.
- Production: `algo-trader-dashboard.service` active, `caddy.service` active, `https://algotrading.marketwarrior.club/api/health` → HTTP 200.
- `git status` clean.

**P1 / P2 / P3 backlog from the audit pass remains OPEN** (except the
data-rate-limit P1 closed by `9994692` — see block below). Recorded in
`docs/NEXT_WORK_REGISTER.md` for carry-forward; see entries
`audit-P1-broker-permId-fallback`, `audit-P1-data-rate-limit-fix` (CLOSED),
`audit-P1-portfolio-ctx-engine-bypass`, `audit-P2-batch`, `audit-P3-batch`.
Each is tracked separately so it cannot be lost when context resets.

**M17 has NOT started.** The audit-only pass is complete; the next concrete
work item is operator-chosen. The M14-extension-to-scanner-path carry-forward
(P0-5) remains a HARD pre-requisite for M22 (Semi-Automated Live Trading) and
is NOT in M17 scope.

---

**audit-P1-data-rate-limit-fix — VPS-verified 2026-06-05, CLOSED:**

First P1 sub-milestone closed after the P0 batch. Single commit on
`main`:

| # | Commit | Patch | Summary |
|---|---|---|---|
| 1 | `9994692` | audit-P1-data-rate-limit-fix | Detect swallowed yfinance rate-limits via `yf.shared._ERRORS` in the OLD provider (`bot/providers/yfinance_provider.py`) and in `bot/backtest.py:_fetch_yf_single`. Mirrors the M16 fix pattern at `bot/historical/providers_yfinance.py`. Plus a strictly-bounded 1-line import repair in `bot/backtest.py` (the module had been unimportable since Milestone 6 — `_browser_session` moved to the provider package at M6 but the import was never updated; discovered mid-task). 7 new helpers (`_is_rate_limit_signal`, `_yf_rate_limit_exc_class`, `_clear_yf_errors`, `_scan_yf_errors_for_rate_limit`, `_scan_yf_errors_for_other_error`, `_is_rate_limit_exception`, `_RATE_LIMIT_TOKENS`). 23 new tests across 4 groups including the smoking-gun `test_fetch_one_detects_rate_limit_in_yf_shared_errors_after_empty_df`. |

**Bug closed:** before this patch, the live scanner silently
misclassified Yahoo rate-limited responses as `no_data` (empty
DataFrame plus error swallowed into `yf.shared._ERRORS`). The
`consec_rl` counter never incremented and the `MAX_CONSEC_RL`
cache-only safety mode never engaged. Operationally the symptom
was scanner output like `[DATA] ... fresh=0 cache=N stale=0 skip=M`
indistinguishable from a thin-volume day. Same shape of bug in
`fetch_bars_range` (backtest_v2 path) and `_fetch_yf_single`
(`bot/backtest.py`) — both used `raise_errors=False`, both
returned `'empty_response'` on swallowed rate-limits with no retry.

**Hard-constraint evidence:**
- Protected files modified by this commit: **0 / 20**.
- Cumulative protected modified vs `ceb8cd5`: **2 / 20** (unchanged
  from P0 batch — `main.py` + `bot/risk.py` from P0-4 `a072032` only).
- `bot/data.py` sha256: byte-identical to baseline.
- AST scan on patched modules: clean.
- No `.env` / service / generated-data changes. No new
  dependencies. No new status codes. Public API signatures
  unchanged (`bot.data.fetch_bars`, `YFinanceProvider.fetch_bars`,
  `YFinanceProvider.fetch_bars_range`, `bot.backtest._fetch_yf_single`
  all unchanged).
- Test results at commit: `test_audit_p1_data_rate_limit.py`
  23/23 OK (new); full unittest sweep all 35 suites OK; targeted
  regression sweep `Ran 206 tests / OK (skipped=1) / exit 0` on
  the VPS.

**VPS evidence (operator-verified 2026-06-05):**
- HEAD = `9994692` (expected = `9994692`).
- All 7 helpers present in `bot/providers/yfinance_provider.py`.
- `_scan_yf_errors_for_rate_limit` present in both call sites
  (provider + backtest).
- `bot/backtest.py` import repair landed; `bot.backtest`
  importable for the first time since M6 closure.
- audit-P1 test suite: `Ran 23 tests / OK / exit 0`.
- Targeted regression: `Ran 206 tests / OK (skipped=1) / exit 0`.
- Production: `algo-trader-dashboard.service` active, `caddy.service`
  active, `https://algotrading.marketwarrior.club/api/health` HTTP 200.
- `git status` clean.

**Side effect (not in scope, not addressed):** `backtest_cli.py`
(the operator-facing CLI runner of `bot.backtest`) was broken on
import since Milestone 6 because of the same `_browser_session`
move. The strict 1-line import repair revives the CLI from broken-
on-import state; the CLI itself was not separately exercised in
this commit and may have other latent issues accumulated since M6.
Any further `backtest_cli.py` work would be a fresh sub-milestone.

**Remaining audit P1 / P2 / P3 backlog stays OPEN** — see
`docs/NEXT_WORK_REGISTER.md` entries `audit-P1-broker-permId-fallback`,
`audit-P1-portfolio-ctx-engine-bypass`, `audit-P2-batch`,
`audit-P3-batch`. M17 has NOT started.

---

### Milestone 17.A — Backtesting Engine Foundation (CLOSED 2026-06-07)

**Single-symbol M16-only backtest engine — strict missing-data semantics, next-open execution with intrabar SL/TP, fees + slippage on round-trip, deterministic artifacts. SMA crossover is the M17.A foundation strategy. `scanner_replica` + multi-timeframe confluence are deferred to M17.B.**

**Commit chain on `origin/main` (14 commits, no squashes, per design decision D10):**
- `5b37194` — M17.A.1: foundation (package skeleton, errors, models, config validation)
- `3d81e3f` — M17.A.2: data loader (strict M16 coverage gate, UTC normalisation)
- `9a71444` — M17.A.3: vectorized indicators (SMA, EMA, RSI, MACD, ATR, Bollinger, volume)
- `7815c97` — M17.A.4: strategy contract + SmaCrossoverStrategy + look-ahead protection
- `dd2470b` — M17.A.5: execution + portfolio + ledger (bar loop, SL/TP, fees, slippage, sizing)
- `2284912` — M17.A.6: metrics (pure (ledger, bars, exec_cfg) → dict)
- `a850ece` — M17.A.7: output (manifest + report + CSV/JSONL artifacts + reproducibility)
- `98988d1` — M17.A.8: runner + CLI (orchestration, example config, golden-path E2E)
- `97e2836` — M17.A.fixup1: pre-Phase-9 quick-inspection fixups (public API + cash-never-negative)
- `e437f79` — M17.A.9: Phase 9 hygiene tests (G10 — AST, protected files, gitignore, no-network)
- `925b79b` — M17.A.fixup2: EOD final equity, round-trip slippage, entry-bar SL/TP eligibility
- `7c7eb97` — M17.A.fixup3: valid M16 refresh commands, manifest schema version, missing-config exit code
- `60cd6c3` — M17.A.fixup4: strict bar-level range check (truncated loaded bars now fail)
- `a05f160` — **M17.A.fixup5: boundary tolerance for non-trading-day start/end (final HEAD)**

**Architecture shipped (per approved pre-code checklist + 5 ChatGPT-review fixup rounds):**
- Package `bot/backtesting/` (canonical name per D1). Single-symbol, M16-only foundation.
- Only `bot/backtesting/data_loader.py` imports `bot.historical`; AST-asserted by G10. Manifest schema version reaches `output.py` via a `M16_SCHEMA_VERSION` re-export in `data_loader.py`, preserving the invariant.
- 4 hard-failure data modes (no coverage / range too narrow / NaN OHLC / duplicate timestamps / quality_status='error'), 2 soft-warning modes (quality_status='warn', freshness non-fresh), plus a 7-day non-trading-day boundary tolerance (codes `boundary_non_trading_start` / `boundary_non_trading_end`) gated on clean coverage. Refresh-command messages emit VALID `bot.historical.cli` invocations (`backfill` / `repair` / `force-rebuild`) with correct plural-vs-singular flag forms per subcommand.
- Execution model (D4): signal at bar i close → entry at bar i+1 open; intrabar SL/TP via high/low including the entry bar; pessimistic SL-first if both touched; gap-aware fills (open beyond stop → fill at open); fees + slippage applied to entry AND exit and round-trip recorded on each `Trade.slippage_paid`; EOD exit at last bar close charges fees and the equity curve's last point is replaced post-fee.
- Fixed-risk sizing with `max_position_pct` cap; zero-size rejected as `sizing_zero` warning rather than producing a bad trade; cash never goes negative due to fees (fixup1).
- Metrics: total return, max drawdown, win rate, profit factor, expectancy, average win/loss, average bars held, exposure-time %, fees and slippage totals, Sharpe and Sortino (NaN under a `MIN_RETURN_SAMPLES_FOR_SHARPE=20` gate), B&H benchmark.
- Filesystem artifacts under `data/backtests/<YYYYMMDDTHHMMSSZ>_<strategy>_<config_hash>/`: `manifest.json` (includes `bot_historical_schema_version`, `engine_version='M17.A.1'`, `config_hash`, `strategy_module_sha256`, `git_head_sha`, runtime metadata), `report.json`, `trades.csv`, `trades.jsonl`, `equity_curve.csv`, `warnings.json`. `data/backtests/` git-ignored by the existing `data/` rule.
- Deterministic reproducibility: identical config + identical M16 fixture → byte-identical `report.json` (asserted by `G9_OutputReproducibility`).
- CLI `python -m bot.backtesting.cli run --config …` exits 0 on success, 2 on missing data with a valid refresh command in stderr, 3 on bad/missing config, 1 on unexpected.
- 140 tests across G1..G10, FutureWarning-clean under warnings-as-errors.

**VPS evidence (2026-06-07, operator-verified at HEAD `a05f160`):**
- HEAD = `a05f160` (= expected)
- M16 + audit-P1 + M17 combined regression: **`Ran 233 tests in 99.798s — OK (skipped=1)`** (70 M16 + 23 audit-P1 + 140 M17; the 1 skip is the pre-existing live-yfinance smoke gated on `M16_LIVE=1`)
- Example backtest exit code = 0
- Run dir created: `data/backtests/20260607T011518Z_sma_crossover_88578b71038d`
- 6 artifacts present: `equity_curve.csv`, `manifest.json`, `report.json`, `trades.csv`, `trades.jsonl`, `warnings.json`
- `manifest.json` contains `"bot_historical_schema_version": 2`
- `warnings.json` = `[]` (clean — example dates 2024-01-02..2024-12-31 are both trading days; boundary path not triggered by the example)
- `bot/data.py` sha256 = `03f488c73feba19a9088b779722ee53515e936f2` (byte-identical to baseline)
- Production: `algo-trader-dashboard.service` active, `caddy.service` active, `https://algotrading.marketwarrior.club/api/health` HTTP 200
- `git status` clean

**Hard-constraint evidence:**
- Protected files modified by M17.A vs `13a3aa4` baseline: **0 / 20**
- Cumulative protected files modified vs `ceb8cd5` (pre-P0 baseline): **2 / 20** (unchanged — `main.py` + `bot/risk.py` from `audit-P0-4` only)
- `bot/data.py`: byte-identical to baseline (sha above)
- AST scan of `bot/backtesting/*`: zero forbidden imports (yfinance / `bot.data` / `bot.providers` / `bot.scanner` / `bot.backtest` / `bot.backtest_v2` / broker_/gateway_/`bot.etoro.live_broker` / `bot.etoro.paper_broker` / `bot.etoro.signal_only_broker` / `bot.risk_authority.engine` / `bot.risk_authority.governor` / `bot.risk_authority.snapshot` / `bot.risk_authority.preflight` / `bot.risk_authority.ibkr_paper_reader` / ibapi / ib_insync / requests / urllib.request / urllib3 / http.client). Asserted by `G10.test_no_forbidden_imports_in_bot_backtesting`.
- `bot.historical` imported by exactly one file in `bot/backtesting/*`: `data_loader.py`. Asserted by `G10.test_only_data_loader_imports_bot_historical`.
- String-literal scan for order-method names (`placeOrder`, `cancelOrder`, etc.) anywhere in `bot/backtesting/*`: 0 occurrences.
- Socket-call scan during a full mocked backtest run: 0 (asserted by `G10.test_no_socket_calls_during_backtest` patching `socket.socket`).
- `data/backtests/` git-ignored: yes.
- No new files outside `bot/backtesting/*`, `configs/backtests/*`, `test_m17_backtesting.py`, `docs/M17_A_closeout.md`, and doc-only updates.
- No `.env`, no service unit files, no generated runtime data, no new dependencies, no `bot/backtest.py` or `bot/backtest_v2.py` modifications.

**M17.A proves end-to-end:**
1. M16 historical bars are the only data source (no yfinance, no `bot.data` provider calls).
2. Coverage gate + bar-level range check enforce strict missing-data semantics with a tight non-trading-day boundary tolerance.
3. Signals translate to executable trades via a next-open + intrabar-SL/TP + EOD model with realistic fee/slippage accounting.
4. Metrics + filesystem artifacts are byte-identical across runs with the same inputs.
5. Operator-actionable error messages include a VALID `bot.historical.cli` refresh command for every hard-fail mode (no `--start`/`--end` flags ever, plural vs singular flag forms correctly per subcommand).
6. The engine cannot reach the network or open a socket during a run; AST + runtime tests guarantee it.
7. No protected files touched; `bot/data.py` byte-identical to baseline; no broker, scanner, strategy-engine, or eToro/IBKR code reached.

**Authoritative operator reference:** [`docs/M17_A_closeout.md`](docs/M17_A_closeout.md).

**Open known limitations (carry-forward to M17.B):**
- `scanner_replica` strategy + multi-timeframe (1D / 4H / 1H / 15m) confluence with `min_valid_tfs ≥ 3`: deferred (this is the M17.B core scope).
- Indicator parity test against `bot.indicators.compute()`: deferred to M17.B (where `scanner_replica` requires it).
- Live-vs-backtest signal-equivalence proof against real `candidate_snapshots` rows: deferred to M17.B.
- ATR-based exits (live scanner uses ATR; M17.A is percentage-only): deferred to M17.B.
- `test_m13_5_reconcile` and `test_m14_risk` errors under `python -m unittest discover` are pre-existing (broken at M17.A baseline `13a3aa4`, same at acceptance HEAD `a05f160`). Both suites pass when run as standalone scripts; their unittest-discovery compatibility is out of M17.A scope. Flagged here so a future audit picks it up.

**Open audit backlog (unchanged from M16 closeout — none closed by M17.A):**
- `audit-P1-broker-permId-fallback` — DEFERRED
- `audit-P1-portfolio-ctx-engine-bypass` — DEFERRED
- `audit-P2-batch` (9 items) — DEFERRED
- `audit-P3-batch` (6 items) — DEFERRED
- `M14-extension-to-scanner-path` — BLOCKER FOR M22 (unrelated to M17.B; M17.B is not blocked on it)
- See `docs/NEXT_WORK_REGISTER.md` for the full active list.

**M17.B has NOT started.** Carry-forward scope recorded in `docs/NEXT_WORK_REGISTER.md` under "M17.B — scanner_replica + multi-timeframe confluence + live equivalence (PROPOSED, AFTER M17.A)".

---

### Milestone 17.B — scanner_replica + Multi-Timeframe Confluence (CLOSED 2026-06-07)

**scanner_replica strategy + multi-timeframe (1D/4H/1H/15m) confluence + indicator parity + ATR-exits-opt-in — built by code on top of M17.A's foundation. No live-module imports inside `bot/backtesting/*`; equivalence proven by synthetic per-rule parity against `bot.scanner.score_timeframe` and `bot.feature_engine.compute_features` to `rtol=1e-9 + atol=1e-8`. Real intraday end-to-end against live AAPL bars remains UNVERIFIED — recorded honestly as a carry-forward, not buried.**

**Commit chain on `origin/main` (8 commits, no squashes, M17.A.D10 discipline preserved):**
- `1b9e3ec` — M17.B.pre-phase: baseline test fix (whitelist M17 docs-closeout files in the G10 `test_no_unexpected_files_added` allowed set; transparent test-only fix; the M17.A docs-closeout VPS verification did NOT re-run unit tests so the whitelist gap slipped through, surfaced here)
- `e45707d` — M17.B.1: indicator parity helpers + expanded AST guard (rsi `mode='sma_gain_loss'`, atr `mode='sma_true_range'`, `vwap_dev`, `bb_pos`; G10 forbidden-import list extended with `bot.scanner`/`bot.strategy`/`bot.feature_engine`/`bot.indicators`/`bot.sentiment`/`bot.flywheel`; `_M17_A_BASELINE_FORBIDDEN` regression asserts the M17.A baseline can't be silently weakened)
- `96ecaff` — M17.B.2: multi-timeframe M16 loader `load_multi_tf_bars` + `MultiTfBars` (strict-per-TF default; PARTIAL mode opt-in with `partial_tf_unavailable` warnings carrying `timeframe` in extras; per-TF warnings re-tagged `[<tf>]` message prefix)
- `16d3006` — M17.B.3: `MultiTimeframeContext` + `SnapshotBar` (anchor enumeration; `searchsorted`-based snapshot lookup, O(log n) per TF; look-ahead-safe; `available_timeframes` filters None/empty entries; perf budget test asserts 6,600 anchors × 4 TFs under 2s on the dev box)
- `9fd7de8` — M17.B.4: `ScannerReplicaStrategy` (`MultiTimeframeStrategy` base; `_score_timeframe_long/_short` and `confluence_min_valid` reproducing live algebra; indicators precomputed once per TF; shorts suppressed since execution layer is long-only; runner extended to detect multi-TF strategies and attach context; config registry expanded to include `scanner_replica`)
- `09586ca` — M17.B.5: ATR-based exits in `ExecutionConfig` (opt-in `stop_mode='atr'` with `stop_atr_mult` + `target_atr_mult`; default `stop_mode='pct'` preserves M17.A byte-identically; `atr_unavailable_at_signal` skip-with-warning path; `atr_stop_above_fill` defensive guard test deliberately skipped — unreachable through valid config)
- `eae0dde` — M17.B.6: `G6_CandidateSnapshotReplay` diagnostic (smoke-only test class; opens `data/signals.db` if present; skip cleanly if absent; per-row attempts to load M16 bars and replay through `scanner_replica`; prints one-line summary; K=0 is accepted-pass per Sharpened Rule #5; no equivalence claim when K=0)
- `3f1079e` — **M17.B.7: `configs/backtests/example_scanner_replica_aapl.json` (live-compatible thresholds inlined; ATR exits configured; narrow 2026-04-08..2026-05-09 date range fitting yfinance 15m retention; `example_sma_aapl.json` unchanged) — FINAL HEAD**

**Architecture shipped:**
- `bot/backtesting/mtf_context.py` — new module providing `MultiTimeframeContext`, `SnapshotBar`, `MtfContextError`. Imports: stdlib + pandas + numpy + `bot.backtesting.errors` only.
- `bot/backtesting/strategy.py` — `MultiTimeframeStrategy` base + `ScannerReplicaStrategy` (registry: `scanner_replica`). Reproduces `bot/scanner.score_timeframe` algebra in code via `_score_timeframe_long/_short`; reproduces `bot/scanner.py:160-166` scaling formula in `confluence_min_valid`; ATR + price at signal taken from highest-TF available at the anchor (matches `bot/scanner.py:211`).
- `bot/backtesting/indicators.py` — RSI gains `mode='wilder'|'sma_gain_loss'`; ATR gains `mode='wilder'|'sma_true_range'`. `vwap_dev` and `bb_pos` added with live-matching `+1e-9` epsilons and `0.5` band-collapse fallback. Defaults are unchanged — M17.A SmaCrossoverStrategy continues to read Wilder semantics.
- `bot/backtesting/data_loader.py` — `load_multi_tf_bars(cfg, timeframes, *, allow_partial_tfs=False)` wraps `load_backtest_bars` per-TF via `dataclasses.replace`; reuses 100% of M17.A's integrity gates (coverage row, NaN/dup-ts/empty, bar-level range, non-trading-day boundary tolerance).
- `bot/backtesting/runner.py` — `isinstance(strategy, MultiTimeframeStrategy)` branch loads multi-TF bars and attaches `MultiTimeframeContext`. Single-TF path UNCHANGED for SmaCrossoverStrategy.
- `bot/backtesting/config.py` — `ExecutionConfig` gains `stop_mode`/`stop_atr_mult`/`target_atr_mult`; defaults preserve M17.A. Registered-strategies set widened to `{sma_crossover, scanner_replica}`; error message updated.
- `bot/backtesting/execution.py` — entry block branches on `stop_mode`; ATR-mode looks up `atr_at_signal` (shifted by 1 to align with the signal-generating bar) and skips entries with `atr_unavailable_at_signal` warning if NaN; per-bar equity-record path preserved on skip.
- `configs/backtests/example_scanner_replica_aapl.json` — operator-facing example.
- 200 M17 tests across G1..G10 (was 140 at M17.A acceptance), FutureWarning-clean under warnings-as-errors. 1 deliberately skipped (the `atr_stop_above_fill` defensive guard, unreachable through valid config — documented).

**VPS evidence (2026-06-07, operator-verified at HEAD `3f1079e`):**
- HEAD = `3f1079e` (= expected)
- 8 M17.B commits present: `f6bf24e..3f1079e`
- `bot/data.py` sha = `03f488c73feba19a9088b779722ee53515e936f2` (byte-identical to the M17.A baseline; verified at every M17.B commit)
- Full M17 + M16 + audit-P1 combined regression: **exit code 0** (expected composition 200 M17 + 70 M16 + 23 audit-P1 = 293 tests OK, skipped=2 — 1 from M17.B.5's unreachable defensive ATR-guard, 1 from M16's live-yfinance smoke gated on `M16_LIVE=1`)
- M17.B.6 candidate_snapshots replay diagnostic: exit code 0 (K-replayed varies depending on M16 intraday coverage; `failed=0` invariant holds; per Sharpened Rule #5 no equivalence claim is made when K=0)
- `example_sma_aapl.json` end-to-end: exit code 0 (M17.A baseline reproducibility preserved)
- `example_scanner_replica_aapl.json` end-to-end: exit code 2 — `MissingDataError` due to missing M16 intraday AAPL coverage (carry-forward; see honest residual below)
- Production: `algo-trader-dashboard.service` active, `caddy.service` active, `https://algotrading.marketwarrior.club/api/health` HTTP 200
- `git status` clean

**Hard-constraint evidence:**
- Protected files modified by M17.B vs `13a3aa4` baseline: **0 / 20**
- Cumulative protected files modified vs `ceb8cd5` (pre-P0 baseline): **2 / 20** (unchanged — `main.py` + `bot/risk.py` from `audit-P0-4` only)
- `bot/data.py`: byte-identical to baseline at every M17.B commit (sha above)
- AST scan of `bot/backtesting/*`: zero forbidden imports including the M17.B additions (`bot.scanner` / `bot.strategy` / `bot.feature_engine` / `bot.indicators` / `bot.sentiment` / `bot.flywheel`) and the M17.A baseline (yfinance / `bot.data` / etc.). `_M17_A_BASELINE_FORBIDDEN` regression asserts the M17.A baseline set is still a subset of the active forbidden set.
- `bot.historical` imported by exactly one file in `bot/backtesting/*`: `data_loader.py`. M17.B.3 `mtf_context.py` is M16-store-free (it operates on already-loaded DataFrames).
- Test-file imports of `bot.scanner.score_timeframe` and `bot.feature_engine.compute_features` (per Sharpened Rule #4 / Q12) live in `test_m17_backtesting.py` only; the G10 AST walker scans `bot/backtesting/*.py` so test-file imports are outside the scan path. Intentional and audited.
- Socket-call scan during a full backtest run: 0 (unchanged from M17.A).
- `data/bar_cache` / `data/bt_v2_cache` legacy caches: AST-scanned and confirmed unused by `bot/backtesting/*`. They exist on the VPS from pre-M16 work; M17.B does NOT read them.
- No new files outside `bot/backtesting/*`, `configs/backtests/*`, `test_m17_backtesting.py`, and the M17 doc area (`docs/M17_B_closeout.md` + the 3 repo-level closeout-touched docs).
- No `.env`, no service unit files, no generated runtime data committed.
- No new runtime dependencies.
- No modifications to `bot/backtest.py` / `bot/backtest_v2.py` (legacy retirement is a separate future sub-milestone).
- M17.A reproducibility intact: `bk.ENGINE_VERSION == 'M17.A.1'` (Sharpened Rule #2 — not bumped because default `stop_mode='pct'` keeps M17.A semantics byte-identical; SmaCrossoverStrategy continues to pass all M17.A tests unchanged on the VPS sweep).

**Sharpened Rules audit (operator-pinned at the start of M17.B implementation):**
- **#1 tolerances:** `_PARITY_RTOL_SYNTH=1e-9`, `_PARITY_RTOL_REAL_REPLAY=1e-4`, `_PARITY_ATOL=1e-8` exposed as named constants and consumed by G3 + G6.
- **#2 perf discipline:** indicators precomputed once per TF; `snapshot_at` is pure `searchsorted`; perf budget test asserts under 2s for 6,600 anchors × 4 TFs (5x headroom on the 10s soft budget).
- **#3 partial-mode semantics:** STRICT default; PARTIAL opt-in via explicit `allow_partial_tfs=True`; `partial_tf_unavailable` warning carries TF + symbol + underlying error; per-anchor `available_tfs` is the unit (not run-level).
- **#4 AST guard expanded EARLY:** M17.B forbidden imports added in M17.B.1 (the first feature commit) before any production code that might tempt the import; baseline-preservation regression added alongside.
- **#5 replay diagnostic:** prints `[m17.b.6] candidate_snapshots replay: N considered, K replayed, S skipped (...), failed=F` and an explicit "K=0 means not enough live data yet; equivalence NOT claimed" follow-up.
- **#6 new example config:** `example_scanner_replica_aapl.json` added; `example_sma_aapl.json` unchanged.

**M17.B proves end-to-end:**
1. scanner_replica's per-rule scoring matches `bot.scanner.score_timeframe` exactly across every long/short rule branch (G4_ScannerReplicaScoringParity, 11 tests).
2. The confluence scaler matches `bot/scanner.py:160-166` for every `(available_tfs, cfg_min)` in 1..4 × 1..4 (G4_ScannerReplicaConfluenceScaler, 4 tests).
3. Per-bar indicator values match `bot.feature_engine.compute_features` to floating-point precision on identical synthetic bars (G3_IndicatorParity, 12 tests, `rtol=1e-9 + atol=1e-8`).
4. The multi-TF loader strictly rejects missing per-TF coverage with the right backfill command in its error message; the strict-per-TF gate behaved exactly as specified on the VPS scanner_replica example (exit code 2 with `MissingDataError`).
5. The multi-TF context is look-ahead-safe by construction; no snapshot ever returns a bar with `ts_utc > anchor`. Asserted across every anchor × every TF (G3_MtfContext).
6. ATR-mode entries refuse to enter without a stop (`atr_unavailable_at_signal` warning path); ATR-mode pct-equivalence smoke confirms `stop_mode='pct'` default keeps M17.A byte-identical.
7. End-to-end through `runner.run` works on a synthetic 4-TF fixture; downtrend fixture confirms zero short trades emitted.
8. Replay diagnostic is honest: K=0 is accepted-pass; no equivalence claim is made when K=0.
9. No protected files touched. `bot/data.py` byte-identical. Zero forbidden imports in production. Zero live scanner/strategy/indicators/feature_engine/sentiment/flywheel touches.

**Authoritative operator reference:** [`docs/M17_B_closeout.md`](docs/M17_B_closeout.md).

**Open known limitations — honest residual at acceptance:**
- **Real intraday end-to-end on VPS is UNVERIFIED.** The
  `example_scanner_replica_aapl.json` example exited code 2 with
  `MissingDataError` because M16 lacks AAPL 4H/1H/15m coverage on
  the VPS. Backfill attempts hit `YFRateLimitError` (`rate_limited=1`,
  `rate_limit_count=6`, exit 1) on 1H and 15m; 4H wrote no data
  because its 1H source is absent. The strict-per-TF gate behaved
  EXACTLY as specified — Sharpened Rule #3 is intact. Equivalence is
  proven by the synthetic per-rule parity tests (G3 + G4) and
  end-to-end synthetic integration (G4_ScannerReplicaIntegration);
  real intraday replay against live `candidate_snapshots` is a
  carry-forward, not a regression. NO code workaround, NO fallback
  to legacy `data/bar_cache` / `data/bt_v2_cache`, NO weakening of
  strict-per-TF was done in response.
- **Shorts in scanner_replica are silently suppressed** because the
  execution layer is long-only (`allow_short=False` is the M17
  invariant). Asserted by `test_scanner_replica_does_not_emit_short_signals`.
  Lifting the long-only constraint is a separate decision.
- **M16 intraday backfill from the VPS IP is yfinance-rate-limit-prone.**
  Not new to M17.B — carry-forward from M16's existing residual
  ("live multi-symbol backfill at scale remains rate-limit-prone
  against Yahoo from the VPS IP"). M17.B did not regress it and did
  not work around it.
- **Pre-existing `test_m13_5_reconcile` / `test_m14_risk` errors
  under `unittest discover`** — same status as at M17.A acceptance:
  broken at M17 baseline `13a3aa4`, same at M17.B acceptance
  `3f1079e`. Both pass when run as standalone scripts. Out of M17.B
  scope; flagged again here for a future audit.
- **Legacy `bot/backtest.py` / `bot/backtest_v2.py`** untouched.
  Retirement is a separate future sub-milestone.

**Open audit backlog (unchanged from M17.A — none closed by M17.B):**
- `audit-P1-broker-permId-fallback` — DEFERRED
- `audit-P1-portfolio-ctx-engine-bypass` — DEFERRED
- `audit-P2-batch` (9 items) — DEFERRED
- `audit-P3-batch` (6 items) — DEFERRED
- `M14-extension-to-scanner-path` — BLOCKER FOR M22 only (does NOT
  block any M17 follow-on work)
- See `docs/NEXT_WORK_REGISTER.md` for the full active list.

**M17.B carry-forward** (Active entry in `docs/NEXT_WORK_REGISTER.md`):
`scanner_replica real intraday E2E — provider/data blocked`. Closes
when M16 intraday coverage exists (yfinance backfill succeeds OR
alternate provider behind `bot.historical` interface) AND scanner_replica
example exits 0 on VPS.

**M17 is now CLOSED** — both M17.A (engine foundation) and M17.B
(scanner_replica + multi-TF confluence + indicator parity + ATR exits)
shipped and VPS-verified. M17.C / future deferred items: real intraday
provider reliability, shorts, multi-symbol portfolio, optimisation /
parameter sweeps, dashboard backtest UI, legacy backtest retirement.

---

## Future milestones (M17–M23)

Listed for scope-preservation; see `ROADMAP.md` for the full descriptions. M16 is CLOSED (see detail above). The M1–M16 audit-only pass is CLOSED 2026-06-05 (P0 batch + the first P1 sub-milestone `audit-P1-data-rate-limit-fix`); remaining P1 / P2 / P3 backlog stays open per `docs/NEXT_WORK_REGISTER.md`. **M17 is now FULLY CLOSED 2026-06-07** — both M17.A (engine foundation at HEAD `a05f160`) and M17.B (scanner_replica + multi-TF confluence at HEAD `3f1079e`); detailed sections above.

| # | Title | Status | Note |
|---|---|---|---|
| 17 | Backtesting + parameter rules | **CLOSED 2026-06-07** — M17.A at `a05f160`, M17.B at `3f1079e` | Engine foundation (M17.A) + scanner_replica with multi-timeframe confluence + indicator parity + ATR-exits-opt-in (M17.B). 200 M17 tests + 70 M16 + 23 audit-P1 = 293 OK on VPS. See `docs/M17_A_closeout.md` and `docs/M17_B_closeout.md`. Carry-forward: real intraday scanner_replica E2E (provider/data-blocked; condition to close = M16 intraday coverage exists or alternate provider in place). |
| 18 | News / Sentiment / Macro | PENDING | aggregation across multiple providers + macro overlay |
| 19 | Universe Diagnostics & Discovery | PENDING | why-no-signal explainer, liquidity/spread filters |
| 20 | Optimiser / Adaptive Sizing | PENDING | confidence-adjusted + volatility-targeted |
| 21 | First Funded eToro Go-Live | PENDING | deferred from M13/M14; funded account onboarding |
| 22 | Semi-Automated Live Trading | PENDING | authority ladder reaches `AUTO_ALLOWED` per-broker |
| 23 | Full Advanced Intelligence | PENDING | correlation-aware sizing, automated broker failover, compliance audit |

---

## Operating principles (carry-forward)

These are the project's permanent ground rules (per user constitution):
- Keep the overall roadmap unchanged.
- Do not reduce the final scope of the bot.
- Work on one milestone at a time; do not move on until verified.
- Do not change strategy thresholds to manufacture signals.
- No terminal work required from the operator unless absolutely unavoidable; if required, a single copy-paste command only.
- Be honest about what is verified vs not verified.
- Every deployment path must be self-contained.
- Git hygiene is critical; secrets gitignored; managed via `.env`.
