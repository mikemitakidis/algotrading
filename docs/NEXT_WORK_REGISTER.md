# Next Work Register

Carry-forward items that are **NOT** being done in the current milestone but **must not be lost** when the chat compacts or context resets.

This file is updated by every milestone closeout. Each item has: **status, why deferred, acceptance criteria, links** — and once shipped, the **closing commit hash**.

> **Rule:** anything explicitly deferred from a milestone discussion goes here within the same commit that defers it. The register is part of the repo so it survives any chat reset.

---

## Active items

### M15.3.A.cutover — Switch dashboard to 127.0.0.1 + Caddy/TLS (DEFERRED, operator action)
- **Status:** Soft-cutover landed in M15.3.A. Default bind remains `0.0.0.0` with an explicit startup warning. The operator must complete the hard cutover.
- **TOTP does not substitute for HTTPS** (added 2026-06-04 alongside M15.3.A.2 closeout-prep). Even with `M15.3.A.2` TOTP enabled, an on-path attacker over plain HTTP can still steal a valid session cookie *after* a successful 2FA login (the cookie is plaintext in network bytes without TLS — `Secure` flag is off pre-cutover by design, since browsers would otherwise refuse to send it). TOTP defends against credential theft alone; it does not defend against active MITM / session theft. This cutover therefore remains a real prerequisite for state-changing operator actions like `M15.3.B` manual_reset.
- **Why deferred:** Required Caddy install on the Hetzner VPS, which is an operator action (root-level package install) outside the M15.3.A code scope. M15.3.A intentionally does not install Caddy automatically — see Q-A.3 / correction #3.
- **Acceptance criteria when complete:**
  - Caddy (or other HTTPS reverse-proxy) is installed and reverse-proxying `:443 → 127.0.0.1:8080`.
  - `/opt/algo-trader/.env` sets `DASHBOARD_BIND_HOST=127.0.0.1` and `DASHBOARD_HTTPS_MODE=true` (or `DASHBOARD_COOKIE_SECURE=true`).
  - `algo-trader-dashboard.service` restarted; dashboard reachable only through HTTPS via the reverse proxy.
  - `curl -s http://<external-ip>:8080/api/health` from outside the VPS returns connection refused.
  - The M15.3.A startup "exposed on plaintext" warning is no longer present in the journal.
- **Estimated effort:** 1-2 hours of operator action, no code changes. Procedure documented in `docs/M15_3_A_dashboard_auth.md` §3.
- **Owner:** operator (Mike).

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

### M15.3.A.2 — Dashboard TOTP / Google Authenticator 2FA (CODE COMPLETE — PENDING VPS VERIFICATION)
- **Status:** Code shipped 2026-06-04; awaiting VPS verification. Pre-code checklist Q-A.1..Q-A.11 + Corrections 1-9 all honoured. Implementation summary:
  - New module `dashboard/auth/totp.py` (TOTP primitives + replay cache, dependency-injectable clock & secret).
  - `/api/login` extended in `dashboard/app.py` with a second-factor block between password verify and session rotation. Byte-identical to M15.3.A when `DASHBOARD_TOTP_SECRET` is unset (regression-tested as G2 hard guarantee).
  - `tools/set_dashboard_password.py` gained `--enable-totp` (generates secret, renders terminal QR, verifies first code before writing `.env`) and `--disable-totp` (best-effort audit; never blocks on broken DB — recovery path).
  - Login form gained an always-visible TOTP field; JS handles the `totp_required` response with an orange-outline focus on the TOTP input.
  - `auth_events` ALLOWED_KINDS extended with 5 new closed values: `totp_success`, `totp_failure`, `totp_required_not_provided`, `totp_setup`, `totp_disabled`. No schema migration (kind enforcement is code-side).
  - Replay cache stores `(sha256(secret)[:16], time_step)` only — never raw codes or secrets. TTL 120 sec.
  - Test surface: 51/51 in `test_m15_3_a_2_totp.py` across 8 groups (primitives, disabled-mode hard guarantee, enabled-mode flows, setpw flags, rate-limit integration, replay prevention, auth events without secret material, AST scan + protected files).
  - Clean-venv proof: `pyotp==2.9.0` + `qrcode==7.4.2` install/check/import all exit 0.
  - Hard constraints: 0/24 protected files modified vs `648682c`.
- **Next step:** operator VPS verification (see runbook §12 in `docs/M15_3_A_dashboard_auth.md`); closeout to follow per Correction 9 (`MILESTONE_STATUS.md` only at closeout).
- **Honest trade-offs documented in runbook §12.6:** TOTP does not substitute for HTTPS; `totp_required` is a small password-validity oracle (rate-limit-capped); in-memory replay cache resets on restart.
- **Reference:** Mike's request 2026-06-04 + M15.3.A.2 pre-code checklist approval.

### M15.3.B — manual_reset operator flow (PENDING M15.3 SEQUENCE — blocked on M15.3.A.2 AND HTTPS decision)
- **Status:** Plan approved (in M15.3 plan), scheduled after M15.3.A.2 (TOTP 2FA) **and** an explicit HTTPS-exposure decision per Correction 1.
- **Blockers (both must clear before coding starts):**
  1. M15.3.A.2 VPS-verified + closed.
  2. Either `M15.3.A.cutover` (Caddy/TLS) is complete, **or** the operator records an explicit acceptance that `manual_reset` will be exposed on plain HTTP. Reason: `manual_reset` clears engine state; without HTTPS, a network attacker who observes a valid 2FA login can hijack the session cookie and invoke it. TOTP protects credential theft, not session theft.
- **Why deferred:** Sequenced after M15.3.A because B uses A's CSRF + auth primitives. Now further sequenced after M15.3.A.2 because `manual_reset` is the kind of state-clearing operator action that 2FA materially protects.
- **Acceptance criteria:** see §5 of the M15.3 plan and the M15.3 closeout discussion.

### M15.3.C — Compliance audit + export (PENDING M15.3 SEQUENCE)
- **Status:** Plan approved (in M15.3 plan), scheduled after M15.3.B.
- **Why deferred:** Sequenced last because C reads B's `manual_reset_audit` and A's `auth_events`.
- **Acceptance criteria:** see §6 of the M15.3 plan.

### M15.3.D or later — Multi-user / read-only dashboard roles (PROPOSED, NOT URGENT)
- **Status:** Not started. Recorded 2026-06-04 to ensure the idea isn't lost. Explicitly NOT scheduled for the immediate next milestone — the single-operator model continues until there is a concrete need for a second human or for a read-only viewer (e.g. an external compliance reader, a non-technical co-operator).
- **Why proposed:** The current dashboard authenticates a single operator with one password and grants full read/write. Once 2FA (M15.3.A.2) and `manual_reset` (M15.3.B) ship, the natural next axis of access-control is splitting the principal into roles. Two roles cover ~95% of realistic needs: `operator` (current behaviour: full read + state-changing actions) and `viewer` (read-only — sees the dashboard, signals, exposure, audit log, but cannot POST to any state-changing endpoint).
- **Why deferred:** No second human is involved in the current operation. Multi-user adds attack surface (user-management endpoints, role-storage table, role-check decorator on every endpoint) for zero current benefit. Better to keep the surface tight until there's a genuine use case.
- **Acceptance criteria when undeferred (sketch — to be expanded into a pre-code checklist at the time):**
  - New `dashboard_users` SQLite table (or equivalent in `.env`/`users.json`) with `username`, `password_hash`, `role`, `totp_secret_optional`, `created_at`. Single-row legacy mode preserved as a fallback when the table is empty (operator-only).
  - `role` is a closed set: at least `operator` and `viewer`. No free-form roles.
  - New `@require_role(<role>)` decorator. `@require_auth` continues to mean "authenticated"; `@require_role("operator")` is required on every state-changing endpoint. All viewer-safe GETs stay on `@require_auth` only.
  - `tools/set_dashboard_password.py` extended with `--user <name> --role <operator|viewer>` for managing multiple users; default behaviour without these flags continues to manage the single legacy operator.
  - `auth_events` gains a `username` column (additive migration) — or, more conservatively, the `extras_json` field carries it. Decision deferred to the pre-code checklist.
  - Test surface: role-based access matrix (operator can POST `/api/kill-switch/activate`; viewer cannot; both can GET `/api/health`), session-cookie correctly identifies the user, CSRF still enforced per-role.
  - Hard constraints unchanged: no orders, no broker writes, no live mode, no scanner/strategy/M14 engine/eToro changes.
- **Estimated effort:** ~500-700 LOC including tests + docs. Self-contained milestone.
- **Why "or later" in the name:** the sequencing is intentionally loose — this could land as M15.3.D, or as a deferred future-work item that gets a fresh number after M15.3.C closes, depending on whether the use case has materialised by then.
- **Reference:** Mike's request 2026-06-04 at M15.3.A closeout. Explicit instruction: "do not implement multi-user now."

### M16+ — Intelligence / self-learning roadmap items (NOT STARTED)
Per `ROADMAP.md`, M16+ begins after M15 closes (i.e. after M15.3.C ships). Items listed in the project roadmap include:

- News/sentiment module
- ML pipeline expansion beyond M9 baseline
- Broker execution architecture for live (vs. paper) flows
- IBKR live trading wiring
- eToro live integration / manual bridge if needed
- Portfolio/risk layer enrichment
- Production hardening (M15 itself — closes when M15.3 completes)

**Why deferred:** Project rule — "Do not move to the next milestone until the current one is verified." M15.3.A → B → C must ship and verify before any M16 work starts.

**Acceptance criteria when each is undeferred:** per its line in `ROADMAP.md`. Each gets its own M16.x sub-milestone plan when authorized.

---

## Closed items

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
