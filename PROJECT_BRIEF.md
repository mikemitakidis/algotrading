# Algo Trader — Project Brief

> **⚠️ HISTORICAL V1 DOCUMENT.** This describes the original V1 scope (the
> shadow-mode scanner with manual execution). Later milestones M10–M18 added
> broker abstraction, IBKR/eToro integration, live-safety infrastructure,
> a portfolio/risk layer, production hardening, a historical data engine,
> a backtesting engine, and the ML foundation. It does **not** describe the
> full current M1–M18 state. For current status see
> [`MILESTONE_STATUS.md`](MILESTONE_STATUS.md) and [`ROADMAP.md`](ROADMAP.md).

## What This System Does

An automated multi-timeframe trading signal scanner that:
- Fetches market data for ~1,200 US equity symbols
- Computes technical indicators across 4 timeframes (15m, 1H, 4H, 1D)
- Generates BUY/SELL signals when all 3 scoring categories pass on ≥3 timeframes
- Logs every signal to a local SQLite database for future ML training
- Sends Telegram alerts to the operator for manual execution on eToro
- Runs 24/7 on a Hetzner VPS with a simple web dashboard for monitoring

## What It Is NOT

- Not a live execution engine (no broker API calls in V1)
- Not an ML model (no XGBoost, no training loop in V1)
- Not a news/sentiment system in V1
- Not connected to Interactive Brokers in V1
- Not connected to eToro API in V1

## Operator Workflow

1. Bot scans markets every 15 minutes
2. Signal found → logged to DB → Telegram alert sent to operator
3. Operator reviews alert on phone → manually executes trade on eToro if agreed
4. All signals accumulate in SQLite for future backtesting and ML training

## Signal Routing Logic

| Timeframes Valid | Action |
|---|---|
| 4 out of 4 | Telegram alert labelled ETORO (highest confidence) |
| 3 out of 4 | Telegram alert labelled IBKR (high confidence) |
| 0–2 out of 4 | Discarded silently |

> **Superseded (ISSUE-011).** The V1 table above used "3 of 4" for the IBKR
> label. The current rule is configurable and defaults to
> `routing.ibkr_min_tfs = 2`, i.e. ETORO at 4/4, **IBKR at 2–3/4**, and WATCH
> below `ibkr_min_tfs`. See `bot/strategy.py` (DEFAULTS) and `bot/scanner.py`
> for the authoritative behaviour, and `MILESTONE_STATUS.md` for status.

## Server

- Provider: Hetzner VPS
- OS: Ubuntu 24.04
- IP: stored in .env only
- Python 3.12 in `/opt/algo-trader/venv`
- All code in `/opt/algo-trader/`
