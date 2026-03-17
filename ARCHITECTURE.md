# Architecture

## Folder Structure

```
/opt/algo-trader/              ← server root
├── bot/
│   ├── __init__.py
│   ├── data.py                ← yfinance fetch functions only
│   ├── indicators.py          ← all technical indicator calculations
│   ├── scanner.py             ← scoring, signal generation, tier A/B logic
│   ├── database.py            ← SQLite init, insert, query
│   ├── notifier.py            ← Telegram alert formatting and sending
│   └── config.py              ← loads .env, validates required keys
├── dashboard/
│   ├── __init__.py
│   └── app.py                 ← Flask dashboard, completely separate from bot
├── main.py                    ← entry point: loads config, starts scan loop
├── requirements.txt
├── .env                       ← secrets only, never in GitHub
├── .env.example               ← template with placeholder values
├── deploy.sh                  ← one-time setup: pip install + start all
├── start.sh                   ← start or restart all processes
├── sync.sh                    ← GitHub auto-sync daemon
├── logs/                      ← gitignored
│   ├── bot.log
│   ├── dashboard.log
│   └── sync.log
└── data/                      ← gitignored
    └── signals.db
```

## Module Responsibilities

### bot/config.py
- Loads .env using python-dotenv
- Validates all required keys are present
- Raises clear error if any key is missing
- No defaults for secrets — fail loudly

### bot/data.py
- Single function: `fetch_bars(symbols, period, interval) -> dict[str, DataFrame]`
- Uses yfinance exclusively
- Batches of 100 symbols
- Returns only symbols with ≥26 bars (minimum for MACD)
- Logs batch count and symbol count

### bot/indicators.py
- Single function: `compute(df) -> dict | None`
- Returns None if insufficient data or any NaN/Inf in result
- Returns dict with all 13 indicator values
- No side effects

### bot/scanner.py
- `score_timeframe(ind, direction) -> int` — 0 or 1
- `rank_symbols(symbols) -> list[str]` — Tier A, top 150
- `scan_cycle(focus, db, config) -> int` — full 4-TF scan, returns signal count

### bot/database.py
- `init_db(path) -> Connection`
- `insert_signal(conn, row)` — only inserts if ind dict is populated
- `recent_signals(conn, limit) -> list`
- `signal_count(conn) -> int`

### bot/notifier.py
- `send_alert(config, signal_dict)` — sends Telegram message
- Formats entry price, SL (2×ATR below), TP (3×ATR above)
- Silently skips if token/chat_id not configured

### dashboard/app.py
- Completely separate process from bot
- Reads bot.log and signals.db — never imports from bot/
- Routes: GET /, POST /login, GET /api/status, GET /api/signals, GET /api/logs
- All write endpoints require session auth

### main.py
- Imports from bot/ only
- Load config → init db → connectivity test → loop:
  - Re-rank every 6 hours
  - Scan every 15 minutes
  - Log clearly at every stage

## Data Flow

```
yfinance → data.py → indicators.py → scanner.py → database.py
                                                 → notifier.py → Telegram
dashboard/app.py reads ← signals.db, bot.log
```

## What Failed in Old Build — Never Repeat

1. **Circular deployment path**: start.sh did not install deps. sync.sh called main.py directly. Neither path ever installed yfinance.
2. **git conflict loop**: settings.yaml tracked in git with real credentials. Server credentials differed from GitHub. Every git pull silently failed.
3. **Self-heal code**: dashboard.py embedded base64-encoded main.py and overwrote it on startup. Made debugging impossible.
4. **Duplicate Flask routes**: two `def start()` functions. Flask registered only one silently.
5. **Alpaca data with free account**: Alpaca's SIP feed requires paid subscription. Free paper trading account blocks all historical bar requests regardless of `feed=` parameter.
6. **Mixed data sources**: main.py used yfinance, backtest.py used Alpaca. Different data, untestable.
7. **Secrets in GitHub**: API key and secret key committed to public repo.
8. **No modular structure**: all 300+ lines in one file. Untestable, hard to debug.
9. **Empty indicator dict passed to DB**: log_signal called with `{}` — silently inserted null rows.
10. **No dependency check before starting**: bot crashed immediately on ModuleNotFoundError with no clear recovery path.
