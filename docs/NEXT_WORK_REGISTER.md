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

### M1–M16 audit-only pass (CLOSED 2026-06-05)
- **Status:** **CLOSED.** Two independent reviews (this assistant + ChatGPT) produced findings lists; lists were merged; the 5 P0 (must-fix) findings were implemented as a 6-commit batch and pushed + VPS-verified 2026-06-05.
- **Commit chain (in order on `main`):**
  - `655c955` — P0-5: docs-only M14 engine vs scanner-path asymmetry carry-forward (created the `M14-extension-to-scanner-path` entry below).
  - `6a04735` — P0-1: XFF trusted-proxy fix in `dashboard/auth/trusted_proxy.py` (login rate-limiter bypass + audit IP corruption via rotating XFF mitigated).
  - `7e83415` — P0-2: `IBKRBroker.cancel()` supports canonical `IB-PERM-{permId}` format.
  - `a072032` — P0-4: `PortfolioRiskContext` populated via `bot/portfolio_ctx.py` (Correction B: live path reuses RiskManager reconcile via `checks['_recon']` stash — zero new IBKR round-trips per signal).
  - `0b4bf69` — P0-3: runtime M13.4A kill-switch enforcement via `bot/runtime_policy.py` (Correction A: fail-SAFE to `policy_unavailable` when DB read fails and no cached policy).
  - `268a50b` — P0-4 fixup: 6 test-fixture updates following the M15.3.B `audit_decisions.py` precedent.
- **VPS evidence (2026-06-05):** HEAD = `268a50b`; regression `Ran 563 tests in 132.984s` / `OK (skipped=1)` / exit 0; dashboard + caddy active; `/api/health` HTTP 200; `git status` clean.
- **Hard constraints upheld:** 2/20 protected files modified (both `a072032` only, both operator-pre-approved); `bot/data.py` byte-identical to `ceb8cd5`; AST-clean new modules; no `.env`/service/data changes; 65 new tests all green.
- **What remains open:** the P1 / P2 / P3 backlog. Tracked as separate entries below so individual items cannot be lost when context resets.
- **Reference:** MILESTONE_STATUS.md "P0 audit batch (M1–M16 audit) — VPS-verified 2026-06-05, CLOSED" block. Original audit-pass directive recorded at M16 closeout (2026-06-05).

### audit-P1-broker-permId-fallback (DEFERRED, recorded 2026-06-05 at P0 closeout)
- **Status:** Not started. Identified by the M1–M16 audit pass; deliberately deferred from the P0 batch to keep the P0 scope focused.
- **What:** `IBKRBroker.submit()` writes the canonical `broker_oid = f'IB-PERM-{parent_perm}'` when `permId` is non-zero, but the fallback branch when `permId` is still zero at write time uses `f'IB-{orderId}-{tp}-{sl}'`. P0-2 fixed `cancel()` to handle BOTH formats correctly. P1 considers whether to harden `submit()` itself to wait briefly for `permId` propagation (with bounded timeout) before writing the broker_oid, OR to explicitly record the `permId=0` case as an actionable warning in `execution_intents` for operator review.
- **Acceptance criteria when undeferred:** decision recorded between (a) bounded-wait approach with explicit timeout config + tests, or (b) accept the legacy format + ensure operator-visible warning. Either way, no silent fallback.
- **Reference:** P0 implementation plan / audit findings — Claude finding A4 + ChatGPT finding #1.

### audit-P1-data-rate-limit-fix (CLOSED 2026-06-05, supersedes audit-P1-data-rate-limit-investigate)
- **Status:** **CLOSED.** Investigation confirmed the bug; the patch landed, regression-green, pushed, and VPS-verified.
- **Investigation outcome (2026-06-05):** ChatGPT's audit findings #3 + #4 were vindicated. Line-by-line code inspection of `bot/providers/yfinance_provider.py`, `bot/backtest.py`, `bot/backtest_v2.py`, `bot/data.py`, `bot/scanner.py`, and the M16 reference path `bot/historical/providers_yfinance.py` confirmed:
  - The OLD provider's `_fetch_one` (used by the live scanner via `bot/data.fetch_bars`) detected rate limits only via `str(exc)` substring match on raised exceptions; it never inspected `yf.shared._ERRORS`. yfinance ≥ 0.2 catches per-symbol exceptions internally and stashes them there while returning an empty DataFrame, so swallowed rate-limits were silently misclassified as `no_data` — the `consec_rl` counter never incremented and the `MAX_CONSEC_RL` cache-only safety mode never engaged.
  - The OLD provider's `fetch_bars_range` (used by `bot/backtest_v2`) used `raise_errors=False`, which *guaranteed* swallowed rate-limits. Empty DataFrame returned `('empty_response')` on the first attempt with NO retry-with-backoff.
  - `bot/backtest.py:_fetch_yf_single` had the identical shape with the same misclassification.
  - The M16 fix pattern in `bot/historical/providers_yfinance.py` (clear `_ERRORS` before call, scan after empty df, layered `YFRateLimitError` isinstance + type-name + substring detection) was the right shape to replicate in the OLD path.
- **Sub-milestone scope (operator-approved 2026-06-05):**
  - Patched `bot/providers/yfinance_provider.py` (new module-level helpers + rewrites of `_fetch_one` and `fetch_bars_range`).
  - Patched `bot/backtest.py:_fetch_yf_single` (mirror pattern via imported helpers from the provider).
  - Strict 1-line import repair in `bot/backtest.py` (`_browser_session` was moved to `bot.providers.yfinance_provider` at Milestone 6 but the import was never updated — the module had been unimportable since M6 and was discovered mid-task). Repair was strictly bounded to the broken import line; no other `bot/backtest.py` changes.
  - `_fetch_benchmark` deliberately NOT touched per operator instruction (degrades-gracefully, lower priority).
- **Commit:** `9994692` "audit-P1-data-rate-limit-fix: detect swallowed yfinance rate-limits via yf.shared._ERRORS in the OLD provider + backtest path".
- **VPS evidence (2026-06-05):** HEAD = `9994692`; all 7 new helpers present in `bot/providers/yfinance_provider.py`; `_scan_yf_errors_for_rate_limit` present in both call sites; `bot/backtest.py` import repair landed; `bot.backtest` importable for the first time since M6. New `test_audit_p1_data_rate_limit.py` 23/23 OK. Targeted regression sweep: 206 tests / OK / 1 skipped pre-existing / exit 0. Dashboard + Caddy active; `https://algotrading.marketwarrior.club/api/health` HTTP 200; `git status` clean.
- **Hard constraints upheld:** 0/20 protected files modified by this commit; cumulative 2/20 unchanged from P0 batch (`main.py` + `bot/risk.py` from P0-4 only). `bot/data.py` sha256 byte-identical to `ceb8cd5`. AST scan clean. No `.env`/service/data changes. No new dependencies. No new status codes. Public API signatures unchanged (`bot.data.fetch_bars`, `YFinanceProvider.fetch_bars`, `YFinanceProvider.fetch_bars_range`, `bot.backtest._fetch_yf_single` all unchanged).
- **Side effect:** `backtest_cli.py` is no longer broken-on-import (it depends on `bot.backtest`). The CLI was not separately exercised in this commit — only its `_fetch_yf_single` data fetch path was patched + tested. Any further bugs in `backtest_cli.py` remain unaddressed and would need a separate sub-milestone if surfaced.
- **Reference:** P0 audit findings — ChatGPT #3 + #4, Claude D3; investigation report 2026-06-05; commit `9994692`.

### audit-P1-portfolio-ctx-engine-bypass (DEFERRED, recorded 2026-06-05 at P0 closeout)
- **Status:** Not started. Distinct from `M14-extension-to-scanner-path` (which addresses the broader M14 24-gate parity gap) — this P1 item is narrower in scope.
- **What:** ChatGPT finding #1 vs Claude finding I3 reconciliation: ChatGPT identified that `PortfolioRiskContext` was structurally underfed; Claude separately identified that the M14 engine itself is bypassed by the scanner path. P0-4 fixed the structural underfeed for `PortfolioRiskPolicy` (the gate set the scanner DOES run). The full M14 engine bypass remains open as `M14-extension-to-scanner-path` (BLOCKER FOR M22). This P1 entry tracks the residual smaller items that surfaced once P0-4 landed — e.g. confirming `local_open_intents` semantics across paper/live boundaries, expanded test fixtures with realistic populated-ctx scenarios.
- **Hard pre-requisite for closing this entry:** P0-4 verified in production (DONE at 2026-06-05) AND `M14-extension-to-scanner-path` scope decided.
- **Reference:** P0 audit findings reconciliation — ChatGPT #1 + Claude I3.

### audit-P2-batch (DEFERRED, 9 items, recorded 2026-06-05 at P0 closeout)
- **Status:** Not started. 9 P2 (should-fix) findings from the audit pass, none safety-critical, none blocking M17 or any current milestone.
- **Notable items:** consolidating the two backtest engines (deferred to M17 scope discussion); cleaning up the M16 historical-data engine + scanner backtest engine duplication; auditing `bot/risk.py` `_load_open_intents` for symbol-direction discrimination consistency; reviewing `bot/etoro/*` SignalOnlyBroker reason-code coverage parity with VALID_REASONS; auditing M13.5 audit-trail completeness across all eToro events; consolidating M14 engine reason-code spelling (`risk_rejected` vs `rejected`); reviewing M14 gate threshold defaults for tightness; rationalising the M15.3.B/.C audit chain CHECK constraint enforcement; reviewing the M14 sector-map config-vs-DB precedence.
- **Acceptance criteria for closing each:** individual sub-milestone with its own pre-code checklist + tests. No bulk fixup.
- **Reference:** P0 audit findings — full P2 list in the audit-pass merged-fix-plan.

### audit-P3-batch (DEFERRED, 6 items, recorded 2026-06-05 at P0 closeout)
- **Status:** Not started. 6 P3 (nice-to-have) findings. Cleanup-grade.
- **Notable items:** docstring consistency across `bot/*` modules; consolidating the 4 separate `protected_files_*` fixture patterns into a single shared helper; tightening test-discovery patterns (`test_m10.py` / `test_m11.py` / `test_m12.py` use custom-script style instead of `unittest`); replacing synthetic test signal IDs (`888888`, `999999`) with an explicit `is_test` column in `execution_intents`; trimming dead imports flagged by static analysis.
- **Acceptance criteria for closing each:** sub-milestone or grouped cleanup commit. No code-quality gate is currently failing on these.
- **Reference:** P0 audit findings — full P3 list in the audit-pass merged-fix-plan.

### M14-extension-to-scanner-path (BLOCKER FOR M22, recorded 2026-06-05)
- **Status:** Not started. Tracked here so it isn't lost. This is the first concrete carry-forward produced by the M1–M16 audit-only pass.
- **What:** Extend the M14 Risk Authority Engine (`bot.risk_authority.engine.decide` — 24 gates) so it wraps the scanner-driven IBKR submit path in `main.py`, OR extend `bot/risk.py` with explicit equivalents of every M14 gate currently missing from the scanner path. AST-asserted parity required either way.
- **Why:** The M14 engine is invoked only by `tools/etoro_live_write.py` via `bot/risk_authority/preflight.py`. The scanner path runs only `bot/risk.py` (`RiskManager` + `PortfolioRiskPolicy`) — a smaller gate set. Gates currently missing from the scanner path: `broker_daily_loss_cap`, `global_capital`, `combined_exposure`, `drawdown_throttle`, per-symbol concentration, `quote_freshness`, `spread`, `data_staleness`, `etoro_live_flag/env`, the unified `policy_invalid`. Some of these (e.g. `broker_open_positions` from the engine) are different in semantics from the `RISK_MAX_OPEN_POSITIONS` counterpart in `bot/risk.py`. The audit P0-3 patch addresses runtime M13.4A kill-switch enforcement separately; it does not close this gap.
- **Hard pre-requisite for M22 (Semi-Automated Live Trading).** Auto-allowed submissions are not operator-pre-checked per transmission, so the scanner-path gate set must equal-or-exceed the M14 engine's gate set before M22 can begin. **NOT in M17 scope.**
- **Until then:** IBKR live submissions in production must remain operator-supervised; do not enable any unattended automation that bypasses operator review.
- **Reference:** [`docs/M14_FINAL_AUDIT.md` §12](M14_FINAL_AUDIT.md) (full text); MILESTONE_STATUS.md M14 detail section "Known coverage gap"; ROADMAP.md M22 line "Requires M14 engine extension to scanner path."

### scanner_replica real intraday E2E — provider/data blocked (PROPOSED, carry-forward from M17.B)
- **Status:** Not started. Carried forward from M17.B closeout 2026-06-07. M17.B IMPLEMENTATION itself is CLOSED at HEAD `3f1079e` — scanner_replica is operational, parity is proven, and the strict-per-TF gate is verified. The only piece that did NOT complete on the VPS at M17.B acceptance is a real intraday end-to-end run of the `example_scanner_replica_aapl.json` config, because M16 lacks AAPL 4H/1H/15m coverage on the VPS and yfinance rate-limited the intraday backfill attempts. The strict-per-TF gate (Sharpened Rule #3) behaved exactly as specified — exit code 2 with `MissingDataError` referencing the right `bot.historical.cli backfill` command.
- **Recorded VPS state at M17.B acceptance (2026-06-07):**
  - AAPL 1D coverage: present, 11,462 bars clean
  - AAPL 4H coverage: ABSENT
  - AAPL 1H coverage: ABSENT
  - AAPL 15m coverage: ABSENT
  - 1H backfill attempt: `YFRateLimitError`, status `failed`, `rate_limited=1`, `rate_limit_count=6`, exit 1
  - 15m backfill attempt: `YFRateLimitError`, same pattern, exit 1
  - 4H backfill attempt: status ok but `no_data=1` (4H is resampled from 1H at write time; without 1H source, no bars are produced)
  - Legacy `data/bar_cache` / `data/bt_v2_cache` present on VPS but NOT consumed by `bot/backtesting/*` (AST-asserted; falling back would have violated the M17.A "M16 sole data source" architecture decision).
- **Condition to close this entry:**
  - M16 intraday coverage for AAPL exists on the VPS via EITHER:
    - (a) yfinance backfill succeeding without rate-limit failures (likely requires either an off-peak retry window, a different VPS egress IP, or rate-limit-aware backfill pacing — none of which are M17.B-in-scope), OR
    - (b) an alternate provider integrated behind the `bot.historical` `BaseProvider` interface (M6 left this contract in place specifically to make provider addition a one-file change; the cost decision and provider selection are operator's to make).
  - With coverage present, `python -m bot.backtesting.cli run --config configs/backtests/example_scanner_replica_aapl.json` exits **0** on VPS and writes the standard 6 artifacts (`manifest.json`, `report.json`, `trades.csv`, `trades.jsonl`, `equity_curve.csv`, `warnings.json`) under `data/backtests/<timestamp>_scanner_replica_*/`.
  - The M17.B.6 candidate_snapshots replay diagnostic produces K > 0 with `failed=0` (recall: K=0 is an accepted-pass per Sharpened Rule #5; the carry-forward is about supplementing the synthetic equivalence proof with real-bar replay evidence, not about replacing it).
- **What stays explicitly DEFERRED beyond this entry:**
  - Engineering rate-limit-aware yfinance pacing for intraday backfill OR integrating a paid provider — both are separate sub-milestones with their own pre-code Q-checklists.
  - Shorts in scanner_replica (execution layer is long-only).
  - Multi-symbol portfolio backtests, optimisation / parameter sweeps / walk-forward, dashboard backtest UI, retirement of legacy `bot/backtest.py` / `bot/backtest_v2.py`.
- **What MUST NOT be done in response** (hard rules, operator-pinned at M17.B closeout):
  - No code workaround inside `bot/backtesting/*` to fake intraday data.
  - No fallback to `data/bar_cache` / `data/bt_v2_cache`.
  - No weakening of strict-per-TF.
  - No automatic provider switching.
  - No silent partial-mode default.
  - No yfinance retry pattern that masks rate-limit signal without addressing the underlying pacing.
- **Reference:** `docs/M17_B_closeout.md` §5 (honest residual) and §7 (carry-forward). Recorded as part of the M17.B docs-closeout 2026-06-07.

### M18 — Advanced signal scoring + paper-trade automation (2-4 weeks; PROPOSED, AFTER M17 + intraday carry-forward)
- **Status:** Not started. Sequenced after M17 (both M17.A and M17.B are CLOSED 2026-06-07); the intraday carry-forward above is sequencing-adjacent, not blocking. Pre-code Q-style checklist required.
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

### M17.B — scanner_replica + Multi-Timeframe Confluence (CLOSED 2026-06-07)
- **Closing commit / final HEAD:** `3f1079e` (M17.B.7). Full 8-commit chain on `origin/main` from `1b9e3ec` (M17.B.pre-phase) to `3f1079e` — listed in `docs/M17_B_closeout.md` §2 and `MILESTONE_STATUS.md`.
- **VPS evidence (2026-06-07, operator-verified):** HEAD = `3f1079e`; 8 M17.B commits present `f6bf24e..3f1079e`; `bot/data.py` sha = `03f488c73feba19a9088b779722ee53515e936f2` (byte-identical to M17.A baseline, unchanged at every M17.B commit); combined M17 + M16 + audit-P1 regression exit code 0 (expected composition 200 M17 + 70 M16 + 23 audit-P1 = 293 tests OK, skipped=2); M17.B.6 replay diagnostic exit 0 (K-replayed varies per VPS M16 coverage; `failed=0` invariant holds; per Sharpened Rule #5 no equivalence claim is made when K=0); SMA example E2E exit 0 (M17.A baseline reproducibility preserved); dashboard active; caddy active; `/api/health` HTTP 200; `git status` clean.
- **What shipped:** indicator parity helpers (RSI `mode='sma_gain_loss'`, ATR `mode='sma_true_range'`, `vwap_dev`, `bb_pos`); strict multi-TF M16 loader (`load_multi_tf_bars` + `MultiTfBars` with `allow_partial_tfs=False` default); `MultiTimeframeContext` with look-ahead-safe O(log n) snapshot lookup; `ScannerReplicaStrategy` reproducing `bot/scanner.score_timeframe` algebra by code; ATR-based exits opt-in in `ExecutionConfig` (default `stop_mode='pct'` preserves M17.A byte-identically); `candidate_snapshots` replay diagnostic (smoke-only; K=0 accepted-pass); `configs/backtests/example_scanner_replica_aapl.json`; G10 AST forbidden-import list extended with `bot.scanner` / `bot.strategy` / `bot.feature_engine` / `bot.indicators` / `bot.sentiment` / `bot.flywheel`; `_M17_A_BASELINE_FORBIDDEN` regression asserts M17.A baseline preserved.
- **Hard-constraint evidence:** 0/20 protected files modified; `bot/data.py` byte-identical; AST-clean (no live scanner/strategy/indicators/feature_engine/sentiment/flywheel imports inside `bot/backtesting/*`); only `data_loader.py` imports `bot.historical`; legacy `data/bar_cache` / `data/bt_v2_cache` NOT consumed; no order-method string literals; no sockets during runtime; no new dependencies; no `.env` / service / generated-data changes; `bk.ENGINE_VERSION == 'M17.A.1'` unchanged (Sharpened Rule #2 honoured).
- **Authoritative operator reference:** [`M17_B_closeout.md`](M17_B_closeout.md).
- **Honest residual** (recorded in `M17_B_closeout.md` §5 + Active carry-forward above): real intraday end-to-end on VPS NOT verified at M17.B acceptance. M16 lacks AAPL 4H/1H/15m coverage on the VPS; yfinance rate-limited intraday backfill attempts (`YFRateLimitError`, `rate_limited=1`, `rate_limit_count=6`, exit 1 on 1H and 15m); 4H wrote no data because its 1H source is missing. Strict-per-TF gate fired correctly with exit code 2 + `MissingDataError` referencing the right backfill command. Equivalence is proven by synthetic per-rule parity (G3_IndicatorParity, G4_ScannerReplicaScoringParity, G4_ScannerReplicaConfluenceScaler, G4_ScannerReplicaIntegration); real-bar replay against `candidate_snapshots` is a carry-forward Active entry, not a blocker. NO code workaround, NO fallback to legacy caches, NO weakening of strict-per-TF was done in response.

### M17.A — Backtesting Engine Foundation (CLOSED 2026-06-07)
- **Closing commit / final HEAD:** `a05f160` (M17.A.fixup5). Full 14-commit chain on `origin/main` from `5b37194` (M17.A.1) to `a05f160` — listed in `docs/M17_A_closeout.md` §2 and `MILESTONE_STATUS.md`.
- **VPS evidence (2026-06-07, operator-verified):** HEAD = `a05f160`; M16 + audit-P1 + M17 combined regression `Ran 233 tests in 99.798s — OK (skipped=1)`; example backtest exit 0 with run dir `data/backtests/20260607T011518Z_sma_crossover_88578b71038d` and all 6 artifacts; `manifest.json` contains `"bot_historical_schema_version": 2`; `warnings.json = []`; `bot/data.py` sha = `03f488c73feba19a9088b779722ee53515e936f2`; dashboard active; caddy active; `/api/health` HTTP 200; `git status` clean.
- **What shipped:** new package `bot/backtesting/` (foundation only) — single-symbol M16-only backtest engine with SMA crossover strategy, strict missing-data semantics, 7-day non-trading-day boundary tolerance, next-open execution + intrabar SL/TP, fees + slippage on round-trip, deterministic filesystem artifacts (manifest/report/CSV/JSONL/equity/warnings), `python -m bot.backtesting.cli` with exit codes 0/2/3/1, 140 tests across G1..G10 including AST/no-network/protected-files G10 hygiene.
- **Hard-constraint evidence:** 0/20 protected files modified; `bot/data.py` byte-identical; AST-clean (no yfinance / `bot.data` / scanner / broker / eToro / risk-engine / network imports anywhere in `bot/backtesting/*`); only `data_loader.py` imports `bot.historical`; no order-method string literals; no sockets during runtime; `data/backtests/` git-ignored; no new dependencies.
- **Authoritative operator reference:** [`M17_A_closeout.md`](M17_A_closeout.md).
- **Carry-forward to M17.B** (active entry above): `scanner_replica` + multi-timeframe (1D/4H/1H/15m) confluence + indicator parity test vs `bot.indicators.compute()` + live-vs-backtest equivalence on real `candidate_snapshots` rows + ATR-based exits.
- **Honest residuals** (recorded in `M17_A_closeout.md` §9):
  - Example config dates were aligned to confirmed trading days (`2024-01-02..2024-12-31`); the boundary-tolerance path is exercised by G2 unit tests against mocked M16 fixtures, not by the live example.
  - `test_m13_5_reconcile` and `test_m14_risk` errors under `unittest discover` are pre-existing (broken at M17 baseline, same at M17.A acceptance); both pass standalone. Out of M17.A scope; flagged for a future audit pass.

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
