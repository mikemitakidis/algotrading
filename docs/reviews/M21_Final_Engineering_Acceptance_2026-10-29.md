# M21 Final Engineering Acceptance Packet — 29 Oct 2026

**Assignment:** "M21 final engineering acceptance and merge-readiness program" (operator, 29 Oct 2026).
**Repo:** `mikemitakidis/algotrading`. **`main`:** `80b4343db4a22413aa670efd32e24952d53e532e`, unchanged.
**What this is:** the single acceptance record for the M21 engineering release candidate. It replaces no other document. The checklist stays in `docs/handoffs/M21_Finish_Line_2026-10-28.md` §6; the register stays in the canonical handoff (v3.11, all 71 tasks kept).

**Nothing was merged, deployed or activated.** There was no VPS access, no IBKR connection, no paper or real orders, no paper loop, no production database change, no Telegram, no protected dashboard change, no CI enablement, no data purchase and no Vercel change. `test_m12.py --live` and the v3 canary were not run. All 16 PRs are still drafts with auto-merge off.

---

## 0. Verdict on one screen

- **The code is a merge-ready release candidate, subject to your reviews and two protected-file approvals.**
  - All 16 PRs merge onto `main` in the stated order with **zero conflicts**, in either lane order. The result equals the integration tree minus its integration-only E2E test file (and that file's two allow-list entries).
  - On the integration tree (that tree plus the E2E file), the full suite result is **129 modules: 128 OK, 0 failed, 1 without tests (test_m12, by design)** (§12).
- **13 new defects were found and fixed this round.** Three were high: a forged M14 decision id could open a paper intent (#17); a check-then-activate race could overwrite an operator's controls change (#22/#23); and a non-UTC publication time leaked future context data (#25). One merge-blocking conflict (#18/#25) is fixed. Nothing open is critical or high.
- **What is left is integration work, not missing features:** 7 small engineering items (≈ 8 dev-days), the merges, the v3 canary run, and the operations OP1–OP5, which wait on your approvals. Real-data and real-runtime claims stay **DATA_UNVERIFIED** / **RUNTIME_UNVERIFIED** until OP1–OP4.

---

## 1. PR heads (exact, verified against GitHub on 29 Oct)

| PR | WP | Branch → base | 28 Oct (late) head | **Final head** | This round |
|---|---|---|---|---|---|
| #12 | A01 | `m21.1/a01-scoring-truth` → `main` | `47e9057` | **`47e9057`** | — |
| #1 | WS2 | `m21-ws2-corrected-review` → `main` | `0f3f9e6` | **`0f3f9e6`** | — |
| #14 | A02 | `m21.1/a02-candidate-ledger` → `m21.1/a01-scoring-truth` | `68ca7b4` | **`68ca7b4`** | — |
| #13 | C01 doc | `docs/m22-c01-risk-parity-design` → `main` | `6a8a690` | **`6a8a690`** | — |
| #15 | WP5 | `m21/wp5-evidence` → `m21/stack-base` | `5394882` | **`5394882`** | audited, no change |
| #16 | WP6 | `m21/wp6-completed-candles` → `m21/wp5-evidence` | `bf92678` | **`bf92678`** | audited, no change |
| #17 | WP7 | `m21/wp7-ledger-wiring` → `m21/wp6-completed-candles` | `4c1cac7` | **`bf94f71`** | M14 authentication, execution permission |
| #21 | WP8 | `m21/wp8-templates` → `m21/wp7-ledger-wiring` | `04970d4` | **`fbfc815`** | merge only |
| #22 | WP9 | `m21/wp9-template-controls` → `m21/wp8-templates` | `ded303d` | **`b4e9955`** | compare-and-swap |
| #23 | WP14 | `m21/wp14-promotion` → `m21/wp9-template-controls` | `28c1943` | **`04bf5ca`** | CAS apply/revert, post-activation verification; ROLL_FORWARD test |
| #18 | WP10 | `m21/wp10-monitoring` → `m21/stack-base` | `45784e7` | **`9012711`** | future-stamp fix; allow-list anchor |
| #19 | WP12 | `m21/wp12-economics` → `m21/stack-base` | `d7b8e66` | **`d7b8e66`** | audited, no change |
| #20 | WP11 | `m21/wp11-readiness` → `m21/wp12-economics` | `5f86c30` | **`4924844`** | G9 root cause; M18 single path; Group F pin |
| #24 | WP13 | `m21/wp13-walkforward` → `m21/wp11-readiness` | `aa00b12` | **`4b677bc`** | planted-effect identity, environment, v3 procedure |
| #26 | WP16–18 | `m21/wp16-18-shadow-ml` → `m21/wp13-walkforward` | `9cffb0a` | **`89bc874`** | merge only |
| #25 | WP15 | `m21/wp15-market-context` → `m21/stack-base` | `d992a0f` | **`f05ed84`** | UTC instants, minimum lag; allow-list anchor |

- `m21/stack-base` = `c7183f1` (#12 + #1 + #14 + #13). Its tree `a1aa8dd` is **identical** to `main` after merging those four.
- `m21/integration` = **`79ae46f`**, tree `7843553` (verification only, never merged).
- All updates were normal merges or new commits, pushed fast-forward. No rebase or force-push; review history is intact.

## 2. Verification of the previous findings

| ID | PR | Finding (28 Oct) | Re-verified on the final heads |
|---|---|---|---|
| F1 | #23 | Non-atomic apply/revert across two stores | Holds. Now also CAS-protected inside the controls transaction and verified after activation (N5, N6). `test_01`–`test_06`, `test_12*`, `test_13`–`test_17` |
| F2 | #23 | One-active-change race | Holds: `test_07`, `test_08`, `test_database_level_guards` |
| F3 | #23 | `record_shadow` wrong-state event | Holds: `test_record_shadow_checks_state_and_tokens_cannot_be_reused` |
| F4 | #17 | Fabricated M14 PASS in reconcile | Holds. The deeper variant (a fabricated id on the **live** path) was open and is now fixed (N1) |
| F5 | #24 | Canary identity incomplete | Holds. The planted-effect parameters are now explicit (N8) |
| A1 | #20 | M17/M18 read M16 start stamps as closes | Holds (fail closed). M18 has exactly one production bar path: `test_m18_has_one_production_bar_path` |
| A2 | #24 | Walk-forward `origin="real"` accepted start stamps | Holds |
| A3 | #25 | Context features accepted start stamps | Holds. A related leak (non-UTC stamps) was found and fixed (N11) |
| A4 | #24 | Unknown exits could support a PASS | Holds |
| O1 | #20 | M17 G9 determinism flaked 1 in 4 | **Root cause found and fixed** (N9). Green in every run since |

## 3. New confirmed defects (this round)

Each was demonstrated before the fix, by a failing test or a reproduction script.

| ID | PR | Severity | Defect | Fix |
|---|---|---|---|---|
| N1 | #17 | **HIGH** | `M14_LIVE` was granted to any non-empty decision id, so a fabricated id satisfied `intent()`. | `594db2b`: the decision is read back read-only from M14 `risk_decisions` and must match symbol, side, notional, `ibkr_paper`, `trade_open`, leverage 1, the result and the tick time. Otherwise UNVERIFIED: no link, no intent. |
| N2 | #17 | MEDIUM (fail-closed) | Time check: the journal stamps the tick start, while M14 stamps wall-clock time later in the tick. **Every genuine production decision** would have been UNVERIFIED, blocking all entries once the ledger was enabled. | `594db2b`: a window of ±10 minutes around the tick. |
| N3 | #17 | MEDIUM | `intent()` accepted `VERIFIED`, a historical reconstruction, as permission to execute. | `af35ea9`: only `M14_LIVE` permits an intent (`permits_execution`). |
| N4 | #17 | LOW | A rejection could be "authenticated" by an `allow` decision. | `594db2b`, `8adf370`: a rejection requires a block result. |
| N5 | #22/#23 | **HIGH** | Check-then-activate race across the two stores: an operator change landing between the check and `activate` was overwritten. An auto-revert could overwrite it too. | `80a2b5c`: `activate`/`rollback` take `expected_current`, checked inside the write transaction, and refusals are audited. `f3f7240`: apply and revert use it. |
| N6 | #23 | MEDIUM | Nothing verified after activation that the active config had the intended digest and had replaced exactly the pre-apply version. A tampered or unexpected state could reach a valid-looking MONITORING. | `f3f7240`: `_verify_applied` runs after activation and in recovery; on failure the op is BLOCKED. |
| N7 | #23 | LOW (liveness) | `resolve_block` ROLL_BACK was refused when the intended version never went live but the operator's own version was active, so the op stayed BLOCKED. | `f3f7240`: a version that never went live is withdrawn and the op ABORTED. A version that went live still needs its pre-apply version active. |
| N8 | #24 | LOW | The identity named only the planted effect's kind and horizon; the other parameters were bound only through the code digest. The environment was not recorded. | `9d9b702`: identity schema `/2` (full spec); the report records Python/NumPy/pandas/SciPy versions. |
| N9 | #20 | LOW | `report.json` carried `run_id` with a wall-clock second: the cause of the G9 flake. | `21615c4`: the per-run id lives in `manifest.json` only. Deterministic test across a one-second boundary. |
| N10 | #18 | MEDIUM | A heartbeat or gateway reading stamped in the future had a negative age and read **OK**, so a dead loop could stay green. | `bb66079`: a reading more than 2 minutes ahead is UNKNOWN (CRITICAL). |
| N11 | #25 | **HIGH (lookahead)** | Publication times were stored as text with their original offset and filtered by text comparison. A `-05:00` stamp was visible **hours before publication**. | `474baa7`, `f986842`: observations are normalised to UTC; `asof`/`history_asof` normalise the query time. |
| N12 | #25 | MEDIUM (lookahead) | `parse_fred_csv(assumed_lag=…)` accepted any lag; 1 h made the VIX close of day D knowable at D 01:00 UTC. | `474baa7`: minimum 22 h (the close is set at 16:15 New York, at most 21:15 UTC). |
| N13 | #18/#25 | MEDIUM (merge) | Both inserted their G10 allow-list entries at the same anchor as the L lane, so a **sequential merge onto `main` conflicted** (the integration branch had hidden this). | `9012711`, `f05ed84`: same entries at their own anchors. The trial merge is now conflict-free. |

**Interaction finding I1, not a defect.** `--min-score` (default 60) now gates the **M19 research rank** (0–100). A clean fixture signal ranks about 33 and is not traded. The threshold must be set from the OP1 rank distribution (OP2/OP3 gate). Recorded by integration test E4.

## 4. Corrections and tests

| PR | Commits this round | Tests (module) | Targeted mutants (final heads) |
|---|---|---|---|
| #17 | `594db2b`, `8adf370`, `af35ea9`, `bf94f71` | 29 (`test_m21_1_wp7_ledger_wiring`), incl. `W8_RealM14GateProvenance` through the **real** M14 gate | 35/35 (24 provenance, 9 authentication, 2 execution permission; 3 anchors re-pointed to the current code) |
| #22 | `80a2b5c` | 20 (`test_m21_1_wp9_template_controls`) | 4/4 |
| #23 | `f3f7240`, `da37f8c`, `04bf5ca` | 46 (`test_m21_1_wp14_promotion`) | 28/28 non-equivalent killed; 1 documented equivalent (redundant pre-activation check); one survivor (ROLL_FORWARD sha check) closed by test-only `04bf5ca` |
| #18 | `bb66079`, `9012711` | 13 (`test_m21_1_wp10_monitoring`) | 4/4 |
| #25 | `474baa7`, `f986842`, `f05ed84` | 18 (`test_m21_1_wp15_market_context`) | 7/7 (one survivor closed by `f986842`) |
| #24 | `9d9b702` | optimiser canary tests, incl. explicit-spec mock | 2/2 |
| #20 | `21615c4`, `4924844` | +1 M17 (G9 root cause), +1 readiness (M18 single path), +1 Group F pin | test-only / deterministic |
| integration | `ec28e0d` | +3 E2E (E4 operational chain) | — |

No production assertion was weakened. No synthetic market was retuned. No seed was changed.

## 5. Timestamp verdict (§3)

**Verdict: READY WITH DOCUMENTED LIMITATIONS.** Every production-compatible entrance fails closed on an unproven convention; positive acceptance needs the verified session calendar.

| Entrance | Convention handling | Result |
|---|---|---|
| M16 store → M17 single TF / scanner replica | M16 stamps bar **starts**. Real-data use goes through `research.dataqa.stamps.to_close_stamped` with a session calendar. | Converted: closes at the session close (early closes honoured). |
| M17 multi-timeframe, M18 real build | Refuse raw M16 bars (A1). M18 has one production path (`_bars_provider` is keyword-only, unreachable from the CLI and production modules). | Fail closed. **Converted-input path still to build (E3).** |
| WP11 readiness | Detects the convention per symbol and timeframe; ambiguous → refused; re-stamps with the calendar. | Tested on 1D/4H/1H/15m across the DST change (30 Oct → 2 Nov), early closes (27 Nov, 24 Dec) and holidays (26 Nov, 25 Dec). |
| WP13 walk-forward `origin="real"` | Requires close stamps (A2). | Fail closed. |
| WP12 liquidity | Uses only daily bars closed by the decision time. | Point-in-time. |
| WP5 liquidity and calendar | `measure_liquidity` counts only sessions completed by `evaluated_at`; the calendar fit needs coverage of the decision day and the 10 days before; corporate-action times use the ex-date session open. | Point-in-time. |
| WP6 scanner seam | `select_completed` scores only bars proven complete at the decision time; without a completion policy, no signals. | Fail closed. |
| WP15 context | Start-stamped bars refused (A3); publication-time filter in UTC (N11); minimum backfill lag (N12). | Point-in-time. |
| Universe membership | Our own universe as of each date. Survivorship (delisted symbols) is **not** handled by the data; see G4 (§18). | Documented limitation. |

**The 09:30 rule is a guard, not a calendar.** The rule ("nothing closes at or before 09:30 New York") is a negative guard. It is exact on M16 provider data. It cannot detect a start-stamped partial day with no bar at or before 09:30. It rejects valid pre-market close stamps. It proves nothing about holidays or completeness. Positive acceptance always needs the calendar conversion, and the code does not use the rule as a calendar substitute.

**Parity.** Scanner replica ↔ backtester (`test_m21_research_genome` trade-for-trade), WP8 research ↔ bot templates (`T2_ResearchParity`), WP6 replica parity (`N3_ReplicaParity`) and WP11 `parity_check`. All of it is **synthetic**; real-bar parity is OP1 (B-1).

**Holiday inference (WP5).** A weekday missing from the Alpaca calendar rows is treated as a holiday. A truncated source response would therefore look like holidays. OP1 compares the rows with the published NYSE holiday list.

**G9.** The flake was a reproducibility defect in the report writer, not in the data path. It is fixed (N9).

## 6. Promotion verdict (#23)

**Verdict: READY FOR MERGE REVIEW.** All ten proof points are tested on the final head `04bf5ca`. The design is unchanged except for the CAS and verification additions; nothing was redesigned.

| # | Required proof | Tests |
|---|---|---|
| 1 | A failed activation cannot create a valid-looking completed promotion | `test_04`, `test_13`, `test_14`, `test_15`, `test_12d` |
| 2 | A failed DB commit cannot silently lose the change budget | `test_03` (the op stays PENDING and blocks every new apply; reconcile records APPLIED, so the budget counts) |
| 3 | Startup reconciliation never reapplies an unsafe change | `test_01` (never-activated → ABORTED, orphan withdrawn), `test_12c`, `test_15`; settle never activates |
| 4 | An unresolved store contradiction blocks further promotion | `test_10`, `test_12`, `test_12b`–`test_12e` |
| 5 | Duplicate or simultaneous requests cannot bypass one-active-change | `test_07`, `test_08`, `test_database_level_guards`, `test_13` (CAS) |
| 6 | Operator recovery is fully audited | `test_12` (BLOCK_RESOLVED with the named human; refusals audited); controls refusals audited (`*_REFUSED`) |
| 7 | A forged or replayed approval cannot authorise a change | `test_only_the_exact_proposal_tier_and_window_verify`, `test_forged_or_mismatched_tokens_are_refused`, `test_record_shadow_checks_state_and_tokens_cannot_be_reused` |
| 8 | A failed alert does not invalidate the recovery record | `test_11`, `test_05`, `test_failed_alert_does_not_undo_the_revert` |
| 9 | A config rollback cannot modify broker orders | `P6_Isolation` (no broker/order/messaging code; the controls feed only the shadow runner), `test_auto_revert_rolls_back_alerts_and_never_cancels_orders` |
| 10 | Recovery is idempotent across restarts | `test_16` (four restarts), `test_06` |

**Check-then-act across the two databases.** Every cross-store step now re-checks the precondition inside the target store's own write transaction:
- the activation CAS on the pre-apply version;
- the revert CAS on the applied version.

A change that lands in between is refused there, audited, and blocks the op; it is never overwritten (`test_13`, `test_17`).

**The one remaining gap is benign and documented.** The machine's own `active == from_version` check before `activate` is now redundant with the CAS. The mutant that removes it is equivalent.

## 7. Candidate / M14 verdict (#17)

**Verdict: READY FOR MERGE REVIEW.**
- **Real WS2 caller.** The scheduler gate is `make_risk_authority_gate` → `run_risk_preflight` → `decide_and_audit`, which commits `risk_decisions` and returns `(allowed, why, decision_id)`. The scheduler passes `decision_id` and `notional_usd` to the ledger. The tool wires `LoopLedger(writer, m14_db=signals_db)`.
- **Execution permission.** Only `M14_LIVE` permits `intent()`. M14_LIVE requires the decision read back from the store and matched on:
  - id and provenance;
  - symbol and direction;
  - notional (the order's `notional_usd`, else qty × entry price);
  - leverage 1, scope `ibkr_paper`, action `trade_open`;
  - result `allow`;
  - time within ±10 minutes of the tick.

  Missing evidence → blocked. Forged id, other symbol, or no store → UNVERIFIED: no link, no intent (`W8_RealM14GateProvenance`, through the real engine).
- **Historical reconstruction is kept apart.** `VERIFIED` (a reconcile backfill matched in the store), `RECONSTRUCTED_INFERRED` and `UNVERIFIED` never permit execution (`bf94f71` uses a fresh candidate, so the test cannot pass by accident).
- **Replay, duplicates, contradictions, restart:**
  - repeated ticks and restarts never duplicate (`W4`);
  - contradictions are counted, never repaired, and terminals are immutable;
  - a legacy fabricated backfill is classified INFERRED;
  - the M14 reader is read-only.
- **Residual, documented rather than changed.** The ledger does not yet constrain a decision id to a single candidate. Reusing one id for a second candidate with the same symbol, side and notional inside the ±10-minute window would therefore verify. It is **unreachable through the WS2 caller**:
  - the gate persists a fresh decision on every entry attempt;
  - the scheduler passes that id straight through;
  - at most one entry is attempted per tick.

  A unique-link guard is optional hardening (≈ 0.25 d). It is listed under E-items only if you want it.
- **Operational chain proven end to end on fixtures** (integration E4): scanner signal → WP5 evidence → M19 score → real M14 gate → ledger `M14_LIVE` → journal → simulated paper broker → outcome, under one candidate id, with `account()` and `journal_consistency` clean. A forged id never reaches the broker.

## 8. Walk-forward / canary verdict (#24)

**Verdict: READY FOR MERGE REVIEW. v3 is prepared, NOT run.**
- **v2 is unchanged and preserved** byte-for-byte (`docs/research/evidence/wp13_canary_v2_historical_609aa85.json`, sha256 `7a6760a1…`):
  - 0/59 null passes (upper bound 4.95 %); 20/20 detections (lower bound 0.83);
  - 30 symbols × 750 sessions; 0.5σ `rsi_oversold`; horizon 5.
- **The corrected code does not inherit CALIBRATED.** The report card needs an experiment identity equal to the evaluation's `canary_experiment`. v2's plan has none, and its ledger was not exported. Its `pipeline_id` used the decision-code digest, while WP13 trials carry the search-code digest. Test: `test_historical_v2_design_is_preserved_and_v3_differs_only_in_its_seeds`, plus the report-card refusals.
- **v3 is preregistered** (`canary.PROTOCOL_V3`; identity schema `/2`). **Expected experiment digest: `01b3f4328fd1c5601a59f1da0f80bfe5ee681fde5c4d39f0bed7aff21be0f262`.** It is identical on #24 `4b677bc`, #26 `89bc874`, the integration tree and the trial-merged `main`; no other PR touches its code paths.
- **One-shot procedure** (`docs/research/WP13_canary_preregistration_v3.md` §4):
  - once, on the merged `main` commit after #19 → #20 → #24 (and #26) merge;
  - offline, `OMP_NUM_THREADS=1 python -m research.optimiser.canary --template mean_reversion --out "$OUT"`, with no design flags;
  - about 45–60 minutes;
  - run acceptance: `ledger_ok`, JSONL verification, the digest above, design = `PROTOCOL_V3`;
  - preservation: a docs-only PR under `docs/research/evidence/wp13_canary_v3_<sha>/`;
  - reporting: one comment on Issue #3 and one on PR #24.
- **Decision rule, fixed now:** the verdict is final. No retuning and no second run. If it is not CALIBRATED, real-data walk-forward cannot PASS until a new design is preregistered with your approval.

## 9. Economics and context verdicts

- **#19 economics — READY WITH DOCUMENTED LIMITATIONS.** Verified: fees (IBKR minimum and maximum, SEC/TAF on sells, by date), splits, dividends, borrow states, ADV and bar caps, NO_FILL vs INCONCLUSIVE.
  - Unknown liquidity, halted or zero-volume entries, and unavailable or unknown borrow → NO_FILL.
  - Raw prices without corporate actions, or an untradable exit → INCONCLUSIVE.
  - **Assumed, labelled as assumptions:** spread, impact and slippage coefficients, the easy-borrow rate, and `exit_liquidity_unknown_used_entry` (flagged).
  - Halts are inferred from zero-volume bars only (no halt feed).
  - Fee rates must be re-verified at OP3. Calibration against paper fills (`analysis.fee_calibration`) waits for real fills (E5).
- **#25 context — READY WITH DOCUMENTED LIMITATIONS** after N11/N12.
  - Point-in-time semantics: `published_at_utc` ≤ t, newest vintage published by t, append-only revisions with a later publication.
  - Supported sources: FRED VIXCLS (parser), ETF/breadth (derived), official FOMC/CPI/NFP schedules (operator file, with announcement time).
  - **EARNINGS: UNAVAILABLE** (G4).
  - The throttle is shadow-only and reduce-only; an outage abstains and never unblocks.
  - **Not wired into WS2 logging yet** (E2).
- **#26 shadow ML — READY WITH DOCUMENTED LIMITATIONS.**
  - OOF only, from purged and embargoed expanding folds.
  - Thresholds come from past folds **resolved before** the fold's first decision (delayed labels).
  - Size multipliers are in {0, 0.5, 1} and never increase size.
  - A missing regime → `unknown` → global threshold; baselines are take-all and global.
  - INCONCLUSIVE below the minimum number of events; `can_change_live` is False.
  - Synthetic findings are preserved (meta-labelling WORSE; regime NO_EVIDENCE). Real runs are DATA_UNVERIFIED.

## 10. Templates and paper-loop isolation verdict

| PR | Verdict | Basis |
|---|---|---|
| #15 WP5 | READY WITH DOCUMENTED LIMITATIONS | Calendar and corporate-action fetches fail closed on missing credentials, transport errors, non-2xx, bad JSON, bad rows or range, and incomplete pages; secret-safe; read-only. Liquidity is UNKNOWN unless 20 completed sessions are present. Real responses are DATA_UNVERIFIED (OP1). Holiday inference trusts source completeness (§5). |
| #16 WP6 | READY FOR MERGE REVIEW — **BLOCKED_BY_APPROVAL (C5b)** | The default path is unchanged (`bar_basis` defaults to `in_progress`). The completed basis is opt-in and emits no signals without a policy. `adjustment="raw"` equals Alpaca's documented default. No new execution path. See §14. |
| #17 WP7 | READY FOR MERGE REVIEW | §7. The ledger is OFF unless `CANDIDATE_LEDGER_ENABLED`; enabled-and-unrecordable blocks the entry, never the loop. |
| #21 WP8 | READY FOR MERGE REVIEW | `LIVE_TEMPLATES = {TEMPLATE_1}` is a constant. Templates 2–4 go to shadow only; `to_loop_candidate` refuses non-live templates; `merge_tradable` is not wired, because only one live template exists. The shadow runner is OFF unless `TEMPLATE_SHADOW_ENABLED=1`, cannot change candidates, and any failure in it is logged and ignored. Research ↔ bot parity is tested on identical bars. |
| #22 WP9 | READY FOR MERGE REVIEW | Bounds validated; immutable versions with digests; human approval, at most once, never by automation; CAS activation and rollback; every refusal audited; the read path never writes and is read only by the shadow runner. **The dashboard UI is not built** (protected file, §14). |
| #18 WP10 | READY WITH DOCUMENTED LIMITATIONS (after N10) | Persistent alarms; CRITICAL latches until acknowledged after clearing; no re-notify on restart; a failing sink is recorded and never changes state; UNKNOWN never reads healthy; read-only journal probe. **Limitations:** no messaging sink (LogSink/MemorySink only); `duplicate_orders` is UNKNOWN until broker open orders are supplied (OP2); sinks are called inside the DB transaction (a slow sink holds the write lock); the drill is B-6 (RUNTIME_UNVERIFIED). |
| #23 WP14 | READY FOR MERGE REVIEW | §6. |

**Static isolation.** Only #12 and #16 change protected files (§14). No M21 package adds broker or order code. Network access is limited to WP5's read-only Alpaca calendar and corporate-action fetchers, plus the existing WS2 paper adapter (#1). No PR touches `.github/`, deploy scripts, requirements or Vercel configuration. The only workflow on `main` is manual-dispatch (`m21u4_europe_source_audit.yml`).

## 11. Integration SHA and tree

- **`m21/integration` = `79ae46f5dc1bc9c886dcb76191373718c35d68d1`, tree `784355336e7845098335801da5bce283daecec9b`.**
- **Sequential trial merge** of all 16 heads onto `main` `80b4343` (local only, never pushed) = tree **`f1ff536e62e7c6479d07d7f404db49c4cce342e8`**. It is the same tree in either lane order (L then R, or R then L).
- `git diff f1ff536 7843553` = **only** `test_m21_e2e_cross_lane.py` (integration-only, 27 tests: 7 positive chain, 17 negative, E4 ×3) and its entry in the two G10 allow-lists (`test_m17`, `test_m18`); the G10 checks also pass on the trial tree itself.
- **The full suite on the integration tree therefore tests exactly the post-merge `main`, plus the E2E file.**
- Every PR head is an ancestor of the integration head. Every lane's owned files match their branch tips.

## 12. Full test evidence

**Environment:**
- Python 3.12.3, NumPy 1.26.4, pandas 2.2.3, SQLite 3.45.1, scikit-learn 1.8.0, SciPy 1.17.1, Flask 3.1.0, pyarrow 24.0.0;
- 2 CPUs; `OMP_NUM_THREADS=1`;
- C5a isolation;
- a detached checkout **outside `/tmp`**;
- no network, broker or VPS.

| Run | Commit / tree | Command | Result |
|---|---|---|---|
| **Full suite** | `79ae46f` / `7843553` | `python _test_isolation.py --all` | **129 modules: 128 OK, 0 failed, 1 without tests (test_m12, by design)** |
| L-lane tip (#23) | `a8b6f3c` / `ffbc10b` | 27 guards + WP5, WP6, WP7, WP8, WP9, WP14, M14 E/F | 35/35 OK (the final #23 head `04bf5ca` adds one test-only commit; WP14 module re-run OK, and it is inside the full suite) |
| R-lane tip (#26) | `89bc874` / `bd1682e` | 27 guards + 11 research suites + Group F + M17 | 40/40 OK |
| #18 | `9012711` / `c9c77bd` | 27 guards + WP10 | 28/28 OK |
| #25 | `f05ed84` / `b0f95eb` | 27 guards + WP15 | 28/28 OK |

The 27 guards are:
- A01, the bridge, group D, the A-path, alerts, B1, A02;
- learning loop, D1 guard, D1 Alpaca source, D2 gates/dry-run/lifecycle, D3 metrics;
- registry shadow-run, hygiene, quarantine, C5b, C5a, G10 ×2, research foundation, paper-port guard, C1/C2/C3 kill-switch compositions, scanner isolation.

**Superseded run, disclosed.** The first full run, on the earlier integration head `bda23c3`, failed one test: `test_m17_backtesting.G10_Hygiene.test_no_unexpected_files_added`. My resolution of the integration merge had dropped the integration-only E2E file from the G10 allow-list. `91fffef` restored that entry on the integration branch only (no PR changed); that corrected head then passed the full suite (129 modules: 128 OK, 0 failed, 1 without tests). The final head `79ae46f` adds only the #23 test commit `04bf5ca`, and the run above is on it.

**Known flakes:** none observed this round; G9 is fixed. `test_m12` has no tests by design (operator `--live` script; never run). One environmental note: `test_m21_1extra_d2_3_learning_loop…test_simulated_mode_tmp_only` fails when run from a checkout under `/tmp`, by design (it asserts a non-`/tmp` path). The evidence runs use a checkout outside `/tmp`.

**Evidence files** (workspace): `evidence/final4_integ_all.txt`, `final_L_wp14.txt`, `final_R_wp16.txt`, `final2_wp10.txt`, `final2_wp15.txt`; each starts with commit, tree, interpreter and command.

## 13. Merge order (do not merge until approved; no auto-merge, squash, rebase or force-push)

**Merge method: "Create a merge commit" for every PR.** Squash or rebase would orphan the stacked heads.

After each merge, the new `main` tree must equal the expected tree below:

```bash
git fetch origin && git rev-parse origin/main^{tree}
```

Any difference → stop.

| Step | PR | Head | Base at merge time | Expected `main` tree after merge | Required before merging |
|---|---|---|---|---|---|
| 1 | #12 | `47e9057` | `main` | `a31b0183` | review; **C5b** (scanner metadata, §14) |
| 2 | #1 | `0f3f9e6` | `main` | `9c57eb85` | review |
| 3 | #14 | `68ca7b4` | retarget → `main` | `1da598c6` | retarget, then confirm the PR diff lists only A02 files |
| 4 | #13 | `6a8a690` | `main` | `a1aa8dd6` (= `m21/stack-base` tree) | review (doc only) |
| 5 | #15 | `5394882` | retarget → `main` | `9a21419e` | review |
| 6 | #16 | `bf92678` | retarget → `main` | `ab74aab1` | review; **C5b** (scanner + Alpaca, §14) |
| 7 | #17 | `bf94f71` | retarget → `main` | `afa93221` | review |
| 8 | #21 | `fbfc815` | retarget → `main` | `6dac7881` | review |
| 9 | #22 | `b4e9955` | retarget → `main` | `f08b79e1` | review |
| 10 | #23 | `04bf5ca` | retarget → `main` | `fa238daa` | review |
| 11 | #18 | `9012711` | retarget → `main` | `fc763db4` | review |
| 12 | #19 | `d7b8e66` | retarget → `main` | `c1ff0ca9` | review |
| 13 | #20 | `4924844` | retarget → `main` | `2fe8a312` | review |
| 14 | #24 | `4b677bc` | retarget → `main` | `4959afc5` | review |
| 15 | #26 | `89bc874` | retarget → `main` | `1153db9a` | review |
| 16 | #25 | `f05ed84` | retarget → `main` | **`f1ff536e`** (final) | review |

**Batches:**
- **B1 foundation:** steps 1–4.
- **B2 L lane:** steps 5–11; needs C5b at step 6.
- **B3 R lane:** steps 12–16. It is independent of B2: if C5b is withheld, B3 can go first. Its tree after step 16 is then different, but the final tree after both lanes is the same `f1ff536`.
- **Then** the v3 canary (§8) and WP19.

**Commands** (operator, at each step; `gh pr ready` only when you approve that PR):

```bash
gh pr ready <n>
gh api -X PATCH repos/mikemitakidis/algotrading/pulls/<n> -f base=main     # retarget steps only
gh pr diff <n> --name-only                                                 # must list only that PR's files
gh pr merge <n> --merge                                                    # merge commit; never --squash/--rebase/--auto
git fetch origin && git rev-parse origin/main^{tree}                       # must equal the expected tree
```

- **Retargeting rule:** retarget only right after the base PR merges, then check that the PR diff lists only its own files (counts per PR are in the table below). A non-matching diff → stop and ask for re-verification.
- **No PR head may change between review approval and merge.** If one does, its expected trees no longer hold and must be recomputed.

**Per-PR merge facts** (files: total / code / test, relative to the PR's base):

| PR | Files | Protected files | Safety notes | Required tests | Deployment side effects |
|---|---|---|---|---|---|
| #12 | 19 / 10 / 8 | `bot/scanner.py` (metadata), `_protected_baselines.py` | A-path, B1, alerts and WS2 selection fail closed until WP5 merges (nothing runs in production) | A01 + guards | see Vercel below |
| #1 | 11 / 8 / 3 | none | adds the WS2 loop and IBKR **paper** adapter; port 4002 guard; inactive until OP3 | WS2 suites | none at runtime (not scheduled) |
| #14 | 12 / 8 / 3 | none | the ledger writer is OFF; no migration runs | A02 | none |
| #13 | 3 / 0 / 2 | none | design doc | — | none |
| #15 | 9 / 6 / 3 | none | read-only fetchers; fail-closed | WP5 | none |
| #16 | 12 / 7 / 5 | `bot/scanner.py`, `bot/providers/alpaca_provider.py`, `_protected_baselines.py` | default behaviour unchanged | WP6 + C5b guard | none |
| #17 | 11 / 8 / 3 | none | M14_LIVE only; the ledger is behind its flag | WP7 | none |
| #21 | 11 / 8 / 3 | none | shadow behind its flag; no routing | WP8 | none |
| #22 | 5 / 2 / 3 | none | controls feed only the shadow runner | WP9 | none |
| #23 | 8 / 5 / 3 | none | no broker code | WP14 | none |
| #18 | 7 / 4 / 3 | none | read-only probe; no messaging | WP10 | none |
| #19 | 7 / 4 / 1 | none | research only | economics | none |
| #20 | 13 / 8 / 3 | none | real-data entrances fail closed | readiness, M17, M18, Group F | none |
| #24 | 11 / 6 / 1 | none | research only | optimiser, integrity | none |
| #26 | 5 / 2 / 1 | none | shadow, `can_change_live` False | shadowml | none |
| #25 | 8 / 5 / 3 | none | point-in-time; shadow throttle | WP15 | none |

**Vercel.**
- A Vercel project named `algotrading` is linked to this repository: a read-only project listing found exactly one, under a team account. Its build settings and deployments are **not readable** with the available connector (403).
- The repo has no `vercel.json`, no `package.json` and no `api/`. What a build of `main` produces is therefore unknown.
- **Assume every merge to `main` triggers a Vercel production deployment of this repo.** It cannot touch the VPS bot or the dashboard (served by the VPS, `algotrading.marketwarrior.club`), and it carries no secrets from this repo.
- Before step 1, look at the Vercel project once and decide whether its production builds should continue. Changing that is a Vercel setting: your action, not Claude's.
- No PR changes CI; the only workflow is manual-dispatch.

## 14. Protected-file requests (approval needed; nothing modified)

### 14a. C5b sign-off — `bot/scanner.py` (#12, #16) and `bot/providers/alpaca_provider.py` (#16)

| File | `main` blob | After #12 | After #16 | Recorded in `_protected_baselines.py` |
|---|---|---|---|---|
| `bot/scanner.py` | `4317e57e72f9d8d862607a8e3f155cc53f4ecd3d` | `08dc7512f019ae9fa1706dcf7776c2b2036e1ee3` | `581f2a7cb40b1b372b443cd416826d16b85a58b7` | chained: `also_from` = `4317e57`; commit `a49375a`; "PENDING independent review" |
| `bot/providers/alpaca_provider.py` | `944f3918c0b10a60edca5e68dc3c4d153a1e9a93` | unchanged | `0d756dd3dbe899bc7aa42a2a317c4385fc562a4e` | commit `a49375a`; "PENDING independent review" |
| `bot/signal_scoring/gates.py`, `bot/brokers/ibkr_broker.py`, `dashboard/app.py` | `7c94181`, `08640cd`, `564fc81` | unchanged | unchanged | unchanged |

**Behaviour, before → after:**
- **#12 (`scanner.py`, +12 lines):** metadata only. `scan_cycle` meta gains `bar_asof` (the UTC start of the bar each timeframe scored). Signals and decisions are identical. `_bar_start_iso` never raises.
- **#16 (`scanner.py`, +32/−1):** an opt-in `bar_basis`.
  - **Default `in_progress`: identical to today** (same bars, same `now_utc` from the wall clock).
  - `completed`: regular-session rows only, and the scored bar must be proven complete at the decision time by an injected policy. `now_utc` = the policy's decision time. Meta gains `bar_basis` and `bar_selection`.
  - An unknown basis, or `completed` without a policy → **no signals** (`blocked_bar_basis`).
- **#16 (`alpaca_provider.py`, +8):** both bar requests send `adjustment="raw"` (constant `BAR_PRICE_ADJUSTMENT`). Raw is Alpaca's documented default, so prices are expected to be unchanged; this is VPS VERIFICATION REQUIRED in OP1, by one read-only request with and without the parameter.
- **No new execution path:** no broker, order or network imports were added. `bot.bar_completion` is stdlib-only.
- **Tests:** `test_m21_c5b_protected_baselines` (the guard accepts exactly these blobs); WP6 (19 tests): `test_default_mode_unchanged_and_completed_needs_a_policy`, `N6_ProtectedTransitions` (each diff is exactly the seam / the raw parameter), `test_main_py_does_not_select_the_completed_basis` (production stays on the default until you decide), `N3_ReplicaParity`; group D scanner; scanner isolation.

**Request:** approve the three listed transitions (#12 metadata; #16 seam; #16 raw adjustment), so the approval text can be recorded in place of "PENDING independent review" at merge.

### 14b. Dashboard — `dashboard/app.py` (no change made)

- **Need:** A16 Phase-1 controls UI (WP9) and the template view of the D3 panel.
- **Proposed minimal protected change:**
  - one blueprint registration line and one nav link in `dashboard/app.py`, the same pattern as the approved D3b change;
  - all UI and API code in a new, unprotected `dashboard/template_controls.py`;
  - auth-required; read views plus POST propose / approve (named human) / activate / rollback through `bot.templates.controls` (CAS); no order, routing or sizing endpoint.
- **Request:** approve that shape. The code follows as its own PR (engineering item E6), and the dashboard restart stays an OP3 step.

## 15. Remaining requirements (definition of done)

Statuses: MERGED_AND_TESTED / IMPLEMENTED_OFFLINE / READY_FOR_REVIEW / DATA_UNVERIFIED / RUNTIME_UNVERIFIED / BLOCKED_BY_APPROVAL / INCOMPLETE.

| Requirement | Status | Evidence / what is missing |
|---|---|---|
| M21.U universe foundation | MERGED_AND_TESTED | historical closeout |
| M21.1 scoring (A01) | READY_FOR_REVIEW · RUNTIME_UNVERIFIED | #12; real scanner sample via OP1 |
| M21.1 candidate audit (A02) | READY_FOR_REVIEW · RUNTIME_UNVERIFIED | #14, #17; migration and flag = OP3 |
| Truthful evidence wiring (WP5) | READY_FOR_REVIEW · DATA_UNVERIFIED | #15; real Alpaca calendar and actions in OP1 |
| Completed candles (WP6) | READY_FOR_REVIEW · BLOCKED_BY_APPROVAL | #16 C5b |
| WS2 loop (A03/A04/A09) | READY_FOR_REVIEW · RUNTIME_UNVERIFIED | #1; OP2/OP3 |
| IBKR paper adapter (A10/A11/A13) | BLOCKED_BY_APPROVAL | OP2 |
| Unattended scheduling (A12) | BLOCKED_BY_APPROVAL | OP3/OP4 |
| Monitoring minimum (A14) | READY_FOR_REVIEW · RUNTIME_UNVERIFIED | #18; drill B-6 |
| Templates in the live path (A15) | READY_FOR_REVIEW | #21 (Template 1 live; 2–4 shadow) |
| Phase-1 controls (A16) | READY_FOR_REVIEW (backend) · **INCOMPLETE (UI)** | #22; **E6** dashboard UI (protected) |
| D3 per strategy/template (A05/A06) | IMPLEMENTED_OFFLINE · RUNTIME_UNVERIFIED | dashboard restart = OP3 |
| Real-bar parity, coverage (A17/B01/E05) | IMPLEMENTED_OFFLINE · DATA_UNVERIFIED | #20; OP1 |
| Economics (B10) | READY_FOR_REVIEW · DATA_UNVERIFIED | #19; **E5** fill/fee reconciliation |
| Walk-forward + report card (B11/B12) | READY_FOR_REVIEW · **INCOMPLETE (report-card wiring)** | #24; **E1** |
| Integrated canary (B08) | READY_FOR_REVIEW | v3 prepared; runs after merges |
| Promotion & approval (B13, B23 part) | READY_FOR_REVIEW · RUNTIME_UNVERIFIED | #23; OP5 |
| Meta-label / regime / weights (B16/B17/B19) | READY_FOR_REVIEW · DATA_UNVERIFIED | #26; real runs after OP1 |
| Market context (B18) | READY_FOR_REVIEW · **INCOMPLETE (WS2 logging)** · DATA_UNVERIFIED | #25; **E2**; G4 |
| Converted-input M17 multi-TF / M18 | **INCOMPLETE** | **E3** |
| Score-vs-outcome (§5 loop, C-4) | **INCOMPLETE** · RUNTIME_UNVERIFIED | **E4** report; needs 20 paper days |
| Paper fill-cost reconciliation (C-4) | **INCOMPLETE** · RUNTIME_UNVERIFIED | **E5**; needs paper fills and the statement |
| Docs reconciliation (E08 / WP19) | **INCOMPLETE** | **E7**, after merges |
| OP1–OP5, full operational acceptance (B-gate) | BLOCKED_BY_APPROVAL | §16, §17 |
| C-gate (performance, integrity, governance) | RUNTIME_UNVERIFIED | after OP4/OP5 |

**Exact remaining-work count: 18 items.**

1. **7 engineering items, all small integration work; none is a major missing implementation:**

   | Item | Dev-days |
   |---|---|
   | E1 walk-forward → report card | 1.0 |
   | E2 WS2 market-context shadow logging | 1.0 |
   | E3 converted-input path for M17 multi-TF and M18 | 1.5 |
   | E4 score-vs-outcome report from ledger joins | 1.0 |
   | E5 fill/fee reconciliation into `fee_calibration` | 0.75 |
   | E6 dashboard controls UI (after 14b) | 1.5 |
   | E7 WP19/E08 | 1.0 |
   | **Total** | **≈ 7.75** |

2. **1 merge batch** of 16 PRs (§13).
3. **3 evidence runs:**
   - the v3 canary;
   - real-data reruns of WP11/WP13/WP16–18 after OP1;
   - the OP1.7 statement reconciliation.
4. **5 operations:** OP1–OP5.
5. **2 reports:** C-4 and C-5.

## 16. OP1 and old-paper plan (prepared; NOT executed)

The runbook is `docs/runbooks/M21_OP1-OP5_Runbooks_2026-10-28.md`, revised 29 Oct.

**OP1.1–OP1.5 — read-only.**
- Every command reads with `mode=ro`, `.backup` to `/tmp/m21_op1`, `stat`, or names-only `grep`.
- Nothing writes under `/opt/algo-trader`, no `.env` values are read, no service is restarted and no broker is connected.

**OP1.6 — old paper system since 1 June.** The SQL is rewritten so that the **nine categories are reported separately**:
1. local simulations;
2. candidate signals (`signals`, `candidate_snapshots`);
3. order intents;
4. journal-reported submissions;
5. broker acknowledgements;
6. real IBKR paper executions;
7. closed paper trades;
8. fees/commissions;
9. unexpected live activity.

The SQL was executed statement by statement through a **read-only** connection on schemas created by the real code (10/10 and 2/2), and a write was refused. It has not been run on any real DB.

**OP1.7 — IBKR statement reconciliation (new).** **DB rows are not proof.**
- You export the paper Activity/Flex statement and the live Trades section yourself; Claude never logs in.
- Claude matches offline on orderRef, then permId, then symbol/side/qty within ±2 minutes, and classifies each row MATCHED / MISMATCH / UNMATCHED_CLAIM / UNATTRIBUTED_EXECUTION.
- Closed trades = matched entry and exit. Fees come from the statement only.
- Any live execution is reported to you.

**Stop conditions:** any live-scope attempt; a non-paper scope holding positions; any write or unexpected error.

## 17. OP2/OP3 readiness

**Not started.** A one-page acceptance gate (16 checks) is now in the runbook:
- account/port isolation;
- broker readiness;
- trusted clock;
- M14 preflight (`M14_LIVE`);
- kill-switch drill;
- order reconciliation;
- no duplicates;
- partial fills (RUNTIME_UNVERIFIED: no offline test covers the timeout path);
- bracket orderRef;
- foreign/manual orders (tested offline: never attributed or cancelled);
- exposure/P&L freshness;
- monitoring;
- ledger migration (approval + `.backup`);
- dashboard restart (approval);
- abort lists;
- per-step rollback.

**Preconditions:**
- **OP2:** #1 merged; gateway health proven (last seen: **failed**).
- **OP3:** #15–#17 and #18 merged; `--min-score` set from OP1 data (I1).

## 18. G4 data and licensing decision matrix (free first; no purchase made)

| Need | Free option | Paid option (not bought) | Recommendation |
|---|---|---|---|
| Historical bar sufficiency | M16 store on the VPS (yfinance, already held); **Alpaca Basic: history since 2016, 200 calls/min, IEX real-time, the latest 15 min of historical data restricted** | Alpaca Algo Trader Plus $99/month (all exchanges, no restriction) | Free: measure coverage in OP1 first |
| Survivorship / delisted symbols | none reliable free (Yahoo/Alpaca lack most delisted symbols) | Norgate US stocks ≈ $35/month (delisted coverage) | **Free + quantify the bias** (C-5) for M21; revisit before M22 |
| Adjusted vs unadjusted | raw bars + Alpaca corporate actions (WP5, `adjustment=raw` explicit) | — | Free; keep raw plus explicit actions |
| Corporate actions | Alpaca corporate-actions API (paginated; fail-closed) | — | Free; verify in OP1 |
| VIX / context | FRED VIXCLS (FRED/CBOE terms) | Cboe data | Free; observed-retrieval timing, or a ≥ 22 h assumed lag |
| ETF / breadth | Alpaca ETF bars; breadth from our own universe | — | Free |
| Macro calendars | official FOMC (federalreserve.gov), CPI/NFP (bls.gov) schedules, kept as an operator file | — | Free |
| Earnings | Alpha Vantage free key (25 requests/day, 5/min); whether its earnings calendar is on the free tier is **unverified** | vendor feeds | Verify at G4; until then EARNINGS stays UNAVAILABLE (fail-closed) |
| Licensing | yfinance/Yahoo: personal-use terms; FRED: series-specific terms; Alpaca: account terms | — | Research and internal use only; no redistribution |

## 19. Remaining effort

- **Engineering:** ≈ 7.75 dev-days (E1–E7) + ≈ 3 review days. That is down from ≈ 13–16 + reviews on 28 Oct, because the audit found no missing major implementation.
- **Calendar estimate** (assumes one review round per PR, G2/OP1 by about 3 Nov, and C5b approved this week):

  | Gate | Planned | Range |
  |---|---|---|
  | A. Engineering complete | **≈ 16–18 Nov 2026** | 12–25 Nov |
  | B. Operational acceptance | **≈ 1–5 Dec 2026** (driven by the 10-day soak after OP3) | late Nov – mid-Dec |
  | C. Full M21 acceptance | **≈ 22 Dec 2026 – 8 Jan 2027** (20 paper trading days, plus OP5) | unchanged |

- This is an estimate with its assumptions stated, not a deadline.

## 20. Approvals, grouped

| Gate | Decision | Recommendation |
|---|---|---|
| **G-review** | Independent exact-head review of all 16 PRs at the heads in §1 (deltas this round: #17, #22, #23, #18, #20, #24, #25; merge-only: #21, #26) | Review now |
| **C5b** | §14a: #12 scanner metadata; #16 scanner seam and Alpaca `adjustment=raw` | Approve |
| **G3a — merges** | §13 batches B1 → B2 → B3, merge commits, tree check after each | Approve after G-review |
| **Dashboard** | §14b: blueprint plus one registration line in `dashboard/app.py`; UI as PR E6 | Approve the shape |
| **G2 — OP1** | Read-only VPS session incl. OP1.6; you provide the IBKR statement exports for OP1.7 | Approve; it unblocks real-data work |
| **G3b — OP2/OP3** | Paper gateway session, then activation, per the gate in §17 | After B1 + B2 merge and OP1 |
| **G4 — data** | §18: free sources; survivorship quantified, not bought; earnings source verified | Approve (a) |
| **Vercel** | Decide whether production builds of `main` should continue (§13); your setting | Decide before step 1 |

**Claude stops here until these decisions arrive.**
