# M13.7 — M13 Closeout & Audit

**Type:** docs-only. No code change, no live/demo call, no eToro write,
no real order.
**Final status: M13 CLOSED.**
**Scope of M13:** eToro integration / manual bridge — live-write
capability *built, gated, reviewed, deployed, and no-write verified*.
The first funded real order is explicitly **outside** M13.

---

## 1. Milestone chain (discovery → M13.5.D)

| Stage | Commit | Summary |
|---|---|---|
| M13 discovery | `61af4f5` | discovery closeout (redacted status note) |
| M13.1 design | `f41e960`, `f7bf628` | eToro integration design (docs-only) + factual fix |
| M13.2 read adapter | `d682bba` | eToro read adapter — library code, dormant in runtime |
| M13.3 paper broker | `b9957cb` | PaperEtoroBroker — dry-run, no write capability |
| M13.4A allocation | `91d2a76` | dashboard broker allocation + budget controls |
| M13.4A.1 UX | `dfca67d` | broker allocation UX polish (UI-only) |
| M13.4B design | `9d27fdb` | minimum live-write test design (docs-only) |
| M13.5.A evidence | `50e0e49` → `2b90a37` | pre-implementation evidence pack + corrections (docs-only) |
| M13.5.B writer | `cb47758` | eToro live writer (operator-only, double-flag + nonce) |
| M13.5.B fix 1 | `f880516` | load `.env` in CLI; `filled→closed_manual` w/o override; cleanup |
| M13.5.B fix 2 | `b051538` | disable `--demo` (fail closed); no real-key/real-URL leak |
| M13.5.B fix 3 | `5cb49ea` | remove `--base-url`; real mode pinned to public API |
| M13.5.C readiness | (VPS run; no commit) | server sync + no-write readiness verification |
| M13.5.D unknowns | `f7a3bc2` | open-unknowns provenance & confidence register (docs-only) |

**Final accepted HEAD at M13 close:** `f7a3bc2` (server synced to
`5cb49ea` code at M13.5.C; M13.5.D added docs only on top).

---

## 2. Safety invariants (accepted)

1. **No scanner-to-live path.** `EtoroLiveBroker` is never imported by
   the scanner/strategy/risk/main/broker-factory runtime.
2. **Operator-only construction.** `EtoroLiveBroker` is constructed
   solely by `tools/etoro_live_write.py`.
3. **Registry fails loud.** `BROKER=etoro_real` raises `ValueError` in
   normal runtime; `get_broker()` never returns the live writer.
4. **`submit()` is not a live path.** The BrokerAdapter `submit()`
   raises `OperatorConfirmationRequired`; live writes go through
   `submit_live()` only.
5. **Double live flag + nonce.** A real POST requires BOTH
   `routing.etoro_live_enabled is True` (strict identity) AND
   `ETORO_LIVE_ENABLED=true`, PLUS an operator-echoed single-use
   per-payload nonce.
6. **Policy validated before use.** `preflight()` calls
   `validate_policy()` before reading any policy field; 16 ordered
   gates; fail-closed on unknown daily loss / stale quote.
7. **Single POST, no retry, no second POST on uncertainty.** Poller has
   no POST capability; exhaustion → `unverified`, never re-submits.
8. **Signal-only / manual mode preserved.** When policy disables
   auto-trading, `get_broker()` wraps in `SignalOnlyBroker`, which never
   calls the wrapped broker; Telegram alert path is unaffected.
9. **Demo disabled, fail-closed.** `--demo` aborts before any credential
   read / import / broker construction; never falls back to real keys;
   never uses the real API base URL.
10. **No endpoint override.** `--base-url` removed; real mode pinned to
    `https://public-api.etoro.com`.
11. **Controlled reconciliation only.** `tools/etoro_reconcile.py` writes
    lifecycle via `bot.etoro.lifecycle` only (no raw write SQL), never
    calls an eToro write endpoint, has an import guard vs `live_broker`.
12. **No secrets in logs/docs.** Audit logger redacts api/user keys,
    Bearer tokens, account IDs; never raises on I/O failure.
13. **No hidden schema migration.** `client_intent_id`/`nonce_digest`/
    `x_request_id` stored in existing `lifecycle_json`.

---

## 3. Tests / evidence proving the invariants

All green at close. M13.5.B suites total **173** (run in two processes
to honour the reconcile import guard: 162 with `live_broker` loaded + 11
reconcile isolated).

| Invariant(s) | Proving suite | Tests |
|---|---|---|
| 1, 2, 3, 4, 11 | `test_m13_5_scanner_isolation.py` | 7 |
| 5, 6, 7 (preflight/POST) | `test_m13_5_live_broker.py` | 40 |
| 5 (nonce) | `test_m13_5_nonce.py` | 18 |
| 7 (poller) | `test_m13_5_poller.py` | 11 |
| 8 (signal-only) | `test_m13_5_signal_only.py` | 17 |
| 9, 10 (demo/base-url/.env) | `test_m13_5_cli_env.py` | 15 |
| 11 (reconcile) | `test_m13_5_reconcile.py` | 11 |
| 12 (redaction/audit) | `test_m13_5_audit.py` | 17 |
| 13 + lifecycle/submitted_at | `test_m13_5_lifecycle.py` | 20 |
| status/error parsing | `test_m13_5_parser.py` | 17 |
| read-path no-write contract | `test_m13_2_etoro_read.py` | 42 |
| paper broker + registry | `test_m13_3_etoro_paper.py` | 48 |
| broker allocation policy | `test_m13_4a_allocation.py` | 61 |

Regression beyond M13 (green at close): M12 13/13 (offline), M14 39/39,
M15 schema 6/6, M15 gateway 33/33, M15.2 health 28/28. Protected files
(`main.py`, `bot/risk.py`, `bot/scanner.py`, `bot/strategy.py`) and
`dashboard/` unchanged across the entire M13.5 line.

---

## 4. M13.5.C — VPS no-write readiness: **PASS with one non-blocking warning**

Run on the VPS under the project venv (`/opt/algo-trader/venv/bin/python`).

**Passed:** server synced to `5cb49ea`; git tree clean; pip install
(venv) OK; eToro + `live_write` imports OK; scanner isolation PASS
(`bot.etoro.live_broker` not imported); `BROKER=etoro_real` fails loudly
(ValueError); `--help` / `oneshot --help` OK; `ETORO_LIVE_ENABLED`
absent/false (redacted check); audit log absent / no `live_post` events;
`/api/health` HTTP 200; dashboard port 8080 listening; Telegram status
message sent; **no eToro write/POST, no flags changed, no order.**

**Non-blocking warning — carry forward:** scanner systemd unit
exact-match check found all candidate names inactive:
`algo-trader=inactive`, `scanner=inactive`, `algo-scanner=inactive`.

**Why non-blocking:** `/api/health` returned 200, heartbeat fresh,
dashboard port listening, scanner isolation passed, Telegram sent, no
eToro write, no flags changed. The likely cause is a different actual
unit name or another process manager.

**Tracked infra cleanup item:** *"Identify actual scanner/dashboard
systemd unit names, or document the current process manager."*

---

## 5. Explicitly NOT done (handoff)

- **No real eToro order** was ever placed in M13. Zero real-money POST.
- **Demo mode disabled** (fail-closed) until a verified eToro sandbox
  base URL exists and is vendored.
- **First funded order is outside M13** — a separate go-live event with
  its own risk sign-off, ideally after M14.
- **Daily-loss is still a manual/operator seam** — `realised_daily_loss`
  is supplied to the live preflight by the operator (CLI arg). Automated
  broker-scoped daily-loss tracking is M14 work.
- **Broker-scoped risk state → M14** (roadmap "Portfolio/risk layer").
  Not pulled into M13.
- **Open unknowns (M13.5.D):** §8.1/§8.2/§8.6 ASSUMED, §8.7 DEFERRED —
  none block M13 close; all block the first funded order. Each has a
  documented upgrade path (save/vendor the source, then re-label).
- **Signal/universe diagnostic** (the low-signal / no-signal issue)
  remains separate, already-tracked next work — not part of M13.
- **Scanner systemd unit-name mismatch** — infra cleanup item from §4.

---

## 6. Final status

**M13 is CLOSED.** eToro live-write capability is built, gated by a
double live flag + per-payload nonce, reviewed across four
ChatGPT-driven corrections, deployed to the VPS, and verified to perform
no writes. The remaining items above are handed off to M14 and to the
separate go-live event; none of them are M13 deliverables.
