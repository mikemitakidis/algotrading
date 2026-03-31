# Algo Trader v1

Multi-timeframe algorithmic trading bot. Currently running IBKR paper trading (account DUP623346). Live trading safety envelope built (M12), not yet activated.
Milestones 1–11 complete. M12 safety envelope built. See [ROADMAP.md](ROADMAP.md) for the full 15-milestone plan.

## Current Status

| Milestone | Name | Status |
|-----------|------|--------|
| 1 | Stable Shadow-Mode Scanner | ✅ Complete |
| 2 | Telegram from Dashboard | ✅ Complete |
| 3 | Dashboard Observability | ✅ Complete |
| 4 | Strategy Engine | ✅ Complete |
| 5 | Backtesting | ✅ Complete |
| 6  | Modular Data-Provider Architecture | ✅ Complete |
| 7  | Feature Engine + ML Logging        | ✅ Complete |
| 8  | News/Sentiment Module              | ✅ Complete |
| 9  | ML Meta-Labeling Pipeline          | ✅ Baseline complete |
| 10 | Broker Execution + Data Flywheel   | ✅ Complete |
| 11 | IBKR Paper Trading                 | ✅ Active (DUP623346) |
| 12 | IBKR Live Trading                  | 🔒 Safety envelope built, not activated |
| 13–15 | See ROADMAP.md                  | ⬜ Planned |

## What the Bot Does

- Scans 89 curated large-cap US symbols every 15 minutes
- Computes RSI, MACD, EMA20/50, Bollinger Bands, VWAP deviation, OBV, ATR across 4 timeframes (15m, 1H, 4H, 1D)
- Generates signals when ≥ 2–3 timeframes agree (configurable from dashboard)
- Logs every signal to SQLite with full indicator snapshot
- Sends Telegram alerts for manual review on eToro
- Routes signals: ETORO (4/4 TFs) or IBKR placeholder (2–3/4 TFs)

## What It Does NOT Do

- No live-money trades yet. IBKR paper trading active via IB Gateway (M11).
- No ML filtering yet (Milestone 9)
- Automated paper execution via IBKR bracket orders (M11 active). Live execution ready behind safety gate (M12).

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

# Data provider (default: yfinance — no API key needed)
DATA_PROVIDER=yfinance
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

---

## Backtesting

### From the Dashboard

Open the **Backtest** tab. Select symbols and date range or use a preset. Results include:
- Trades table, equity curve, trade scatter plot
- Monthly breakdown, per-symbol stats, by-timeframe stats
- TF availability panel with 15m limit explanation
- Run history table (last 20 runs)
- CSV and JSON export

### From the Command Line

```bash
cd /opt/algo-trader
source venv/bin/activate

# Single symbol
python backtest_cli_v2.py --symbols AAPL --start 2025-01-01 --end 2025-12-31

# Multiple symbols
python backtest_cli_v2.py --symbols AAPL,MSFT,NVDA --start 2025-06-01 --end 2026-03-01

# Named preset (dates auto-set)
python backtest_cli_v2.py --preset aapl1y
python backtest_cli_v2.py --preset mega1y
python backtest_cli_v2.py --preset mixed1y
python backtest_cli_v2.py --preset 90d15m

# Verbose fetch logs
python backtest_cli_v2.py --symbols AAPL --start 2025-01-01 --end 2025-12-31 --verbose
```

**Output:** Console summary + files saved to `data/reports/<timestamp>/`:
- `report.txt` — human-readable summary
- `trades.csv` — trade list
- `results.json` — full JSON results

**Notes:**
- Daily data (1D): up to 730 days history
- Hourly data (1H/4H): up to 730 days
- 15m data: last 60 days only (Yahoo Finance limit)
- First run fetches from Yahoo or the live bot's bar cache. Subsequent runs use disk cache.
- Cancel mid-run with Ctrl+C (CLI) or Cancel button (dashboard)

---

## Data Provider Architecture (Milestone 6)

All market data flows through a provider abstraction layer. The active provider
is selected by `DATA_PROVIDER` in `.env` (default: `yfinance`).

```
scanner / backtest
       │
       ▼
 bot/data.py          ← thin delegation shim (public API unchanged)
       │
       ▼
bot/providers/
  ├── __init__.py           factory: get_provider(), get_provider_name()
  ├── base.py               abstract DataProvider interface
  ├── yfinance_provider.py  active default — Yahoo Finance via yfinance
  └── alpaca_provider.py    placeholder — Milestone 11, not yet implemented
```

### Switching providers

```bash
# In .env:
DATA_PROVIDER=yfinance   # default, always works
DATA_PROVIDER=alpaca     # placeholder — returns 'not_implemented' until M11
```

No strategy, scanner, or backtest code needs editing when the provider changes.

### Provider capabilities

Each provider exposes a `capabilities` dict (visible via `/api/provider`):

| Field | yfinance |
|---|---|
| Supported timeframes | 1d, 1h, 15m |
| Max history (1d/1h) | 730 days |
| Max history (15m) | 60 days |
| Intraday | ✅ |
| Benchmark | ✅ |
| Real-time | ❌ |

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
├── main.py                      — bot entry point
├── backtest_cli_v2.py           — CLI backtest runner (active)
├── bot/
│   ├── strategy.py              — ALL signal thresholds (single source of truth)
│   ├── scanner.py               — live scan cycle (uses bot.data → provider)
│   ├── indicators.py            — RSI, MACD, EMA, BB, VWAP, OBV, ATR
│   ├── data.py                  — market data shim (delegates to provider layer)
│   ├── backtest_v2.py           — walk-forward backtest engine (uses provider)
│   ├── backtest_job.py          — backtest thread wrapper for dashboard
│   ├── database.py              — SQLite with auto-migration
│   └── providers/
│       ├── __init__.py          — get_provider() factory
│       ├── base.py              — abstract DataProvider interface
│       ├── yfinance_provider.py — Yahoo Finance (active default)
│       └── alpaca_provider.py   — Alpaca placeholder (Milestone 11)
├── dashboard/app.py             — Flask dashboard (all tabs)
├── data/
│   ├── signals.db               — signal database
│   ├── strategy.json            — active strategy settings
│   ├── strategy_audit.jsonl     — strategy change log
│   ├── bar_cache/               — live scanner bar cache (sym_interval.json)
│   ├── bt_v2_cache/             — backtest data cache (date-range keyed)
│   ├── backtest_history.json    — last 20 run summaries
│   └── reports/                 — timestamped backtest report folders
├── logs/
│   ├── bot.log
│   └── dashboard.log
├── deploy.sh                    — first-time setup
├── sync.sh                      — GitHub auto-sync daemon
└── .env                         — credentials (never committed)
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
| `[BT2]` | Backtest v2 activity |
| `[PROV]` | Provider fetch activity |
| `[STRATEGY]` | Strategy load/save |
| `[DATA]` | Data fetch / cache |
