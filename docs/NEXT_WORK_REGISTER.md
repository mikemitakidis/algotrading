# Next Work Register

Carry-forward items that are **NOT** being done in the current milestone but **must not be lost** when the chat compacts or context resets.

This file is updated by every milestone closeout. Each item has: **status, why deferred, acceptance criteria, links** — and once shipped, the **closing commit hash**.

> **Rule:** anything explicitly deferred from a milestone discussion goes here within the same commit that defers it. The register is part of the repo so it survives any chat reset.

---

## Active items

### M15.3.A.cutover — Switch dashboard to 127.0.0.1 + Caddy/TLS (DEFERRED, operator action)
- **Status:** Soft-cutover landed in M15.3.A. Default bind remains `0.0.0.0` with an explicit startup warning. The operator must complete the hard cutover.
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

### M15.3.A.2 — Dashboard TOTP / Google Authenticator 2FA (PROPOSED, before M15.3.B)
- **Status:** Not started. Approved as a recommendation; awaiting explicit go-ahead. Sequenced **before M15.3.B** because `manual_reset` is sensitive and benefits from 2FA more than a basic password+CSRF gate provides.
- **Why proposed (and why TOTP not email codes):**
  - No SMTP secrets / provider lock-in / deliverability risk during an incident
  - Phishing-resistant against credential theft (the realistic threat for a single-operator system)
  - Slots cleanly into the existing M15.3.A stack (rate-limit + bcrypt + audit + CSRF)
  - Doesn't depend on the Caddy/HTTPS cutover (works over plain HTTP today)
  - WebAuthn/passkeys would be stronger but require HTTPS first — TOTP is the right step now
- **Why before M15.3.B (manual_reset):** `manual_reset` will let the operator clear engine state. An attacker with a stolen password but no TOTP code should NOT be able to invoke it. Password+CSRF alone is acceptable for read-only ops; 2FA is the floor for state-clearing operator actions.
- **Acceptance criteria when undeferred:**
  - New deps pinned: `pyotp==2.9.0` and `qrcode==7.4.2` (both small, no transitive issues; verified in a clean venv before commit).
  - Env: `DASHBOARD_TOTP_SECRET` (base32). Optional — when unset, login flow is unchanged (transitional grace, same pattern as plaintext password fallback).
  - `/api/login` accepts an optional `totp_code` field; when `DASHBOARD_TOTP_SECRET` is set, the code is REQUIRED. Replay attacks blocked by pyotp's time-window logic.
  - New `GET /api/auth/totp-setup` returns the QR-code SVG/PNG inline (operator scans into Google Authenticator / Authy / 1Password / Bitwarden — never emailed).
  - `tools/set_dashboard_password.py` extended with `--enable-totp` and `--disable-totp` flags; QR code never logged; secret rotates on `--enable-totp`.
  - `auth_events` gains new closed kinds: `totp_failure`, `totp_setup`, `totp_disabled`. Schema unchanged (only the validator in `audit.py` updated).
  - Test suite: enable/disable transitions, code-replay rejection (same code used twice within window fails second attempt), audit row writes, rate-limiter integration (5 wrong TOTP codes → same lockout policy as wrong password).
  - Runbook section added to `docs/M15_3_A_dashboard_auth.md` covering: setup flow, QR-code display, lost-device recovery via `--disable-totp` over SSH.
- **Estimated effort:** ~400-500 LOC including tests + docs. Standalone milestone. No engine, no broker, no scanner/strategy, no eToro, no IBKR changes.
- **Owner:** TBD (proposed).
- **Reference:** Mike's request 2026-06-04 + Claude's agreement to sequence before M15.3.B.


### M15.3.B — manual_reset operator flow (PENDING M15.3 SEQUENCE — now blocked on M15.3.A.2)
- **Status:** Plan approved (in M15.3 plan), scheduled after M15.3.A.2 (TOTP 2FA).
- **Why deferred:** Sequenced after M15.3.A because B uses A's CSRF + auth primitives. Now further sequenced after M15.3.A.2 because `manual_reset` is the kind of state-clearing operator action that 2FA materially protects.
- **Acceptance criteria:** see §5 of the M15.3 plan and the M15.3 closeout discussion.

### M15.3.C — Compliance audit + export (PENDING M15.3 SEQUENCE)
- **Status:** Plan approved (in M15.3 plan), scheduled after M15.3.B.
- **Why deferred:** Sequenced last because C reads B's `manual_reset_audit` and A's `auth_events`.
- **Acceptance criteria:** see §6 of the M15.3 plan.

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

*(items move here with their closing commit hash when their acceptance criteria are met)*
