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
| 14 | Portfolio / Risk Layer | PARTIAL (A–D CLOSED; E/F/G/H PENDING) | See M14 detail below |
| 15 | Production Hardening | PARTIAL (M15.0/.1/.2 CLOSED; M15.3 PENDING) | See M15 detail below |
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

### Milestone 14 — Portfolio / Risk Layer (PARTIAL — A–D CLOSED; E/F/G/H PENDING)

| Sub-milestone | Status | Commit | Evidence |
|---|---|---|---|
| M14.A — Risk Intelligence Design | CLOSED | `3f4448e` | `docs/M14_A_design.md` |
| M14.B — Schema + migration | CLOSED | `42ee08c` | `test_m14_b_schema.py` 27/27; VPS verified |
| M14.C — Realised-PnL ingestion adapters | CLOSED | `d9c53eb` | `test_m14_c_ingest.py` 47/47; VPS dry-run verified |
| M14.D — Exposure ingestion + `broker_positions` | CLOSED | `729ad2d` | `test_m14_d_exposure.py` 60/60; VPS dry-run verified |
| M14.E — Risk Authority Engine + Governor | PENDING | — | Plan: see `docs/PROJECT_STATUS_RECONCILIATION.md` §M14.E |
| M14.F — eToro preflight integration | PENDING | — | Closes the manual `realised_daily_loss` seam in `tools/etoro_live_write.py` |
| M14.G — Dashboard risk surfaces | PENDING | — | Read-only; no live-write button |
| M14.H — Closeout / audit doc | PENDING | — | — |

**Files in `bot/risk_authority/`** (12 modules):
- M14.B: `state.py` (compat shim).
- M14.C: `reading.py`, `ingest.py`, `ingest_etoro.py`, `ingest_ibkr.py`, `ingest_audit.py`.
- M14.D: `exposure_reading.py`, `ingest_exposure.py`, `ingest_etoro_exposure.py`, `ingest_ibkr_exposure.py`.
- Plus `__init__.py` and `__pycache__`.

**Carry-forward limitation (acknowledged):** On the VPS, M14.C/M14.D return
`unknown` for the eToro real adapter (`keys_absent`) and the IBKR exposure
adapter (`NotImplementedError` — positions reader not yet wired to Gateway).
This is acceptable for M14.C/M14.D closure because the engines correctly
classify unknowns and refuse to fake zero; production-grade adapter wiring
happens around M14.E/F/G or in M15.3 infra recovery.

### Milestone 15 — Production Hardening (PARTIAL)

| Sub-milestone | Status | Evidence |
|---|---|---|
| M15.0 — Flywheel schema baseline | CLOSED | `bot/flywheel.py`; `test_m15_schema.py` 6/6 |
| M15.1 — Gateway state + reconciliation | CLOSED | `test_m15_gateway.py` 33/33 |
| M15.2 — Health endpoint + external monitoring | CLOSED | `test_m15_2_health.py` 28/28; `docs/M15_2_external_monitoring.md` |
| M15.3 — Infra recovery (process manager, IB Gateway reliability) | PENDING | — |

**M15.3 open items** (carry-forwards from M13.5.C, M14.B, M14.C, M14.D VPS verifications):
- **Scanner systemd unit-name mismatch.** `algo-trader`, `scanner`, and `algo-scanner` all report `inactive` while the bot is demonstrably running (`/api/health` 200, heartbeat fresh, dashboard port listening). The actual process manager / unit name needs identifying or documenting.
- **IB Gateway reliability hardening.** Beyond nightly `AutoRestartTime`: structured restart-on-stale-heartbeat, monitor 4001/4002 socket health, alert on prolonged disconnect.
- **Compliance-grade audit log + regulatory export** — explicit M15.3 scope, not happening earlier by accident.

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
