# Algo Trader v2

Multi-timeframe algorithmic trading bot with ML signal logging.

## Architecture
- **Tier A**: 1,701 US assets ranked daily → Top 150 focus set
- **Tier B**: Top 150 analyzed across 15m/1H/4H/Daily timeframes
- **Scoring**: Momentum + Trend + Volume must all = 1 per timeframe
- **Routing**: 4/4 TF → eToro | 3/4 TF → IBKR
- **ML**: Every signal logged to SQLite for XGBoost training

## Server
- IP: 138.199.196.95
- Dashboard: http://138.199.196.95:8080
- Password: AlgoTrader2024!

## Auto-sync
The server runs `sync.sh` which checks this repo every 60 seconds.
Any push here auto-deploys to the server within 60 seconds.
