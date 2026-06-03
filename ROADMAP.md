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
- **M15.0-pre** Flywheel schema (CLOSED, prerequisite for M14; originally
  labelled M15.0 — renumbered to disambiguate from the production-process
  M15.0 that closed 2026-06-02).
- **M15.1** Gateway state + reconciliation tooling (CLOSED, `test_m15_gateway.py` 33/33).
- **M15.2** Health endpoint + external monitoring (CLOSED, `test_m15_2_health.py` 28/28, `docs/M15_2_external_monitoring.md`).
- **M15.0** ✅ CLOSED — Scanner / systemd reliability + production process
  clarity (commit chain `57dc200` → `597635d`, `test_m15_0_service.py` 40/40).
  Canonical systemd units installed and active on the VPS:
  `algo-trader.service` (main bot/scanner) and
  `algo-trader-dashboard.service` (Flask dashboard). VPS verification
  2026-06-02: both PIDs owned by `/system.slice/<unit>`, both
  active/enabled, exactly one of each process, `/api/health` HTTP 200.
  Rollback snapshot: `/var/lib/algo-trader/m15_0_snapshots/20260602T210527Z`.
  New read-only `/api/system/services` endpoint reports the canonical
  service map; auth-protected (returns `{"error":"Unauthorized"}` to
  unauthenticated callers — expected behaviour). `deploy.sh` + `sync.sh`
  are systemd-aware with verbatim legacy nohup fallback for pre-install
  and post-rollback states. Authoritative reference:
  `docs/M15_0_systemd_canonical.md`.
- **M15.4** ✅ CLOSED — IB Gateway reliability + broker connectivity
  health (visibility/truth layer) (commit `073a8bd`,
  `test_m15_4_gateway_health.py` 47/47). New read-only helper
  `bot/gateway_health.py` combines `systemctl is-active/is-enabled/show`,
  TCP connect-and-close probe on 4001/4002, trading-mode discovery
  from `start_ibgateway.sh` + IBC config, `/var/log/ibgateway/ibgateway.log`
  tail, and `journalctl -u ibgateway.service` into a single
  point-in-time classification (`service_down`,
  `service_active_port_closed`, `service_active_login_error`,
  `service_active_api_port_open`, `unknown`). New endpoint
  `GET /api/gateway/health` — auth-protected, returns HTTP 401 to
  unauthenticated callers (confirmed on VPS after dashboard restart);
  existing M15.1 `/api/gateway/state` preserved unchanged. **No IB
  API call was added** — `reqCurrentTime` / `ib.connect` / `placeOrder`
  / `cancelOrder` are AST-asserted absent. Reference mirror of
  production `ibgateway.service` at
  `infra/systemd/ibgateway.service.documented` (not installed by any
  script). VPS classification on closeout day: `ibgateway.service`
  active/enabled but **no listener on either 4001 or 4002**, log
  shows a login/authentication error → `status =
  service_active_login_error`, `ready_for_ibkr_trading = False`.
  This is the headline value: systemd "active" is no longer
  conflated with "IBKR trading is ready". Authoritative reference:
  `docs/M15_4_ib_gateway_runbook.md`.
- **M15.5** ✅ CLOSED — IBKR exposure reader wiring (paper mode)
  (commits `138df9e` → `2446df6`, `test_m15_5_ibkr_exposure.py`
  78/78). The `NotImplementedError` stub at
  `tools/ingest_exposure_state.py::_build_ibkr_exposure_adapter`
  for `ibkr_paper` is replaced by a real read-only IB API positions
  reader at `bot/risk_authority/ibkr_paper_reader.py`. The reader
  connects to `127.0.0.1:4002` with `clientId=15`, `readonly=True`,
  waits for the account-update snapshot to be ready (bounded by
  `api_timeout`), cross-confirms `ib.portfolio()` against
  `ib.positions()`, then disconnects in a `finally` block. The
  M14.D `IBKRExposureAdapter` is byte-identical — M15.5 only
  supplies a real `positions_reader` callable. `ibkr_live`
  continues to raise `NotImplementedError` from the CLI path by
  design. Phased dry-run with per-step observability
  (`error_phase`, `elapsed_ms`, per-step booleans) added in
  `56bb5ce`; login-error precedence gate hardening added in
  `2446df6` (also fixed an M15.4 bug where TCP port-open won
  over login-error). Live VPS evidence on closeout day:
  connected to IBKR paper, server version 176, synchronization
  complete, disconnected cleanly. Confirmed zero open positions:
  `open_positions=0`, `capital_deployed_usd=0.0`, no orders, no
  broker writes, no live mode. Risk Authority verification:
  `ibkr_paper.exposure_known=True` on all three surfaces (DB,
  snapshot ScopeView, M14.G dashboard). Status is
  `exposure_partial` by design — `current_equity_usd` and
  `peak_equity_usd` are classified as `OPPORTUNISTIC_EXPOSURE`
  (not required for known exposure); M14.E/M14.G accept both
  `exposure_fresh` and `exposure_partial` as known. The
  `exposure_stale` UI badge remains until 3 successful reads
  (current count 1) — UI-only, engine gate already cleared.
  `pnl_unknown` is separate (M14.C PnL ingestion surface) and out
  of M15.5 scope. Authoritative reference:
  `docs/M15_5_ibkr_exposure_reader.md`.
- **M15.3** Infra recovery — remaining scope after M15.0, M15.4,
  and M15.5: dashboard auth / TLS / IP-allowlist hardening,
  `manual_reset` operator flow, compliance-grade audit log +
  regulatory export. PENDING.
  *(Process-manager / systemd unit-name mismatch is no longer a
  carry-forward — closed in M15.0. IB Gateway visibility / truth
  reporting is no longer a carry-forward — closed in M15.4.
  IBKR paper exposure ingestion is no longer a carry-forward —
  closed in M15.5. M15.3 is now purely about active remediation
  surfaces and operator-action layers.)*

> **Next concrete unit of work after M15.5:** TBD per operator
> direction. Candidates, in rough priority order:
> 1. M15.3 dashboard auth/security hardening + `manual_reset`
>    operator flow + compliance audit/export. Closes the
>    remaining M15.3 scope.
> 2. Optional M15.5.A polish: populate `current_equity_usd` via
>    `ib.accountSummary()` to lift exposure from `exposure_partial`
>    to `exposure_fresh` and remove the `exposure_stale` UI badge.
>    Pure observability — engine semantics unchanged.
> 3. M14.C PnL ingestion wiring for IBKR paper (resolves the
>    `pnl_unknown` warning surfaced by M14.G).
>
> The roadmap order is unchanged; M16+ intelligence does not
> start until M15 closes (which requires M15.3 to ship).

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
