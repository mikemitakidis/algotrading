# Algo Trader v2

Multi-timeframe algorithmic trading signal engine.

## Architecture
- 4-timeframe confluence: 15m / 1H / 4H / Daily
- Tier A: ranks full universe daily → Top 150 focus set
- Tier B: full indicator analysis on focus set
- Signal routing: 4/4 TF → eToro | 3/4 TF → IBKR
- Shadow mode: signals logged, no live trades

## Stack
- Python 3.12, Alpaca data feed, SQLite ML database
- Flask dashboard, GitHub auto-sync
- pandas-ta, xgboost, lightgbm, flask

## Setup
See server documentation for configuration details.
