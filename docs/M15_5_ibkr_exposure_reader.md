# M15.5 — IBKR Exposure Reader Wiring (Paper Mode)

**Status:** Implementation shipped; awaiting VPS verification.
**Scope:** Paper-only. Live IBKR scope (`ibkr_live`) remains intentionally unwired and continues to raise `NotImplementedError` from the CLI path.

For project-wide status, see [`../MILESTONE_STATUS.md`](../MILESTONE_STATUS.md). For the truth-layer that gates M15.5 readiness, see [`M15_4_ib_gateway_runbook.md`](M15_4_ib_gateway_runbook.md).

---

## §1 — What M15.5 does

M14.D shipped the `IBKRExposureAdapter` with an injected `positions_reader` callable. That reader was a `NotImplementedError` stub until now. M15.5 replaces the stub for the `ibkr_paper` scope with a real read-only IB API session that:

1. Calls `bot.gateway_health.assemble_health` first (the M15.4 gate). If the gateway is not `ready_for_ibkr_trading` with `mode='paper'` and `expected_port=4002`, the reader raises `GatewayNotReadyError` and the adapter produces a fail-closed `EXPOSURE_UNKNOWN` reading. No IB API call happens.
2. Opens an `ib_insync.IB()` session with `connect(host='127.0.0.1', port=4002, clientId=15, readonly=True, timeout=5.0)`.
3. **Waits for the account-update snapshot to be ready**, bounded by `api_timeout` (default 5 s). We poll `ib.accountValues()` via `ib.waitOnUpdate(timeout=0.25)` in a loop until either the list is non-empty (snapshot delivered) or the timeout elapses. If it elapses, the reader raises `IBPaperReadError(account_snapshot_not_ready_within_timeout:...)`; the adapter converts to `EXPOSURE_UNKNOWN`. **Empty positions are never reported as known-zero until the snapshot is confirmed ready.**
4. Reads BOTH `ib.portfolio()` AND `ib.positions()`. Both are synchronous cache reads from the account-update subscription; they do NOT accept a timeout. The `api_timeout` above is the wall-clock bound on the wait for the cache to populate, not on the cache read itself.
5. **Cross-confirms** the two reads. We extract the symbol set of non-zero positions from each. If the sets disagree (`portfolio` shows AAPL but `positions` doesn't, or vice versa), the reader raises `IBPaperReadError(portfolio_positions_disagreement:...)`. The adapter converts to UNKNOWN. We NEVER report `known-zero` when the two views disagree.
6. Calls `ib.disconnect()` in a `finally` block — runs even when `portfolio()` raises.
7. Forwards what IB returned to the existing adapter, which decides known-zero / known-nonzero / partial / unknown. M15.5 itself never invents a value, never substitutes a zero for unknown, never fabricates FX rates.

The IBKR exposure surface in the M14 Risk Authority engine now sees real data instead of `exposure_unknown` for `ibkr_paper`, **and only when three independent IB API signals agree**: (a) `accountValues()` non-empty, (b) `portfolio()` content, (c) `positions()` content matching (b).

---

## §2 — Reserved client IDs (drift detection)

The repo-wide IBKR client ID reservations as of M15.5:

| Client ID | Owner | Source of truth |
|---|---|---|
| 11 | `PAPER_CLIENT_ID` — IBKR broker adapter (paper) | `bot/brokers/ibkr_broker.py` |
| 12 | `LIVE_CLIENT_ID` — IBKR broker adapter (live)  | `bot/brokers/ibkr_broker.py` |
| 15 | `M15_5_CLIENT_ID` — M15.5 exposure reader (paper) | `bot/risk_authority/ibkr_paper_reader.py` |
| 99 | `WATCHDOG_CLIENT_ID` — M15.1 gateway watchdog | `bot/gateway_watchdog.py` (env default) |

If you add another IB API consumer in a future milestone, pick a new ID and document it here. A duplicate ID will cause IB to reject one of the sessions.

---

## §3 — Read-only contract

The reader's invariants are enforced both at runtime and by AST scan in `test_m15_5_ibkr_exposure.py`:

- Every `ib.connect(...)` call passes `readonly=True`. The IB API rejects writes on read-only sessions; we set it anyway as defence in depth.
- The reader never references: `placeOrder`, `cancelOrder`, `modifyOrder`, `reqGlobalCancel`, `reqMktData`, `reqHistoricalData`, `reqOpenOrders`, `reqExecutions`.
- The reader never imports order classes from `ib_insync` (`Order`, `Trade`, `MarketOrder`, `LimitOrder`, etc.). Only `IB` is imported, lazily.
- The reader never calls a mutating `systemctl` subcommand.
- The reader never opens a SQLite connection. Persistence is the adapter's job, not the reader's.

Scanner-isolation invariant carries forward: importing `bot.scanner` / `bot.strategy` / `bot.risk` / `bot.brokers` does **not** load `bot.risk_authority.ibkr_paper_reader` or `ib_insync` into `sys.modules`. The reader is lazily imported inside `tools/ingest_exposure_state.py`'s paper branch only.

---

## §4 — Operator workflow

### 4.1. Dry-run first (REQUIRED before any real DB update)

The dry-run proves all preconditions are met without writing to the DB:

```python
sudo /opt/algo-trader/venv/bin/python3 -c "
import json
from bot.risk_authority.ibkr_paper_reader import run_paper_dryrun
summary = run_paper_dryrun()
# Redact positions_count if you don't want to surface it in a paste-back.
print(json.dumps(summary, indent=2, default=str))
"
```

A successful dry-run reports:

```
{
  "dry_run":                  true,
  "gateway_ready":            true,
  "mode":                     "paper",
  "expected_port":            4002,
  "ib_connect_ok":            true,
  "positions_read_ok":        true,
  "positions_count":          <integer>,
  "forbidden_calls_detected": [],
  "error":                    null
}
```

If any of those is `false`/non-empty, **do not proceed to the real ingest.** Investigate using M15.4's `/api/gateway/health` first.

### 4.2. Real ingest (writes to `signals.db`)

The existing M14.D CLI works as-is — no new flag added:

```bash
sudo /opt/algo-trader/venv/bin/python3 \
    /opt/algo-trader/tools/ingest_exposure_state.py --scope ibkr_paper
```

To preview what would be written without touching the DB (different from the dry-run above — this exercises the full ingestion path with an in-memory throwaway `sqlite3` connection):

```bash
sudo /opt/algo-trader/venv/bin/python3 \
    /opt/algo-trader/tools/ingest_exposure_state.py --scope ibkr_paper --dry-run
```

### 4.3. Verify post-ingest state

Inspect the row written for `(today_utc, ibkr_paper)`:

```bash
sudo /opt/algo-trader/venv/bin/python3 -c "
import sqlite3, json
c = sqlite3.connect('/opt/algo-trader/data/signals.db')
row = c.execute('''
    SELECT date, broker_scope, open_positions, capital_deployed,
           lifecycle_json, source, last_ingested_at, fresh_reads_count
    FROM daily_state_per_broker
    WHERE broker_scope = 'ibkr_paper'
    ORDER BY date DESC, last_ingested_at DESC LIMIT 1
''').fetchone()
print(row)
"
```

Cross-check via the M14.G dashboard endpoint (authenticated):

```
GET /api/risk-authority/scopes
```

The `ibkr_paper` entry should now show `exposure_known=true` and the appropriate `exposure_known_zero` boolean. Pre-M15.5 it always showed `exposure_known=false`.

---

## §5 — Known-zero vs unknown vs partial — fail-closed by construction

The reader's cross-confirm layer combines with the M14.D adapter (unchanged) to produce these outcomes:

| IB API state | Reader output | Adapter quality | `is_known_zero_exposure()` | Engine consequence |
|---|---|---|---|---|
| `accountValues()` empty after `api_timeout` | raises `IBPaperReadError(account_snapshot_not_ready_within_timeout:...)` | `UNKNOWN` with that error | **False** | engine fails closed on `exposure_unknown` |
| Snapshot ready; `portfolio()` empty; `positions()` empty | empty list | `FRESH` (or `PARTIAL`) | **True** | engine knows IBKR paper is flat |
| Snapshot ready; `portfolio()` non-empty USD with `marketValue`; `positions()` matches symbols | per-item dicts | `FRESH` | False | engine sees real exposure |
| Snapshot ready; `portfolio()` empty BUT `positions()` non-empty | raises `IBPaperReadError(portfolio_positions_disagreement:...)` | `UNKNOWN` | **False** | engine fails closed |
| Snapshot ready; `portfolio()` non-empty BUT `positions()` empty | raises `IBPaperReadError(portfolio_positions_disagreement:...)` | `UNKNOWN` | **False** | engine fails closed |
| Snapshot ready; symbols agree; any malformed position (missing symbol/side/qty) | per-item dict with bad field | `UNKNOWN` | False | engine fails closed |
| Snapshot ready; any non-USD position without broker USD notional | per-item dict | `UNKNOWN` (no fake FX) | False | engine fails closed |
| Gateway not ready (M15.4 gate fail) | raises `GatewayNotReadyError(...)` | `UNKNOWN` | False | engine fails closed |
| `portfolio()` or `positions()` raises (timeout, OS error) | raises `IBPaperReadError(ib_portfolio_read_failed:...)` | `UNKNOWN` | False | engine fails closed |

M15.5 itself never substitutes `0.0` for an unknown exposure value, and never marks a reading known-zero unless three independent signals all agree: accountValues populated, portfolio empty, positions empty.

---

## §6 — Rollback

Rollback is small because M15.5 is additive at the runtime surface.

### Repo rollback

```bash
cd /opt/algo-trader && git fetch
git reset --hard <pre-M15.5-HEAD>
sudo systemctl restart algo-trader.service algo-trader-dashboard.service
```

After this, `tools/ingest_exposure_state.py --scope ibkr_paper` returns to `EXPOSURE_UNKNOWN` for `ibkr_paper` because the `_reader()` reverts to the `NotImplementedError` stub. Engine immediately fails closed again. No schema migration to undo.

### DB rollback (if a specific bad ingest needs to be removed)

```sql
-- 1. Identify the bad batch from the ingest audit table.
SELECT id, batch_id, ingested_at_utc, status, error
FROM ingest_events
WHERE broker_scope = 'ibkr_paper' AND ingested_at_utc > '<bad_run_utc>'
ORDER BY id DESC LIMIT 5;

-- 2. Delete the broker_positions rows for that batch (append-only schema).
DELETE FROM broker_positions WHERE batch_id = '<bad_batch_id>';

-- 3. Reset the daily_state row to exposure_unknown.
UPDATE daily_state_per_broker
SET lifecycle_json = json_set(coalesce(lifecycle_json, '{}'),
                                '$.exposure_status', 'unknown',
                                '$.exposure_fresh_reads_count', 0),
    fresh_reads_count = 0
WHERE date = '<bad_date>' AND broker_scope = 'ibkr_paper';
```

The engine then immediately fails closed on `exposure_unknown` again until the next ingest. This is the same fail-closed property the M14.D design provides; M15.5 doesn't change it.

### Operator gateway rollback

If the M15.5 ingest seems to be interacting poorly with the gateway (very unlikely — the session is read-only, brief, and uses a dedicated client ID), the M15.4 runbook §10 procedure applies: snapshot `/opt/ibc/config.ini`, `sudo systemctl restart ibgateway.service`, re-run `bot.gateway_health.assemble_health()`.

---

## §7 — What M15.5 does NOT do

Explicitly out of scope:

- **No live IBKR wiring.** The `ibkr_live` scope still raises `NotImplementedError` from `_build_ibkr_exposure_adapter` in `tools/ingest_exposure_state.py`. Adding live wiring requires a separately approved milestone.
- **No order paths.** No `placeOrder`, `cancelOrder`, `modifyOrder`, `reqGlobalCancel`. AST-asserted.
- **No automatic scheduling.** No cron entry, no systemd timer. The ingest runs when the operator runs the CLI.
- **No new dashboard endpoint or panel.** The existing `/api/risk-authority/scopes` (M14.G) and `/api/portfolio-risk/state` will reflect the new data automatically.
- **No changes to `bot/brokers/ibkr_broker.py`.** That module is order-aware; M15.5 uses a fresh thin session instead.
- **No changes to `bot/risk_authority/ingest_ibkr_exposure.py`** (the M14.D adapter). M15.5 swaps in a real `positions_reader` callable; the adapter itself is byte-identical.
- **No changes to `bot/gateway_watchdog.py`** (M15.1) or `bot/gateway_health.py` (M15.4).
- **No changes to `main.py`, `bot/scanner.py`, `bot/strategy.py`, `bot/risk.py`**, the M14 engine/governor/snapshot/audit/preflight, or any eToro file. Protected.

---

*M15.5 runbook end.*
