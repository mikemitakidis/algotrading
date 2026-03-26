# Algo Trader — Roadmap

This is the agreed 15-milestone roadmap. Do not reorder or compress milestones.

## Milestone 1 — Stable Shadow-Mode Scanner ✅ COMPLETE
**Goal:** Bot runs 24/7, fetches real market data, generates signals, logs to DB, sends Telegram alerts.

- Scans 89 curated large-cap US symbols across 1D / 4H / 1H / 15m
- RSI + MACD + EMA + VWAP + volume confluence scoring
- SQLite signal storage with auto-migration
- Telegram alerts for manual review
- Flask dashboard on port 8080
- GitHub → server auto-sync via sync.sh

## Milestone 2 — Telegram from Dashboard ✅ COMPLETE
**Goal:** Configure and test Telegram from the dashboard without touching .env manually.

- Enable/disable Telegram, set token and chat_id from UI
- "Find My ID" button auto-fills chat_id from bot's recent messages
- Send Test button verifies configuration

## Milestone 3 — Dashboard Observability ✅ COMPLETE
**Goal:** Dashboard alone is enough to understand what the bot is doing.

- Phase badge (scanning / cooldown / stopped / crashed) with live countdown
- Last cycle summary: signals, TFs, symbols, duration, TF pip strip
- System panel: mode, focus count, DB rows, Telegram status, interval
- Improved log colouring and Cycle events filter
- `data/bot_state.json` written atomically on every phase change

## Milestone 4 — Strategy Engine ✅ COMPLETE
**Goal:** Strategy logic visible and editable from dashboard. No hidden hardcoding.

- All thresholds in `bot/strategy.py` (single source of truth)
- Dashboard Strategy tab: edit long/short rules, confluence, ATR risk, routing
- Validation, default-reset, and full audit trail with version numbers
- Scanner reads strategy from `data/strategy.json` on every cycle

## Milestone 5 — Backtesting ✅ COMPLETE
**Goal:** Walk-forward backtest using the exact same live strategy — no parallel code.

- `bot/backtest.py` calls live `compute()`, `score_timeframe()`, `load_strategy()`
- Full date-range data fetching via same yfinance browser-session path as live bot
- 3-tier cache (bt_cache → live bot cache → network with pacing + retry)
- Real cancel: threading.Event + run-token prevents stale writes
- Status: `running | done | partial | cancelled | timeout`
- Stats: win rate, profit factor, drawdown, annualised return, monthly breakdown,
  per-symbol, by timeframe, by TF combination, equity curve, benchmark vs SPY
- Dashboard Backtest tab: validation presets, equity chart, scatter plot,
  TF availability panel, run history
- CLI: `python backtest_cli.py --symbols AAPL --start 2025-01-01 --end 2025-12-31`
- Timestamped reports in `data/reports/` (report.txt + trades.csv + results.json)

## Milestone 6 — Modular Data-Provider Architecture ✅ COMPLETE
**Goal:** Clean provider abstraction so yfinance can be swapped without touching strategy or backtest code.

- `bot/providers/base.py` — abstract DataProvider class
- `bot/providers/yfinance_provider.py` — current yfinance implementation
- Config-selectable via `DATA_PROVIDER=yfinance` in .env
- Dashboard shows active provider in System panel

## Milestone 7 — More Indicators / Richer Logging
**Goal:** Add more signal features needed by the ML pipeline.

- Expose indicator periods (RSI 14, MACD 12/26/9, etc.) to Strategy dashboard
- Add Stochastic, Williams %R, or VWAP bands as optional indicators
- Log full indicator snapshot with every signal for ML training data

## Milestone 8 — News / Sentiment Module
**Goal:** Pre-filter signals with news sentiment alignment.

- NewsAPI.org or similar source
- Sentiment score computed before signal generation
- Logged alongside every signal for ML use

## Milestone 9 — ML Pipeline
**Goal:** XGBoost (or similar) model trained on accumulated signal data to filter low-quality setups.

- Training script reads from signals.db and backtest trade logs
- Model saved to data/model.pkl
- Integrated as optional filter in scanner.py
- A/B comparison: filtered vs unfiltered signals logged separately

## Milestone 10 — Broker Execution Architecture
**Goal:** Clean broker abstraction, no live execution yet.

- `bot/brokers/base.py` — abstract BrokerInterface
- Execution logic fully separated from signal logic

## Milestone 11 — IBKR Paper Trading
**Goal:** Automated order placement on IBKR paper account.

- ib_insync integration
- Position sizing: 2% of capital per trade
- Stop loss: 2×ATR, take profit: 3×ATR
- Paper trades logged separately in signals.db
- 2 weeks of paper trading required before live

## Milestone 12 — IBKR Live Trading
**Goal:** Real-money execution on IBKR. Requires proven paper results.

## Milestone 13 — eToro Integration / Manual Bridge
**Goal:** If eToro API becomes available, automate 4/4 TF signals. Otherwise refine Telegram alerts.

## Milestone 14 — Portfolio / Risk Layer
**Goal:** Portfolio-level position sizing, correlation limits, drawdown circuit breakers.

## Milestone 15 — Production Hardening
**Goal:** Monitoring, alerting, failover, full audit log, compliance-grade logging.
