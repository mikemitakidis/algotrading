# M13.5.B — eToro Live Writer Implementation

**Status:** implemented, tested offline. NO real-money POST performed.
**Predecessor:** M13.5.A evidence pack (commit `2b90a37`).
**Scope:** ship the operator-only eToro live-write capability behind a
double live flag + per-payload nonce, with strict scanner isolation.

---

## 1. What this milestone delivers

A real-money eToro order can now be placed, but **only** by an operator
running `tools/etoro_live_write.py` from a shell, and **only** when every
one of the following is simultaneously true:

1. `routing.etoro_live_enabled = true` in the Broker Allocation policy
   (set via the dashboard Risk page, persisted in `portfolio_risk_state`).
2. `ETORO_LIVE_ENABLED=true` in `.env`.
3. The 16-gate preflight passes (policy validity, global/broker
   switches, kill switches, capital/position/loss caps, market state,
   quote freshness, spread).
4. The operator echoes the correct per-payload nonce
   (`CONFIRM <8-hex-digest>`) at run time.

If any one is missing, no POST is emitted. The scanner, strategy, risk
manager, dashboard, and `get_broker()` cannot reach the live writer at
all — it is constructed solely by the operator CLI.

---

## 2. New modules (`bot/etoro/`)

| Module | Responsibility | Network? |
|---|---|---|
| `nonce.py` | Per-payload, single-use, TTL-bound confirmation nonce | no |
| `audit.py` | Redaction primitives + rotating JSONL audit log | no |
| `response_parser.py` | Parse eToro open/order-info responses per OpenAPI | no |
| `lifecycle.py` | Sole writer of eToro intent rows; sets `submitted_at` | DB only |
| `order_poller.py` | Bounded 5×2s post-POST polling; fails closed to `unverified` | via injected reader |
| `signal_only_broker.py` | Wrapper that records intents without submitting; preserves Telegram | no |
| `live_broker.py` | `EtoroLiveBroker`: preflight + single POST. **Operator-only.** | yes (operator only) |

## 3. New tools (`tools/`)

| Tool | Responsibility |
|---|---|
| `etoro_live_write.py` | The **only** constructor of `EtoroLiveBroker`. `oneshot` subcommand: preflight → nonce confirm → single POST → bounded poll. |
| `etoro_reconcile.py` | Controlled reconciliation after an `unverified` outcome. Updates lifecycle via `bot.etoro.lifecycle` only — never raw SQL, never an eToro write. Has an import-time guard against being loaded alongside `live_broker`. |

## 4. Modified production files

- `bot/broker_allocation.py`
  - `ALLOWED_BROKER_WHITELIST` now includes `etoro_real`; `FORBIDDEN_BROKERS`
    is now empty. Live writes are gated at **runtime**, not at policy
    validation.
  - The M13.4A `etoro_live_forbidden` rejection is removed.
  - `is_auto_trading_allowed()` now uses strict `routing.etoro_live_enabled
    is True` (identity), not `bool(...)` — **ChatGPT audit fix**.
- `bot/brokers/__init__.py`
  - `BROKER=etoro_real` **still raises `ValueError`** — the registry never
    constructs the live writer (scanner-isolation invariant).
  - New behaviour: when policy disables auto-trading for the active
    broker, `get_broker()` returns `SignalOnlyBroker(concrete, reason)`.
    `main.py` is untouched; the Telegram alert path that runs after
    `broker.submit()` is unaffected because the wrapper still returns a
    normal `OrderResult`.

## 5. ChatGPT audit fixes (all applied)

1. **Strict identity** on `routing.etoro_live_enabled is True` in
   `is_auto_trading_allowed`, `determine_signal_only_reason`, and
   `EtoroLiveBroker.preflight`. A truthy-but-not-`True` value never
   enables a live write.
2. **Validate policy first** — `preflight()` calls `validate_policy()`
   before reading any policy field; invalid policy → `PolicyInvalid`,
   no POST.
3. **`submitted_at` correctness** — `lifecycle.apply_transition(...,
   "submitted")` sets `submitted_at`. `flywheel.update_intent_status`
   only set it for `accepted`/`paper_logged`, which would have left the
   eToro `submitted` rows with a NULL timestamp.
4. **No hidden schema migration** — `client_intent_id`, `nonce_digest`,
   and `x_request_id` are stored inside the existing `lifecycle_json`
   column. No `ALTER TABLE`, no new columns.

## 6. Safety properties (enforced + tested)

- **Single POST, no retry.** `submit_live()` issues exactly one POST and
  never retries on 429/5xx/network error. The poller has no POST
  capability — it only takes a read callable.
- **Fail closed.** Unknown daily loss → `DailyLossUnknown`. Stale/absent
  quote → `StaleQuote`. Exhausted polling → `unverified`, never a second
  POST.
- **No secrets in logs.** `x-api-key`/`x-user-key`/`Authorization` are
  redacted in every audit record; account IDs truncated to last 4 chars;
  Bearer tokens scrubbed. The audit logger never raises on I/O failure.
- **Scanner isolation.** `test_m13_5_scanner_isolation.py` proves that
  importing `bot.scanner`/`bot.strategy`/`bot.risk`/`bot.brokers` does
  not transitively import `bot.etoro.live_broker`, that
  `BROKER=etoro_real` raises `ValueError`, and that
  `EtoroLiveBroker.submit()` raises `OperatorConfirmationRequired`.

## 7. Lifecycle status vocabulary

`pending_live_write → awaiting_confirm → submitted → {filled |
broker_rejected | cancelled | unverified}`; plus `policy_rejected` /
`risk_rejected` (preflight) and `closed_manual` (operator close). eToro
`statusID` mapping: 0/4 → submitted (keep polling), 1 → filled (only
once `positions[]` is populated), 2 → cancelled, 3 → broker_rejected;
`errorCode` set → broker_rejected regardless of statusID.

`filled → closed_manual` is an explicitly-permitted lifecycle transition
(the operator manual-close path) and does **not** require
`allow_terminal_override`; every other transition out of a terminal
status still does. The operator CLI loads `<repo>/.env` automatically at
startup (no manual `source` needed); an already-exported variable is not
overridden, and no secret value is ever printed.

## 8. Test summary

New M13.5.B suites (154 tests; run in two processes to honour the
reconcile import-time guard):

- `test_m13_5_nonce.py`, `test_m13_5_audit.py`, `test_m13_5_parser.py`,
  `test_m13_5_lifecycle.py`, `test_m13_5_poller.py`,
  `test_m13_5_live_broker.py`, `test_m13_5_signal_only.py`,
  `test_m13_5_scanner_isolation.py` — 144 with `live_broker` loaded.
- `test_m13_5_reconcile.py` — 10, run in its own process.

Regression (all green): M12 13/13 (offline), M14 39/39, M13.2 42/42,
M13.3 48/48, M13.4A 61/61, M15 schema 6/6, M15 gateway 33/33,
M15.2 health 28/28.

M13.2/M13.3 read-path no-write AST scans were scoped to exclude the
sanctioned operator-only `live_broker.py` (`EXCLUDED_FILES`), and the
`etoro_real` ValueError-message assertions were updated to match the new
"operator-only" wording. All other `bot/etoro/` modules remain
write-free and env-read-free.

## 9. Open known unknowns (deferred to a demo dry-run)

Carried from M13.5.A §8 — to be resolved against the eToro **demo**
endpoint before the first real write, not in this milestone:

- §8.1 No-SL/No-TP encoding (currently `IsNoStopLoss`/`IsNoTakeProfit`).
- §8.2 Failure response body shape (parser is defensive across shapes).
- §8.3 statusID semantics across order types.
- §8.6 API-side minimum `Amount` (CLI default `--amount-min=10.0`).
- §8.7 Cancel-endpoint exact path (not invoked in the happy path).

No demo call was made in M13.5.B implementation; all transport in tests
is injected.
