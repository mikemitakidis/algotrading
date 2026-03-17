# Deployment Guide

## Server Details

- Provider: Hetzner VPS
- OS: Ubuntu 24.04 LTS
- Python: 3.12
- Install path: `/opt/algo-trader`
- Venv: `/opt/algo-trader/venv`
- Dashboard: `http://<server-ip>:8080`

## Prerequisites (one-time, already done)

```bash
apt-get update && apt-get install -y python3.12 python3.12-venv python3-pip git wget
python3 -m venv /opt/algo-trader/venv
```

## Setup Steps

### 1. Clone repo to server

```bash
cd /opt
git clone https://github.com/mikemitakidis/algotrading.git algo-trader
cd /opt/algo-trader
git checkout rebuild-from-zero   # use this branch until approved for main
```

### 2. Create .env file (NEVER commit this file)

```bash
cp .env.example .env
nano .env
```

Fill in all values. See `.env.example` for required keys.

### 3. Run deploy.sh (installs all dependencies)

```bash
bash deploy.sh
```

This script:
- Activates venv
- Runs `pip install -r requirements.txt`
- Verifies all imports succeed
- Starts bot and dashboard
- Sets up crontab for reboot recovery

### 4. Verify

```bash
tail -f /opt/algo-trader/logs/bot.log
```

Expected output within 3 minutes:
```
[STARTUP] Config loaded. All required keys present.
[STARTUP] DB initialised at data/signals.db
[STARTUP] Connectivity test: AAPL=90bars MSFT=90bars NVDA=90bars
[TIER-A] Ranking 1210 symbols on daily bars...
[TIER-A] Scored: 980 symbols. Focus set: 150. Top 5: [...]
[CYCLE-1] Scanning 150 symbols across 4 timeframes...
[CYCLE-1] 1D: 148 symbols | 4H: 145 symbols | 1H: 143 symbols | 15m: 140 symbols
[CYCLE-1] Symbols with ≥1 valid TF: 42
[CYCLE-1] Signals generated: 3
[SIGNAL] ETORO NVDA LONG 4/4 TF | RSI:62.1 Price:$891.20 SL:$878.40 TP:$917.80
[DB] Signal inserted: id=1
[TELEGRAM] Alert sent for NVDA LONG
[CYCLE-1] Done. Next cycle in 15 min.
```

## Auto-Sync Setup

`sync.sh` runs as a background daemon, checks GitHub every 60 seconds:

```bash
nohup bash /opt/algo-trader/sync.sh >> /opt/algo-trader/logs/sync.log 2>&1 &
```

On new commit detected:
1. `git reset --hard origin/rebuild-from-zero`
2. `pip install -r requirements.txt`
3. Restart bot and dashboard

## Crontab (reboot recovery)

```bash
crontab -e
```

Add:
```
@reboot sleep 10 && bash /opt/algo-trader/deploy.sh >> /opt/algo-trader/logs/boot.log 2>&1
```

## Secrets Management

All secrets in `/opt/algo-trader/.env` only. Never in:
- Any Python file
- Any YAML file
- Any shell script
- GitHub

`.env` is in `.gitignore`. It is never tracked.

## Required .env Keys

```
TELEGRAM_TOKEN=        # from @BotFather
TELEGRAM_CHAT_ID=      # your personal chat ID
DASHBOARD_PASSWORD=    # choose any password
```

Optional (not used in V1):
```
ALPACA_API_KEY=        # reserved for future IBKR/Alpaca execution
ALPACA_SECRET_KEY=     # reserved for future use
```
