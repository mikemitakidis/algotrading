# M13.1 — eToro Integration Design (Documentation-Only)

**Status:** Design phase. No production code in this commit.
**Predecessor:** M13 discovery (closed in commit `61af4f5`).
**Successor (not yet started):** M13.2 read adapter implementation.

This document captures the integration design for adding eToro as a
second broker alongside the existing IBKR path. The IBKR path is the
active execution path today and remains unchanged.

## Goals

1. Add a second broker (eToro) capable of executing on the Real Main
   account, using the existing M10 `OrderIntent` type and M10 execution
   intents lifecycle without schema changes.
2. Keep the IBKR path bit-for-bit unchanged in behaviour and risk.
3. Phase implementation so each step is independently reviewable,
   reversible, and not load-bearing for live trading until explicit
   operator approval.
4. Use only the public, documented eToro Public API surface
   (`https://public-api.etoro.com/api/v1/`) — no Agent Portfolios.

## Non-goals (for M13.1 specifically)

- No production code in this commit.
- No `etoro_read.py`, no `etoro_broker.py`, no dashboard changes,
  no `main.py` changes, no `.env.example` entries, no `bot/risk.py`
  changes, no changes to any M15 module.
- No POST or DELETE calls to eToro from anywhere.
- No claim that eToro execution is ready.

## Architecture summary

The existing `bot/brokers/` directory contains the broker abstraction
(`PaperBroker`, `IBKRBroker`). The eToro broker will be a peer of
those, registered by broker name in the existing factory pattern.
Selection between brokers stays a runtime config choice
(`BROKER=ibkr_live` / `BROKER=etoro_real` / `BROKER=paper`).

The eToro broker uses the same authentication and request pattern for
every call:

- Base URL: `https://public-api.etoro.com/api/v1`
- Required headers on every request:
  - `x-api-key: <main account public key>`
  - `x-user-key: <main account user key>`
  - `x-request-id: <fresh UUIDv4 per request>`
  - `Accept: application/json`
  - `Content-Type: application/json` (POST only)

Credentials live in `.env` (gitignored) on the server. Never committed.
The bot reads them at startup; they are not written to logs.

## Verified rates endpoint (R4 correction)

The R4 404 during discovery used the wrong path. The eToro OpenAPI spec
(authoritative source) confirms the correct path is:

```
GET https://public-api.etoro.com/api/v1/market-data/instruments/rates
        ?instrumentIds=<csv list of int32>
```

Note the `/instruments/` segment that was missing in R4. Path resolved
from the OpenAPI spec at
`https://api-portal.etoro.com/api-reference/market-data/retrieve-current-market-rates-and-pricing-information-for-specified-instruments.md`
— no additional curl call needed.

Key facts:

- Up to 100 instrument IDs per call.
- Response shape: `{"rates": [{"instrumentID": int, "ask": float,
  "bid": float, "lastExecution": float, "conversionRateAsk": float,
  "conversionRateBid": float, "date": iso8601, ...}]}`.
- Several response fields are flagged "Obsolete" in the schema
  (`unitMargin*`, `bidDiscounted`, `askDiscounted`). The adapter will
  ignore them and rely on `bid`, `ask`, `lastExecution`,
  `conversionRateBid`, `conversionRateAsk`, and `date`.

## Rate limits (operator-relevant)

From the official rate-limits documentation, limits are per user key
over a 1-minute rolling window:

| Class                     | Limit (req/min) | Endpoints                                            |
|---------------------------|-----------------|------------------------------------------------------|
| Read (GET data)           | 60              | `/market-data/*`, `/trading/info/*`, feeds, watchlists |
| Write / heavy (POST/DELETE) | 20            | `/trading/execution/*`, watchlist mutations           |

Implications for design:

- Cache non-volatile data (instrument IDs, exchange metadata) at
  startup; do not look them up on every order.
- Polling for order/fill status must respect the 60/min read limit.
  A worst case of 20 simultaneous unfilled orders polled every second
  would burn the entire read budget — design uses backoff or a single
  poll-all-orders endpoint per cycle.
- `429 Too Many Requests` responses must trigger exponential backoff
  in the read adapter and a hard stop in the write path (never retry
  a 429 on a POST without operator review).

## Phase plan (each phase is its own milestone, gated)

| Milestone | Scope                                                   | Touches production code? |
|-----------|---------------------------------------------------------|--------------------------|
| **M13.1** | Docs only (this commit)                                 | No                       |
| M13.2     | Read adapter (`bot/brokers/etoro_read.py`); GET-only    | Yes, read-only           |
| M13.3     | `PaperEtoroBroker` dry-run + schema validation tests    | Yes, no real API calls   |
| M13.4     | Minimum viable single live write (gated approval)       | One controlled write     |
| M13.5     | Production `etoro_broker.py` + dashboard integration    | Yes, live trading        |
| M13.6     | Cross-broker risk pool decision + reconciliation        | Touches risk policy      |

Each milestone is independently committed, reviewed by ChatGPT, and
operator-approved before the next begins. No skipping.

## Currency model

| Account | Base currency | Source of truth |
|---------|--------------|-----------------|
| IBKR live | GBP        | IBKR account                |
| eToro Real | USD       | eToro `/trading/info/real/pnl` `accountCurrencyId` field |

Cross-broker portfolio calculations (combined position sizing, combined
risk limits) need a currency normalisation step. M13.1 design decision:
**defer cross-broker portfolio math to M13.6.** Until then, each broker
is treated as an independent portfolio with independent risk limits.
See `M13_1_risk_compatibility.md` for the full risk decision.

## IBKR path isolation guarantees

The eToro work must not regress the IBKR path. Concrete guarantees:

- `bot/brokers/ibkr_broker.py`: untouched across the entire M13 series.
- `bot/risk.py`: unchanged in M13.1–M13.5; only touched in M13.6
  (cross-broker risk pool), and only with explicit approval.
- `bot/gateway_watchdog.py`, `bot/recovery_executor.py`,
  `bot/heartbeat.py`: untouched (M15 series intact).
- `bot/flywheel.py`: schema unchanged. The existing execution_intents
  table already has the columns needed for eToro lifecycle
  (see `M13_1_lifecycle.md`).
- `main.py` broker selection logic unchanged except for adding
  `etoro_real` as a recognised `BROKER=` value when M13.5 lands.

## What gets committed in M13.1 (this milestone)

Four files under `docs/`:

| File | Purpose |
|------|---------|
| `docs/M13_1_design.md` | This file |
| `docs/M13_1_order_schema_mapping.md` | Field mapping: `OrderIntent` ↔ eToro REST bodies |
| `docs/M13_1_lifecycle.md` | execution_intents lifecycle for eToro orders |
| `docs/M13_1_risk_compatibility.md` | M14 risk-layer compatibility decision |

No other files are added or modified.
