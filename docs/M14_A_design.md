# M14.A — Risk Intelligence Layer: Design

**Type:** docs-only. No code change, no schema change, no live/demo call,
no eToro write, no order. This is the reviewable design artifact for
M14; nothing in this document is implemented yet.

**Predecessors:** M13.7 closeout (`1e2ced7`). M13.5.B live writer
(`5cb49ea`) with its 16-gate preflight is the contract M14 must integrate
with without weakening.

**Core principle (carried from review):** *correctness before
cleverness.* The advanced features sit on a verified data foundation.
A wrong exposure number feeding a clever governor is worse than no
governor.

---

## 1. Scope (build) and non-goals

### Build in M14
- Broker-scoped + global **risk state** schema (additive, backward-compatible).
- **Realised PnL ingestion** per broker (IBKR + eToro, read-only).
- **Portfolio exposure engine** (positions → exposure, capital used, open-position counts).
- **Risk Authority Engine** — the pure decision core (§3).
- **Authority ladder + downgrade-only governor** with cooldown/hysteresis (§5–6).
- **Risk snapshot + decision audit** (§7).
- **Staleness rules** that fail-closed (§8).
- **Dashboard read surfaces** for state + decision explanations (read-only).
- **Account health score** and a **risk budget engine** (per-broker + global).
- **Dynamic drawdown throttling**, **concentration cap** (per symbol/sector).
- Closeout + audit doc.

### Non-goals (explicit)
- **No first funded eToro order.** Go-live event is outside M14.
- No enabling `ETORO_LIVE_ENABLED`, no enabling demo, no `--base-url` reintroduction.
- **No strategy/signal/threshold/universe changes.**
- **No ML / confidence-adjusted sizing logic.** Interface stub only; logic deferred.
- **No correlation-aware sizing logic.** Hook only; matrix source not vetted.
- **No automated broker failover execution.** Designed + gated; cutover is manual.
- **No dashboard live-write button.** Read-only forever.
- **No edits to `main.py`, `bot/scanner.py`, `bot/strategy.py`.** `bot/risk.py`
  may host the new engine *only if* explicitly justified at M14.E commit
  time; otherwise the engine lives in a new module
  (`bot/risk_authority/`) and `bot/risk.py` becomes a thin adapter.
  Default plan: **new module, do not edit `bot/risk.py`**; the existing
  `RiskManager` keeps working unchanged.
- No infra/systemd work (→ M15.3).

---

## 2. Sub-milestone plan

| ID | Title | Output | Live calls |
|---|---|---|---|
| M14.A | Design doc | this file | none |
| M14.B | Schema + migration | new `daily_state_per_broker`, backfill, compat shim, idempotent | none |
| M14.C | Realised-PnL ingestion adapters | per-broker read-only ingestion → state | none (mocked in tests) |
| M14.D | Portfolio exposure engine | positions → exposure / capital / counts | none |
| M14.E | **Risk Authority Engine + Governor** | pure decision core + state machine + audit | none |
| M14.F | eToro preflight integration | kill the manual `realised_daily_loss` seam | none |
| M14.G | Dashboard risk surfaces | read-only state + decision explainer | none |
| M14.H | Tests + regression + closeout | full suite + audit doc | none |

Each sub-milestone returns the same compact-evidence shape M13 used
(commit, files, tests, proofs, protected-files-unchanged, clean tree).

---

## 3. The Risk Authority Engine

The keystone. A **pure function** consumed by every place a trade decision
is needed (the operator CLI, the dashboard explainer, future autonomous
paths). Five questions it must answer:

1. Is this broker allowed to trade at all?
2. Is this specific trade allowed right now?
3. What authority level does the system currently have?
4. *Why* — every reason code that drove the answer.
5. *What must happen* to restore higher authority (the recovery path).

Signature (Python pseudocode, illustrative):

```
RiskDecision = decide(
    context: RiskContext,
    snapshot: RiskSnapshot,
    request: Optional[TradeRequest] = None,   # None = "what authority do I have?"
) -> RiskDecision
```

The engine **never** mutates state, never performs I/O, never calls a
broker. State is gathered by ingestion (M14.C/D); the engine consumes a
frozen `RiskSnapshot`. This is what makes it exhaustively testable.

---

## 4. Interfaces

### 4.1 `RiskContext` — *the question*
- `broker_scope`: `'ibkr_live'|'ibkr_paper'|'etoro_real'|'etoro_paper'|'GLOBAL'`
- `policy`: M13.4A broker-allocation policy (read; not mutated)
- `now_utc`, `trading_day` (date)
- `requested_action`: one of `{trade_open, trade_close, query_authority}`
- `request_payload`: optional `TradeRequest` (symbol, amount, side, leverage)

### 4.2 `RiskSnapshot` — *the evidence*
Reproducible, immutable; what the engine sees, recorded with the decision.
- `taken_at`: ISO-UTC
- `policy_version`: from `portfolio_risk_state` row
- `per_broker`: `{scope -> {realised_pnl, realised_daily_loss, open_positions,
   capital_deployed, peak_equity, drawdown_from_peak, source, freshness_sec}}`
- `global`: roll-up of per-broker (combined exposure, combined daily-loss,
   combined positions, combined drawdown — explicit recomputation, not stored)
- `positions`: list (broker, symbol, qty, mark, exposure_usd, opened_at)
- `freshness`: per-data-source `last_updated_at` + age
- `market_state`: per-symbol (open/closed, last_quote_age, spread_bps) supplied
  by caller (kept out of the engine's I/O)
- `concentration`: per-symbol and per-sector exposure aggregates

### 4.3 `TradeRequest`
`{broker_scope, symbol, side, amount_usd, leverage, sl?, tp?, source}` —
amount is dollars, mirroring the eToro `Amount` field.

### 4.4 `RiskDecision` — *the answer*
- `decision_id`: UUID
- `result`: `'allow' | 'block' | 'downgrade_then_block'`
- `authority_before`, `authority_after`
- `reason_codes`: ordered list (most blocking first)
- `recovery_paths`: per reason, what restores it (see §6 cooldown table)
- `explainer`: structured human-readable trace (one line per gate evaluated)
- `snapshot_ref`: pointer to the stored snapshot row
- `audit_ref`: pointer to the audit row

### 4.5 Compatibility shim
The engine wraps the existing `RiskManager` and `PortfolioRiskContext` in
`bot/risk.py` — it does not replace them. Existing M12/M13 callers keep
working; the engine becomes the *new* path the eToro CLI consults.

---

## 5. Authority ladder

| Level | Description | Allowed actions |
|---|---|---|
| `OFF` | Hard kill — global kill switch or unrecoverable broker state | nothing |
| `SIGNAL_ONLY` | Scan + alert, no broker submission | scanner, Telegram |
| `PAPER_ONLY` | Paper broker submissions allowed; no real-money path | paper submit |
| `ONE_SHOT_MANUAL` | One operator-confirmed real trade at a time (today's M13.5.B mode) | operator CLI single shot |
| `AUTO_ALLOWED` | Future autonomous live submission permitted | (not built in M14) |

Rules — **non-negotiable**:
- The governor **may downgrade** authority autonomously on breach.
- The governor **may never upgrade** autonomously. Every upgrade is an
  explicit, audited human action (dashboard button or CLI command), with
  reason logged. *Property-tested:* `authority_after >= authority_before`
  for any autonomous transition is **forbidden** unless the transition is
  flagged `source='human'` or `source='manual_reset'`.
- The ladder is a **lower bound on safety**, not on capability — being at
  `AUTO_ALLOWED` does not bypass any gate; a single-trade still passes
  every gate in §9.

Current system reality at M14 start: max attainable level today is
`ONE_SHOT_MANUAL` because `AUTO_ALLOWED` requires features outside M14.

---

## 6. Governor state machine (with cooldown / hysteresis)

A finite state machine over `(scope, authority)`. Transitions:

- **Downgrade trigger** → immediate transition, no flapping possible
  because downgrades are monotonic safety moves.
- **Upgrade trigger** → never automatic. A pending request is recorded;
  a human action is required.

Cooldown / hysteresis table (the heart of "don't flap"):

| Trigger | Effect | Auto-restore? | Restore condition |
|---|---|---|---|
| Global kill switch | → `OFF` (all scopes) | no | manual reset |
| Broker kill switch | scope → `OFF` | no | manual reset |
| Global / broker daily-loss breach | scope → `SIGNAL_ONLY` | **no** — same trading day stays downgraded | next UTC trading day **and** loss reset **and** human ack |
| Drawdown breach (peak→current beyond threshold) | scope → `SIGNAL_ONLY`; throttle curve applies | no | equity recovery above threshold for N consecutive snapshots **and** human ack |
| Stale broker data (PnL or positions) | scope → `SIGNAL_ONLY` (live writes blocked) | yes, conditional | N consecutive fresh reads (default N=3) within freshness window |
| Stale equity/capital | block **capital expansion** only; existing positions readable | yes, conditional | N consecutive fresh reads |
| Concentration breach | block trades that increase concentration | yes, conditional | exposure falls below cap on a fresh snapshot |
| Open-position cap hit | block new opens | yes, conditional | a close brings count below cap |
| Unknown daily loss | scope → block live writes (fail-closed) | yes | ingestion produces a fresh number |

Two important properties:
- *Hysteresis on stale data*: a single fresh read doesn't restore — N
  consecutive fresh reads do. Tunable per source.
- *No same-day re-arm on daily-loss*: even if PnL turns positive, the day
  stays downgraded. Tomorrow re-evaluates from snapshots.

---

## 7. Risk snapshot + decision audit

Every decision is tied to a reproducible snapshot. Two new tables in M14.B
(detailed in §10).

**`risk_snapshots`** — what the engine saw:
- `id`, `taken_at`, `policy_version`, `snapshot_json` (full RiskSnapshot),
  `freshness_summary`, `source` (`scheduled|on_demand|pre_decision`).

**`risk_decisions`** — what it decided:
- `decision_id` (UUID), `taken_at`, `broker_scope`, `requested_action`,
  `request_json` (redacted), `result`, `authority_before`,
  `authority_after`, `reason_codes` (JSON array), `recovery_paths` (JSON),
  `snapshot_id` (FK), `source` (`auto|manual|reconciled`), `actor` (system
  or operator id), `explainer` (text).

Audit invariants:
- Every `decide()` call writes a `risk_decision` row, even when nothing
  changes (a `query_authority` call still records "current state, no
  change").
- No secrets in any column; same redaction rules as M13.5.B audit logger.
- `snapshot_id` makes any past decision **reproducible** by re-running
  `decide()` on the stored snapshot.

---

## 8. Staleness rules — fail-closed by default

Each data source has a `max_age_sec` and a `consecutive_fresh_required`
(for hysteresis). Defaults (tunable in policy, never made laxer
silently):

| Source | `max_age_sec` | On stale | Restore N |
|---|---|---|---|
| Realised PnL (per broker) | 300 | block live write; downgrade scope to `SIGNAL_ONLY` if persistent | 3 |
| Positions (per broker) | 120 | block auto-trade; allow `query_authority` | 3 |
| Equity / capital | 600 | block capital expansion; allow same-size trades | 2 |
| Policy row | 60 | refuse decision; return `block` with `policy_stale` | n/a (refresh) |
| Market quote / spread | M13.5.B value | unchanged | n/a |

**Unknown ≠ zero.** The engine never substitutes 0 for missing daily-loss
or missing exposure. Missing → `block` with explicit reason code, exactly
mirroring M13.5.B's `DailyLossUnknown` discipline.

---

## 9. Unified gate order

Mirrors and extends M13.5.B's 16 gates so there is **one mental model**
across brokers. Gates run in this fixed order; first failure determines
the decision. **Bold = new in M14.**

1. Policy loaded & valid
2. Global kill switch
3. Broker kill switch
4. Global auto-enabled
5. Broker auto-enabled
6. Broker in `allowed_brokers`
7. (eToro only) `routing.etoro_live_enabled is True`
8. (eToro only) `ETORO_LIVE_ENABLED` env true
9. **Authority ladder ≥ required level for requested action**
10. Amount valid + ≥ `amount_min`
11. Single-trade cap
12. Broker capital cap
13. Global capital cap
14. **Combined-exposure cap (cross-broker)**
15. Broker open-positions cap
16. **Global open-positions cap (cross-broker)**
17. Broker daily-loss (uses ingested PnL; unknown → block)
18. **Global daily-loss (uses combined ingested PnL; unknown → block)**
19. **Drawdown throttle (per scope and global)**
20. **Concentration cap (per symbol; per sector if sector data fresh)**
21. Market open
22. Quote freshness
23. Spread cap
24. **Data staleness summary** (final fail-closed sweep before allow)

M13.5.B's existing 16 gates are preserved at the broker level *and* the
engine evaluates them — they run twice (engine pre-check, then live
broker preflight) by design. Belt-and-braces: the engine produces the
inputs the live broker preflight already validates.

---

## 10. Schema design

### 10.1 New tables (additive only)

**`daily_state_per_broker`**
```
PRIMARY KEY (date, broker_scope)
broker_scope         TEXT NOT NULL
date                 TEXT NOT NULL
realised_pnl_usd     REAL DEFAULT 0
realised_pnl_pct     REAL DEFAULT 0
realised_daily_loss  REAL DEFAULT 0
open_positions       INTEGER DEFAULT 0
capital_deployed     REAL DEFAULT 0
peak_equity          REAL
drawdown_from_peak   REAL DEFAULT 0
source               TEXT       -- 'ingested' | 'reconciled' | 'manual_fallback'
last_ingested_at     TEXT
fresh_reads_count    INTEGER DEFAULT 0   -- hysteresis counter
lifecycle_json       TEXT       -- audit/diagnostics
updated_at           TEXT
```

`broker_scope ∈ {'ibkr_live','ibkr_paper','etoro_real','etoro_paper','GLOBAL'}`.
`GLOBAL` rows are roll-ups produced by the engine; never used as a broker.

**`risk_snapshots`** — see §7.
**`risk_decisions`** — see §7.

### 10.2 Migration strategy (M14.B)
- **Additive only.** Do not alter `daily_state`.
- **Backfill** every existing `daily_state` row into `daily_state_per_broker`
  with `broker_scope='GLOBAL'`. Idempotent (INSERT OR IGNORE).
- **Compatibility shim**: a view `daily_state_compat` (or, if SQLite view
  semantics are awkward, a function `get_daily_state_compat(conn)`) that
  returns today's GLOBAL row in the *exact* shape M12/M15 callers expect.
  Old `bot/flywheel.get_daily_state` continues to work unchanged because
  the source table is untouched.
- Schema migration carries a version row in `portfolio_risk_state`;
  re-run is a no-op.

### 10.3 What `bot/flywheel.py` does + doesn't change
- **Does**: gain a write path for `daily_state_per_broker` and the new
  audit tables (new functions, not edits to existing ones).
- **Does not**: change `get_daily_state` / `set_daily_loss_block` / any
  existing column. M12 readers untouched.

---

## 11. eToro preflight integration (M14.F)

Today: `LiveWriteContext.realised_daily_loss = float(args.realised_daily_loss)`.
After M14.F:

- The operator CLI (`tools/etoro_live_write.py`) calls `engine.decide(
  query_authority for etoro_real)` to get the current authority + state.
- If authority allows the trade and ingestion has produced a fresh
  `realised_daily_loss` for `etoro_real`, the CLI populates the
  `LiveWriteContext` from `daily_state_per_broker`.
- If ingestion is stale/missing → `LiveWriteContext.realised_daily_loss
  = None` → existing M13.5.B `DailyLossUnknown` fires. **Fail-closed
  preserved unchanged.**
- `--realised-daily-loss` CLI flag remains as **explicit operator
  override**, logged with `source='manual_fallback'` in the audit row.
- M13.5.B's 40 live_broker tests must stay green untouched.

---

## 12. Build-now vs design-only

| Feature | M14 status | Why |
|---|---|---|
| Risk Authority Engine (pure core) | **Build** | Keystone |
| Authority ladder + downgrade-only governor | **Build** | One-directional → safe |
| Per-broker + global state schema | **Build** | Closes the seam |
| Realised PnL ingestion (IBKR + eToro read-only) | **Build** | Closes manual seam |
| Portfolio exposure engine | **Build** | Required for global gates |
| Risk budget engine | **Build** | Natural home for caps/loss |
| Dynamic drawdown throttle | **Build** | Deterministic, testable |
| Concentration cap (per symbol; per sector if data) | **Build** | Real protection |
| Account health score + explainability | **Build** | Cheap; makes audit useful |
| Risk snapshots + decision audit | **Build** | Reproducibility |
| Dashboard read-only risk surfaces | **Build** | Operator visibility |
| Correlation-aware sizing | **Design-only** | Matrix source not vetted |
| Confidence-adjusted sizing | **Design-only (interface stub)** | Couples risk to immature ML |
| Capital allocation recommendations | **Design-only** | Advisory; never auto-applied |
| Automated broker failover | **Design-only** | Money-movement bugs hide here |
| Exposure heat map | **Optional in M14.G** | UI candy, not a blocker |

Design-only items receive a written interface + non-implementation
rationale in M14.A's deliverable, no executable code path.

---

## 13. Dashboard plan (read-only)

M14.G adds **read-only** panels and endpoints:

- **State panel:** per-broker + global rows (PnL today, daily-loss, open
  positions, capital deployed, drawdown, freshness).
- **Authority panel:** current authority per scope, with recovery path
  per active reason (e.g. "scope etoro_real downgraded to SIGNAL_ONLY —
  reason: stale_realised_pnl — restore: 3 fresh reads within 5m").
- **Decision explainer:** a `/api/risk/explain?broker=etoro_real` GET
  that returns a fresh `decide(query_authority)` result + snapshot
  reference.
- **Audit log viewer:** `risk_decisions` table, redacted, paginated.
- **Manual upgrade buttons:** clearly marked, each click writes an
  audit row with `source='manual_reset'` and `actor`. Re-arming
  daily-loss requires explicit confirmation.
- **No live-write button anywhere.** This is enforced by `grep`-able
  test (extending M13.5.B's no-live-write-button discipline).

---

## 14. Tests + acceptance

### Engine tests (M14.E)
- Every gate, every breach, fixed order, fail-closed on unknown/stale.
- Property: `decide()` is pure (idempotent under same inputs).
- Property: **monotone authority** — no autonomous upgrade is ever
  produced by any input sequence (random-input property test).
- Property: drawdown throttle is monotone in drawdown.
- Property: same-day daily-loss latch — once downgraded today, no input
  short of a `manual_reset` action restores authority within the same
  UTC day.
- Reason codes are stable strings; recovery paths are non-empty for
  every block.

### Schema/migration tests (M14.B)
- Backfill correctness (every existing `daily_state` row appears with
  `broker_scope='GLOBAL'`).
- Idempotency (re-running migration produces no changes).
- Compatibility shim returns the same shape M12/M15 callers consume.

### Ingestion tests (M14.C)
- Mocked broker responses only — **no live broker calls.**
- Stale / missing / partial → `None` → fail-closed.
- Hysteresis counter increments on fresh, resets on stale.

### Integration tests (M14.F)
- eToro CLI with ingestion present uses ingested number;
  `--realised-daily-loss` override is honoured but tagged
  `source='manual_fallback'`.
- Ingestion unavailable → `DailyLossUnknown` still fires (M13.5.B unchanged).
- M13.5.B 173-test suite green untouched.

### Dashboard tests (M14.G)
- All new endpoints are read-only; `grep`-test confirms no live-write
  control was added; no `EtoroLiveBroker` constructor reachable.

### Cross-cutting
- Full regression: M12, M13.2/3/4A, all M13.5.B suites, M15 schema/
  gateway/health — all green unchanged.
- Protected files diff vs M13.7 closeout: empty (or any exception is
  reviewed at commit time).

### Acceptance criteria to close M14
- Per-broker + global state correct, backfilled, old readers unbroken.
- Realised PnL ingested for both brokers (read-only); manual seam
  removed; override remains, labeled.
- Engine shipped with full gate suite green; governor proven
  downgrade-only.
- Drawdown throttle + concentration + budget + health score operational
  and explainable.
- eToro preflight fed automatically; fail-closed intact; M13.5.B
  unchanged.
- Dashboard shows per-broker + global state + decision explanations
  (read-only; no live-write button).
- Full regression green; protected execution files untouched (or
  exception reviewed).
- Closeout doc with invariants + "not done" handoff.
- Still **zero real orders.**

---

## 15. Honest risks and trade-offs

- **Schema doubling.** Two daily-state tables for a window. Mitigated
  by additive design + shim; eventual deprecation of `daily_state` is
  out of M14 scope.
- **Engine vs broker double-evaluation.** Same gates run in two places
  (engine + live preflight). Cost: tiny. Benefit: defense in depth, and
  M13.5.B's preflight stays the last line of defense.
- **Ingestion correctness is the hard part.** A confidently-wrong
  realised_daily_loss is worse than the current manual seam. Mitigations:
  (a) staleness rules fail-closed, (b) reconciliation source-of-truth is
  broker positions + executed trades, (c) every ingested value carries
  a `source` and `last_ingested_at` written to audit.
- **Health score is opinionated.** Risk: it becomes a number people
  trust without reading reason codes. Mitigation: the score never
  *gates* anything by itself — gates are explicit. The score is a
  *display*.
- **Cooldown calibration.** Defaults in §6/§8 are educated guesses;
  M14.E ships them as policy fields, not hardcoded, so review can tune.

---

## 16. What remains after M14

- Signal/universe diagnostic — separate, still tracked.
- Learning/outcome loop / flywheel maturation → feeds confidence-sizing later.
- **First funded eToro order** — separate go-live event, now backed by
  automated risk state.
- M15.3 infra recovery + scanner systemd unit-name item.
- Deferred advanced items: correlation logic, confidence-sizing logic,
  automated broker failover — designed in M14, not activated.

---

## 17. What this document is *not*

- Not a schema migration (M14.B).
- Not an implementation (M14.C–G).
- Not a policy change. M13.4A policy values are read by the engine; no
  new dashboard editable field is added in M14.A.
- Not authorisation for any live or demo eToro call. Demo remains
  disabled; the operator CLI's safety surface (single endpoint, no
  `--base-url`, double live flag + nonce) is unchanged.

**Final M14.A status: design only. Awaiting ChatGPT review before any
M14.B implementation work.**
