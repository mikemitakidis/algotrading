#!/bin/bash
# Algo Trader v2 — GitHub Auto-Sync Daemon
# Checks GitHub every 60 seconds. On change: pulls, preserves credentials, restarts.

BASE=/opt/algo-trader
CFG=$BASE/config/settings.yaml
LOG=/var/log/algo-sync.log

echo "$(date): Sync daemon started" >> $LOG

while true; do
    cd $BASE

    # Step 1: Save credentials that exist only on this server
    SECRET_KEY=$(python3 -c "
import yaml
try:
    with open('$CFG') as f:
        c = yaml.safe_load(f)
    print(c.get('alpaca',{}).get('secret_key',''))
except: print('')
" 2>/dev/null)

    DASH_PASS=$(python3 -c "
import yaml
try:
    with open('$CFG') as f:
        c = yaml.safe_load(f)
    print(c.get('dashboard',{}).get('password','AlgoTrader2024!'))
except: print('AlgoTrader2024!')
" 2>/dev/null)

    TG_TOKEN=$(python3 -c "
import yaml
try:
    with open('$CFG') as f:
        c = yaml.safe_load(f)
    print(c.get('telegram',{}).get('token',''))
except: print('')
" 2>/dev/null)

    TG_CHAT=$(python3 -c "
import yaml
try:
    with open('$CFG') as f:
        c = yaml.safe_load(f)
    print(c.get('telegram',{}).get('chat_id',''))
except: print('')
" 2>/dev/null)

    # Step 2: Check for GitHub changes
    git fetch origin main -q 2>/dev/null
    LOCAL=$(git rev-parse HEAD 2>/dev/null)
    REMOTE=$(git rev-parse origin/main 2>/dev/null)

    if [ "$LOCAL" != "$REMOTE" ]; then
        echo "$(date): Change detected — pulling..." >> $LOG
        git pull origin main -q >> $LOG 2>&1

        # Step 3: Re-inject saved credentials back into settings.yaml
        python3 - << PYEOF
import yaml
with open('$CFG') as f:
    cfg = yaml.safe_load(f)
if '$SECRET_KEY':
    cfg['alpaca']['secret_key'] = '$SECRET_KEY'
if '$DASH_PASS':
    cfg.setdefault('dashboard', {})['password'] = '$DASH_PASS'
if '$TG_TOKEN':
    cfg.setdefault('telegram', {})['token'] = '$TG_TOKEN'
if '$TG_CHAT':
    cfg.setdefault('telegram', {})['chat_id'] = '$TG_CHAT'
with open('$CFG', 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False)
print("Credentials preserved after pull")
PYEOF

        echo "$(date): Restarting services..." >> $LOG
        source $BASE/venv/bin/activate

        pkill -f "python3.*main.py"   || true
        pkill -f "python3.*dashboard" || true
        sleep 2

        nohup python3 $BASE/main.py      >> $BASE/logs/bot.log 2>&1 &
        nohup python3 $BASE/dashboard.py >> $BASE/logs/dashboard.log 2>&1 &

        echo "$(date): Services restarted" >> $LOG
    fi

    sleep 60
done
