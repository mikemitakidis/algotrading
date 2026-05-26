# M13.1 — eToro Order Lifecycle (No Schema Changes)

**Status:** Documentation only.

This document specifies how eToro orders flow through the existing M10
`execution_intents` table. **No new columns are required.** Every
eToro lifecycle event maps to columns already present in the schema.

## Existing schema (unchanged)

The current `execution_intents` schema (from `bot/flywheel.py`):

```
id                INTEGER PRIMARY KEY
signal_id         INTEGER         -- FK to signals
timestamp         TEXT            -- intent creation time (UTC ISO)
symbol            TEXT
direction         TEXT            -- 'long' | 'short'
route             TEXT            -- 'IBKR' | 'ETORO' | 'WATCH'
entry_price       REAL
stop_loss         REAL
target_price      REAL
position_size     REAL
risk_usd          REAL
valid_count       INTEGER
strategy_version  INTEGER
broker            TEXT            -- 'paper' | 'ibkr_live' | 'etoro_real' (new value)
status            TEXT            -- see status set below
broker_order_id   TEXT            -- eToro orderID (initially) or positionID (after fill)
rejection_reason  TEXT
risk_checks       TEXT (JSON)
submitted_at      TEXT            -- M12: when POST went out
filled_at         TEXT            -- M12: when statusID=1 observed
fill_price        REAL            -- M12: from positions[0].rate
fill_qty          REAL            -- M12: from positions[0].units
cancelled_at      TEXT            -- M12: when statusID=2 observed
lifecycle_json    TEXT (JSON)     -- full event log
```

The controlled `status` value set is also reused as-is:

```
pending | risk_rejected | paper_logged | accepted | rejected | filled |
cancelled | error | not_implemented | live_safety_blocked |
account_mismatch | connection_failed | broker_unready
```

## eToro lifecycle through existing columns

### Open order — happy path

| # | Bot action | Status | Columns updated | lifecycle_json event |
|---|-----------|--------|------------------|----------------------|
| 1 | Risk passes, intent created | `pending` | initial INSERT with all signal/risk fields | `{"event": "created"}` |
| 2 | eToro adapter pre-validates | (still `pending`) | `risk_checks` extended with eToro pre-check results | `{"event": "etoro_pre_validated", "rates_age_sec": N}` |
| 3 | POST /market-open-orders/by-amount sent | `pending` | `submitted_at` set | `{"event": "etoro_submit", "request_token": "<uuid>"}` |
| 4 | eToro returns 200 with `orderForOpen` | `accepted` | `broker_order_id = str(orderID)` | `{"event": "etoro_accepted", "orderID": N, "statusID": 0, "token": "<uuid>"}` |
| 5 | Poll status, statusID=1 (executed) | `filled` | `filled_at`, `fill_price = positions[0].rate`, `fill_qty = positions[0].units`. **Also update `broker_order_id` to `positionID`** (see "ID refinement" below). | `{"event": "etoro_filled", "positionID": N, "rate": F, "units": F, "occurred": "<iso>"}` |

### Open order — rejection paths

| Scenario | Status | rejection_reason | Notes |
|----------|--------|------------------|-------|
| Pre-execution validation fails (direction, stop side, currency mismatch, etc.) | `rejected` | `etoro_validation_<rule>` | NEVER submitted |
| eToro returns 400/422 | `rejected` | `etoro_4xx_<errorCode>` | `errorMessage` recorded in lifecycle_json |
| eToro returns 429 | `rejected` | `etoro_rate_limit` | Hard stop — operator must investigate |
| Network error before response | `error` | `etoro_network_<exception_class>` | DO NOT retry. Operator reviews via `/trading/info/portfolio` |
| eToro accepts then statusID=3 on poll | `rejected` | `etoro_post_accept_reject_<errorCode>` | Rare but real |
| eToro accepts then statusID=2 on poll | `cancelled` | (none) | populate `cancelled_at` |
| Watchdog blocks before submit (future, when M15.1 extended to eToro) | `broker_unready` | `etoro_unhealthy_block` | Same pattern as IBKR |

### Close order — happy path

Close orders get their own `execution_intents` row, linked to the open
intent via lifecycle_json. The schema does not need a new
`parent_intent_id` column — the linkage lives in JSON, queryable but
not indexed.

| # | Bot action | Status | Columns updated | lifecycle_json event |
|---|-----------|--------|------------------|----------------------|
| 1 | Close intent created (stop hit, target hit, or operator) | `pending` | INSERT with `direction = inverse(parent.direction)`, `signal_id = parent.signal_id`, lifecycle_json carries `"parent_intent_id": N` | `{"event": "close_created", "parent_intent_id": N, "reason": "stop_hit"|"target_hit"|"operator"}` |
| 2 | POST /market-close-orders/positions/{positionId} | `pending` | `submitted_at` set | `{"event": "etoro_close_submit", "positionID": N}` |
| 3 | 200 response | `accepted` | `broker_order_id = str(close orderID)` | `{"event": "etoro_close_accepted", "orderID": N, "token": "<uuid>"}` |
| 4 | Poll statusID=1 | `filled` | `filled_at`, `fill_price`, `fill_qty` | `{"event": "etoro_close_filled", ...}` |

Linking close→open is via `lifecycle_json.parent_intent_id` plus
`signal_id` equality. Either is sufficient for reconstruction.

## ID refinement: orderID → positionID

eToro distinguishes between `orderID` (the request) and `positionID`
(the resulting position). For an open order, the lifecycle is:

1. POST returns `orderID` → record in `broker_order_id`.
2. Poll until statusID=1 → `positions[0].positionID` becomes the
   long-lived identifier.
3. Update `broker_order_id` to `str(positionID)` and add the original
   `orderID` to `lifecycle_json` under `"open_order_id"`.

After step 3, `broker_order_id` is the position ID — which is what the
close endpoint needs as a path parameter. This avoids carrying two ID
columns.

For close orders, `broker_order_id` is the close `orderID` (terminal —
positions don't persist after a close). The parent positionID is still
recoverable from `lifecycle_json.positionID`.

## Status polling discipline

The polling design is constrained by the 60 GET/min read budget
(shared across market data, portfolio reads, and order status):

- Open orders awaiting fill: poll `/trading/info/real/orders/{orderID}`
  every 5s for the first 60s, then every 30s up to 10 minutes, then
  give up and treat as `error`. Most market orders fill in <5s.
- Or: poll `/trading/info/portfolio` once per scan cycle and reconcile
  all in-flight orders in a single call (preferred for steady state —
  saves rate budget).

M13.3 will pick one of these. M13.1 (this doc) just records the
constraint.

## Startup reconciliation (same pattern as M12 for IBKR)

On bot start, the eToro adapter does the same reconciliation IBKR does:

1. Fetch live state via `GET /trading/info/portfolio`.
2. List all in-flight `execution_intents` for `broker='etoro_real'`
   in statuses `(pending, accepted)`.
3. For each, look up the eToro `orderID`/`positionID` in the live
   portfolio:
   - Present in `positions` → update local row to `filled`.
   - Present in `orders` or `ordersForOpen` → stays `accepted`.
   - Absent from both → ambiguous. Log to lifecycle_json, mark with
     a `"reconciliation_missing": true` flag, and surface to the
     operator via the dashboard (not auto-resolved).
4. Same `broker_unready` style of behaviour if reconciliation fails:
   pre-trade gate blocks new submissions until reconciliation is
   complete and clean.

## What this doc does NOT change

- No new columns in `execution_intents`.
- No new tables.
- No changes to `bot/flywheel.py`.
- No changes to `bot/risk.py`.
- No new status values.

The only new value introduced (for M13.5, not M13.1) is `etoro_real`
as a `broker` column value. That's just a string change — no schema
migration needed.
