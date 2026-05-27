# M13.4B — Minimum Live Write Test Design

**Status:** Design / documentation only. **No code, no live API call,
no eToro `POST/DELETE/PUT/PATCH`, no secrets, no order placed.**
Approval chain: this doc → ChatGPT review → M13.5 implementation →
explicit separate go-ahead → first live write.

This document specifies the safest possible first live-write test for
eToro. The intent is to make every gate, every payload field, every
abort condition, and every audit-log expectation explicit *before* any
code can submit an order.

---

## 1. Purpose & scope

Define the controlled procedure under which a single, tiny, real-money
eToro `POST` would be issued for the first time. Nothing in this
milestone executes that procedure. Implementation (M13.5) must conform
to this document; deviations require a documented amendment.

**Out of scope for M13.4B:** policy code, broker adapters,
`bot/risk.py`, `bot/flywheel.py`, M15 modules, `main.py` wiring,
M13.4A policy lifting, any live API call.

---

## 2. Status flag & approval chain

| Stage    | Owner            | Deliverable                                | Gate                                   |
|----------|------------------|--------------------------------------------|----------------------------------------|
| M13.4B   | Claude (doc)     | This document                              | ChatGPT design review                  |
| M13.5    | Claude (code)    | Live writer + `etoro_live_enabled` lift    | ChatGPT code review + operator sign-off|
| First write | Operator      | One-shot CLI command with payload + nonce  | Operator explicit confirmation         |

The M13.4A validator currently rejects `routing.etoro_live_enabled = true`
with code `etoro_live_forbidden`. **That rejection is lifted only in
M13.5**, in the same gated commit that introduces the live writer, so
that the flag and the executing code ship together. M13.4B does not
touch that validator.

---

## 3. Preconditions

All preconditions are evaluated server-side **immediately before payload
construction**. Any false → abort with named reason; no `POST` is
emitted. Default on missing or stale data: **fail closed**.

| #     | Precondition                                                                  | Source                          | Fail action / reason code         |
|-------|-------------------------------------------------------------------------------|---------------------------------|-----------------------------------|
| 3.1   | M13.4A policy loaded and parses cleanly                                       | `load_policy(conn)`             | abort `policy_missing`            |
| 3.2   | `global.auto_trading_enabled == true`                                         | policy                          | abort `global_disabled`           |
| 3.3   | `etoro.auto_trading_enabled == true`                                          | policy                          | abort `broker_disabled`           |
| 3.4   | `etoro.max_single_trade_amount > 0` and within a tiny ceiling (see §4)        | policy                          | abort `single_trade_invalid`      |
| 3.5   | `etoro.max_auto_trading_capital > 0` and within a tiny ceiling (see §4)       | policy                          | abort `broker_capital_invalid`    |
| 3.6   | `global.kill_switch == false`, `ibkr.kill_switch == false`, `etoro.kill_switch == false` | policy             | abort `global_kill_switch` / `broker_kill_switch` |
| 3.7   | `routing.etoro_live_enabled == true` — **future precondition only**. The M13.4A validator rejects this; the M13.5 commit lifts the rejection and adds explicit broker-side guards. M13.4B does not lift the guard. | policy | abort `etoro_live_disabled`      |
| 3.8   | No existing open eToro position on the chosen instrument                      | `GET /trading/info/portfolio`   | abort `existing_position`         |
| 3.9   | No open eToro order on the chosen instrument                                  | (TBD: open-orders read endpoint; verify in M13.5 from `api-portal.etoro.com`) | abort `existing_order`            |
| 3.10  | Operator-triggered command (CLI / dashboard button), **never** scanner-triggered. The scanner code path must not be able to construct an eToro live payload. | runtime context | abort `non_operator_trigger`     |
| 3.11  | Operator has the eToro web UI open, logged in, on the target instrument page  | operator attestation            | abort `operator_not_ready`        |
| 3.12  | US market is open (regular session) and within the configured execution window | clock + market-calendar         | abort `market_closed`             |
| 3.13  | Fresh quote/rate retrieved within the last N seconds (N defined in M13.5)     | `GET /market-data/instruments/rates` | abort `stale_quote`          |
| 3.14  | Spread within tolerance (tolerance defined in M13.5)                          | quote bid/ask                   | abort `spread_too_wide`           |
| 3.15  | eToro account base currency confirmed (GBP per memory; M13.5 must read live)  | `GET /me`                       | abort `account_currency_unknown`  |

---

## 4. Test size

**Order style:** cash-amount market-open buy via
`POST /api/v1/trading/execution/market-open-orders/by-amount`.
Cash amount, **not** share count: a $10 cap will not buy "1 share AAPL"
if AAPL > $10, but it can buy fractional shares.

**Sizing rules (all must hold):**

1. `Amount <= etoro.max_single_trade_amount`
2. `Amount <= etoro.max_auto_trading_capital` (and headroom against any
   already-deployed capital tracked in M13.5)
3. `Amount <= global.max_auto_trading_capital` (when > 0)
4. `Amount >= eToro platform minimum order amount`

### 4.1 eToro platform minimum

eToro publicly documents a **$10 minimum** for opening a stock / ETF /
crypto position on the retail platform (from $10, fractional). Sources:

- eToro Help / news ("Enjoy lower minimum trade sizes on eToro",
  <https://www.etoro.com/news-and-analysis/etoro-updates/introducing-lower-unified-minimum-trade-sizes/>)
- eToro App Store description: "fractional investments for as low as $10"
- Independent summary citing eToro Help Center
  (<https://wikitoro.org/trading/what-is-the-minimum-you-can-invest-in-etoro>):
  *"You can open a stock, ETF, or crypto position from $10."*

**API-specific minimum:** the builders.etoro.com docs do not currently
publish an explicit numeric floor for
`/trading/execution/market-open-orders/by-amount`. The example in the
public guide uses `"Amount": 5000` as illustration only, not as a
minimum.

> **TBD — must be verified from official eToro docs or a safe read-only
> pre-check before M13.5 execution.** The first M13.5 step must call a
> read-only endpoint (e.g. instrument metadata or a demo-environment
> dry-run on `POST /trading/execution/demo/market-open-orders/by-amount`,
> which is *demo only* and does not move real money) to confirm the
> minimum accepted `Amount` for the chosen real-money instrument. Only
> after that confirmation may the real-money `Amount` be locked in.

### 4.2 First-write Amount range

- Floor: max(`$10`, API-confirmed minimum from §4.1, eToro fee threshold
  if applicable)
- Ceiling: `etoro.max_single_trade_amount` set by the operator in the
  M13.4A panel, kept deliberately tiny (recommendation: **≤ $25** for
  the very first write, set during M13.5 sign-off; the doc does not
  pre-commit a number)

---

## 5. Instrument selection rules

Selection criteria only. **The specific symbol is deferred to the
M13.5 sign-off message — M13.4B does not pre-commit one.**

Required:

- US equity or US ETF listed on NYSE / NASDAQ
- Highly liquid, mega-cap or major-index ETF (e.g. categories: top-50
  S&P 500 names, SPY-class broad ETFs)
- Non-leveraged
- Long only (`IsBuy: true`)
- Leverage = 1
- Not on the eToro restricted/special list (operator to verify via UI)

Disallowed for the first write:

- Crypto
- Penny stocks (< $5 share price)
- Inverse / leveraged ETFs (TQQQ, SQQQ, SOXL, etc.)
- Options
- CFDs
- Volatile single-stocks with extreme intraday range
- Anything denominated outside USD
- Anything subject to corporate-action windows in the next 5 trading days
- Anything matching an existing eToro position the account already holds

---

## 6. Order type

- One single market-open order by cash amount
- One single `POST`
- No bracket order on the first write (no SL, no TP)
- No repetition, no loop, no auto re-submit on any failure
- No strategy-generated payload — payload fields are operator-typed
  parameters
- No multi-order batch

---

## 7. Safety gates (order of evaluation)

Gates are evaluated **in the order below**. First failure aborts and
records the reason. No gate is skipped, even if a prior one already
established the same condition.

| # | Gate                                              | Pass condition                                  | Fail action                    |
|---|---------------------------------------------------|-------------------------------------------------|--------------------------------|
| 1 | Policy load                                       | `load_policy` returns dict                      | abort `policy_missing`         |
| 2 | Global kill switch                                | `global.kill_switch == false`                   | abort `global_kill_switch`     |
| 3 | Global auto-trading                               | `global.auto_trading_enabled == true`           | abort `global_disabled`        |
| 4 | Broker kill switch                                | `etoro.kill_switch == false`                    | abort `broker_kill_switch`     |
| 5 | Broker auto-trading                               | `etoro.auto_trading_enabled == true`            | abort `broker_disabled`        |
| 6 | Broker in `allowed_brokers`                       | `etoro_real` (or its M13.5 successor) listed    | abort `broker_not_allowed`     |
| 7 | `etoro_live_enabled`                              | `true` (per §3.7)                               | abort `etoro_live_disabled`    |
| 8 | `Amount <= max_single_trade_amount`               | per §4                                          | abort `exceeds_single_trade`   |
| 9 | `Amount <= broker.max_auto_trading_capital` headroom | per §4                                       | abort `exceeds_broker_capital` |
|10 | `Amount <= global.max_auto_trading_capital` headroom (if > 0) | per §4                              | abort `exceeds_global_capital` |
|11 | `etoro.max_open_positions` headroom               | live position count from `GET /trading/info/portfolio` < cap | abort `exceeds_open_positions` |
|12 | Daily loss cap                                    | `realised_daily_loss < etoro.max_daily_loss`; if data unavailable → **fail closed** | abort `daily_loss_unknown` / `daily_loss_breached` |
|13 | Market hours                                      | within regular US session and configured window | abort `market_closed`          |
|14 | Quote freshness                                   | latest rate younger than N seconds              | abort `stale_quote`            |
|15 | Spread tolerance                                  | `(ask - bid) / mid <= tolerance`                | abort `spread_too_wide`        |
|16 | Operator confirmation (per-payload nonce, §10)    | echoed nonce matches issued nonce               | abort `confirmation_failed`    |

M12 live safety principles (kill switch, broker reconciliation, fail-
closed defaults, evidence-based acceptance) continue to apply on top
of the above.

---

## 8. Lifecycle (state machine)

### 8.1 Proposed M13.5 lifecycle statuses

The following names are proposed for the eToro live-write path. **They
are not implemented in M13.4B.** M13.5 must finalise them, update any
controlled status documentation/comments, and ensure they do not
collide with existing M12 IBKR statuses (`submitted`, `filled`,
`cancelled`, `broker_rejected`, `broker_unready`).

| Status              | Meaning                                                              |
|---------------------|----------------------------------------------------------------------|
| `pending_live_write`| `execution_intent` row inserted; before policy/risk gates run        |
| `policy_rejected`   | Any §7 gate 1–10 failed                                              |
| `risk_rejected`     | §7 gates 11–15 failed (per-broker risk / market state)               |
| `awaiting_confirm`  | Payload built, nonce issued, waiting for operator echo               |
| `submitted`         | `POST` accepted, order ID extracted (reuses M12 status name)         |
| `filled`            | Order filled, position ID captured (reuses M12 status name)          |
| `broker_rejected`   | `POST` returned an error response (reuses M12 status name)           |
| `cancelled`         | Order cancelled before fill (reuses M12 status name)                 |
| `unverified`        | `POST` returned, but post-POST polling could not confirm state (§8.3)|

### 8.2 Sequence

```
operator one-shot CLI
  └─> insert execution_intent (status=pending_live_write)
       └─> §7 gates 1–10  ── fail ──> update status=policy_rejected; stop
            └─> §7 gates 11–15 ── fail ──> update status=risk_rejected; stop
                 └─> build payload (§9)
                      └─> issue per-payload nonce (§10)
                           └─> update status=awaiting_confirm
                                └─> operator echoes nonce ── mismatch ──> stop, no POST
                                     └─> POST /trading/execution/market-open-orders/by-amount
                                          ├─ HTTP 200 + valid schema ──> extract order ID,
                                          │                              update status=submitted,
                                          │                              submitted_at, lifecycle_json
                                          ├─ HTTP error / schema mismatch ──> status=broker_rejected
                                          │                                   (log raw response, redacted)
                                          └─ network/timeout ──> status=unverified
                                               └─> §8.3 polling
```

### 8.3 Post-POST polling

- Up to **5 retries × 2 seconds** against the order-status read endpoint
  (exact path TBD from `api-portal.etoro.com` — confirmed in M13.5).
- On fill confirmation: update `status=filled`, `filled_at`,
  `fill_price`, `fill_qty`, capture `positionId`, write
  `lifecycle_json` snapshot.
- On confirmed reject/expire: `status=broker_rejected` or `cancelled`.
- If state still unknown after the 5×2s window:
  - `status=unverified`
  - **Telegram alert to operator**
  - **All automation stops** (process exits, watchdog flag set)
  - **No second `POST` under any condition**
  - Manual reconciliation against eToro web UI; operator updates the
    intent row out-of-band

### 8.4 Outcome linkage to `signal_outcomes`

Deferred to M13.5. The first live write may be standalone (no upstream
signal row); M13.5 must define the linkage rules for both cases.

### 8.5 Manual close plan (mandatory)

- Operator must have the eToro web UI open and authenticated on the
  target instrument page **before** the nonce echo.
- Default close plan: **manually close via eToro web UI after
  verification, or within a defined short window such as 24 hours**,
  unless the M13.5 sign-off message specifies a shorter window.
- A future API-side close command may be designed in a later
  milestone but is **not required for the first write**.
- The close plan is written into `lifecycle_json` at submit time.

---

## 9. eToro write payload — required evidence before M13.5

### 9.1 Endpoint paths (confirmed from public eToro builders docs)

- **Base:** `https://public-api.etoro.com/api/v1`
- **Live write (real money):**
  `POST /trading/execution/market-open-orders/by-amount`
  (cited in builder FAQ: *"opening by notional amount is done through
  `/trading/execution/market-open-orders/by-amount`"* —
  <https://builders.etoro.com/faq>)
- **Demo write (sandbox, no real money):**
  `POST /trading/execution/demo/market-open-orders/by-amount`
  (cited in builder algo-trading guide:
  <https://builders.etoro.com/use-cases/algo-trading>)
- **Portfolio read:** `GET /trading/info/portfolio` (verified read-only
  in M13 discovery)
- **Trade history read:** `GET /trading/info/trade/history` (verified)
- **Account info:** `GET /me` (verified)
- **Market data search:** `GET /market-data/search` (verified)
- **Rates:** `GET /market-data/instruments/rates?instrumentIds=<csv>`
  (per M13.1 correction — TBD live-verified in M13.5)
- **Order status / open orders:** TBD — exact paths must be read from
  `api-portal.etoro.com` operational reference and recorded in the
  M13.5 design before any live `POST`.

### 9.2 Auth headers (confirmed)

```
x-api-key:    <eToro public API key>      ← .env-injected, never logged
x-user-key:   <eToro per-user key>        ← .env-injected, never logged
x-request-id: <fresh UUID per request>    ← one per POST, deterministic
```

### 9.3 Payload shape (confirmed from public guide)

```json
{
  "InstrumentID": <int>,
  "Amount": <decimal USD>,
  "IsBuy": true
}
```

M13.5 must additionally confirm whether the real-money endpoint accepts
or requires:
- `Leverage` (must be `1` per §5)
- `StopLoss` / `TakeProfit` (omitted on first write)
- Idempotency / request-deduplication field

### 9.4 Expected success response

From the public guide example, the demo response shape is:

```json
{
  "data": {
    "positionId": <int>,
    ...
  }
}
```

M13.5 must record the **complete real-money** response schema before
the first live `POST`, including:
- `data.positionId` (or equivalent) location
- order ID vs position ID distinction
- any `status` / `state` field values returned synchronously
- any execution timestamps
- any fee fields

### 9.5 Expected failure response

TBD from `api-portal.etoro.com`. M13.5 must record:
- HTTP status codes used for validation errors vs. server errors vs.
  insufficient funds vs. instrument-restricted
- Body shape for error responses (likely `{"error": {...}}` or similar)
- Whether partial fills can appear under any error path

### 9.6 Order ID → status mapping

TBD. M13.5 must record:
- Exact status endpoint path
- Required URL/query parameters (likely `orderId` or `positionId`)
- Response shape and status vocabulary
- How `positionId` is captured if not returned synchronously by §9.4

---

## 10. Confirmation token — per-payload nonce (Option B)

The first live write requires a per-request nonce bound to the exact
payload. A static phrase ("I CONFIRM LIVE ETORO WRITE") is **not
acceptable**.

### 10.1 Nonce generation

After §7 gates 1–15 pass and the payload is built, the system:

1. Computes a payload digest:
   `digest = sha256(canonical_json(payload) || timestamp_ms).hex()[:8]`
2. Displays to operator:
   ```
   ┌───────── LIVE WRITE CONFIRMATION ────────┐
   │ Endpoint:      POST <real path>          │
   │ Instrument:    <symbol>  (id=<int>)      │
   │ Side:          BUY                       │
   │ Amount:        $<amount>                 │
   │ Leverage:      1                         │
   │ Account base:  <ccy>                     │
   │ Spread:        <bps>                     │
   │ Caps:          single=$X, broker=$Y,     │
   │                global=$Z, openPos=N/M    │
   │                                          │
   │ NONCE: <8-char digest>                   │
   │ To proceed, echo: CONFIRM <NONCE>        │
   └──────────────────────────────────────────┘
   ```
3. Records the nonce in `execution_intent.lifecycle_json` along with
   the payload digest and a TTL (e.g. 60 seconds).

### 10.2 Nonce echo & validation

- Operator must type `CONFIRM <NONCE>` on stdin / dashboard.
- On mismatch, or after TTL expiry, abort `confirmation_failed`. No
  `POST` is emitted. The nonce is single-use; replays are rejected.
- The `x-request-id` header sent with the POST is derived from the same
  digest so the request is also idempotency-bound to the confirmation.

---

## 11. Telegram notifications

Reuse the existing notifier (used by M15.1 gateway alerts and current
trade-signal alerts). **No new notification channel is introduced.**

Required notifications:

| Trigger                             | Channel  | Content (redacted)                              |
|-------------------------------------|----------|-------------------------------------------------|
| Submit attempt (after gates pass)   | Telegram | symbol, amount, broker=eToro                    |
| `submitted` (order ID captured)     | Telegram | symbol, amount, orderId                         |
| `filled`                            | Telegram | symbol, fill_price, fill_qty, positionId        |
| `broker_rejected` / failure         | Telegram | symbol, amount, error code, redacted body excerpt |
| `unverified` (post-POST poll fail)  | Telegram | symbol, amount, **STOP signal** + manual reconcile prompt |

Secrets, headers, and full response bodies are **never** sent to
Telegram. Only the fields above. Bodies, if shown anywhere, are
written to local audit logs with the redaction rules in §12.

---

## 12. Audit logging & secrets

- All credentials (`x-api-key`, `x-user-key`) are read from `.env`,
  never from `settings.yaml`, never committed, never echoed to stdout,
  Telegram, or `lifecycle_json`.
- The raw eToro response is written to a local rotating audit log,
  with the following fields **redacted**:
  - any `x-api-key` / `x-user-key` headers (request side)
  - any returned account ID truncated to last 4 chars
  - any returned tokens or session fields
- `lifecycle_json` stores the payload as sent **minus** auth headers,
  plus the response **with redactions applied**.
- Audit log rotation, retention, and path are TBD in M13.5.

---

## 13. Idempotency — exactly one `execution_intent` row

- One operator command → exactly one new `execution_intent` row.
- The row is inserted in status `pending_live_write` **before** any
  gate evaluation; subsequent gates only update its status and
  `lifecycle_json`. They never insert a second row for the same
  command.
- `x-request-id` UUID is recorded against the row at POST time.
- Re-issuing the same operator command produces a **new** intent row
  with a new UUID; nonce TTL prevents accidental double-submit within
  the same command window.
- The reconciliation path (M12 broker reconciliation) must be able to
  link a returned `orderId` back to exactly one intent row.

---

## 14. Abort & rollback (consolidated)

Conditions that abort **before** any `POST`:

- Any §3 precondition fails
- Any §7 gate fails
- Nonce mismatch or TTL expiry
- Operator pressed Ctrl-C / closed the dashboard tab
- eToro API returns 401/403 on a preceding read-only call (auth broken)
- Quote/rate missing or stale beyond tolerance
- Account base currency cannot be confirmed

Conditions that mark the intent terminal **after** a `POST` was sent:

- 4xx/5xx → `broker_rejected`
- Response schema mismatch → `broker_rejected` + raw response logged
- Network failure or post-POST polling failure → `unverified` (§8.3)

**No automatic retry under any condition.** All recovery is manual,
operator-initiated, and produces a new intent row.

---

## 15. Required pre-implementation evidence (M13.5 entry criteria)

Before any line of M13.5 code submits a real-money `POST`:

1. Exact real-money endpoint path documented (§9.1)
2. Exact request payload schema documented and dry-run validated
   against the **demo** endpoint (§9.3)
3. Expected success response fields including `orderId` /
   `positionId` location confirmed against a demo response (§9.4)
4. Expected failure response shape documented for at least:
   insufficient funds, invalid instrument, restricted instrument,
   auth failure (§9.5)
5. Status endpoint path + order ID mapping documented (§9.6)
6. Position ID capture path documented (§9.6)
7. `execution_intent` lifecycle field map documented for each
   transition (§8.1, §8.2)
8. Audit log redaction policy implemented and tested with a fake key
   so no secret appears anywhere except `.env` (§12)
9. Idempotency: unit-tested guarantee of exactly one intent row per
   operator command (§13)
10. API-confirmed minimum `Amount` for the chosen instrument (§4.1)

---

## 16. Non-goals (explicit)

- ❌ Automatic scanner-to-eToro live trading
- ❌ Multi-order strategy
- ❌ Short selling
- ❌ Leverage (anything other than `1`)
- ❌ Crypto
- ❌ Copy-trading / agent-portfolio path
- ❌ Unattended live execution
- ❌ Bracket / SL / TP on first write
- ❌ Automatic close
- ❌ Second `POST` of any kind for any reason without a fresh operator
  command and a fresh nonce
- ❌ Lifting `etoro_live_enabled` in M13.4B (lifted in M13.5 only)

---

## 17. Acceptance criteria for M13.4B

- ✅ This file present
- ✅ Docs-only commit
- ✅ No production code changed
- ✅ No tests required
- ✅ No live API call performed
- ✅ No secrets present
- ✅ No eToro `POST/DELETE/PUT/PATCH` performed
- ✅ One small docs-only commit pushed to `origin/main`
- ✅ ChatGPT design review gates M13.5

---

## 18. Sources cited

- eToro Builders FAQ —
  <https://builders.etoro.com/faq>
- eToro Builders — Algo Trading guide —
  <https://builders.etoro.com/use-cases/algo-trading>
- eToro Builders — API Quick Reference —
  <https://builders.etoro.com/reference>
- eToro — "Introducing lower unified minimum trade sizes" —
  <https://www.etoro.com/news-and-analysis/etoro-updates/introducing-lower-unified-minimum-trade-sizes/>
- eToro iOS app listing ("fractional investments for as low as $10") —
  <https://apps.apple.com/us/app/-/id674984916>
- Independent summary of eToro Help Center minimums —
  <https://wikitoro.org/trading/what-is-the-minimum-you-can-invest-in-etoro>
- eToro API Portal (operational reference, TBD items to be resolved here in M13.5) —
  <https://api-portal.etoro.com/>
