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
| 16–23 | Future scope | PENDING | See `ROADMAP.md` |

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
- **M15 is now fully CLOSED.** The next active milestone is **M16 (Historical data + first signal engine)** per the post-M15 strategic direction (recorded 2026-06-04 on M15.3.A.cutover closeout, reaffirmed at every subsequent M15.3 sub-milestone closeout). Dashboard work stops unless safety- or compliance-driven.

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

## Future milestones (M16–M23)

Listed for scope-preservation; see `ROADMAP.md` for the full descriptions.

| # | Title | Status | Note |
|---|---|---|---|
| 16 | Strategy / Historical Intelligence | PENDING | regime-aware backtesting, A/B vs live shadow |
| 17 | Outcome Learning Loop / Closed-Loop ML | PENDING | hooks `ml_train.py` into scanner as a live filter |
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
