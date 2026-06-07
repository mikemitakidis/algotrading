# M17.A — Backtesting Engine Foundation (CLOSED 2026-06-07)

**Status:** CLOSED. Implementation VPS-verified at HEAD `a05f160`. M17.B
(scanner_replica + multi-timeframe confluence + live-vs-backtest
equivalence) is **NOT** part of M17.A — explicitly deferred per the
operator's M17 staging decision.

---

## 1. Scope shipped

A new `bot/backtesting/` package that reads ONLY from the M16 historical
store and produces filesystem artifacts. Strict missing-data semantics,
next-open execution, intrabar SL/TP with pessimistic same-bar handling,
fees + slippage on every trade, fixed-risk position sizing with a
max-position cap, deterministic reproducibility metadata. SMA crossover
is the only registered strategy in M17.A — the engine is the foundation,
not the strategy library.

| Capability | M17.A coverage |
|---|---|
| Single-symbol M16-only backtest | ✓ |
| SMA crossover foundation strategy | ✓ |
| Next-open execution (signal at i close → fill at i+1 open) | ✓ |
| Intrabar SL/TP via high/low, pessimistic SL-first if both touched | ✓ |
| Gap-aware stop fills (open beyond stop → fill at open) | ✓ |
| Entry-bar SL/TP eligibility (intentional, fixup2) | ✓ |
| Fees + slippage on every entry AND exit (round-trip) | ✓ |
| Fixed-risk sizing with `max_position_pct` cap | ✓ |
| Zero-size rejection → warning, not bad trade | ✓ |
| Cash never goes negative due to fees (fixup1) | ✓ |
| Metrics: total return, max DD, win rate, profit factor, expectancy, Sharpe, Sortino (with small-sample gate), B&H benchmark | ✓ |
| Filesystem artifacts: manifest.json, report.json, trades.csv, trades.jsonl, equity_curve.csv, warnings.json | ✓ |
| Byte-identical reproducibility (same config + same M16 fixture → same report.json) | ✓ |
| Manifest includes `bot_historical_schema_version` (M16 schema number, fixup3) | ✓ |
| Strict bar-level range check with 7-day non-trading-day boundary tolerance (fixup4 + fixup5) | ✓ |
| CLI `python -m bot.backtesting.cli run --config …` with exit codes 0/2/3/1 | ✓ |
| Missing-data CLI exits 2 with a VALID `bot.historical.cli` refresh command (fixup3) | ✓ |
| AST-asserted hygiene: no yfinance / bot.data / broker / scanner / eToro / ibapi imports anywhere in `bot/backtesting/*` | ✓ |
| Runtime no-network proof | ✓ |
| `scanner_replica` strategy | **DEFERRED to M17.B** |
| Multi-timeframe confluence (`min_valid_tfs`) | **DEFERRED to M17.B** |
| Live-vs-backtest signal equivalence proof | **DEFERRED to M17.B** |
| ATR-based exits | **DEFERRED to M17.B** |
| Optimisation / grid search / walk-forward | **DEFERRED beyond M17.B** |
| Multi-symbol portfolio engine | **DEFERRED beyond M17.B** |
| Dashboard UI for backtests | **DEFERRED beyond M17.B** |
| Short selling | **DEFERRED beyond M17.B** |
| Retirement of `bot/backtest.py` / `bot/backtest_v2.py` | **DEFERRED** (separate sub-milestone) |

---

## 2. Implementation chain on `origin/main`

14 commits between the audit-P1-data-rate-limit-fix closeout (`13a3aa4`)
and the M17.A acceptance HEAD (`a05f160`). No squashes — each phase is
its own commit per operator decision D10.

| Commit | Title |
|---|---|
| `5b37194` | M17.A.1 — Foundation: package skeleton + errors + models + config validation |
| `3d81e3f` | M17.A.2 — Data loader: strict M16 coverage gate + UTC normalisation |
| `9a71444` | M17.A.3 — Vectorized indicators: SMA, EMA, RSI, MACD, ATR, Bollinger, volume |
| `7815c97` | M17.A.4 — Strategy contract + SmaCrossoverStrategy + look-ahead protection |
| `dd2470b` | M17.A.5 — Execution + portfolio + ledger: bar loop, SL/TP, fees, slippage, sizing |
| `2284912` | M17.A.6 — Metrics: pure (ledger, bars, exec_cfg) → dict |
| `a850ece` | M17.A.7 — Output: manifest + report + CSV/JSONL artifacts + reproducibility |
| `98988d1` | M17.A.8 — Runner + CLI: orchestration, example config, golden-path E2E |
| `97e2836` | M17.A.fixup1 — Pre-Phase-9 quick-inspection fixups (public API + cash-never-negative) |
| `e437f79` | M17.A.9 — Phase 9: hygiene tests (G10) — AST, protected-files, gitignore, no-network |
| `925b79b` | M17.A.fixup2 — EOD final equity, round-trip slippage, entry-bar SL/TP eligibility |
| `7c7eb97` | M17.A.fixup3 — Valid M16 refresh commands, manifest schema version, missing-config exit code |
| `60cd6c3` | M17.A.fixup4 — Strict bar-level range check (truncated loaded bars now fail) |
| `a05f160` | **M17.A.fixup5 — Boundary tolerance for non-trading-day start/end (final HEAD)** |

---

## 3. VPS evidence (2026-06-07, operator-verified at HEAD `a05f160`)

```
HEAD = a05f160
M16 + audit-P1 + M17 regression = Ran 233 tests in 99.798s — OK (skipped=1)
example backtest exit code = 0
example run written to:
  data/backtests/20260607T011518Z_sma_crossover_88578b71038d

Artifacts present:
  - equity_curve.csv
  - manifest.json
  - report.json
  - trades.csv
  - trades.jsonl
  - warnings.json

manifest contains:
  "bot_historical_schema_version": 2

warnings.json:
  []

bot/data.py sha:
  03f488c73feba19a9088b779722ee53515e936f2

dashboard active: active
caddy active: active
HTTPS /api/health HTTP 200
git status clean
```

Breakdown of the 233-test regression sweep:
- `test_m16_historical_data`: 70 OK (1 skipped — pre-existing live-yfinance smoke gate)
- `test_audit_p1_data_rate_limit`: 23 OK
- `test_m17_backtesting`: 140 OK (G1–G10 hygiene group inclusive)

Arithmetic: 70 + 23 + 140 = 233 ✓

---

## 4. Hard-constraint evidence

These hold at the final HEAD `a05f160` and are asserted by the G10
hygiene tests at every run:

| Invariant | Result |
|---|---|
| Protected files modified by M17.A vs `13a3aa4` baseline | **0 / 20** |
| Cumulative protected files modified vs `ceb8cd5` (pre-P0 baseline) | **2 / 20** (unchanged — `main.py` + `bot/risk.py` from P0-4 only) |
| `bot/data.py` sha256 | `03f488c73feba19a9088b779722ee53515e936f2` (byte-identical to every prior baseline) |
| Forbidden imports in `bot/backtesting/*` (yfinance, `bot.data`, `bot.providers`, `bot.scanner`, `bot.backtest`, `bot.brokers`, broker_, gateway_, `bot.etoro.{live,paper,signal_only}_broker`, `bot.risk_authority.{engine,governor,snapshot,preflight,ibkr_paper_reader}`, ibapi, ib_insync, requests, urllib.request, urllib3, http.client) | **0 offenders** |
| `bot.historical` imported anywhere outside `bot/backtesting/data_loader.py` | **0 violations** (manifest schema version reaches `output.py` via re-export, not direct import) |
| String-literal scan for order method names (`placeOrder`, `cancelOrder`, `submitOrder`, `placeOrders`, `modifyOrder`, `closePosition`) | **0 occurrences** |
| Socket calls during a full backtest run (patched-socket test) | **0** |
| `data/backtests/` is git-ignored | yes (existing `data/` rule in `.gitignore`) |
| New files outside `bot/backtesting/*`, `configs/backtests/*`, `test_m17_backtesting.py`, `docs/M17_A_closeout.md`, doc-only updates | **0** |
| `.env`, service unit files, generated data | none touched |
| New dependencies | none |
| `bot/backtest.py` / `bot/backtest_v2.py` modifications | none (retirement is a separate future sub-milestone) |

---

## 5. Design decisions locked in

Recording these so M17.B doesn't accidentally re-litigate them:

- **D1** Package name: `bot/backtesting/` (canonical, not `bot/backtest_m17/`).
- **D2** SMA crossover is the M17.A foundation strategy. `scanner_replica` lands in M17.B.
- **D3** Missing/partial/NaN/duplicate/quality-error data = hard fail. Quality-warn / freshness-non-fresh = warning. Non-trading-day boundary gaps ≤ 7 days with clean coverage = warning. Beyond that = hard fail.
- **D4** Execution model: signal at bar i close → entry at bar i+1 open. SL/TP intrabar via high/low. Pessimistic SL-first if both touched. Gap-aware fills. Fees + slippage on every entry AND exit. Entry-bar SL/TP eligibility enabled (fixup2). No leverage. No shorts in M17.A.
- **D5** Output path: `data/backtests/<YYYYMMDDTHHMMSSZ>_<strategy>_<config_hash>/`. 6 artifacts per run. `data/backtests/` git-ignored by the existing `data/` rule.
- **D6** Scope: single-symbol, M16-only, SMA-crossover-only foundation. Multi-symbol portfolio, optimisation, walk-forward, ML, paper-trade automation, dashboard UI, short selling, options/futures, retirement of older `bot/backtest*.py` modules — all deferred beyond M17.A.
- **D7** Indicator parity test vs `bot.indicators.compute()` — DEFERRED to M17.B (where `scanner_replica` requires it).
- **D8** `adjusted=True` default for `get_bars()`; configurable via JSON config field `data.adjusted`.
- **D9** CLI dates inclusive on both ends; `data_loader.py` converts to M16's exclusive end (`start_utc=request.start`, `end_utc=request.end + 1 day`).
- **D10** One local commit per phase. No squash. Push per-phase as approved.

---

## 6. Public API contract at acceptance

```python
import bot.backtesting as bk

result = bk.run(cfg)              # BacktestResult
run_dir = bk.run_and_write(cfg)   # Path to artifact directory

bk.ENGINE_VERSION                 # 'M17.A.1' — bump on engine-semantic changes
```

The single orchestration path is `bot.backtesting.runner.run`. Top-level
re-exports are for ergonomics only.

Module layout:

```
bot/backtesting/
  __init__.py        public surface (run, run_and_write, ENGINE_VERSION)
  errors.py          BacktestError hierarchy
  models.py          Bar, Position, Trade, EquityPoint, BacktestWarning, BacktestResult
  config.py          BacktestRequest, DataConfig, StrategyConfig, ExecutionConfig, BacktestConfig
  data_loader.py     ONLY module that imports bot.historical
                      exposes M16_SCHEMA_VERSION as a re-export
  indicators.py      vectorized SMA, EMA, RSI, MACD, ATR, Bollinger, volume
  strategy.py        Strategy base + SmaCrossoverStrategy + registry
  portfolio.py       cash, position, fixed-risk sizing with max-position cap
  execution.py       bar loop: entry/exit/SL/TP/EOD
  ledger.py          append-only accumulator (trades, equity, warnings)
  metrics.py         pure (ledger, bars, exec_cfg) -> dict
  output.py          filesystem artifacts (no bot.historical import)
  runner.py          single public entry point
  cli.py             python -m bot.backtesting.cli run …
configs/backtests/example_sma_aapl.json   reference config (AAPL 1D 2024-01-02..2024-12-31)
test_m17_backtesting.py                   140 tests across G1..G10
```

---

## 7. What is explicitly deferred to M17.B

Carried over as the M17.B-active entry in `docs/NEXT_WORK_REGISTER.md`:

1. **`scanner_replica` strategy** — the canonical use case for the M17
   engine. Must produce identical signals to the live `bot/scanner.py`
   for the same bars and parameters, OR a documented divergence with a
   reason.

2. **Multi-timeframe confluence** (1D / 4H / 1H / 15m). The live scanner
   requires `min_valid_tfs ≥ 3`; M17.A is single-timeframe. M17.B must
   load all four timeframes for a symbol and reproduce the confluence
   gate.

3. **Indicator parity test against `bot.indicators.compute()`**. M17.B's
   `scanner_replica` must produce identical RSI / MACD / EMA / Bollinger
   values to the live scanner's last-bar engine.

4. **Live-vs-backtest equivalence proof.** Replay the 10 real
   `candidate_snapshots` rows through the backtest engine and assert
   the signal at each (symbol, ts_utc, timeframe) matches the recorded
   live signal. If 10 rows is too thin, supplement with a synthetic
   golden trace as a regression guard.

5. **ATR-based exits.** The live scanner uses ATR for both stops and
   targets; M17.A's `stop_loss_pct` / `take_profit_pct` are
   percentage-based. M17.B adds ATR-based exits as a strategy parameter.

---

## 8. Authoritative references

- This file is the M17.A closeout.
- For the pre-code architecture decisions and phase plan, see the
  approved checklist in the M17.A chat thread (commit messages
  `5b37194` through `e437f79` summarise it). No separate design doc was
  produced for M17.A — the implementation chain + this closeout is the
  record.
- M17.B carry-forward lives in `docs/NEXT_WORK_REGISTER.md` under
  "M17.B — scanner_replica + multi-timeframe confluence + equivalence
  (PROPOSED, AFTER M17.A)".
- M16 (the data source) reference: `docs/M16_historical_data.md`.

---

## 9. Honest residuals at acceptance

- **Example backtest dates were softened.** The original
  `configs/backtests/example_sma_aapl.json` requested `2024-01-01..2024-12-31`,
  which triggered the strict bar-level check at fixup4 because 2024-01-01
  is a US market holiday. Fixup5 introduces a 7-day boundary tolerance
  that would have accepted that request with a warning; out of caution
  the example config was also moved to `2024-01-02..2024-12-31` (both
  Tuesdays, both confirmed trading days). The boundary path is
  exercised by the regression tests, not by the shipped example.

- **`test_m13_5_reconcile` and `test_m14_risk` errors under
  `unittest discover` are pre-existing.** Both pass when run as
  standalone scripts (e.g. `test_m14_risk` returns 39/39 OK
  standalone). They were broken at the M17 baseline `13a3aa4`,
  remain broken at the M17 acceptance HEAD `a05f160`, and are unaffected
  by M17.A. Investigating their unittest-discovery compatibility is
  out of M17.A scope; flagging here so a future audit pass picks it up.

- **Example backtest dates do not yet exercise the boundary-tolerance
  path in a live VPS run.** The shipped config aligns to trading days
  so `warnings.json` is `[]`. The boundary code path is covered by
  G2 unit tests against mocked M16 fixtures. If you'd like a live
  exercise of the boundary path (e.g. a 2024-01-01 start running
  against real AAPL data), it's a one-line config tweak in a future
  sub-milestone or operator test run — engine semantics don't change.
