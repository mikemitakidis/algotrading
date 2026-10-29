# Algo Trading — FULL Canonical Project Handoff & Completion Register
**Snapshot:** 26 October 2026, **status refresh 29 October 2026 (v3.11) Europe/London** · **Version:** 3.11 (29 Oct 2026: final engineering acceptance — 13 defects fixed incl. #17 forged-M14-id, #22/#23 activation race, #25 timezone lookahead; all 16 PRs proven to merge conflict-free onto `main` in order; full suite on the post-merge tree; C5b and dashboard requests; OP1.6 nine categories + OP1.7 statement reconciliation; G4 matrix; `docs/reviews/M21_Final_Engineering_Acceptance_2026-10-29.md`; `main` unchanged; all 71 tasks kept) · 3.10 (28 Oct 2026, late: corrections after the independent review of the accelerated delivery — #23 cross-store governance, #17 risk-evidence provenance, #24 canary experiment identity, plus §6/§7 audit fixes on #20/#24/#25; corrected heads, evidence and OP1.6 in `docs/reviews/M21_Review_Corrections_2026-10-28.md`; `main` unchanged; all 71 tasks kept) · 3.9 (28 Oct 2026: M21 finish line, requirements matrix, remaining-work plan, estimate, merge readiness and decisions in `docs/handoffs/M21_Finish_Line_2026-10-28.md`; A01 completed-bar staleness basis on PR #12 @ `47e9057`; `main` unchanged; all 71 tasks kept) · 3.8 (28 Oct 2026: PR #1 market-clock hardening R1–R3; C01 final design decisions D-1..D-6 (rev 3); A02 candidate ledger offline on draft PR #14; `main` unchanged at `80b4343`; scope unchanged from 3.0) · 3.7 (28 Oct 2026: WP1/WP3/WP4 delivered as draft PRs #12/#1/#13; checkpoint corrections) · previous 3.6 (27 Oct 2026: M21 research PRs #4–#11 merged to main `80b4343`) · **Repo:** mikemitakidis/algotrading · **Dashboard:** algotrading.marketwarrior.club

> **Permanent rule:** This handoff covers every remaining capability from the June 2026 canonical roadmap through M23 and the two-broker operating objective, plus all still-open technical debt and the new advanced AI/self-development research architecture. **No delivery deadlines or calendar estimates.** Work proceeds by dependencies, testing evidence and operator approvals. Preserve original milestone names and numbers. A GitHub merge, a VPS deployment, a runtime activation and an independently validated trading edge are FOUR DIFFERENT STATES.

## 1. Mission / non-negotiable product goals

Build a sophisticated continuously learning trading research-and-execution platform that improves in BOTH dimensions, in parallel:
- **Evidence intelligence:** more point-in-time, trustworthy observations and counterfactual outcomes for accepted, rejected and blocked signals; realistic costs/fills; accurate effective sample sizes; immutable trial history; adversarial validation.
- **Advanced AI intelligence:** feature discovery, strong classical baselines, boosting/ensembles, meta-labeling, calibrated probability/uncertainty, regime conditioning, concept-drift monitoring, champion/challenger, safe autonomous multi-agent research, learning from failed hypotheses and published results. No model complexity earns an automatic promotion.
- **IBKR PAPER = permanent research lab**, even if a later eToro live lane exists. Scanner → score → risk → paper entry/exit → durable outcome → research/training → shadow validation must run reliably before paper results are trusted.
- **eToro LIVE = eventual restricted, operator-governed track-record lane.** Begin only with verified funded-broker capability, full risk parity, paper evidence, manual approvals and tiny capital; scale only on out-of-sample net performance, drawdown, execution reliability and legal/platform feasibility. A public/copyable record is an objective, **not a confirmed available API capability or income guarantee**.
- Continue producing clear LONG/SHORT technical signals distinctly from actual broker orders; do not conflate a SHORT-entry candidate with selling an owned position.

## 2. Sources and conflict-resolution rules

1. **Original business/milestone scope:** user-supplied “30.06.2026 Algo_Trading_Bot_Canonical_Roadmap.docx” (v2, document itself dated 29 June). It retains M21.U, M21.1, M21.1extra, M21.2–M21.7, M22.1–M22.6, M23.1–M23.5, four starter templates, promotion tiers and two broker lanes.
2. **Live code truth:** verified GitHub branches and exact SHAs below. Do not treat the historically dated ROADMAP.md or MILESTONE_STATUS.md header as up-to-date; both still describe D3 as “not started”, though its code merged in October. Update those docs as a separate, reviewable documentation task.
3. **Outstanding older debt:** docs/NEXT_WORK_REGISTER.md, M14 audit carry-forward, M17 real-intraday replay debt and older October C5a/roadmap handoff. Keep deliberate deferrals visible; don't silently mark them done.
4. **New R&D design:** Issue #2 and research/m21-autonomous-research-engine, including the independent critique on Issue #2. Integrate its 24 research capabilities into M21/M23 sub-work, not new top-level milestone numbers.
5. **This file + Issue #3** becomes the operational full handoff. Every subsequent handoff must refresh each status, last tested SHA, blockers, approvals and complete unfinished checklist; never replace this with only the next workstream.

## 3. Verified checkpoint — not a claim of runtime completion

| Scope | Verified state as of snapshot | Remaining gate |
|---|---|---|
| M1–M18, M19/M20 and M21.U | Foundations in main; historical implementation recorded as closed, with known deferred follow-ups | Reuse and extend; do not redo |
| C5a/C5b protected-baseline/test isolation | **Merged** into main, before latest October integration | Keep narrow exact-pinned guards |
| M21.1 signal scoring and scanner bridge | M19 research-grade scoring + enrichment bridge exist; no proven calibrated live win probability | Prove actual scanner-to-score parity, freshness, traceable ranked decisions |
| M21.1extra D0/D1/supervised D2 | Pre-existing safety groundworks delivered | Unattended lifecycle not activated |
| WS1 / D3 outcome metrics | **Code merged** to main at 015d2ce | Dashboard service NOT restarted; panel not yet runtime verified; genuine net outcomes absent |
| WS3 Telegram BUY/SHORT signal alerts | **Code merged** at 015d2ce | No live send, schedule or duplicate-notifier resolution approved |
| WS2 corrected unattended paper loop | **Draft PR #1**, branch m21-ws2-corrected-review @ **0f3f9e6db860bb2359238c8b83283d0c93d76a4e** (v3.8: refreshed from main, market-state boundary, hardening R1–R3; previously 5d6654d); offline suite green (Claude-reported) | Operator merge permission, independent CI/review gates, separately approved genuine IBKR paper-adapter tests, gateway and activation |
| Research architecture | Proposal **r2** (Issue #2). Offline research lanes (foundation, B integrity, A counterfactual, F data QA, C model lab, D genome/backtester, E offline agents, six-lane integration) **CODE_MERGED to main `80b4343`** (PRs #4–#11, operator-authorised, 27 Oct 2026) after three independent reviews and one comprehensive audit; TESTED_OFFLINE on synthetic data only (§3a) | NOT data-, runtime- or trading-verified; integrated verdict INCONCLUSIVE; real-data use needs approval (B01/E05) |
| GitHub main | **80b4343db4a22413aa670efd32e24952d53e532e** (27 Oct 2026, merge of PR #11; previously 015d2ce). Tree identical to the reviewed integration head `a645a91`; 89 files changed vs 015d2ce, all under `research/`, `docs/research/`, `test_m21_research_*.py` plus test-only M17/M18 hygiene allow-lists; no `bot/`, `dashboard/`, `tools/`, `main.py`, `requirements.txt` or active workflow changes | No changes to main without separate explicit approval. Vercel auto-deployed the repo's web project (READY); that is NOT the trading runtime |
| Last operator-reported VPS | Trader inactive; ibgateway failed; dashboard + Caddy running; no dashboard restart after WS1; no Telegram, no new IBKR orders; sync daemon off | Current runtime must be independently rechecked at activation, not guessed |
| M21.2–M21.7, M22, M23 | Unfinished or only supporting infrastructure exists | Completion tasks follow |

### 3a-000000. v3.11 — final engineering acceptance (29 Oct 2026)

**Input:** the operator's "M21 final engineering acceptance and merge-readiness program" (29 Oct). No new development cycle.

**Result** (full record: `docs/reviews/M21_Final_Engineering_Acceptance_2026-10-29.md`):
- **Heads:**
  - #17 `bf94f71`, #21 `fbfc815`, #22 `b4e9955`, #23 `04bf5ca`, #18 `9012711`;
  - #20 `4924844`, #24 `4b677bc`, #26 `89bc874`, #25 `f05ed84`;
  - unchanged: #12, #1, #14, #13, #15, #16, #19.
  - `m21/integration` `79ae46f`.
- **Defects fixed (13):**
  - **#17:** M14_LIVE now authenticated against the persisted M14 decision; tick time window; only M14_LIVE permits an intent.
  - **#22/#23:** compare-and-swap activation and revert; post-activation digest and previous-version verification; never-live ROLL_BACK.
  - **#24:** explicit planted-effect identity and environment.
  - **#20:** G9 root cause (per-run id in `report.json`).
  - **#18:** future-stamped readings are UNKNOWN.
  - **#25:** UTC publication instants; minimum 22 h backfill lag.
  - **#18/#25:** allow-list anchors, so the sequential merge onto `main` is conflict-free.
- **Merge proof:** all 16 PRs merge onto `main` in order, conflict-free, ending at tree `f1ff536`, which is the integration tree minus the E2E file and its allow-list entries. The full suite ran on that tree.
- **Protected files:** C5b sign-off requested for #12 and #16 (`bot/scanner.py`, `bot/providers/alpaca_provider.py`); dashboard shape requested (blueprint plus one line). Nothing protected was modified.

**Task statuses.** No task changed to CLOSED; all 71 are kept.
- A15/A16 backends: READY_FOR_REVIEW. The A16 UI is INCOMPLETE (E6, protected).
- B08: v3 prepared, not run.
- B18: WS2 logging is INCOMPLETE (E2).
- §5 loop and C-4: score-vs-outcome (E4) and fill reconciliation (E5) are INCOMPLETE.
- E08: E7.
- Everything real-data or runtime stays DATA_UNVERIFIED or RUNTIME_UNVERIFIED until OP1–OP4.

### 3a-00000. v3.10 — corrections after the independent review (28 Oct 2026, late)

**Inputs:**
- Issue #3 review record 6475528106;
- PR reviews 5686441635 (#23), 5686442374 (#17), 5686443135 (#24);
- the operator's review brief: finish verification, fix defects, do not expand M21.

**Fixed, each on its owning branch, with normal merges downstream:**
- **#23** `28c1943`: durable cross-store promotion operations, reconcile, BLOCKED + `resolve_block`, an atomic one-active rule, a state-checked `record_shadow`.
- **#17** `4c1cac7`: risk evidence M14_LIVE / VERIFIED / RECONSTRUCTED_INFERRED / UNVERIFIED. A journal order is never turned into an M14 PASS, and `intent()` refuses non-authorising evidence.
- **#24**: canary experiment identity bound and verified by the report card; v2 preserved; v3 preregistered, not run. Also: real walk-forward input must be close-stamped, and unknown-exit trades block a PASS.
- **#20**: M17 multi-timeframe and M18 real-data entrances refuse M16 start-stamped bars (lookahead).
- **#25** `d992a0f`: index/breadth features refuse start-stamped bars.

**Evidence:** an integration tree rebuilt by normal merges; full suite and mutants as reported in the corrections doc.

**Task statuses.** No task changed CLOSED status. The A10–A13 runtime items stay BLOCKED (OP2/OP3).
- B01/E05 (real-data readiness): DATA_UNVERIFIED until OP1.
- B13 (M21.3 promotion): TESTED_OFFLINE, RUNTIME_UNVERIFIED (OP5).
- The Finish Line §6 checklist carries DATA_UNVERIFIED / RUNTIME_UNVERIFIED markers.

### 3a-0000. v3.9 — M21 finish line (28 Oct 2026)

**Operator instruction (28 Oct, 08:46).** The operator asked for:
- a fixed, measurable M21 definition of done;
- a requirements matrix against the June roadmap, a remaining-work plan and a bottom-up estimate;
- merge readiness for #12, #14 and #1, and a short decision list;
- the WP1 daily-bar freshness fix.

Then STOP. Excluded: merges, VPS, broker, orders, production DB, CI, Telegram.

**The finish line now lives in one file:** [`docs/handoffs/M21_Finish_Line_2026-10-28.md`](M21_Finish_Line_2026-10-28.md). It is the **single authoritative M21 checklist** (its §6). It contains:
- engineering / operational / performance gates A, B and C;
- the requirements matrix (M21.U–M21.7 plus Appendix A);
- the 71-task classification for M21 sign-off (none removed);
- work packages WP5–WP19 and operator gates OP1–OP5, with dev-day estimates;
- completion windows, merge readiness, and decisions G1–G4 (+ optional CI).

**Planned windows** (estimates with stated assumptions, not deadlines; see §6 rule 6):
- engineering ≈ 9 Dec 2026;
- operational acceptance ≈ 11–16 Dec 2026;
- full M21 acceptance ≈ 8–15 Jan 2027.

**PR #12 (A01) @ `47e905709919ed11f3ab31bc4be0e058df2fce8e`:**
- **Change:** an opt-in completed-bar staleness basis (`TruthPolicy.bar_basis="completed"`).
  - Lag is measured from the close of the following (newer) bar, so a prior daily close is current for the whole next session.
  - A forming bar is INVALID under this basis.
  - The default (`in_progress`) is unchanged for every caller.
- **Tests:** 7 new tests plus 1 default-regression test (the A01 suite now has 67). 6/6 targeted mutants killed.

**Merge readiness.**
- All four heads (#12, #1, #14, #13) were trial-merged locally onto `80b4343` with no conflicts. The local tree is `a5036df` and was not pushed.
- 23 targets passed on it under Python 3.12.3 / NumPy 1.26.4. Claude-reported.

**Scope frozen** to the June M21 text plus genuine safety dependencies, pending operator decision G1:
- the completed-candle scanner (C01 §11) is pulled into M21 as WP6;
- the rest of C01 stays an M22 hard blocker.

### 3a-000. v3.8 development record (28 Oct 2026)

**Operator instruction (28 Oct, 08:03).** The independent reviewer confirmed that **PR #12's risk-decision provenance finding is resolved at `8013044`**. Then:
1. harden PR #1 (R1–R3);
2. finalise the C01 decisions;
3. prepare and start A02 offline;
4. verify, push, update this handoff, and STOP.

Excluded: merges, VPS access, broker sessions, orders, production DB writes, CI, Telegram.

**Status:**
- **Nothing is merged.** `main` = `80b4343db4a22413aa670efd32e24952d53e532e`.
- All PRs are draft, with zero unpushed commits.

| item | PR (draft) | head | state |
|---|---|---|---|
| A01 truthful scoring contract | #12 | **`80130441309b6023f243e9a3ad268cae5b13aa0d`** (unchanged) | TESTED_OFFLINE; provenance finding resolved per review; awaiting merge decision |
| A09 WS2 refresh + market-state boundary + hardening R1–R3 | #1 | **`0f3f9e6db860bb2359238c8b83283d0c93d76a4e`** (`2562c5c` hardening, `0f3f9e6` redundant-clamp removal) | TESTED_OFFLINE |
| C01 parity design, rev 3: final decisions D-1..D-6 | #13 | **`6a8a690eea14a639836af3fbe48f563c83288d73`** (`8c6b050` rev 3, `6a8a690` G10 allowlist fix) | DESIGN ONLY |
| A02 candidate-decision ledger (offline, flag-gated, not wired) | #14, stacked on #12 | **`71bccd60900bf444e818a1e494c567ca498b37f7`** | TESTED_OFFLINE; nothing deployed or enabled |

**PR #1, R1–R3:**
- **R1:** the gate only accepts a `TrustedMarketStateProvider` (exact type), built only from exact `AlpacaClockSource` instances; the decision must name source `alpaca` with status ok.
- **R2:** an absolute 30 s ceiling, enforced at construction and in the helper; a limit can only tighten.
- **R3:** a session close that is valid, in the future and ≤ 24 h ahead is required.
- **Tests:** they drive the real Alpaca source (HTTP stubbed) and the real D1 guard. WS2 123 tests, boundary 15, D1 48 + 56, D2 suites, guards: all OK outside `/tmp`. All targeted mutants killed; one equivalent mutant removed as dead code.

**C01 rev 3:**
- **Decisions D-1..D-6 final:** the simulation boundary; M14 for broker-connected entries; governor-persisted authority (store schema specified); completed candles (new §11 with test matrix T-N); reduce-only flatten with closed-market broker semantics (new §12); no silent daily-loss override.
- **Finding:** A01 needs a completed-bar staleness basis (lag from the next bar's close).
- **Fixes:** the doc branch now registers its file in the M17/M18 G10 allowlists, so G10 passes. This handoff branch received the same fix.

**A02 (PR #14):**
- **Ids and stages:** lane-A-identical candidate ids; seven stages; nine terminal kinds.
- **Schema:** additive (`candidate_ledger`, `candidate_ledger_events`, `candidate_ledger_meta`). The migration is explicit, idempotent, all-or-nothing and version-checked, and touches no existing table.
- **Writer:** off by default (`CANDIDATE_LEDGER_ENABLED=1` exactly); never creates schema; uses its own connection; idempotent; order-checked; never raises.
- **`account()`:** stage accounting plus join integrity.
- **Offline legacy replay:** read-only source, output under `/tmp` only.
- **Plan:** `docs/M21_1_A02_candidate_ledger_plan.md`.
- **Tests:** 23; 12/12 mutants killed.

**Test evidence (Python 3.12.3 / NumPy 1.26.4, C5a isolation; Claude-reported, not independently re-run):**
- PR #1 @ `0f3f9e6`: see above.
- PR #14 @ `71bccd6`: 14 targets OK (A02, A01, bridge, group D, A-path, alerts, B1, guards, research foundation).
- PR #13 @ `6a8a690`: G10 ×2, hygiene, quarantine and C5b OK.

### 3a-00. v3.7 development record — approved offline package (27–28 Oct 2026)

**Approval:** the operator's "approved next offline development phase" (27 Oct 19:04).
- Approved work: WP1 (A01), WP3 (A09), WP4 (C01 design); WP2 (A02) only after the WP1 contract stabilises.
- Excluded: merges, VPS, broker, Telegram, CI, production DB, spending.

**Status:**
- **Nothing is merged.** `main` = `80b4343db4a22413aa670efd32e24952d53e532e`.
- All PRs are draft, with zero unpushed commits.

| WP | PR (draft) | branch | head | state |
|---|---|---|---|---|
| WP1 A01 truthful scanner → M19 scoring contract | #12 | `m21.1/a01-scoring-truth` | **`8013044`** (`6242bde` contract, `3f1fb1e` C5b transition, `8013044` risk-decision provenance fix) | TESTED_OFFLINE; awaiting exact-head re-review |
| WP3 A09 WS2 refresh + market-state boundary | #1 | `m21-ws2-corrected-review` | **`46c87ab`** (`8242a12` merge of main, `46c87ab` boundary fix) | TESTED_OFFLINE; exact-head audit posted; merge needs separate approval |
| WP4 C01 parity design (design only) | #13 | `docs/m22-c01-risk-parity-design` | **`4584a3a`** (`bfd9fdc` design, `4584a3a` revision 2: policies P-1..P-6) | DESIGN ONLY |

**Independent checkpoint (ChatGPT, Issue #3 comment 6463489608), 28 Oct.** The WP1 structure, the WP3 boundary and the WP4 design-only scope were confirmed. Each follow-up was addressed:
- **WP1 (review 5679297098):** a caller-built `RiskDecisionRef` could claim an M14 approval.
  - **Fixed in `8013044`.** The reference is now only a pointer. A decision counts only when it is read back from M14's `risk_decisions` through `ReadOnlyM14DecisionStore` (SQLite `mode=ro` + `query_only`, one SELECT) and matches the candidate: `trade_open`, scope, symbol, side, leverage, approved notional ≥ intended, timing.
  - Otherwise it is UNVERIFIED and M19 blocks.
  - Regression: an invented but valid id cannot pass. 9/9 provenance mutants killed.
- **WP3:** exact-head audit posted (PR #1 comment 6463712926).
  - Evidence: 121 WS2 tests plus the D1/D2 suites and guards OK; 6/6 mutants killed.
  - Three LOW residuals (R1 typed-not-provenanced decision, R2 unbounded max-age configuration, R3 no `next_close`) are proposed as hardening for operator approval and were not applied.
- **WP4:** revision 2 adopts policies P-1..P-6:
  - M14 for broker-connected paper/live;
  - a provable no-broker/no-network boundary for any simulation exemption;
  - governor-persisted authority (no store exists yet);
  - unknown/stale/incomplete inputs block entries, which implies closed-bar scanner evaluation;
  - one-shot entries via M14, and flatten via a separate audited reduce-only path;
  - no silent daily-loss override for automatic entries.

**Consequence of A01 (operator decision pending):** callers that score without evidence now fail closed. These are the A-path sim, B1 dry-run, WS3 alerts and, after A01 merges, the WS2 scheduler.

**Test evidence (Python 3.12.3 / NumPy 1.26.4, C5a isolation, Claude-reported; not independently re-run):**
- **WP1 @ `8013044`:**
  - 14 targets OK (A01 59 tests);
  - the 48 scanner-pin guard tests were run at `3f1fb1e`, before the provenance fix;
  - mutants: 10/10 at `3f1fb1e` and 9/9 at `8013044`.
- **WP3 @ `46c87ab`:** WS2 121 tests, D1 48, D2 gates 22, D2.0 33, D2 lifecycle 25, plus guards; 6/6 mutants killed.
- **Sandbox artifact:** path-guard tests that reject repo paths fail only when the checkout lives under `/tmp`; they pass outside it.

### 3a-0. v3.6 merge record — M21 research lanes on `main` (27 Oct 2026)

The operator authorised the merge. ChatGPT performed it with merge commits, retargeting each stacked PR to `main` and requiring the exact reviewed head (Issue #3 comment 6450481056).

**Claude independently verified the result on 27 Oct 2026:**
- `origin/main` = `80b4343db4a22413aa670efd32e24952d53e532e`.
- Each merge commit's second parent is the reviewed PR head.
- `git diff a645a91 main` is empty.
- The research suites re-run on `main`: Python 3.12.3 / NumPy 1.26.4, 8 modules (213 tests) OK.
- The guards re-run on `main`: Python 3.13, hygiene, quarantine, C5b, C5a and G10 ×2 (79 tests) OK.

| PR | lane (tasks) | reviewed head | merge commit |
|---|---|---|---|
| #4 | research foundation | `1569235` | `fcb7713144d4d165b4358e07c9dcc40ee0601f70` |
| #5 | B integrity (B05–B08, B12, B20) | `3b5e8f9` | `96bf39bc2d5c1ebb7b34ed5c6094b86637813f6e` |
| #6 | A counterfactual (A02 part, A17 synthetic, B03, B04) | `6f51f79` | `5fa4d1c69dafd245873c9c26c705d249ae0cdebf` |
| #8 | F data QA (B01, B02, E05, E09) | `22d07d5` | `095274f76827bfa93870386b5191960fceb2768b` |
| #7 | C model lab (B14–B17, B19, B23 block, D07) | `f3801bf` | `cf46fea32fce080d357060184169f55934afd058` |
| #10 | D genome / backtester (A15, B09–B11) | `66f1555` | `40ba7dbfdbbf21a05e77b4040ecf84f2b6ccc31a` |
| #9 | E offline agents (B21, B22, D06) | `44be47b` | `c7775db0411bd787487b24ff72442a357b1a2e3c` |
| #11 | six-lane integration | `a645a91` | `80b4343db4a22413aa670efd32e24952d53e532e` |

**Review trail:**
1. Second independent review (Issue #3 6438467140 / 6438712526): corrected.
2. Third review (6443615799): corrected.
3. Final follow-up (6446441615): corrected.
4. Comprehensive source audit (6447127696): seven findings, all corrected (v3.5).

**Verification vocabulary (v3.6).** Every task carries exactly the states it has proved:
- **CODE_MERGED** — on `main`.
- **TESTED_OFFLINE** — automated tests pass offline, on synthetic or fixture data.
- **DATA_VERIFIED** — validated on approved real historical data, with point-in-time and corporate-action correctness.
- **RUNTIME_VERIFIED** — observed working in the deployed runtime, with operator approval.
- **TRADING_VERIFIED** — genuine broker paper or live outcomes reconciled; for real money, M22 gates.

No M21 research task is DATA_VERIFIED, RUNTIME_VERIFIED or TRADING_VERIFIED. A merged research foundation does **not** complete its milestone.

**Results that remain true after the merge (synthetic only):**
- The six-lane integrated verdict is **INCONCLUSIVE**: 6 of 9 search trials hold positions across suspected unadjusted splits, and there is no calibrated integrated canary.
- The lab's `linear` and `regime` benchmarks are not matched.
- The calibrated lab card FAILs.
- No trading edge is shown.

**Still to resolve before research results can count:**
- approved real-data inventory and snapshot (B01, E05);
- validated corporate-action, point-in-time and liquidity policy (B10, E05, A17);
- an independent signer (B06);
- OS-level agent isolation (B22);
- a calibrated integrated canary (B08, B20);
- a clean-runner CI and optional-model check (E09);
- real-bar M17 parity (A17).

### 3a. Session delivery 26–27 Oct 2026 (Claude Code): offline development history (now merged, see 3a-0)

**v3.3 update (27 Oct 2026, after the second independent exact-head review, Issue #3 comments 6438467140 / 6438712526 and reviews on #5–#11).** Every finding was corrected on its owning branch and merged downstream (no rebase, no force-push). All heads pushed; zero unpushed commits; all PRs still draft. Every result is synthetic-machinery evidence, not a market edge and not P&L.

| Lane | Branch | Head at review → **head now** | Draft PR (base) | Tests on the new head | Repro mutants (fix reverted → test fails) |
|---|---|---|---|---|---|
| Foundation | `research/m21-foundation` | `97e295d` (unchanged; no finding) | #4 (main) | foundation (30) + guards | — |
| B: integrity (B05–B08, B20) | `research/m21-b-integrity` | `9e0a292` → **`da6052cef5cc67aaa3753dc4c4755c86c1e8d553`** | #5 (#4) | integrity (43) + guards | R5-1…4 killed |
| A: counterfactual (B03, B04) | `research/m21-a-evidence` | `00bb121` → `969cec7` → `ed62fdb` → **`62232507b64ed9c93d9d89c249ec93d1442e139d`** (v3.4 final) | #6 (#4) | evidence (28) + guards | R6-1…4 killed |
| C: model lab (B14–B17, B19, D07) | `research/m21-c-mllab-wip` | `282df4a` → **`8f64a07665a584c420ff4ab216e684daaadf7fc5`** | #7 (#5) | mllab (42) + integrity + guards incl. C5a | R7-1…5 killed |
| D: genome, templates, backtester (A15, B09–B11) | `research/m21-d-genome` | `f0d576a` → **`33a05ad3ac1af111f544dbf0b6b901b3c9c029a3`** | #10 (#5) | genome (24) + G10 ×2 + C5a/C5b + guards | R10-1…5 killed |
| E: agents and knowledge library (B21, B22, D06) | `research/m21-e-agents` | `20a639a` → **`0845a6982f3df0f8d90139119bd633e2130f228e`** | #9 (#10 branch) | agents (13) + genome + guards | R9-1…3 killed |
| F: data inventory, quality, vendors (B01, B02, E05, E09) | `research/m21-f-data` | `39e9b5b` → `e813b49` → **`77139ccf56cc83f735a2c2496080500000285ba2`** (v3.4) | #8 (#4) | data (14) + guards | R8-1…4 killed |
| Integration: six lanes end to end | `research/m21-integration` | `f73781b` → `edad279` → `5cc3dce` → **`98d1067f7aa1cd4bd9d31e1fa921d0378cedf39f`** (v3.4 final) | #11 (E branch) | **12 modules, 264 tests (205 research + 59 guard) on Python 3.13; the 8 research suites (205 tests) on Python 3.12.3 / NumPy 1.26.4 / pandas 2.2.3 / scikit-learn 1.8.0** | R11-1…6 killed |
| Handoff | `docs/full-canonical-handoff-2026-10-26` | this commit | — | — | — |

**Comprehensive independent source audit (27 Oct 2026, Issue #3 comment 6447127696): seven findings, all CORRECTED, awaiting exact-head re-verification (v3.5).**

| PR | head audited → **head now** | correction | regression evidence |
|---|---|---|---|
| #4 foundation | `97e295d` → **`1569235`** | P2: `resample` ORs `quality_flags` bits (was `max`) | `test_resample_ors_quality_flag_bits` (real M16 bits, 1H/4H/1D); R4-1 killed |
| #5 integrity | `da6052c` → **`3b5e8f9`** | P1: null-only batteries are `NULL_ONLY`, never CALIBRATED; the card verifies the battery in its own ledger, for `pipeline_identity(family, code)`, with positive-control power re-derived from recorded counts; `require_canary=False` can never PASS. P2: PBO/SPA/RC only over the complete comparable family | canary negatives (null-only, other pipeline/code/ledger, fabricated/edited/forged result, low power, diagnostic config); omitted-trial family test; R5-5…7 killed |
| #7 model lab | `8f64a07` → **`f3801bf`** | P1: fold threshold + conformal use only outcomes resolved before the fold; P1/P2: labels whose path crosses a missing or blocked bar are dropped; context eligibility mask (mechanism for #11) | adversarial delayed-outcome test across 3 folds; removed/blocked stop bar test; context-mask unit test; R7-6, R7-7 killed; evidence regenerated |
| #8 data | `77139cc` → **`22d07d5`** | P2: inactive CI example lists all eight research suites (agents, integration added); still INACTIVE | suite-coverage test fails on the old example |
| #11 integration | `98d1067` → **`a645a91`** | P1: market context excludes quarantined (symbol, bar) cells; min 3 eligible symbols or context unavailable | future-split context test with leaky control; invariant `lab_context_uses_point_in_time_eligibility`; R11-7 killed |
| #6, #10, #9 | `6223250`, `33a05ad`, `0845a69` → **`6f51f79`**, **`66f1555`**, **`44be47b`** | propagated by merges (no finding) | suites re-run on each head |

Tests on the integration head: 14 targets, 292 tests OK (8 research suites = 213, plus hygiene, quarantine, C5b, C5a = 59, plus G10 ×2 = 20) (Python 3.13); 8 research suites, 213 tests OK (Python 3.12.3 / NumPy 1.26.4). Evidence: lane C regenerated (linear/regime still not matched; calibrated card verifies its battery and still FAILs). Integration: 24/24 invariants; integrated verdict INCONCLUSIVE; lab 99 rows (14 unobserved paths excluded), card FAIL.

**Third review (27 Oct 2026, Issue #3 comment 6443615799; reviews on #6, #8, #11): three targeted corrections, all CORRECTED, awaiting final independent verification.** Unchanged heads: #4 `97e295d`, #5 `da6052c`, #7 `8f64a07`, #9 `0845a69`, #10 `33a05ad`.
- **#8 → `77139cc`:** `admissible_mask` requires finite, non-negative volume (NaN, ±inf and non-numeric are excluded). Zero volume stays a warning, documented as no evidence that a fill was executable.
- **#6 → `ed62fdb` → `6223250`:** a `NO_POSITION` candidate gets the new stage `unfillable`. It is never `executed`, holds no slot and uses no daily capacity, in either symbol-capacity mode (the final follow-up fixed the `one_open_per_symbol=False` path). The fix was merged into #11 at `98d1067`.
- **#11 → `5cc3dce`:** fail-closed split-crossing guard. Any trade, search trial, label or lab row whose holding window contains a suspected unadjusted split is flagged. A contaminated selected run or search family makes the integrated verdict INCONCLUSIVE, and buy-and-hold is reported INVALID. No hindsight pre-close is used. In the committed fixed-seed run, the selected genome's 62 trades are clean but 6 of 9 search trials cross a split, so the integrated verdict is INCONCLUSIVE (economics invalid). A multi-seed control and a hand-built fixture are tested.

**Second-review finding dispositions (CORRECTED; confirmed substantially closed by the third review):**
- **#5:** report card verifies outcomes (candidate gross + every family-matrix column) against trial artifacts; attestations re-verified cryptographically at consumption; holdout release needs a signed, unexpired approval and an attestation before the forward window and the seal; the HMAC verifier is labelled a local policy model (independent = False) everywhere.
- **#6:** release maps fail closed for accepted candidates; real data needs explicit bar opens; `simulated_entry` stage so backtest fills are never "executed".
- **#7:** dataset digest covers every field (schema v2); ACI uses labels only after they resolve before the decision; real code identity + outcome artifacts on trials. Evidence regenerated with nothing re-tuned: null and interaction matched; **linear and regime still NOT matched**; canary CALIBRATED (0/59, power 20/20 at ×1.5); calibrated card FAIL.
- **#8:** quality flags drive grades and admissibility (warning vs blocking; unknown bits block); retrospective audit separated from a causal split flag.
- **#10:** trades report decision time, bar-open fill events, bar-close labels and unobservable intrabar windows (M17 legacy labels kept for parity; parity still 1,784 / 0); one decision record per candidate; search trials carry real code identity, the market seed and outcome artifacts.
- **#9:** operator verification requires the operator's signature over the source content; identity strings, self-declared status, forged entries and replays are refused.
- **#11:** point-in-time universe (negative test: a later split/flag changes nothing earlier; the old rule is the leaky control); every engine decision labelled with its real stage and joined to trades on id and fill time; card outcomes verified, still INCONCLUSIVE (no integrated canary; lane C's not borrowed); in-process runtime network/process guard, stated as not OS isolation.

**Status separation (27 Oct 2026, v3.5).** Applies to every research lane above.

| state | B | A | C | D | E | F | integration |
|---|---|---|---|---|---|---|---|
| implemented | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| tested (offline, synthetic) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| independently reviewed (current head) | ❌ pending | ❌ pending | ❌ pending | ❌ pending | ❌ pending | ❌ pending | ❌ pending |
| merged to main | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| deployed | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| runtime-verified | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| trading-verified | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

**Evidence reports**, all in `docs/research/evidence/` unless stated: `lane_b_integrity.md`, `lane_a_counterfactual.md` (not regenerated: identical semantics for complete maps), `lane_c_mllab.md` (regenerated), `lane_d_genome.md` (regenerated; economics unchanged), `lane_e_agents.md` (regenerated), `docs/research/lane_f/lane_f_data_report.md` (regenerated) and `vendor_licensing_memo.md`, `integration_e2e.md` (regenerated: 22/22 invariants).

**Unresolved limitations (carried, not hidden):**
- Control batteries are verified against the ledger; a forged ledger entry with internally consistent counts would still verify (ledger = trust boundary; no authority-signed batteries yet).
- Market-context masking uses the causal split flag; a split it misses still enters the context until validated corporate-action/adjusted bars exist.
- Without an exchange calendar, ML label observability treats holidays as gaps (conservative).
- No validated corporate-action/share-count adjustment: economics of any holding window across a suspected unadjusted split are invalid and fail closed (tracked under B10/E05); adjusted point-in-time bars or a verified split policy are required before such results can count.
- Liquidity is not modelled: zero-volume bars are admissible for research but are no evidence of an executable fill.
- The authority is a local HMAC policy model; an operational asymmetric signer held outside every research runner is still required (B06).
- Runtime isolation is an in-process guard only; real agents or networked code need an OS sandbox.
- Labels measure a fixed bracket; engine trades also use template signal exits; they are separate measurements. Capacity/risk blocks did not bind at the integration's default size.
- The integrated pipeline has no calibrated canary, so it cannot PASS by design. Synthetic data only; no real bars in the repository.
- Lane C linear/regime benchmarks remain unmatched findings.

**Deferred tests, recorded so they are not lost:** full D (15), E (6) and F (7) mutation lists (only targeted repro mutants were run, as instructed); the full `_test_isolation.py --all` sweep of non-research modules (no production code changed); the M18 full suite on lane branches; LightGBM/CatBoost under Python 3.12 (not installed there).

### 3b. Milestone status summary (M21.U → M23.5), derived from §4

| milestone | verdict (v3.6) | tasks: status |
|---|---|---|
| M1–M20, M21.U | — | Foundations in main (historical, closed with known deferred follow-ups): see §3 |
| M21.1 scoring / candidate audit | **NOT COMPLETE** — A01 on draft PR #12 and A02 on draft PR #14 (both tested offline, not merged; A02 not deployed or wired) | **A01** PR_PENDING · **A02** PR_PENDING |
| M21.1extra (C5a/C5b, D0–D3, WS1–WS3, paper loop, monitoring, dashboard, real-bar parity) | **NOT COMPLETE** — code for C5, D0–D2, WS1, WS3 merged; WS2 (PR #1) pending; runtime/paper acceptance blocked on approval | **A03** ✅ CLOSED · **A04** ✅ CLOSED · **A05** ✅ CODE_MERGED · **A06** RUNTIME_UNVERIFIED · **A07** ✅ CODE_MERGED · **A08** BLOCKED · **A09** PR_PENDING · **A10** BLOCKED · **A11** PR_PENDING · **A12** BLOCKED · **A13** PR_PENDING · **A14** NOT_STARTED · **A16** NOT_STARTED · **A17** CODE_MERGED |
| Four starter templates (June roadmap) | research CODE_MERGED; not data-verified | **A15** CODE_MERGED |
| M21.2 parameter optimisation | foundations merged; **not complete** | **B09** CODE_MERGED · **B10** CODE_MERGED · **B11** PARTIAL |
| M21.3 walk-forward, promotion model, integrity | integrity stack merged; promotion model (B13) not started; **not complete** | **B05** CODE_MERGED · **B06** CODE_MERGED · **B07** CODE_MERGED · **B08** CODE_MERGED · **B12** PARTIAL · **B13** NOT_STARTED |
| M21.4 meta-labeling | research merged (synthetic); **not complete** | **B16** CODE_MERGED |
| M21.5 regime-aware thresholds | research merged (synthetic); **not complete** | **B17** CODE_MERGED |
| M21.6 market context | **not started** (lab context features are research inputs only) | **B18** NOT_STARTED |
| M21.7 learned regime weights | partial research; **not complete** | **B19** PARTIAL |
| Evidence and advanced-AI research capabilities (Issue #2) | core lanes merged; real-data and runtime validation outstanding | **B01** CODE_MERGED · **B02** CODE_MERGED · **B03** CODE_MERGED · **B04** CODE_MERGED · **B14** CODE_MERGED · **B15** CODE_MERGED · **B20** PARTIAL · **B21** CODE_MERGED · **B22** CODE_MERGED · **B23** NOT_STARTED · **B24** NOT_STARTED |
| M22.1–M22.6 controlled real-money readiness (NO live authorisation; C01 HARD blocker) | **not started** (C01 design only, PR #13) | **C01** DESIGN_ONLY · **C02** NOT_STARTED · **C03** NOT_STARTED · **C04** NOT_STARTED · **C05** NOT_STARTED · **C06** NOT_STARTED · **C07** NOT_STARTED · **C08** NOT_STARTED · **C09** NOT_STARTED · **C10** NOT_STARTED · **C11** NOT_STARTED |
| M23.1–M23.5 | **not started** | **D01** NOT_STARTED · **D02** NOT_STARTED · **D03** NOT_STARTED · **D04** NOT_STARTED · **D05** NOT_STARTED |
| M23 extended (autonomous R&D, uncertainty economics, twin, audit, acceptance) | offline foundations only | **D06** PARTIAL · **D07** CODE_MERGED · **D08** NOT_STARTED · **D09** NOT_STARTED · **D10** NOT_STARTED |
| Carry-forward audit / data / security / governance | mostly open | **E01** NOT_STARTED · **E02** NOT_STARTED · **E03** NOT_STARTED · **E04** NOT_STARTED · **E05** PARTIAL · **E06** NOT_STARTED · **E07** BLOCKED · **E08** NOT_STARTED · **E09** PARTIAL |

All **71** tasks (A01–E09) remain in §4 with their full statuses and original milestone references. ✅ marks the 4 tasks closed earlier. No task was removed. No milestone M21.1–M23.5 is complete.

**v3.9:** the classification of each task for M21 sign-off is in `docs/handoffs/M21_Finish_Line_2026-10-28.md` §4:
- 25 mandatory, still open;
- 1 closeout task;
- 1 decision;
- 9 supporting, already delivered;
- 14 not required for M21 sign-off;
- 11 M22 tasks;
- 10 M23 tasks.

This classification changes no task's status.

## 4. Full remaining-work inventory — carry every unchecked item until proved CLOSED

The task IDs below are **tracking IDs, not replacement milestone numbers**. A task is CLOSED only if GitHub SHA, exact test evidence and applicable runtime or broker evidence exist. Tasks can be developed in parallel in isolated branches; integration/promotion remains gated.

### A. Close the core M21.1 / M21.1extra loop

- [ ] **A01 · M21.1 scoring reality check.** Verify real scanner signals reach the M19 enrichment/scorer and return valid ranking, component breakdown, direction, explanation, timestamp and freshness; no fabricated calibrated probabilities, no execution grant from research scores. Automated real-fixture parity tests and source-aware error codes. **Status (28 Oct 2026, v3.9): PR_PENDING — TESTED_OFFLINE on draft PR #12 @ `47e9057`.**
- **v3.9 adds** an opt-in completed-bar staleness basis for the completed-candle scanner (WP6). Default behaviour is unchanged.
- **Re-review:** only the delta `8013044..47e9057`.
- **Provenance:** the risk-decision provenance finding was already resolved at `8013044`.
- **Remaining evidence wiring:** WP5 in the finish-line doc.

v3.8: TESTED_OFFLINE @ `8013044`. v3.7 detail:
- **Contract:** pure `bot/scoring_truth`:
  - DST-correct session and bar-close semantics;
  - MISSING / INVALID / UNVERIFIED / STALE / INCOMPLETE / VALID per symbol × timeframe;
  - conservative corporate actions;
  - Risk Authority verified against M14's persisted decisions through a read-only store.
- **Fail-closed:** unknown inputs are removed, so M19 blocks.
- **Scanner as-of record:** metadata only, via a C5b transition.
- **Ranking:** preserved as `research_rank_score`.
- **Pending:**
  - exact-head re-review and the operator's merge decision;
  - real evidence sources (verified calendar, M14 decisions for scanner candidates, corporate-action data);
  - the fail-closed consequence for consumers.

Previous: NOT_STARTED (27 Oct 2026).
- [ ] **A02 · M21.1 candidate decision audit.** Persist every candidate stage, M19 gates/reasons, paper risk decision and outcomes with stable join IDs; distinguish rejected, watch, eligible, blocked and skipped; instrument effective data coverage. **Status (28 Oct 2026, v3.8): PR_PENDING — offline implementation TESTED_OFFLINE on draft PR #14 @ `71bccd6` (stacked on #12).**
- **Contents:** ledger schema, stable ids, stages, joins, flag-gated writer (off), legacy replay.
- **Not done** (each needs approval): no migration deployed, no writer enabled, no runtime wiring.
- **Next:** an approved read-only replay on a copy of production data, then the wiring PR.

v3.7: WP2 waited for the A01 review. v3.6: Stage taxonomy, lane-A candidate ids, counterfactual labels and engine decision records are on main (`80b4343`); production persistence of scanner → M19 → risk → outcome join ids is the next work package (§5). History: Stage taxonomy + read-only `candidate_snapshots` adapter + labels for every stage in PR #6 @ 3398de4 (offline). Production persistence of M19/Risk join IDs not started; real export needs approval.
- [x] **A03 · M21.1extra C5a/C5b controls.** October protected-file/test-isolation work merged. Preserve exact content-transition pins, no generic guard weakening. **Status (27 Oct 2026): CLOSED.** Merged earlier (main 015d2ce).
- [x] **A04 · M21.1extra D0, D1, supervised D2 foundation.** Already shipped historically; do not confuse supervised proof with unattended activity. **Status (27 Oct 2026): CLOSED.** Historical delivery.
- [x] **A05 · M21.1extra D3 WS1 code delivery.** Read-only authenticated performance metrics, honest N/A, fees and drawdown merged; runtime proof still in A06. **Status (27 Oct 2026): CODE_MERGED.** main 015d2ce; runtime proof is A06.
- [ ] **A06 · D3 dashboard operational acceptance.** Separately approved restart/deploy, authenticated GET-only dashboard/mobile inspection, genuine-vs-simulated and pending-fee N/A displays, no write/broker route, service/health unchanged. **Status (27 Oct 2026): RUNTIME_UNVERIFIED / needs approval.** Dashboard restart not approved.
- [x] **A07 · WS3 signal-alert code.** Signals-only Telegram outbox/cooldown/expiry/retry implementation merged; activation remains A08. **Status (27 Oct 2026): CODE_MERGED.** main 015d2ce; activation is A08.
- [ ] **A08 · WS3 operational signal alerting.** Decide separate credentials and bot .env notifier overlap to prevent duplicates; dry-run/controlled send and scheduled source only after operator permission. Confirm BUY versus SHORT, stale signals, dedup, restart safety, security and observability. **Status (27 Oct 2026): BLOCKED (approval).** Credentials/duplicate-notifier decision + send permission.
- [ ] **A09 · WS2 controlled code integration.** Exact-head independent review of draft PR #1 (5d6654d), safeguarded fast-forward/merge only upon operator approval; no broker actions from code merge. **Status (28 Oct 2026, v3.8): PR_PENDING — TESTED_OFFLINE @ `0f3f9e6`, with market-clock hardening R1–R3 (trusted Alpaca provider only, absolute 30 s ceiling, valid future session close required).** Merge needs separate approval.

v3.7:
- `8242a12` is a normal merge of `main` `80b4343`.
- `46c87ab`: the risk gate now verifies a fresh, typed D1 market decision on every call, so it cannot be bypassed by direct invocation, a closed market, a holiday, or a stale or unavailable clock.
- The exact-head audit was posted with LOW residuals R1–R3.
- The merge needs the operator's separate approval.
- Interaction: WS2 selects nothing once A01 merges, until evidence or research-rank selection is wired.

v3.6: Draft PR #1 @ 5d6654d: the three findings were reported corrected by an independent focused re-review (26 Oct). It is now 92 commits behind main; a trial merge with main is clean. It needs a normal merge-from-main refresh, the WS2 suite re-run on the refreshed head, an exact-head review, then the operator's separate merge decision. History: Draft PR #1 @ 5d6654d awaiting independent re-review + operator merge decision.
- [ ] **A10 · WS2 IBKR adapter acceptance.** On separately approved PAPER gateway only: verify account/port isolation, bracket orderRef across legs, permIds, completed/open order visibility across client IDs, real market data type/timestamps, broker errors, true paper fills/fees, and manual foreign-order coexistence. No real-money order. **Status (27 Oct 2026): BLOCKED (approval).** Needs separately approved IBKR PAPER gateway session.
- [ ] **A11 · WS2 crash/exit reconciliation acceptance.** Prove pending-submit and pending-flatten uncertainty survive disconnect/restart, no duplicate entry/close, late take-profit/stop and flatten fill, partial fills, fee delays, expired broker history, manual intervention on NEEDS_REVIEW, broker-owned-only cancellations and safe kill-switch. **Status (27 Oct 2026): PR_PENDING (offline) / BLOCKED (runtime).** Offline crash/reconciliation tests in PR #1; runtime acceptance needs paper gateway approval.
- [ ] **A12 · WS2 scheduler/market clock and operational gating.** Isolated test → authorised paper shadow/read-only probe → explicit permission for unattended PAPER schedule. Multiple sessions, holidays, half-days, DST, market halts, rate limits, clock skew; D0 expiry and account equity/source freshness. Bot remains off absent approval. **Status (27 Oct 2026): BLOCKED (approval).**
- [ ] **A13 · True paper outcome capture.** Exact entry/exit quantity and VWAP, commissions final versus unknown, P&L net of costs, R-multiple, stop/target/time-stop taxonomy, drawdown, account/strategy attribution, provenance, trade counts and real vs simulator flags. Reconcile unmatched events to NEEDS_REVIEW, no invented results. **Status (27 Oct 2026): PR_PENDING (partial).** Outcome capture code in PR #1; true paper outcomes need A10.
- [ ] **A14 · Production monitoring for paper research.** Reliable services, restart recovery, connectivity health, stale data detection, backups/restores, structured logs, alert escalation, capacity monitoring and durable journaling, tested without live-capital exposure. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **A15 · Four original starter templates.** Implement/verify, independently, (1) trend-following multi-TF confluence; (2) mean-reversion RSI/Bollinger; (3) breakout/momentum volume/ATR; (4) pullback-in-trend. Validate exits and adjustable parameters, and make template identity explicit in outcomes. No unreviewed switch to paper/live. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** Lane D on main `80b4343`. History: Lane D @ 33a05ad: the four templates unchanged in behaviour; each now also reports its setup and the filter that rejected it. Real-data validation and multi-TF confluence on real bars (M17 scanner_replica) remain.
- [ ] **A16 · Original dashboard strategy-controls scope.** Selected template, RSI/MACD/EMA/Bollinger/ATR/volume/confluence/stop/target/trailing parameters, validation, versioning, audit, rollback, least privilege. No unrestricted free-form user code or silent live parameter changes. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **A17 · M17/M16 real-bar parity debt.** Confirm adequate real 15m/1h/4h/1d coverage and proper corporate actions, reproducible scanner_replica parity on actual historical candidate rows (not only synthetic). Preserve missing-data fail-closed semantics. Consolidate/retire legacy backtest engines only after proved equivalence. **Status (27 Oct 2026, v3.6): CODE_MERGED (synthetic parity) / BLOCKED (real bars).** On main `80b4343`. History: PR #6: 6,122/6,122 exact counterfactual-vs-M17 parity on unambiguous synthetic paths. Finding: M17 books a stop when a bar gaps above the target then touches the stop (pessimistic tie rule) — documented, not changed. Real-bar parity needs an approved M16 export.

### B. Evidence + integrity + more advanced AI, developed in parallel (M21.2 / M21.3 / M21.4–M21.7)

- [ ] **B01 · Historical data inventory.** Inspect *available* M16/M18 datasets, timestamps, frequency, gaps, market sessions, corporate actions and as-of universe membership; no private VPS export or paid vendor access without approval. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (tools) · BLOCKED (real inventory needs approval).** On main `80b4343`. History: v3.4: PR #8 @ 77139cc also rejects non-finite / non-numeric volume. PR #8 @ e813b49: quality flags now drive grades and research admissibility (warning vs blocking bits; unknown bits block); retrospective full-sample audit separated from a causal split flag usable point-in-time (causal recall 87.5 %, precision 98.2 % vs retrospective 96.9 % / 98.4 %). VPS read-only runbook needs operator approval.
- [ ] **B02 · Data lake readiness.** Evaluate licensing-compatible survivorship-free/delisted constituent data and point-in-time fundamentals; Linux-compatible ingestion, quality/freshness audits and immutable snapshot hashes; propose vendors, do not purchase. **Status (27 Oct 2026, v3.6): CODE_MERGED (memo + manifests) · decision pending.** On main `80b4343`; nothing purchased. History: PR #8: vendor/licensing memo with sources verified 27 Oct (Norgate Platinum/Diamond delisted + constituents, Windows updater, personal-use licence; Massive ex-Polygon $29–$199/mo personal tiers keep delisted tickers; Sharadar publication-approval restriction; Alpaca Algo Trader Plus $99/mo). Nothing purchased.
- [ ] **B03 · Counterfactual evidence v0.** Label accepted, rejected, gated, near-miss, historical anchors and shadow candidates for hypothetical path outcomes. Flag unobservable stop-vs-target order, executable-fill unknowns, incomplete prices, costs, overlapping windows and clusters. Never label EOD ambiguity as certain intraday fill or treat counterfactual as realised P&L. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: v3.4: PR #6 @ ed62fdb: NO_POSITION candidates are `unfillable`, never executed, no daily capacity. PR #6 @ 969cec7: release maps fail closed for any accepted candidate without a release entry (unfillable = explicit NO_POSITION); real data must carry explicit bar opens; new `simulated_entry` stage so backtest fills are never 'executed'. 27 tests; repro mutants killed.
- [ ] **B04 · Gate-economics/regret ledger.** Report observational avoided/foregone hypothetical R by each filter, holdout, threshold and Risk Authority reason. Do not infer causal benefit absent appropriate comparison. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: PR #6: observational gate-economics ledger with date-cluster bootstrap CIs and non-causal caveat.
- [ ] **B05 · Immutable experiment/trial registry.** All tried/failed/discarded hypotheses, family IDs, code/data/config/model hashes, splits, seeds, economic assumptions, CI report cards, contamination/research access ledger and verified references. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE.** On main `80b4343`. History: PR #5 @ da6052c: trials carry outcome artifacts and real code identities; the report card verifies the candidate's gross series and every family-matrix column against them (substituted outcomes with genuine ids fail). 43 tests; repro mutants killed.
- [ ] **B06 · Sealed holdout and preregistration.** Separately permissioned holdout and access log; Git signed identity and independent/server-side timestamp attestation, frozen specifications and one-use final tests. Never rely on a mutable Git commit date or CODEOWNERS alone as security. **Status (27 Oct 2026, v3.6): CODE_MERGED (local policy model) / NOT_STARTED (operational independent authority).** On main `80b4343`. History: PR #5 @ da6052c: attestations re-verified cryptographically at consumption; holdout release needs a valid, unexpired, separately signed approval and an attestation before the forward window and the seal. The verifier is labelled a LOCAL HMAC POLICY MODEL (independent=False) everywhere it is used; an operational asymmetric signer is still required.
- [ ] **B07 · Anti-overfit testing suite.** Purging/embargo and genuinely forward splits, CPCV/CSCV PBO, deflated Sharpe with effective trial multiplicity sensitivity, White Reality Check/SPA as appropriate, reality checks for label overlap, walk-forward regime/market splits and transaction-cost stress. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE.** On main `80b4343`. History: PR #5: purged k-fold, purged walk-forward, CPCV, leakage verifier, DSR/PSR, PBO (CSCV), White RC, Hansen SPA, Holm/BH/BY, cost-stress report card.
- [ ] **B08 · Independent statistical canaries.** Large pre-registered known-null batteries, planted synthetic positives, negative controls (shuffled labels/placebos), proper uncertainty intervals on false pass/detection rates. Five nulls alone cannot estimate a ≤5% false-positive rate reliably. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE.** On main `80b4343`; no calibrated integrated canary yet. History: v3.5: PR #5 @ 3b5e8f9: null-only batteries are NULL_ONLY; PASS needs a ledger-verified battery for the exact pipeline (family + code) with re-derived positive-control power. PR #5: one-use battery plans, CP lower-bound power rule. Lane C (PR #7) adds a CALIBRATED lab canary: 0/59 null false claims (UB 5.0 %), power 20/20 at x1.5.
- [ ] **B09 · Strategy Genome v0.** Declarative, constrained JSON/schema of existing templates, typed entry/exit/risk/timeframe, semantic IDs, lineage/variant accounting, fail-closed validator, reproducible M17 parity. Offline research only; no direct broker path. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: Lane D @ 33a05ad: genome v0 unchanged; M17 trade-for-trade parity kept (legacy close labels checked equal to the new bar-close fields).
- [ ] **B10 · Multi-symbol and portfolio-aware event backtesting.** Time-aligned data, borrow/short restrictions, capital/slot/exposure and correlations, spreads/slippage/gaps, survivor-free membership, next-open/intrabar ambiguity policies, deterministic fixtures, throughput benchmarking and parity with M17 where valid. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic) · corporate actions and liquidity NOT modelled.** On main `80b4343`. History: v3.4: no corporate-action/split adjustment yet; the integration fails closed on positions crossing suspected unadjusted splits (PR #11 @ 5cc3dce). Lane D @ 33a05ad: trades now carry decision time, fill-bar open (entry/exit fill events), bar-close labels and explicit unobservable intrabar windows; one decision record per evaluated candidate (entered / rejected by rule / position open / re-entry / universe / capacity / risk / unfillable / no next bar). Borrow/short, sector caps and real-data throughput remain.
- [ ] **B11 · M21.2 parameter optimisation.** RSI, MACD, EMA/MA, Bollinger, ATR, volume, confluence, stops, targets and trailing variants. Search parameter *surfaces and plateaus*, not best isolated backtest; count every trial and compare no-trade/simple baselines. **Status (27 Oct 2026, v3.6): PARTIAL (foundations CODE_MERGED).** On main `80b4343`. History: Lane D @ 33a05ad: search trials record real code identity, the market seed, outcome artifacts and accept the as-of universe mask. Full M21.2 surfaces on real data remain.
- [ ] **B12 · M21.3 out-of-sample walk-forward optimiser.** Train/validation/testing fully separated; embargo overlapping labels; sealed final sample; cost-adjusted net expectancy with confidence intervals, drawdown, expected shortfall/CVaR, turnover, capacity and stability across market regimes. Win rate/avg-win-to-loss remain recorded but are not sole approval metrics. **Status (27 Oct 2026, v3.6): PARTIAL (infrastructure CODE_MERGED).** On main `80b4343`. History: v3.5: PR #5: PBO / SPA / Reality Check only over the complete comparable search family. Purged walk-forward + report card in PR #5; optimiser not started.
- [ ] **B13 · M21.3 Promotion & Approval Model.** Preserve GREEN safer-only/AMBER material/RED increased risk/new champion distinctions, shadow-first, change budget, explicit reviewer/operator authorization for behaviour changes and fail-safe deactivation/rollback. Conservative changes must be proven genuinely safer (e.g. a tighter stop can increase realised losses through churn); never auto-promote risk. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **B14 · Advanced ML laboratory NOW, not deferred.** Baseline logistic/linear/random-forest/boosted trees versus XGBoost/LightGBM/CatBoost and appropriate temporal/representation challengers; deterministic offline comparison, feature-as-of audit, proper train/calibration/test, model cards and reproducible experiments. No claimed real alpha from synthetic data. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`; linear and regime benchmarks not matched. History: v3.5: PR #7 @ f3801bf: fold threshold / conformal use only resolved outcomes; unobserved barrier paths dropped; evidence regenerated. PR #7 @ 8f64a07: dataset digest covers every field (schema v2); trials carry real code identity and outcome artifacts; evidence regenerated with the same honest findings (null and interaction matched; linear and regime NOT matched, not re-tuned).
- [ ] **B15 · Ensembles / model diversity.** Independently trained feature families, out-of-fold stacking, calibrated probabilities, robustness against correlated errors, ablations, calibration quality and honest abstention thresholds. Demonstrate incremental OOS gains over simpler models before promotion. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: PR #7: mean/stacked/bagged ensembles tested; dispersion analysed.
- [ ] **B16 · M21.4 meta-labeling.** Take/skip/downsize (no authority to widen risk limits) from point-in-time candidate/outcome labels, realistic costs and selection-bias correction; probability calibration, uncertainty interval, decision-curve economics and independent OOS testing. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: PR #7: meta-labeling on primary-rule candidates vs take-all; integration branch meta-labels lane D genome candidates.
- [ ] **B17 · M21.5 regime-aware thresholds.** Bull/bear/range, volatility/breadth/dispersion/liquidity states; causal as-of features and realistic detection lag. Start as shadow conditioning and risk throttles, not brittle instant regime switches. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: PR #7: regime model + regime-blind twins; on the regime benchmark the context gain over logistic_noctx (raw Holm p 0.018) was withheld by the skill gate (neither beat the prior on probabilities).
- [ ] **B18 · M21.6 market context.** VIX/volatility term structure, breadth, sector rotation, index trend, macro and news/event calendars, earnings/central-bank proximity, delayed-revision control, licensing, source availability and event-time correctness. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **B19 · M21.7 learned regime weights.** Model conditional contribution of indicators/context by regime, bounded/drift-aware adaptation, out-of-fold training and comparison with global weights; no automatic production scoring/risk override. **Status (27 Oct 2026, v3.6): PARTIAL (research CODE_MERGED).** On main `80b4343`. History: PR #7 regime-conditioned models; learned regime weights on real data not started.
- [ ] **B20 · Self-improvement evaluation.** Measure model calibration, net return per R, abstention quality, false discovery, drift detection, research hypotheses discarded, agent cost per validated discovery and regression against the previous approved champion. Track outcome-lag and learning-data staleness. **Status (27 Oct 2026, v3.6): PARTIAL (CODE_MERGED canaries; no integrated canary).** On main `80b4343`. History: Canary batteries (PR #5) + calibrated lab canary and model cards (PR #7).
- [ ] **B21 · Autonomous research knowledge library.** Source-verified citations, point-in-time paper knowledge cutoff, falsifiable hypotheses/mechanisms, strategy and trial lineage, rejection graveyard, semantic dedupe, re-test triggers and owner/audit accountability. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE.** On main `80b4343`. History: PR #9 @ 0845a69: operator verification needs the operator's signature over the source content (lane B local model); identity strings, self-declared status, forged ledger entries and replayed signatures are refused; status re-verified on read.
- [ ] **B22 · Research agents mock → gated proof.** Design Director/Scout/Hypothesist/Engineer/Skeptic/Librarian/Reporter and deterministic Auditor, allowed tools and writing areas; synthetic end-to-end mock first. Any paid/scheduled agent action, API key, GitHub workflow with broader permissions or autonomous PR creation requires separate approval. Agents never access broker credentials or independent evaluation vaults. **Status (27 Oct 2026, v3.6): CODE_MERGED (mock only) · real agents need approval + OS sandbox.** On main `80b4343`. History: PR #9 @ 0845a69: unchanged fail-closed role gate; mock trials now carry real code identity. Real agents need approval + an OS sandbox (a Python allow-list is not a sandbox).
- [ ] **B23 · Continuous validation and decay monitoring.** Sequential change alarms, uncertainty/cost drift, overfit safeguards, predicted-versus-observed calibration and automated proposals to abstain/demote; quantify false-alarm/detection trade-off before auto-safe changes. **Status (27 Oct 2026, v3.6): NOT_STARTED (building block CODE_MERGED: causal ACI).** On main `80b4343`. History: Adaptive conformal in PR #7 WIP.
- [ ] **B24 · Cross-market / causal / alternative-data challenge.** Falsify US-discovered effects on UK/EU/HK when genuine equivalent data exist; evaluate point-in-time SEC/earnings/insider/short/option data, causal assumptions and event-time leakage. Do not claim transfer where coverage is absent. **Status (27 Oct 2026): NOT_STARTED.**

### C. M22.1–M22.6 controlled real-money readiness: NO LIVE AUTHORIZATION

The June document groups M22.1–M22.6 without fixing a complete subnumber-to-deliverable mapping. **Retain M22.1–M22.6 exactly; reconcile precise sub-numbering with repo before implementation.** The following are required acceptance tasks under the M22 family, not an invented renumbering.

- [ ] **C01 · M14 scanner-path Risk Authority parity — HARD BLOCKER.** Every scanner-driven submit path must invoke or meet/exceed the full M14 gate set: broker/global capital, broker daily loss, combined exposure, drawdown throttle, per-symbol/sector concentration, quote/spread and data freshness, kill switches and unified policy-invalid semantics; AST and runtime tests. **Status (28 Oct 2026, v3.8): DESIGN ONLY — final decisions D-1..D-6 (rev 3) on draft PR #13 @ `6a8a690`; implementation NOT_STARTED — HARD BLOCKER for M22.**
- Adds the governor-store schema, completed-candle design §11 (T-N) and reduce-only flatten §12 (closed-market semantics).
- Remaining: approvals and VPS verification only.

v3.7:
- **The design (`docs/M22_C01_risk_authority_parity_design.md`) contains:**
  - a 25-row gate parity matrix;
  - CRITICAL findings: no M14 decision on the scanner path; unverified market state (M14 `market_open` defaults to True); legacy fail-open on DB read errors;
  - the fail-closed table, IP-1..IP-10 and tests T-A..T-M;
  - a phased migration (`legacy AND M14`, never OR);
  - revision-2 policies P-1..P-6.
- **Still open:** the flatten closed-market rule; whether to keep the local JSONL paper broker; approval of the closed-bar scanner change; VPS verification of M14 ingestion.

Previous: NOT_STARTED (27 Oct 2026).
- [ ] **C02 · Paper/live separation.** Broker accounts, ports, keys, data, signals, execution permissions, model promotion and kill switches separated; eToro live cannot be enabled by a research environment flag or paper-only workflow. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **C03 · Production hardening.** Supervisors/auto-recovery, IBKR reconnection, stale price and positions rejection, external monitoring, safe deploy/rollback, test-in-isolation discipline, secrets/file permission audit, rotation/backup/restore drills, no unexpected auto-sync or restart on push. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **C04 · Recovery under partial broker truth.** UNKNOWN orders/fills, double-submit prevention, account-wide manual activity separation, trade ownership, cancellations, OCO/bracket orphan detection, fee reconciliation and break-glass operator flow, fail closed. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **C05 · Funded eToro broker/API feasibility proof.** Verify current eToro authorised access, order permissions, sandbox-vs-live differences, account residency, regulatory/platform terms and public copy-track-record support. No promise of automated eToro execution until independently proven. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **C06 · Per-order human approval.** Order preview, sized maximum loss, spread/quote/position age, risk reasons, independent TOTP/step-up, atomic one-time authorisation, durable audit, revoked/expired approvals and operator cancellation. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **C07 · Tiny live order permission gate.** Evidence of stable out-of-sample positive **net** expectancy (not just win rate), execution reliability, stress capacity, complete M14 parity, verified broker flatness/exposure and explicit operator permission for each first small funded order. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **C08 · Post-trade funded reconciliation.** Actual broker confirmations, fees, taxes/FX where applicable, manual fills, net P&L, broker-corrected history, audit replay, displayed track record distinct from hypothetical/paper performance. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **C09 · Controlled promotion / capital progression.** Explicit approval before every increase in size, autonomy, broker allocation or acceptable risk. Predefined daily loss, overall drawdown, exposures, currency and concentration caps; capital never scales just because of age. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **C10 · Public track record and copying objective.** Check platform/regulatory eligibility, verifiable statements, strategy disclosure, copier-specific risks, operational transparency, drawdown limitations and honest profitability claims. No guaranteed income. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **C11 · Persistent broker paper laboratory.** IBKR paper continues researching and challenging even when eToro live is approved; separate ledgers, scoring and risk policies, never assume research-edge transfers to execution fills. **Status (27 Oct 2026): NOT_STARTED.**

### D. M23 advanced professional operating system — keep every original advanced task

- [ ] **D01 · M23.1 champion/challenger.** Multi-family/spec breeding, meaningful diversity not cloned factor variants, independent OOS/forward challenge, calibrated and cost-aware report cards, shadow champion pairing, explicit promotion and rollback. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **D02 · M23.2 drift detection.** Feature/concept/label/cost regime shifts, sequential alerts, retraining proposals, anti-churn guards, degraded-data abstention and safe rollback/demotion. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **D03 · M23.3 portfolio correlation/exposure.** Holdings/strategies/brokers correlations, sector/country/currency/beta/volatility/crowding, tail stress, capital and liquidity budgets, kill-switch and daily loss across all open orders and actual exposures. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **D04 · M23.4 advanced macro/news intelligence.** Reliable event-time news, revision-aware macro/earnings/central-bank calendars, source scoring, natural language event extraction with model cutoff/leakage controls, provider-failure behavior and audited impact on outcomes. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **D05 · M23.5 multi-strategy management.** Several reviewed strategies coexist under common risk authority, strategy-specific attribution/exposure/fees, champion selection, deactivation when deteriorating, dashboard side-by-side metrics and no unapproved live strategy switching. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **D06 · Autonomous R&D organisation (within M23, parallel foundations in B).** Research agents propose new specs, experiments, feature ideas, tests and PRs on isolated branches; knowledge accumulation, bounded research-yield allocation, independent deterministic evaluator, canary-based safeguards, approval-only merge/promotion, and cost controls. **Status (27 Oct 2026, v3.6): PARTIAL (offline foundations CODE_MERGED).** On main `80b4343`; no autonomous or scheduled agents. History: No autonomous or scheduled agent activity.
- [ ] **D07 · Advanced uncertainty and decision economics.** Confidence calibration, conformal/ensemble uncertainty where empirical coverage holds, expected net-R lower bounds, explicit no-trade option, strategy allocation based on evidence not raw scores, portfolio-aware opportunity costs. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: v3.5: fold-level split conformal now calibrates only on outcomes resolved before the fold. PR #7: ACI is now causal: a label enters calibration only after it resolved strictly before the decision using it; static conformal calibrates only on warm-up labels resolved before the first evaluated decision.
- [ ] **D08 · Research versus operating twin.** Use a read-only/shadow digital twin to compare forecast fills, broker paper fills and real brokerage fills, explain discrepancies, report transaction cost analysis and capacity limits. No unsafe direct self-modifying order code. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **D09 · Fully auditable mature operation.** Recovery drills, live/paper separation, signed model/spec manifests, reproducible source/data hashes, credible health alerts, daily independent broker reconciliation, operator runbooks and third-party reproducibility of performance where allowed. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **D10 · Project-end acceptance.** All original M21/M22/M23 functions either accepted with evidence or transparently marked intentionally not deployed; capability dashboard, latest experiment/model registry, live/paper order trail, rigorous OOS risk metrics and complete incident/rollback proof. “Project finished” describes validated capabilities; permanent learning/operations continue indefinitely. **Status (27 Oct 2026): NOT_STARTED.**

### E. Audit, data, security and historical carry-forward — do not lose this work

- [ ] **E01 · Older audit-P1-broker-permId-fallback.** Confirm intended legacy behavior versus bounded wait; no silent fallback; ensure WS2's new adapter does not wrongly mark the older path repaired. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **E02 · audit-P1-portfolio-ctx-engine-bypass.** Check local-intent semantics and realistic populated contexts; distinct from the M14 scanner-path full-gate M22 blocker C01. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **E03 · audit-P2-batch: 9 items.** Backtest engine duplication; historical scanner/backtest overlap; open-intents symbol/direction discrimination; eToro SignalOnlyBroker reason parity; M13.5 eToro event audit completeness; M14 decision/reason spelling; M14 gate default tightness; M15 manual-reset/export audit constraints; M14 sector-map config precedence. Audit against code and close only with evidence. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **E04 · audit-P3-batch: 6 deferred items.** Preserve as an open batch; individually inventory exact six records from original audit before planning their fixes (not specified in the June canonical document). **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **E05 · Real M17 intraday E2E and provider quality.** Incomplete 15m/1h/4h coverage and mismatch risk, cross-provider latency, rate limiting, listing history, corporate actions and missing-data semantics; linked to A17/B01. **Status (27 Oct 2026, v3.6): PARTIAL (tools CODE_MERGED) · real-data run needs approval.** On main `80b4343`. History: v3.5: integration market context excludes quarantined symbols (PR #11 @ a645a91); resample ORs flags (PR #4). v3.4: integration flags and fails closed on split-crossing economics; adjusted point-in-time bars remain required. PR #8 audit + point-in-time admissibility ready to run read-only on the VPS store (approval required). The integration branch now uses only point-in-time quality knowledge.
- [ ] **E06 · M15 deferred UX/security items.** Login latency investigation; persistent rate limiter only if warranted; multi-user/read-only roles **deferred indefinitely unless a concrete need arises**. Do not revive cosmetic dashboard work as a current priority. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **E07 · Secret and backup permissions.** Prior historical report flagged an .env backup as overly permissive; check metadata only with operator permission and remediate via separately approved controlled action; do not read or expose credentials. **Status (27 Oct 2026): BLOCKED (approval).**
- [ ] **E08 · Canonical documentation reconciliation.** Update GitHub ROADMAP.md, MILESTONE_STATUS.md, NEXT_WORK_REGISTER and status snapshot to match **actual code/activation state**, preserving original milestone numbers, exact commit references, and deliberately deferred audit items. No accidental “D3 not started” after code merge. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **E09 · Test and deployment governance.** Isolated no-secrets offline CI, protected paths, reviewer checks on exact head, deterministic tests and mutations, PR-only updates; no unreviewed auto-sync, service restarts or production DB writes. **Status (27 Oct 2026, v3.6): PARTIAL (inactive CI example CODE_MERGED; activation needs approval; clean runner unverified).** On main `80b4343`. History: v3.5: the inactive CI example lists all eight research suites (PR #8 @ 22d07d5); still INACTIVE. PR #8 adds an INACTIVE least-privilege research CI example (docs/research/ci, not in .github/workflows). Research suites also run under production-pinned numpy 1.26.4 / Python 3.12 (see v3.2 §3a).

## 5. Work order — parallelism without deadline promises

> **v3.9:** M21 sequencing and acceptance are now defined in `docs/handoffs/M21_Finish_Line_2026-10-28.md`:
> - §5: work packages WP1–WP19 and operator gates OP1–OP5;
> - §6: the authoritative checklist.
>
> The lanes and principles below still apply.

- **Immediate engineering lane 1:** finish review/paper integration preparation A01–A17 where OFFLINE possible; WS2 draft PR #1 kept isolated until operator approves merge, real paper gateway and runtime separately.
- **Immediate research lane 2:** implement B03–B08 evidence/integrity **and simultaneously** B14–B19 sophisticated AI harness/ensembles/regime work on synthetic and already authorised public or repository data. Do not wait for real fills before coding/test harnesses.
- **Immediate research lane 3:** implement B09/B10 strategy genome and parity, E agent mock/design, vendor/license memo B01/B02, and E08 documentation alignment on separate branches. Where data lacks coverage, report N/A and proceed with unblocked technical work.
- **Later once validated:** B11–B13 optimiser/governance; M21.4–M21.7 and M23 capabilities; C01–C11 operational/funded readiness subject to explicit evidence gates. Tasks need not be coded in a strictly serial sequence when files and risk surfaces are independent.
- Claude Chat/Code may use separate branches and draft PRs, complete as many safe offline tasks as possible, and independently prove tests; ChatGPT/Codex reviews exact heads; only the operator authorises merge and operational effects.
- An architecture/model is not “closed” just because it runs on synthetics, and a backtest win is not proof of funded positive expected value.

## 6. Rules for all future complete handoffs

1. Begin with objective + architecture + **both brokers** + evidence AND AI priorities; keep original M1–M23 milestone numbering unchanged.
2. Include every task A01–E09 (even deferred) with CLOSED / CODE_MERGED / TESTED_OFFLINE / DATA_VERIFIED / RUNTIME_VERIFIED / TRADING_VERIFIED / PR_PENDING / PARTIAL / RUNTIME_UNVERIFIED / BLOCKED / NOT_STARTED / INTENTIONALLY_DEFERRED, exact SHA and proof (vocabulary in §3a-0). Add tasks if new material defects emerge; never silently remove tasks.
3. Identify the exact current GitHub main, open PR heads, services, operator kill switch, data provenance and gateway state **only if reverified**; otherwise label last-known and date. Distinguish paper simulated trades, actual IBKR paper trades and eToro funded trades.
4. State who is working on which non-overlapping branch, outstanding review findings, what may proceed without approval and the next explicit operator gate.
5. Summarise the last applied change and its tests without restarting old broad audits; maintain links to original June roadmap, Issue #2, this handoff, audit backlog, and relevant pull requests.
6. No time estimates, deadlines or invented percentages. Evidence-driven gates decide progression. **Exception (operator request, 28 Oct 2026):** the bottom-up M21 estimate in the finish-line doc is allowed. It is labelled as an estimate with stated assumptions, re-estimated at each phase gate, and never treated as a deadline or a reason to skip a gate. **No paid purchases, credentials, private data export, broker calls, live/paper orders, dashboard/Telegram enablement, VPS deployment or merges without the matching separate permission.**
7. **Hand off the FULL project**, not just the next sprint. Keep end-state functionality and unproved operational steps visible until accepted, even if current chat or engineering agent changes.

## 7. Links and current action

- **This full handoff:** GitHub Issue #3, and this file in its isolated documentation branch.
- **Original canonical plan:** user-supplied 29 June 2026 canonical roadmap (uploaded 30.06.2026).
- **Research discussion and proposal:** https://github.com/mikemitakidis/algotrading/issues/2
- **WS2 review:** https://github.com/mikemitakidis/algotrading/pull/1
- **Working research report:** https://github.com/mikemitakidis/algotrading/blob/research/m21-autonomous-research-engine/docs/research/M21_autonomous_research_engine_proposal.md
- **GitHub source:** ROADMAP.md; MILESTONE_STATUS.md; docs/NEXT_WORK_REGISTER.md.

**Claude's next authorised action (resume point, v3.11):** **STOPPED** after the final engineering acceptance packet (`docs/reviews/M21_Final_Engineering_Acceptance_2026-10-29.md`, posted on Issue #3).

Waiting for the grouped approvals in its §20:
- G-review;
- C5b (#12, #16);
- G3a merges, in the §13 order and trees, merge commits only;
- the dashboard shape;
- G2/OP1, with your IBKR statement exports;
- G3b OP2/OP3;
- G4;
- the Vercel decision.

After the merges: the v3 canary once, then E1–E7.

Earlier resume point (v3.9): **STOPPED for review** of the M21 finish line (`docs/handoffs/M21_Finish_Line_2026-10-28.md`) and of the PR deltas:
- #12 A01 (`47e9057`; delta `8013044..47e9057`);
- #1 WS2 (`0f3f9e6`; delta `46c87ab..0f3f9e6`);
- #13 C01 design (`6a8a690`);
- #14 A02, stacked on #12 (`71bccd6`).

**After the operator's decisions G1–G4** (finish-line §8), follow finish-line §5 and §6:
1. Merges #12 → #1 → #14 (retarget to `main`) → #13.
2. WP5 / WP6 / WP7 / WP10 in lane L, with WP12 / WP11 in lane R.
3. The OP1 read-only VPS session.
4. The paper-loop activation gate (OP2 / OP3).

C01 phases other than §11 (WP6) remain M22 and need a separate decision.

v3.8 resume point:
- review of #12 @ `8013044`, #1, #13 and #14;
- then merges, the A01 fail-closed consequence, the A02 replay, and C01 phases as approved.

Real-data, VPS, IBKR paper, Telegram, CI activation and any merge need separate approval. Do **not** merge or alter production.

**Next operator decisions, only when needed:** WS2 code merge permission; IBKR PAPER gateway/order test permission; WS1 dashboard restart; WS3 Telegram send/activation; actual private data export; vendor spend; paid agent or scheduled activity; any eToro funded order. None is approved by this handoff.
