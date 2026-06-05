# M14 — Risk Authority Layer: Final Audit

**Status:** ✅ M14 CLOSED (A through H)
**Closeout commit:** `db7d9f5` (M14.H — docs-only closeout audit)
**Closeout date:** 2026-06-02 (UTC)

This is the **authoritative closeout document** for M14 (Portfolio / Risk Layer). For the live state of every milestone across the project, see [`MILESTONE_STATUS.md`](../MILESTONE_STATUS.md). The historical reconciliation context lives in [`PROJECT_STATUS_RECONCILIATION.md`](PROJECT_STATUS_RECONCILIATION.md) and is preserved as a record of how the project's status was understood at earlier points; this document supersedes it for **M14-specific** status from 2026-06-02 onward.

---

## §1 — Executive summary

M14 built the Risk Authority layer in nine commits across seven scoped sub-milestones (M14.A → M14.G), closed out by M14.H (this document). The layer is **read-only at every operator-facing surface** and **fail-closed at every decision gate**. Live writes still flow only through the M13.5.B operator CLI (`tools/etoro_live_write.py`), which since M14.F consults the engine before any transport, env-flag check, nonce mint, or broker construction. No real-money order was placed during M14, and no scanner-to-live shortcut was introduced. M14 is **safe to mark closed**. The recommended next milestone is **M15.0 — scanner / systemd reliability and production process clarity**, before any M16+ intelligence work.

---

## §2 — M14 by the numbers

| Metric | Value |
|---|---|
| Sub-milestones closed | **7** (A, B, C, D, E, F, G) + H closeout |
| Commits on `main` | **9** (`3f4448e`, `42ee08c`, `e569d43`, `d9c53eb`, `7aa1082`, `729ad2d`, `ace0fda`, `2e20b52`, `71e893a`) |
| Lines added across M14 | **~12,321** (sum of insertion counts) |
| New modules under `bot/risk_authority/` | **17** Python files (excluding `__init__.py`) |
| Engine gates | **25** in fixed order, first-failure-wins |
| Closed-set reason codes | **31** (each with a documented recovery path) |
| Read-only dashboard endpoints added | **4** (`/api/risk-authority/{decisions,scopes,snapshot/latest,authority}`) |
| Live-write protections added (defense in depth) | **10 layers** (see §6) |
| M14 sub-milestone tests | **324** (B 27, C 47, D 60, E 105, F 34, G 51) |
| Full regression at closeout | **~1,041 tests** (M14 + M13.x + M15.x + standalone) — all green |
| Real-money orders placed during M14 | **0** |
| Bypasses around Risk Authority | **0** |

---

## §3 — What each sub-milestone delivered

### M14.A — Design (commit `3f4448e`) ✅
- **Scope:** Risk Intelligence Layer design document.
- **Files:** [`docs/M14_A_design.md`](M14_A_design.md) (503 lines).
- **Tests:** none — design phase.
- **VPS proof:** N/A (docs-only).
- **Status:** CLOSED.

### M14.B — Additive schema + migration (commit `42ee08c`) ✅
- **Scope:** Add `daily_state_per_broker`, `risk_snapshots`, `risk_decisions`, `broker_positions` tables without touching the legacy `daily_state` / `portfolio_risk_snapshots` tables.
- **Files:** `bot/risk_authority/state.py`, `bot/risk_authority/reading.py`, schema additions inside `bot/flywheel.py`.
- **Tests:** [`test_m14_b_schema.py`](../test_m14_b_schema.py) — **27/27**.
- **VPS proof:** schema migration ran clean on production DB; legacy tables byte-identical post-migration.
- **Status:** CLOSED.

### M14.C — Realised-PnL ingestion adapters (commits `e569d43`, correction `d9c53eb`) ✅
- **Scope:** Read-only PnL ingestion adapters for IBKR + eToro; correctly classifies "unknown" so the engine can fail closed; CLI `tools/ingest_risk_state.py`.
- **Files:** `bot/risk_authority/ingest.py`, `ingest_etoro.py`, `ingest_ibkr.py`, `ingest_audit.py`.
- **Correction (`d9c53eb`):** **ANY missing or non-numeric same-day PnL field → status `unknown`**, never silently zero. Known-zero vs unknown-zero distinction begins here.
- **Tests:** [`test_m14_c_ingest.py`](../test_m14_c_ingest.py) — **47/47**.
- **VPS proof:** dry-run ingest produced `unknown` on the VPS (eToro keys absent — fail-closed as designed).
- **Status:** CLOSED.

### M14.D — Exposure / position / capital engine (commits `7aa1082`, bugfix `729ad2d`) ✅
- **Scope:** Read-only exposure adapters for IBKR + eToro; append-only `broker_positions` batch schema; cross-engine separation of M14.C and M14.D owned columns; CLI `tools/ingest_exposure_state.py`.
- **Files:** `bot/risk_authority/exposure_reading.py`, `ingest_exposure.py`, `ingest_etoro_exposure.py`, `ingest_ibkr_exposure.py`.
- **Bugfix (`729ad2d`):** exposure CLI imports the shipped audit factory (resolves an import-time crash).
- **Tests:** [`test_m14_d_exposure.py`](../test_m14_d_exposure.py) — **60/60**.
- **VPS proof:** exposure ingest dry-run produced `exposure_unknown` for IBKR (positions reader not wired) and eToro (keys absent). Cross-engine separation invariant verified: M14.C ingestion leaves M14.D-owned columns byte-identical and vice versa.
- **Status:** CLOSED.

### M14.E — Risk Authority Engine + downgrade-only Governor (commit `ace0fda`) ✅
- **Scope:** Pure `decide()` core, 25 gates, downgrade-only governor, M13.4A broker-allocation policy bridge, `decide_and_audit` thin wrapper as the **only** DB-writing surface.
- **Files:** `bot/risk_authority/authority.py`, `engine.py`, `governor.py`, `snapshot.py`, `audit_decisions.py`.
- **Tests:** [`test_m14_e_engine.py`](../test_m14_e_engine.py) — **105/105**, including a **1000-sequence property test** proving the governor never auto-upgrades authority.
- **VPS proof:** policy loaded from broker-allocation source; snapshot loaded with all four scopes; `decide()` ran successfully; result = block; `authority_before = AUTO_ALLOWED`, `authority_after = SIGNAL_ONLY`; reason `('global_auto_disabled',)`. Engine correctly consumed dashboard-set policy and failed closed when global automation was disabled.
- **Status:** CLOSED.

### M14.F — eToro live-write preflight integration (commit `2e20b52`) ✅
- **Scope:** Wire the M13.5.B operator CLI to the engine. Every live-write attempt now runs `run_risk_preflight()` **before** transport, env-flag check, nonce mint, schema validate, or broker construction. New exit code `4` for "Risk Authority blocked".
- **Files:** `bot/risk_authority/preflight.py` (new), `tools/etoro_live_write.py` (surgical edit: +83/−0 lines, preflight call + `--authority` argparse arg).
- **Tests:** [`test_m14_f_preflight.py`](../test_m14_f_preflight.py) — **34/34**, including AST ordering tests (preflight call appears before `_read_keys`, env-flag check, broker construction, nonce issuance) and a subprocess end-to-end test (forced block → exit 4 → audit log not even created).
- **VPS proof:** HEAD includes `2e20b52`; tests passed 34/34; no `live_post` lines found in any audit log; protected files unchanged; dashboard `/api/health` returned HTTP 200.
- **Status:** CLOSED.

### M14.G — Read-only Risk Authority dashboard (commit `71e893a`) ✅
- **Scope:** Four GET-only API endpoints + a Risk Authority tab in the dashboard. Read-only end-to-end. `manual_reset` stays design-only — no endpoint, no button, no authority editing.
- **Files:** `bot/risk_authority/dashboard_read.py` (new, 360 lines), `dashboard/app.py` (additive, +316/−0).
- **Tests:** [`test_m14_g_dashboard.py`](../test_m14_g_dashboard.py) — **51/51**, including an AST + runtime probe proving the four endpoints are SELECT-only (a `NOT-WRITE` connection wrapper raises on any non-SELECT statement; all four helpers pass through it).
- **VPS proof:** HEAD is `71e893a`; tests passed 51/51; four routes registered; Risk Authority tab present (nav + page + JS); no manual reset button detected; protected files unchanged; dashboard `/api/health` returned HTTP 200.
- **Status:** CLOSED.

### M14.H — Closeout / audit (this document) ✅
- **Scope:** Final M14 audit artifact. Documentation only.
- **Files:** `docs/M14_FINAL_AUDIT.md` (new), `MILESTONE_STATUS.md` (edited), `ROADMAP.md` (edited).
- **Status:** CLOSED.

---

## §4 — File-to-milestone map

The complete inventory of M14-owned files. Future-Mike: open this table to answer "which milestone owns this file?" without grep.

| File | Milestone | Role |
|---|---|---|
| `bot/risk_authority/state.py` | M14.B | flywheel-table schema/migration compat shim |
| `bot/risk_authority/reading.py` | M14.B | read-only DAO for `daily_state_per_broker` |
| `bot/risk_authority/ingest.py` | M14.C | unified PnL ingestion orchestrator |
| `bot/risk_authority/ingest_etoro.py` | M14.C | eToro realised-PnL adapter |
| `bot/risk_authority/ingest_ibkr.py` | M14.C | IBKR realised-PnL adapter |
| `bot/risk_authority/ingest_audit.py` | M14.C | ingestion event audit logger |
| `bot/risk_authority/exposure_reading.py` | M14.D | exposure DAO |
| `bot/risk_authority/ingest_exposure.py` | M14.D | exposure ingestion orchestrator |
| `bot/risk_authority/ingest_etoro_exposure.py` | M14.D | eToro exposure adapter |
| `bot/risk_authority/ingest_ibkr_exposure.py` | M14.D | IBKR exposure adapter (`NotImplementedError` stub — fail-closed) |
| `bot/risk_authority/authority.py` | M14.E | Authority ladder + `is_monotone_safe` + `REQUIRED_AUTHORITY` |
| `bot/risk_authority/engine.py` | M14.E | pure `decide()` + 25 gates + M13.4A policy bridge |
| `bot/risk_authority/governor.py` | M14.E | downgrade-only governor state machine |
| `bot/risk_authority/snapshot.py` | M14.E | read-only `assemble_snapshot` |
| `bot/risk_authority/audit_decisions.py` | M14.E | `decide_and_audit` — the **only** DB-writing surface |
| `bot/risk_authority/preflight.py` | M14.F | `run_risk_preflight` bridge (operator CLI ↔ engine) |
| `tools/etoro_live_write.py` (edit) | M14.F | preflight call inserted before transport |
| `bot/risk_authority/dashboard_read.py` | M14.G | four read-only query helpers |
| `dashboard/app.py` (edit) | M14.G | four GET routes + Risk Authority tab |

Schema additions in `bot/flywheel.py` (commit `42ee08c`): `daily_state_per_broker`, `risk_snapshots`, `risk_decisions`, `broker_positions` — all additive, never destructive.

---

## §5 — Test-to-milestone map

| Test file | Milestone | Count | Highlights |
|---|---|---|---|
| [`test_m14_b_schema.py`](../test_m14_b_schema.py) | M14.B | 27 | additive migration; legacy tables unchanged |
| [`test_m14_c_ingest.py`](../test_m14_c_ingest.py) | M14.C | 47 | fail-closed on missing/NaN PnL; known-zero distinguished |
| [`test_m14_d_exposure.py`](../test_m14_d_exposure.py) | M14.D | 60 | cross-engine column isolation; append-only batch invariant |
| [`test_m14_e_engine.py`](../test_m14_e_engine.py) | M14.E | 105 | 25-gate ordering; 1000-seq governor monotone property; M13.4A policy bridge |
| [`test_m14_f_preflight.py`](../test_m14_f_preflight.py) | M14.F | 34 | AST ordering (preflight runs first); subprocess end-to-end block → exit 4 |
| [`test_m14_g_dashboard.py`](../test_m14_g_dashboard.py) | M14.G | 51 | AST + runtime NOT-WRITE probe; 405 on POST/DELETE/PUT/PATCH |
| **M14 sub-milestone total** | | **324** | |
| `test_m14_risk.py` (predates split) | carry-forward | 39 | older end-to-end smoke battery |

---

## §6 — Live-write protections now in place (defense in depth)

In order from outermost (operator-facing) to innermost (transport):

1. **Scanner isolation invariant.** Importing `bot.scanner`/`bot.strategy`/`bot.risk`/`bot.brokers` does **not** load `tools.etoro_live_write`, `bot.etoro.live_broker`, `bot.risk_authority.preflight`, `bot.risk_authority.dashboard_read`, or any M14 engine module. Subprocess-tested in every milestone since M13.5.
2. **Argparse forbidden flags.** `--base-url`, `--override-*`, `--assume-yes` not accepted; `--demo` accepted but disabled (fail-closed).
3. **M14.F Risk Authority preflight (NEW).** `run_risk_preflight()` runs **before** anything else in `cmd_oneshot`. On block, the CLI exits **4** and writes a `risk_decisions` audit row. The audit log file used by `EtoroLiveBroker`'s `AuditLogger` is not even created, because the broker is never constructed.
4. **`ETORO_LIVE_ENABLED` env flag (M13.5.B).** Required `true` in `.env`. The engine's gate #8 (`etoro_live_env_disabled`) mirrors this for defense in depth — both gates fire on the same env var.
5. **`--authority` operator declaration (M14.F).** Operator types the level per invocation (`OFF` / `SIGNAL_ONLY` / `PAPER_ONLY` / `ONE_SHOT_MANUAL` / `AUTO_ALLOWED`). Passing `AUTO_ALLOWED` does **not** bypass any engine gate.
6. **`_read_keys()` credential discipline (M13.5.B).** No demo→real fallback; demo mode never returns the real API URL.
7. **Schema validator (M13.5.B).** Exact payload shape required; rejects anything off-schema.
8. **Per-payload nonce mint (M13.5.B).** Single-use, must be echoed verbatim by the operator.
9. **Operator confirmation prompt (M13.5.B).** Operator types `CONFIRM <nonce>` before any HTTP write.
10. **`EtoroLiveBroker` constructed in exactly one site** (`tools/etoro_live_write.py`). AST-verified in every milestone since M13.5.

The dashboard is **not** part of this stack — it has zero live-write paths. M14.G's read-only contract is AST + runtime probe enforced.

---

## §7 — What the Risk Authority blocks today

The engine walks 25 gates in fixed order, **first-failure-wins**. Every block emits a stable reason code from a closed set of 31, with a documented recovery path.

### Kill switches (immediate hard block)
- `global_kill` — from M13.4A `policy.global.kill_switch`. Recovery: operator clears the switch via `manual_reset` (NOT implemented in M14; design-only).
- `broker_kill` — from M13.4A per-family `policy.{ibkr,etoro}.kill_switch`. Same recovery path.

### Auto-trading disabled
- `global_auto_disabled` — `policy.global.auto_trading_enabled = False`.
- `broker_auto_disabled` — per-family flag.

### Broker-scope guards
- `broker_not_allowed` — scope not in `policy.routing.allowed_brokers`.
- `etoro_live_flag_disabled` — `policy.routing.etoro_live_enabled = False`.
- `etoro_live_env_disabled` — `ETORO_LIVE_ENABLED` not set in env. Mirrors M13.5 envelope.

### Authority ladder
- `authority_too_low` — caller's authority below `REQUIRED_AUTHORITY[action]`.

### Request validation
- `amount_invalid`, `amount_below_min`, `single_trade_cap_exceeded`.

### Capital / exposure caps
- `broker_capital_cap_exceeded` — per-scope `broker_capital_cap_usd`.
- `global_capital_cap_exceeded` — `RISK_GLOBAL_CAPITAL_CAP_USD` / `policy.global.max_auto_trading_capital`.
- `combined_exposure_cap_exceeded` — combined across **all four scopes** (including disallowed brokers).
- `combined_exposure_unknown` — fail-closed when any scope is unknown.

### Position caps
- `broker_open_positions_exceeded`, `global_open_positions_exceeded`, `global_open_positions_unknown`.

### PnL gates (fail-closed on unknown)
- `broker_daily_loss_exceeded`, `daily_pnl_unknown`, `global_daily_loss_exceeded`, `global_daily_loss_unknown`.

### Exposure gates (fail-closed on unknown)
- `exposure_unknown`, `exposure_stale`.

### Concentration
- `concentration_cap_exceeded` — per-symbol exposure cap **cross-aggregated across all four scopes**.

### Drawdown
- `drawdown_throttle_hit`.

### Market gates
- `market_closed`, `quote_stale`, `spread_too_wide`.

### Carry-forward latch
- `daily_loss_block_active` — once tripped, the latch persists for the rest of the UTC trading day.

### Four hard corrections (enforced everywhere)
- **Combined-exposure cap covers all four scopes** including disallowed brokers. Disallowed-broker exposure still counts toward the combined number.
- **Per-symbol concentration cross-aggregates** across all four scopes.
- **UTC trading day** keys every day-latch gate (`broker_daily_loss_exceeded`, `global_daily_loss_exceeded`, `daily_loss_block_active`). No local-time drift.
- **Known-zero ≠ unknown-zero** at every consumer site. The engine consults `ScopeView.is_pnl_known()` / `is_exposure_known()`, never raw numeric columns. The dashboard echoes the same distinction via explicit `pnl_known_zero` / `exposure_known_zero` booleans.

---

## §8 — What is deliberately NOT done in M14

These are tracked open items. Each has a documented home in a later milestone.

- **IBKR exposure reader not yet wired to Gateway.** `ingest_ibkr_exposure.py` is a `NotImplementedError` stub. The engine returns `exposure_unknown` for `ibkr_paper` / `ibkr_live` when no exposure has been ingested — fail-closed by design. Wiring depends on Gateway reliability work and is **M15.x** territory.
- **eToro keys absent on VPS.** Confirmed by every VPS verification since M14.C — `ETORO_LIVE_ENABLED` is not set, and `ETORO_REAL_API_KEY` / `ETORO_REAL_USER_KEY` are not in env. Without keys, the M13.5 envelope cannot be exercised end-to-end. First funded order is a separate gated event (**M21**).
- **First funded eToro order is still a later event.** M14 deliberately stopped before any real-money write. The engine + preflight + audit trail are ready; the operator hand-pulled trigger is the only thing missing, and it's intentional. Tracked as **M21**.
- **Dashboard security hardening remains M15.3.** The dashboard is accessed via `http://138.199.196.95:8080/` in production. Current `@require_auth` is session-based; M14.G inherits this access model truthfully (no claim of localhost-only was made). Auth model tightening, TLS, IP allowlisting → **M15.3**.
- **`manual_reset` remains design-only.** The audit vocabulary exists (`audit_decisions.write_decision` accepts `source='manual_reset'`), and `run_risk_preflight` explicitly rejects it (M14.F). No UI or API can issue one. A future milestone may add it as a separately gated, operator-token-protected, double-confirmation action. **Not in M15.0**; candidate for **M15.3** or later.
- **Production process / systemd / IB Gateway reliability remains M15.** Specifically: scanner systemd unit-name mismatch (the three known unit names — `algo-trader`, `scanner`, `algo-scanner` — all report `inactive` despite the bot demonstrably running with `/api/health` 200 and a fresh heartbeat). The actual process manager / unit name needs identifying or documenting. This undermines confidence in milestone-acceptance signals and should be fixed before anything else. **M15.0 priority.**
- **No persistent governor state.** M14.G reports per-scope authority from the latest `risk_decisions` row (read-only derivation). Persistent `GovernorState` across runs awaits the `manual_reset` story.

---

## §9 — Is M14 safe to mark closed?

**Yes**, with all four explicit conditions satisfied:

1. **Every sub-milestone's VPS verification passed.** Receipts captured in §3.
2. **No operator surface can write to a broker without the engine's consent.** Proven by M14.F's 9 proof points (AST ordering + subprocess end-to-end block → exit 4 + audit log not created on block) and M14.G's read-only contract (AST scan + runtime NOT-WRITE probe + 405 on every POST/DELETE/PUT/PATCH).
3. **Every operator-facing surface added in M14 is read-only or audit-only.** Dashboard reads; CLI writes one `risk_snapshots` + one `risk_decisions` row per invocation via the engine; engine itself never writes.
4. **Scanner isolation invariant unbroken** through M13.5 → M14.G. Subprocess-tested in every milestone.

The user explicitly accepted M14.G on `71e893a` with the statement: *"The grep output only found comment/display text references to `tools/etoro_live_write.py`, not active live-write imports or order paths, so it is not a blocker."*

**M14 closeout is the right call.**

---

## §10 — Recommended next milestone: M15.0 — Scanner / systemd reliability

After M14, the Risk Authority exists. Before moving to M16+ intelligence (strategy/regime work, closed-loop ML, news/sentiment aggregation), the production layer needs hardening. M15.0 is the first concrete unit because it has the lowest risk and the highest debugging value — until the systemd unit-name mismatch is resolved, every milestone-acceptance signal carries an asterisk.

### M15.0 scope (proposed, awaiting separate plan)
- Identify the actual systemd unit (or supervisor process) running the bot on the Hetzner VPS.
- Reconcile the running-but-reported-inactive state across `algo-trader` / `scanner` / `algo-scanner` unit names.
- Document the canonical process manager + unit name in `MILESTONE_STATUS.md` and the operator runbook.
- Ensure dashboard health checks reflect the *actual* process state, not a hardcoded unit-name lookup.

### After M15.0
- **M15.x — IB Gateway reliability hardening.** Restart-on-stale-heartbeat, 4001/4002 socket health monitoring, alerts on prolonged disconnect. Required before `ingest_ibkr_exposure.py` can be wired off its `NotImplementedError` stub.
- **M15.3 — Dashboard + auth hardening.** Auth model tightening; TLS; IP allowlist; potentially the gated `manual_reset` operator flow if scope allows.

### Not next
- **M16+ intelligence** (strategy/regime work, closed-loop ML, news/sentiment, optimiser) does not start until M15 is closed. The roadmap order is unchanged.

---

## §11 — Historical context

`docs/PROJECT_STATUS_RECONCILIATION.md` (commit `c5536b5`) remains in the repository as the historical record of how project status was reconciled at that time. It is **not deleted**. From 2026-06-02 onward, **this document (`docs/M14_FINAL_AUDIT.md`) is the authoritative closeout document for M14-specific status**. For the live state of every milestone across the whole project, [`MILESTONE_STATUS.md`](../MILESTONE_STATUS.md) remains the single source of truth and has been updated to reflect M14's full closure.

---

## §12 — Scanner-path coverage gap (open carry-forward, recorded 2026-06-05)

Recorded at the post-M16 M1–M16 independent audit pass.

### What the gap is

The M14 Risk Authority Engine (`bot.risk_authority.engine.decide` — 24 ordered gates) is invoked **only** from `tools/etoro_live_write.py` via `bot/risk_authority/preflight.py`. The scanner-driven IBKR submit path in `main.py` (lines 226–322) runs only `bot/risk.py` (`RiskManager.evaluate` + `PortfolioRiskPolicy.evaluate`) — a smaller set of gates.

Gates that the M14 engine enforces but the scanner path does NOT consult:

- `broker_daily_loss_cap` / `global_daily_loss_cap`
- `global_capital` / `combined_exposure` / `broker_open_positions` (max cap from engine, distinct from M10 `max_open_positions`)
- `drawdown_throttle`
- `per-symbol concentration` (M14.E)
- `quote_freshness`, `spread`, `data_staleness`
- `etoro_live_flag` / `etoro_live_env`
- `policy_invalid` (the unified policy-validity check)

Gates that the scanner path DOES enforce (via `bot/risk.py`):

- Per-position size cap (`RISK_MAX_POSITION_PCT`)
- Live hard cap (`LIVE_MAX_POSITION_PCT = 2.0` for IBKR live)
- Per-symbol broker position-exists check (live mode)
- Per-symbol broker open-order-exists check (live mode)
- `RISK_MAX_OPEN_POSITIONS` ceiling
- Duplicate same-symbol+direction
- File-based `bot/kill_switch.py`
- M13.4A broker-allocation policy (at scanner startup only until runtime enforcement lands; see audit P0-3)

### Why the asymmetry exists

M14 was scoped specifically for the eToro live-write operator-CLI path (M14.F preflight). It was not extended to the scanner path because the scanner was running paper trading and a small IBKR live trial that the existing `bot/risk.py` gates were deemed sufficient for.

### Why it must NOT remain like this before M22

The M22 milestone (Semi-Automated Live Trading — authority ladder reaches `AUTO_ALLOWED` per-broker) is incompatible with this asymmetry. Auto-allowed submissions, by definition, are not pre-checked by a human operator before each transmission. The scanner-driven path is what would carry those submissions. Without the M14 engine wrapping the scanner submit path, M22 would automate trading through the smaller gate set.

### Hard pre-requisite recorded here

**Before M22 (Semi-Automated Live Trading) can begin: the M14 Risk Authority Engine must wrap the scanner path's broker submits, OR `bot/risk.py` must be extended with explicit equivalents of every M14 gate listed above, with AST-asserted parity.** Either approach is acceptable; the recorded requirement is that **the gate set the scanner enforces must equal or exceed the M14 engine's gate set** at M22 time.

This is NOT in M17 scope.

### Until then

IBKR live submissions in production must remain operator-supervised; do not enable any unattended automation that bypasses operator review. Scanner-driven paper trading is fine — paper has no real-money impact.

### Linked tracking

- `docs/NEXT_WORK_REGISTER.md` — entry "M14-extension-to-scanner-path" listed as blocker for M22.
- `ROADMAP.md` — under M22, precondition note added pointing here.

---

*End of M14 final audit.*
