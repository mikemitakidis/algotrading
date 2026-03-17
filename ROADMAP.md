# Roadmap

## Milestone 1 — Stable Shadow Mode ✅ (current)
**Goal**: Bot runs 24/7, fetches real data, generates signals, logs to DB, sends Telegram alerts.

Acceptance criteria:
- [ ] One full scan cycle completes without error
- [ ] At least 1 signal logged to signals.db within 24 hours
- [ ] Telegram alert received on phone
- [ ] Dashboard accessible and shows live log
- [ ] Bot survives server reboot
- [ ] sync.sh deploys GitHub changes automatically

## Milestone 2 — Backtesting
**Goal**: Validate the scoring logic on 3 years of historical data before any live execution.

Acceptance criteria:
- [ ] backtest.py uses yfinance (same data source as live bot)
- [ ] Tests on 50+ symbols over 3 years
- [ ] Produces: total trades, win rate, profit factor, max drawdown, equity curve
- [ ] Results saved to data/backtest_results.json
- [ ] Win rate > 50% and profit factor > 1.2 required before proceeding to M3

## Milestone 3 — ML Pipeline
**Goal**: XGBoost model trained on shadow mode signal data to filter low-quality signals.

Prerequisites:
- Minimum 2 weeks of shadow mode data in signals.db
- Milestone 2 completed

Acceptance criteria:
- [ ] Training script reads from signals.db
- [ ] Model saved to data/model.pkl
- [ ] Model integrated as optional filter in scanner.py
- [ ] A/B comparison: signals with and without ML filter logged separately

## Milestone 4 — IBKR Paper Trading
**Goal**: Automated order execution on 3/4 TF signals via Interactive Brokers paper account.

Prerequisites:
- Milestone 2 AND Milestone 3 completed
- IBKR paper account open and API approved

Acceptance criteria:
- [ ] ib_insync integration in bot/execution.py
- [ ] Position sizing: 2% of capital per trade
- [ ] Stop loss: 2×ATR
- [ ] Take profit: 3×ATR
- [ ] Paper trades logged to separate table in signals.db
- [ ] 2 weeks paper trading with positive expectancy before live

## Milestone 5 — IBKR Live Trading
**Goal**: Real money execution on IBKR.

Prerequisites: Milestone 4 with proven paper results.

## Milestone 6 — eToro Integration Readiness
**Goal**: If eToro API becomes available, automate 4/4 TF signals.

Currently: manual via Telegram alert. eToro API not available.

## Milestone 7 — Sentiment Filter
**Goal**: Add news sentiment as a pre-filter. Signal only fires if sentiment aligned.

Data source: NewsAPI.org or similar.
Integration point: scanner.py, before signal generation.
