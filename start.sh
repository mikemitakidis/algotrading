#!/bin/bash
# Algo Trader v2 — Startup Script
# Downloads latest code directly from GitHub (bypasses git conflicts)

BASE=/opt/algo-trader
LOG=$BASE/logs/bot.log
RAW="https://raw.githubusercontent.com/mikemitakidis/algotrading/main"

mkdir -p $BASE/logs $BASE/data
echo "$(date): === STARTING ===" >> $LOG

# Download latest files directly from GitHub raw URLs
echo "$(date): Downloading latest code from GitHub..." >> $LOG
wget -q -O $BASE/main.py      "$RAW/main.py"      && echo "$(date): main.py updated" >> $LOG
wget -q -O $BASE/backtest.py  "$RAW/backtest.py"   && echo "$(date): backtest.py updated" >> $LOG

echo "$(date): Download done." >> $LOG

# Kill only the bot process
pkill -f "python3.*main.py" 2>/dev/null || true
sleep 2

# Start the bot
source $BASE/venv/bin/activate
nohup python3 $BASE/main.py >> $BASE/logs/bot.log 2>&1 &
echo "$(date): Bot started (PID $!)" >> $LOG
