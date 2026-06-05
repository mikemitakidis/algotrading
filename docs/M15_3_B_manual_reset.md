# M15.3.B — `manual_reset` operator flow (runbook + design)

**Sub-milestone 3 of 4 inside M15.3.** Closes the gap that has existed since M14: an operator-initiated mechanism to clear the M13.4A allocation-policy kill switches, with all M15.3 defences in place (auth + CSRF + TOTP step-up + dual audit + rate limit).

For project-wide status, see [`../MILESTONE_STATUS.md`](../MILESTONE_STATUS.md). For carry-forward items, see [`NEXT_WORK_REGISTER.md`](NEXT_WORK_REGISTER.md). The operator-approved pre-code checklist is the canonical scope (Q-B.1..Q-B.10 + corrections C1..C4 + implementation corrections 1..10).

---

## §1 — Purpose + design intent

`manual_reset` is the operator's recovery mechanism when the M14 Risk Authority Engine is locked down by the M13.4A allocation policy's `kill_switch` flags. When `policy.global.kill_switch=true` (or a per-broker variant) is set, the M14 preflight enforces `authority=OFF` for the affected scope — no orders can be issued by the engine.

Until M15.3.B, the operator's only recovery path was to hand-edit the M13.4A allocation JSON. M15.3.B formalises the action:
- Single dedicated endpoint with a clear audit trail
- Fresh step-up TOTP required at the time of action
- 60-second preview-then-execute pattern
- Operator-supplied reason (10..500 chars) recorded in both audit angles
- Tight 3/hour rate limit
- Dual atomic audit (operator-side `auth_events` + engine-side `risk_decisions`)

**Design-intent disclosure (operator correction C4):** `manual_reset` itself does NOT trade. It does NOT call broker methods. It does NOT place, cancel, or modify orders, and it does NOT close positions. However, **the purpose of clearing kill switches is exactly to allow the M14 engine to resume normal operation under its existing gating logic**. After a successful `manual_reset`, the engine's next decision cycle will re-evaluate authority based on the new policy state. Live-trading risk is currently negligible (live IBKR account unfunded, scanner in shadow-mode) but this design-intent must be understood before the live path matures.

**This is different from `bot/kill_switch.py`** — the file-based `data/kill_switch.json` "emergency stop" that the dashboard exposes elsewhere is unchanged by `manual_reset`. The two safety mechanisms are independent.

---

## §2 — What the reset mutates (and ONLY what)

Per Q-B.1 (Option A approved): kill-switch clear + audit only. Nothing else.

| Target | Operation | Storage |
|---|---|---|
| `policy.global.kill_switch` | `True` → `False` (if was `True`) | `portfolio_risk_state.broker_allocation_policy` (M13.4A) |
| `policy.<broker>.kill_switch` for each broker | `True` → `False` (if was `True`) | Same row, same JSON |
| Operator-action audit row | INSERT | `auth_events` (kind=`manual_reset_*`) |
| Engine-side audit row | INSERT | `risk_decisions` (source=`manual_reset`) |

That is the complete list. Three writes (one policy upsert + two audit row inserts), all atomic in a single SQLite transaction (per Q-B.8).

---

## §3 — What the reset MUST NOT touch (explicit non-targets, per Q-B.2)

All of the following are byte-identical before and after a `manual_reset` call:

- `auth_events` history (append-only; existing rows never modified or deleted)
- `risk_decisions` history (append-only; existing rows never modified or deleted)
- `candidate_snapshots` table (M9 ML training data — deletion would be catastrophic)
- `daily_state_per_broker` rows (today's row or any historical row)
- `broker_positions`, `execution_intents`, `signals`, and all scanner/strategy tables
- Strategy parameters and thresholds (permanent project rule)
- `bot/kill_switch.py` and `data/kill_switch.json` (the file-based emergency stop is separate)
- TOTP secret, password hash, secret key, session keys in `.env`
- Broker connections, IB Gateway, eToro adapters
- Open orders, positions, PnL at the broker
- `systemd` services, `sync.sh`, `deploy.sh`
- M14 engine / governor / snapshot / preflight code

`manual_reset` does NOT call any broker method (`placeOrder`, `cancelOrder`, `modifyOrder`, `closePosition`). The AST scan in `test_m15_3_b_manual_reset.TestNoBrokerImports` enforces this.

---

## §4 — Endpoint surface (per Q-B.4)

### `GET /api/manual-reset/preview`
**Auth:** `@require_auth` (session cookie required).
**CSRF:** not required for GET (no side effect on policy).
**Audit:** writes `manual_reset_preview` to `auth_events` (always, success or fail).
**Response (200):**
```json
{
  "ok": true,
  "kill_switch_state": {"global": false, "ibkr": false, "etoro": false},
  "preview_token": "<32-byte url-safe random>",
  "preview_token_ttl_seconds": 60
}
```
The token is bound to the current Flask session (via a per-session nonce stored in the encrypted session cookie). 60-second TTL. Single-use — consumed by the next successful POST.

### `POST /api/manual-reset`
**Auth:** `@require_auth` + `@csrf_required` (X-CSRF-Token header required).
**Body (all fields required):**
```json
{
  "confirm": "RESET",
  "preview_token": "<token from GET preview>",
  "reason": "<10..500 chars of operator-reason text>",
  "totp_code": "<fresh 6-digit authenticator code>"
}
```

**Validation order** (each failure writes a `manual_reset_failure` audit row and returns the appropriate error):
1. JSON body parses (else `confirm_invalid` 400)
2. Rate limit (else `rate_limited` 429)
3. `confirm == "RESET"` (else `confirm_invalid` 400)
4. Preview token valid + session-bound (else `preview_token_invalid` 400)
5. Reason 10..500 chars (else `reason_invalid` 400)
6. Step-up TOTP (else `totp_invalid` 401)
7. Atomic write transaction (else `db_error` 500)

**Response (200 on success):**
```json
{
  "ok": true,
  "before_state": {"global": true, "ibkr": false, "etoro": false},
  "after_state":  {"global": false, "ibkr": false, "etoro": false},
  "switches_cleared": ["global"],
  "noop": false,
  "audit": {"auth_event_id": 42, "decision_id": "mr-abc123..."}
}
```

**Idempotent (per operator C2):** if no kill switches were set, the response has `switches_cleared=[]`, `noop=true`, and both audit rows are still written. The operator's stated intent ("reset confirmed") is recorded regardless of state change.

---

## §5 — TOTP error UX (per operator C1, strict)

The API exposes **exactly one** `hint`:

| Server response | UI message |
|---|---|
| `{"ok": false, "error": "totp_invalid", "hint": "recently_used"}` | "This code was recently used. Wait ~30 seconds for your authenticator to generate a new one." |
| `{"ok": false, "error": "totp_invalid"}` | "Invalid authenticator code." |

The `recently_used` hint is the ONLY classification the API will ever return. Wrong/malformed/missing/no-secret-configured all collapse to the generic response (no hint). Rationale: differentiated error messages aid attackers; `recently_used` is the one case where operators naturally trip over the replay cache (using their login code immediately for step-up) and deserve a friendly redirect.

**No TOTP code, TOTP secret, otpauth URI, password, raw session ID, or broker credentials appear in any audit row, log message, or API response.** AST + substring tests in G7 (`test_extras_json_never_contains_secret_material`) enforce this.

---

## §6 — Audit angles (per Q-B.7)

### `auth_events` — operator/security audit
Four new closed kinds in `dashboard.auth.audit.ALLOWED_KINDS`:
- `manual_reset_preview` — written on every GET preview (cheap audit of "who looked")
- `manual_reset_attempt` — written FIRST on every POST attempt, before any validation; this is the "someone tried this" audit-of-record
- `manual_reset_success` — written inside the atomic transaction on success (rolls back with the policy write on failure)
- `manual_reset_failure` — written OUTSIDE the transaction on every validation failure (so we keep evidence of failed attempts even when the transaction rolls back)

`extras_json` shape per kind (closed schemas; details in `dashboard/auth/manual_reset.py`):
- `_attempt`: `{has_csrf, has_preview_token, has_totp, has_reason, confirm_ok}` — captures which inputs were present, NOT their values
- `_success`: `{switches_cleared, noop, before_state, after_state, reason}`
- `_failure`: `{reason: <closed-set code>, ...extra non-secret diagnostics}`
- `_preview`: `{kill_switch_state, token_issued}`

### `risk_decisions` — M14/Risk Authority audit
Single row per successful reset with `source='manual_reset'`. Written via `bot.risk_authority.audit_decisions.write_manual_reset_decision()` — the additive sibling to `write_decision()` added in this milestone (operator-approved per pre-code checklist; the M14 source enum has accepted `'manual_reset'` since M14, only the writer was missing).

Schema mapping:
- `broker_scope='GLOBAL'` (manual_reset is always a global operator action)
- `requested_action='query_authority'` (best fit; not a trade)
- `result='allow'` (the operation succeeded)
- `authority_before='OFF'`, `authority_after='OFF'` — kill_switch=true forces OFF per M14.F preflight; the engine re-evaluates authority on its next cycle based on the new policy state
- `reason_codes=['manual_reset']`
- `snapshot_id=NULL` (operator action, not an engine evaluation)
- `actor='operator'` (short identifier; never the raw session id or any secret material)
- `explainer`: human-readable narrative including the operator's reason text

---

## §7 — Atomicity (per Q-B.8)

The success path opens a single `BEGIN IMMEDIATE` SQLite transaction containing:
1. `INSERT OR REPLACE` into `portfolio_risk_state` (the policy row with kill switches cleared)
2. `INSERT` into `risk_decisions` (the M14 audit row)
3. `INSERT` into `auth_events` (the `manual_reset_success` audit row)

`COMMIT` runs after all three. Any exception triggers `ROLLBACK` and the endpoint returns 500. The `manual_reset_attempt` row (written BEFORE the transaction starts) and the `manual_reset_failure` row (written on the rollback path) are committed via their own connection and therefore survive even when the main transaction rolls back. This means there's always an audit trail of failed attempts.

---

## §8 — Rate limit (per Q-B.9)

Dedicated per-IP limiter:
- **Threshold:** 3 attempts
- **Window:** 60 minutes
- **Lockout:** 60 minutes
- **Counts:** every POST attempt that passes auth + CSRF and reaches the endpoint body. GET preview is NOT counted.
- **Storage:** in-memory (same trade-off as M15.3.A's login limiter; resets on dashboard restart). Operator could revisit DB-backed persistence under `M15.3.A.persist` if a real abuse incident occurs.

The 429 response includes `retry_after_sec`. The endpoint writes a `manual_reset_failure` audit row with `reason='rate_limited'` and the policy snapshot in `extras`.

---

## §9 — Implementation files

| File | Type | Role |
|---|---|---|
| `dashboard/auth/manual_reset.py` | NEW | Pure-logic primitives: PreviewTokenStore, rate-limiter factory, step-up TOTP check, policy I/O, validators, atomic-reset transaction |
| `dashboard/auth/audit.py` | extended | 4 new kinds added to `ALLOWED_KINDS` |
| `dashboard/app.py` | extended | Two new endpoints + minimal Recovery tab + JS handlers |
| `bot/risk_authority/audit_decisions.py` | extended | Additive new function `write_manual_reset_decision`; all pre-existing functions byte-identical (asserted by `test_audit_decisions_only_additive_change`) |
| `test_m15_3_b_manual_reset.py` | NEW | 51 tests across 12 groups (G1..G12); see §10 |
| `docs/M15_3_B_manual_reset.md` | NEW | This file |
| `docs/NEXT_WORK_REGISTER.md` | updated | M15.3.B moved Active → Closed |

Estimate from the pre-code checklist was ~1100 LOC. Final delta is broadly in that range.

**Files explicitly NOT touched** (per protected-files invariant, asserted in G11):
`main.py`, `bot/scanner.py`, `bot/strategy.py`, `bot/risk.py`, `bot/risk_authority/engine.py`, `bot/risk_authority/governor.py`, `bot/risk_authority/authority.py`, `bot/risk_authority/snapshot.py`, `bot/risk_authority/preflight.py`, `bot/risk_authority/ingest_ibkr_exposure.py`, `bot/risk_authority/ibkr_paper_reader.py`, `bot/risk_authority/exposure_reading.py`, `bot/risk_authority/ingest_exposure.py`, `bot/gateway_health.py`, `bot/gateway_watchdog.py`, `bot/etoro/live_broker.py`, `tools/etoro_live_write.py`, `tools/ingest_exposure_state.py`, `infra/systemd/algo-trader.service`, `infra/systemd/algo-trader-dashboard.service`, `infra/systemd/ibgateway.service.documented`, `sync.sh`, `deploy.sh`.

---

## §10 — Test suite (51 tests across 12 groups)

| Group | Class | Coverage |
|---|---|---|
| G1 | `TestEndpointAuth` | unauthenticated 401, POST without CSRF 403, GET via POST 405 |
| G2 | `TestPreviewEndpoint` | state read, 60s TTL token, audit row, not rate-limited |
| G3 | `TestConfirmString` | exact "RESET", lowercase rejected, missing rejected, wrong type rejected, pure-function unit test |
| G4 | `TestStepUpTOTP` | missing/wrong/malformed/empty → 401 no hint; replay → 401 + `hint='recently_used'`; valid fresh code succeeds; no-secret refuses |
| G5 | `TestReasonField` | missing/too-short/too-long/whitespace-only rejected; pure-function unit test |
| G6 | `TestKillSwitchClearing` | clears global, clears multiple, idempotent no-op (C2), before/after state, audit IDs, preview token single-use |
| G7 | `TestAuditWrites` | attempt always written, success extras well-formed, risk_decisions row well-formed, actor short+no session id, failure audit on validation failure, **secret-material invariant sweep** |
| G8 | `TestAtomicity` | policy unchanged on rollback, failure audit still written, no success audit on rollback |
| G9 | `TestRateLimit` | 3 attempts → 429, preview doesn't count, rate-limit failure audit |
| G10 | `TestNoBrokerImports` | AST scan of manual_reset.py, of audit_decisions writer function, of endpoint function bodies; no broker imports, no broker method names |
| G11 | `TestProtectedFilesUntouched` | 0/24 protected files modified vs ae8fb0d; audit_decisions.py is additive-only (pre-existing functions byte-identical) |
| G12 | `TestAuthEventsKindsRegistered` | 4 new kinds present in ALLOWED_KINDS |

---

## §11 — VPS deployment + operator verification

**Deploy** (operator-side, NOT via `sudo ./sync.sh` per implementation correction 9):

```bash
cd /opt/algo-trader && \
sudo git fetch origin main && \
sudo git reset --hard origin/main && \
git rev-parse --short HEAD
```

**Verify tests** (no service restart needed for code-only changes — test files don't affect runtime; but `dashboard/app.py` DOES need a dashboard restart for the new routes/UI to be active):

```bash
sudo systemctl restart algo-trader-dashboard.service && \
sleep 3 && \
sudo systemctl is-active algo-trader-dashboard.service && \
sudo -u root /opt/algo-trader/venv/bin/python -m unittest test_m15_3_b_manual_reset 2>&1 | tail -3 && \
echo "regression sweep:" && \
for t in test_m15_3_a_dashboard_auth test_m15_3_a_2_totp test_m13_4a_allocation test_m14_e_engine test_m14_g_dashboard test_m15_4_gateway_health test_m15_5_ibkr_exposure; do
  r=$(sudo -u root /opt/algo-trader/venv/bin/python -m unittest $t 2>&1 | grep -E "^Ran|^OK|^FAILED" | tr '\n' ' ')
  printf "  %-34s %s\n" "$t" "$r"
done
```

**Verify production state still healthy** (no regression of M15.3.A.cutover):

```bash
sudo ss -ltnp 'sport = :8080' && \
curl -s -o /dev/null -w "  HTTPS /api/health -> %{http_code}\n" \
   --max-time 6 https://algotrading.marketwarrior.club/api/health
```

**Operator browser verification** (the real end-to-end test):

1. Open `https://algotrading.marketwarrior.club`.
2. Log in with password + 6-digit Google Authenticator code.
3. Navigate to the M13.4A Broker Allocation tab. Set `global.kill_switch=true` and save. Verify the kill switch is now active.
4. Click the new **Recovery** tab in the top nav.
5. Click "Load current state" → the preview shows `global.kill_switch=true (locked)` and other kill switches.
6. Wait at least ~30 seconds for your authenticator to generate a new TOTP code (the one used for login is in the replay cache).
7. Type a real reason (10..500 chars), type `RESET` in the confirm box, enter your current 6-digit code, click "Clear kill switches".
8. Verify the success message shows `switches_cleared: ['global']`, `noop: false`, and the audit IDs.
9. Reload the M13.4A tab and verify the kill switch is now cleared.
10. Query the audit log:

```bash
sudo sqlite3 /opt/algo-trader/data/signals.db \
  "SELECT id, ts_utc, kind, success FROM auth_events WHERE kind LIKE 'manual_reset_%' ORDER BY id DESC LIMIT 10;"
sudo sqlite3 /opt/algo-trader/data/signals.db \
  "SELECT decision_id, taken_at, source, actor FROM risk_decisions WHERE source='manual_reset' ORDER BY taken_at DESC LIMIT 5;"
```

You should see one `manual_reset_preview` + one `manual_reset_attempt` + one `manual_reset_success` row per executed reset, and one `risk_decisions` row with `source='manual_reset'`, `actor='operator'`.

11. **Idempotent re-run check**: trigger the reset flow again (with the kill switch now cleared). Verify the response shows `switches_cleared: []`, `noop: true`, and the audit rows are STILL written.

---

## §12 — Honest residual / known limitations

- **Rate-limit storage is in-memory.** Same trade-off as M15.3.A's login limiter. A dashboard restart resets the limiter. Acceptable trade-off; revisit only on a real incident.
- **TOTP replay cache is in-memory.** A dashboard restart clears it. Same trade-off as M15.3.A.2.
- **No multi-user roles.** Single-operator model preserved per the operator's "no multi-user work" constraint. If multiple operators ever co-administer, each successful reset's audit row currently records `actor='operator'` rather than a specific user.
- **The endpoint does NOT cancel orders, close positions, or restart services.** It clears policy flags only. If the operator wants to do those things, they must be done explicitly via the existing surfaces (or via the broker directly).

---

## §13 — Closeout evidence (2026-06-04)

M15.3.B was VPS-verified by the operator on 2026-06-04 and is **CLOSED**. Recorded here for historical traceability.

**Commit chain:** `2f55f1d` — single implementation commit. No follow-up fix commits were required.

**Terminal verification (operator, on VPS):**
- `git rev-parse --short HEAD` = `2f55f1d`
- `test_m15_3_b_manual_reset.py` → 51/51 OK
- Regression sweep all green: `test_m15_3_a_dashboard_auth` 101/101, `test_m15_3_a_2_totp` 52/52, `test_m13_4a_allocation` 61/61, `test_m14_e_engine` 105/105, `test_m14_g_dashboard` 51/51, `test_m15_4_gateway_health` 50/50, `test_m15_5_ibkr_exposure` 78/78
- `algo-trader-dashboard.service` → `active`
- `caddy.service` → `active`
- `ss -ltnp 'sport = :8080'` → `127.0.0.1:8080` only (M15.3.A.cutover bind preserved)
- `https://algotrading.marketwarrior.club/api/health` → 200
- `git status` clean

**Browser end-to-end verification (operator, real session over HTTPS):**

| Step | Result |
|---|---|
| Login at `https://algotrading.marketwarrior.club` with password + Google Authenticator code | ✓ Success |
| Set `ibkr.kill_switch=true` via the M13.4A Broker Allocation tab (controlled test) | ✓ Lock applied |
| Open Recovery → click "Load current state" | Preview shows `etoro=false`, `global=false`, `ibkr=true (locked)` ✓ |
| Enter operator reason (>10 chars): "M15.3.B browser verification: clearing test ibkr kill switch after confirming no broker action is performed." | ✓ Accepted |
| Type `RESET` in the confirm box | ✓ Accepted |
| Enter fresh Google Authenticator code (replay cache aged out naturally from login) | ✓ Accepted |
| Click "Clear kill switches" | ✓ Submit |
| Browser response | `Success. Cleared 1 kill switch(es): ibkr.` |
| `auth_event_id` in response | `38` |
| `decision_id` in response | `mr-3086a40a9b2f46e5` |

**End-to-end chain verified:** preview state read → preview token issuance → CSRF check → confirm-string validation → preview token consume → reason validation → step-up TOTP verification → atomic policy update + dual audit writes (`auth_events` + `risk_decisions`) → response with before/after state + audit IDs → UI confirmation. Browser-side HTTPS via Caddy → loopback → dashboard preserved across the full request chain (M15.3.A.cutover transport intact).

**What this closeout proves end-to-end:**
- The operator can recover from a kill-switch lockout via the dashboard, with full audit trail, without shell access to the VPS.
- The M15.3 defensive stack (HTTPS + auth + CSRF + step-up TOTP + rate limit + dual audit) holds together for a state-changing operator action.
- The dual audit angle works: `auth_events` row `id=38` (operator-side) and `risk_decisions` row `decision_id=mr-3086a40a9b2f46e5` (M14-side) both written atomically in one transaction.
- The design-intent disclosure in §1 is honoured at runtime: `manual_reset` cleared the policy flag and did not call any broker method, place any order, or modify any position.

**Carry-forward to M15.3.C** (the next and final M15.3 sub-milestone): the compliance-grade audit log + regulatory export feature will read M15.3.B's `manual_reset_*` rows from `auth_events` + the `risk_decisions` rows with `source='manual_reset'` and surface them in a compliance-friendly format. M15.3.B's append-only audit schema makes this straightforward.
