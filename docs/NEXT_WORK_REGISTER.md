# Next Work Register

Carry-forward items that are **NOT** being done in the current milestone but **must not be lost** when the chat compacts or context resets.

This file is updated by every milestone closeout. Each item has: **status, why deferred, acceptance criteria, links** — and once shipped, the **closing commit hash**.

> **Rule:** anything explicitly deferred from a milestone discussion goes here within the same commit that defers it. The register is part of the repo so it survives any chat reset.

---

## Active items

### M15.3.A.cutover.perf — Dashboard login latency follow-up (DEFERRED, non-blocking)
- **Status:** Recorded 2026-06-04 at M15.3.A.cutover closeout. Not blocking the cutover (cutover is CLOSED).
- **Symptom:** Operator observed browser login felt slow (~7-10 seconds end-to-end) after the HTTPS cutover landed. Expected order of magnitude is ~1-2 seconds (bcrypt verify at cost factor 12 is ~250ms; the rest of the chain should be sub-second).
- **Why deferred:** No safety-critical impact; login works correctly. The cause is unknown. Investigation requires measurement before any code change.
- **Suggested first investigation steps when the operator picks this up:**
  - Measure `/api/login` raw server response time via `curl -w '%{time_total}' -o /dev/null -s ...` (separates server-side from client-side).
  - Check Caddy access logs (`/var/log/caddy/access.log`) for outlier latencies; format is JSON so it greps cleanly.
  - Check the dashboard `journalctl -u algo-trader-dashboard.service` for any slow-query warnings during login.
  - If server-side is fast (<1s) and total feels slow, the latency is client-side: browser fetching the dashboard's inline HTML/CSS/JS bundle, image search calls, etc.
  - Note: bcrypt cost factor is 12; reducing it would speed login but at a security cost. Don't change without explicit reason.
- **Acceptance when complete:** measured baseline + either an accepted explanation or a targeted fix.
- **Hard constraints:** no code changes for this entry; pure investigation/measurement. Any subsequent code change to address slowness would be a fresh sub-milestone with its own pre-code checklist.

### M15.3.A.persist — Persist login rate-limit state across restarts (DEFERRED)
- **Status:** Not started. In-memory rate-limiter shipped in M15.3.A per Q-A.1 approval.
- **Why deferred:** In-memory was the approved trade-off in M15.3.A — simpler, faster, no schema migration. The DB-backed variant only adds value if a real brute-force incident occurs (since `auth_events` already captures the audit trail across restarts).
- **Acceptance criteria when undeferred:**
  - New `auth_rate_limit` SQLite table or extend `auth_events` with a denormalized view.
  - `RateLimiter` class learns to read/write SQLite while keeping the in-memory cache hot.
  - Tests for: process restart preserves lockouts; concurrent multi-process workers share state.
- **Estimated effort:** ~150-250 LOC including tests. No engine impact.
- **Owner:** TBD.

### M15.5.A — current_equity_usd / accountSummary polish (DEFERRED, optional)
- **Status:** Path A chosen at M15.5 closeout. `exposure_partial` is by design and accepted by the engine; the polish would lift it to `exposure_fresh` and remove the `exposure_stale` UI badge.
- **Why deferred:** Engine semantics unchanged regardless. The polish is UI-only.
- **Acceptance criteria when undeferred:**
  - New `make_ibkr_paper_account_reader()` factory in `bot/risk_authority/ibkr_paper_reader.py` (or extend `make_ibkr_paper_positions_reader` to share state).
  - Reads `ib.accountSummary()` for `tag='NetLiquidation'` only — no other fields.
  - Read-only contract preserved: `readonly=True`, no order methods, no live mode.
  - Wired into `tools/ingest_exposure_state.py` as the `account_reader` callable on `IBKRExposureAdapter`.
  - Tests assert: equity present → `exposure_status=exposure_fresh`; equity missing → `exposure_partial` (current behaviour preserved as fallback).
- **Estimated effort:** ~150-200 LOC including tests. No engine changes.
- **Owner:** TBD.
- **Reference:** M15.5 closeout discussion (commit `60281c4`).

### M14.C.IBKR — IBKR paper PnL ingestion (DEFERRED)
- **Status:** Not started. Currently `pnl_unknown` warning shows on M14.G for `ibkr_paper`.
- **Why deferred:** M15.5 wired exposure ingestion only. PnL is a separate M14.C surface.
- **Engine semantics:** `pnl_unknown` is a `SIGNAL_ONLY` gate today (`bot/risk_authority/engine.py` reason `daily_pnl_unknown`). Clearing it lets the engine consume real daily PnL for `ibkr_paper`.
- **Acceptance criteria when undeferred:**
  - New `bot/risk_authority/ibkr_paper_pnl_reader.py` mirroring the M15.5 exposure-reader pattern.
  - Read-only contract identical to M15.5 (`readonly=True`, dedicated client ID distinct from 11/12/15/99).
  - Uses `ib.accountSummary()` (NetLiquidation, DayPnL, RealizedPnL) OR `ib.pnl()` — whichever IB exposes cleanly.
  - Wired into existing M14.C `tools/ingest_pnl_state.py`.
  - AST-asserted: no order methods, no live mode.
- **Estimated effort:** 1 sub-milestone, ~300-500 LOC including tests.
- **Owner:** TBD.

### M15.3.B — manual_reset operator flow (CLOSED 2026-06-04)
- **Status:** CLOSED. Single implementation commit `2f55f1d`. Terminal verification + operator browser end-to-end verification both passed on VPS 2026-06-04. Pre-code Q-style checklist approved by operator with corrections C1..C4 + implementation corrections 1..10; all honoured.
- **Browser verification evidence (operator, real session over HTTPS):** Logged in at `https://algotrading.marketwarrior.club` with password + Google Authenticator code. Opened Recovery / Operator manual_reset section. Set `ibkr.kill_switch=true` for the controlled test. Recovery preview correctly showed `etoro=false, global=false, ibkr=true (locked)`. Entered operator reason ("M15.3.B browser verification: clearing test ibkr kill switch after confirming no broker action is performed."), typed `RESET`, entered fresh authenticator code, submitted. Browser returned success: `Cleared 1 kill switch(es): ibkr`, `auth_event_id=38`, `decision_id=mr-3086a40a9b2f46e5`. Full chain verified: preview → confirm → step-up TOTP → reset → dual audit → kill switch clear.
- **Terminal verification evidence (operator, on VPS):** HEAD `2f55f1d`; `test_m15_3_b_manual_reset` 51/51 OK; regression sweep all green; `algo-trader-dashboard.service` active; `caddy.service` active; `ss` confirms `:8080` still bound to `127.0.0.1:8080` only (M15.3.A.cutover bind preserved); `https://algotrading.marketwarrior.club/api/health` 200; `git status` clean.
- **What shipped:**
  - `dashboard/auth/manual_reset.py` (NEW, ~370 LOC) — pure-logic helpers: PreviewTokenStore (session-bound, 60s TTL, single-use), rate-limiter factory (3/3600s/3600s), step-up TOTP check (only `hint='recently_used'` per C1), policy I/O, validators, atomic-reset transaction
  - `bot/risk_authority/audit_decisions.py` (extended, +133 LOC) — additive new `write_manual_reset_decision()` function; all pre-existing functions byte-identical (asserted by G11 `test_audit_decisions_only_additive_change`)
  - `dashboard/auth/audit.py` (extended, +5 lines) — 4 new closed kinds in `ALLOWED_KINDS`: `manual_reset_preview`, `_attempt`, `_success`, `_failure`
  - `dashboard/app.py` (extended, ~+490 LOC) — `GET /api/manual-reset/preview` + `POST /api/manual-reset` endpoints + Recovery nav link + minimal Recovery UI + JS handlers
  - `test_m15_3_b_manual_reset.py` (NEW, 51 tests across 12 groups G1..G12, ~870 LOC)
  - `docs/M15_3_B_manual_reset.md` (NEW, full runbook + threat model + §11 VPS verification command + §12 honest residual)
  - Three older test files (`test_m15_3_a_dashboard_auth.py`, `test_m15_3_a_2_totp.py`, `test_m15_5_ibkr_exposure.py`) had `bot/risk_authority/audit_decisions.py` removed from their PROTECTED tuple with a docstring note pointing to test_m15_3_b's additive-only check; nothing else touched in those files
- **Hard constraints honoured** (asserted in test suite):
  - No broker orders / writes / live-trading code (AST scan G10)
  - No scanner/strategy changes (protected-files G11, 0/23)
  - No M14 engine/governor/snapshot/preflight changes (protected-files G11)
  - No eToro/IBKR adapter changes (protected-files G11)
  - No M16 work, no multi-user work, no extra dashboard platform work
  - TOTP error API exposes ONLY `hint='recently_used'`; wrong/malformed/expired/missing all return generic `totp_invalid` (G4 + operator C1)
  - No TOTP code/secret/otpauth URI/password/raw session ID in logs or audit extras (G7 secret-material invariant sweep with known-secret substring blacklist)
- **Authoritative operator reference:** [`docs/M15_3_B_manual_reset.md`](M15_3_B_manual_reset.md).
- **Post-M15 direction note (added 2026-06-04 on M15.3.A.cutover closeout):** `M15.3.B` (operator-action safety surface, now CLOSED) and `M15.3.C` (compliance audit/export, remaining) are explicitly preserved on the active path because they fit the "safety/compliance" exception to the post-M15 dashboard freeze. Other dashboard work (e.g. `M15.3.D` multi-user roles) is deferred indefinitely.

### M15.3.C — Compliance audit + export (CLOSED 2026-06-05)
- **Status:** CLOSED. Implementation chain `0018c32` (initial) → `02b5dcf` (ChatGPT-review rate-limit fix). Terminal verification + operator browser end-to-end verification both passed on VPS 2026-06-05. Pre-code checklist Q-C.1..Q-C.12 + post-review correction C-α (strict "every authenticated attempt counts" rate-limit, M15.3.C-local `ExportAttemptLimiter` class, shared M15.3.A/B `RateLimiter` unchanged) all honoured.
- **Browser verification evidence (operator, real session over HTTPS):** Logged in at `https://algotrading.marketwarrior.club` with password + Google Authenticator. Opened Recovery → Audit Export (M15.3.C). Downloaded JSONL at `audit_export_20260605T094145Z.jsonl`: manifest parsed, `_schema_version=1`, `_format=jsonl`, `_row_counts={auth_events:48, risk_decisions_manual_reset:1}`, payload SHA-256 verified, `_source` values only `auth_events` and `risk_decisions_manual_reset` (Q-C.1 scope holds). Downloaded CSV ZIP at `audit_export_20260605T094158Z.zip`: contains exactly `manifest.txt` + `auth_events.csv` (50 data rows) + `risk_decisions_manual_reset.csv` (1 data row). The +2 auth_events delta vs JSONL confirms the meta-audit chain works as designed — every export attempt that reaches the endpoint writes a permanent `audit_export_request` row visible in subsequent exports.
- **Terminal verification evidence (operator, on VPS):** HEAD `02b5dcf`. `test_m15_3_c_audit_export` 37/37 OK. Regression sweep all green: `test_m15_3_b_manual_reset` 51/51, `test_m15_3_a_dashboard_auth` 101/101, `test_m15_3_a_2_totp` 52/52, `test_m13_4a_allocation` 61/61, `test_m14_e_engine` 105/105, `test_m14_g_dashboard` 51/51, `test_m15_4_gateway_health` 50/50, `test_m15_5_ibkr_exposure` 78/78. Total: 586 tests OK. `algo-trader-dashboard.service` + `caddy.service` both `active`. `ss` confirms `:8080` bound to `127.0.0.1:8080` only (M15.3.A.cutover bind preserved). Caddy listening on `*:80` + `*:443`. `https://algotrading.marketwarrior.club/api/health` returns HTTP 200. Unauthenticated `GET /api/audit-export` returns HTTP 401 (expected — `@require_auth` enforced). `git status` clean.
- **What shipped (2 commits, 7 files total — see `MILESTONE_STATUS.md` M15.3.C closeout block for the per-commit breakdown):**
  - `dashboard/auth/audit_export.py` (NEW, ~650 LOC after both commits) — `ExportAttemptLimiter` class + pure-logic primitives (date validation, row reading, JSONL/CSV-ZIP builders, redaction scanner, manifest builder, filename construction). Zero broker/scanner/strategy/engine imports.
  - `dashboard/auth/audit.py` (extended, +7 lines) — `audit_export_request` added as the 18th `ALLOWED_KINDS` value.
  - `dashboard/app.py` (extended, ~+290 LOC net after both commits) — `m153c_audit_export()` endpoint + minimal Audit Export card on the Recovery page.
  - `test_m15_3_c_audit_export.py` (NEW, 37 tests across 12 groups, ~1000 LOC)
  - `docs/M15_3_C_audit_export.md` (NEW, ~330 LOC) — full operator runbook.
- **Hard constraints honoured** (asserted in test suite):
  - No broker orders / writes / live-trading code (AST scan G10)
  - No scanner/strategy changes (protected-files G11, 0/24)
  - No M14 engine/governor/snapshot/preflight changes (protected-files G11)
  - No eToro/IBKR adapter changes (protected-files G11)
  - No M16 work, no multi-user work, no new dashboard platform (one endpoint + small UI card only)
  - No mutation of audit data — strictly read-only export of immutable rows; only write is the single `audit_export_request` meta-audit row per attempt
  - No new external dependencies (stdlib only + `dashboard.auth.audit`)
  - No service restarts / sync.sh / deploy.sh / systemd-unit changes
  - No `.env` mutation
  - No secret material in output / logs / extras / filenames / response bodies (G7 secret-material sweep + runbook §5)
  - **No changes to the shared `dashboard.auth.rate_limit.RateLimiter`** — M15.3.A login and M15.3.B manual_reset rate-limit semantics preserved exactly. M15.3.C's stricter "every attempt counts" semantics live in the new M15.3.C-local `ExportAttemptLimiter` class.
- **Authoritative operator reference:** [`docs/M15_3_C_audit_export.md`](M15_3_C_audit_export.md).
- **M15 status:** M15.3.C was the final M15.3 sub-milestone. With it CLOSED, **M15 itself is now fully CLOSED.** The next active milestone is M16 (see below).

### M15 — fully CLOSED 2026-06-05
All M15 sub-milestones (M15.0-pre, M15.0, M15.1, M15.2, M15.3.A, M15.3.A.2, M15.3.A.cutover, M15.3.B, M15.3.C, M15.4, M15.5) are CLOSED. The Production Hardening milestone is complete. Carry-forwards remain DEFERRED (none blocking): `M15.3.A.cutover.perf` (login latency follow-up), `M15.3.A.persist` (DB-backed rate-limit), `M15.3.D or later` (multi-user roles — DEFERRED INDEFINITELY). Dashboard work now stops unless safety- or compliance-driven, per the post-M15 strategic direction.

### M15.3.D or later — Multi-user / read-only dashboard roles (DEFERRED INDEFINITELY)
- **Status:** Not started. Recorded 2026-06-04 to ensure the idea isn't lost. **Updated 2026-06-04 on M15.3.A.cutover closeout: DEFERRED INDEFINITELY under the post-M15 strategic direction.** The dashboard work is frozen post-M15 unless safety- or compliance-driven; multi-user is neither.
- **Why proposed (originally):** The current dashboard authenticates a single operator with one password and grants full read/write. Once 2FA (M15.3.A.2) and `manual_reset` (M15.3.B) ship, the natural next axis of access-control is splitting the principal into roles. Two roles cover ~95% of realistic needs: `operator` (current behaviour: full read + state-changing actions) and `viewer` (read-only — sees the dashboard, signals, exposure, audit log, but cannot POST to any state-changing endpoint).
- **Why now deferred indefinitely:** No second human is involved in the current operation. Multi-user adds attack surface for zero current benefit. The post-M15 direction reaffirms this: dashboard expansion stops unless safety- or compliance-driven, and multi-user roles are neither. Re-evaluate only when a concrete need materialises (e.g. an external compliance reader, a non-technical co-operator).
- **Acceptance criteria when undeferred (sketch — to be expanded into a pre-code checklist at the time):**
  - New `dashboard_users` SQLite table (or equivalent in `.env`/`users.json`) with `username`, `password_hash`, `role`, `totp_secret_optional`, `created_at`. Single-row legacy mode preserved as a fallback when the table is empty (operator-only).
  - `role` is a closed set: at least `operator` and `viewer`. No free-form roles.
  - New `@require_role(<role>)` decorator. `@require_auth` continues to mean "authenticated"; `@require_role("operator")` is required on every state-changing endpoint. All viewer-safe GETs stay on `@require_auth` only.
  - `tools/set_dashboard_password.py` extended with `--user <name> --role <operator|viewer>` for managing multiple users; default behaviour without these flags continues to manage the single legacy operator.
  - `auth_events` gains a `username` column (additive migration) — or, more conservatively, the `extras_json` field carries it. Decision deferred to the pre-code checklist.
  - Test surface: role-based access matrix (operator can POST `/api/kill-switch/activate`; viewer cannot; both can GET `/api/health`), session-cookie correctly identifies the user, CSRF still enforced per-role.
  - Hard constraints unchanged: no orders, no broker writes, no live mode, no scanner/strategy/M14 engine/eToro changes.
- **Estimated effort:** ~500-700 LOC including tests + docs. Self-contained milestone.
- **Reference:** Mike's request 2026-06-04 at M15.3.A closeout. Explicit instruction: "do not implement multi-user now." Updated 2026-06-04 on M15.3.A.cutover closeout: "After M15, stop expanding the dashboard unless needed for safety."

### M16 — Historical data + first signal engine (CLOSED 2026-06-05)
- **Status:** **CLOSED.** Five commits on `origin/main`:
  - `c6e98b7` — M16.A: historical data engine + M16.B local-read proof
  - `af96eda` — M16.A.fix-1: honest rate-limit classification (was silently `no_data`)
  - `c5702f1` — M16.A.fix-2: `cmd_status` auto-migrates v1 DB + clean stale docstrings
  - `cc979aa` — M16.A.fix-3: `/api/historical/status` auto-migrates v1 DB
  - `aef8335` — M16.A.fix-4: freshness-aware incremental no-op + clean remaining docstrings
- **Terminal verification evidence (operator, on VPS, 2026-06-05 fix-4 acceptance):** HEAD `aef8335` (= expected). `requirements.txt` install exit 0; pyarrow = 24.0.0. M16 suite: 70/70 OK, 1 skipped. `schema_version = 2`, `symbols_rate_limited` column present. AAPL 1D backfill (fix-3 run): `status=ok`, `symbols_ok=1`, `bars_fetched=11462`, `bars_written=11462`. Local read proof: `get_bars rows = 11462`, `freshness_status = fresh`, `last_ts_utc = 2026-06-05 00:00:00+00:00`, SMA(20) returned 5 trailing values. **Real Parquet on disk at `/opt/algo-trader/data/historical/yfinance/1D/AAPL.parquet` = 571,139 bytes.** Fix-4 acceptance (back-to-back incremental against fresh coverage): `status=ok`, `symbols_attempted=1`, `symbols_ok=1`, `no_data=0`, `failed=0`, `rate_limited=0`, `bars_fetched=0`, `bars_written=0`, `bars_updated=0`, `duration_sec=0.01`, **no provider call, no banner, no yfinance error output**. DB last-run rows: `run_id 7 = incremental|ok|1|1|0|0|0|0|0`, `run_id 4 = backfill|ok|1|1|0|0|0|0|11462`. Production: dashboard active, caddy active, HTTPS `/api/health` = 200. `git status` clean.
- **M16 proves end-to-end:** real yfinance provider fetch → atomic Parquet write → SQLite coverage update → local `get_bars()` read → SMA local-read proof. Plus four operational properties: honest rate-limit classification (fix-1), CLI status migration safety (fix-2), dashboard status migration safety (fix-3), freshness-aware incremental no-op (fix-4). Plus no generated runtime data ever committed.
- **Hard constraints upheld across all five commits:** protected files vs `ceb8cd5` = 0/20 modified; `bot/data.py` sha256 byte-identical to baseline; AST scan over `bot/historical/` = 0 broker/order/scanner/strategy imports across 11 files; regression sweep 1,183 tests across 30 suites, 0 failed; git-tracked under `data/` = only the 2 intentional CSVs.
- **Authoritative operator reference:** [`docs/M16_historical_data.md`](M16_historical_data.md) — includes the §P Yahoo/yfinance rate-limit operations runbook landed in fix-1.
- **Known carry-forward limitations (NOT blocking closure):**
  - Live multi-symbol backfill at scale remains rate-limit-prone against Yahoo from the VPS IP. The engine reports rate-limits honestly; the operator may need to stagger/reduce/wait. A paid provider behind the same `BaseProvider` contract is a one-file future addition (out of scope for M16).
  - 4H ↔ 1H consistency edge: if 1H was just updated but 4H is "fresh" per its own threshold, 4H is not automatically re-resampled. Tracked as a future enhancement; current behaviour is documented in M16 runbook.
  - Freshness-aware incremental no-op (fix-4) means a split that lands within the freshness window AND triggers yfinance to retroactively rewrite Adj Close in the same window will not be detected until the next incremental past the window. For US equities at 1D granularity this is acceptable; `force-rebuild` provides an escape hatch when needed.

### M1–M16 audit-only pass (NEXT ACTIVE — recorded 2026-06-05 at M16 closeout)
- **Status:** Not started. **This — not M17 coding — is the next active task.** Recorded at M16 closeout per explicit operator instruction.
- **Why now:** The M16 work surfaced multiple class-of-issues that ChatGPT's line-by-line review caught after VPS verification (rate-limit classification, two separate migration-order bugs, missing incremental no-op). Each was a small fix individually but the pattern suggests the prior M1–M15 surface area should be audited from the actual code before another large coding milestone (M17 Outcome Learning Loop) begins.
- **Scope:** independent inspection of the M1–M16 codebase by two reviewers (this assistant + ChatGPT) producing separate findings lists. Lists will be compared; only then will fix-priority decisions be made.
- **Hard constraint:** **NO CODE CHANGES during the audit phase.** Inspection only. Output is a written findings list (sub-milestones if any fixes are approved come later, each with its own pre-code checklist).
- **Acceptance criteria when audit pass complete:** a comparison document (or chat record) of the two findings lists, with operator decisions on which findings (if any) become tracked sub-milestone fixes. Once the audit pass clears, M17 (Outcome Learning Loop / Closed-Loop ML) becomes the next coding milestone.
- **Reference:** operator instruction at M16 closeout, 2026-06-05.

### M17 — Outcome Learning Loop / Closed-Loop ML (PROPOSED, AFTER AUDIT PASS)
- **Status:** Not started. Sequenced AFTER the M1–M16 audit pass clears, not directly after M16 closure.
- **Scope sketch:** the dataset bottleneck for ML readiness is the `candidate_snapshots` flywheel which is still accumulating from the M14 pipeline. M17 wires `ml_train.py` (existing 541-line XGBoost meta-labeling, M9) into the scanner as a live signal filter, using the M16 historical store as the source of truth for backtest-vs-live consistency.
- **Hard constraint:** matches the permanent rule "Backtesting using the same live strategy". Any closed-loop ML training that diverges from the live scanner's feature path is a P0 bug.
- **Reference:** repositioned from "after M16" to "after audit pass" at M16 closeout, 2026-06-05. Original entry sketched at M15.3.A.cutover closeout, 2026-06-04.

### M18 — Advanced signal scoring + paper-trade automation (2-4 weeks; PROPOSED, AFTER M17)
- **Status:** Not started. Sequenced after M17. Pre-code Q-style checklist required.
- **Scope sketch:** ranked signal scoring (multi-factor); automated paper-trade execution on IBKR paper (M11 path already wired); flywheel data accumulation accelerated; integration with the M14 risk authority engine for sizing/gating.
- **Reference:** Mike's request 2026-06-04 at M15.3.A.cutover closeout. Timing estimate 2-4 weeks.

### M19+ — Optimisation, news/sentiment, universe diagnostics, controlled live, fully autonomous (DEFERRED, sequencing tbd)
- **Status:** Not started. Listed here so scope is not lost between M18 closeout and the next planning session.
- **Approximate timing (per operator direction 2026-06-04):**
  - **Controlled live trading readiness**: 2-3+ months minimum from M15 closeout.
  - **Fully autonomous advanced live bot**: 3-6+ months minimum from M15 closeout.
- **Existing content in `ROADMAP.md`** (some of which has been folded into M16-M18 above): optimiser / adaptive sizing; news / sentiment / macro overlay; universe diagnostics & discovery; per-regime sizing curves; controlled live (operator-in-the-loop); semi-automated live; fully autonomous; correlation-aware sizing; automated broker failover.
- **Acceptance criteria when each is undeferred:** per its line in `ROADMAP.md` (M16+ section restructured 2026-06-04). Each gets its own pre-code checklist when authorised.

---

## Post-M15 strategic direction (recorded 2026-06-04 at M15.3.A.cutover closeout)

**After M15 closes, dashboard work stops unless safety- or compliance-driven.** The priority becomes the advanced trading bot: historical data → strategy criteria & parameters → backtesting → signal scoring → paper-trade automation → optimisation → controlled live trading → fully autonomous.

What stays "dashboard work" on the active path: M15.3.B (manual_reset — safety surface) and M15.3.C (compliance audit/export). Both fit the safety/compliance exception.

What is now deferred indefinitely on the dashboard side: M15.3.D (multi-user roles), M15.3.A.persist (DB-backed rate-limit; only revisit on real incident), M15.3.A.cutover.perf (login latency follow-up; non-blocking).

What is the next major work after M15.3.B + M15.3.C ship: **M16 (historical data + first signal engine)**. See above for the M16/M17/M18 breakdown with concrete timing estimates and `ROADMAP.md` for the full forward sequence.

---

## Closed items

### M15.3.A.cutover — Caddy/TLS + 127.0.0.1 bind (CLOSED 2026-06-04)
- **Closing chain:** Caddy install + `/etc/caddy/Caddyfile` + ACME issuance for `algotrading.marketwarrior.club` → `224e8a3` (production-code bind-host fix: `app.run(host=_m153a_bind_host, ...)` replaces hardcoded `'0.0.0.0'`) → `383bec0` (test-fixture dotenv-isolation against post-cutover VPS `.env`, mirrors M15.3.A.2 fix-1 `7ab7555` and proactively fixes `test_m13_4a_allocation.py` with the same pattern).
- **Acceptance criteria met:**
  - Caddy (latest stable) installed via the official Cloudsmith apt repo on Ubuntu 24.04. `caddy.service` `active` and `enabled` (survives reboot).
  - `/etc/caddy/Caddyfile` declares `algotrading.marketwarrior.club` reverse-proxying to `127.0.0.1:8080`, with gzip + `X-Real-IP`/`X-Forwarded-For` headers + access log to `/var/log/caddy/access.log` (50 MB × 5 rotation).
  - ACME / Let's Encrypt cert obtained automatically (HTTP-01 challenge). Auto-renewal handled by Caddy internally — no cron.
  - `/opt/algo-trader/.env`: `DASHBOARD_BIND_HOST=127.0.0.1`, `DASHBOARD_HTTPS_MODE=true`. Permissions `0o600` preserved.
  - `app.run()` now passes the env-controlled `_m153a_bind_host` variable (fixed in `224e8a3` — was previously hardcoded `'0.0.0.0'` and ignored the env var).
  - Test fixtures (`test_m15_3_a_dashboard_auth.py` + `test_m13_4a_allocation.py`) now isolate cleanly from the post-cutover VPS `.env` regardless of which dashboard env vars dotenv loads (fixed in `383bec0` — test-only patch, no production-code change).
- **VPS verification on closeout day (2026-06-04, HEAD `383bec0`):**
  - `test_m15_3_a_dashboard_auth.py` 101/101 OK. Regression sweep on VPS all green: `test_m13_4a_allocation` 61/61, `test_m15_3_a_2_totp` 52/52, `test_m15_5_ibkr_exposure` 78/78, `test_m15_4_gateway_health` 50/50, `test_m14_g_dashboard` 51/51, `test_m14_e_engine` 105/105.
  - `algo-trader-dashboard.service` and `caddy.service` both `active`.
  - `ss -ltnp 'sport = :8080'` shows `127.0.0.1:8080` — the critical evidence of backend lockdown. External `:8080` from operator laptop returns connection refused.
  - Caddy listens on `*:80` and `*:443`.
  - `https://algotrading.marketwarrior.club/api/health` → HTTP/2 200 with `via: 1.1 Caddy` header.
  - `http://algotrading.marketwarrior.club` → `HTTP/1.1 308 Permanent Redirect` to HTTPS.
  - **Operator authenticated against the dashboard in a real browser session over HTTPS** with password + 6-digit Google Authenticator code — full chain works (HTTPS → Caddy → loopback → dashboard → password → TOTP → session rotation → CSRF token → state-changing POSTs accepted).
- **Two real bugs caught + fixed during the cutover** (honestly recorded in the commit messages):
  1. **`224e8a3`** — production-code bind-host bug. `dashboard/app.py` line 103 correctly read `DASHBOARD_BIND_HOST` into `_m153a_bind_host`, but the `app.run()` call at the bottom passed a hardcoded `'0.0.0.0'`. After writing `DASHBOARD_BIND_HOST=127.0.0.1` to `.env`, `ss` still showed `0.0.0.0:8080`. Fix: 1 functional line + 5 comment lines. Three regression tests added (AST scan + two subprocess env→variable tests). Negative-verified.
  2. **`383bec0`** — test-fixture dotenv pollution after cutover. M15.3.A tests failed 21/100 on the VPS because `dotenv` repopulated the polluted env vars after the fixture's cleanup. Fix: same import-first-then-clean pattern as M15.3.A.2 fix-1; extended `_AUTH_ENV_KEYS` with `DASHBOARD_TOTP_SECRET` + `DASHBOARD_PORT`; seeded empty env vars in tests that reload dashboard.app; added `test_fixture_isolates_password_only_login_from_vps_totp_dotenv` regression. Same fix applied to `test_m13_4a_allocation.py`. **Test-only patch; production code in `dashboard/app.py` not touched in fix-2; production VPS `.env` not touched.**
- **Hard-constraint evidence:** protected files modified across `224e8a3` + `383bec0` vs `274f12e` (pre-cutover baseline): **0 / 24**. No trading code, scanner, strategy, M14 engine/governor/snapshot/preflight, eToro, IBKR-reader, broker, order-path, live-mode, sync.sh, deploy.sh, or systemd-unit changes. Caddy is a new OS-package systemd service outside the project repo.
- **Authoritative operator reference:** [`M15_3_A_dashboard_auth.md`](M15_3_A_dashboard_auth.md) §3 (Caddy install runbook) and §13 (cutover closeout evidence).
- **Honest residual exposure / follow-up (non-blocking):** browser login felt slow (~7-10 seconds) on closeout day. Recorded as `M15.3.A.cutover.perf` in Active items above. Not a security issue; investigation queued.
- **Strategic note (recorded at this closeout):** with HTTPS + 2FA both in place, the cutover unblocks `M15.3.B` (manual_reset). Beyond M15.3, dashboard work stops unless safety/compliance-driven. See "Post-M15 strategic direction" below.

### M15.3.A.2 — Dashboard TOTP / Google Authenticator 2FA (CLOSED 2026-06-04)
- **Closing commits:** `723b963` (initial implementation per pre-code checklist Q-A.1..Q-A.11 + Corrections 1–9) → `7ab7555` (test-fixture VPS regression fix — test-only patch, no production code changes; covers the dotenv-pollution issue surfaced during VPS verification).
- **Acceptance criteria met:**
  - New module `dashboard/auth/totp.py`: RFC 6238 TOTP (30-sec window, ±1 step tolerance via `valid_window=1`); replay cache keyed by `(sha256(secret)[:16], time_step)` with 120-sec TTL; **no raw codes or secrets stored in cache memory** (per Q-A.10 correction).
  - `/api/login` extended with a second-factor block between password verify and session rotation. **Hard guarantee preserved**: password-only login is byte-identical to M15.3.A when `DASHBOARD_TOTP_SECRET` is unset/empty.
  - Failure semantics per Correction 3: wrong-password → 401 generic; wrong-TOTP → 401 generic (no leak of which-factor-failed); right-password + missing-TOTP → 401 `totp_required` (acknowledged password-validity oracle, rate-limit-capped); missing-TOTP does NOT increment counter (operator forgot); wrong-TOTP DOES increment same per-IP bucket as wrong-password.
  - `tools/set_dashboard_password.py` gained `--enable-totp` (sanity-checks password is set, refuses overwrite, verify-before-write, abort-on-Ctrl-C without `.env` mutation) and `--disable-totp` (removes only `DASHBOARD_TOTP_SECRET`, best-effort `totp_disabled` audit — recovery path must not block on broken DB).
  - Login form gained always-visible TOTP input (per Q-A.8 — no probe endpoint). JS handles `totp_required` response with orange-outline focus.
  - `auth_events.ALLOWED_KINDS` extended with 5 new closed values; **`extras_json` invariant**: never contains code/secret/otpauth-URI/password material (Correction 4). Verified against the live VPS audit log: `SECRET_MATERIAL_DETECTED = False`.
  - New deps pinned and clean-venv verified: `pyotp==2.9.0`, `qrcode==7.4.2`. Install / pip-check / imports all exit 0 in a fresh venv.
- **VPS verification on closeout day (2026-06-04, HEAD `7ab7555`):** M15.3.A.2 tests 52/52 OK; M15.3.A regression 97/97 OK; clean temp venv (`CLEAN_INSTALL_EXIT=0`, `CLEAN_CHECK_EXIT=0`, `CLEAN_IMPORT_EXIT=0`); pyotp/qrcode imports OK; dashboard `is-active = active`; `/` and `/api/health` both HTTP 200. `--enable-totp` ran successfully — `.env` backup created, `DASHBOARD_TOTP_SECRET` written (length 32, valid base32). **Operator authenticated against the dashboard in a real browser session with password + Google Authenticator code** — the full chain works (login form → password verify → TOTP verify → session rotation → CSRF token → state-changing POSTs accepted). `auth_events` recorded `totp_setup`, `totp_success`, `login_success`. Redacted audit check confirms `SECRET_MATERIAL_DETECTED = False`.
- **VPS-only bug caught + fixed during closeout** (`723b963` → `7ab7555`): test-setup bug, not production-code bug. `dashboard.app` calls `load_dotenv()` at module-import time; my original `_make_test_app` cleaned `os.environ` BEFORE the dashboard.app import, so dotenv re-populated `DASHBOARD_PASSWORD_HASH` from the real `.env` afterwards, and `verify_password` rejected the test's plaintext password — login returned 401 at the password step, never reaching the TOTP block, so no `totp_*` audit rows were written. Sandbox could not reproduce (no `.env` in sandbox). Fix: import dashboard.app first, clean env after, set test values. New `test_fixture_overrides_preexisting_password_hash_from_env` regression test explicitly seeds a real bcrypt hash into `os.environ` before invoking the fixture and asserts the fixture cleanly overrides it. Negative-verified by reverting the fix. **The "extras_json never leaks secrets" production invariant was unchanged by the fix.**
- **Tests at closeout:** `test_m15_3_a_2_totp.py` 52/52 OK (51 design + 1 fixture-robustness regression). Regressions clean across the whole project: `test_m15_3_a_dashboard_auth` 97/97, `test_m15_5_ibkr_exposure` 78/78, `test_m15_4_gateway_health` 50/50, `test_m14_g_dashboard` 51/51, `test_m13_4a_allocation` 61/61, `test_m14_e_engine` 105/105.
- **Hard-constraint evidence:** protected files modified vs `648682c` (pre-M15.3.A.2 baseline) across both commits in the chain: **0 / 24**. No engine, broker, scanner, strategy, eToro, IBKR-reader, systemd, `sync.sh`, or `deploy.sh` changes. No `manual_reset` code. No multi-user code. No live mode. No orders. No broker writes. AST scan of `dashboard/auth/totp.py` confirms no forbidden imports.
- **Authoritative operator reference:** [`M15_3_A_dashboard_auth.md`](M15_3_A_dashboard_auth.md) §12 (TOTP runbook, including §12.7 closeout evidence).
- **Honest trade-offs (documented in runbook §12.6):**
  - **TOTP does NOT substitute for HTTPS.** Over plain HTTP an on-path attacker can still steal a valid session cookie after a successful 2FA login. `M15.3.A.cutover` remains an open carry-forward and a real prerequisite for state-changing operator actions like `M15.3.B` manual_reset.
  - **`totp_required` is a small password-validity oracle**, rate-limit-capped at 5 probes / 15 min. Approved trade-off for legitimate-operator UX clarity.
  - **In-memory replay cache resets on dashboard restart** — same trade-off as M15.3.A rate-limiter.
- **Carry-forward note**: `M15.3.A.cutover` (Caddy/TLS) is still important even with TOTP enabled. `M15.3.B` (manual_reset) is now blocked on either Caddy/TLS landing OR an explicit operator decision to expose `manual_reset` over plain HTTP. A DB-backed replay cache could be added later as `M15.3.A.2.persist` if a real incident materializes.

### M15.3.A — Dashboard auth/security hardening (CLOSED 2026-06-04)
- **Closing commits:** `34fc157` (initial M15.3.A shipped) → `c280a83` (script-mode `sys.path` bootstrap fix in `dashboard/app.py` + `--stdin` flag for `tools/set_dashboard_password.py`) → `f26407f` (same `sys.path` bootstrap applied to `tools/set_dashboard_password.py` + this M15.3.D entry).
- **Acceptance criteria met:**
  - bcrypt verification (cost 12) preferred via `DASHBOARD_PASSWORD_HASH`, plaintext fallback retained for transition; `'changeme'` default rejected.
  - In-memory sliding-window login rate-limit (5 failures / 10 min → 15 min lockout) per Q-A.1.
  - CSRF protection on all 16 non-exempt state-changing POST endpoints; `/api/login` the only exempt POST per Q-A.7.
  - Session cookies: `HttpOnly` always, `SameSite=Strict` always, `Secure` env-gated per correction #2.
  - Hybrid timeout: 30 min idle + 12 h absolute, both env-configurable.
  - Stable `DASHBOARD_SECRET_KEY` (no longer password-derived).
  - `auth_events` append-only audit log; raw session IDs sha256-hashed before persisting per Q-A.8.
  - `tools/set_dashboard_password.py` operates from any cwd without `PYTHONPATH`; never prints the password; backs up `.env`; preserves unrelated lines; sets 0600 perms.
- **VPS verification on closeout day (2026-06-04):** dashboard `is-active = active`; `/` and `/api/health` both HTTP 200; `auth_events` table present with 8 expected columns; `DASHBOARD_PASSWORD_HASH` valid bcrypt (prefix `$2b$`, length 60); `DASHBOARD_SECRET_KEY` length 64; `.env` permissions `0o600`; **operator successfully logged into the dashboard in a real browser session** — confirming the full login → session → CSRF → state-changing-POST flow works end-to-end.
- **Tests at closeout:** `test_m15_3_a_dashboard_auth.py` 97/97. Regressions clean: `test_m13_4a_allocation` 61/61 (minor test-only CSRF-header update — no production-code workaround), `test_m14_g_dashboard` 51/51, `test_m15_5_ibkr_exposure` 78/78, `test_m15_4_gateway_health` 50/50.
- **Hard-constraint evidence:** protected files modified vs `60281c4` (pre-M15.3.A baseline) across the entire `34fc157 → c280a83 → f26407f` chain: **0 / 24**.
- **Authoritative operator reference:** [`M15_3_A_dashboard_auth.md`](M15_3_A_dashboard_auth.md).
- **Carry-forward items deferred from M15.3.A** (still in Active above): `M15.3.A.cutover` (Caddy/TLS — operator action), `M15.3.A.persist` (DB-backed rate-limit), and the new sub-milestone `M15.3.A.2` (TOTP 2FA, sequenced before `M15.3.B`).

*(items move here with their closing commit hash when their acceptance criteria are met)*
