#!/bin/bash
BASE=/opt/algo-trader
LOG=$BASE/logs/bot.log
mkdir -p $BASE/logs $BASE/data

echo "$(date): === START ===" | tee -a $LOG

# Kill everything
pkill -f "python3.*main.py" 2>/dev/null || true
sleep 1

# Download latest main.py directly from GitHub (no git, no conflicts, always works)
echo "$(date): Downloading main.py from GitHub..." | tee -a $LOG
wget -q --timeout=30 \
  "https://raw.githubusercontent.com/mikemitakidis/algotrading/main/main.py" \
  -O $BASE/main.py && echo "$(date): main.py downloaded OK" | tee -a $LOG \
  || echo "$(date): wget failed, using existing main.py" | tee -a $LOG

# Verify it's the yfinance version
if grep -q "yfinance" $BASE/main.py; then
    echo "$(date): CONFIRMED: main.py uses yfinance" | tee -a $LOG
else
    echo "$(date): ERROR: main.py does NOT have yfinance - check GitHub" | tee -a $LOG
fi

# Install / verify all required packages in venv
source $BASE/venv/bin/activate

echo "$(date): Installing required packages..." | tee -a $LOG
pip install --quiet --upgrade \
    yfinance \
    pandas \
    numpy \
    pyyaml \
    flask \
    requests \
    >> $LOG 2>&1

# Verify yfinance is importable before starting
python3 -c "import yfinance; import pandas; import numpy; import flask; import yaml" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date): ERROR — package import check failed. See log above." | tee -a $LOG
    exit 1
fi
echo "$(date): All packages OK." | tee -a $LOG

# Start bot
nohup python3 $BASE/main.py >> $BASE/logs/bot.log 2>&1 &
echo "$(date): Bot started PID $!" | tee -a $LOG
