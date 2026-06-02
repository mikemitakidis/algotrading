# Algo Trader — Roadmap

This is the canonical project roadmap. The **original 15-milestone plan stays
intact** at the top level; expansions (sub-milestones like M13.5.A–D, M14.A–H,
M15.1–3) live inside the parent milestone and are tracked in
[`MILESTONE_STATUS.md`](MILESTONE_STATUS.md). Do not reorder, compress, or
delete milestones.

For the live state of every milestone (✅ closed / ⚠ partial / ⏳ pending /
🔄 superseded), see [`MILESTONE_STATUS.md`](MILESTONE_STATUS.md). For a
narrative snapshot of where the project is right now, see
[`docs/PROJECT_STATUS_RECONCILIATION.md`](docs/PROJECT_STATUS_RECONCILIATION.md).

---

## Original 15-Milestone Plan (intact)

### Milestone 1 — Stable Shadow-Mode Scanner ✅ CLOSED
**Goal:** Bot runs 24/7, fetches real market data, generates signals, logs to DB, sends Telegram alerts.
- Scans curated large-cap US symbols across 1D / 4H / 1H / 15m
- RSI + MACD + EMA + VWAP + volume confluence scoring
- SQLite signal storage with auto-migration
- Telegram alerts for manual review
- Flask dashboard on port 8080
- GitHub → server auto-sync via `sync.sh`

### Milestone 2 — Telegram from Dashboard ✅ CLOSED
**Goal:** Configure and test Telegram from the dashboard without touching `.env` manually.
- Enable/disable Telegram, set token and chat_id from UI
- "Find My ID" auto-fills chat_id from bot's recent messages
- Send Test button verifies configuration

### Milestone 3 — Dashboard Observability ✅ CLOSED
**Goal:** Dashboard alone is enough to understand what the bot is doing.
- Phase badge (scanning / cooldown / stopped / crashed) with live countdown
- Last cycle summary: signals, TFs, symbols, duration, TF pip strip
- System panel: mode, focus count, DB rows, Telegram status, interval
- Improved log colouring and Cycle events filter
- `data/bot_state.json` written atomically on every phase change

Closure note: completed after a GitHub/server branch reconciliation and JS fixes.

### Milestone 4 — Strategy Engine ✅ CLOSED
**Goal:** Strategy logic visible and editable from dashboard. No hidden hardcoding.
- All thresholds in `bot/strategy.py` (single source of truth)
- Dashboard Strategy tab: edit long/short rules, confluence, ATR risk, routing
- Validation, default-reset, and full audit trail with version numbers
- Scanner reads strategy from `data/strategy.json` on every cycle

### Milestone 5 — Backtesting ✅ CLOSED (accepted-enough; known limitations)
**Goal:** Walk-forward backtest using the exact same live strategy — no parallel code.
- `bot/backtest.py` calls live `compute()`, `score_timeframe()`, `load_strategy()`
- 3-tier cache (`bt_cache` → live bot cache → network with pacing + retry)
- Real cancel; status `running / done / partial / cancelled / timeout`
- Stats: win rate, profit factor, drawdown, annualised return, monthly breakdown, per-symbol, by timeframe, by TF combination, equity curve, benchmark vs SPY
- Dashboard Backtest tab + `backtest_cli.py`

**Known carry-forward:** Yahoo/cache rate limits and gaps. Backtesting is
accepted as functional; provider-side reliability tracked under M6/M15
hardening, not reopened in M5.

### Milestone 6 — Modular Data-Provider Architecture ✅ CLOSED
**Goal:** Clean provider abstraction so providers can be swapped without touching strategy or backtest code.
- `bot/providers/base.py` — abstract `DataProvider`
- `bot/providers/yfinance_provider.py` — current implementation
- `bot/providers/alpaca_provider.py` — alternate implementation present
- Config-selectable via `DATA_PROVIDER=…` in `.env`
- Dashboard shows active provider in System panel

### Milestone 7 — More Indicators / Richer Logging ✅ CLOSED
**Goal:** Add features needed by the ML pipeline.
- Per-signal `_ml_features` block attached in `bot/scanner.py` (indicator
  snapshot logged with every signal)
- Indicator periods exposed via `data/strategy.json`
- Feature payload powers `ml_train.py` (see M9)

### Milestone 8 — News / Sentiment Module ⚠ IMPLEMENTED (real, not closed-loop)
**Goal:** Pre-filter signals with news-sentiment alignment.
- `bot/sentiment/news_provider.py` (~456 lines) — real NewsAPI integration
  with caching, error classification, fetch-success flags
- `bot/sentiment/disabled_provider.py` — opt-out path
- Pluggable via `get_sentiment_provider()`; sentiment applied inside the
  scanner cycle via `apply_sentiment(signal, sent_result, sent_mode)`
- `SentimentResult` dataclass; failure modes captured (`fetch_failed`,
  `keys_absent`, …)

**Honest gap:** macro/news aggregation across multiple sources and a
confidence-weighted sentiment score are not yet built. Current
implementation is single-provider, single-pass. Tracked for M18.

### Milestone 9 — ML Pipeline ⚠ INFRASTRUCTURE COMPLETE, NOT CLOSED-LOOP
**Goal:** XGBoost (or similar) model trained on accumulated signal data to filter low-quality setups.
- `ml_train.py` (~541 lines) — XGBoost meta-labeling with walk-forward
  cross-validation, calibrated probabilities, precision-recall, per-group
  evaluation, filter-comparison tables. Reads
  `data/ml/training_dataset.parquet` or scattered backtest reports.
- `ml_build_dataset.py` — dataset assembly.
- Data flywheel (M10+) feeds `candidate_snapshots` / `execution_intents` /
  `signal_outcomes`.

**Honest gap:** model is NOT yet wired into scanner.py as a live filter.
Closed-loop self-learning (train → predict → filter → outcome → retrain) is
deferred. Today: trains and evaluates only.

### Milestone 10 — Broker Execution Architecture ✅ CLOSED
**Goal:** Clean broker abstraction; no live execution as part of this milestone.
- `bot/brokers/base.py` — `BrokerAdapter`, `OrderIntent`, `OrderResult`
- `bot/brokers/paper_broker.py` — paper path
- `bot/brokers/ibkr_broker.py` — IBKR adapter
- Broker registry with `BROKER=` env switching
- Flywheel schema (`execution_intents`, `candidate_snapshots`, `signal_outcomes`)

### Milestone 11 — IBKR Paper Trading ✅ CLOSED
**Goal:** Automated order placement on IBKR paper account.
- `bot/brokers/ibkr_broker.py` (via `ib_insync`) wired to paper port 4002
- IBC 3.22.0 headless IB Gateway, systemd-managed, `Restart=always`
- `test_m11.py` confirms paper login flow

### Milestone 12 — IBKR Live Trading ✅ CLOSED (capability proven; sustained live trading is post-M14)
**Goal:** Real-money execution on IBKR.
- Live Gateway on port 4001 (`config.live.ini`, `start_ibgateway_live.sh`,
  `/var/lib/ibgateway-live`); nightly `AutoRestartTime=23:45`
- Real broker acceptance proven by a controlled live order (Ford/F, 1 share,
  delayed market data) with confirmed `permId`; `execution_intents` row
  reflects the truthful state; position cancelled cleanly afterwards.
- `test_m12.py` (offline 13 tests) + `test_m12_live_order.py` (`--live` flag,
  Gateway connection + reconciliation)

### Milestone 13 — eToro Integration / Manual Bridge ✅ CLOSED (capability built; first funded order is separate)
**Goal:** eToro live-write capability, built / gated / reviewed / deployed / no-write verified.
- See `docs/M13_7_closeout.md` for the full chain (61a→f7a3bc2) and the
  13 safety invariants with their 173 proving tests in `test_m13_5_*.py`.
- Demo disabled (fail-closed); `--base-url` removed; real mode pinned to
  `https://public-api.etoro.com`; double live flag + per-payload nonce;
  scanner-isolation invariant maintained.
- **Zero real eToro orders placed.** First funded order remains a separate
  later go-live event (not part of M13, not part of M14).

### Milestone 14 — Portfolio / Risk Layer ✅ CLOSED (A through H)
**Goal:** Risk Intelligence Layer — broker-scoped state, exposure, decision
core + governor, eToro preflight integration, dashboard, closeout.

**Authoritative closeout document:** [`docs/M14_FINAL_AUDIT.md`](docs/M14_FINAL_AUDIT.md).

- **M14.A** design doc (CLOSED, `3f4448e`).
- **M14.B** additive `daily_state_per_broker` + `risk_snapshots` +
  `risk_decisions` + `broker_positions` schema; legacy `daily_state` untouched (CLOSED, `42ee08c`).
- **M14.C** read-only realised-PnL ingestion adapters (IBKR + eToro) with
  fail-closed semantics; CLI `tools/ingest_risk_state.py` (CLOSED, `d9c53eb`).
- **M14.D** read-only exposure/positions adapters + `broker_positions` batch
  schema + cross-engine separation; CLI `tools/ingest_exposure_state.py`
  (CLOSED, `729ad2d`).
- **M14.E** Risk Authority Engine + downgrade-only governor; pure `decide()`,
  25 gates, 31 reason codes, M13.4A policy bridge, `decide_and_audit`
  thin wrapper as the only DB-writing surface (CLOSED, `ace0fda`,
  `test_m14_e_engine.py` 105/105).
- **M14.F** eToro live-write preflight integration — `run_risk_preflight`
  inserted before transport/env/nonce/broker construction; exit 4 on block
  (CLOSED, `2e20b52`, `test_m14_f_preflight.py` 34/34).
- **M14.G** read-only Risk Authority dashboard tab + 4 GET endpoints
  (CLOSED, `71e893a`, `test_m14_g_dashboard.py` 51/51).
- **M14.H** Closeout / audit doc — this document + `docs/M14_FINAL_AUDIT.md`
  (CLOSED).

**M14 totals:** 9 commits, ~12,321 lines, 17 new modules, 324 sub-milestone tests, 0 real-money orders placed, 0 bypasses around Risk Authority.

### Milestone 15 — Production Hardening ⚠ PARTIAL
**Goal:** Monitoring, alerting, failover, full audit log, compliance-grade logging.
- **M15.0** Flywheel schema (CLOSED, baseline for M14).
- **M15.1** Gateway state + reconciliation tooling (CLOSED, `test_m15_gateway.py` 33/33).
- **M15.2** Health endpoint + external monitoring (CLOSED, `test_m15_2_health.py` 28/28, `docs/M15_2_external_monitoring.md`).
- **M15.3** Infra recovery: process-manager / systemd unit-name cleanup
  (carried forward from M13.5.C, M14.B/C/D VPS warnings, and reaffirmed
  by M14 final audit as the **next concrete unit of work after M14**),
  IB Gateway reliability hardening — PENDING.

> **Post-M14 priority** (from `docs/M14_FINAL_AUDIT.md` §10): the next
> milestone after M14 is **M15.0 — scanner / systemd reliability and
> production process clarity**, before any M16+ intelligence work.
> Until the scanner systemd unit-name mismatch is resolved (all known
> unit names report inactive while the bot demonstrably runs), every
> milestone-acceptance signal carries an asterisk. The roadmap order is
> unchanged; M16+ does not start until M15 is closed.

---

## Future Roadmap (M16–M23)

Visible plan. None are started; they're listed so scope is never quietly
lost during reviews.

### Milestone 16 — Strategy / Historical Intelligence
- Multi-regime backtest harness; bull/bear/range/vol clustering
- Strategy versioning + A/B comparison against live shadow
- Hyperparameter introspection per market regime

### Milestone 17 — Outcome Learning Loop / Closed-Loop ML
- Live shadow scoring with the M9 model
- Outcome → retraining pipeline
- Drift detection + automatic model rollback
- Reaches the "self-learning" bar that M9 does not currently meet

### Milestone 18 — News / Sentiment / Macro
- Multi-source news aggregation
- Macro overlay (rates, VIX, calendar events)
- Confidence-weighted sentiment score replacing single-provider value

### Milestone 19 — Universe Diagnostics & Discovery
- Symbol coverage analytics
- Why-no-signal diagnostic per (symbol, TF, cycle)
- Liquidity / spread / event filters as first-class universe inputs

### Milestone 20 — Optimiser / Adaptive Sizing
- Confidence-adjusted position sizing (interface stub from M14.A → real)
- Volatility-targeted exposure
- Per-regime sizing curves

### Milestone 21 — First Funded eToro Go-Live
- The deferred go-live event (intentionally outside M13/M14)
- Manual operator confirmation; daily-loss seam fed by M14.E/F
- Funded account onboarding, capital allocation policy review

### Milestone 22 — Semi-Automated Live Trading
- Authority ladder reaches `AUTO_ALLOWED` for one broker at a time
- Operator-in-the-loop override always available
- Risk Authority Engine governs every order

### Milestone 23 — Full Advanced Intelligence
- Correlation-aware sizing (M14.A design-only → real)
- Automated broker failover (M14.A design-only → real, gated)
- Compliance-grade audit log, regulatory artifact export
