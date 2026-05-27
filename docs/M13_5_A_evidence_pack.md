# M13.5.A — Pre-Implementation Evidence Pack

**Status:** Docs / evidence only. **No production code changes, no live
real-money POST, no eToro write of any kind.** All eToro information
below is sourced from public documentation fetched read-only from
`api-portal.etoro.com` and `builders.etoro.com`.

This document resolves every "TBD" left by M13.4B before M13.5.B may
begin writing code. It also establishes a new architectural invariant
required by ChatGPT review: **signal generation and Telegram alerts
must be independent from broker auto-execution.**

Approval chain (unchanged from M13.5 proposal):

| Stage      | Owner          | Deliverable                                   | Gate                                  |
|------------|----------------|-----------------------------------------------|---------------------------------------|
| M13.5.A    | Claude (doc)   | This document                                 | ChatGPT design review                 |
| M13.5.B    | Claude (code)  | Live writer + demo dry-run evidence           | ChatGPT code review                   |
| First write| Operator       | One-shot CLI run with nonce                   | Operator + ChatGPT explicit go-ahead  |

M13.5.A introduces zero code changes. M13.5.B is unblocked only after
ChatGPT accepts this document.

---

## 1. Signal-only / manual-trading mode invariant (NEW)

This invariant is a hard requirement, added by ChatGPT during the
M13.5 proposal review. Any M13.5.B code that breaks it is a defect.

### 1.1 Statement of invariant

> **Signal generation, signal storage, scanner analysis, and Telegram
> alerts run unconditionally.** The Broker Allocation auto-trading
> switches and kill switches control **only** whether a broker
> submission attempt is made. They never disable the scanner, the
> indicator pipeline, or the Telegram notifier.

Explicit consequences:

- `global.auto_trading_enabled = false`
  → no broker submission of any kind for any broker;
  scanner continues; Telegram signals continue;
  signals are persisted to `signals` and `signal_features` as today.
- `etoro.auto_trading_enabled = false`
  → no eToro broker submission;
  IBKR submissions still subject to IBKR's own gates;
  scanner continues; Telegram signals continue.
- `ibkr.auto_trading_enabled = false`
  → no IBKR broker submission;
  eToro submissions still subject to eToro's own gates;
  scanner continues; Telegram signals continue.
- `global.kill_switch = true` or any per-broker `kill_switch = true`
  → same as the corresponding `auto_trading_enabled = false`
  for the relevant scope, with the kill switch taking precedence.
- `routing.etoro_live_enabled = false` (the M13.4A guard)
  → no eToro **real-money** submission;
  `etoro_paper` dry-run submissions still allowed;
  scanner continues; Telegram signals continue.

Manual trading is the default operating mode: the operator reads
Telegram signals (symbol, entry, stop, target, suggested size) and
decides whether to trade by hand on IBKR / eToro UI. The bot does not
require auto-execution to be useful.

### 1.2 Where signals come from regardless of execution switches

The scan → store → alert chain in `main.py` does not change in
M13.5.B. M13.5.B only changes which broker is constructed and whether
its `submit()` is a real call or a no-op. The relevant existing path
(unchanged):

```
scan_cycle(focus, config, conn, cycle_id) → signals
  └─> insert_signal(conn, signal)                      ← always runs
       └─> insert_signal_features(conn, row_id, ...)   ← always runs
            └─> log_candidate(conn, ..., 'final_signal') ← always runs
                 └─> RiskManager.evaluate(intent)
                      └─> PortfolioRiskManager.evaluate(intent, _ctx)
                           └─> [decision point: see §1.3]
                                └─> alert_signal(config, signal)   ← always runs
                                     └─> alert_cycle_summary(...)  ← always runs
```

`bot/notifier.py::alert_signal()` reads only `config` and the `signal`
dict. It does not consult any broker, does not consult policy, does
not consult any auto-execution switch. It is structurally independent
from execution. **No M13.5.B change is permitted to add a coupling.**

### 1.3 The decision point that M13.5.B introduces

In the current `main.py` flow, after risk gates pass, `broker.submit()`
is called unconditionally. M13.5.B must short-circuit the broker call
when auto-execution is disabled for the chosen broker, *without
modifying `main.py`*. The chosen mechanism:

**`bot/brokers/__init__.py::get_broker()` returns a `SignalOnlyBroker`
wrapper when the active broker's auto-execution is disabled by policy.**

`SignalOnlyBroker`:

- Exposes the same `BrokerAdapter` interface as every other broker
  (`.name`, `.submit(intent) -> OrderResult`).
- `.name` is `signal_only:<wrapped_broker_name>` so logs and dashboard
  reflect the current state.
- `.submit(intent)` does **not** call any broker API. It returns an
  `OrderResult` with `status='signal_only_skipped'` and a fixed
  `rejection_reason='auto_trading_disabled'` (or `'kill_switch_active'`
  / `'broker_kill_switch_active'` if those are the reason).
- The wrapper is constructed by reading `policy = load_policy(conn)`
  inside the broker factory; no new API calls are introduced into
  `main.py`.

This means:

- `alert_signal()` runs after `broker.submit()` as today; alerts fire
  regardless of whether the wrapper or the real broker handled the
  intent.
- `execution_intents` rows are still written with a real, named
  rejection reason; reconciliation tools (M13.5 reconciler, future
  dashboards) still see the lifecycle.
- The scanner code path is unchanged. No new import of any live broker
  appears in `bot/scanner.py`, `bot/strategy.py`, or `main.py`.

### 1.4 Scanner-isolation guarantee

The live writer `EtoroLiveBroker` is **never** constructed from
`get_broker()` driven by the `BROKER` env var. It is constructed only
inside the operator CLI `tools/etoro_live_write.py`, which is not
imported anywhere in `main.py`, `bot/scanner.py`, `bot/strategy.py`,
`bot/risk.py`, or any module on the scan path.

Tests in M13.5.B will assert (programmatically):

1. Importing `main` does not transitively import `bot.etoro.live_broker`.
2. Importing `bot.scanner` does not transitively import
   `bot.etoro.live_broker`.
3. Setting `BROKER=etoro_real` in the env and calling `get_broker()`
   raises a controlled error and does not return `EtoroLiveBroker`
   (it returns a no-op or raises, depending on whether the live
   write env-flag is also set; see §3).
4. Constructing `EtoroLiveBroker.submit_live()` without a valid
   per-payload nonce raises `OperatorConfirmationRequired`.

### 1.5 Manual-close compatibility

Manual close (operator closes the position via the eToro web UI or
IBKR TWS) is the **default, supported, and recommended** close method
for the first real eToro write, per M13.4B §8.5. Specifically:

- Closing positions manually never disables Telegram signals.
- Closing positions manually does not require any bot interaction.
- After a manual close, the operator uses
  `tools/etoro_reconcile.py` (see §6) to attach the manual close
  to the existing `execution_intents` row in a controlled, auditable
  way. **No raw SQL.**
- Manual close is independent of the kill switches. Kill switches
  block *new* submissions; they do not interact with already-open
  positions.

---

## 2. Resolved evidence — eToro Real-money endpoints

All evidence below sourced from official eToro OpenAPI specs fetched
read-only from `api-portal.etoro.com`. No write call performed.
Snippets reproduced are schema and field descriptions only; no
credentials or response bodies are included.

### 2.1 Endpoint paths (REAL environment, confirmed)

| Operation                              | Method | Path                                                            |
|----------------------------------------|--------|-----------------------------------------------------------------|
| Open market order by cash amount       | POST   | `/api/v1/trading/execution/market-open-orders/by-amount`        |
| Get order info + positions (status)    | GET    | `/api/v1/trading/info/real/orders/{orderId}`                    |
| Cancel a pending market-open order     | POST   | (real environment "Cancels a market order for open" — full path TBD; documented in `api-portal.etoro.com/api-reference/trading--real/`) |
| Close position by units                | POST   | (real environment "market-close-orders" — documented in same section; **not used in M13.5**, manual close only)             |
| Portfolio + PnL (read)                 | GET    | `/api/v1/trading/info/real/pnl` and portfolio endpoint (verified live read-only in M13 discovery) |
| Trade history                          | GET    | `/api/v1/trading/info/real/trading-history` (verified live read-only in M13 discovery) |
| Identity (CID / GCID)                  | GET    | `/api/v1/identity` (per OpenAPI: "Get authenticated user identity") |
| Instrument search                      | GET    | `/api/v1/market-data/search`                                    |
| Instrument rates (bid/ask)             | GET    | `/api/v1/market-data/instruments/rates?instrumentIds=<csv>`     |
| Instrument metadata                    | GET    | `/api/v1/market-data/instruments?...`                           |

Base URL: `https://public-api.etoro.com`.

> Sources:
> - <https://api-portal.etoro.com/llms.txt> (documentation index)
> - <https://api-portal.etoro.com/api-reference/trading--real/create-a-market-order-to-open-a-position-by-specifying-the-amount-of-cash-you-would-like-to-use-in-the-trade.md>
> - <https://api-portal.etoro.com/api-reference/trading--real/get-order-information-and-position-details-for-real-account.md>

### 2.2 Authentication headers (REAL POST, confirmed)

All real-environment requests require **three** headers (no Bearer
token, no OAuth flow on the order endpoint itself):

| Header           | Type     | Source                                          | M13.5 handling                                                            |
|------------------|----------|-------------------------------------------------|---------------------------------------------------------------------------|
| `x-request-id`   | UUID v4  | Generated per request                           | `uuid.uuid4()`; recorded in `lifecycle_json`; not reused on retry         |
| `x-api-key`      | password | `ETORO_REAL_API_KEY` from `.env`                | Never logged, never committed, redacted in audit log                      |
| `x-user-key`     | password | `ETORO_REAL_USER_KEY` from `.env`               | Never logged, never committed, redacted in audit log                      |

Important properties of eToro keys (from authentication docs):

- A key is **Real** or **Demo**, never both. Live writes need a Real,
  Write-permission key. M13.2/M13.3 used a Real, Read-only key —
  **a new Write-permission Real key must be created in eToro UI before
  M13.5.B can dry-run real-account auth handshakes** (read-only).
- Keys support **IP whitelist** and **expiration date**. M13.5.B will
  document setting both for the Real Write key (operator action,
  performed in eToro UI, not by the bot).
- Demo keys live entirely behind `/api/v1/trading/execution/demo/...`
  and `/api/v1/trading/info/demo/...`. They cannot accidentally hit
  real-money endpoints because the URL paths differ.

> Source: <https://api-portal.etoro.com/getting-started/authentication.md>

### 2.3 Request payload — open by amount (REAL, confirmed)

Schema reproduced from the official OpenAPI spec (no values, no keys):

```jsonc
// POST /api/v1/trading/execution/market-open-orders/by-amount
// Headers: x-request-id, x-api-key, x-user-key
{
  "InstrumentID":    <int32, required>,        // eToro instrument id
  "IsBuy":           <bool,  required>,        // true = long
  "Leverage":        <int32, required>,        // M13.5: must be 1
  "Amount":          <double, required>,       // USD cash amount
  "StopLossRate":    <double|null, optional>,  // M13.5 first write: omit (null)
  "TakeProfitRate":  <double|null, optional>,  // M13.5 first write: omit (null)
  "IsTslEnabled":    <bool|null,   optional>,  // M13.5 first write: false / null
  "IsNoStopLoss":    <bool|null,   optional>,  // M13.5 first write: true
  "IsNoTakeProfit":  <bool|null,   optional>   // M13.5 first write: true
}
```

Required fields per spec: `InstrumentID`, `IsBuy`, `Leverage`, `Amount`.

M13.4B §6 requires no SL/TP on the first write. The OpenAPI spec allows
optional fields, so M13.5.B will send only the required four fields,
or set the explicit `IsNoStopLoss=true` and `IsNoTakeProfit=true`
booleans to disambiguate. **Open known unknown §8.1**: which form does
eToro require for "no SL/TP" — omitting the fields, or sending the
`IsNo*` flags? Must be verified against the demo endpoint in M13.5.B
before any real call.

> Source: same OpenAPI page as §2.1.

### 2.4 Synchronous response — open by amount (REAL, confirmed)

```jsonc
// HTTP 200 on accepted submission
{
  "orderForOpen": {
    "instrumentID":       <int>,
    "amount":             <int>,
    "isBuy":              <bool>,
    "leverage":           <int>,
    "stopLossRate":       <int>,
    "takeProfitRate":     <int>,
    "isTslEnabled":       <bool>,
    "mirrorID":           <int>,
    "totalExternalCosts": <int>,
    "orderID":            <int>,           // ← captured by M13.5; this becomes broker_order_id
    "orderType":          <int>,
    "statusID":           <int>,           // initial status (likely 0=Pending)
    "CID":                <int>,           // customer id (real account)
    "openDateTime":       <ISO8601 string>,
    "lastUpdate":         <ISO8601 string>
  },
  "token": <UUID>                          // eToro-side correlation/tracking token (not auth)
}
```

**Important:** `positionId` is **not** in the synchronous POST
response. The position is created by the matching engine after the
order executes. M13.5 must **poll** `/api/v1/trading/info/real/orders/{orderId}`
to read the `positions[]` array — see §2.6 and §3.

### 2.5 Failure responses — open by amount

The OpenAPI spec does not enumerate failure shapes for the open-by-
amount endpoint specifically. The get-order-info endpoint enumerates:

| HTTP code | Meaning                                                        |
|-----------|----------------------------------------------------------------|
| 400       | Bad Request — invalid format or validation error              |
| 404       | Not Found — order not found                                    |
| 500       | Internal Server Error                                          |
| 429       | (from rate-limits page) Too Many Requests                      |

**Open known unknown §8.2**: the exact body shape on a 4xx from the
write endpoint (whether `{"error": {...}}` or `{"errorCode", "errorMessage"}`)
must be confirmed by triggering controlled failures against the demo
endpoint in M13.5.B (e.g. invalid `InstrumentID`, insufficient demo
funds, restricted instrument).

`errorCode` / `errorMessage` fields do appear in the get-order-info
response schema, so M13.5 should expect a similar shape from POST
failures, but this must be empirically confirmed.

### 2.6 Order status polling — response schema (REAL, confirmed)

```jsonc
// GET /api/v1/trading/info/real/orders/{orderId}
// Headers: x-request-id, x-api-key, x-user-key
{
  "token":           <UUID>,
  "orderID":         <int64>,
  "cid":             <int64>,
  "referenceID":     <string>,
  "statusID":        <int>,        // see §2.7
  "orderType":       <int>,        // 1=Market, 2=Limit, 3=Stop (per spec)
  "openActionType":  <int>,
  "errorCode":       <int|null>,
  "errorMessage":    <string|null>,
  "instrumentID":    <int>,
  "amount":          <decimal>,    // USD requested
  "units":           <decimal>,    // units requested (irrelevant for by-amount)
  "requestOccurred": <ISO8601>,
  "positions": [
    {
      "positionID":     <int64>,    // ← captured by M13.5 on fill
      "orderType":      <int>,
      "occurred":       <ISO8601>,
      "rate":           <decimal>,  // execution rate (price)
      "units":          <decimal>,  // units actually acquired
      "conversionRate": <decimal>,  // FX conversion when account base ≠ instrument currency
      "amount":         <decimal>,  // USD invested
      "isOpen":         <bool>
    }
  ]
}
```

`positions[]` is empty until the order has executed.

The `conversionRate` field resolves M13.4B §3.15 (account base
currency handling): for a GBP-base account placing a USD-denominated
trade, eToro reports the conversion rate applied. M13.5 captures and
stores this in `lifecycle_json`.

### 2.7 Status code vocabulary (confirmed)

Per OpenAPI description on `statusID`:

| `statusID` | Meaning            | M13.5 mapping to `execution_intents.status`            |
|------------|--------------------|--------------------------------------------------------|
| 0          | Pending            | remain `submitted`                                     |
| 1          | Executed           | `filled`                                               |
| 2          | Cancelled          | `cancelled`                                            |
| 3          | Rejected           | `broker_rejected`                                      |
| 4          | Partially Executed | `submitted` (continue polling); if poll budget exhausted with `positions[]` non-empty but order still partial → `filled` with partial-fill flag in `lifecycle_json` |

`orderType` vocabulary: `1 = Market`, `2 = Limit`, `3 = Stop`. M13.5
sends Market only and asserts `orderType == 1` on response (defense
in depth).

> The OpenAPI description text on `statusID` states *"The exact meaning
> of status codes may vary based on order type and system configuration."*
> This is acknowledged as **Open known unknown §8.3**. M13.5.B will
> empirically verify the mapping against demo orders before any real
> POST, and refuse to proceed if observed values deviate from the
> table above.

### 2.8 Rate limits (confirmed)

| Tier          | Endpoints                                                     | Limit                                    |
|---------------|---------------------------------------------------------------|------------------------------------------|
| Read          | Market data, portfolio, PnL, watchlists (read), feeds (read)  | **60 req/min** per user key, rolling 1m  |
| Write / Exec  | Trading execution (POST/DELETE), watchlist write, feed write  | **20 req/min** per user key, rolling 1m  |

`HTTP 429 Too Many Requests` on breach. eToro recommends exponential
backoff.

M13.5 design implications:

- One real write per operator command is well under the 20/min budget;
  not a constraint.
- Post-POST polling: 5 retries × 2 s = 5 GETs in ~10 s. Bursts well
  under 60/min.
- The scanner does not call eToro live endpoints (per §1.4), so it
  contributes zero to either bucket.
- M13.5 does not implement automatic exponential backoff retry. On
  429 the writer fails closed with `status='broker_rejected'`,
  `errorCode='rate_limited'`, alerts operator, stops. No second POST.

> Source: <https://api-portal.etoro.com/getting-started/rate-limits.md>

---

## 3. Double live flag — resolved

Per ChatGPT review: both flags must be true before any real-money POST.

| Flag                                          | Owner     | Default | Set by                                                                                  |
|-----------------------------------------------|-----------|---------|------------------------------------------------------------------------------------------|
| `routing.etoro_live_enabled` (policy)         | dashboard | `false` | Operator via Broker Allocation panel. M13.4A validator currently rejects `true`. **Lifted in M13.5.B in the same gated commit that ships the live writer.** |
| `ETORO_LIVE_ENABLED` (env)                    | host      | `false` | Operator edits `.env` on the VPS (`sync.sh` does not overwrite `.env`). Reload by restart. |

Both must read true before:

- `etoro_real` can appear in `routing.allowed_brokers`
  (also gated by the M13.5.B validator change; see §4)
- `EtoroLiveBroker.preflight()` returns clean
- The operator CLI accepts an `etoro_real` target

M13.5.B tests required (per ChatGPT review):

- Policy alone (without `.env ETORO_LIVE_ENABLED=true`) → preflight
  aborts with `etoro_live_disabled_env`.
- `.env` alone (without policy `etoro_live_enabled=true`) → preflight
  aborts with `etoro_live_disabled`.
- Both true → preflight proceeds to subsequent gates.

The validator change in §4 lifts the M13.4A `etoro_live_forbidden`
rejection only when `etoro_live_enabled=true`. `etoro_real` whitelisting
remains conditional on subsequent runtime gates (`.env` + nonce +
preflight).

---

## 4. `etoro_real` whitelisting — staged

Per ChatGPT review: move `etoro_real` into the M13.4A allowed-broker
whitelist **only as part of the M13.5.B commit**, not earlier. The
move is a six-line diff to `bot/broker_allocation.py`:

```diff
# bot/broker_allocation.py — M13.5.B edit, NOT made in M13.5.A
- ALLOWED_BROKER_WHITELIST = {"paper", "ibkr_paper", "ibkr_live", "etoro_paper"}
- FORBIDDEN_BROKERS = {"etoro_real"}
+ ALLOWED_BROKER_WHITELIST = {"paper", "ibkr_paper", "ibkr_live",
+                              "etoro_paper", "etoro_real"}
+ FORBIDDEN_BROKERS = set()  # no broker is hard-blocked; runtime gates handle live
```

And lift the explicit `etoro_live_forbidden` rejection in
`_validate_routing()`:

```diff
-    elif r["etoro_live_enabled"] is True:
-        # M13.4A: live eToro is explicitly rejected.
-        _err(errors, "routing.etoro_live_enabled", "etoro_live_forbidden",
-             "etoro_live_enabled=true is not permitted in M13.4A")
+    # M13.5.B: etoro_live_enabled=true is now policy-permitted.
+    # Runtime guards (.env ETORO_LIVE_ENABLED + EtoroLiveBroker preflight
+    # + operator nonce) decide whether a real POST is actually emitted.
```

`is_auto_trading_allowed()` keeps its `etoro_real`/`etoro_live_disabled`
branch unchanged. The branch is now actually reachable (was previously
unreachable because `etoro_live_enabled=true` was rejected at validation).

**M13.4A tests updated, not removed:** the two assertions that prove
`etoro_real` is forbidden and `etoro_live_enabled=true` is rejected at
validation are replaced (not deleted) with assertions that prove the
new runtime gating works through `is_auto_trading_allowed()`. Test
count holds. This is the only test-file edit M13.5.B makes to existing
suites; everything else is additive.

---

## 5. Lifecycle status reconciliation with M12

M13.5.B adds three new status names. Reuses six existing M12 names.
Compatibility verified by reading `bot/flywheel.py` (read-only,
no edits in M13.5.A).

| Status              | Source              | New / Reused                                       |
|---------------------|---------------------|----------------------------------------------------|
| `pending_live_write`| M13.5.B (new)       | Inserted at row creation, before any gate runs    |
| `policy_rejected`   | M13.5.B (new)       | §7 gates 1–10 in M13.4B                            |
| `awaiting_confirm`  | M13.5.B (new)       | Set after preflight passes, awaiting nonce echo   |
| `risk_rejected`     | M12 (reused, identical semantics) | §7 gates 11–15 in M13.4B             |
| `broker_unready`    | M15.1 (reused, identical semantics) | Gateway/connectivity issues          |
| `submitted`         | M12 (reused, identical semantics) | POST 200, orderID captured              |
| `filled`            | M12 (reused, identical semantics) | Polling confirmed `statusID=1` or `positions[]` populated |
| `broker_rejected`   | M12 (reused, identical semantics) | POST 4xx/5xx, or `statusID=3`           |
| `cancelled`         | M12 (reused, identical semantics) | `statusID=2`                            |
| `unverified`        | M13.5.B (new)       | Post-POST polling exhausted, state unknown        |

`bot/flywheel.py` does not currently constrain status values via an
enum or check — statuses are free-form strings stored in TEXT columns.
Therefore **no `bot/flywheel.py` code change is required in M13.5.B**
to support the three new names. Schema additions in M15.0 (the six
M12 lifecycle columns) cover everything M13.5.B writes. This resolves
M13.4B §8 (lifecycle status vocabulary finalisation).

---

## 6. Controlled reconciliation tool — `tools/etoro_reconcile.py`

Per ChatGPT review of M13.4B: avoid making "operator updates the
intent row out-of-band" the normal reconciliation process.

`tools/etoro_reconcile.py` is a CLI shipped as part of M13.5.B with the
following constraints, **all enforced in code**:

| Property                                                  | How enforced                                   |
|-----------------------------------------------------------|------------------------------------------------|
| Cannot place an order                                     | No import of `EtoroLiveBroker`, no POST helper |
| Cannot call any eToro write endpoint                      | Only read-only `bot/etoro/read_adapter.py` calls + operator-pasted JSON |
| Updates lifecycle through `bot/etoro/lifecycle.py` only   | No raw SQL in the CLI; lifecycle module is sole writer |
| Requires explicit `intent_id` to act on                   | No "reconcile all" mode                        |
| Refuses if intent row not in a reconcilable terminal-ish state (e.g. `submitted`, `unverified`) | Validated by `lifecycle.py` |
| Logs full action trail to the audit log                   | Same audit module as the live writer           |
| Two input sources only: (a) read-only GET against eToro order-info endpoint, or (b) operator-pasted JSON from eToro UI export | Code only accepts these two paths |

Use cases supported:

1. **`unverified` → `filled` after manual web-UI verification.** Operator
   pastes the eToro order info JSON (or the tool fetches it read-only by
   `orderId`); tool extracts `positions[0].positionID`, rate, units,
   updates the row via `lifecycle.py`.
2. **`unverified` → `broker_rejected` after manual confirmation the order
   never reached the matching engine.** Tool records the determination
   with operator-supplied reason text.
3. **`filled` → `closed_manual` after operator closes via eToro web UI.**
   Tool records the close (separate proposed status name, see §8 known
   unknown §8.5).

`tools/etoro_reconcile.py` does **not** make any POST/DELETE/PUT/PATCH
call to eToro under any circumstance. The strictest enforcement is at
import time: the module fails to import if `bot.etoro.live_broker` is
in `sys.modules` (guard test in M13.5.B test suite).

---

## 7. M13.4B "TBD" items — status

| M13.4B TBD                                                                | Status in M13.5.A                                                |
|---------------------------------------------------------------------------|------------------------------------------------------------------|
| Exact real-money endpoint path                                            | ✅ Resolved (§2.1)                                                |
| Exact request payload schema                                              | ✅ Resolved (§2.3)                                                |
| Real success response fields, `orderId` / `positionId` location           | ✅ Resolved (§2.4, §2.6) — `orderID` synchronous, `positionID` via polling |
| Real failure response shape                                               | ⚠ Partial (§2.5) — HTTP codes confirmed, body shape to be empirically confirmed in M13.5.B demo dry-run (§8.2) |
| Order-status endpoint path + params                                       | ✅ Resolved (§2.6) — `GET /trading/info/real/orders/{orderId}`    |
| Open-orders read endpoint (precondition §3.9)                             | ✅ Resolved — covered by the portfolio endpoint's `orders` field (per `Get Real Account PnL and Portfolio Details` description) |
| Idempotency mechanism                                                     | ⚠ Resolved with caveat (§8.4) — `x-request-id` is "unique request identifier" per spec but not explicitly an idempotency key; M13.5 idempotency guarantee comes from `execution_intents` row + per-payload nonce |
| API-side minimum `Amount`                                                 | ⚠ Open (§8.6) — not documented in OpenAPI; platform minimum is $10 per eToro retail pages; M13.5.B confirms via demo probes |
| Account base currency handling                                            | ✅ Resolved (§2.6) — `conversionRate` in `positions[]` reflects FX |
| Field map IBKR `execution_intents` → eToro lifecycle                      | ✅ Resolved (§5)                                                  |
| Rate limits                                                               | ✅ Resolved (§2.8)                                                |

Remaining "Open known unknowns" are listed in §8. Each has an explicit
resolution path in M13.5.B before any real POST.

---

## 8. Open known unknowns (to be resolved in M13.5.B demo dry-run)

These items are not blockers for M13.5.B implementation — the code can
be written defensively around them. They are blockers for the
**first real-money POST**.

### 8.1 No-SL / No-TP encoding

eToro accepts either omission or the `IsNoStopLoss=true` / `IsNoTakeProfit=true`
booleans. M13.5.B must empirically confirm which the demo endpoint
accepts cleanly and ship that form. Recommendation pending demo
evidence: send `IsNoStopLoss=true, IsNoTakeProfit=true` explicitly.

### 8.2 Failure response body shape

Reproduce at least these failures against the demo endpoint:

- Invalid `InstrumentID` (e.g. 9_999_999)
- Insufficient demo funds (oversized `Amount`)
- Restricted/closed instrument
- Missing required header
- Invalid `Leverage` for the instrument

Capture (redacted) response bodies into `docs/M13_5_B_implementation.md`.
Confirm whether errors come back as `{"error": {...}}`, as the
`errorCode`/`errorMessage` flat shape seen in the order-info schema,
or a third shape.

### 8.3 `statusID` semantics across order types

OpenAPI cautions *"may vary based on order type and system
configuration"*. M13.5.B sends market orders only and verifies that
the observed `statusID` values fall within {0, 1, 2, 3, 4}. Deviation
aborts the writer.

### 8.4 `x-request-id` idempotency behaviour

The header is documented as "unique request identifier" but **not** as
an idempotency key with explicit dedupe semantics. M13.5.B treats it
as best-effort tracking only. The exactly-one-row guarantee comes
from:

1. Per-payload nonce TTL preventing accidental double-confirm
2. `execution_intents` row inserted **before** the POST, in status
   `pending_live_write`, with a unique `(client_intent_id)` column
   (already part of the M15.0 lifecycle additions)
3. Operator CLI exits immediately after one POST; no loop

This is a stronger guarantee than relying on eToro-side dedupe.

### 8.5 `closed_manual` status name

The reconciliation tool (§6) needs a status for "operator closed via
eToro web UI." Proposed name: `closed_manual`. Not yet used anywhere.
M13.5.B finalises the name and uses it through `lifecycle.py`. M13.5.A
does not lock the name; it is a known unknown.

### 8.6 Minimum API `Amount`

Demo probe: `Amount = 1.00`, `Amount = 5.00`, `Amount = 10.00` against
the demo endpoint with a liquid US ETF instrument. Capture each
response. Document the smallest accepted value in
`docs/M13_5_B_implementation.md`. Real-money first write uses the
maximum of (platform $10 floor, observed API minimum, fee-coverage
threshold).

### 8.7 Cancel-endpoint path

M13.4B §3.9 requires verifying that no open eToro order exists on the
chosen instrument before submit. The path for cancelling a pending
market-open order in the real environment is referenced in the
OpenAPI index but not fully captured here. M13.5.B fetches the OpenAPI
JSON spec at <https://api-portal.etoro.com/api-reference/openapi.json>
and records the exact path. Cancel is **not** invoked automatically in
M13.5.B's first-write path — it is only used by the reconciliation
tool in §6.

---

## 9. Updated file list for M13.5.B (small refinements vs. proposal)

Refinements made after M13.5.A research:

- `bot/etoro/order_poller.py` will use the polling response schema
  in §2.6 verbatim. Schema parsing lives in a new
  `bot/etoro/response_parser.py` for unit testability (added to
  the file list; see below).
- `bot/etoro/signal_only_broker.py` is **added** to the file list
  to implement the §1.3 wrapper. (Previously implicit; now explicit.)
- `bot/brokers/__init__.py` modification is **narrowed**: it adds
  exactly one branch — when policy says auto-trading is disabled for
  the active `BROKER`, wrap the constructed broker in
  `SignalOnlyBroker`. The existing `etoro_real` `ValueError` is
  replaced with construction of `EtoroLiveBroker` (gated by
  `ETORO_LIVE_ENABLED` env flag).

Final M13.5.B file list (additive unless marked **MOD**):

**New (code):**
- `bot/etoro/live_broker.py`
- `bot/etoro/signal_only_broker.py`     ← added vs. proposal
- `bot/etoro/nonce.py`
- `bot/etoro/lifecycle.py`
- `bot/etoro/audit.py`
- `bot/etoro/order_poller.py`
- `bot/etoro/response_parser.py`        ← added vs. proposal
- `tools/etoro_live_write.py`
- `tools/etoro_reconcile.py`

**New (tests):** as proposed plus a `test_m13_5_signal_only.py` suite
for the §1 invariant.

**Modified (code, all small):**
- `bot/broker_allocation.py` **MOD** — §4 diff above
- `bot/brokers/__init__.py` **MOD** — single new branch per §9.1
- `test_m13_4a_allocation.py` **MOD** — two assertion swaps per §4

**Modified (docs):**
- `docs/M13_4A_broker_allocation.md` — one-paragraph cross-reference

**New (docs):**
- `docs/M13_5_B_implementation.md`
- `docs/M13_5_operator_runbook.md`
- `.env.example` documentation block (no secrets) for
  `ETORO_REAL_API_KEY`, `ETORO_REAL_USER_KEY`, `ETORO_LIVE_ENABLED`

**Explicitly NOT touched in M13.5.B:**
- `main.py`
- `bot/risk.py`
- `bot/scanner.py`, `bot/strategy.py`
- M15 modules
- `bot/etoro/read_adapter.py`, `bot/etoro/client.py`, `bot/etoro/errors.py`,
  `bot/etoro/paper_broker.py`, `bot/etoro/schema_validator.py`,
  `bot/etoro/instrument_cache.py`
- IBKR broker code
- `dashboard/app.py` (no live-write button in M13.5; deferred)
- `bot/notifier.py` (no new channels; reuse existing pattern)

---

## 10. Acceptance criteria for M13.5.A

- ✅ This file present
- ✅ Docs-only commit
- ✅ No production code changed
- ✅ No live API call performed beyond read-only documentation fetches
- ✅ No eToro `POST/DELETE/PUT/PATCH` performed
- ✅ No secrets in the doc
- ✅ Every M13.4B "TBD" either resolved or listed in §8 with an
  explicit M13.5.B resolution path
- ✅ Signal-only / manual-trading invariant documented (§1)
- ✅ Scanner-isolation contract documented (§1.4)
- ✅ Double live flag documented (§3)
- ✅ Staged `etoro_real` whitelisting documented (§4)
- ✅ Lifecycle status reconciliation documented (§5)
- ✅ Controlled reconciliation tool documented (§6)
- ✅ Push one small docs-only commit to `origin/main`
- ✅ ChatGPT design review of this document gates M13.5.B

---

## 11. Sources cited (all read-only, no write calls performed)

- eToro Public API — Authentication —
  <https://api-portal.etoro.com/getting-started/authentication.md>
- eToro Public API — Rate Limits —
  <https://api-portal.etoro.com/getting-started/rate-limits.md>
- eToro Public API — Documentation index —
  <https://api-portal.etoro.com/llms.txt>
- eToro Public API — Real: Open market order by amount —
  <https://api-portal.etoro.com/api-reference/trading--real/create-a-market-order-to-open-a-position-by-specifying-the-amount-of-cash-you-would-like-to-use-in-the-trade.md>
- eToro Public API — Real: Get order info + positions —
  <https://api-portal.etoro.com/api-reference/trading--real/get-order-information-and-position-details-for-real-account.md>
- eToro Builders — FAQ (M13.4B citation, reused) —
  <https://builders.etoro.com/faq>
- eToro Builders — Algo trading guide (M13.4B citation, reused) —
  <https://builders.etoro.com/use-cases/algo-trading>
- M13.4B — Minimum Live Write Test Design (this repo,
  `docs/M13_4B_minimum_live_write_design.md`)
- M13.4A — Broker Allocation policy (this repo,
  `docs/M13_4A_broker_allocation.md`, `bot/broker_allocation.py`)
