# M17.B — scanner_replica + Multi-Timeframe Confluence (CLOSED 2026-06-07)

**Status:** CLOSED. Implementation VPS-verified at HEAD `3f1079e`. M17.B's
scanner_replica strategy is the production-side backtest equivalent of
`bot/scanner.py`, reproduced **by code** (not by import). Live-vs-backtest
equivalence is proven by synthetic per-rule parity tests against
`bot.scanner.score_timeframe` and `bot.feature_engine.compute_features`.
Real intraday E2E against `candidate_snapshots` rows remains unverified
because M16 lacks AAPL 4H/1H/15m coverage and yfinance rate-limits
intraday backfills from the VPS IP — recorded honestly as a carry-forward,
not silently glossed over.

---

## 1. Scope shipped

The M17.A foundation gains a parity-driven multi-timeframe strategy plus
the supporting infrastructure. Default `stop_mode='pct'` keeps M17.A
SmaCrossoverStrategy byte-identical; M17.B work is strictly additive.

| Capability | M17.B coverage |
|---|---|
| `scanner_replica` strategy (registry: `scanner_replica`) | ✓ |
| Reproduces `bot/scanner.score_timeframe` algebra in code (no `bot.scanner` import) | ✓ |
| Multi-timeframe 1D / 4H / 1H / 15m loading via `load_multi_tf_bars` | ✓ |
| STRICT-per-TF default (`allow_partial_tfs=False`) | ✓ |
| PARTIAL mode opt-in with explicit `partial_tf_unavailable` warnings | ✓ |
| `MultiTimeframeContext` with anchor enumeration + O(log n) per-TF snapshot lookup | ✓ |
| Look-ahead-safe: snapshot returns the most-recent bar at-or-before the anchor; never beyond | ✓ |
| Per-anchor `available_tfs` semantics (Sharpened Rule #3) — stale TF bars don't count | ✓ |
| Confluence scaler exactly mirrors `bot/scanner.py:160-166` | ✓ |
| RSI live-compatible mode `'sma_gain_loss'` (default still `'wilder'` for M17.A) | ✓ |
| ATR live-compatible mode `'sma_true_range'` (default still `'wilder'` for M17.A) | ✓ |
| `vwap_dev` cumulative VWAP deviation helper with live `+1e-9` epsilons | ✓ |
| `bb_pos` Bollinger band position helper with `0.5` band-collapse fallback | ✓ |
| Indicator parity vs `bot.feature_engine.compute_features` to `rtol=1e-9 + atol=1e-8` | ✓ |
| ATR-based exits opt-in: `ExecutionConfig.stop_mode='atr'` + `stop_atr_mult` + `target_atr_mult` | ✓ |
| `atr_unavailable_at_signal` skip-with-warning path (no blind entries) | ✓ |
| `candidate_snapshots` replay diagnostic (smoke-only; K=0 is accepted-pass per Sharpened Rule #5) | ✓ |
| Example config `configs/backtests/example_scanner_replica_aapl.json` | ✓ |
| G10 AST forbidden-import list extended; M17.A baseline preserved (asserted) | ✓ |
| Default `stop_mode='pct'` preserves M17.A reproducibility byte-identically | ✓ |
| Real intraday E2E on VPS against live AAPL bars | **UNVERIFIED** (carry-forward) |
| Shorts in scanner_replica | DEFERRED (execution layer is long-only) |
| Multi-symbol portfolio backtests | DEFERRED beyond M17.B |
| Optimisation / parameter sweeps / walk-forward | DEFERRED |
| Dashboard backtest UI | DEFERRED |
| Retirement of `bot/backtest.py` / `bot/backtest_v2.py` | DEFERRED (separate sub-milestone) |

---

## 2. Implementation chain on `origin/main`

8 commits between the M17.A docs-closeout HEAD `f6bf24e` and the M17.B
acceptance HEAD `3f1079e`. No squashes — each phase is its own commit,
per the M17.A discipline.

| Commit | Title |
|---|---|
| `1b9e3ec` | M17.B.pre-phase — Baseline test fix: whitelist M17 docs-closeout files |
| `e45707d` | M17.B.1 — Indicator parity helpers + expanded AST guard |
| `96ecaff` | M17.B.2 — Multi-timeframe M16 loader (strict-per-TF default) |
| `16d3006` | M17.B.3 — MultiTimeframeContext: anchor enumeration + look-ahead-safe snapshots |
| `9fd7de8` | M17.B.4 — scanner_replica strategy: live scanner parity by code |
| `09586ca` | M17.B.5 — ATR-based exits in ExecutionConfig (opt-in; default unchanged) |
| `eae0dde` | M17.B.6 — candidate_snapshots replay diagnostic (smoke-only) |
| `3f1079e` | **M17.B.7 — scanner_replica example config + final M17.B hygiene (final HEAD)** |

The pre-phase commit (`1b9e3ec`) is a transparent baseline test-whitelist
fix, not a feature: M17.A's `test_no_unexpected_files_added` did not
include the four M17.A-closeout doc files in its allowed set, so it was
failing at `f6bf24e`. M17.A's docs-closeout VPS verification only checked
docs presence, `bot/data.py` sha, and service health — it did NOT re-run
the unit-test suite, so the failure slipped through. The fix is
test-only, in-scope, and reported transparently rather than buried inside
M17.B.1.

**Lesson recorded for M17.B closeout VPS verification:** the
docs-closeout step itself runs the M17 + M16 + audit-P1 regression
suite, so a similar slip cannot recur.

---

## 3. VPS evidence (2026-06-07, operator-verified at HEAD `3f1079e`)

Captured verbatim from the M17.B implementation-batch VPS verification:

```
HEAD = 3f1079e
8 M17.B commits present on origin/main: f6bf24e..3f1079e

bot/data.py sha:
  03f488c73feba19a9088b779722ee53515e936f2
  (byte-identical to the M17.A baseline; unchanged at every M17.B commit)

Full M17 + M16 + audit-P1 regression:
  exit code 0
  Expected composition: 200 M17 + 70 M16 + 23 audit-P1 = 293 tests OK
  (skipped=2 — 1 from M17.B.5's unreachable ATR-guard defensive test,
                1 from M16's live-yfinance smoke gated on M16_LIVE=1)

M17.B.6 candidate_snapshots replay diagnostic:
  exit code 0
  (K-replayed varies depending on live M16 intraday coverage at the time
   of the run; failed=0 invariant holds regardless. Per Sharpened Rule #5
   no equivalence claim is made when K=0.)

example_sma_aapl.json end-to-end (M17.A baseline preserved):
  exit code 0
  run artifacts written to data/backtests/<timestamp>_sma_crossover_*/

example_scanner_replica_aapl.json end-to-end:
  exit code 2 — MissingDataError
  Root cause: M16 lacks AAPL 4H/1H/15m coverage on the VPS.
  Strict-per-TF gate behaved CORRECTLY (Sharpened Rule #3).
  See section 5 below for the full residual.

Production services:
  algo-trader-dashboard.service:  active
  caddy.service:                  active
  https://algotrading.marketwarrior.club/api/health: HTTP 200

git status: clean
```

---

## 4. Hard-constraint evidence

These hold at the final HEAD `3f1079e` and are asserted by the G10
hygiene tests at every run.

| Invariant | Result |
|---|---|
| Protected files modified by M17.B vs `13a3aa4` (M17 baseline) | **0 / 20** |
| Cumulative protected files modified vs `ceb8cd5` (pre-P0 baseline) | **2 / 20** (unchanged — `main.py` + `bot/risk.py` from `audit-P0-4` only) |
| `bot/data.py` sha256 | `03f488c73feba19a9088b779722ee53515e936f2` (byte-identical at every M17.B commit) |
| Forbidden imports in `bot/backtesting/*`: M17.B additions (`bot.scanner`, `bot.strategy`, `bot.feature_engine`, `bot.indicators`, `bot.sentiment`, `bot.flywheel`) | **0** offenders |
| Forbidden imports in `bot/backtesting/*`: M17.A baseline (yfinance, `bot.data`, `bot.providers`, `bot.backtest`, broker_/gateway_/eToro/risk-authority writers, ibapi/ib_insync/requests/urllib*/http.client) | **0** offenders (baseline preserved; asserted by `G10.test_m17_a_forbidden_baseline_preserved` regression) |
| `bot.historical` imported anywhere outside `bot/backtesting/data_loader.py` | **0** violations |
| Test-file-only import of `bot.scanner.score_timeframe` + `bot.feature_engine.compute_features` (per Sharpened Rule #4 / Q12) | intentional, isolated to `test_m17_backtesting.py`; G10 AST walker scans `bot/backtesting/*.py` only, so test-file imports are outside the scan path |
| Socket calls during a full backtest run | **0** (asserted by G10) |
| String-literal scan for order method names | **0** occurrences |
| `data/backtests/` git-ignored | yes (existing `data/` rule) |
| `data/bar_cache` / `data/bt_v2_cache` consumed by `bot/backtesting/*` | **0** (AST-scanned; legacy caches present on VPS but unused) |
| New files outside scope (`bot/backtesting/*`, `configs/backtests/*`, `test_m17_backtesting.py`, M17 doc area) | **0** |
| `.env`, service unit files, generated runtime data | none touched |
| New runtime dependencies | none |
| `bot/backtest.py` / `bot/backtest_v2.py` modifications | none |
| M17.A reproducibility (engine_version) | unchanged: `'M17.A.1'` (Sharpened Rule #2 honoured — no engine-version bump because default `stop_mode='pct'` keeps M17.A behaviour byte-identical) |

---

## 5. Honest residual — real intraday E2E unverified

This is the deliberate "tell the truth even when it complicates the
victory lap" section.

**What we wanted to demonstrate:** the
`configs/backtests/example_scanner_replica_aapl.json` config runs
end-to-end on the VPS against real AAPL intraday bars, producing
artifacts (`report.json`, `equity_curve.csv`, `trades.csv` etc.) the same
way the M17.A SMA example does, and the M17.B.6 replay diagnostic
replays a non-zero number of `candidate_snapshots` rows successfully.

**What actually happened on the VPS:**

- `scanner_replica` example exited **code 2** with `MissingDataError`,
  pointing at AAPL 4H coverage being absent. **This is the strict-per-TF
  gate (Sharpened Rule #3) behaving correctly** — the M17.B.2 loader
  refused to silently degrade and emitted the right
  `bot.historical.cli backfill` command in its error message.

- M16's actual state on the VPS at acceptance time:
  - AAPL 1D: 11,462 bars, clean, present
  - AAPL 4H: no coverage row
  - AAPL 1H: no coverage row
  - AAPL 15m: no coverage row

- Backfill attempts to populate the missing TFs:
  - 1H backfill → `YFRateLimitError`, status `failed`,
    `rate_limited=1`, `rate_limit_count=6`, exit code 1
  - 15m backfill → `YFRateLimitError`, status `failed`,
    `rate_limited=1`, `rate_limit_count=6`, exit code 1
  - 4H backfill → status ok but `no_data=1`, no bars written
    (because 4H is resampled from 1H at write time, and 1H is missing)

- Old yfinance cache files exist under `data/bar_cache` and
  `data/bt_v2_cache` on the VPS, but M17.B correctly does **not** read
  from them (AST-asserted; legacy paths are not in the allowed-import
  set). Falling back to those caches would have been a violation of
  the M17.A "M16 is the sole data source" architecture decision and was
  not done.

**What this means for the equivalence claim:**

- scanner_replica's parity with the live scanner is proven by:
  - **`G3_IndicatorParity`** — every indicator value (RSI live-mode,
    ATR live-mode, EMA20, EMA50, MACD hist, vwap_dev, bb_pos,
    vol_ratio) matches `bot.feature_engine.compute_features` on
    identical synthetic bars to `rtol=1e-9 + atol=1e-8`.
  - **`G4_ScannerReplicaScoringParity`** — every branch of
    `_score_timeframe_long` / `_score_timeframe_short` (rsi-low /
    rsi-high / macd-fail / trend-fail / vwap-fail / volume-fail /
    all-three-pass per direction) matches
    `bot.scanner.score_timeframe` exactly for the same indicator dict.
  - **`G4_ScannerReplicaConfluenceScaler`** — every
    `(available_tfs, cfg_min)` combination across 1..4 × 1..4 matches
    the live `bot/scanner.py:160-166` scaling formula.
  - **`G4_ScannerReplicaIntegration`** — end-to-end through
    `runner.run` on a synthetic 4-TF uptrend fixture; downtrend
    fixture confirms zero short trades emitted (Sharpened Rule #3 +
    long-only execution).

- The replay diagnostic in **`G6_CandidateSnapshotReplay`** runs the
  whole scanner_replica path against real `candidate_snapshots` rows
  if (and only if) M16 has the required intraday bars. On the
  acceptance VPS that's K=0 (no rows replayable because intraday
  coverage is absent) — recorded honestly in stderr with the explicit
  note that equivalence is **not** claimed when K=0.

- A real-bar intraday E2E run would be a nice supplementary data
  point. It is **not** required for equivalence — the synthetic
  per-rule parity above is the durable proof — but it is recorded as
  a carry-forward in `docs/NEXT_WORK_REGISTER.md` so it doesn't get
  forgotten.

- The strict-per-TF gate, the M16 refresh-command messaging, and the
  loader's "no silent degrade" behaviour all worked exactly as
  specified. That part of M17.B is fully proven.

**What is NOT being done in response:**

- No code workaround for intraday coverage.
- No fallback to `data/bar_cache` / `data/bt_v2_cache`.
- No weakening of strict-per-TF.
- No automatic provider switching.
- No silent partial-mode default.
- No re-attempting yfinance with cosmetic retries that mask the
  underlying rate-limit pattern.

The right home for these is a separate future sub-milestone (see
section 7).

---

## 6. Public API surface added in M17.B

```python
# Multi-timeframe data loading
from bot.backtesting.data_loader import (
    load_multi_tf_bars,   # cfg + timeframes list -> MultiTfBars
    MultiTfBars,          # symbol, requested_timeframes, per_tf_bars,
                          # per_tf_coverage, warnings, allow_partial_tfs;
                          # .loaded_timeframes property
)

# Multi-timeframe context (anchor + snapshot lookup)
from bot.backtesting.mtf_context import (
    MultiTimeframeContext,   # per_tf_bars + anchor_tf -> ctx
                              # .anchors() -> Iterator[pd.Timestamp]
                              # .snapshot_at(ts) -> dict[tf -> SnapshotBar|None]
                              # .available_timeframes, .num_anchors
    SnapshotBar,             # frozen: timeframe, idx, ts_utc
    MtfContextError,         # subclass of BacktestError
)

# Multi-timeframe strategy contract
from bot.backtesting.strategy import (
    MultiTimeframeStrategy,   # base class; runner detects via isinstance
                              # subclasses still implement generate(bars)
                              # where bars = anchor TF; context attached
                              # via attach_context() before generate()
    ScannerReplicaStrategy,   # name='scanner_replica';
                              # default_params mirror bot/strategy.DEFAULTS;
                              # _score_timeframe_long/_short, confluence_min_valid
                              # all by-code; no live imports.
)

# Indicators (live-compatible modes added)
from bot.backtesting.indicators import (
    rsi,         # mode='wilder' (default) | 'sma_gain_loss' (live)
    atr,         # mode='wilder' (default) | 'sma_true_range' (live)
    vwap_dev,    # NEW: cumulative VWAP deviation
    bb_pos,      # NEW: position inside Bollinger band [0.0, 1.0]
)

# ExecutionConfig (ATR exits added)
# config.execution.stop_mode:        'pct' (default — M17.A) | 'atr'
# config.execution.stop_atr_mult:    Optional[float]
# config.execution.target_atr_mult:  Optional[float] (None = TP disabled)
```

`bk.ENGINE_VERSION` remains `'M17.A.1'`. Not bumped because M17.B
defaults preserve M17.A semantics byte-identically and M17.A
reproducibility hashes are unchanged.

---

## 7. Carry-forward to M17.C (or wherever) — recorded in NEXT_WORK_REGISTER

### scanner_replica real intraday E2E — provider/data blocked

**Condition to close:** M16 intraday coverage for AAPL (4H + 1H + 15m)
exists on the VPS via either (a) yfinance backfill succeeding without
rate-limit failures, OR (b) an alternate provider behind the same
`bot.historical` interface. With coverage present, the scanner_replica
example runs to completion on VPS (exit code 0, all 6 artifacts
written, manifest schema version present).

**What stays deferred beyond M17.C** (sub-milestones, sized later):

- **Real intraday provider reliability.** Either:
  - Engineering yfinance rate-limit-aware backfill pacing for the
    intraday TFs (bigger than it sounds — the live scanner currently
    works against yfinance because it reads only the most recent
    short window; M16 backfilling 30+ days of 15m is a different
    request pattern), OR
  - Integrating a paid provider behind `BaseProvider`. M6 left the
    contract in place specifically to make this a one-file addition;
    the cost decision and provider selection are still operator's
    to make.
- **Shorts in scanner_replica.** Execution layer is long-only
  (`ExecutionConfig.allow_short=False` is the M17 invariant); M17.B
  silently suppresses scanner_replica's short side, asserted by
  `test_scanner_replica_does_not_emit_short_signals`. Lifting the
  long-only constraint is a separate decision.
- **Multi-symbol portfolio backtests.** Currently one symbol per
  run; multi-symbol with correlation-aware sizing is M22+ scope.
- **Optimisation / parameter sweeps / walk-forward.** Per-symbol or
  per-regime thresholds, holdout validation, etc.
- **Dashboard backtest UI.** Operator-facing view of `data/backtests/`
  with run comparisons.
- **Legacy backtest retirement.** `bot/backtest.py` and
  `bot/backtest_v2.py` are still present in the repo (untouched by
  M17.A/B). Retirement happens once `bot/backtesting/*` proves
  sufficient operationally.

**Note:** None of the above are M18 (per the agreed roadmap). M18 is
the advanced-signal-scoring + paper-trade-automation sub-milestone.

---

## 8. Existing audit backlog (unchanged by M17.B)

- `audit-P1-broker-permId-fallback` — DEFERRED
- `audit-P1-portfolio-ctx-engine-bypass` — DEFERRED
- `audit-P2-batch` (9 items) — DEFERRED
- `audit-P3-batch` (6 items) — DEFERRED
- `M14-extension-to-scanner-path` — BLOCKER FOR M22 only (unrelated
  to scanner_replica / M17.B)

See `docs/NEXT_WORK_REGISTER.md` for the full active list.

---

## 9. Sharpened Rules audit (operator-pinned rules from the M17.B
   planning Q-checklist)

- **#1 tolerances** — `_PARITY_RTOL_SYNTH = 1e-9`,
  `_PARITY_RTOL_REAL_REPLAY = 1e-4`, `_PARITY_ATOL = 1e-8` exposed as
  named constants in `test_m17_backtesting.py`. Used by G3 indicator
  parity tests and G6 replay diagnostic.
- **#2 perf discipline** — indicators precomputed once per TF as
  vectorized Series; `snapshot_at` is pure searchsorted, O(log n) per
  TF; perf budget test asserts 6,600 anchors × 4 TFs in under 2s on
  the dev box (5x headroom on the 10s soft budget).
- **#3 partial-mode semantics** — STRICT default;
  `allow_partial_tfs=True` is opt-in and emits explicit
  `partial_tf_unavailable` warnings; per-anchor `available_tfs` is
  the unit, not run-level.
- **#4 AST guard expanded EARLY** — added the M17.B forbidden set
  (`bot.scanner` / `bot.strategy` / `bot.feature_engine` /
  `bot.indicators` / `bot.sentiment` / `bot.flywheel`) in
  M17.B.1 BEFORE any production code that might tempt the import.
  `test_m17_a_forbidden_baseline_preserved` asserts the M17.A
  baseline is still a subset of the active set (no silent weakening).
- **#5 replay diagnostic** — `G6_CandidateSnapshotReplay` prints the
  one-line summary `[m17.b.6] candidate_snapshots replay: N considered,
  K replayed, S skipped (...), failed=F` and an explicit "K=0 means
  not enough live data yet; equivalence NOT claimed" note when
  applicable.
- **#6 new example config** — `configs/backtests/example_scanner_replica_aapl.json`
  ships with inline live-compatible thresholds + ATR exits + a narrow
  date range fitting yfinance 15m retention; `example_sma_aapl.json`
  unchanged.

---

## 10. Authoritative references

- This file is the M17.B closeout.
- For M17.A precedents (architecture decisions D1–D10, single-symbol
  M16-only foundation), see `docs/M17_A_closeout.md`.
- For the planning audit + Q-checklist that authorised M17.B scope,
  see the M17.B planning audit in the closeout chat thread.
- For per-commit detail, the commit messages of the 8 commits listed
  in section 2 contain the per-phase implementation rationale.
- M16 (the data source) reference: `docs/M16_historical_data.md`.
