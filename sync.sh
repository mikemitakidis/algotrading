#!/bin/bash
# sync.sh — GitHub auto-sync daemon
# Checks GitHub every 60 seconds.
# On new commit: pull, install deps, restart bot and dashboard.
# Run as background process: nohup bash sync.sh >> logs/sync.log 2>&1 &

BASE=/opt/algo-trader
VENV=$BASE/venv
LOG=$BASE/logs/sync.log
BRANCH=rebuild-from-zero   # change to main after milestone approval

mkdir -p $BASE/logs
echo "$(date): Sync daemon started (branch: $BRANCH)" >> $LOG

while true; do
    cd $BASE

    git fetch origin $BRANCH -q 2>/dev/null

    LOCAL=$(git rev-parse HEAD 2>/dev/null)
    REMOTE=$(git rev-parse origin/$BRANCH 2>/dev/null)

    if [ "$LOCAL" != "$REMOTE" ]; then
        echo "$(date): New commit detected ($LOCAL -> $REMOTE)" >> $LOG

        # Force update — .env is gitignored so never conflicts
        git reset --hard origin/$BRANCH >> $LOG 2>&1
        NEW=$(git rev-parse --short HEAD)
        echo "$(date): Updated to $NEW" >> $LOG

        # Install dependencies in deployment path
        echo "$(date): Installing dependencies..." >> $LOG
        $VENV/bin/pip install -r $BASE/requirements.txt --quiet >> $LOG 2>&1
        echo "$(date): pip done" >> $LOG

        # Verify imports before restarting
        $VENV/bin/python3 -c "import yfinance, pandas, numpy, flask, dotenv, requests" >> $LOG 2>&1
        if [ $? -ne 0 ]; then
            echo "$(date): ERROR — import check failed after update. Not restarting." >> $LOG
            sleep 60
            continue
        fi

        # Restart both services
        pkill -f "python3.*main.py"    2>/dev/null || true
        pkill -f "python3.*dashboard"  2>/dev/null || true
        sleep 2

        nohup $VENV/bin/python3 $BASE/dashboard/app.py >> $BASE/logs/dashboard.log 2>&1 &
        nohup $VENV/bin/python3 $BASE/main.py          >> $BASE/logs/bot.log       2>&1 &

        echo "$(date): Restarted. Bot:$(pgrep -f main.py) Dash:$(pgrep -f dashboard)" >> $LOG
    fi

    sleep 60
done
