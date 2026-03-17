# Algo Trader v1

Multi-timeframe trading signal scanner. Shadow mode only.

## What It Does

- Scans ~1,200 US equity symbols every 15 minutes
- Computes RSI, MACD, EMA, Bollinger, VWAP, OBV, ATR across 4 timeframes (15m, 1H, 4H, 1D)
- Generates signals when ≥3 timeframes align
- Logs every signal to SQLite
- Sends Telegram alerts for manual review
- Web dashboard on port 8080

## What It Does NOT Do

No live trades. No broker connections. Shadow mode only.

## Server Requirements

- Ubuntu 24.04 VPS
- Python 3.12+
- git, wget installed

## Setup (One Time)

### 1. Clone the repo

```bash
cd /opt
git clone https://github.com/mikemitakidis/algotrading.git algo-trader
cd /opt/algo-trader
git checkout rebuild-from-zero
```

### 2. Create .env

```bash
cp .env.example .env
nano .env
```

Fill in:
```
TELEGRAM_TOKEN=your_token_from_botfather
TELEGRAM_CHAT_ID=your_chat_id
DASHBOARD_PASSWORD=your_chosen_password
```

Telegram is optional — leave blank to disable alerts.

### 3. Run deploy.sh

```bash
bash deploy.sh
```

This installs all Python dependencies, starts the bot and dashboard, sets up the crontab for reboot recovery, and starts the GitHub auto-sync daemon.

### 4. Verify

```bash
tail -f /opt/algo-trader/logs/bot.log
```

Expected output within 3 minutes:
```
ALGO TRADER v1 — SHADOW MODE — STARTING
Config loaded. All required keys present.
yfinance OK: AAPL:5bars MSFT:5bars NVDA:5bars SPY:5bars QQQ:5bars
[TIER-A] Ranking 1210 symbols...
[TIER-A] Focus set: 150
[CYCLE] Scanning 150 symbols across 4 timeframes...
[SIGNAL] ETORO NVDA LONG 4/4 TF | RSI:62.1 Price:$891.20
```

## Dashboard

URL: `http://<your-server-ip>:8080`
Password: whatever you set in `.env`

## Get Your Telegram Chat ID

1. Message `@BotFather` on Telegram → `/newbot` → get your token
2. Message your bot once
3. Visit: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
4. Find `"id"` inside `"chat"` — that is your chat ID

## Files

```
main.py               Entry point, scan loop
bot/config.py         Loads .env, validates keys
bot/data.py           yfinance fetch
bot/indicators.py     RSI, MACD, EMA, BB, VWAP, OBV, ATR
bot/scanner.py        Scoring, Tier A/B, signal generation
bot/database.py       SQLite init, insert, query
bot/notifier.py       Telegram alerts
dashboard/app.py      Flask web dashboard
deploy.sh             One-time setup
start.sh              Restart bot
sync.sh               GitHub auto-sync daemon
requirements.txt      Pinned Python dependencies
.env.example          Secrets template
```

## Branches

- `rebuild-from-zero` — current clean build (this branch)
- `archive-broken-implementation` — old broken code, kept for reference only
- `main` — will be updated after milestone approval

## Logs

```bash
tail -f logs/bot.log        # main bot activity
tail -f logs/dashboard.log  # dashboard process
tail -f logs/sync.log       # GitHub auto-sync
```
