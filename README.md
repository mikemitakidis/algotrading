# Algo Trader v1

Multi-timeframe trading signal scanner running in shadow mode (no live execution).
Milestones 1–5 complete. See [ROADMAP.md](ROADMAP.md) for the full 15-milestone plan.

## Current Status

| Milestone | Name | Status |
|-----------|------|--------|
| 1 | Stable Shadow-Mode Scanner | ✅ Complete |
| 2 | Telegram from Dashboard | ✅ Complete |
| 3 | Dashboard Observability | ✅ Complete |
| 4 | Strategy Engine | ✅ Complete |
| 5 | Backtesting | ✅ Complete |
| 6 | Modular Data-Provider Architecture | ⬜ Next |
| 7–15 | See ROADMAP.md | ⬜ Planned |

## What the Bot Does

- Scans 89 curated large-cap US symbols every 15 minutes
- Computes RSI, MACD, EMA20/50, Bollinger Bands, VWAP deviation, OBV, ATR across 4 timeframes (15m, 1H, 4H, 1D)
- Generates signals when ≥ 2–3 timeframes agree (configurable from dashboard)
- Logs every signal to SQLite with full indicator snapshot
- Sends Telegram alerts for manual review on eToro
- Routes signals: ETORO (4/4 TFs) or IBKR placeholder (2–3/4 TFs)

## What It Does NOT Do

- No live trades. No broker connections. Shadow mode only.
- No ML filtering yet (Milestone 9)
- No automated execution (Milestones 10–13)

---

## Server Requirements

- Ubuntu 24.04 VPS (Hetzner or equivalent)
- Python 3.12+
- git, wget installed
- Install path: `/opt/algo-trader`

## Setup (One Time)

### 1. Deploy

```bash
cd /opt
git clone https://github.com/mikemitakidis/algotrading.git algo-trader
cd /opt/algo-trader
bash deploy.sh
```

`deploy.sh` creates the venv, installs dependencies, starts the bot and dashboard, sets up crontab for reboot recovery, and starts the GitHub auto-sync daemon.

### 2. Configure Credentials

Create `/opt/algo-trader/.env`:

```
DASHBOARD_PASSWORD=your_chosen_password
TELEGRAM_BOT_TOKEN=your_token_from_botfather
TELEGRAM_CHAT_ID=your_numeric_chat_id
TELEGRAM_ENABLED=true
```

Telegram is optional — leave blank to disable alerts.

### 3. Sync Updates

```bash
bash ~/algotrading/sync.sh
```

`sync.sh` checks GitHub every 60 seconds and auto-deploys new commits.

---

## Dashboard

URL: `http://<your-server-ip>:8080`  
Password: set in `.env`

### Tabs

| Tab | What it shows |
|-----|---------------|
| **Overview** | Bot phase (scanning/cooldown), live countdown, last cycle summary, system stats |
| **Signals** | All generated signals with filters by symbol/direction/route |
| **Logs** | Live log with colour coding, filter by type |
| **Backtest** | Walk-forward backtest with full analytics |
| **Strategy** | Edit all signal thresholds, confluence rules, ATR risk params |
| **Settings** | Telegram config, dashboard password |

---

## Backtesting

### From the Dashboard

Open the **Backtest** tab. Use a preset or enter symbols manually.

Preset buttons: **AAPL 1yr**, **Mega-cap 5 1yr**, **Mixed 10 1yr**, **90d (15m avail)**

Results include:
- Win rate, profit factor, max drawdown, annualised return
- Equity curve chart with benchmark overlay (SPY for multi-symbol, self for single)
- Monthly breakdown, per-symbol stats, by-timeframe stats
- Trade scatter plot (RSI / ATR / confluence vs return)
- TF availability panel with 15m limit explanation
- Run history table (last 20 runs)
- CSV and JSON export

### From the Command Line

```bash
cd /opt/algo-trader
source venv/bin/activate

# Single symbol
python backtest_cli.py --symbols AAPL --start 2025-01-01 --end 2025-12-31

# Multiple symbols
python backtest_cli.py --symbols AAPL,MSFT,NVDA --start 2025-06-01 --end 2026-03-01

# Named preset (dates auto-set)
python backtest_cli.py --preset aapl1y
python backtest_cli.py --preset mega1y
python backtest_cli.py --preset mixed1y
python backtest_cli.py --preset 90d15m

# Verbose fetch logs
python backtest_cli.py --symbols AAPL --start 2025-01-01 --end 2025-12-31 --verbose

# Skip benchmark comparison
python backtest_cli.py --symbols AAPL --start 2025-01-01 --end 2025-12-31 --no-benchmark
```

**Output:** Console summary + files saved to `data/reports/<timestamp>/`:
- `report.txt` — human-readable summary
- `trades.csv` — trade list
- `results.json` — full JSON results

**Notes:**
- Daily data (1D): up to 2 years history
- Hourly data (1H/4H): up to 730 days
- 15m data: last 60 days only (Yahoo Finance limit)
- First run fetches from Yahoo (30–120s). Subsequent runs use disk cache (instant).
- Cancel mid-run with Ctrl+C (CLI) or Cancel button (dashboard)
- Partial/cancelled/timeout runs are clearly labelled in results

---

## Strategy Configuration

Open **Strategy** in the dashboard to edit:

- **Timeframes**: enable/disable 1D / 4H / 1H / 15m
- **Confluence**: minimum TFs required for a signal (default: 3)
- **Long rules**: RSI range, MACD histogram, EMA trend, VWAP deviation, volume ratio
- **Short rules**: same conditions inverted
- **Risk / ATR**: stop multiplier (default: 2×ATR), target multiplier (default: 3×ATR)
- **Route labels**: ETORO min TFs (default: 4), IBKR min TFs (default: 2)

All changes are versioned with an audit trail. The bot restarts automatically after saving.

---

## File Layout

```
/opt/algo-trader/
├── main.py                  — bot entry point
├── backtest_cli.py          — CLI backtest runner
├── bot/
│   ├── strategy.py          — ALL signal thresholds (single source of truth)
│   ├── scanner.py           — walk-forward scoring engine
│   ├── backtest.py          — backtesting engine (reuses live strategy)
│   ├── indicators.py        — RSI, MACD, EMA, BB, VWAP, OBV, ATR
│   ├── data.py              — yfinance fetcher with browser session + cache
│   └── database.py          — SQLite with auto-migration
├── dashboard/app.py         — Flask dashboard
├── data/
│   ├── signals.db           — signal database
│   ├── strategy.json        — active strategy settings
│   ├── strategy_audit.jsonl — strategy change log
│   ├── bot_state.json       — live bot phase / cycle info
│   ├── backtest_results.json — latest backtest run
│   ├── backtest_history.json — last 20 run summaries
│   ├── bt_cache/            — backtest data cache
│   ├── bar_cache/           — live bot bar cache
│   └── reports/             — timestamped backtest reports
├── logs/
│   ├── bot.log
│   └── dashboard.log
├── deploy.sh                — first-time setup
├── sync.sh                  — GitHub auto-sync daemon
└── .env                     — credentials (never committed)
```

---

## Logs

```bash
tail -f /opt/algo-trader/logs/bot.log
tail -f /opt/algo-trader/logs/dashboard.log
```

Key log prefixes:

| Prefix | Meaning |
|--------|---------|
| `[STARTUP]` | Bot initialising |
| `[CYCLE]` | Scan cycle progress |
| `[SIGNAL]` | Signal generated |
| `[DB]` | Database insert |
| `[BT]` | Backtest activity |
| `[STRATEGY]` | Strategy load/save |
| `[DATA]` | Data fetch / cache |
