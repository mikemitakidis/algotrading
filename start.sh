#!/bin/bash
cd /opt/algo-trader
source venv/bin/activate
pkill -f "python3 main.py" 2>/dev/null
pkill -f "dashboard.py" 2>/dev/null
sleep 1
python3 dashboard.py &
sleep 2
python3 main.py >> logs/bot.log 2>&1 &
echo "All started. Dashboard: http://138.199.196.95:8080 | Password: AlgoTrader2024!"
