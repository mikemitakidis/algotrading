# M15.3.C — Compliance audit + export

**Status:** IMPLEMENTATION LANDED — AWAITING VPS VERIFICATION

This is the final M15.3 sub-milestone. With M15.3.C closed, M15.3 itself
closes, and the project moves to **M16 (historical data + first signal
engine)** per the post-M15 strategic direction recorded at the M15.3.A.cutover
closeout.

---

## §1 — Purpose, scope, design intent

M15.3.C provides a compliance-friendly export of the M15.3 audit trail.
The operator can download a single file containing:

- All `auth_events` rows (M15.3.A login/session/CSRF + M15.3.A.2 TOTP +
  M15.3.B `manual_reset_*` audit history + M15.3.C's own
  `audit_export_request` meta-audit rows)
- All `risk_decisions` rows with **`source='manual_reset'` only**

The two streams together represent the operator/security audit picture for
the entire M15.3 surface: who logged in, who solved or failed a 2FA challenge,
who initiated and completed a `manual_reset`, who exported the audit log.

### What is NOT exported

Per Q-C.1 of the pre-code checklist (operator-approved 2026-06-04):

- `risk_decisions` rows with `source IN ('auto','manual','reconciled')` —
  these are operational/risk-engine audit rows, not security/operator audit.
  Out of scope for M15.3.C. A future milestone may add them.
- `signals`, `execution_intents`, `broker_positions`,
  `candidate_snapshots`, strategy params, `portfolio_risk_state` snapshots,
  `daily_state` per-broker rows, M14 `risk_snapshots` — none of these are
  audit data.

### Design-intent disclosure (operator approval Q-C.7)

The export endpoint is **read-only with respect to all trading and account
state**. The *only* write it performs is a single `audit_export_request`
row in `auth_events` — the meta-audit-of-the-audit. This row records who
requested the export, when, for what date range, with what format, what
the row counts were, and (in failure cases) why the export was refused.

No broker, scanner, strategy, M14 engine/governor/snapshot/preflight, eToro,
or IBKR-adapter code is touched. Hard-asserted by `TestNoBrokerImports`
and `TestProtectedFilesUntouched` in the test suite.

---

## §2 — Mutations

| Resource | Mutation | When |
|---|---|---|
| `auth_events` table | INSERT one row with `kind='audit_export_request'` | Every export attempt that reaches the endpoint body (after `@require_auth`). Includes both successful exports (`success=1`) and refused-after-auth exports (`success=0`). |

Everything else is read-only. No policy mutations. No kill-switch mutations.
No M14 state mutations. No trading mutations.

---

## §3 — Endpoint surface

### `GET /api/audit-export`

| Query param | Required | Default | Description |
|---|---|---|---|
| `format` | no | `jsonl` | One of `jsonl` (default) or `csv` |
| `from`   | no | `1970-01-01` | UTC date in `YYYY-MM-DD`; inclusive lower bound on `ts_utc` (auth_events) and `taken_at` (risk_decisions). The full 00:00:00–23:59:59 UTC day is included. |
| `to`     | no | today (UTC) | UTC date in `YYYY-MM-DD`; inclusive upper bound. |

### Responses

#### `200 OK` (export delivered)

JSONL:
- `Content-Type: application/x-ndjson`
- `Content-Disposition: attachment; filename="audit_export_<YYYYMMDDTHHMMSSZ>.jsonl"`
- `X-Export-Id: exp-<16hex>`
- `X-Export-Sha256: <64hex>`
- Body: line 1 is the manifest, lines 2+ are audit rows (one JSON object per line)

CSV:
- `Content-Type: application/zip`
- `Content-Disposition: attachment; filename="audit_export_<YYYYMMDDTHHMMSSZ>.zip"`
- `X-Export-Id: exp-<16hex>`
- `X-Export-Sha256: <64hex>`
- Body: a ZIP containing `manifest.txt`, `auth_events.csv`,
  `risk_decisions_manual_reset.csv`

#### `400 Bad Request`

| `error` field | Cause |
|---|---|
| `format_invalid` | `format` is not `jsonl` or `csv` |
| `date_format_invalid` | `from` or `to` does not match `YYYY-MM-DD` or is not a real date (e.g. `2026-02-30`) |
| `date_range_invalid` | `to < from` after parsing |
| `row_cap_exceeded` | Combined `auth_events` + `risk_decisions` row count exceeds `MAX_EXPORT_ROWS` (default 100,000); response includes `max_rows`, `row_counts`, and a `hint` |

#### `401 Unauthorized`

No valid session cookie. Same `@require_auth` semantics as the rest of
the dashboard.

#### `429 Too Many Requests`

Rate-limit lockout active for this IP. Response includes `retry_after_sec`.

#### `500 Internal Server Error`

| `error` field | Cause |
|---|---|
| `redaction_violation` | The body contained a known secret substring (env-keyed or literal). Refused per Q-C.5 fail-fast policy. Response includes `export_id` and **labels-only** `violation_labels` (e.g. `["DASHBOARD_TOTP_SECRET"]`) — never the secret value. The meta-audit row carries the same labels but no value. |
| `build_failed` | Unexpected error during body construction. Should be rare; the test suite covers the validation paths. |

### Auth / CSRF / TOTP (operator decisions, Q-C.8)

- `@require_auth` is mandatory.
- **No CSRF token required.** The endpoint is GET, so no CSRF surface
  exists — the existing M15.3.A CSRF requirement covers POST/PUT/DELETE
  to dashboard endpoints, and GET is exempt by convention.
- **No step-up TOTP.** Conscious decision per Q-C.8: the exported data is
  already visible to the authenticated operator via the existing
  dashboard views; the export is a convenience aggregation, not new
  access. If broader trading/risk exports are exposed later, step-up
  TOTP should be reconsidered.
- Rate limit: **10 attempts per IP per hour**, sliding window. Per Q-C.8
  M15.3.C re-spec (operator + ChatGPT review, 2026-06-05): the cap
  counts **every authenticated attempt that reaches this endpoint**,
  regardless of outcome — successful JSONL/CSV exports, format-invalid
  responses, date-invalid responses, `row_cap_exceeded` rejections,
  `redaction_violation` rejections. The 11th attempt within any 1-hour
  window returns 429 and is itself meta-audited as
  `audit_export_request` with `success=0`, `reason='rate_limited'`. No
  secret values appear in the 429 response or its audit row.

  This is **stricter than the shared M15.3.A/B `RateLimiter`** (which
  counts only failed attempts — correct for login endpoints where
  automated bots probe wrong creds). For a compliance export endpoint
  every download is sensitive enough to bound by total volume. The
  shared `RateLimiter` is **unchanged**; M15.3.C uses a new
  M15.3.C-local `ExportAttemptLimiter` class in
  `dashboard/auth/audit_export.py`. Both limiters coexist; M15.3.A
  and M15.3.B behaviour is preserved.

  Rejected (429) attempts are intentionally **not** themselves
  recorded into the limiter's per-IP bucket — this prevents a burst
  of post-cap requests from indefinitely extending the lockout. The
  cap is purely on attempts that were ALLOWED through the rate
  check. Rejected attempts are still meta-audited (one
  `audit_export_request` row per 429) for compliance visibility.

---

## §4 — Export format

### JSONL (default)

```
<manifest as a single-line JSON object, ending with \n>
{"_source":"auth_events", id, ts_utc, kind, client_ip, user_agent, session_id_hash, success, extras}
{"_source":"auth_events", ...}
...
{"_source":"risk_decisions_manual_reset", decision_id, taken_at, broker_scope, requested_action, request, result, authority_before, authority_after, reason_codes, recovery_paths, snapshot_id, source, actor, explainer, created_at}
{"_source":"risk_decisions_manual_reset", ...}
```

All JSON objects are written with `sort_keys=True`, `separators=(",",":")`,
`ensure_ascii=False`. The `_source` field disambiguates which table a row
came from.

`extras_json` from the database is **parsed back to a Python object** before
serialisation — the JSONL output preserves the nested structure of the
operator's reason text, kill-switch lists, before/after policy states, etc.

### CSV (ZIP)

The ZIP contains exactly three files:

- `manifest.txt` — human-readable manifest, one field per line, in a
  documented order. Includes the `_sha256_payload` covering the two
  CSV bodies (NOT manifest.txt itself).
- `auth_events.csv` — RFC-4180-quoted (via `csv.QUOTE_MINIMAL` +
  `lineterminator='\n'`). Columns:
  `id, ts_utc, kind, client_ip, user_agent, session_id_hash, success, extras_json`.
  The `extras_json` cell is the stringified JSON object — opens in
  Excel/LibreOffice as one cell containing the raw JSON text.
- `risk_decisions_manual_reset.csv` — RFC-4180-quoted. Columns:
  `decision_id, taken_at, broker_scope, requested_action, request_json,
  result, authority_before, authority_after, reason_codes_json,
  recovery_paths_json, snapshot_id, source, actor, explainer, created_at`.

### Manifest fields

| Field | Type | Notes |
|---|---|---|
| `_schema_version` | int | `1` for M15.3.C. Bumps if the export schema ever changes. |
| `_export_id` | string | `exp-<16hex>`. Also written into the `audit_export_request.extras_json` row for bidirectional linkage. |
| `_generated_at_utc` | string | ISO-8601 UTC timestamp |
| `_generated_by_actor` | string | `operator` (single-user model preserved from M15.3.B) |
| `_date_range` | object | `{from_iso, to_iso}` — the inclusive UTC window actually applied |
| `_row_counts` | object | `{auth_events: N, risk_decisions_manual_reset: M}` |
| `_sha256_payload` | string | 64-hex SHA-256. **JSONL**: covers the body (everything after the manifest line). **CSV**: covers `auth_events.csv` bytes concatenated with `risk_decisions_manual_reset.csv` bytes (NOT manifest.txt). |
| `_format` | string | `jsonl` or `csv` |

---

## §5 — Redaction and secret handling

Per Q-C.5: **fail-fast, do NOT silent-strip**.

Before the export is returned, the full body bytes are scanned for any
known-secret substring. Two classes:

### Class A — env-keyed secrets

For each of these env vars, the value (if non-empty and ≥12 characters)
is treated as a secret substring to scan for:

- `DASHBOARD_TOTP_SECRET`
- `DASHBOARD_PASSWORD`
- `DASHBOARD_PASSWORD_HASH`
- `DASHBOARD_SECRET_KEY`
- `IBKR_API_KEY`
- `IBKR_PASSWORD`
- `ETORO_API_KEY`
- `ETORO_USER_KEY`
- `ETORO_PASSWORD`
- `TELEGRAM_BOT_TOKEN`

The 12-character minimum exists to avoid false positives — if an operator
has set `DASHBOARD_PASSWORD=abc`, scanning for `abc` would match every
legitimate row containing the substring `abc`. Real high-entropy secrets
are always much longer.

### Class B — literal substrings

These are scanned unconditionally:

- `otpauth://` — TOTP setup URI prefix
- `-----BEGIN ` — PEM private-key header

### Failure behaviour

If any match is found:

1. The export body is **discarded** — not returned to the client.
2. A meta-audit row is written with `kind='audit_export_request'`,
   `success=0`, `extras_json` containing `reason='redaction_violation'`
   and `redaction_violations: [<labels>]`. **Labels only** — never the
   secret value, never any byte from the matched string.
3. The HTTP 500 response carries the same labels-only.
4. A log line at `ERROR` level records the export_id and labels but
   never the secret value.

The audit invariants of M15.3.A.2 and M15.3.B already guarantee these
substrings are never written to audit rows. A `redaction_violation` in
production therefore indicates a **bug in audit-row writing** somewhere
upstream — which is exactly what the defence-in-depth scan is supposed
to catch.

### What is NOT redacted (pass-through)

- Hashed session IDs (already SHA-256'd at write time in M15.3.A)
- Client IPs (compliance routinely needs these)
- User agents
- Operator reason text from M15.3.B manual_reset (10–500 chars,
  pre-validated, UI warned operator not to paste secrets)
- `extras_json` payloads for known closed-schema kinds
- `risk_decisions.explainer` text

---

## §6 — Self-audit (meta-audit)

Every export attempt that reaches the endpoint body writes exactly one
`audit_export_request` row to `auth_events`:

- **Success** (`success=1`): `extras_json` contains `export_id`,
  `format`, `from_iso`, `to_iso`, `row_counts`. The `export_id` matches
  the one in the delivered file's manifest — bidirectional linkage from
  audit row to file and back.
- **Failure** (`success=0`): `extras_json` contains `reason` (one of
  `rate_limited`, `format_invalid`, `date_format_invalid`,
  `date_range_invalid`, `row_cap_exceeded`, `redaction_violation`,
  `build_failed`) plus whatever subset of `export_id`, `format`,
  `from_iso`, `to_iso`, `row_counts`, `redaction_violations` is known
  at the failure point. Never any secret value.

Rate-limit lockout failures (HTTP 429) also write a meta-audit row —
the operator's burst-attempts are themselves audit-relevant.

The `audit_export_request` kind is registered in the closed
`ALLOWED_KINDS` set in `dashboard/auth/audit.py` (the 18th kind, after
M15.3.B's 4 manual_reset kinds).

---

## §7 — Implementation files

| File | LOC | Role |
|---|---|---|
| `dashboard/auth/audit_export.py` (NEW) | ~530 | Pure-logic primitives: date validation, row reading, JSONL/CSV builders, redaction scanner, rate-limiter factory, filename construction. Zero broker/scanner/strategy/engine imports (AST-asserted by G10). |
| `dashboard/auth/audit.py` (MODIFIED, +7 lines) | — | Adds `audit_export_request` as the 18th `ALLOWED_KINDS` value. |
| `dashboard/app.py` (MODIFIED, +~270 LOC) | — | `m153c_audit_export()` endpoint, lazy rate-limiter init, small Audit Export card on the Recovery page with date pickers + format selector + download button. |
| `test_m15_3_c_audit_export.py` (NEW) | ~870 | 32 tests across 12 groups (see §8). |
| `docs/M15_3_C_audit_export.md` (NEW, this file) | ~280 | Operator runbook. |
| `docs/NEXT_WORK_REGISTER.md` (MODIFIED) | — | M15.3.C entry added. |
| `MILESTONE_STATUS.md` (MODIFIED, closeout) | — | M15.3.C → CLOSED + M15 fully CLOSED (after VPS verification). |

---

## §8 — Test suite (37 tests, 12 groups)

| G | Class | Tests | Asserts |
|---|---|---|---|
| G1 | `TestExportAuth` | 2 | Unauth → 401, POST → 405 |
| G2 | `TestExportFormatJSONL` | 4 | 200 + correct content-type/disposition, manifest is first line and well-formed, sha256 verifies, row counts match body |
| G3 | `TestExportFormatCSV` | 4 | ZIP with 3 files, manifest.txt fields present, RFC-4180 round-trip survives embedded commas/quotes/newlines, sha256 verifies |
| G4 | `TestExportScope` | 3 | Only `ALLOWED_KINDS` appear in auth_events stream; only `source='manual_reset'` rows in risk_decisions stream; no other `_source` values leak in (no signals/execution_intents/etc.) |
| G5 | `TestExportDateFilters` | 5 | Inclusive `from`/`to`, malformed → 400, reversed → 400, empty range → valid empty manifest, format_invalid → 400 |
| G6 | `TestExportRowCap` | 1 | Monkey-patches `MAX_EXPORT_ROWS=5`, seeds 10 rows, asserts 400 `row_cap_exceeded` with `max_rows` + `row_counts` + `hint` |
| G7 | `TestExportRedaction` | 4 | Scanner finds env-keyed secret; ignores short values; catches `otpauth://`; endpoint 500 + meta-audit + NO secret value in response/extras/logs |
| G8 | `TestExportSelfAudit` | 2 | Success writes audit row with matching `export_id`; failure writes row with `reason` |
| G9 | `TestExportRateLimit` | 6 | **(Re-spec 2026-06-05)** First 10 successful exports allowed; 11th successful attempt → 429; mixed-outcome attempts (success + format-invalid + date-invalid) also count toward cap; rate-limited attempt writes `audit_export_request` `success=0` `reason='rate_limited'`; no secret values leak into rate-limit response or audit extras even with known long secrets in env; unit-level `ExportAttemptLimiter` semantics (3 allowed / 4th denied / sliding-window age-out / per-IP isolation) |
| G10 | `TestNoBrokerImports` | 3 | AST scan: `audit_export.py` has no broker/scanner/strategy/engine imports; string literals have no broker method names; endpoint function body has no broker imports |
| G11 | `TestProtectedFilesUntouched` | 1 | 0/24 protected files modified vs `384e484` (M15.3.B-closeout HEAD) |
| G12 | `TestAllowedKindsRegistered` | 2 | `audit_export_request` is in `ALLOWED_KINDS`; the runtime snapshot in `audit_export.py` matches the live `ALLOWED_KINDS` set |

**Per Q-C.9 correction**: the optional RSS memory-footprint test was
dropped — the 100k row cap + spool-to-bytes approach is sufficient defence
against runaway memory use, and an RSS test would be fragile across
platforms.

---

## §9 — VPS deploy + verification

Per Q-C.10 — same direct-sync pattern as M15.3.B:

```bash
cd /opt/algo-trader && \
sudo git fetch origin main && \
sudo git reset --hard origin/main && \
git rev-parse --short HEAD && \
sudo systemctl restart algo-trader-dashboard.service && sleep 3 && \
sudo systemctl is-active algo-trader-dashboard.service && \
sudo -u root /opt/algo-trader/venv/bin/python -m unittest test_m15_3_c_audit_export 2>&1 | tail -3 && \
echo "regression sweep:" && \
for t in test_m15_3_b_manual_reset test_m15_3_a_dashboard_auth test_m15_3_a_2_totp test_m13_4a_allocation test_m14_e_engine test_m14_g_dashboard test_m15_4_gateway_health test_m15_5_ibkr_exposure; do
  r=$(sudo -u root /opt/algo-trader/venv/bin/python -m unittest $t 2>&1 | grep -E "^Ran|^OK|^FAILED" | tr '\n' ' ')
  printf "  %-34s %s\n" "$t" "$r"
done && \
sudo ss -ltnp 'sport = :8080' && \
sudo systemctl is-active caddy.service && \
curl -s -o /dev/null -w "  HTTPS /api/health -> %{http_code}\n" --max-time 6 https://algotrading.marketwarrior.club/api/health && \
git status --short
```

### Operator browser walkthrough

1. Log in at `https://algotrading.marketwarrior.club` with password + Google
   Authenticator code (same as M15.3.B).
2. Navigate to the **Recovery** tab — the existing `manual_reset` card is
   at the top; the new **Audit Export (M15.3.C)** card is below it.
3. Pick a `From` date and a `To` date (UTC, inclusive). Leave blank to
   default to all-time / today.
4. Pick a format: `JSONL` (default, high-fidelity) or `CSV` (ZIP, opens
   in Excel).
5. Click **Download export**.
6. The browser downloads `audit_export_<YYYYMMDDTHHMMSSZ>.{jsonl|zip}`.
7. The status line below the button shows `Downloaded <filename>
   (export_id=exp-..., sha256=...)`.

### Operator post-download verification

For JSONL:
```bash
head -1 audit_export_*.jsonl | python3 -m json.tool
# → should pretty-print the manifest with _schema_version, _export_id,
#   _row_counts, _sha256_payload, etc.

wc -l audit_export_*.jsonl
# → should equal 1 (manifest) + sum(_row_counts.values())
```

For CSV:
```bash
unzip -l audit_export_*.zip
# → manifest.txt, auth_events.csv, risk_decisions_manual_reset.csv
unzip -p audit_export_*.zip manifest.txt
# → human-readable manifest
```

Verify the meta-audit row was written:
```bash
sudo sqlite3 /opt/algo-trader/data/signals.db \
  "SELECT id, ts_utc, success, json_extract(extras_json, '\$.export_id') \
   FROM auth_events WHERE kind='audit_export_request' \
   ORDER BY id DESC LIMIT 5;"
```

---

## §10 — Honest residual

- **No cryptographic signing.** The SHA-256 in the manifest is integrity
  (was the file modified after export), not provenance (did Anthropic
  generate this). Adding HMAC/sig would require a server-side key plus
  a published verification step; out of scope for M15.3.C.

- **No append-only manifest of exports.** Each export is independent; we
  rely on the `audit_export_request` rows in `auth_events` as the audit
  trail of who exported what. If a regulator wants a single rolling
  manifest, that's a future feature.

- **`extras_json` schema is open within each `kind`.** The closed-set
  test asserts `kind` values, but the JSON inside `extras_json` can
  contain arbitrary keys per kind. M15.3.A/A.2/B are disciplined about
  the shape (and the audit invariants prove no secrets leak there), but
  formal extras schemas were not adopted in M15.3.

- **Single-user model.** `_generated_by_actor` is hard-coded to
  `'operator'`. M15.3.D-or-later would need to thread the authenticated
  user identity through; out of scope here.

- **Rate-limit + replay caches are in-memory and per-process.** A
  dashboard restart resets them. Same trade-off as M15.3.A and M15.3.B.
  The dashboard runs single-worker (no gunicorn), so per-process is
  sufficient. If multi-worker is introduced later, the
  `ExportAttemptLimiter` would need to move to a shared store (e.g.
  the SQLite DB) to maintain its semantics across workers.

- **Rejected-attempt write amplification.** Per the rate-limit
  design, every 429 response writes one `audit_export_request` row.
  An authenticated attacker could in principle burst thousands of
  requests after hitting the cap, each one generating one audit
  row. This is actually a *feature* for compliance — every attempted
  access is logged — but it bounds the per-day audit-table growth
  at "attacker request rate × 24h". Acceptable given the attacker
  must already have valid creds + TOTP to reach this endpoint.

- **No CSV-Excel character set negotiation.** The CSV files are UTF-8.
  Excel on Windows defaults to CP1252 in some locales; if the operator's
  Excel doesn't auto-detect UTF-8, characters in `extras_json` may
  render badly. LibreOffice handles UTF-8 cleanly. If this becomes a
  pain point, a BOM prefix could be added — but it's not the kind of
  change to make speculatively.
