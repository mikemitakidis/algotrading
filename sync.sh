#!/bin/bash
# Algo Trader v2 — GitHub Auto-Sync Daemon

BASE=/opt/algo-trader
CFG=$BASE/config/settings.yaml
LOG=/var/log/algo-sync.log

echo "$(date): Sync daemon started (PID $$)" >> $LOG

# Function to save credentials before pull
save_creds() {
    python3 - << PYEOF 2>/dev/null
import yaml
try:
    with open('$CFG') as f:
        c = yaml.safe_load(f)
    print(c.get('alpaca',{}).get('secret_key','')+'|||'+
          c.get('dashboard',{}).get('password','AlgoTrader2024!')+'|||'+
          c.get('telegram',{}).get('token','')+'|||'+
          c.get('telegram',{}).get('chat_id',''))
except:
    print('|||AlgoTrader2024!|||')
PYEOF
}

# Function to restore credentials after pull
restore_creds() {
    SECRET="$1" DASH="$2" TG_TOK="$3" TG_CHAT="$4"
    python3 - << PYEOF 2>/dev/null
import yaml, os
with open('$CFG') as f:
    cfg = yaml.safe_load(f)
sk = os.environ.get('SECRET','')
dp = os.environ.get('DASH','AlgoTrader2024!')
tt = os.environ.get('TG_TOK','')
tc = os.environ.get('TG_CHAT','')
if sk: cfg.setdefault('alpaca',{})['secret_key'] = sk
if dp: cfg.setdefault('dashboard',{})['password'] = dp
if tt: cfg.setdefault('telegram',{})['token'] = tt
if tc: cfg.setdefault('telegram',{})['chat_id'] = tc
with open('$CFG','w') as f:
    yaml.dump(cfg, f, default_flow_style=False)
PYEOF
}

restart_services() {
    source $BASE/venv/bin/activate
    pkill -f "python3.*main.py"    2>/dev/null || true
    pkill -f "python3.*dashboard"  2>/dev/null || true
    sleep 2
    nohup python3 $BASE/main.py      >> $BASE/logs/bot.log 2>&1 &
    nohup python3 $BASE/dashboard.py >> $BASE/logs/dashboard.log 2>&1 &
    echo "$(date): Services restarted (bot PID: $!)" >> $LOG
}

while true; do
    cd $BASE
    git fetch origin main -q 2>/dev/null
    LOCAL=$(git rev-parse HEAD 2>/dev/null)
    REMOTE=$(git rev-parse origin/main 2>/dev/null)

    if [ "$LOCAL" != "$REMOTE" ]; then
        echo "$(date): New commit detected. Saving creds..." >> $LOG
        CREDS=$(save_creds)
        IFS='|||' read -r SECRET DASH TG_TOK TG_CHAT <<< "$CREDS"

        git pull origin main -q >> $LOG 2>&1
        echo "$(date): Pulled. Restoring creds..." >> $LOG

        export SECRET DASH TG_TOK TG_CHAT
        restore_creds

        restart_services
    fi
    sleep 60
done
