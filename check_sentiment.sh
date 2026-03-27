#!/bin/bash
cd /opt/algo-trader
source venv/bin/activate

python3 << 'PY'
import sqlite3, json, os
from pathlib import Path

out = []
db = Path('data/signals.db')
out.append(f"DB exists: {db.exists()}, size: {db.stat().st_size if db.exists() else 0}")

if db.exists():
    c = sqlite3.connect(str(db))
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    out.append(f"Tables: {tables}")
    count = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    out.append(f"Signal count: {count}")
    if count > 0:
        cur = c.execute("SELECT symbol, direction, sentiment_enabled, sentiment_mode, sentiment_score, sentiment_label, sentiment_source, sentiment_status FROM signals ORDER BY id DESC LIMIT 5")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        for row in rows:
            out.append(str(dict(zip(cols, row))))
    c.close()

env_mode = os.getenv('SENTIMENT_MODE', 'NOT SET')
env_prov = os.getenv('SENTIMENT_PROVIDER', 'NOT SET')
out.append(f"SENTIMENT_MODE={env_mode}")
out.append(f"SENTIMENT_PROVIDER={env_prov}")

result = '\n'.join(out)
Path('/tmp/sentiment_check.txt').write_text(result)
print(result)
PY

cat /tmp/sentiment_check.txt
