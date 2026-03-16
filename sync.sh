#!/bin/bash
# Algo Trader — Auto Sync Daemon
BASE=/opt/algo-trader
LOG=/var/log/algo-sync.log

echo "$(date): Sync daemon started" >> $LOG

while true; do
    cd $BASE
    git fetch origin main -q 2>/dev/null

    LOCAL=$(git rev-parse HEAD 2>/dev/null)
    REMOTE=$(git rev-parse origin/main 2>/dev/null)

    if [ "$LOCAL" != "$REMOTE" ]; then
        echo "$(date): New code detected ($LOCAL -> $REMOTE)" >> $LOG

        # Force update — settings.yaml is in .gitignore so never conflicts
        git reset --hard origin/main -q >> $LOG 2>&1
        echo "$(date): Code updated to $(git rev-parse --short HEAD)" >> $LOG

        # Verify yfinance is in main.py
        if grep -q "yfinance" $BASE/main.py; then
            echo "$(date): OK - main.py has yfinance" >> $LOG
        else
            echo "$(date): ERROR - main.py missing yfinance!" >> $LOG
        fi

        # Restart services
        pkill -f "python3.*main.py"    2>/dev/null || true
        pkill -f "python3.*dashboard"  2>/dev/null || true
        sleep 2

        source $BASE/venv/bin/activate
        nohup python3 $BASE/main.py      >> $BASE/logs/bot.log       2>&1 &
        nohup python3 $BASE/dashboard.py >> $BASE/logs/dashboard.log 2>&1 &

        echo "$(date): Services restarted" >> $LOG
    fi

    sleep 60
done
