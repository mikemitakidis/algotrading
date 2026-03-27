#!/bin/bash
# Enables real sentiment and verifies it is working
# Run: bash setup_sentiment.sh
cd /opt/algo-trader

echo "=== Setting sentiment config in .env ==="
sed -i '/^SENTIMENT_MODE=/d;/^SENTIMENT_PROVIDER=/d' .env
echo "SENTIMENT_MODE=ignore" >> .env
echo "SENTIMENT_PROVIDER=yfinance_news" >> .env
grep "SENTIMENT" .env

echo ""
echo "=== Restarting bot ==="
bash start.sh

echo ""
echo "=== Waiting 16 minutes for scan cycle ==="
sleep 960

echo ""
echo "=== DB Check ==="
source venv/bin/activate
python3 << 'PY'
import sqlite3
from pathlib import Path
db = Path('data/signals.db')
if not db.exists():
    print("ERROR: signals.db not found")
    exit()
c = sqlite3.connect(str(db))
count = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
print(f"Total signals: {count}")
cur = c.execute("""
    SELECT symbol, direction, sentiment_enabled, sentiment_mode,
           sentiment_score, sentiment_label, sentiment_source, sentiment_status
    FROM signals ORDER BY id DESC LIMIT 5
""")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    print(dict(zip(cols, row)))
c.close()
PY
