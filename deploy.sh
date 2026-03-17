#!/bin/bash
# deploy.sh — One-time setup and first start
# Run this once after cloning the repo to the server.
# Safe to re-run: pip install is idempotent.

set -e

BASE=/opt/algo-trader
VENV=$BASE/venv
LOG=$BASE/logs/boot.log

mkdir -p $BASE/logs $BASE/data
echo "$(date): === deploy.sh starting ===" | tee -a $LOG

# ── 1. Check .env exists ──────────────────────────────────────────────────────
if [ ! -f "$BASE/.env" ]; then
    echo "ERROR: .env file not found at $BASE/.env" | tee -a $LOG
    echo "Copy .env.example to .env and fill in your values, then re-run." | tee -a $LOG
    exit 1
fi
echo "$(date): .env found" | tee -a $LOG

# ── 2. Create venv if missing ─────────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
    echo "$(date): Creating venv..." | tee -a $LOG
    python3 -m venv $VENV
fi

# ── 3. Install dependencies ───────────────────────────────────────────────────
echo "$(date): Installing dependencies from requirements.txt..." | tee -a $LOG
$VENV/bin/pip install --upgrade pip --quiet
$VENV/bin/pip install -r $BASE/requirements.txt --quiet
echo "$(date): pip install done" | tee -a $LOG

# ── 4. Verify all imports succeed ────────────────────────────────────────────
echo "$(date): Verifying imports..." | tee -a $LOG
$VENV/bin/python3 -c "
import yfinance, pandas, numpy, flask, dotenv, requests
print('  yfinance:', yfinance.__version__)
print('  pandas:  ', pandas.__version__)
print('  numpy:   ', numpy.__version__)
print('  flask:   ', flask.__version__)
print('All imports OK')
" 2>&1 | tee -a $LOG

if [ $? -ne 0 ]; then
    echo "ERROR: Import verification failed. Check log above." | tee -a $LOG
    exit 1
fi

# ── 5. Kill any existing processes ───────────────────────────────────────────
pkill -f "python3.*main.py"    2>/dev/null || true
pkill -f "python3.*dashboard"  2>/dev/null || true
sleep 1

# ── 6. Start bot ──────────────────────────────────────────────────────────────
echo "$(date): Starting bot..." | tee -a $LOG
nohup $VENV/bin/python3 $BASE/main.py >> $BASE/logs/bot.log 2>&1 &
BOT_PID=$!
echo "$(date): Bot started PID=$BOT_PID" | tee -a $LOG

# ── 7. Start dashboard ────────────────────────────────────────────────────────
echo "$(date): Starting dashboard..." | tee -a $LOG
nohup $VENV/bin/python3 $BASE/dashboard/app.py >> $BASE/logs/dashboard.log 2>&1 &
DASH_PID=$!
echo "$(date): Dashboard started PID=$DASH_PID" | tee -a $LOG

# ── 8. Set up crontab for reboot recovery ────────────────────────────────────
CRON_JOB="@reboot sleep 15 && bash $BASE/deploy.sh >> $BASE/logs/boot.log 2>&1"
( crontab -l 2>/dev/null | grep -v "deploy.sh" ; echo "$CRON_JOB" ) | crontab -
echo "$(date): Crontab set for reboot recovery" | tee -a $LOG

# ── 9. Start sync daemon ──────────────────────────────────────────────────────
pkill -f "sync.sh" 2>/dev/null || true
sleep 1
nohup bash $BASE/sync.sh >> $BASE/logs/sync.log 2>&1 &
echo "$(date): Sync daemon started PID=$!" | tee -a $LOG

echo "$(date): === deploy.sh complete ===" | tee -a $LOG
echo ""
echo "  Bot log:       tail -f $BASE/logs/bot.log"
echo "  Dashboard:     http://$(hostname -I | awk '{print $1}'):8080"
echo ""
