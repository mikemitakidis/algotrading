# Requirements

## Functional Requirements — V1

### Data
- REQ-D1: Fetch OHLCV bars for US equities using yfinance (free, no API key)
- REQ-D2: Support 4 timeframes: 15m, 1H, 4H, 1D
- REQ-D3: Tier A scan: rank all ~1,200 symbols daily by momentum, select top 150
- REQ-D4: Tier B scan: run full indicator analysis on top 150 every 15 minutes

### Indicators
- REQ-I1: RSI (period 14)
- REQ-I2: MACD (12/26/9) — histogram only used for scoring
- REQ-I3: EMA 20 and EMA 50
- REQ-I4: Bollinger Bands (20, 2) — position within bands
- REQ-I5: VWAP deviation (cumulative daily)
- REQ-I6: OBV slope (5-bar)
- REQ-I7: ATR (period 14) — used for stop-loss sizing in alerts
- REQ-I8: Volume ratio vs 20-bar average

### Scoring
- REQ-S1: Each timeframe scored categorically: 0 or 1
- REQ-S2: A timeframe scores 1 only if ALL 3 categories pass: Momentum + Trend + Volume
- REQ-S3: Partial category passes do not count
- REQ-S4: Both LONG and SHORT directions evaluated independently

### Signal Generation
- REQ-G1: Signal generated when ≥3 timeframes score 1 in same direction
- REQ-G2: 4/4 = route ETORO, 3/4 = route IBKR
- REQ-G3: Signal logged to SQLite with all raw indicator values
- REQ-G4: No duplicate signal for same symbol+direction within 4 hours

### Telegram Alerts
- REQ-T1: Alert sent for every generated signal
- REQ-T2: Alert includes: symbol, direction, route, timeframes valid, RSI, price, ATR-based SL and TP
- REQ-T3: Token and chat_id loaded from .env only

### Dashboard
- REQ-W1: Password-protected web UI on port 8080
- REQ-W2: Show bot running status, start/stop/restart buttons
- REQ-W3: Show last 20 signals with key fields
- REQ-W4: Show live bot log (last 200 lines)
- REQ-W5: Show and edit config (non-secret fields only)
- REQ-W6: Password loaded from .env only

### Database
- REQ-DB1: SQLite at data/signals.db
- REQ-DB2: Table: signals with all 22+ indicator columns
- REQ-DB3: Every signal insert includes full indicator dict — no empty inserts

### Deployment
- REQ-DEP1: Single .env file holds all secrets — never in any Python/YAML/shell file
- REQ-DEP2: requirements.txt lists all dependencies with pinned versions
- REQ-DEP3: deploy.sh installs dependencies, then starts bot and dashboard
- REQ-DEP4: sync.sh daemon checks GitHub every 60 seconds, installs deps on update, restarts
- REQ-DEP5: All processes log startup, dependency check result, each scan cycle, errors

## Non-Functional Requirements

- REQ-NF1: Bot must restart cleanly after server reboot (crontab)
- REQ-NF2: No secret ever written to GitHub
- REQ-NF3: Logs must be human-readable and show each pipeline stage
- REQ-NF4: A complete scan cycle must finish without crashing before V1 is considered done

## Explicitly Out of Scope — V1

- Alpaca data API
- Google News / sentiment
- XGBoost / ML pipeline
- IBKR execution
- eToro API
- Backtesting
- Multi-user dashboard
