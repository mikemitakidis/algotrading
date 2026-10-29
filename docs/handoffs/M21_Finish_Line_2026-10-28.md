# M21 Finish Line — requirements, remaining work, estimate, merge readiness, decisions

**Date:** 28 Oct 2026, updated 29 Oct (Europe/London) · **Repo:** mikemitakidis/algotrading · **main:** `80b4343db4a22413aa670efd32e24952d53e532e`
**Scope source:** the 29 June 2026 canonical roadmap (“Algo_Trading_Bot_Canonical_Roadmap.docx”, v2.0) — sections M21.U, M21.1, M21.1extra, M21.2–M21.7, §5 learning loop, §8 safety rules, Appendix A. Task IDs (A01–E09) are from the canonical handoff register; **all 71 stay in the register** (see §4).
**Status of this file:** the single authoritative M21 checklist (§6). It replaces separate plan documents; the canonical handoff links here.

---

## 0. In plain words (one screen)

> **Update, 29 Oct (evening) — release preparation (§7d).**
> - The 16 PR heads are re-verified unchanged; they still merge onto `main` `80b4343` conflict-free to tree `f1ff536`.
> - E1–E7 are delivered as draft PRs #27–#33, stacked on `m21/rc-base` (= that exact post-merge tree). With them the
>   proposed `main` tree is `1ed8c0f`; the tested integration tree is that plus the E2E test only.
> - E6's protected `dashboard/app.py` wiring is a prepared, verified patch — **not applied**.
> - OP1 is final: scripts verified read-only on a synthetic replica of the VPS, minimum permissions, the IBKR Flex
>   specification and the E5 reconciliation. It has not been run.
> - Vercel: every push to any branch creates a Preview deployment and every push to `main` a Production deployment
>   (GitHub deployment records); the project settings are not readable from here.
> - Nothing is merged; `main` is unchanged; every new runtime path is OFF.

> **Update, 29 Oct — final engineering acceptance.** The full record is `docs/reviews/M21_Final_Engineering_Acceptance_2026-10-29.md` (§7c below).
> - All 16 PRs merge onto `main` in order with **zero conflicts**. The result is tree `f1ff536`, which equals `m21/integration` `79ae46f` minus its E2E file and that file's two allow-list entries. Full suite on the integration tree: 129 modules: 128 OK, 0 failed, 1 without tests (test_m12, by design).
> - 13 new defects were fixed: 3 high (#17 forged M14 id, #22/#23 activation race, #25 timezone lookahead) and the #18/#25 merge conflict. None critical/high remains open.
> - Remaining: 7 small engineering items E1–E7 (≈ 8 dev-days), the merges (C5b for #12/#16), the v3 canary run, and OP1–OP5.
> - Estimate: engineering ≈ 16–18 Nov; operational ≈ 1–5 Dec; full acceptance ≈ 22 Dec – 8 Jan.

> **Update, 28 Oct (late), after the independent review.**
> - All three material findings are fixed, each on its owning PR:
>   - #23: cross-store promotion consistency;
>   - #17: no fabricated M14 approvals;
>   - #24: canary bound to a full experiment identity.
> - The audit fixed four more defects:
>   - M17/M18 multi-timeframe reads of start-stamped M16 bars (lookahead) now fail closed;
>   - the walk-forward refuses unconverted real bars;
>   - market context refuses start-stamped bars;
>   - unknown-exit trades can no longer support a walk-forward PASS.
> - Details, SHAs and test evidence: `docs/reviews/M21_Review_Corrections_2026-10-28.md` (§7b below).
> - Nothing is merged; `main` is unchanged.
> - Every item below that needs real data or a real runtime is marked **DATA_UNVERIFIED** or **RUNTIME_UNVERIFIED** (§6). Synthetic tests do not make it "done".

> **Update, 28 Oct (evening), after the accelerated program.**
> - **Code now exists for every remaining M21 work package**: WP5–WP18 and the cross-lane test. It sits in 12 draft PRs (#15–#26), all offline and all unmerged. `main` is unchanged.
> - **What is left:**
>   - review rounds and merges;
>   - the operations OP1–OP5 (your gates);
>   - re-running the research packages on **real** data after OP1;
>   - the dashboard UI for templates and controls (a protected-file change);
>   - WP19.
> - **Estimate:** engineering ≈ 25 Nov; operational ≈ 1–5 Dec; full acceptance ≈ 22 Dec – 8 Jan (§7a).

- **Where we are.** The foundations are built and well tested offline: scoring, the paper-loop code, safety gates, and the research engine (backtester, walk-forward tools, ML lab). Four pull requests are waiting for review and merge. None of it has run against real data or a real IBKR paper account yet.
- **What M21 still needs.** Three things:
  1. switch on the real paper loop, so it starts collecting genuine outcomes;
  2. build the parts of the roadmap that don't exist yet: the **Promotion & Approval Model** (M21.3), **market context** (M21.6), and the four strategy templates running live with dashboard controls (Appendix A);
  3. run the optimiser and the AI models (M21.2–M21.7) on **real** historical data, not synthetic.
- **How long.** Remaining engineering is about **49 developer-days** plus about 10 for review fixes.
  - **Engineering:** realistically **5–6 weeks (about 9 Dec 2026)**. One month (about 25 Nov) is possible only if every review needs a single round and approvals arrive within a day or two.
  - **Full M21 acceptance:** realistically **mid-January 2027**. It needs real paper outcomes, and those take calendar time that code can't shorten.
- **The biggest speed-up.** Start the paper loop in weeks 2–3. Don't wait for M21.2–M21.7: the outcome clock, not the code, is what limits full acceptance.
- **Stopping the drift.** M21 scope is frozen to the June roadmap text plus genuine safety dependencies. Research ideas added later are kept in the register but marked **not required for M21 sign-off** (§4). Nothing is deleted.
- **Your decisions.** §8 lists only six approvals, plus one optional one that would speed up reviews. They are grouped into four gates, so you aren't asked for each item separately.

---

## 1. The finish line (three separate gates, each measurable)

### A. Engineering completion
Engineering is complete when every item below holds:
- **A-1.** Every mandatory work package in §5 is merged to `main` after an independent exact-head review, and every acceptance test listed for it passes. Python 3.12 / NumPy 1.26, C5a isolation.
- **A-2.** The guards are green on the final `main`: C5a/C5b, M17/M18 G10, hygiene, quarantine.
- **A-3.** There are zero open CRITICAL/HIGH review findings.
- **A-4.** The safety boundaries are unchanged:
  - no live-order path from M21 code;
  - every broker-connected paper entry passes M14 and the trusted market clock;
  - the kill switch works;
  - scoring never grants execution.
- **A-5.** The canonical docs are reconciled (E08): ROADMAP.md, MILESTONE_STATUS.md and the register match the code.

### B. Operational acceptance (needs your approvals; see §8)
- **B-1. Genuine historical data.**
  - A read-only inventory of the VPS M16/M18 store is complete: coverage per symbol and timeframe, gaps, adjustment basis, and the universe as of each date.
  - Real-bar `scanner_replica` parity is reported, and every mismatch is explained (A17/E05/B01).
- **B-2. IBKR PAPER adapter.** These must be verified in a real paper session (A10, A11, A13):
  - account/port isolation;
  - bracket orderRef on every leg, and permIds;
  - real fills and fees;
  - crash/restart and reconnect drills;
  - partial and late fills;
  - coexistence with manual foreign orders.
- **B-3. Unattended scheduled paper loop.** It runs for **10 consecutive trading days** with:
  - zero duplicate orders and zero unknown exposure;
  - every NEEDS_REVIEW resolved;
  - a kill-switch drill passed;
  - the market-clock gate observed across at least one closure or half day where the window allows (Thanksgiving is 26 Nov and the half day is 27 Nov 2026) (A12).
- **B-4. Dashboard.** The D3 panel is live behind authentication, with metrics per template and honest N/A values (A05/A06). Phase-1 strategy controls work, with an audit trail and rollback (A16).
- **B-5. Ledger.**
  - The migration is deployed and the writer is enabled for paper.
  - `account()` reports zero violations every day.
  - Every paper trade can be traced from candidate → score → risk decision → intent → broker result → outcome (A02).
- **B-6. Monitoring.** In a drill, the heartbeat, the gateway-down alarm and the stale-data alarm all fire, and the service restarts and recovers (A14 minimum).

### C. Trading-performance validation (honest evaluation, NOT a profit target)
A strategy that fails validation is a valid result. What does **not** count as completion: missing tests, invalid inputs, simulated results presented as real, or incomplete integration.
- **C-1. Templates walk-forward on real data.** Each of the four templates is evaluated walk-forward and out of sample on real adjusted bars, with realistic costs:
  - a purged walk-forward with an embargo, and a sealed final sample used once;
  - a report card showing net expectancy with a confidence interval, win rate **and** average win vs average loss (the roadmap's win-rate-first principle), drawdown, CVaR, turnover and trade count;
  - a verdict of PASS, FAIL or INCONCLUSIVE.
- **C-2. Parameter choice (M21.2).** Chosen parameters sit on a plateau, not an isolated peak. Every trial is counted, deflated metrics are reported, and results are compared with no-trade and default-parameter baselines.
- **C-3. AI models on real data.** M21.4, M21.5 and M21.7 are each compared out of sample against their baseline (take-all, global thresholds, global weights), with calibration reported. "No improvement" is recorded as such. A model is promoted only through the Promotion & Approval Model.
- **C-4. Paper vs model.** After at least **20 trading days** of the paper loop:
  - a score-vs-outcome report (did higher scores win more? This is the roadmap §5 step "compare predicted vs actual");
  - paper fills vs simulated fills (slippage);
  - a fee reconciliation;
  - the sample size stated.
- **C-5. Integrity.**
  - Survivorship status is disclosed: either the bias is quantified or survivorship-free data is used.
  - Corporate actions are handled.
  - No synthetic result is presented as real, and every trial is in the registry.
  - The integrated canary false-pass upper bound is ≤ 5 % for the exact pipeline.
- **C-6. Governance demonstrated in paper.**
  - One complete Promotion & Approval cycle in paper: proposal → tier → shadow → operator approval → applied → guard.
  - An auto-revert drill.

**M21 is fully accepted only when A + B + C are all met.** The "few hundred closed trades" in roadmap §6 is the gate for opening **eToro (M22)**, not for M21.

---

## 2. Requirements matrix (original roadmap → today)

Status vocabulary:
- CLOSED / CODE_MERGED / TESTED_OFFLINE / PR_PENDING / PARTIAL / DESIGN_ONLY / RUNTIME_UNVERIFIED / BLOCKED (approval) / NOT_STARTED.
- **Mand.** = mandatory for M21 sign-off.

### M21.U — Global Universe Foundation
| ID | Requirement | Implemented | Missing | Status | Acceptance | Deps / permissions | Mand. |
|---|---|---|---|---|---|---|---|
| M21.U | U0→U4, UQ, UR, Runtime Registry, FOCUS_SIZE, Alpaca data path | Delivered historically (handoff §3; test_m21u* suites on main) | — | CLOSED | Historical closeout | — | Yes (done) |

### M21.1 — Score Optimisation (incl. the scoring bridge)
| ID | Requirement | Implemented | Missing | Status | Acceptance | Deps / permissions | Mand. |
|---|---|---|---|---|---|---|---|
| M21.1-a (A01) | Scanner signal → enriched context → M19 score | Bridge on main. A01 truthful contract on PR #12 @ `47e9057`: per-timeframe observation states, M14-verified risk authority, unknown inputs removed → M19 fails closed | Merge; real evidence sources wired (WP5) | PR_PENDING · TESTED_OFFLINE | A01 suite (67 tests). On a real scanner sample: every candidate is scored, and unknown inputs are listed, never invented | #12 merge; real sample via OP1 | Yes |
| M21.1-b | Ranked output: composite score, component breakdown, confidence, reasons | M19 components and buckets; `research_rank_score` preserved (A01 T7) | Ranking surfaced per cycle in the ledger and a report on real signals | TESTED_OFFLINE | T7. A real-signal ranking report with ordered candidates and reasons | WP5, WP7 | Yes |
| M21.1-c | Read-only; scoring never enables execution | A01 T8: `execution_eligible` always False; no submit imports | — | TESTED_OFFLINE | T8 | — | Yes (met) |
| M21.1-d (A02) | Candidate decision audit: every stage, gate, risk decision and outcome joinable (roadmap §9 Trust Test) | Ledger, migration, flag-gated writer, accounting, legacy replay on PR #14 @ `71bccd6` | Runtime wiring (WP7); migration and flag (OP3); read-only replay on a production copy (OP1) | PR_PENDING · TESTED_OFFLINE | `account()` zero violations; candidate → outcome trace for every paper trade | #12/#14 merges; OP1, OP3 | Yes |
| M21.1-e | Freshness truthful under the scanner's real bar semantics | Completed-bar basis added @ `47e9057` (opt-in; default unchanged) | Scanner switch to completed bars (WP6) | TESTED_OFFLINE | T2b (7 tests, 6/6 mutants killed); T-N matrix after WP6 | Decision G1 | Yes |

### M21.1extra — run the live paper loop + dashboard panel + templates
| ID | Requirement | Implemented | Missing | Status | Acceptance | Deps / permissions | Mand. |
|---|---|---|---|---|---|---|---|
| X-a (A03/A04/A09) | Scheduled cycle: focus → scan → score → decide → IBKR **paper** → record | C5 controls and D0–D2 CLOSED. WS2 on PR #1 @ `0f3f9e6`: scheduler, M14 risk gate, trusted market clock (R1–R3), outcome capture | Merge; selection by research rank (WP5); completed candles (WP6); activation (OP3) | PR_PENDING · RUNTIME_UNVERIFIED | WS2 suite (123 tests); B-3 soak | Merge, paper-gateway and schedule approvals | Yes |
| X-b (A10) | Real IBKR paper adapter behaviour | Adapter code in #1 | Real paper session | BLOCKED (approval) | B-2 checklist | G3 | Yes |
| X-c (A11) | Crash/exit reconciliation | Offline tests in #1 | Runtime drills | PR_PENDING / BLOCKED | Disconnect, restart, partial fill, late TP/SL drills | G3 | Yes |
| X-d (A12) | Market clock and unattended scheduling | D1 guard; R1–R3 hardening | Live observation incl. holiday/half day | BLOCKED (approval) | B-3 | G3 | Yes |
| X-e (A13) | Record entry, reason, score and outcome | Outcome capture in #1; D3 metrics on main | True fills and fees | PR_PENDING | Net P&L with fees, R-multiple, exit taxonomy, real-vs-sim flag | G3 | Yes |
| X-f (A05/A06) | Dashboard: win rate, average win vs loss, trade count, drawdown **per strategy** | D3 merged; `paper_metrics_view` groups by strategy | Dashboard restart; per-template identity (WP8) | CODE_MERGED · RUNTIME_UNVERIFIED | B-4 | Restart approval (G3) | Yes |
| X-g | Kill switch active; no eToro | Kill-switch suites; WS2 gate | Drill in the soak | TESTED_OFFLINE | Kill drill in B-3 | G3 | Yes |
| X-h (A14) | Runs unattended | `gateway_health`, watchdog, heartbeat modules exist | Health/stale/gateway alarms and a restart drill (WP10) | NOT_STARTED (minimum) | B-6 | G3 | Yes (minimum). Full A14 → M22 C03 (decision G1) |
| X-i (A15) | Loop runs a **selectable template** with SL/TP on every trade (Appendix A) | 4 templates in the research genome (synthetic); live loop = Template 1 | Templates 2–4 in the live path; template id on every signal, intent and outcome (WP8) | PARTIAL | Research↔live signal parity on identical bars; 100 % of trades carry template id + SL/TP | WP6 | Yes |
| X-j (A16) | Appendix A **Phase 1**: templates selectable, parameters adjustable, champion picked on the dashboard | Single-strategy editor (`/api/strategy`, `data/strategy.json`) | Template selection; per-template parameters with bounds; versioning, audit, rollback; paper lane only (WP9) | NOT_STARTED | Validation-bound tests; audit row per change; rollback test; cannot touch a live lane | WP8 | Yes |
| X-k (A17/E05) | Backtest and real-bar parity (trust in the record) | Synthetic parity 6,122/6,122 | Real-bar parity; coverage report (WP11) | BLOCKED (data approval) | B-1 | OP1, WP6 | Yes |
| X-l (A07/A08) | Telegram BUY/SHORT alerts | Code merged | Activation | BLOCKED | — | Approval | **No**: not in the June M21 text (decision G1) |

### M21.2 – M21.7
| ID | Requirement (June text) | Implemented | Missing | Status | Acceptance | Deps / permissions | Mand. |
|---|---|---|---|---|---|---|---|
| M21.2 (B11; uses B09/B10) | Tune RSI/MACD/MA/ATR/volume/stops/targets/thresholds from real outcomes | Genome, portfolio backtester, search trials (synthetic) | Real-bar surfaces and plateaus per template; costs, liquidity, corporate actions (WP12, WP13) | PARTIAL | C-2 | WP11, WP12 | Yes |
| M21.3-a (B12; uses B05–B08) | Walk-forward optimiser (optimise → test unseen → roll) | Purged WF, CPCV, DSR, PBO, SPA, trial registry, sealed holdout (local model), canaries | Optimiser driver on real data; integrated canary (WP13) | PARTIAL | C-1, C-5 | WP11, WP12 | Yes |
| M21.3-b (B13; plus the auto-revert part of B23) | **Promotion & Approval Model**: GREEN/AMBER/RED tiers, shadow-first, change budget, auto-revert on decay; AMBER/RED never live without approval | Nothing | All of it (WP14) | NOT_STARTED | Tier classifier (incl. "tighter stop can raise losses" → not GREEN unless proven); shadow over N live signals before eligibility; budget of one change at a time, at most one a week; auto-revert fires on simulated decay; no AMBER/RED applied without an audited operator approval; RED cooling-off; C-6 | WP7, WP8, WP9 | Yes |
| M21.4 (B16) | Meta-labelling: take / skip / size down | Lab (synthetic) | Real labels (counterfactual on real bars + paper outcomes); calibrated; shadow proposals (WP17) | CODE_MERGED (synthetic) | C-3; can only skip or size down, never up | WP11, WP13, WP7 | Yes |
| M21.5 (B17) | Regime-aware thresholds | Lab regime models (synthetic) | Real regime features; per-regime thresholds walk-forward; shadow (WP16) | CODE_MERGED (synthetic) | C-3; detection lag modelled | WP15, WP13 | Yes |
| M21.6 (B18) | VIX / breadth / sector / index trend / macro-news context | Sentiment confirm/block layer in the scanner; lab context features | Sources, point-in-time store, features, shadow risk throttle (WP15) | NOT_STARTED | Point-in-time tests (value at t uses only data published ≤ t); source outage → abstain; features in the ledger; throttle evaluated walk-forward | Data decision G4 | Yes |
| M21.7 (B19) | Learned regime weights | Lab regime-conditioned models | Real-data out-of-fold weights vs global; bounded and drift-aware; shadow (WP18) | PARTIAL | C-3 | WP16, WP17 | Yes |
| §5 loop | "Compare predicted vs actual" | — | Score-vs-outcome report from ledger joins | NOT_STARTED | C-4 | WP7 plus paper data | Yes |

**Genuine safety dependency pulled into M21 from C01 (M22).** The scanner currently scores the forming (in-progress) bar, while the backtester and `scanner_replica` use completed bars. Paper outcomes then can't be compared with backtests (C-4), and real-bar parity (A17) can't pass by construction. So C01 §11 (the completed-candle scanner) is scheduled as **WP6**. The rest of C01 (governor store, reduce-only flatten, M14 on the legacy scanner path) stays an **M22 hard blocker**: the M21 paper loop uses only the WS2 path, which already applies M14 and the trusted clock.

---

## 3. What we are deliberately NOT adding to M21

The following are not added: risk-aware portfolio construction (M23.3), research-agent integration (M23 D06), cross-market or alternative data (B24), and independent signing authority (B06 operational). They come either from Issue #2 or from later milestones, not the June M21 text. They stay tracked (§4). The paper loop keeps the existing M14 per-trade and exposure gates.

## 4. The 71 tasks, classified for M21 sign-off (none removed)

- **M21 mandatory, still open (25):**
  - A: A01, A02, A06, A09, A10, A11, A12, A13, A14 (minimum), A15, A16, A17
  - B: B01, B03 (real-data run), B08 (integrated canary), B10 (costs, liquidity, corporate actions), B11, B12, B13, B16, B17, B18, B19, B23 (auto-revert part)
  - E: E05
- **M21 closeout task (1):** E08, the docs reconciliation.
- **Decision only (1):** B02, the vendor and survivorship decision (G4).
- **M21 supporting, already delivered (9):**
  - CLOSED: A03, A04.
  - CODE_MERGED: A05, A07, B05, B06 (local policy model), B07, B09, B14.
  - These are reused, not redone. The research part of A15 is also delivered, but A15 stays mandatory for its live-loop part.
- **Not required for M21 sign-off (14, still tracked, status unchanged):**
  - A08 (alerts activation).
  - B04, B15, B20, B21, B22 (→ D06), B24.
  - E01, E02, E03, E04, E06, E07, E09.
  - Also outside the mandatory scope, although their tasks stay listed above: A14 beyond the minimum (→ C03), B06 operational independent signer, B23 beyond auto-revert (→ D02).
- **M22 (C01–C11, 11) and M23 (D01–D10, 10):** unchanged. C01 §11 is pulled forward as WP6; everything else in C01 stays M22.
- **Check:** 25 + 1 + 1 + 9 + 14 + 11 + 10 = **71**.

Recommendation: do E07 (secret/backup permissions, metadata only) in the same read-only VPS session (OP1), because it is a cheap security fix. E09 (offline CI) is an optional accelerator; see G-opt.

---

## 5. Remaining work plan (priority order) and bottom-up estimate

**Units.** A dev-day is one focused developer day. Review is the independent reviewer's effort. A fix allowance of about 20 % of engineering is held centrally. "Lane": L = loop/runtime lane, R = research/data lane, O = operations (operator-run). The L and R lanes run in parallel on separate branches.

| WP | What | Tasks | Eng d | Review d | Depends on | Lane | External blocker | Earliest done | Planned |
|---|---|---|---|---|---|---|---|---|---|
| WP1 | A01 merge (provenance resolved; completed basis added) | A01 | done | 0.5 (delta `8013044..47e9057`) | — | L | Your merge approval | 30 Oct | 2 Nov |
| WP3 | WS2 merge (R1–R3) | A09 | done | 0.5 (delta `46c87ab..0f3f9e6`) | — | L | Your merge approval | 30 Oct | 3 Nov |
| WP2 | A02 merge (retarget to main after #12; merge `main` in, no rebase) | A02 | 0.25 | 0.5 | WP1 | L | Your merge approval | 2 Nov | 4 Nov |
| WP4 | C01 design doc merge (M22 reference) | C01 | — | 0.25 | — | — | Your merge approval | 30 Oct | 4 Nov |
| WP5 | Scoring evidence wiring | A01 (pending) | 3 | 1 | WP1, WP3 | L | — | 3 Nov | 6 Nov |
| WP6 | Completed-candle scanner (C01 §11, T-N), `bar_basis="completed"`, fresh-quote entry + slippage guard | A17 prerequisite | 2.5 | 1 | WP1; G1 | L | Approval (protected scanner, C5b) | 4 Nov | 9 Nov |
| WP10 | Minimal unattended monitoring | A14 (minimum) | 2 | 0.5 | WP3 | L | — | 4 Nov | 9 Nov |
| WP12 | Cost / liquidity / corporate-action model | B10 | 2 | 0.5 | — | R | — | 3 Nov | 5 Nov |
| WP7 | Ledger runtime wiring (flag-gated), score payloads, legacy replay report | A02 | 2 | 0.5 | WP2, WP5 | L | OP1 for replay | 6 Nov | 11 Nov |
| WP11 | Real historical-data readiness | B01, E05, A17 | 3 | 1 | OP1, WP6 | R | VPS read-only approval | 9 Nov | 13 Nov |
| WP8 | Templates 2–4 in the live path; template id end to end; D3 per template | A15 | 4 | 1 | WP6 | L | — | 11 Nov | 16 Nov |
| WP9 | Dashboard strategy controls, Phase 1 | A16 | 3 | 1 | WP8 | L | — | 16 Nov | 20 Nov |
| WP13 | Walk-forward optimiser on real data, per template | B11, B12, B08 | 5 | 1.5 | WP11, WP12 | R | — | 18 Nov | 23 Nov |
| WP15 | Market context (M21.6) | B18 | 6 | 1.5 | G4 | R | Data-source decision and licences | 18 Nov | 24 Nov |
| WP14 | Promotion & Approval Model + auto-revert | B13, B23 (part) | 6 | 2 | WP7, WP8, WP9 | L | — | 23 Nov | 27 Nov |
| WP16 | Regime-aware thresholds on real data (shadow) | B17 | 2.5 | 1 | WP13, WP15 | R | — | 25 Nov | 30 Nov |
| WP17 | Meta-labelling on real data (shadow) | B16, B03 | 3.5 | 1 | WP7, WP11, WP13 | R | — | 26 Nov | 2 Dec |
| WP18 | Learned regime weights on real data (shadow) | B19 | 3 | 1 | WP16, WP17 | R | — | 1 Dec | 7 Dec |
| WP19 | Docs reconciliation + M21 acceptance report | E08 | 1 | 0.5 | all | — | — | 2 Dec | 9 Dec |
| **Total** | | | **≈ 48.75** (+ ≈ 10 fixes) | **≈ 17** | | | | | |

What each work package contains:
- **WP5:**
  - a verified calendar built from the Alpaca calendar into a `SessionCalendar`;
  - liquidity measured from bars;
  - corporate-action state from M16 adjustment data, with suspected splits failing closed;
  - WS2 selection by `research_rank_score` across data-quality gates, with M14 deciding execution.
- **WP7:** a legacy replay report on a read-only production copy.
- **WP8:** research↔live parity tests for the templates.
- **WP9:** template selection and champion pick; parameter bounds; versioning, audit and rollback; paper lane only.
- **WP10:** heartbeat and health; gateway-down and stale-data alarms; a restart-recovery drill.
- **WP11:**
  - a coverage report per timeframe and symbol;
  - an adjusted-bar policy;
  - real-bar parity for `scanner_replica`;
  - survivorship bias quantified.
- **WP12:**
  - IBKR tiered commissions;
  - spread and slippage by ADV and price;
  - an ADV participation cap;
  - a short-borrow flag;
  - adjusted bars.
- **WP13:**
  - parameter surfaces and plateaus;
  - a sealed holdout used once;
  - the trial registry;
  - an integrated canary run on the exact pipeline.
- **WP14:**
  - proposal records and the tier classifier;
  - a shadow runner on live paper signals that places no orders;
  - the change budget;
  - an auto-revert guard with an alert;
  - audited operator approval and RED cooling-off.
- **WP15:**
  - VIX (FRED VIXCLS / CBOE);
  - SPY/QQQ trend and sector ETFs (Alpaca);
  - breadth from our own universe;
  - official FOMC/CPI/NFP calendars;
  - earnings proximity (source per G4);
  - news risk from the existing sentiment layer;
  - point-in-time stamps, publication lag and revision policy;
  - a shadow throttle.

**Operations (your gates; Claude prepares runbooks and probes, you approve and run):**

| Op | What | Needs | Planned |
|---|---|---|---|
| OP1 | One **read-only** VPS session: data inventory (B01/E05); copy of the signals DB for the A02 replay; M14 ingestion and `RISK_*` environment check (marked "VPS VERIFICATION REQUIRED"); `.env` backup permission metadata (E07). No writes. | G2 | Any day this week |
| OP2 | IBKR **paper** gateway acceptance (A10, A11 runtime, A13) | G3; #1 merged; WP5 | 10–13 Nov |
| OP3 | Activation in paper: dashboard restart (A06); A02 migration and flag; scheduled unattended loop (A12); monitoring (WP10) | G3; WP5–WP7, WP10 merged | ≈ 16 Nov |
| OP4 | 10-trading-day soak (B-3) | OP3 | ≈ 16 Nov – 1 Dec |
| OP5 | Deploy merged M21 features to the paper lane (templates, controls, promotion shadow), then one governance cycle (C-6) | G3 follow-up | ≈ 10–24 Dec |

### Completion windows (from 28 Oct 2026)
| Gate | Optimistic | **Planned** | Pessimistic | What drives it |
|---|---|---|---|---|
| A. Engineering complete | 25–27 Nov | **≈ 9 Dec 2026** | ≈ 23 Dec | About 49 dev-days of genuinely missing work: the Promotion model, M21.6 data, real-data walk-forward and the templates |
| B. Operational acceptance | ≈ 1–4 Dec | **≈ 11–16 Dec 2026** | early Jan | Approval timing; the IBKR gateway (last known: **failed** on the VPS); the 10-day soak |
| C. Full M21 acceptance | ≈ 16–18 Dec | **≈ 8–15 Jan 2027** | late Jan | At least 20 paper trading days after activation; one governance cycle; holidays (26–27 Nov, 25 Dec, 1 Jan) |

**Why the one-month goal is tight.** About 49 dev-days of real work remain, and about a third of it is new capability that doesn't exist in any form yet. One month needs roughly 2.5 dev-days of output per working day, one review round per package, and decisions G1–G4 this week. That has happened on some days (26–28 Oct) but can't be assumed every day. The plan therefore says 5–6 weeks.

**Critical paths.**
1. **Engineering:** OP1 → WP11 → WP13 → WP16/WP17 → WP18 → WP19. The serial chain is about 15.5 dev-days plus five review rounds. WP15 feeds WP16, and WP14 waits for WP7–WP9.
2. **Full acceptance:** merges → WP5/WP6/WP7/WP10 → OP2 → **OP3 activation** → 20 trading days → C-4. Every week activation slips moves full acceptance by a week.

**Main risks.**
- The IBKR gateway on the VPS was last reported failed (OP2 could slip).
- Real intraday coverage gaps (E05) may force a smaller, honestly documented universe.
- The earnings-calendar source and its licence (WP15).
- More than two review rounds per package.
- Holiday thin markets.
- A paper-session finding that requires a code change (loops back to L).
- Survivorship bias flattering backtests, which must be quantified (C-5).

---

## 6. Authoritative M21 checklist (update on every merge; link from Issue #3)

**Markers:**
- **DATA_UNVERIFIED**: correct on synthetic/fixture data only; needs the real data from OP1.
- **RUNTIME_UNVERIFIED**: correct offline; never run against the real gateway, scheduler or dashboard.
- **REVIEW**: waiting for an independent exact-head review.

**Phase 1 — close the open PRs**
- [ ] WP1 #12 merged
- [ ] WP3 #1 merged
- [ ] WP2 #14 retargeted and merged
- [ ] WP4 #13 merged

**Phase 2 — make the loop real**
- [ ] WP5 — code in PR #15 (REVIEW; RUNTIME_UNVERIFIED)
- [ ] WP6 — code in PR #16 (REVIEW; protected scanner + Alpaca transitions need C5b approval; proof in the corrections doc §9; RUNTIME_UNVERIFIED)
- [ ] WP7 — PR #17 `bf94f71` (REVIEW; M14_LIVE authenticated against the store; only M14_LIVE permits an intent; RUNTIME_UNVERIFIED)
- [ ] WP10 — PR #18 `9012711` (REVIEW; future-stamped readings are UNKNOWN; RUNTIME_UNVERIFIED: the alarm drill is B-6)
- [ ] OP1
- [ ] OP2
- [ ] OP3 — **paper loop running**
- [ ] OP4 soak (B-3)

**Phase 3 — remaining M21 capability**
- [ ] WP8 — PR #21 `fbfc815` (REVIEW; templates 2–4 shadow-only; RUNTIME_UNVERIFIED)
- [ ] WP9 — PR #22 `b4e9955` backend with compare-and-swap (REVIEW). Dashboard UI = **E6, PR #32** (isolated blueprint; the protected `dashboard/app.py` wiring is a prepared patch, NOT applied). RUNTIME_UNVERIFIED.
- [ ] WP12 — PR #19 (REVIEW; verified in the corrections doc §8; spread, impact, slippage and borrow are assumptions → DATA_UNVERIFIED until paper fills)
- [ ] WP11 (B-1) — PR #20 `4924844` (REVIEW; real-data entrances fail closed; G9 root cause fixed). Real run = OP1 → DATA_UNVERIFIED.
- [ ] WP13 (C-1, C-2) — PR #24 `4b677bc` (REVIEW). **v3 canary preregistered, not run**; expected digest on the proposed `main` tree with E1–E7: `7c49b813…6b26b` (was `01b3f432…f262` before E1/E3/E5 changed its code paths). Report-card wiring = E1 (PR #27). DATA_UNVERIFIED.
- [ ] WP14 / M21.3 (C-6) — PR #23 `04bf5ca` (REVIEW; cross-store ops + CAS + post-activation verification). Governance cycle = OP5 → RUNTIME_UNVERIFIED.
- [ ] WP15 / M21.6 — PR #25 `f05ed84` (REVIEW; UTC publication instants, minimum backfill lag). Context sources (G4); WS2 logging = E2 → DATA_UNVERIFIED.
- [ ] WP16 — PR #26 `89bc874` (REVIEW); real run after OP1 → DATA_UNVERIFIED
- [ ] WP17 — PR #26 (REVIEW); real run after OP1 → DATA_UNVERIFIED
- [ ] WP18 (C-3) — PR #26 (REVIEW); real run after OP1 → DATA_UNVERIFIED
- [ ] E3 — converted-input path for M17 multi-timeframe and M18 real builds — code in **PR #29** (REVIEW) → DATA_UNVERIFIED
- [ ] E1 — walk-forward → report card — code in **PR #27** (REVIEW)
- [ ] E2 — WS2 market-context shadow logging — code in **PR #28** (REVIEW; flag OFF) → DATA_UNVERIFIED
- [ ] E4 — score-vs-outcome report from ledger joins (C-4) — code in **PR #30** (REVIEW); needs genuine paper outcomes
- [ ] E5 — paper fill/fee statement reconciliation into `fee_calibration` (C-4) — code in **PR #31** (REVIEW); needs the IBKR statement
- [ ] v3 canary — one run on merged `main` (acceptance packet §8)
- [ ] Runbooks OP1–OP5 — prepared, with abort and rollback; **OP1 final (29 Oct)**: scripts verified read-only on a synthetic VPS replica, minimum permissions, IBKR Flex specification, E5 reconciliation; OP2/OP3 one-page gate. Not run.

**Closeout**
- [ ] OP5
- [ ] C-4 report
- [ ] C-5 report
- [ ] WP19 / E08 (= E7) — ROADMAP.md / MILESTONE_STATUS.md in **PR #33** (REVIEW; merge last; update to merged SHAs on merge day); this checklist and the register on the docs branch
- [ ] A-gate
- [ ] B-gate
- [ ] C-gate → **M21 ACCEPTED**

---

## 7. Immediate merge readiness (exact heads, 28 Oct)

**Combined proof.** All four PR heads were trial-merged locally onto `main` `80b4343` in the order #12 → #1 → #14 → #13:
- **Conflicts:** none.
- **Resulting tree:** `a5036dfcdabe52ab25f8a9acce6933c101d524aa` (local only, not pushed).
- **Tests:** 23 test targets on that tree under Python 3.12.3 / NumPy 1.26.4, with C5a isolation and a checkout outside `/tmp`: **all OK**.
- **What the 23 covered:**
  - A01, the bridge, group D, the A-path, alerts, B1 and A02;
  - WS2 learning loop, D1 guard, D1 Alpaca source, D2 gates/dry-run/lifecycle, and D3 metrics (with Flask 3.1.0 from `requirements.txt` installed in the test environment);
  - registry shadow-run, hygiene, quarantine, C5a, C5b, G10 ×2, research foundation and research integration.

| PR | Head | Ready? | Remaining blockers |
|---|---|---|---|
| **#12 A01** | `47e905709919ed11f3ab31bc4be0e058df2fce8e` | **Yes, after a delta re-check.** | **Delta to re-check:** `8013044..47e9057` (5 files), the completed-bar basis. Default behaviour is unchanged, and the provenance finding was already resolved at `8013044`.<br>**Consequence of merging:** the A-path, B1, alerts and WS2 selection fail closed until WP5. Nothing runs in production, so this is safe. |
| **#1 WS2** | `0f3f9e6db860bb2359238c8b83283d0c93d76a4e` | **Yes, after the R1–R3 check.** | **Check:** the reviewer verifies `2562c5c` + `0f3f9e6`.<br>**Scope:** merging the code ≠ paper acceptance (A10–A12 stay BLOCKED until G3).<br>**Rule:** merge `main` in normally (no rebase) if `main` moves first. |
| **#14 A02** | `71bccd60900bf444e818a1e494c567ca498b37f7` | **Yes, once #12 is merged.** | **Steps:** retarget the base to `main`, merge `main` in (no rebase), re-run A02 and the guards.<br>**Kept as-is:** no migration is deployed and the writer stays OFF. |
| #13 C01 (doc) | `6a8a690eea14a639836af3fbe48f563c83288d73` | **Yes** | Doc only. It remains the M22 hard-blocker design; implementation needs a separate decision (only §11 is pulled into M21, as WP6). |

---

## 7a. Accelerated program, 28 Oct: PRs, evidence and the new estimate

**All of these are offline draft PRs.** Nothing is merged, deployed, scheduled or connected. The flags stay **OFF**. Guard runs used Python 3.12.3 / NumPy 1.26.4 with C5a isolation from a checkout outside `/tmp`.

| PR | WP | Branch → base | Head | Tests | Mutants | Guard + suite run |
|---|---|---|---|---|---|---|
| #15 | WP5 evidence wiring | `m21/wp5-evidence` → stack-base | `5394882` | 26 | 12/12 | 28/28 |
| #16 | WP6 completed-candle scanner | `m21/wp6-completed-candles` → wp5 | `bf92678` | 19 | 11/11 | 47/47 |
| #17 | WP7 ledger wiring | `m21/wp7-ledger-wiring` → wp6 | `16ad7fb` | 16 | 11/11 | 32/32 |
| #18 | WP10 monitoring | `m21/wp10-monitoring` → stack-base | `45784e7` | 13 | 10/10 | 28/28 |
| #19 | WP12 economics | `m21/wp12-economics` → stack-base | `d7b8e66` | 43 | 14/14 | 35/35 |
| #20 | WP11 readiness + **lookahead fix** | `m21/wp11-readiness` → wp12 | `ac31ef8` | 35 | 16/16 | 37/37 |
| #21 | WP8 templates | `m21/wp8-templates` → wp7 | `24a64a9` | 21 | 15/15 | 33/33 |
| #22 | WP9 template controls | `m21/wp9-template-controls` → wp8 | `bb1b571` | 18 | 13/13 | 33/33 |
| #23 | WP14 promotion & approval | `m21/wp14-promotion` → wp9 | `b671d92` | 20 | 18/18 | 33/33 |
| #24 | WP13 walk-forward driver | `m21/wp13-walkforward` → wp11 | `609aa85` | 16 | 13/13 | 37/37 |
| #25 | WP15 market context | `m21/wp15-market-context` → stack-base | `b421903` | 15 | 12/12 | 28/28 |
| #26 | WP16–18 shadow ML | `m21/wp16-18-shadow-ml` → wp13 | `6fe2d1a` | 11 | 10/10 | 38/38 |

**Mutant counts are after the fixes.** Eight first-pass survivors were real test gaps; each now has a test. No production assertion was weakened.

**Cross-lane end-to-end test.**
- Lives on the integration-only branch `m21/integration` (`a4791f6`), which merges both lanes plus WP10 and WP15. It is **not for merging**.
- `test_m21_e2e_cross_lane.py` has 24 tests, including **17 negative cases**.
- **48/48** suites pass on that tree.
- The chain:
  1. start-stamped store bars;
  2. WP11 re-stamp;
  3. WP13 walk-forward with WP12 economics, then the one-shot holdout;
  4. WP8 bot↔research signal parity;
  5. WP9 controls;
  6. WP14 shadow → token → apply → confirm;
  7. WP16–18 evidence gate;
  8. WP10 alarms;
  9. WP15 shadow context.

**Findings that change the plan:**
1. **Lookahead (fixed in tooling).** M16 stamps bar starts, while M17 and research read closes. Every real-data use must pass through `research.dataqa.stamps.to_close_stamped` (WP11).
2. **Power.** On an 8-symbol universe the walk-forward cannot detect a planted 0.5σ effect after multiplicity is charged; on 30 symbols it can. The real-data universe must be wide enough. The canary battery (59 nulls + 20 positives) is reported on PR #24.
3. **Meta-labelling.** In synthetic pilots it was **WORSE** than taking every template entry, and the regime refinements showed no evidence of improvement. The gates correctly refuse to promote them. Real-data runs decide.

**Remaining work after review:**
- review fixes (≈ 20 %);
- the walk-forward report card;
- loading the context data and wiring the WS2 shadow;
- the dashboard UI for templates and controls (protected file);
- real-data runs of WP11/13/16–18 after OP1;
- WP19.

That is about 13–15 dev-days plus reviews, against ≈ 49 before.

| Gate | Optimistic | **Planned** | Pessimistic | Driver |
|---|---|---|---|---|
| A. Engineering complete | ≈ 18 Nov | **≈ 25 Nov 2026** | ≈ 9 Dec | 12 PR reviews (1–2 rounds each); OP1 by ≈ 3 Nov for the real-data reruns |
| B. Operational acceptance | ≈ 27 Nov | **≈ 1–5 Dec 2026** | mid-Dec | merges, then OP2 (the gateway was last seen failed), OP3 activation ≈ 16 Nov, 10-day soak |
| C. Full M21 acceptance | ≈ 18 Dec | **≈ 22 Dec 2026 – 8 Jan 2027** | late Jan | 20 paper trading days after activation; one governance cycle (OP5); holidays |

**Runbooks** for OP1–OP5 (preparation only) are in `docs/runbooks/M21_OP1-OP5_Runbooks_2026-10-28.md` on this branch.

**Merge order:**
1. #12 → #1 → #14 → #13 (unchanged).
2. Then L: #15 → #16 → #17 → #21 → #22 → #23, plus #18.
3. Then R: #19 → #20 → #24 → #26, plus #25.

Stacked PRs are retargeted to `main` as each base merges, by merging `main` in. No rebases.

## 7b. Review-correction round, 28 Oct (late)

**Corrected heads.** Everything else is unchanged. Merge order is unchanged (§7a); then the v3 canary runs on the merged WP13 code.

| PR | Head | Change |
|---|---|---|
| #17 | `4c1cac7` | risk provenance |
| #21 | `04970d4` | merge only |
| #22 | `ded303d` | merge only |
| #23 | `28c1943` | cross-store ops |
| #20 | `5f86c30` | real-data entrances |
| #24 | `aa00b12` | experiment identity, real-input guard, outcome separation |
| #26 | `9cffb0a` | merges only |
| #25 | `d992a0f` | start-stamped bars refused |
| `m21/integration` | `bcbcf01` | — |

**Evidence:**
- **129 modules: 128 OK, 0 failed, 1 with no tests (test_m12, by design)** on the integration tree;
- 75/75 targeted mutants;
- +49 tests.

Full report: `docs/reviews/M21_Review_Corrections_2026-10-28.md`.

## 7c. Final engineering acceptance, 29 Oct

**Final heads:**
- #12 `47e9057`, #1 `0f3f9e6`, #14 `68ca7b4`, #13 `6a8a690`;
- L lane: #15 `5394882`, #16 `bf92678`, #17 `bf94f71`, #21 `fbfc815`, #22 `b4e9955`, #23 `04bf5ca`, #18 `9012711`;
- R lane: #19 `d7b8e66`, #20 `4924844`, #24 `4b677bc`, #26 `89bc874`, #25 `f05ed84`;
- `m21/integration` `79ae46f` (tree `7843553`).

**Merge proof.** A sequential trial merge of all 16 heads onto `main` (local only) is conflict-free in either lane order and ends at tree `f1ff536`. That tree equals the integration tree minus `test_m21_e2e_cross_lane.py` and its two G10 allow-list entries. The expected `main` tree after every step, the commands and the retarget rule are in the acceptance packet §13. Use merge commits only.

**Evidence:** full suite 129 modules: 128 OK, 0 failed, 1 without tests (test_m12, by design); lane tips 35/35 and 40/40; #18 and #25 28/28 each.

**Verdicts:**
- READY FOR MERGE REVIEW: #17, #21, #22, #23, #24.
- READY WITH DOCUMENTED LIMITATIONS: #15, #18, #19, #20, #25, #26.
- BLOCKED_BY_APPROVAL (C5b): #16, and #12's scanner metadata.
- No PR is CHANGES REQUIRED after this round's fixes.

Full report: `docs/reviews/M21_Final_Engineering_Acceptance_2026-10-29.md`.

## 7d. Release preparation, 29 Oct (evening)

**Nothing merged; `main` is `80b4343`; every new runtime path is OFF.**

**1. The 16 PR heads, re-verified on GitHub (all open drafts, unchanged):** #12 `47e9057`, #1 `0f3f9e6`, #14 `68ca7b4`,
#13 `6a8a690`; L lane #15 `5394882`, #16 `bf92678`, #17 `bf94f71`, #21 `fbfc815`, #22 `b4e9955`, #23 `04bf5ca`,
#18 `9012711`; R lane #19 `d7b8e66`, #20 `4924844`, #24 `4b677bc`, #26 `89bc874`, #25 `f05ed84`.

**2. New draft PRs (base `m21/rc-base` `5616f3a` = the exact post-merge tree `f1ff536`):**

| PR | Item | Head | Tests | Mutants |
|---|---|---|---|---|
| #27 | E1 walk-forward → report card | `c4a5ef1` | 12 | 13/13 |
| #28 | E2 WS2 market-context shadow record (flag OFF) | `f9141e6` | 10 | 10/11 (1 equivalent) |
| #29 | E3 converted-data inputs for M17/M18 | `4cff094` | 13 | 15/15 |
| #30 | E4 score-versus-outcome report | `e26da8b` | 18 | 21/21 |
| #31 | E5 IBKR statement reconciliation + fee calibration | `e62ee43` | 21 | 29/29 |
| #32 | E6 dashboard template controls (app.py patch prepared, NOT applied) | `1181077` | 15 | 18/18 |
| #33 | E7 ROADMAP.md / MILESTONE_STATUS.md reconciliation | `1e3921d` | docs | — |

**3. Merge sequence (merge commits only — no rebase, no squash; a stacked PR is retargeted by merging `main` into it).**
Expected `main` tree after each step:

| # | PR @ head | tree | # | PR @ head | tree |
|---|---|---|---|---|---|
| 1 | #12 `47e9057` | `a31b018` | 13 | #20 `4924844` | `2fe8a31` |
| 2 | #1 `0f3f9e6` | `9c57eb8` | 14 | #24 `4b677bc` | `4959afc` |
| 3 | #14 `68ca7b4` | `1da598c` | 15 | #26 `89bc874` | `1153db9` |
| 4 | #13 `6a8a690` | `a1aa8dd` | 16 | #25 `f05ed84` | **`f1ff536`** |
| 5 | #15 `5394882` | `9a21419` | 17 | #27 E1 `c4a5ef1` | `b44a49c` |
| 6 | #16 `bf92678` (C5b) | `ab74aab` | 18 | #28 E2 `f9141e6` | `6780b99` |
| 7 | #17 `bf94f71` | `afa9322` | 19 | #29 E3 `4cff094` | `808d3a5` |
| 8 | #21 `fbfc815` | `6dac788` | 20 | #30 E4 `e26da8b` | `63d6704` |
| 9 | #22 `b4e9955` | `f08b79e` | 21 | #31 E5 `e62ee43` | `c798a58` |
| 10 | #23 `04bf5ca` | `fa238da` | 22 | #32 E6 `1181077` | `8214ac0` |
| 11 | #18 `9012711` | `fc763db` | 23 | #33 E7 `1e3921d` | **`1ed8c0f`** (recomputed on merge day: E7 is updated to the merged SHAs first) |
| 12 | #19 `d7b8e66` | `c1ff0ca` | | | |

The E PRs merge conflict-free in any order (same final tree, checked forwards and backwards). The tested integration
tree `m21/integration` `b7b8a90` equals `1ed8c0f` plus `test_m21_e2e_cross_lane.py` and its two G10 allow-list lines.

**4. Merge blockers that are not code.**
- **VPS auto-deploy (new, highest priority).** `sync.sh` (started by `deploy.sh`) polls GitHub every 60 s and, on a new
  `main` commit, runs `git reset --hard origin/main`, `pip install -r requirements.txt` and restarts both
  `algo-trader.service` and `algo-trader-dashboard.service`. Whether it runs on the VPS is unknown here (**VPS
  VERIFICATION REQUIRED**, OP1 item 14). If it runs, each of the 23 merges deploys and restarts production. Decide
  before merging: stop it for the merge window and deploy once deliberately (recommended), or accept it.
- **Vercel.** GitHub deployment records show a Vercel **Production** deployment for every push to `main` (one per merge
  commit: 8 within 3 minutes on 27 Oct, each ~10 s after the merge) and a **Preview** deployment for every branch push
  (92 of the last 100 records, including today's E-branch pushes). Project settings are not readable with the
  available access. To check (Vercel → Project → Settings): **Git → Production Branch** (`main`), **Git → Ignored Build
  Step** (or disconnect the Git repository), and **Deployment Protection** for previews. Nothing was changed.
- **C5b approval** for #16 (`bot/scanner.py` opt-in completed-bar seam, `bot/providers/alpaca_provider.py`
  explicit `adjustment=raw`) and #12's scanner metadata; independent review of #27–#33.

**5. Protected files.** The E PRs change no protected file. The batch changes exactly `bot/scanner.py` (A01 metadata +
WP6 opt-in seam; default behaviour unchanged), `bot/providers/alpaca_provider.py` (WP6), the new
`bot/providers/alpaca_corporate_actions.py` (WP5) and `_protected_baselines.py` — as reviewed. `dashboard/app.py`,
`main.py`, `bot/risk.py`, `bot/brokers/*`, `server_scripts/`, `deploy.sh`, `requirements.txt` are unchanged. The only
pending protected change is E6's `dashboard/app.py` +6 lines (`docs/m21/E6_dashboard_app_wiring.patch`, separate
approval).

**6. Production runtime behaviour on deployment, default configuration.** The scanner records a per-timeframe bar
as-of in its cycle metadata (A01) and imports `bot.bar_completion`; signals and decisions are unchanged (bar basis
`in_progress`). Everything else is inert until enabled: the WS2 loop is not scheduled; the candidate-ledger writer is
disabled and its migration not deployed; `MARKET_CONTEXT_SHADOW_ENABLED`, `TEMPLATE_CONTROLS_DB_PATH` /
`TEMPLATE_CONTROLS_WRITES_ENABLED` are unset; templates 2–4 stay shadow-only (`LIVE_TEMPLATES = {scanner}`); the
minimum score is unchanged. A restart of both services is a production change in itself (see §4).

**7. v3 canary.** E1/E3/E5 change files in its code paths, so the preregistered expected digest on the proposed tree
is now `7c49b813835cfba77527379a17da4b2f8a0cf1757e794198c332a59016a5b26b` (was `01b3f432…f262`). NOT run.

**8. Evidence.** Python 3.12.3 · NumPy 1.26.4 · pandas 2.2.3 · SciPy 1.17.1 · scikit-learn 1.8.0 · Flask 3.1.0 · pyarrow 24.0.0 ·
SQLite 3.45.1; C5a isolation; checkout outside `/tmp`.
- **Full suite on `m21/integration` `32f8e7b`** (tree `a862952`: all 16 + E1–E7 + the E2E test; the final head `b7b8a90`
  differs only by one line in `docs/m21/E6_dashboard_app_wiring.md`, and the guards + E2E were re-run on it: 9/9 OK):
  **135 modules, 0 failed**, 1 without tests (`test_m12`, by design); pre-existing skips: m16 1, m17 2, m18 3, m20j 1.
  The cross-lane E2E (`test_m21_e2e_cross_lane`) now includes R1: E5 statement-matched trades flow into E4 while
  journal-only and simulated trades never become evidence, and every E1–E7 runtime path is OFF by default.
- **The first full run found two real guard failures, both fixed on their PRs (no guard weakened):**
  `test_m21_research_data.F4` — every research suite must be listed in the inactive CI example (E1/E3/E5 each add
  their module on its own line); `test_m21_1_wp14_promotion.P6` — every reader of the template-controls store must
  be reviewed in the guard (E6 is now recorded there, with its import set pinned and no broker/order/ledger code).
  Verifying the app.py patch also found that a test importing `dashboard.app` must install C5a isolation first (fixed
  in E6).
- **Branch tips** (own tests + the related WP suite + M17 G10, research foundation, research-data F4, WP14 P6, A01,
  C5a, C5b, hygiene, quarantine): E1 11/11, E2 12/12, E3 11/11, E4 11/11, E5 11/11, E6 12/12, E7 9/9 modules OK.
- **E6 `dashboard/app.py` patch** (applied with the documented procedure in scratch): 21/21 modules OK (protected-file guards incl. the `test_m17` sha256 pin, the M20 runtime guards, C5a, C5b, WP14 P6, WP9, D3, E6 real-app wiring: 401 without a session, 503 with one). An earlier run of `test_m18_ml` on the patched tree: OK. Without the governance half the guards fail, as they must.
- **Mutation testing:** E1 13/13, E2 10/11 (1 equivalent), E3 15/15, E4 21/21, E5 29/29, E6 18/18.
- **OP1 scripts:** verified read-only on a synthetic VPS replica (see 9).

**9. OP1 is final** (runbook OP1.0–OP1.7): scripts in `docs/runbooks/op1/` and `docs/runbooks/sql/`, verified
read-only on a synthetic VPS replica (tree hash including `.git` unchanged; no SQLite side files; no planted secret in
any output; with and without the `sqlite3` CLI). Not run.

**10. Still needs real data or IBKR paper:** OP1 (data inventory, old-system truth, statement exports), real-bar
runs of WP11/WP13/WP16–18 through E3, v3 canary, IBKR paper gateway (OP2), activation (OP3), 10-day soak (OP4),
statement-matched paper outcomes for E4/E5 (C-4), the governance cycle (OP5), D3/E6 dashboard runtime acceptance.

## 8. Decision list (only what is genuinely needed)

| Gate | Decision | Recommendation |
|---|---|---|
| **G1** | Approve this finish line and scope freeze:<br>• the §4 classification;<br>• the completed-candle scanner (WP6) pulled into M21;<br>• A14 split (minimum in M21, the rest in M22 C03);<br>• A08 Telegram alerts not required for M21 sign-off. | Approve |
| **G2** | One **read-only** VPS session (OP1): data inventory, signals-DB copy for the A02 replay, M14/`RISK_*` checks, `.env` backup metadata. No writes. | Approve this week; it unblocks the real-data work |
| **G3** | **Paper-loop activation gate**, given together once WP5–WP7 and WP10 are merged:<br>• merges #12 → #1 → #14 → #13 (after the delta re-checks);<br>• IBKR **PAPER** gateway sessions (OP2);<br>• dashboard restart, ledger migration and flag, unattended paper schedule (OP3). | Approve the merges now; approve OP2/OP3 when the runbook is presented |
| **G4** | Data for M21.6 and survivorship:<br>(a) free sources only (FRED VIX, Alpaca ETFs, official macro calendars, a free earnings API to be verified), with survivorship bias quantified;<br>(b) buy survivorship-free data (≈ $29–199/month Massive, or Norgate). | (a) for M21; revisit before M22 |
| G-opt | Activate the existing inactive offline CI (E09: no secrets, read-only) so reviewers rely on CI runs rather than Claude-reported results. | Optional; it would shorten every review round |

Nothing else needs your approval for M21 engineering. Everything in §5 marked L or R is offline work on branches.

**29 Oct:** the current grouped approvals are in the acceptance packet §20: G-review, C5b, G3a merges, the dashboard shape, G2/OP1 (with your IBKR statement exports), G3b OP2/OP3, G4 and the Vercel decision.

**29 Oct (evening), added by §7d:** (1) how to handle the VPS `sync.sh` auto-deploy before merging (stop it for the merge window and deploy once — recommended — or accept it); (2) the Vercel Production deployments on `main`; (3) the E6 `dashboard/app.py` patch; (4) review of #27–#33. OP1 remains G2.

## 9. Working process (to stop the drift)

1. **Acceptance criteria are fixed in §2 and §5 before coding.** A package is done when its criteria pass, not when a reviewer runs out of ideas.
2. **One review packet per PR:** acceptance checklist, the exact delta range, test commands and output. Re-review covers **only the delta and its direct dependents**.
3. **Mutation testing** only for new safety gates (risk, market clock, order paths, promotion tiers): one run per PR, with no repeat campaigns.
4. **At most two parallel lanes** (L and R), so reviews keep flowing.
5. **Approvals come in gates G1–G4,** not item by item.
6. **The canonical handoff is updated at phase gates,** not for every small correction. This checklist (§6) is updated on every merge, with one short Issue #3 comment.
7. **New ideas** go to a "post-M21" list unless they are a genuine safety dependency. Adding one to M21 needs your explicit decision.
