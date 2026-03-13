#!/bin/bash
# Algo Trader v2 — Start Script
# Always pulls latest code from GitHub before starting

BASE=/opt/algo-trader
CFG=$BASE/config/settings.yaml
LOG=$BASE/logs/bot.log

mkdir -p $BASE/logs $BASE/data

echo "$(date): === Algo Trader v2 Starting ===" >> $LOG

# Step 1: Save credentials before git pull (so they survive the pull)
SECRET_KEY=$(python3 -c "
import yaml
try:
    c = yaml.safe_load(open('$CFG'))
    print(c.get('alpaca',{}).get('secret_key',''))
except: print('')
" 2>/dev/null)

DASH_PASS=$(python3 -c "
import yaml
try:
    c = yaml.safe_load(open('$CFG'))
    print(c.get('dashboard',{}).get('password','AlgoTrader2024!'))
except: print('AlgoTrader2024!')
" 2>/dev/null)

# Step 2: Pull latest code from GitHub
echo "$(date): Pulling latest code from GitHub..." >> $LOG
cd $BASE
git pull origin main -q >> $LOG 2>&1
echo "$(date): Git pull done. Current commit: $(git rev-parse --short HEAD)" >> $LOG

# Step 3: Restore credentials after pull
python3 - << PYEOF 2>/dev/null
import yaml
with open('$CFG') as f:
    cfg = yaml.safe_load(f)
if '$SECRET_KEY':
    cfg.setdefault('alpaca', {})['secret_key'] = '$SECRET_KEY'
if '$DASH_PASS':
    cfg.setdefault('dashboard', {})['password'] = '$DASH_PASS'
with open('$CFG', 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False)
PYEOF

# Step 4: Kill existing processes
pkill -f "python3.*main.py"    2>/dev/null || true
pkill -f "python3.*dashboard"  2>/dev/null || true
sleep 2

# Step 5: Start services
source $BASE/venv/bin/activate
nohup python3 $BASE/main.py      >> $BASE/logs/bot.log       2>&1 &
nohup python3 $BASE/dashboard.py >> $BASE/logs/dashboard.log 2>&1 &

echo "$(date): Bot and dashboard started." >> $LOG
echo "$(date): Dashboard: http://$(hostname -I | awk '{print $1}'):8080" >> $LOG
