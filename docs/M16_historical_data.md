# M16 Historical Data Engine — Runbook

**Milestone:** M16.A (engine) + M16.B (local-read capability proof) + four small follow-up fixes (1-4)
**Status:** **CLOSED 2026-06-05** (VPS verification of fix-4 acceptance complete; full evidence in §Q below)
**Baseline:** M15 closeout at `ceb8cd5`
**HEAD at closure:** `aef8335`
**Commit chain on `origin/main`:**
- `c6e98b7` — M16.A: historical data engine + M16.B local-read proof
- `af96eda` — M16.A.fix-1: honest rate-limit classification (was silently `no_data`)
- `c5702f1` — M16.A.fix-2: `cmd_status` auto-migrates v1 DB + clean stale docstrings
- `cc979aa` — M16.A.fix-3: `/api/historical/status` auto-migrates v1 DB
- `aef8335` — M16.A.fix-4: freshness-aware incremental no-op + clean remaining docstrings

**Package:** `bot/historical/` (NOT `bot/data/` — see §A.0 note)

---

## §A.0 Naming note (honest)

The pre-code checklist proposed `bot/data/` as the package path. During
implementation it was discovered that `bot/data.py` already exists at
baseline as the M6 provider-delegation layer (`fetch_bars`,
`resample_to_4h`, `save_focus_cache`, `load_focus_cache`). A new
`bot/data/` package would have shadowed this module at import time and
broken `bot/scanner.py`. The M16 package was renamed to
`bot/historical/` — matching Gemini's original suggestion — to avoid
the collision. The original `bot/data.py` is byte-identical to
baseline; the protected-files invariant holds.

---

## §A. Purpose and scope

M16 builds a local historical OHLCV store so future backtesting,
signal scoring, optimisation, and ML have a reliable data foundation.

It does **not** rewrite the existing scanner. `bot/scanner.py` keeps
its existing data path. The historical store is read-only from the rest
of the bot's perspective for V1; only `bot.historical.refresh.run(...)`
writes.

### Layer model

```
Ingestion        bot/historical/providers.py + providers_yfinance.py
  (yfinance V1; future IBKR/paid via the same BaseProvider contract)
            │
            ▼
Storage          bot/historical/{schema,refresh,quality,coverage,timeframes}.py
  data/historical.db   (SQLite metadata, separate from signals.db)
  data/historical/<provider>/<timeframe>/<symbol>.parquet
            │
            ▼
Access           bot/historical/store.py
  get_bars(...) — the ONLY public read API
  get_coverage(...), list_symbols(...), list_quality_events(...)
```

---

## §B. Storage choice

**Hybrid: SQLite metadata + Parquet bars.** Rationale and tradeoffs
documented in the pre-code checklist (`§B Storage choice`). Pure SQLite
would lock-contend with `signals.db`; pure Parquet would lack
transactional metadata; DuckDB was out of scope for V1.

The historical SQLite DB lives at `data/historical.db` — **completely
separate** from `data/signals.db` (M15.3 audit). No cross-DB foreign
keys; no shared tables.

---

## §C. Data schema

### SQLite (`data/historical.db`)

Schema version 1. Six tables (DDL in `bot/historical/schema.py`):

1. `historical_schema_version` — single-row version marker
2. `historical_symbols` — symbol universe + active flag
3. `historical_coverage` — per-(symbol, timeframe, provider) state
   * includes resample metadata (`source_timeframe`,
     `derivation_method`, `resample_rule_version`) per D-α
4. `historical_refresh_runs` — one row per refresh invocation
5. `historical_quality_events` — append-only observations
6. `historical_refresh_lock` — single-row advisory lock per
   Correction 1

### Parquet OHLCV

Path: `data/historical/<provider>/<timeframe>/<symbol>.parquet`
(provider in path per Correction 1; symbol upper-cased).

Columns:

| column              | type                          | meaning                                |
| ------------------- | ----------------------------- | -------------------------------------- |
| `ts_utc`            | timestamp[us, tz=UTC]         | bar OPEN, always tz-aware UTC          |
| `open`/`high`/`low`/`close` | float64               | **RAW** prices as provider returned    |
| `volume`            | int64                         | raw shares                             |
| `adj_close`         | float64 (nullable)            | provider-supplied adjusted close       |
| `adjustment_ratio`  | float64 (nullable)            | `adj_close / close` at ingest time     |
| `is_adjusted`       | bool                          | True iff adjustment_ratio is non-null  |
| `provider`          | dictionary<string>            | `'yfinance'` (V1)                      |
| `ingested_at_utc`   | timestamp[us, tz=UTC]         | when row was written or rewritten      |
| `quality_flags`     | int32 (bitset)                | 0=clean; bit 0=zero_volume etc.        |

### Adjusted-price approximation (Correction 3 — documented honestly)

yfinance exposes only `Adj Close`, not separate adjusted open/high/low.
M16 derives adjusted O/H/L by multiplying raw values by a uniform
`adjustment_ratio = adj_close / close` computed at ingest.

This is industry-standard for back-adjustment but **is an
approximation** — strictly correct for split-only adjustments, but
introduces small intraday error around dividend payment dates because
dividends are paid at a specific intraday moment, not uniformly across
the bar. For daily timeframes this is negligible; for intraday it's
typically <0.05% near ex-dividend bars. Documented here so future
backtest authors don't assume mathematical exactness.

`get_bars(..., adjusted=False)` returns raw OHLC + volume for callers
that need the unmodified prices.

---

## §D. Refresh modes

| Mode             | When invoked                                | Effect                                                |
| ---------------- | ------------------------------------------- | ----------------------------------------------------- |
| `backfill`       | First-ever entry; after force_rebuild       | Fetch from earliest provider date to now              |
| `incremental`    | Operator CLI (manual; timer is post-M16)    | Fetch `(last_ts - small overlap, now]`; split-check   |
| `repair`         | Operator CLI                                | Refetch flagged gaps                                  |
| `force_rebuild`  | Operator CLI                                | Delete Parquet + reset coverage, then backfill        |

### Lock (Correction 1)

A single-row `historical_refresh_lock` table acts as cross-process
advisory lock. Acquisition takes a `BEGIN EXCLUSIVE` SQLite transaction,
inspects the current holder (PID + lease + liveness via `os.kill(pid, 0)`),
either claims the lock or raises `RefreshLockHeld`. Lease is 30 minutes;
a previous holder that crashes is detected via PID-aliveness and the
lock is reclaimed.

Tests prove: two refreshes cannot run simultaneously; the second exits
cleanly with `status='failed'` and no partial run row.

### Retry policy

Per-symbol exponential backoff: 1s, 2s, 5s, 15s, 60s — each multiplied
by `random.uniform(0.5, 1.5)` (jitter, from Gemini's review). Rate-
limited responses retry with backoff; provider errors retry with
backoff; no-data responses succeed-but-mark-no-data immediately.

### Three distinct outcomes

| Outcome          | Detection                                | Action                                              |
| ---------------- | ---------------------------------------- | --------------------------------------------------- |
| `no_data`        | empty DataFrame, no exception            | `symbols_no_data++`; quality_event severity=info    |
| `provider_error` | exception, HTTP/parse error              | `symbols_failed++`; quality_event severity=error    |
| `rate_limited`   | yfinance 429 / known rate-limit patterns | `rate_limit_count++`; retry                         |

---

## §E. Quality rules

All applied at write time. Hard rejections drop the row; warnings tag
the row's `quality_flags` bitset and write a quality_event.

### Hard rejections

| Kind                  | Trigger                                                    |
| --------------------- | ---------------------------------------------------------- |
| `nan_ohlc`            | Any of open/high/low/close is NaN                          |
| `invalid_hl`          | `high < low`, or `high < max(o,c)`, or `low > min(o,c)`    |
| `negative_volume`     | volume < 0                                                 |
| `non_positive_ohlc`   | Any of open/high/low/close ≤ 0                             |
| `non_utc_ts`          | Timestamp is naive or not UTC (adapter bug)                |

### Warnings (write + tag + record)

| Kind         | Bit | Trigger                                              |
| ------------ | --- | ---------------------------------------------------- |
| `zero_volume`| 0   | volume == 0                                          |
| `outlier`    | 1   | close > N×σ from trailing 60 bars (default N=8)      |
| `duplicate_ts`| 2  | same ts_utc twice in batch (last kept)               |
| `missing_bar`| —   | fewer bars than expected for window                  |

### Coverage-level

| Kind                          | Trigger                                                   |
| ----------------------------- | --------------------------------------------------------- |
| `stale`                       | last_ts older than freshness threshold                    |
| `split_detected`              | adjustment_ratio drift between stored and refetched       |
| `lookback_exceeded`           | request exceeds provider's max lookback                   |
| `resample_source_incomplete`  | 4H bucket has fewer than 4 source 1H bars                 |

---

## §F. 4H resampling (D-α)

4H is **resampled at write time from 1H**, not fetched natively.

| Parameter                | Value                                                 |
| ------------------------ | ----------------------------------------------------- |
| Source timeframe         | `1H`                                                  |
| Derivation method        | `resample`                                            |
| Resample rule version    | `1`                                                   |
| UTC bucket alignment     | 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC          |
| OHLC reduction           | open=first, high=max, low=min, close=last             |
| Volume                   | sum                                                   |
| Adjustment ratio         | recomputed per 4H bucket as `adj_close/close`         |

Every 4H bucket is emitted; if a bucket has fewer than 4 source 1H bars
a `resample_source_incomplete` quality event is recorded (`severity=warn`).
This is **explicit incompleteness** — the bucket is still emitted with
what's available; the operator can decide whether to act.

Test G11.1 (`test_24_hourly_bars_become_6_4h_bars`) proves the
resampling produces the expected bucket count.
Test G11.2 (`test_4h_coverage_metadata_recorded`) proves the
derivation metadata is persisted in `historical_coverage`.

---

## §G. Read API

Single read façade in `bot/historical/store.py`:

```python
from bot.historical.store import get_bars, get_coverage, list_symbols, list_quality_events

df = get_bars(
    symbol="AAPL", timeframe="1D",
    start_utc="2024-01-01T00:00:00+00:00",
    end_utc="2026-06-05T00:00:00+00:00",
    provider="yfinance",
    adjusted=True,
)
# Returns DataFrame with: ts_utc, open, high, low, close, volume, quality_flags
# Empty DataFrame if no local data.
# NEVER calls a provider. NEVER writes.
```

- `adjusted=True` (default): O/H/L scaled by adjustment_ratio; close from adj_close
- `adjusted=False`: raw OHLC as provider returned

The scanner is NOT migrated to this API in M16. That's an explicit
future-milestone decision.

---

## §H. CLI

Operator entry points (no systemd timer in M16 acceptance):

```bash
# Initial backfill of the 10-symbol sample universe
python -m bot.historical.cli backfill \
    --symbols AAPL,MSFT,GOOGL,AMZN,META,NVDA,TSLA,JPM,WMT,KO \
    --timeframes 1D,1H,15m,4H

# Default to data/symbol_universe.csv when --symbols omitted
python -m bot.historical.cli backfill --timeframes 1D

# Incremental — pulls only new bars since last refresh
python -m bot.historical.cli incremental

# Repair gaps recorded as quality events
python -m bot.historical.cli repair

# Force-rebuild a single (symbol, timeframe)
python -m bot.historical.cli force-rebuild --symbol AAPL --timeframe 1D

# Status
python -m bot.historical.cli status
```

Exit codes: `0` on `ok` or `partial`; `1` on `failed`; `2` on missing args.

---

## §I. Dashboard endpoints (read-only)

Three new GET endpoints, all `@require_auth`:

| Endpoint                                       | Returns                                                  |
| ---------------------------------------------- | -------------------------------------------------------- |
| `GET /api/historical/status`                   | Last refresh + totals + oldest stale + 24h error count   |
| `GET /api/historical/coverage?symbol=AAPL`     | Per-timeframe coverage rows                              |
| `GET /api/historical/quality-events?...`       | Recent quality events (newest first; severity-filterable)|

**No POST endpoint** (Correction D-ε). Refresh stays operator-CLI only.

---

## §J. M16.B — local-read capability proof

`bot/historical/preview.py` provides a `compute_recent_sma(...)`
function that reads via `get_bars()` only — no provider calls.

This is a **capability proof**, NOT:
- a strategy
- a signal scoring engine
- a backtester
- ML feature engineering
- a scanner migration

Test G16 proves the preview reads local Parquet data without
invoking the provider.

---

## §K. Acceptance criteria

### M16.A — engine

1. Schema applied: `data/historical.db` exists with 6 tables; version = 2 ✓
2. CLI backfill creates Parquet files at the canonical path
   (CONDITIONAL — gated on yfinance not rate-limiting the VPS;
   see §P)
3. Coverage table populated; refresh_run row with `status='ok'`
   when symbols succeed (CONDITIONAL — see §P)
4. Idempotent: second incremental writes 0 new bars ✓
5. Quality events written for the four distinct outcomes:
   `no_data`, `provider_error`, `rate_limited`, plus structural rules ✓
6. `get_bars()` returns local bars with NO provider call ✓
7. Dashboard endpoints return expected JSON shape ✓
8. Full test suite passes (57 / 57, 1 skipped = live-yfinance) ✓
9. Regression: all M14/M15 suites green (zero new failures) ✓
10. AST scan: no broker / scanner / strategy imports in `bot/historical/*` ✓
11. Protected-files diff vs `ceb8cd5`: 0/N modified ✓
12. pyarrow installs cleanly in venv; `import pyarrow` succeeds ✓
13. **Rate-limit honesty (M16.A.fix-1):** a refresh where every symbol
    is rate-limited reports `status='failed'`, `symbols_rate_limited > 0`,
    `symbols_no_data = 0`, and quality events of `kind='rate_limited'`
    (NOT `kind='no_data'`) ✓

### M16.B — local read proof

1. `python -c "from bot.historical.preview import compute_recent_sma; ..."`
   succeeds after backfill (CONDITIONAL — gated on §K.2/§K.3 above)
2. Test G16 proves no provider call on cache hit ✓

---

## §P. Yahoo/yfinance rate-limit operations (M16.A.fix-1)

**Yahoo Finance rate-limits aggressively** — the VPS IP can be capped
within a few minutes of starting a multi-symbol backfill. This is a
property of the upstream provider, not of M16; we handle it honestly
rather than papering over it.

### How M16 reports rate-limit conditions

When yfinance is rate-limiting:

1. **Adapter layer (`providers_yfinance.py`):** every yfinance
   response is inspected via TWO paths because yfinance 0.2.x has
   inconsistent error propagation:
   - Path A: `YFRateLimitError` raised → caught explicitly by type
   - Path B: empty DataFrame returned + per-symbol error in
     `yf.shared._ERRORS` → scanned for rate-limit substrings
   Both paths return `FetchResult(outcome=FETCH_RATE_LIMITED)`.

2. **Orchestrator (`refresh.py`):** retries with exponential backoff
   + jitter. If retries exhaust with the symbol still rate-limited,
   the symbol is counted in `symbols_rate_limited` (a NEW first-class
   counter — schema v2). It is NOT silently rolled into
   `symbols_no_data`.

3. **Run status:** a refresh where zero symbols succeeded and any
   rate-limited or failed symbols exist is `status='failed'`
   (NOT `'ok'`). A run where some succeeded and some were
   rate-limited is `status='partial'`.

4. **CLI:** when `symbols_rate_limited > 0 and symbols_ok == 0`,
   `python -m bot.historical.cli backfill ...` prints a
   `PROVIDER RATE-LIMITED — no bars were written` banner with
   suggested operator actions.

5. **Dashboard `/api/historical/status`:** the `last_refresh` object
   includes `symbols_rate_limited` and `rate_limit_count`.

### Operator response when rate-limited

In order of escalating effort:

1. **Wait and retry.** Yahoo's rate-limit windows are typically
   5–15 minutes. The cheapest action is to wait and retry.
2. **Reduce scope.** Retry with a single symbol or single timeframe:
   ```bash
   python -m bot.historical.cli backfill --symbols AAPL --timeframes 1D
   ```
3. **Stagger.** If multi-symbol backfill is needed, run them one at
   a time with a delay between, e.g. a shell loop with `sleep 10`.
4. **Verify off-VPS.** Use the standalone `python -c "import
   yfinance; print(yfinance.Ticker('AAPL').history(period='5d'))"`
   from a different network to confirm Yahoo is not blocking the
   VPS IP specifically.
5. **Defer to a paid provider.** Out of scope for M16.A; a future
   milestone can drop in IBKR or Polygon behind the same
   `BaseProvider` contract.

### Acceptance with persistent rate-limiting

If yfinance continues to rate-limit during M16.A acceptance, the
operator has three options:

- **(A) Wait + retry until a small live backfill succeeds.**
  Even one symbol × one timeframe with bars written + idempotent
  incremental confirms the engine works.
- **(B) Defer live-provider acceptance.** Mark the engine
  acceptance complete based on the unit/integration tests + the
  honest rate-limit reporting (above), and defer the "real bars
  stored" acceptance to a separate task.
- **(C) Inject a fixture provider for runtime acceptance.** Not
  shipped with M16.A.fix-1 — would require a new CLI flag that
  reads bars from a CSV fixture. Out of scope here.

**M16 must not be marked closed until at least one of A/B is
satisfied.** This runbook does not claim live backfill acceptance
has passed when zero bars have been stored.

---

## §L. Dependency

| Package    | Version pin                | Verified install              |
| ---------- | -------------------------- | ----------------------------- |
| `pyarrow`  | `>=24,<25`                 | sandbox: 24.0.0 OK            |

**Pin rationale (Correction 7):** the initial pre-code proposal of
`>=10,<18` was too narrow — pyarrow 24.0.0 is the current stable
release at the time of M16.A implementation. The clean-install
evidence (sandbox + VPS venv) shows 24.0.0 imports cleanly with
`pyarrow.parquet.read_table` / `write_table` / `Table.from_pandas`
all functional.

The pin `>=24,<25`:
- Locks to the major-version line that's been verified end-to-end
- Allows in-major patches (e.g. 24.0.1 security fixes) without churn
- Prevents an automatic jump to pyarrow 25.x which could ship ABI
  breaking changes that the M16 read/write paths would need to
  re-validate before being trusted

When pyarrow 25.x is released, the upgrade is a one-line bump in
`requirements.txt` + a full M16 test re-run; the integration tests
in `test_m16_historical_data.py` are the catch-net for any breakage.

`requirements.txt` carries: `pyarrow>=24,<25`.

**DuckDB is NOT added** per Correction 7.

---

## §M. VPS verification command

```bash
cd /opt/algo-trader && \
sudo git fetch origin main && \
sudo git reset --hard origin/main && \
git rev-parse --short HEAD && \

# Dependency — install everything from the manifest (the manifest is
# the source of truth; do NOT use a standalone `pip install pyarrow`)
sudo /opt/algo-trader/venv/bin/python -m pip install -r requirements.txt 2>&1 | tail -5 && \
sudo /opt/algo-trader/venv/bin/python -c \
  "import pyarrow; print('pyarrow', pyarrow.__version__)" && \

# Dashboard reload — new endpoints live
sudo systemctl restart algo-trader-dashboard.service && sleep 3 && \
sudo systemctl is-active algo-trader-dashboard.service && \

# M16 test suite (live-yfinance smoke skipped without M16_LIVE=1)
sudo -u root /opt/algo-trader/venv/bin/python -m unittest \
  test_m16_historical_data 2>&1 | tail -3 && \

# Regression sweep — every M14/M15 suite + signed-off earlier tests
for t in test_m13_2_etoro_read test_m13_3_etoro_paper \
         test_m13_4a_allocation test_m13_5_audit test_m13_5_cli_env \
         test_m13_5_lifecycle test_m13_5_live_broker test_m13_5_nonce \
         test_m13_5_parser test_m13_5_poller test_m13_5_reconcile \
         test_m13_5_scanner_isolation test_m13_5_signal_only \
         test_m14_b_schema test_m14_c_ingest test_m14_d_exposure \
         test_m14_e_engine test_m14_f_preflight test_m14_g_dashboard \
         test_m15_0_service test_m15_2_health test_m15_3_a_2_totp \
         test_m15_3_a_dashboard_auth test_m15_3_b_manual_reset \
         test_m15_3_c_audit_export test_m15_4_gateway_health \
         test_m15_5_ibkr_exposure test_m15_gateway test_m15_schema; do
  r=$(sudo -u root /opt/algo-trader/venv/bin/python -m unittest $t 2>&1 \
        | grep -E "^Ran|^OK|^FAILED" | tr '\n' ' ')
  printf "  %-36s %s\n" "$t" "$r"
done && \

# Sample-10 backfill (live yfinance)
sudo -u root /opt/algo-trader/venv/bin/python -m bot.historical.cli backfill \
  --symbols AAPL,MSFT,GOOGL,AMZN,META,NVDA,TSLA,JPM,WMT,KO \
  --timeframes 1D 2>&1 | tail -10 && \

# Idempotency proof — incremental immediately after
sudo -u root /opt/algo-trader/venv/bin/python -m bot.historical.cli \
  incremental 2>&1 | tail -10 && \
echo "expect bars_written=0 above" && \

# Status
sudo -u root /opt/algo-trader/venv/bin/python -m bot.historical.cli status && \

# Parquet files exist
ls -la /opt/algo-trader/data/historical/yfinance/1D/ | head -15 && \
du -sh /opt/algo-trader/data/historical/ && \

# M16.B local-read proof — provider must not be called
sudo -u root /opt/algo-trader/venv/bin/python -c \
  "from bot.historical.preview import compute_recent_sma; \
   s = compute_recent_sma('AAPL','1D',periods=20,lookback=5); \
   print(s)" && \

# Production state untouched
sudo ss -ltnp 'sport = :8080' && \
sudo systemctl is-active caddy.service && \
curl -s -o /dev/null -w "HTTPS /api/health -> %{http_code}\n" \
  --max-time 6 https://algotrading.marketwarrior.club/api/health && \

# Dashboard endpoint smoke (authenticated — operator does this in browser)
echo "Browser check: open Observability page, confirm HISTORICAL DATA card present"
```

---

## §N. Out of scope (confirmed)

- ❌ Live trading changes
- ❌ Order path changes (no `placeOrder`/`cancelOrder` anywhere — AST-asserted)
- ❌ IBKR / eToro adapter changes
- ❌ Strategy redesign
- ❌ Scanner changes (`bot/scanner.py` byte-identical to `ceb8cd5`)
- ❌ Risk / portfolio redesign
- ❌ ML model training
- ❌ Signal scoring engine
- ❌ Sentiment changes
- ❌ Big dashboard redesign (3 GET endpoints + 1 card only)
- ❌ Precomputed indicators in raw bar store
- ❌ Paid provider integration
- ❌ DuckDB
- ❌ POST refresh endpoint
- ❌ systemd timer (deferred per Correction 5)
- ❌ Generated historical data in Git

---

## §O. Honest residuals

1. **yfinance is V1's single point of failure.** Capability layer makes
   adding a second provider mechanical; not urgent.
2. **Adjusted-OHL is a uniform-ratio approximation** (see §C above).
3. **No transactionality across SQLite + Parquet.** A crash mid-refresh
   could leave a Parquet temp file. The atomic-rename protocol
   (`temp → validate → replace`) handles this for the destination file;
   stray temp files in the parent directory are cleaned on next refresh.
4. **Split detection rewrites only `adj_close` + `adjustment_ratio`**
   for already-stored rows. A full historical re-adjustment requires
   `force_rebuild`. This is documented.
5. **The 1H→4H resampler emits any bucket with at least 1 source bar.**
   This is the deliberate "explicit incompleteness" choice — incomplete
   buckets are logged but emitted.
6. **`pyarrow>=24,<25`.** See §L for the honest pin rationale. Pinned
   to the verified major-version line; bump intentionally on a future
   M17+ subtask with a full M16 test re-run.

---

## §Q. Closeout evidence — VPS-verified 2026-06-05

M16 closed on the strength of the following terminal evidence from the
operator's VPS verification at HEAD `aef8335` (= expected). Every line
is what the VPS actually reported; nothing here is inferred.

### Commit chain

| Commit | Title | What it fixed |
|---|---|---|
| `c6e98b7` | M16.A: historical data engine + M16.B local-read proof | Initial 4298-line landing: schema, store, refresh, providers, quality, CLI, 3 dashboard endpoints, M16.B preview |
| `af96eda` | M16.A.fix-1: honest rate-limit classification | yfinance 0.2.x's `download()` swallows `YFRateLimitError` and stores it in `yf.shared._ERRORS`; original code saw empty DF + classified as `no_data`. fix-1 inspects `_ERRORS`, adds `symbols_rate_limited` first-class counter (schema v2 with additive migration), `_process_one` handles `FETCH_RATE_LIMITED` as a distinct outcome, honest status determination (zero-success + any rate-limit = `failed` not `ok`), CLI banner |
| `c5702f1` | M16.A.fix-2: `cmd_status` auto-migrates v1 DB + clean stale docstrings | `bot.historical.cli.cmd_status` SELECTed `symbols_rate_limited` without first calling `apply_schema`; failed against pre-v2 DBs. Fixed + 4 stale `bot/data/...` docstring headers cleaned |
| `cc979aa` | M16.A.fix-3: `/api/historical/status` auto-migrates v1 DB | Same bug-shape as fix-2 in `dashboard/app.py m16_historical_status`. The other two endpoints (coverage, quality-events) audited as safe — no v2-column refs |
| `aef8335` | M16.A.fix-4: freshness-aware incremental no-op + clean remaining docstrings | Back-to-back incremental was unnecessarily calling the provider (and getting rate-limited) even when local coverage was already fresh. fix-4 adds an early-return at both code paths (native + 4H) when `mode='incremental' AND cov exists AND last_ts_utc exists AND Parquet exists AND freshness=='fresh'`. Result is a clean provider-free no-op. Plus 4 more stale `bot/data/...` docstring headers cleaned — zero residual refs across the whole package |

### Fix-4 acceptance run (the proof M16 is closeable)

```text
$ python -m bot.historical.cli incremental --symbols AAPL --timeframes 1D
  status=ok
  symbols_attempted=1
  symbols_ok=1   no_data=0   failed=0   rate_limited=0
  bars_fetched=0   bars_written=0   bars_updated=0
  duration_sec=0.01
```

- **No provider rate-limit banner.**
- **No yfinance error output.**
- DB row: `run_id 7 = incremental|ok|1|1|0|0|0|0|0`.

### Live-data acceptance run (fix-3 run, the proof of real bars)

```text
$ python -m bot.historical.cli backfill --symbols AAPL --timeframes 1D
  status=ok
  symbols_attempted=1
  symbols_ok=1
  bars_fetched=11462
  bars_written=11462
```

- File on disk: `/opt/algo-trader/data/historical/yfinance/1D/AAPL.parquet` = **571,139 bytes**
- Historical data dir total: **572K**
- DB row: `run_id 4 = backfill|ok|1|1|0|0|0|0|11462`

### Local-read proof

```python
>>> from bot.historical.store import get_bars, get_coverage
>>> from bot.historical.preview import compute_recent_sma
>>> bars = get_bars('AAPL', '1D')
>>> len(bars)
11462
>>> cov = get_coverage('AAPL', '1D')
>>> cov['freshness_status']
'fresh'
>>> cov['last_ts_utc']
'2026-06-05 00:00:00+00:00'
>>> compute_recent_sma('AAPL', '1D', periods=20, lookback=5)  # returned 5 values
```

### Engine / regression / infra

- M16 test suite: **70/70 OK, 1 skipped** (skip is the live-yfinance smoke gated on `M16_LIVE=1`)
- Regression sweep across earlier suites: **1,113 tests, 0 failed** (M13: 313, M14: 324, M15: 476)
- Combined including M16: **1,183 tests, 0 failed across 30 suites**
- `requirements.txt` install exit code: 0
- pyarrow imported and reports version 24.0.0
- `algo-trader-dashboard.service`: active
- `caddy.service`: active
- HTTPS `/api/health`: 200
- `git status`: clean
- HEAD at closure: `aef8335` (= expected)
- Schema migration verified live: `schema_version = 2`, `symbols_rate_limited` column present

### Hard constraints upheld across all five commits

- Protected files vs `ceb8cd5`: **0/20 modified** (every commit)
- `bot/data.py` sha256: **byte-identical** to baseline (the rename collision avoided any modification)
- AST scan over `bot/historical/`: **11 files, 0 violations** (no broker / order / scanner / strategy / engine / governor / heartbeat imports; no `placeOrder`/`cancelOrder`/`modifyOrder`/`closePosition`/`submitOrder` strings)
- Files git-tracked under `data/`: **only the 2 intentional CSVs** (`symbol_metadata.csv` pre-existing + `symbol_universe.csv` for the V1 universe). Zero generated runtime data ever committed.

### What M16 proves end-to-end

1. **Real yfinance provider fetch** (11,462 AAPL 1D bars)
2. **Atomic Parquet write** (`temp → validate → os.replace`, dup ts_utc refused)
3. **SQLite coverage update** (with freshness + derivation + provider-limit-note columns)
4. **Local `get_bars()` read** (façade returns DataFrame with no network call)
5. **SMA local-read proof** (M16.B capability gate: `compute_recent_sma` returns numbers from cache)
6. **Honest rate-limit classification** (fix-1: distinct `symbols_rate_limited` counter, `status='failed'` not `'ok'` when zero succeed, no silent `no_data` masquerade)
7. **CLI status migration safety** (fix-2: `apply_schema` runs before any v2-column SELECT)
8. **Dashboard status migration safety** (fix-3: same fix shape in `m16_historical_status`)
9. **Freshness-aware incremental no-op** (fix-4: back-to-back incremental never hits the provider when coverage is fresh; result is `status=ok, bars_written=0, rate_limit_count=0`, no banner)
10. **No generated runtime data committed** (only the 2 intentional symbol CSVs)

### Next step after closure

**Not M17 coding.** Per operator instruction at closeout, the next task is an
**audit-only pass over M1–M16** from the actual code, conducted by two
independent reviewers (this assistant + ChatGPT). Findings lists will be
compared before any fix-priority decisions are made. M17 (Outcome Learning
Loop / Closed-Loop ML) becomes the next coding milestone only after the
audit pass clears. See `MILESTONE_STATUS.md` and `docs/NEXT_WORK_REGISTER.md`
for the canonical record.
