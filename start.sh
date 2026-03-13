#!/bin/bash
# Algo Trader v2 — Start Script
# Credentials live ONLY on the server in /opt/algo-trader/.env
# They are never stored in GitHub.

BASE=/opt/algo-trader
ENV_FILE=$BASE/.env
CFG=$BASE/config/settings.yaml

echo "=== Algo Trader v2 Starting ==="

# Load credentials from .env and inject into settings.yaml
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
    # Inject credentials into settings.yaml (keeps GitHub version clean)
    python3 - << PYEOF
import yaml, os
with open('$CFG') as f:
    cfg = yaml.safe_load(f)
cfg['alpaca']['api_key']   = os.environ.get('ALPACA_API_KEY', '')
cfg['alpaca']['secret_key'] = os.environ.get('ALPACA_SECRET_KEY', '')
cfg['dashboard']['password'] = os.environ.get('DASHBOARD_PASSWORD', 'AlgoTrader2024!')
cfg['telegram']['token']   = os.environ.get('TELEGRAM_TOKEN', '')
cfg['telegram']['chat_id'] = os.environ.get('TELEGRAM_CHAT_ID', '')
with open('$CFG', 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False)
print("Credentials injected from .env")
PYEOF
else
    echo "WARNING: .env file not found at $ENV_FILE"
    echo "Credentials must be set manually in settings.yaml"
fi

# Kill existing processes
pkill -f "python3.*main.py"    || true
pkill -f "python3.*dashboard"  || true
sleep 1

# Activate venv
source $BASE/venv/bin/activate

# Start bot
nohup python3 $BASE/main.py > /dev/null 2>&1 &
echo "Bot started (PID $!)"

# Start dashboard
nohup python3 $BASE/dashboard.py > /dev/null 2>&1 &
echo "Dashboard started (PID $!)"

echo "=== All services running ==="
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):8080"
