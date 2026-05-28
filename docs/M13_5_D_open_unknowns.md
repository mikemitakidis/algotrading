# M13.5.D — eToro Open Unknowns: Provenance & Confidence

**Type:** docs-only. No code change, no live/demo call, no eToro write.
**Purpose:** consolidate every open unknown from M13.5.A §8 (and the
M13.4B design) into one auditable register, each with an honest
confidence label and its provenance in the repo.

## Honesty statement

The raw eToro OpenAPI / official source specs were **not vendored** into
this repo when they were consulted for M13.5.A. This document therefore
does **not** constitute fresh primary-source verification. Where a item
is marked `CONFIRMED`, it means the repo already carries a clear
M13.5.A / M13.4B citation derived from official docs at the time; it is
**not** re-verified here. Items resting on our own code or defensive
design are `ASSUMED`. Items needing a real response are `DEFERRED`.

## Label key

- **CONFIRMED** — repo contains a clear M13.5.A/M13.4B provenance from
  official docs; carried forward, not re-fetched.
- **ASSUMED** — based mainly on our code / defensive parser / current
  design choice, not on a cited official-doc fact.
- **DEFERRED** — requires future live / dry-run / go-live evidence.

---

## Register

### §8.1 — No-SL / No-TP encoding
- **Implementation assumption:** the operator CLI sends explicit
  `IsNoStopLoss=true` and `IsNoTakeProfit=true` in the payload
  (`tools/etoro_live_write.py` `_build_payload`).
- **Label:** ASSUMED.
- **Provenance:** M13.5.A §8.1 / §2.4 notes eToro "accepts either
  omission or the booleans" but does not lock which form is cleanest;
  the explicit-boolean choice is our recommendation, not a cited
  requirement. Code: `tools/etoro_live_write.py`.
- **Upgrade path:** a verified sandbox/demo (once enabled) or the
  go-live dry-run that shows the explicit-boolean payload is accepted
  cleanly; or a vendored OpenAPI snapshot showing the required field
  semantics. Either upgrades to CONFIRMED.

### §8.2 — Failure response body shape
- **Implementation assumption:** the parser is defensive across shapes —
  it reads flat `errorCode`/`errorMessage`, a nested `{"error": {...}}`,
  a bare `message`, or raw text (`response_parser.parse_error`).
- **Label:** ASSUMED.
- **Provenance:** M13.5.A §2.5 / §8.2 — "OpenAPI does not enumerate
  failure shapes"; HTTP status codes were treated as confirmed, body
  shape was explicitly left for empirical confirmation. Code:
  `bot/etoro/response_parser.py::parse_error`.
- **Upgrade path:** capture real (redacted) 4xx/5xx bodies from the
  documented failure cases (invalid InstrumentID, oversized Amount,
  restricted instrument, missing header, invalid Leverage) during a
  go-live dry-run; record them and pin the parser to the observed
  shape. → CONFIRMED.

### §8.3 — `statusID` semantics across order types
- **Implementation assumption:** `statusID ∈ {0,1,2,3,4}` mapped as
  0/4→submitted, 1→filled, 2→cancelled, 3→broker_rejected; an unknown
  value raises and aborts (`response_parser.ETORO_STATUS_ID_TO_INTERNAL`,
  `KNOWN_STATUS_IDS`).
- **Label:** CONFIRMED (with caveat).
- **Provenance:** M13.5.A §2.7 derived the 0–4 mapping from the OpenAPI
  `statusID` description. Caveat: the same description warns meaning
  "may vary based on order type and system configuration" (M13.5.A
  §8.3). We send **market orders only** and abort on any out-of-set
  value, so the confirmed mapping holds for our single order type.
- **Upgrade path:** none required for market orders; if other order
  types are ever used, re-verify against a vendored spec.

### §8.4 — `x-request-id` idempotency behaviour
- **Implementation assumption:** `x-request-id` is best-effort tracking
  only; the exactly-one guarantee comes from (a) per-payload single-use
  nonce, (b) the `execution_intents` row inserted before the POST with
  `client_intent_id`/`nonce_digest`/`x_request_id` in `lifecycle_json`,
  and (c) the CLI exiting after one POST.
- **Label:** CONFIRMED (resolved by design, not by eToro dedupe).
- **Provenance:** M13.5.A §8.4. Code: `bot/etoro/lifecycle.py`,
  `bot/etoro/nonce.py`, `tools/etoro_live_write.py`. Note: M13.5.A
  states "no new column"; the identifiers live in the existing
  `lifecycle_json` (M15.0). No schema migration was added in M13.5.B.
- **Upgrade path:** n/a — our guarantee does not depend on eToro-side
  idempotency. (If `lifecycle_json` scanning ever proves too slow, a
  real `client_intent_id` column would be a separate reviewed proposal,
  per M13.5.A §8.4.)

### §8.5 — `closed_manual` status name
- **Implementation assumption:** the status `closed_manual` is used for
  an operator close via the eToro web UI, applied through
  `bot/etoro/lifecycle.py`; `filled → closed_manual` is an explicitly
  permitted transition (M13.5.B correction).
- **Label:** CONFIRMED (internal naming, resolved in M13.5.B).
- **Provenance:** M13.5.A §8.5 proposed the name; M13.5.B finalised it
  and wired it through `lifecycle.py` + `tools/etoro_reconcile.py`.
  This is an internal vocabulary choice, not an eToro fact.
- **Upgrade path:** n/a.

### §8.6 — Minimum API `Amount`
- **Implementation assumption:** preflight rejects `Amount` below
  `amount_min`, CLI default `--amount-min=10.0`
  (`bot/etoro/live_broker.py` preflight, `tools/etoro_live_write.py`).
- **Label:** ASSUMED.
- **Provenance:** M13.5.A §8.6 — the $10 figure is eToro's **retail
  platform** minimum from public pages, **not** a documented API
  minimum; the OpenAPI does not state an API-side floor. Code default
  encodes the $10 floor defensively.
- **Upgrade path:** probe `Amount = 1 / 5 / 10` against a verified
  sandbox (when enabled) or confirm at go-live; set `amount_min` to
  `max(platform floor, observed API minimum, fee-coverage threshold)`.
  → CONFIRMED.

### §8.7 — Cancel-endpoint path
- **Implementation assumption:** no cancel call exists in the live-write
  happy path (`bot/etoro/live_broker.py` has no cancel); cancellation,
  if ever needed, is operator-driven via the eToro web UI and recorded
  by the reconciliation tool.
- **Label:** DEFERRED.
- **Provenance:** M13.5.A §2.3 / §8.7 — the real-environment cancel path
  is referenced in the OpenAPI index but the exact path was **not**
  captured/vendored. M13.5.A planned to fetch
  `api-portal.etoro.com/.../openapi.json` to record it; that fetch was
  not saved in-repo.
- **Upgrade path:** when an automated cancel is actually required
  (not in M13's scope), fetch and **vendor** the OpenAPI path, then
  implement + test behind preflight. → CONFIRMED. Not needed to close
  M13.

---

## Summary table

| Item | Label | Rests on |
|---|---|---|
| §8.1 No-SL/No-TP encoding | ASSUMED | our payload choice |
| §8.2 Failure body shape | ASSUMED | defensive parser |
| §8.3 statusID semantics | CONFIRMED* | M13.5.A spec read; market-only, abort-on-unknown |
| §8.4 x-request-id idempotency | CONFIRMED | our design (nonce + row), not eToro dedupe |
| §8.5 closed_manual name | CONFIRMED | internal naming, M13.5.B |
| §8.6 Minimum Amount | ASSUMED | retail $10 floor, not API-documented |
| §8.7 Cancel-endpoint path | DEFERRED | spec not vendored; not invoked |

\* CONFIRMED for the single market-order type we send; caveated by the
OpenAPI "may vary by order type" note.

**None of the ASSUMED/DEFERRED items block closing M13** (live-write
capability built, gated, deployed, no-write verified). All of them
block the **first funded real order**, which is a separate go-live
event outside M13.

---

## Process note — provenance discipline for money-touching code

This gap (consulting an external API spec without saving it) must not
recur for code that can move real money. Going forward:

1. **Any external API spec used for money-touching code must be saved
   or indexed in the repo at the time it is used.**
2. **Minimum saved record per source:** the URL, the fetched date, the
   endpoint/path(s) relied on, the relevant excerpt or a faithful
   summary, and which code/design decision it supports.
3. **If license/terms permit:** vendor the raw OpenAPI/spec snapshot
   (e.g. `docs/vendor/etoro_openapi_<date>.json`) and record its hash.
4. **If license/terms forbid redistribution:** save a source index with
   limited excerpts plus metadata (URL, date, content hash/length) so
   the provenance is reproducible without redistributing the full spec.
5. A `CONFIRMED` label in any future evidence pack must point to one of
   these saved records, not to an un-saved fetch or to model memory.

Applying this retroactively to the items above is the explicit
**upgrade path** for every ASSUMED/DEFERRED entry: save the source,
then re-label.
