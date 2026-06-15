# Project Status Reconciliation

> **Update 2026 (post-M18 merge).** This document below was written at
> repo HEAD `729ad2d` (M14.D) and predates M16/M17 closure and M18. Current
> truth: M16 CLOSED; **M17 CLOSED** (`origin/main` = `M17.B.closeout`
> `a8d8ca4`, `test_m17_backtesting` 200 OK); **M18 (ML strategy/criteria
> foundation, read-only/shadow-only) is CLOSED and merged to `main` at
> `264fba84`** (`test_m18_ml` loader 669 — see `docs/M18_ACCEPTANCE.md`).
> Where the text below
> says ML closed-loop / sentiment / scoring is "M17 or M18 scope", note that
> M18 delivers the ML *foundation* only (sklearn/RandomForest, gated,
> shadow-only); it is **not** live trading and **not** M19 signal scoring.
> M19 (signal scoring) is the next concrete milestone after M18 merges.

**Date:** 2026-05-29
**Repo HEAD at writing:** `729ad2d` (M14.D bugfix; M14.D accepted on VPS)
**Scope:** docs-only reconciliation. No code change, no broker writes,
no live calls, no orders.

This document reconciles three things that have drifted apart:

1. The **original 15-milestone roadmap** (preserved verbatim in `ROADMAP.md`).
2. The **expanded sub-milestone work** (M13.1 → M13.7, M14.A → M14.H,
   M15.0 → M15.3) implemented in the current chat thread.
3. The **older project notes** that described M13 as "externally blocked"
   and M14 as "not started." Those statements were true at their time of
   writing and are no longer accurate.

The single source of truth for the live state of each milestone is
[`MILESTONE_STATUS.md`](../MILESTONE_STATUS.md). This document explains
*why* the status now reads the way it does.

---

## 1. Historical view vs current truth

### What the old notes said

- M1–M6: complete.
- M7–M10: partial / not started.
- M11–M12: IBKR work in flight, paper validated, live not yet attempted.
- **M13: "externally blocked"** — pending eToro API availability.
- **M14: "not started"** — portfolio/risk layer pending.
- M15: "production hardening" pending.

### What is actually true now

- M1–M7: closed.
- M8: implemented as a real NewsAPI integration with caching and pluggable
  providers, used in the live cycle. Not yet macro-aware or multi-source.
- M9: XGBoost meta-labeling **training infrastructure** complete
  (`ml_train.py`, `ml_build_dataset.py`); **not** yet wired into the
  scanner as a live filter. The closed-loop self-learning piece is M17.
- M10: broker abstraction closed; both paper and IBKR adapters present.
- M11: closed; IBKR paper login verified, IBC 3.22.0 running headless
  with systemd.
- M12: **closed with real broker acceptance**. A controlled live order
  (Ford / 1 share / delayed market data) was accepted by the live broker
  with a confirmed `permId`; `execution_intents` row reflects the truthful
  state; position cancelled cleanly; no residual F exposure; bot returned
  to paper.
- M13: **closed in the current chat thread**. Capability built, gated,
  reviewed, deployed, and no-write verified across 173 M13.5.B tests +
  M13.5.C VPS readiness pass. The "externally blocked" framing is
  historical drift; eToro's public API launched in October 2025 and the
  capability has been built against it (read + live-write capability)
  without placing any real eToro order. The first funded eToro order
  remains a separate later go-live event (now tracked as **M21**).
- M14: **A, B, C, D closed**. E, F, G, H pending.
  - M14.A design (commit `3f4448e`).
  - M14.B additive `daily_state_per_broker` + `risk_snapshots` +
    `risk_decisions` schema (commit `42ee08c`).
  - M14.C realised-PnL ingestion adapters with fail-closed semantics
    (commit `d9c53eb`).
  - M14.D exposure ingestion + `broker_positions` batch schema + strict
    cross-engine separation (commit `729ad2d`).
- M15: partial — M15.0/.1/.2 closed, M15.3 (infra recovery, IB Gateway
  reliability hardening, process-manager / systemd unit-name cleanup,
  compliance-grade audit) pending.

### The diff in one sentence

The two biggest drifts are: **M8/M9 are more built than the old notes
suggest** (but with honest gaps that map to M17 and M18), and **M13/M14
are far more built than the old notes suggest** (M13 closed; M14 half
done). Everything else aligns.

---

## 2. How sub-milestones map back to the original numbers

The original 15-milestone numbering is preserved. Sub-milestones never
take new top-level numbers. They live inside their parent.

| Parent | Sub-milestones (chronological) |
|---|---|
| M13 | M13.0 discovery → M13.1 design → M13.2 read adapter → M13.3 paper broker → M13.4A allocation → M13.4A.1 UX polish → M13.4B minimum live-write design → M13.5.A evidence pack → M13.5.B live writer (`cb47758` + three corrections `f880516` / `b051538` / `5cb49ea`) → M13.5.C VPS readiness → M13.5.D open-unknowns → M13.7 closeout (`1e2ced7`) |
| M14 | M14.A design → M14.B schema → M14.C PnL ingestion → M14.D exposure ingestion → **M14.E engine + governor** (next) → M14.F preflight integration → M14.G dashboard read-only → M14.H closeout |
| M15 | M15.0 flywheel schema → M15.1 gateway state → M15.2 health endpoint → **M15.3 infra recovery** (pending) |

When a milestone is closed, the chain and all proving tests are captured
in its closeout doc:
- M13: `docs/M13_7_closeout.md`
- M14: pending (M14.H)
- M15: pending (covered piecewise in `docs/M15_2_external_monitoring.md`
  and per-suite test files)

---

## 3. Why the "implemented but not closed" classifications matter

Two milestones (M8 sentiment, M9 ML) carry an explicit "implemented but
not closed-loop" label. This is deliberate honesty, not foot-dragging:

- **M8** has a real NewsAPI integration used in the live cycle. Calling
  it "closed" would imply macro/multi-source aggregation and
  confidence-weighted scoring exist. They don't. Those are M18 scope.
  Keeping M8 as "implemented, not closed" preserves the original goal
  visibility.
- **M9** has 541 lines of XGBoost meta-labeling with walk-forward CV,
  isotonic calibration, and per-group evaluation. Calling it "closed"
  would imply the model filters live signals. It doesn't — `bot/scanner.py`
  contains no `model.predict` / `joblib.load` / `xgb` imports. The
  closed-loop hookup is M17.

Both lines preserve the original goal while flagging the remaining work.
Neither "downgrades" the existing implementation.

---

## 4. Carry-forward warnings (active)

These are real items that have surfaced across multiple VPS verifications
and remain open. None block the current milestone; all need resolution
before sustained automated live trading (M22).

### 4.1 Scanner systemd unit-name mismatch
Observed in: M13.5.C, M14.B, M14.C, M14.D VPS runs.
Each exact-match check returns:
```
algo-trader   = inactive
scanner       = inactive
algo-scanner  = inactive
```
…while `/api/health` returns 200, heartbeat is fresh, and dashboard port
8080 is listening. The bot **is** running; the unit name is wrong (or a
different process manager is in use). Tracked under M15.3.

### 4.2 IBKR exposure reader not wired to Gateway
M14.D ships the adapter shape; production wiring to the live Gateway
positions surface is intentionally deferred (so M14.D could close without
mixing concerns). On the VPS, the IBKR exposure adapter currently returns
`exposure_unknown(positions_reader_failed:NotImplementedError)`. Tracked
for the post-M14.D follow-up (around M14.E/F/G or M15.3).

### 4.3 eToro keys absent on VPS
The eToro real adapter returns `exposure_unknown(keys_absent)` on the
VPS because no live keys are configured. This is the **correct** state
for a no-real-orders project; keys come in for M21 (first funded
eToro go-live), under explicit operator confirmation.

### 4.4 Backtesting Yahoo/cache limits
Tracked at M5 (accepted-enough). Provider-side reliability lives at
M6/M15; not reopened in M5.

---

## 5. What this reconciliation deliberately does NOT change

- **Permanent operating rules** (one milestone at a time; verified before
  next; no strategy threshold changes to manufacture signals; honesty
  about verified vs not verified; secrets gitignored; deployment paths
  self-contained) — unchanged.
- **The original 15-milestone numbering** — unchanged. Sub-milestones
  belong to a parent.
- **No code, no schema, no runtime behaviour** — this is a docs-only pass.
- **No claims about milestones not yet executed** (M14.E onwards) — they
  remain `PENDING`. The M14.E plan lives separately and is not implemented
  yet.

---

## 6. References

- [`ROADMAP.md`](../ROADMAP.md) — the narrative roadmap (1–23).
- [`MILESTONE_STATUS.md`](../MILESTONE_STATUS.md) — the live-state table.
- [`docs/M13_7_closeout.md`](M13_7_closeout.md) — M13 closeout (full chain).
- [`docs/M14_A_design.md`](M14_A_design.md) — M14 risk intelligence design.
- [`docs/M13_5_D_open_unknowns.md`](M13_5_D_open_unknowns.md) — eToro open-unknowns provenance register.
- [`docs/M15_2_external_monitoring.md`](M15_2_external_monitoring.md) — health endpoint surface.
