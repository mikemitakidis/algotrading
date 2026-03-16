#!/bin/bash
BASE=/opt/algo-trader
LOG=/var/log/algo-sync.log
cd $BASE
echo "$(date): Sync daemon started" >> $LOG
while true; do
    git fetch origin main -q 2>/dev/null
    LOCAL=$(git rev-parse HEAD 2>/dev/null)
    REMOTE=$(git rev-parse origin/main 2>/dev/null)
    if [ "$LOCAL" != "$REMOTE" ]; then
        echo "$(date): Update detected. Deploying..." >> $LOG
        git reset --hard origin/main >> $LOG 2>&1
        echo "$(date): Deployed $(git rev-parse --short HEAD)" >> $LOG
        pkill -f "python3.*main.py"   2>/dev/null || true
        pkill -f "python3.*dashboard" 2>/dev/null || true
        sleep 2
        source $BASE/venv/bin/activate
        nohup python3 $BASE/dashboard.py >> $BASE/logs/dashboard.log 2>&1 &
        nohup python3 $BASE/main.py      >> $BASE/logs/bot.log       2>&1 &
        echo "$(date): Restarted. Bot:$(pgrep -f main.py) Dash:$(pgrep -f dashboard.py)" >> $LOG
    fi
    sleep 30
done
