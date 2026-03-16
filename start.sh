#!/bin/bash
BASE=/opt/algo-trader
LOG=$BASE/logs/bot.log
mkdir -p $BASE/logs $BASE/data
cd $BASE

echo "$(date): Starting..." >> $LOG

# Stash local changes, pull, restore — no more git conflicts ever
git stash -q 2>/dev/null || true
git pull origin main -q >> $LOG 2>&1
git stash pop -q 2>/dev/null || true

echo "$(date): Code updated. Commit: $(git rev-parse --short HEAD)" >> $LOG

# Kill only the bot (not dashboard — dashboard called this)
pkill -f "python3.*main.py" 2>/dev/null || true
sleep 2

source $BASE/venv/bin/activate
nohup python3 $BASE/main.py >> $BASE/logs/bot.log 2>&1 &
echo "$(date): Bot started PID $!" >> $LOG
