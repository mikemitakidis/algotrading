#!/bin/bash
# deploy.sh — First-time setup and start
# Safe to re-run. Creates minimal .env if missing.

set -e

BASE=/opt/algo-trader
VENV=$BASE/venv
LOG=$BASE/logs/boot.log

mkdir -p $BASE/logs $BASE/data
echo "$(date): === deploy.sh ===" | tee -a $LOG

# ── 1. Create .env if missing (bot will warn but still start) ─────────────────
if [ ! -f "$BASE/.env" ]; then
    echo "$(date): .env not found — creating minimal .env with defaults" | tee -a $LOG
    cat > $BASE/.env << 'ENVEOF'
# Auto-created by deploy.sh
# Edit this file to set your real values
DASHBOARD_PASSWORD=changeme
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
ENVEOF
    echo "$(date): .env created at $BASE/.env — edit it to set your password and Telegram keys" | tee -a $LOG
fi

# ── 2. Create venv if missing ─────────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
    echo "$(date): Creating Python venv..." | tee -a $LOG
    python3 -m venv $VENV
fi

# ── 3. Install dependencies ───────────────────────────────────────────────────
echo "$(date): Installing dependencies..." | tee -a $LOG
$VENV/bin/pip install --upgrade pip --quiet
$VENV/bin/pip install -r $BASE/requirements.txt --quiet
echo "$(date): Dependencies installed" | tee -a $LOG

# ── 4. Verify imports ─────────────────────────────────────────────────────────
echo "$(date): Verifying imports..." | tee -a $LOG
$VENV/bin/python3 -c "
import yfinance, pandas, numpy, flask, dotenv, requests
print('  yfinance:', yfinance.__version__)
print('  pandas:  ', pandas.__version__)
print('  flask:   ', flask.__version__)
print('  All imports OK')
" 2>&1 | tee -a $LOG

# ── 5. Kill existing processes ────────────────────────────────────────────────
pkill -f "python3.*main.py"   2>/dev/null || true
pkill -f "python3.*app.py"    2>/dev/null || true
sleep 1

# ── 6. Start bot ──────────────────────────────────────────────────────────────
nohup $VENV/bin/python3 $BASE/main.py > /dev/null 2>&1 &
echo "$(date): Bot started PID=$!" | tee -a $LOG

# ── 7. Start dashboard ────────────────────────────────────────────────────────
nohup $VENV/bin/python3 $BASE/dashboard/app.py >> $BASE/logs/dashboard.log 2>&1 &
echo "$(date): Dashboard started PID=$!" | tee -a $LOG

# ── 8. Crontab for reboot recovery ───────────────────────────────────────────
CRON="@reboot sleep 15 && bash $BASE/deploy.sh >> $BASE/logs/boot.log 2>&1"
( crontab -l 2>/dev/null | grep -v "deploy.sh" ; echo "$CRON" ) | crontab -
echo "$(date): Crontab set" | tee -a $LOG

# ── 9. Start sync daemon ──────────────────────────────────────────────────────
pkill -f "sync.sh" 2>/dev/null || true
sleep 1
nohup bash $BASE/sync.sh >> $BASE/logs/sync.log 2>&1 &
echo "$(date): Sync daemon started PID=$!" | tee -a $LOG

echo "" | tee -a $LOG
echo "$(date): === DONE ===" | tee -a $LOG
echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):8080  (password: changeme unless you edited .env)" | tee -a $LOG
echo "  Bot log:   tail -f $BASE/logs/bot.log" | tee -a $LOG
