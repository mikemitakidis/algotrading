#!/bin/bash
set -e
echo "=========================================="
echo "  ALGO TRADER - SERVER SETUP"
echo "=========================================="

REPO_DIR="/opt/algo-trader"
TOKEN_FILE="$REPO_DIR/.github_token"

if [ ! -f "$TOKEN_FILE" ]; then
    echo "ERROR: Token file missing. It was created during setup at $TOKEN_FILE"
    exit 1
fi

GITHUB_TOKEN=$(cat "$TOKEN_FILE")
REPO_URL="https://${GITHUB_TOKEN}:x-oauth-basic@github.com/mikemitakidis/algotrading.git"

echo "[1/6] Stopping existing services..."
pkill -f "python3 main.py" 2>/dev/null || true
pkill -f "dashboard.py" 2>/dev/null || true
pkill -f "sync.sh" 2>/dev/null || true
sleep 1

echo "[2/6] Pulling latest from GitHub..."
cd "$REPO_DIR"
git remote set-url origin "$REPO_URL" 2>/dev/null || git remote add origin "$REPO_URL"
git fetch origin main
git checkout -f main 2>/dev/null || git checkout --track origin/main
git pull origin main --force
echo "GitHub sync: OK"

echo "[3/6] Setting permissions..."
chmod +x "$REPO_DIR/start.sh"
chmod +x "$REPO_DIR/sync.sh"

echo "[4/6] Starting auto-sync daemon..."
nohup bash "$REPO_DIR/sync.sh" >> /var/log/algo-sync.log 2>&1 &
echo "Auto-sync PID: $!"

echo "[5/6] Starting bot and dashboard..."
source "$REPO_DIR/venv/bin/activate"
python3 "$REPO_DIR/dashboard.py" &
sleep 2
python3 "$REPO_DIR/main.py" >> "$REPO_DIR/logs/bot.log" 2>&1 &

echo "[6/6] Adding to crontab..."
(crontab -l 2>/dev/null | grep -v algo; echo "@reboot bash /opt/algo-trader/start.sh && nohup bash /opt/algo-trader/sync.sh >> /var/log/algo-sync.log 2>&1 &") | crontab -

echo ""
echo "=========================================="
echo "  ALL DONE!"
echo "  Dashboard: http://138.199.196.95:8080"
echo "  Password:  AlgoTrader2024!"
echo "  Auto-sync: Active (checks GitHub every 60s)"
echo "=========================================="
