#!/bin/bash
# start.sh — Restart bot and dashboard
# Called by: dashboard Restart button, sync.sh on new deploy
# Always installs/upgrades dependencies before starting.

BASE=/opt/algo-trader
VENV=$BASE/venv
LOG=$BASE/logs/bot.log

mkdir -p $BASE/logs $BASE/data
echo "$(date): === start.sh ===" >> $LOG

# ── Install / upgrade dependencies ───────────────────────────────────────────
echo "$(date): Installing dependencies..." >> $LOG
$VENV/bin/pip install -r $BASE/requirements.txt --quiet >> $LOG 2>&1
echo "$(date): pip done" >> $LOG

# ── Verify imports before starting ───────────────────────────────────────────
$VENV/bin/python3 -c "import yfinance, pandas, numpy, flask, dotenv, requests" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date): ERROR — import check failed" >> $LOG
    exit 1
fi
echo "$(date): Imports OK" >> $LOG

# ── Kill existing bot only (not dashboard — dashboard called this) ────────────
pkill -f "python3.*main.py" 2>/dev/null || true
sleep 2

# ── Start bot ─────────────────────────────────────────────────────────────────
nohup $VENV/bin/python3 $BASE/main.py > /dev/null 2>&1 &
echo "$(date): Bot started PID=$!" >> $LOG
