# Previous-Milestone Debt Register

**Created:** M21.1extra docs-debt pass (docs/status only — no runtime code changed).
**Baseline audited:** `main` at `b1dd3690b1f7b2f81006c7b2eab82b8607e2d283`.

This register records known debt carried from earlier milestones, with
file/line evidence verified against the actual repo. Each entry states the gate
it must clear before. **None of these are fixed here** — each fix needs its own
plan + review. This document only makes the debt explicit and honest.

## Current operating state (single glance)

- **Active milestone:** M21.1extra **D2** (unattended paper lifecycle wiring).
- **Broker mode:** IBKR **paper only** (port 4002, account `DUP623346`). No IBKR live. No eToro real orders.
- **D2 status:** dry-run + real D0/D1 gate adapters + supervised paper lifecycle **merged**; supervised VPS proof **pending** (needs market-open window + flat-check verifier merge).
- **D3 (dashboard metrics):** **not started.**
- **M21.2 (parameter optimisation):** **not started** — blocked on D3 and on the PnL/backtest debt below.
- **eToro:** blocked until **M22** gate. No real eToro order is reachable from scanner / dashboard / D2.
- **ML (M18):** read-only / shadow-only. Not live scoring. Predict-time isotonic calibration is **stored but not applied** (raw model probability must never be reported as a calibrated win probability).

## Debt entries

### D-1 — M14 Risk Authority not applied to the scanner→IBKR submit path  *(highest-priority code debt)*
- **Evidence:** `main.py:44,125-126,328-363` gates the scanner→IBKR submit with `RiskManager` + `PortfolioRiskPolicy` (from `bot/risk.py`). The M14 `risk_authority` engine (`Authority` / `decide` / `preflight`) is imported only inside `bot/risk_authority/*` and the eToro live-write path — **not** in `main.py`'s submit path.
- **Impact:** scanner-driven IBKR trades use the smaller risk path, not the full M14 authority engine.
- **Gate:** must be resolved **before M22 / live automation**. Needs its own milestone (touches the live submit path and `bot/risk.py` boundary).

### D-2 — M15 IBKR paper PnL ingestion unresolved
- **Evidence:** `bot/risk_authority/ingest_exposure.py` ingests exposure and `unrealised_pnl_usd`; the eToro path (`ingest_etoro_exposure.py`) carries `unrealized_pnl`, but IBKR **paper realized-PnL** ingestion is not populated equivalently.
- **Impact:** no trustworthy paper PnL series.
- **Gate:** must be resolved **before any performance claim, before M21.2 optimisation, and before M22**. You cannot optimise on outcomes you cannot score.

### D-3 — M15 IBKR paper account/equity reader not wired in production
- **Evidence:** `IBKRExposureAdapter` accepts `positions_reader` (required) and `account_reader` (`Optional`, default `None`) — `bot/risk_authority/ingest_ibkr_exposure.py:135-136`. Production construction in `tools/ingest_exposure_state.py:124-125` passes **only** `positions_reader=make_ibkr_paper_positions_reader()` and **no** `account_reader`. So `current_equity_usd` / `peak_equity_usd` remain unpopulated for IBKR paper (the fields exist; the reader is not wired).
- **Impact:** no equity/drawdown base for IBKR paper risk logic.
- **Gate:** should be resolved **before live-scale risk/drawdown logic (M22)**.

### D-4 — Backtest integrity audit (M5 / M17) not done
- **Evidence:** fees/slippage **are** configurable (`bot/backtesting/config.py:135-136`, `fee_bps`/`slippage_bps` default 5 per side), so the debt is **not** "no fees". The debt is a dedicated integrity audit: lookahead, survivorship, provider consistency, walk-forward split, and whether results are reliable enough to drive tuning.
- **Impact:** backtests not yet certified for optimisation / live-readiness decisions.
- **Gate:** must be audited **before M21.2 / M22** rely on backtest results.

### D-5 — M1–M4 base runtime re-verification (deferred)
- **Scope:** dashboard health, scanner heartbeat, strategy config round-trip, `signals.db` writes, `bot_state` freshness.
- **Gate:** post-proof; not blocking unless something is found broken.

## Status-honesty notes (no code debt; wording only)

- **M8 sentiment:** implemented but **not closed-loop** — single-provider, no macro overlay, no confidence-weighted scoring. Do not describe as "fully professional". (`MILESTONE_STATUS.md` row 8 already reflects this.)
- **M9 old ML pipeline:** historical **training infra only** (`ml_train.py`); **not** wired as a live scanner filter and **not** self-learning. M18/M19/M21 are the current ML/scoring direction. (Row 9 already reflects this.)
- **M12 live capability:** capability proven only; **not** sustained automated live trading. Sustained live is **M22 or later**.
- **M13 / eToro:** first funded eToro order is gated at **M22 or later**, not M21. (Corrected in `MILESTONE_STATUS.md`, `ROADMAP.md`, and the M14 docs in this pass.)
- **M18 ML:** read-only / shadow-only; **not** live scoring, **not** M19 scoring. (Row 18 already reflects this.)

## Fix sequencing (each needs a separate plan + review)

| Before | Debt items |
|---|---|
| D2 supervised proof | none of these (proof is paper, gated, flat-verified) — only flat-check merge + proof review |
| Scheduler | D3 first (not started) |
| M21.2 optimisation | D-2 (PnL), D-4 (backtest integrity), M18 B3 calibration honesty |
| M22 (live / eToro) | D-1 (risk authority), D-2 (PnL), D-3 (equity reader), plus M12/M13 wording |
