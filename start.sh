#!/bin/bash
# Algo Trader v2 — Start Script

BASE=/opt/algo-trader
CFG=$BASE/config/settings.yaml
LOG=$BASE/logs/bot.log

mkdir -p $BASE/logs $BASE/data
cd $BASE

echo "$(date): === Starting ===" >> $LOG

# 1. Save credentials before touching anything
SECRET=$(python3 -c "import yaml; c=yaml.safe_load(open('$CFG')); print(c.get('alpaca',{}).get('secret_key',''))" 2>/dev/null || echo "")
PASS=$(python3 -c "import yaml; c=yaml.safe_load(open('$CFG')); print(c.get('dashboard',{}).get('password','AlgoTrader2024!'))" 2>/dev/null || echo "AlgoTrader2024!")
TG_TOK=$(python3 -c "import yaml; c=yaml.safe_load(open('$CFG')); print(c.get('telegram',{}).get('token',''))" 2>/dev/null || echo "")
TG_CHAT=$(python3 -c "import yaml; c=yaml.safe_load(open('$CFG')); print(c.get('telegram',{}).get('chat_id',''))" 2>/dev/null || echo "")

echo "$(date): Saved creds. Pulling from GitHub..." >> $LOG

# 2. Force discard local changes then pull (this is why git pull was failing)
git fetch origin main -q 2>/dev/null
git reset --hard origin/main -q 2>/dev/null
git clean -fd -q 2>/dev/null

echo "$(date): Pull done. Commit: $(git rev-parse --short HEAD)" >> $LOG

# 3. Re-inject credentials
python3 - << PYEOF
import yaml
with open('$CFG') as f:
    cfg = yaml.safe_load(f)
if '$SECRET': cfg.setdefault('alpaca',{})['secret_key'] = '$SECRET'
if '$PASS':   cfg.setdefault('dashboard',{})['password'] = '$PASS'
if '$TG_TOK': cfg.setdefault('telegram',{})['token'] = '$TG_TOK'
if '$TG_CHAT': cfg.setdefault('telegram',{})['chat_id'] = '$TG_CHAT'
with open('$CFG','w') as f:
    yaml.dump(cfg, f, default_flow_style=False)
PYEOF

echo "$(date): Credentials restored." >> $LOG

# 4. Kill existing bot (NOT dashboard — dashboard restarts itself)
pkill -f "python3.*main.py" 2>/dev/null || true
sleep 2

# 5. Start bot
source $BASE/venv/bin/activate
nohup python3 $BASE/main.py >> $BASE/logs/bot.log 2>&1 &
echo "$(date): Bot started (PID $!)" >> $LOG
