#!/bin/bash
# Auto-sync from GitHub every 60 seconds
REPO_DIR="/opt/algo-trader"
TOKEN_FILE="/opt/algo-trader/.github_token"

if [ ! -f "$TOKEN_FILE" ]; then
    echo "$(date): ERROR - Token file not found at $TOKEN_FILE" >> /var/log/algo-sync.log
    exit 1
fi

GITHUB_TOKEN=$(cat "$TOKEN_FILE")
REPO_URL="https://${GITHUB_TOKEN}:x-oauth-basic@github.com/mikemitakidis/algotrading.git"

echo "$(date): Sync service started" >> /var/log/algo-sync.log

while true; do
    cd "$REPO_DIR"
    git remote set-url origin "$REPO_URL" 2>/dev/null
    git fetch origin main --quiet 2>/dev/null
    LOCAL=$(git rev-parse HEAD 2>/dev/null)
    REMOTE=$(git rev-parse origin/main 2>/dev/null)
    
    if [ "$LOCAL" != "$REMOTE" ]; then
        echo "$(date): Changes detected, pulling..." >> /var/log/algo-sync.log
        git pull origin main --quiet 2>/dev/null
        pkill -f "dashboard.py" 2>/dev/null; pkill -f "python3 main.py" 2>/dev/null
        sleep 1
        source venv/bin/activate
        python3 dashboard.py &
        sleep 2
        python3 main.py >> logs/bot.log 2>&1 &
        echo "$(date): Services restarted after update" >> /var/log/algo-sync.log
    fi
    sleep 60
done
