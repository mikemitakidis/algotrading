# Algo Trading — FULL Canonical Project Handoff & Completion Register
**Snapshot:** 26 October 2026, **status refresh 8 November 2026 (v3.24); previous v3.23/v3.22/v3.21/v3.20/v3.19/v3.18/v3.17/v3.16 (7 Nov), v3.15/v3.14/v3.13 (6 Nov) and v3.12 (29 Oct)** · **Version:** 3.24 (8 Nov 2026: independently reviewed Gate A-2 + IBKR PAPER draft plans and main code; flagged targeted runbook and PnL correctness gaps; no deployment approval) · prior v3.23 (7 Nov 2026: Gate A-1 formally CLOSED; operator post-merge VPS-0=0; prepare A-2 and IBKR PAPER separately) · prior v3.22 (7 Nov 2026: all 24 Gate A-1 GitHub merges independently checked by exact SHA/parents/trees, post-merge VPS-0 pending) · prior v3.21 (7 Nov 2026: four safe Gate A-1 PR merges, STOP at #15 GitHub mergeability, Option A requires fresh operator approval) · prior v3.20 (7 Nov 2026: real pre-merge VPS-0 operator-supplied PASS, 24 merges pending fresh GitHub preflight) · prior v3.19 (7 Nov 2026: final operator Gate A-1 24-merge authorisation recorded on Issue #3; execution not yet observed) · prior v3.18 (7 Nov 2026: Gate A-1 final launcher exit-status correction independently verified; ready for separate operator merge authorisation, not yet executed) · prior v3.17 (7 Nov 2026: independently checked PR #34 revision 5 exact scripts/hash and 24 PR heads; launcher exit-status correction pending, no merge approval) · prior v3.16 (7 Nov 2026: focused independent review of Gate A-1 rev4 VPS-0 exit-code fix, no execution authorisation; Vercel preserved unchanged) · prior v3.15 (6 Nov 2026, 19:35 UTC: operator approves C5b, #35 inclusion, and #33 reviewed-head strategy; Vercel left connected and unchanged; no Gate A-1 merge authorisation) · prior v3.14 (6 Nov 2026, 18:53 UTC: VPS-0 revision 3 independently reviewed, 24 PR heads matched and five handoff corrections; no merge approval) · prior v3.13 (6 Nov 2026, 18:35 UTC: complete Gate A-0 recorded, Gate A-1 engineering review revision 2 under independent review, canonical register reconciled; no merge approval) · prior v3.12 (29 Oct 2026, evening: release preparation — 16 PR heads re-verified; E1–E7 as draft PRs #27–#33 on `m21/rc-base`; proposed `main` tree `1ed8c0f`; E6 `dashboard/app.py` wiring prepared, not applied; OP1 final; Vercel findings; Finish Line §7d; `main` unchanged; all 71 tasks kept) · 3.11 (29 Oct 2026: final engineering acceptance — 13 defects fixed incl. #17 forged-M14-id, #22/#23 activation race, #25 timezone lookahead; all 16 PRs proven to merge conflict-free onto `main` in order; full suite on the post-merge tree; C5b and dashboard requests; OP1.6 nine categories + OP1.7 statement reconciliation; G4 matrix; `docs/reviews/M21_Final_Engineering_Acceptance_2026-10-29.md`; `main` unchanged; all 71 tasks kept) · 3.10 (28 Oct 2026, late: corrections after the independent review of the accelerated delivery — #23 cross-store governance, #17 risk-evidence provenance, #24 canary experiment identity, plus §6/§7 audit fixes on #20/#24/#25; corrected heads, evidence and OP1.6 in `docs/reviews/M21_Review_Corrections_2026-10-28.md`; `main` unchanged; all 71 tasks kept) · 3.9 (28 Oct 2026: M21 finish line, requirements matrix, remaining-work plan, estimate, merge readiness and decisions in `docs/handoffs/M21_Finish_Line_2026-10-28.md`; A01 completed-bar staleness basis on PR #12 @ `47e9057`; `main` unchanged; all 71 tasks kept) · 3.8 (28 Oct 2026: PR #1 market-clock hardening R1–R3; C01 final design decisions D-1..D-6 (rev 3); A02 candidate ledger offline on draft PR #14; `main` unchanged at `80b4343`; scope unchanged from 3.0) · 3.7 (28 Oct 2026: WP1/WP3/WP4 delivered as draft PRs #12/#1/#13; checkpoint corrections) · previous 3.6 (27 Oct 2026: M21 research PRs #4–#11 merged to main `80b4343`) · **Repo:** mikemitakidis/algotrading · **Dashboard:** algotrading.marketwarrior.club

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

### 3a-A2-PAPER-REVIEW. v3.24 — independent review of new plans; offline blockers verified; targeted fixes pending (8 November 2026)

> **CURRENT RESUME POINT.** Gate A-1 remains CLOSED/PASS. Gate A-2 and IBKR PAPER operation are **NOT AUTHORISED**. Claude prepared two read-only review plans and seven new runbook scripts on docs PR #34, current draft head `ac3ef2b6cd020b953973dafff84977568d3ea93e`, without code deployment or broker connection. ChatGPT independently reviewed actual source at `main=963bb893c81c2482a08d78a11b1aafe98f542b51` and scripts/docs at PR #34. [Specific review findings on Issue #3](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6726669904).

**Claude's five automated IBKR PAPER blockers on current main (code verified):**
1. `BLK-1`: `snapshot._build_global_view` aggregates all four scopes (IBKR paper/live and eToro paper/live); when any source is unknown, combined capital, positions and P&L become unknown and M14 refuses even paper orders. No LIVE-data polling should be required just to trade in PAPER.
2. `BLK-2`: `tools/ingest_risk_state.py` IBKR P&L reader deliberately raises `NotImplementedError`. Paper daily loss can never become known through that CLI.
3. `BLK-3`: both ingestors are operator-run, no auto-refresh; exposure has 120-second and P&L 300-second age thresholds.
4. `BLK-4`: M14 family-auto flag fans `ibkr` to **both** `ibkr_paper` and `ibkr_live`; D0 broker-allocation policy already has *separate* time-boxed lanes. Must unify to preserve explicit live isolation.
5. `BLK-5`: paper loop and WP10 monitoring libraries do not yet have reviewed/installed unattended service/timer runners.

**Specific independent review changes required before Gate A-2.2 or paper activation:**
- **Containment at the moment of code switch:** `docs/runbooks/a2/a2_deploy.sh` runs a potentially long full test pass; just before checkout it rechecks kill switch/bot inactive/HEAD but not effective systemd guard, allow-file, boot link or `sync.sh`. Add full `a2_containment.sh "$FROM"` just before the git switch; mock negative tests where a guard or start allowance changes during tests.
- **Read-only diagnostics must signal DB errors:** `a2_precheck.sh` prints corrupt/`READ FAILED` required database lines without elevating to ATTENTION; `paper_p0_readonly.sh` deliberately exits 0 on `signals.db` open error and otherwise may still say `P0 INVENTORY COMPLETE` after queries error. Distinguish optional absence from required read failures, surface as STOP/attention and add targeted injected failures. The combined read-only launcher currently prints `vpsc_exit`, `precheck_exit`, `p0_exit` but finishes with status 0 despite a failing VPS-C; enforce meaningful outer failure/STOP.
- **P&L source semantics:** Official IBKR API documents IB Gateway `reqExecutions` limited to current-day executions, so it alone is not guaranteed a complete, trustworthy daily loss source after restarts, time resets or commission delays. IBKR also provides `reqPnL` with broker-account daily/realised/unrealised fields, but they follow configured PnL reset semantics. Prove account-specific, finite, correctly time-scoped daily realised-loss evidence with fees, correction/incomplete-source handling, a broker-verified no-execution day (legitimate known zero vs unknown), and reconciled account/journal statements. Missing data must block, not become synthetic zero. See https://www.interactivebrokers.com/docs/tws-api/doc/order-management/execution-details and https://www.interactivebrokers.com/docs/tws-api/doc/accounts/pnl.
- **Paper-vs-live risk aggregation:** M14 gates `_gate_global_capital`, `_gate_combined_exposure`, `_gate_global_open_positions`, `_gate_global_daily_loss`, **and** `_gate_concentration` depend on global aggregates. Separate paper-domain scope for every applicable gate, not just combined exposure; retain live account risk enforcement exactly as-is, unknown live still blocks live.
- **Dashboard H-1:** protect the five legacy direct-start paths before releasing kill switch or starting any paper scheduling, because they bypass systemd bot-start guard. Ensure denial tests; do not treat installing new PAPER timer as automatically safe.

**Priorities, approval boundaries, and evidence:** Recommend user separately authorise development **only** of BLK-1…5 and H-1 as draft PRs, with explicit protected-file review, offline/regression tests, original 72 task IDs preserved and no merge/runtime activation. Claude reports 135/0 modules on a mock VPS, 30 tests on old VPS-0, and targeted script tests; ChatGPT has **not rerun** them on the VPS. Correct the runbook issues and get sanitized read-only A-2.0/VPS-C/P0 results before approving A-2.2 deploy. One deployment of reviewed code while bot disabled and kill switch ON is distinct from later broker and paper activation. Alpaca PAPER keys regenerated by operator but VPS reconfiguration pending separately; dashboard password age unknown, fresh bcrypt rotation with Google Authenticator retained pending separately. LIVE IBKR holding stays untouched. **No VPS service change, credential edit, IBKR API connection, order or trading approved by this checkpoint.**

---

### 3a-GATE-A1-CLOSED. v3.23 — complete GitHub merge plus actual VPS-0 postcheck PASS (7 November 2026)

> **CURRENT RESUME POINT. Gate A-1 is CLOSED / PASS.** All 24 previously approved GitHub merges are done and verified; the actual VPS was checked by the operator **before and after** the sequence and remained in Gate A-0 containment. This approval **does not** authorise Gate A-2 deployment, IBKR paper or any live trading.

- **Exact GitHub final state:** `main=963bb893c81c2482a08d78a11b1aafe98f542b51`, tree `f04d1d65dd830894156425f5a870a50591a3a805`. Independently re-read after the post-VPS check; matches reviewed full tree. The full set of 24 merge commits individually passed ChatGPT's checks for full SHA, first-parent chain, second-parent reviewed head and exact expected tree. Source: [Claude's merge table](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712104253) and [independent check](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712165676). PR #34 remains open/draft, held for post-A-1 documentation reconciliation; no GitHub merge of #34 was implied.
- **Actual VPS-0 PRE-MERGE:** user ran the pinned launcher as root at `2026-11-07T19:14:13Z`; `vps0_exit=0`, kill switch ON, bot disabled/inactive, guard loaded/effective, no allow-file/boot symlink or bot/sync processes, checkout `015d2cea927b`. [Checkpoint](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711844958).
- **Actual VPS-0 POST-MERGE:** user ran the **same** launcher as root at `2026-11-07T19:49:45Z`; `vps0.sh: OK`, 148 processes checked, no `main.py` or `sync.sh`; `kill_switch_active=True`; `bot_unit=disabled/inactive`; `guard_condition=FALSE`; `allow_file=absent`; `vps_head=015d2cea927b`; `guard_drop_in=loaded`; `guard_line_in_loaded_unit=1`; `need_daemon_reload=no`; `boot_link=absent`; `guard_condition_effective=effective`; `VPS-0 OK`; **outer `vps0_exit=0`**. The launcher still labels its check “pre-merge” even on second use, but timestamp and sequence establish this as post-merge. Real-host output was provided by user, **not independently run on the VPS by ChatGPT**. [Formal closure on Issue #3](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712228663).
- **Operational boundary:** no VPS deployment, service restart, bot startup, kill-switch release, broker connection, Alpaca runtime keys update, dashboard password rotation or trade was performed under Gate A-1. Vercel existing GitHub integration was left unchanged; expected automatic builds from main merges were accepted, but their status is not part of the VPS gate.
- **Next priorities (planning/read-only until fresh approval):** **Gate A-2** — controlled deployment from exact `963bb893` with backup and rollback, dependency and migration assessment, flag/safety verification and dashboard/authentication smoke tests, while bot remains disabled, kill switch ON and unit guard loaded/effective. Separately coordinate regeneration already done for Alpaca PAPER keys → secure VPS config update without displaying secrets; rotate the dashboard password using bcrypt and remove old plaintext fallback, preserving Google Authenticator, with separately approved service action. **IBKR PAPER activation preparation** — recover/verify PAPER Gateway, exact paper-account routing and live-account isolation, risk authority, ownership/duplicate-order protections, fills and commission reconciliation, monitoring, manual supervised first PAPER orders, then the 10-trading-day unattended soak and 20-paper-trading-day genuine data requirement. No live orders or existing LIVE holding changes. No profitability claims.
- **Preserve scope:** M1–M23, original 71 A01–E09 IDs plus A18 (72 tasks total), historical debt, AI/data/real-paper requirements, and all incomplete items stay in the handoff. Status updates and baselines `PENDING` text, cross-lane E2E test promotion, raw Alpaca adjustment validation are carried forward as controlled work, not silently erased. PR #34 doc reconciliation may be prepared; do not use it to imply deployment.
- **Next decision:** Claude prepares A-2 and IBKR PAPER plans/read-only diagnostics and returns exact runbook, risks, checkpoints and explicit proposed separate authorisations. No service/broker/runtime changes on this handoff update.

---

### 3a-ALL-MERGES-VERIFIED. v3.22 — Gate A-1 GitHub portion complete, VPS-0 POSTCHECK PENDING (7 November 2026)

> **CURRENT resume point — this supersedes v3.21 STOP at step 5.** Claude successfully finished all 24 merges after receiving a separate [operator permission for Option A](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712054723) for step 5. **GitHub merge portion PASS. Full Gate A-1 not closed until a second real VPS-0 reports `vps0_exit=0`.**

- **Actual GitHub main:** independently fetched `963bb893c81c2482a08d78a11b1aafe98f542b51`; its full Git tree is **`f04d1d65dd830894156425f5a870a50591a3a805`**, exactly the pre-approved 24-step final tree. Claude's full run and 24 SHA table are at [Issue #3 comment 6712104253](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712104253).
- **Independent hard verification:** ChatGPT fetched **each of the 24 full Git commit objects** through GitHub and matched four attributes at every step: logged merge SHA, first parent previous step's main commit (starting at `80b4343db4a22413aa670efd32e24952d53e532e`), second parent pinned reviewed PR head, and full tree expected in PR #34 package Appendix A. **24/24 exact matches, no discrepancy.** This includes the one-time locally constructed #15 step-5 commit `30957ffa695c8503360ff6a23baca1da88221f8b` and final #35 step-24 commit `963bb893...`. PR #15 independently confirmed `merged=true`. PR #34 is still open/draft and intentionally not merged in A-1. The independent check does *not* claim to have run the full test suite or executed real VPS checks.
- **Safety boundary:** The operator's first pinned real VPS-0 PASS at 19:14:13 UTC was recorded on [Issue #3](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711844958). Claude reports no VPS action during merges, but a second **real VPS-0** as root must prove this. Expected `kill_switch_active=True`, `bot_unit=disabled/inactive`, no bot or `sync.sh` processes, active `ConditionPathExists` guard, no allow file or boot symlink, and **unchanged** `vps_head=015d2cea927b`, outer `vps0_exit=0`. Use exact [launcher rev4 pinned on Issue #3](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711417527). If STOP/nonzero, halt and investigate. [ChatGPT independent post-merge checkpoint](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712165676).
- **No Gate A-2 approval:** No VPS code deployment, bot/dashboard service restart, broker connection, order, kill-switch release, Alpaca keys update, dashboard password rotation or trading is authorised by completion of the GitHub merges.
- **Upcoming priorities after real VPS-0 passes and A-1 is formally closed:** 1) prepare and separately authorise Gate A-2 controlled M21 VPS deployment (backups, rollback, runtime checks, only required schema migrations, dashboard/auth checks, safe Alpaca PAPER credentials update and password rotation with Google Authenticator retained); 2) IBKR PAPER gateway readiness, account isolation from existing manually managed LIVE holding, duplicate-order prevention, risk and reconciliation, monitored first paper execution, and the specified 10-trading-day soak/20-paper-trading-day validation. Keep entire 72-task historic register and advanced future roadmap. Planning/read-only checks are allowed; runtime/deployment/trading activation require distinct approvals.

**Resume action:** Ask operator to run the identical pinned launcher on actual VPS **now**, supply full redacted stdout to ChatGPT/Claude, and record on Issue #3. Only after `vps0_exit=0` mark Gate A-1 DONE and proceed to *planning* Gate A-2 and IBKR PAPER.

---

### 3a-STOP-AT-STEP5. v3.21 — actual Gate A-1 partial merges; Option A pending explicit approval (7 November 2026)

> **CURRENT resume point supersedes all pre-merge readiness sections.** Gate A-1 was authorised under [Issue #3 approval comment 6711510167](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711510167) and *began*, but **STOPPED safely at step 5/24** after four successful reviewed GitHub merges. No VPS code deployment or paper/live trading occurred. Source: [Claude's exact run report on Issue #3](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711951855); [ChatGPT's narrow independent review](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712005085).

- **Actual merge sequence reported:** step 1 #12 merge `28cbcd9`, tree `a31b018`; step 2 #1 merge `e5547bc`, tree `9c57eb8`; step 3 #14 merge `1ed27b5`, tree `1da598c`; step 4 #13 merge `fe93e9c`, tree `a1aa8dd`. Fresh GitHub read independently confirms `main=fe93e9ca448f65c2c7082c921e01b75c51a7a6d7`, full tree `a1aa8dd666fdb603efdd398b89b6730a700c62f2` (exact pinned step 4).
- **STOP reason:** PR #15 open, `draft=false`, `mergeable=false`/dirty, base retargeted to `main`, reviewed head unchanged `53948821caacbeed91a62bb83ff399b2dcb974e8`. Pinned `gate_a1.sh` halted with `gate_a1_exit=2`, no merge of #15, no retry or revert. The pending final 20 steps (#15 and steps 6–24) remain to complete A-1.
- **Claude's diagnosis, not independently reproduced by ChatGPT:** criss-cross history with three merge bases; local git `merge-ort` computes a clean two-parent merge of current `main` and reviewed #15 head, with exact step-5 expected tree `9a21419ea8cb0925467cfd32edbcfa8e1264b739`, no file hand-resolutions; future steps 6–24 also re-simulated to final expected `f04d1d65dd830894156425f5a870a50591a3a805`. GitHub API's reason for `mergeable=false` is an inference; exact backend reasoning not known.
- **New authorisation required for workaround:** Original Issue #3 Gate A-1 approval explicitly required stopping on any STOP, **no retry/resume/change of merge method without new operator approval**. Claude proposes Option A (local exact two-parent merge commit pushed fast-forward to main with no force, check main tree and GitHub #15 `merged=true`, then resume unchanged pinned script at step 6). This procedure is plausible and preserves the reviewed tree *if* it passes the exact SHA/tree checks; it is **NOT YET AUTHORISED**. Require explicit fresh operator approval to locally construct and push #15's exact merge and to resume subsequent steps, conditioned on current first parent, reviewed second parent, exact tree `9a21419e...`, no edits, no force, no unexpected main movement and stop on any error. If PR #15 does not show merged, STOP. No Gate A-2 deployment or service change under this authorisation.
- **VPS state:** last actual operator-supplied VPS-0 PASS before merges `2026-11-07T19:14:13Z`, kill switch ON, bot unit disabled/inactive, effective guard, VPS checkout `015d2ce`. Claude reports no VPS actions during GitHub mergers. Post-merge VPS-0 is still required once all 24 merges complete. Four accepted automatic Vercel production builds may be triggered, but no deliberate Vercel settings changes were authorised.
- **Next priorities once A-1 is completed:** operator separately wants **Gate A-2 deploy to VPS** (controlled backups, rollback, exact commit, dependencies, only required DB migrations, dashboard authentication + checks, Alpaca PAPER key update and dashboard password rotation under separate approval; trading bot still disabled and kill switch ON), followed by **IBKR PAPER operational readiness and automated paper trading activation** (gateway, exact paper-account verification, live-account isolation, risk & duplicate-order controls, reconciliation, monitoring, real paper evidence/soak). Preserve entire M21 and 72-item task register; no simplification or deletion. Plans/read-only work may be prepared; no Gate A-2, broker connection or bot start approval yet.

---

### 3a-VPS0-PREPASS. v3.20 — operator-supplied actual VPS-0 result passes (7 November 2026)

> **CURRENT resume point:** User ran the exact pinned VPS-0 rev5 safety launcher on actual Ubuntu VPS as root and supplied the stdout. **Pre-merge VPS-0 PASSED** at `2026-11-07T19:14:13Z`, with printed outer `vps0_exit=0`. This is operator-provided real-host evidence, **not a check independently rerun by ChatGPT**. It has been recorded on [GitHub Issue #3](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711844958) without exposing the SSH hostname or credentials.

- Reported `vps0.sh: OK`, no `main.py` or `sync.sh` among 149 scanned processes.
- `kill_switch_active=True`; bot unit `disabled/inactive`; guard-condition standalone false; `ALLOW_BOT_START` file absent.
- `guard_drop_in=loaded`; `guard_line_in_loaded_unit=1`; `NeedDaemonReload=no`; boot symlink absent; `guard_condition_effective=effective`.
- VPS checkout remains exact pre-merge `015d2cea927b`; no GitHub merge is inferred from this check.
- **Next step:** Claude's fresh read-only 24-head GitHub `gate_a1.sh preflight --with-35`. If and only if it returns `PREFLIGHT OK`, Claude executes the **previously authorised** 24 GitHub merges with `A1_APPROVAL_REF=https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711510167` and `A1_VERCEL_CHOICE=accept`. No retries or continuation on STOP. Upon `GATE A-1 DONE`, user must rerun **the same real VPS-0** post-merge and send the output; require exit 0; Claude logs 24 merge commits and expected final tree `f04d1d65dd830894156425f5a870a50591a3a805` on Issue #3.
- **No Gate A-2 authorisation**: do not deploy or restart VPS components, connect to broker, enable bot, release kill switch, or place orders. User has indicated desire to move next to controlled VPS deployment and IBKR PAPER activation *after* A-1, both requiring separate safety evidence and explicit approvals. Alpaca PAPER keys were rotated by operator; secure runtime configuration remains pending separately. Dashboard password rotation with TOTP preserved is pending separately.

---

### 3a-EXECUTION-AUTHORISED. v3.19 — user grants Gate A-1 final execution permission (7 November 2026)

> **CURRENT resume point.** The operator explicitly answered **YES** to the separate authorisation to execute the **complete reviewed 24-merge GitHub Gate A-1 procedure** subject to mandatory safety checks. ChatGPT recorded the approval on [Issue #3, comment 6711510167](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711510167). **This is the authoritative `A1_APPROVAL_REF` for the approved execution.** Posting this note is not executing the merge sequence: **NO MERGES HAVE BEEN OBSERVED AT THIS CHECKPOINT.**

- **Exact reviewed source and runbook:** draft PR #34 pinned head `f44ca2f9f0bdb55f523176c1439765f9249ab9d4` contains Gate A-1 package revision 5. Corrected outer-status VPS-0 launcher [Issue #3, instructions revision 4](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711417527). Merge script SHA-256 `3c991a141e6a4e59941bffd40a205548bfca7d53a74b0cd85798a7d1c92c8adc`; VPS-0 revision 5 SHA-256 `bb5359b8616bc0faecef102e5645b7ce8d6475a8311ed2eeb099f2355a58478d`.
- **Approved order:** #12, #1, #14, #13, #15, #16, #17, #21, #22, #23, #18, #19, #20, #24, #26, #25, #27, #28, #29, #30, #31, #32, **#33**, **#35**, expected final Git tree `f04d1d65dd830894156425f5a870a50591a3a805`. All scoped protected-file C5b changes are approved. Vercel stays connected and unchanged; `A1_VERCEL_CHOICE=accept` acknowledges incidental automatic production builds from the existing integration, **not** manual Vercel deployment.
- **Latest read-only GitHub state at recording of approval:** `main=80b4343db4a22413aa670efd32e24952d53e532e`, PR #34 at `f44ca2f`, PR #33 at `6540e4136f2758c5ecbe2cdbe758019b5090fbee`, PR #35 at `da56e12a9e9baec8634b618cb936be1f40a84a78`, all latter PRs still draft/unmerged. **Claude must rerun fresh VPS-0 and a 24-head preflight before merging**; stale checks are not permission.
- **Execution guards:** (1) root-run pinned VPS-0 launcher in read-only-for-system mode, require *outer* `vps0_exit=0`; (2) fresh GitHub `preflight --with-35`, require 24 merges left and full exact heads/trees; (3) execute pinned script `--with-35`, placing **approval comment URL** into each merge message; (4) on any exception, **STOP** and seek a new explicit decision—no auto retry, resume, revert or force push; (5) after successful `GATE A-1 DONE`, repeat VPS-0, record results and all merge SHAs on Issue #3 and **STOP before Gate A-2**.
- **Scope exclusions:** no VPS deployment, bot/dashboard restart, kill-switch release, broker queries/orders, paper or live activation, Vercel settings change, GitHub visibility change or credential changes. Security work remains separate: the operator says they regenerated the Alpaca PAPER keys; runtime credentials need a separately approved secure update; recommend dashboard password rotation because user does not recall its age, preserving Google Authenticator and all A-0 containment. Do not expose historical or new credentials.
- **What comes next:** Claude executes only the approved safe Gate A-1 operations *after fresh checks*. ChatGPT independently reviews the resulting main merge SHAs/tree and VPS-0 evidence before any later Gate A-2 authorisation. Update this canonical handoff with the **actual observed** GitHub/VPS results; do not mark A-1 complete merely because the approval comment exists.

---

### 3a-FINAL-READY. v3.18 — Gate A-1 launcher verified; operator execution approval still pending (7 November 2026)

> **CURRENT resume point (supersedes historical v3.17 blocking finding).** This is a documentation-only update on `docs/full-canonical-handoff-2026-10-26`. No `main` merge, VPS action, bot deployment, Vercel setting change, broker query or order has occurred through this update.

- **Latest instructions:** Issue #3 [revision 4 execution instructions](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711417527), 7 Nov 18:35 UTC. Claude updated only the launcher after ChatGPT's previous v3.17 review; **no PR #34 code change**. Current PR #34 package v5 exact head remains `f44ca2f9f0bdb55f523176c1439765f9249ab9d4`, draft/unmerged.
- **Launcher correction, independently confirmed:** `( bash -euo pipefail <<'V0RUN' ... V0RUN; rc=$?; echo "vps0_exit=$rc"; exit "$rc" )` captures and returns the nested VPS-0 status instead of only printing it. ChatGPT ran a separate local Bash test of the same status-propagation structure: expected/got 0→0, 3→3, and 1→1. Claude reports **5/5 complete launcher cases PASS**, including no leftover temporary folders and the caller continuing after failure. The separate mock test did not invoke the real VPS and is not an independent rerun of Claude's five end-to-end scenarios.
- **Pinned code unchanged:** `gate_a1.sh` SHA-256 `3c991a141e6a4e59941bffd40a205548bfca7d53a74b0cd85798a7d1c92c8adc` and VPS-0 rev 5 SHA-256 `bb5359b8616bc0faecef102e5645b7ce8d6475a8311ed2eeb099f2355a58478d`; both previously independently verified directly from GitHub. The 24 pinned PR heads, order, expected final tree `f04d1d65dd830894156425f5a870a50591a3a805` and three approved scoped decisions are unchanged.
- **GitHub checkpoint:** `main` reverified at `80b4343db4a22413aa670efd32e24952d53e532e`, PR #34 still draft/unmerged. No GitHub merges have occurred yet. Vercel project stays linked as-is; automatic production builds are expected as an incidental result of later main merges, but no new Vercel work is needed for the existing private VPS-hosted dashboard.
- **Decision still required:** user must separately **authorise execution of the entire reviewed 24-step Gate A-1 merge batch**. Upon that approval, Claude must fresh-run VPS-0 (outer status 0), GitHub preflight (24 reviewed heads), sequential exact PR merges with expected tree checks, VPS-0 again (outer status 0), record merge SHAs and evidence on Issue #3, then **STOP before Gate A-2**. Any nonzero STOP means no further merge, no retry or revert without review/approval.
- **Security maintenance remains independent:** operator reports **Alpaca PAPER** keys regenerated; new key and secret must be configured securely on the VPS before Alpaca-dependent features use them, under separate approval. Dashboard password age unknown, so recommend rotating bcrypt password, removing plaintext fallback if present, retaining TOTP and preserving A-0 containment. Do not echo secrets or make changes under the Gate A-1 merge approval. Public historical branch exposure still warrants cleanup.

---

### 3a-LATEST-REVIEW. v3.17 — Gate A-1 VPS-0 rev 5 & launcher status; Alpaca PAPER credentials rotated (7 November 2026)

> **CURRENT resume point.** No Gate A-1 execution authorisation. Previous milestone/register content and all prior checkpoints are preserved.

- **PR #34 source:** current draft head `f44ca2f9f0bdb55f523176c1439765f9249ab9d4`. The 6 Nov Gate A-1 package now contains VPS-0 **revision 5**, with read-only D-Bus `Unit.Conditions` access via `busctl --json=short`. Effective `ConditionPathExists=/var/lib/algo-trader/ALLOW_BOT_START` must be non-triggering, non-negated and actually present in systemd's parsed conditions list, in addition to the prior drop-in, `NeedDaemonReload=no`, boot link, kill switch, process and unchanged VPS checkout checks. Unknown/unavailable D-Bus data returns STOP 3. This closes the previously identified reset-by-later-drop-in gap in source; actual VPS safety must still be checked fresh before any authorised merge.
- **Independent exact-source verification:** ChatGPT fetched the embedded **both scripts** at `f44ca2f` and independently recomputed SHA-256 with a cross-checked hash function. Merge script `3c991a141e6a4e59941bffd40a205548bfca7d53a74b0cd85798a7d1c92c8adc` and VPS-0 `bb5359b8616bc0faecef102e5645b7ce8d6475a8311ed2eeb099f2355a58478d` match the package exactly; Appendix A merge code is byte-identical to `edcd713`. All **24/24** pinned PR heads were re-read on GitHub and were open/draft/unmerged at the reviewed SHA and allowed base. `main` remained `80b4343db4a22413aa670efd32e24952d53e532e`.
- **Offline tests:** Claude reports **30/30 cases PASS** for VPS-0 revision 5, including 11 new effective-guard/D-Bus scenarios; those use real `busctl` against a private mock D-Bus systemd object, **not** an independently executed check on the real VPS. The 20 merge-script scenarios and exact final tree remain prior Claude-reported evidence. No new full tests were independently run by ChatGPT.
- **One small remaining launcher defect (instructions, not the pinned script):** the published wrapper prints `vps0_exit=$?` after invoking nested `bash -euo pipefail` but finishes with `echo`, causing the *whole wrapper* to exit 0 even if VPS-0 returned 3. ChatGPT independently reproduced this shell behaviour. The launcher must capture the nested status in an `if/else`, print it, and return that *same nonzero status* as the wrapper's final command; test whole-wrapper exit for STOP and checksum failure. **Do not change the 24 PR heads, embedded merge driver or VPS-0 source for this wrapper-only fix.** Finding: [Issue #3 checkpoint](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711240818). HOLD until its focused verification and operator's explicit final merge authorisation.
- **Security note:** user reports they have **regenerated Alpaca PAPER API keys**. No key/secret values copied into GitHub or chat. Revocation of the exposed pair is assumed only to the extent the same paper account was rotated. The bot's data-provider configuration might still reference the old key until a separately authorised secure update. User does not remember when the current dashboard password was set; Google Authenticator 2FA exists, but because the old historical branch was public, recommend a fresh bcrypt dashboard password rotation with plaintext fallback removed, coordinated as a separately approved dashboard-only maintenance operation while keeping Gate A-0 safety intact.
- **All other decisions unchanged:** protected C5b approved; reviewed #33 at step 23; #35 included as step 24; Vercel linked *as-is*, ordinary automatic GitHub production builds accepted but no new Vercel work; no VPS deployment, bot start, service restart, risk override, paper/live broker order, repo-visibility change, or Gate A-2 approval.

**Next action:** Claude makes the tiny **launcher-only** status propagation correction, validates the *outer command's exit* (0 OK, 3 STOP, 1 pre-run failure), and sends only the changed instructions; ChatGPT performs focused check. Then request a **separate explicit Gate A-1 execution authorisation** and record its Issue #3 comment URL before running fresh VPS-0 → preflight → exact 24 merges → VPS-0 → STOP ahead of A-2. Independently coordinate credential rotation and configuration update.

---

### 3a-NOW. v3.16 — Gate A-1 final package revision 4 reviewed, Vercel kept available (7 November 2026)

> **CURRENT resume point** (supersedes older dated checkpoints but preserves every original item). No Gate A-1 execution approval has been provided; this checkpoint is documentation-only.

- **PR #34 current exact head:** `edcd71348db958c74178a16876bb39e345f09e67`, draft, unmerged. Gate A-1 package `docs/reviews/M21_Gate_A1_Review_Package_2026-11-06.md` updated 6 Nov 19:48 UTC. `main` independently rechecked on GitHub 7 Nov and remains `80b4343db4a22413aa670efd32e24952d53e532e`. PR #33 remains at `6540e4136f2758c5ecbe2cdbe758019b5090fbee`, draft/unmerged; PR #35 at `da56e12a9e9baec8634b618cb936be1f40a84a78`, draft/unmerged.
- **Independent focused review: PASS for readiness, NOT an execution approval.** Prior VPS-0 revision 3 printed `VPS-0 STOP` but exited with status 0. New **VPS-0 revision 4** explicitly exits 0 on OK and 3 on STOP, preserving the complete Gate A-0 guard/process/kill-switch/VPS SHA checks. Published SHA-256 `2b90968deb51ccfa09caa4d22a2142a1f59e758b7ea7499c684395b5d9cdc750`. Claude reports 19/19 offline cases PASS, checking output and exit codes; ChatGPT inspected both exact GitHub versions and confirmed the exit instructions are present. These tests were **not** independently rerun on the VPS. The execution wrapper reports `vps0_exit=`; only 0 may continue.
- **Verified no merge-driver drift:** GitHub package comparison at PR #34 heads `4ad8a93` and `edcd713` found **identical** embedded merge-driver Appendix A and **identical 24 pinned PR rows**. Merge script reported SHA-256 remains `3c991a141e6a4e59941bffd40a205548bfca7d53a74b0cd85798a7d1c92c8adc`, expected final tree after 24 approved merges remains `f04d1d65dd830894156425f5a870a50591a3a805`. No GitHub main or VPS changes were executed during this review.
- **User's Vercel clarification:** Keep the already-linked Vercel `algotrading` project *exactly as configured*, as an optional future tool. **Do not build a new Vercel web application or make hosting/DNS/settings changes now**: the privately used bot and dashboard operate through the VPS/Caddy/Flask path, so Vercel is not a dependency of current M21/M22/M23 requirements. Automatic Vercel Production builds on GitHub `main` updates will still occur; the user accepts those incidental builds as-is. Future separate web products, if ever desired, require a new design and authorisation, not a current 'leftover' requirement.
- **Three prior user approvals remain valid:** C5b reviewed protected-file transitions; #33 at step 23 using the already reviewed head with documentation sync afterward; #35 watchdog fix included as step 24. No permission to retarget drafts, execute `gate_a1.sh`, deploy VPS, release kill switch, connect broker, trade, change GitHub visibility or alter Vercel settings has been given by this dated discussion.
- **Outstanding security follow-up, separate but important:** public `archive-broken-implementation` historical branch contains plausible old Alpaca PAPER key/secret and password (secret values never echoed); recommend operator revoke/rotate keys, change any reused password, and only then consider making personal-use repo private. Privacy alone does not revoke previously exposed secrets. This is an open security action, **not a tested Gate A-1 code defect**.
- **Next:** request one explicit operator authorisation for the whole reviewed 24-step Gate A-1 batch, recorded on Issue #3 with a unique URL. Only if granted, Claude runs fresh read-only-state VPS-0 (exit 0), GitHub preflight, sequential GitHub merges at pinned heads and trees, post-merge VPS-0 (exit 0), then records exact results and **stops before Gate A-2**. Any STOP/nonzero means stop and report; no automatic retries or force pushes.

---

### 3a-CURRENT. v3.15 — operator decisions on C5b, PR #35 and PR #33; Vercel retained (6 Nov 2026, 19:35 UTC)

> **CURRENT resume point, superseding v3.14 decisions pending.** The operator has expressly approved three reviewed Gate A-1 decisions and decided to retain the existing connected Vercel project with **no changes to its settings, DNS, or deployment configuration**. Approval of the contents of these changes is **not** an approval to execute the 24-step Gate A-1 merge batch or a VPS deployment. No merge, deploy, broker call or setting change was made by this handoff edit.

| Decision | User decision (6 Nov 2026) | Implementation constraint |
|---|---|---|
| **D-A1-1 C5b** | **APPROVED**: reviewed protected scanner/Alpaca/data-related changes at the pinned heads. | No extra edits; preserve the exact reviewed file transitions. Any new head requires a review. |
| **D-A1-3 PR #35 / A18** | **APPROVED FOR INCLUSION** as proposed step 24, head `da56e12a9e9baec8634b618cb936be1f40a84a78`. | It is stacked on `m21/rc-base` and must follow the reviewed Gate A-1 merge order; do not merge it standalone or deploy/activate the gateway. |
| **D-A1-4 PR #33 / E7** | **APPROVED**: merge already-reviewed head `6540e4136f2758c5ecbe2cdbe758019b5090fbee` at step 23; refresh historical status documentation afterward. | Preserve expected final trees; avoid a mid-gate head alteration or another full-review loop. |
| **D-A1-2 Vercel** | **KEEP EXISTING CONNECTED PROJECT; NO VERCEL CONFIGURATION CHANGES.** Personal-use bot/dashboard, no need for a public Vercel-hosted interface. | Connecting GitHub `main` to the existing Vercel project **means main merges still automatically cause Production builds**. With no settings change, the default is to **accept their occurrence** (anticipated 24), not suppress them. Do not manually deploy/promote, alter Git integration, or touch other Vercel projects. |

**Hosting clarification:** `https://algotrading.marketwarrior.club/` is the existing personal-use VPS/Caddy/Flask trading dashboard per Claude's read-only review. Vercel's `algotrading` project is linked to the same GitHub repository and currently has no custom domain `algotrading.marketwarrior.club` in its Vercel domain list. The custom-domain DNS chain was not independently confirmed; do not claim DNS was verified or alter its records. No evidence that a Vercel preview or production build deploys/restarts the VPS. The user wishes to retain Vercel just in case.

**GitHub repository security:** The trading repository is currently **PUBLIC**. Claude raised apparent historical Alpaca paper API key, secret and an old password in the public `archive-broken-implementation` branch; ChatGPT read `config/settings.yaml` on that branch and independently confirmed non-placeholder-looking values without publishing them. **Rotate/revoke those credentials and change any reused password promptly**, regardless of whether GitHub visibility is later switched to private. User mentioned making the repository private, but has **not requested a visibility change**. Do not change visibility or delete the branch without separate approval. Changing to private alone cannot retroactively erase exposure.

**Next action for Claude:** proceed with scoped preparation: verify pinned heads, check the current branch and VPS-0 requirements and work out the sequencing for the approved changes; send a concise ready-to-run Gate A-1 execution statement that explicitly identifies all 24 steps, the Vercel automatic-build consequence, and the required fresh VPS-0 + GitHub preflight. **STOP before making any merge, marking drafts ready, retargeting PR bases, Vercel setting change, GitHub visibility change, VPS action, or broker activity until the operator separately authorises execution of the complete Gate A-1 sequence.** Current expected tree if all 24 merges: `f04d1d6`. Gate A-2 remains a distinct later approval.

---

### 3a-LATEST. v3.14 — Gate A-1 reviewed; GitHub checkpoint (6 Nov 2026, 18:53 UTC)

> **CURRENT resume point**, superseding historical v3.13 and earlier instructions, but preserving all original 71 tasks and the M1–M23 roadmap. Docs-only update. No merge, VPS deployment, restart, broker call or Vercel setting change authorised.

- **GitHub verification:** ChatGPT checked the 24 proposed PR heads directly on GitHub against the pinned Gate A-1 rows: **24/24 exact**, all open, draft and unmerged. Draft PR #34 head `4ad8a93c77929202c0b7d74e34456f913cfe6147` contains the updated [Gate A-1 package](https://github.com/mikemitakidis/algotrading/pull/34), and optional watchdog draft PR #35 remains at `da56e12a9e9baec8634b618cb936be1f40a84a78`. `main` is last reported `80b4343`; **A-1 not executed**.
- **VPS-0 correction accepted in code review:** revision 3 checks that `gate-a0.conf` appears in loaded systemd `DropInPaths`, the exact `ConditionPathExists=/var/lib/algo-trader/ALLOW_BOT_START` line is present via `systemctl cat`, `NeedDaemonReload=no`, and the boot link is absent even if dangling. It also retains pre-existing process, kill switch, service, allow-file and exact VPS-head checks. Published script SHA-256 `5f2e66f8295922a94d18eaf71b6970d03c10db838f719ee6a9e4d276b4b052d7`. Claude reports **19 offline VPS-0 cases passed**, including guard-removal and stale-loaded-state faults; this has not yet been executed against the actual VPS. The GitHub merge script is unchanged at reported hash `3c991a141e6a4e59941bffd40a205548bfca7d53a74b0cd85798a7d1c92c8adc` (20 reported offline cases). ChatGPT inspected the source, but did not independently rerun the full suites.
- **Gate A-1 status: READY FOR FOUR OPERATOR DECISIONS, NOT AUTHORISED.** (1) C5b protected-file transition approval (including #35 if chosen); (2) Vercel choice, including acceptance of 23 or 24 website Production builds or a separately verified pause/disconnect; (3) include optional #35 as step 24; (4) merge #33 at the reviewed head and refresh its status later, or modify/re-review it mid-gate. Proposed trees: `5b0f40b` (23 merges), `f04d1d6` (with #35). These builds don't deploy the VPS, but *can update the public website*. `main` has no branch protection. VPS-0 and GitHub preflight must be fresh immediately before approved merges, VPS-0 again after; stop before Gate A-2. Claude recommends approve/accept/include/merge #33 as reviewed, but the operator has not yet decided.
- **Corrected timeline and state:** Gate A-0 actually ran on **5 Nov at approximately 19:31 UTC** (time attested in Claude Code's private VPS report; completion recorded to GitHub **6 Nov**). Gate B found gateway systemd `failed` (explicitly stopped 3 Sept), not merely 'inactive'. Gate A-0 `gate_a0_exit=0`; existing manually managed live holding untouched, kill switch active, systemd bot disabled and guarded, exposed .env backup mode 644→600, no orders or deploy. H3/H4 not separately run, but the block's own final checks were accepted.
- **Documentation integrity:** Claude returned PASS for handoff v3.13 `74f1f3c` with five factual changes, now reflected in v3.14: timestamp, gateway failed state, D-3/D-4/D-5 in §4 inventory, stale §7 resume point, PR #34 new head. A18 remains new numbered task 72, E07 specific exposure CLOSED, original 71 IDs/titles remain unchanged. Historic debt D-3/4/5 are **carry-forward notes, not new numbered IDs** and not M23 tasks D01–D10. Both IBKR PAPER research and separately gated future eToro LIVE are preserved.
- **Next:** operator decisions and separate explicit approval recorded on [Issue #3](https://github.com/mikemitakidis/algotrading/issues/3), then and only then Gate A-1. No new coding or correction is needed for this gate's reviewed VPS-0 procedure.

---

### 3a-NEW. v3.13 — operational completion and independent Gate A-1 review (6 Nov 2026, 18:35 UTC)

> **Current dated checkpoint (supersedes historical resume points elsewhere in this document).** This section is a documentation-only update. Existing milestone numbering, original 71-task register and all v3.0–v3.12 history are preserved. A18 is an **additional** tracked task (72 tracked IDs total); it does not rename or delete any earlier task. Source of operational claims is the operator's / Claude Code's sanitised VPS report, independently assessed by ChatGPT, **not a new VPS read**. Tests stated below were reported by Claude's engineering review; ChatGPT inspected the referenced GitHub material but did not rerun their full suite.

**Architecture and authority unchanged.** Two distinct lanes: permanent **IBKR PAPER** laboratory for scientifically valid research and *separately approved* broker validation; future **eToro LIVE** funded track only after independent risk, broker/legal, evidence and operator gates. Advanced AI modelling and trustworthy point-in-time learning evidence remain parallel priorities. No permission for broker connections, paper/live orders, funded capital, dashboard activation, Telegram send, service starts, deployment, PR merge, private data export or subscription follows from this checkpoint.

**Source-of-truth status (as verified/reported at this checkpoint):**

| Surface | State |
|---|---|
| GitHub `main` | `80b4343db4a22413aa670efd32e24952d53e532e` (reported 6 Nov in Gate A-1 GitHub preflight; no Gate A-1 merges yet) |
| VPS deployed checkout | `015d2cea927b9b4db3ee65604e0b4ebb58211e61` (operator/Claude Code reported 5 Nov; separate new VPS-0 check required before any merge) |
| Full canonical handoff | **v3.13**, this document, on isolated `docs/full-canonical-handoff-2026-10-26` branch, **not on main** |
| Roadmap/operational status | Draft PR [#34](https://github.com/mikemitakidis/algotrading/pull/34), exact reviewed head `86c6c0a25fdfdc15c1a0da4bf2c4ba6d0d434d3a` (status 3.0.8; roadmap v3.0 still DRAFT) |
| Watchdog offline fix | Draft PR [#35](https://github.com/mikemitakidis/algotrading/pull/35) head `da56e12a9e9baec8634b618cb936be1f40a84a78`; **NOT merged/deployed** |
| Canonical checkpoint trail | [Issue #3](https://github.com/mikemitakidis/algotrading/issues/3); 5–6 Nov contains Session 1/2, Gate A-0 and Gate A-1 preparation |
| Actual trading | No new paper or live orders; broker allocation stays signal-only (five lanes per Session 2). Existing pre-existing live holding remains manual |

**29–30 October carried forward.** All 71 original tasks remain described below. v3.12 records the 27–29 October draft engineering work and E1–E7 (#27–#33). The **M21 Finish Line** at `docs/handoffs/M21_Finish_Line_2026-10-28.md` defines the later reviewed Gate A sequence, 23 merge steps, C5b transitions, OP1–OP5 and the E6 dashboard shape. Those are engineering proposals and offline evidence, not production acceptance. The 26 October WS2 corrected PR #1 remains part of the 23-step merge plan; it is **not** an authorised paper scheduler or broker execution.

**5 November operational investigation (Gate B):** Session 1 and Session 2 were read-only. Session 2 `exit=0`, eight verified script checksums, all five policy lanes signal-only; found 27,994 `gw-watchdog` event-loop errors, historical data for only one stale symbol, missing WS2/D2 lifecycle tables (NOT VERIFIED), and a mode-644 `.env` backup. Bot was inactive, although enabled for boot before containment; gateway systemd FAILED (explicitly stopped 3 September); dashboard running. The separate watchdog defect is **A18**, fixed offline in PR #35, not on the VPS.

**Gate A-0 revision 6 — EXECUTED, CLOSED.** Approved script from PR #34 commit `f179029cd7cadeb45342d2b71a0c2cd61f65667d`, SHA-256 `86f9f75292b7c121e8cc5e0e0e20fbd1725ae1ca50ee38211163ecaa221eace7`. H1 verification succeeded; **actual run on 5 November 2026 at approximately 19:31 UTC** (`checked_at_utc=2026-11-05T19:31Z` in the accepted attestation), and **GitHub completion recorded on 6 November**. Operator reported paper 0 positions and working orders; live one pre-existing manually managed position, zero working orders. `gate_a0_exit=0`; kill switch ACTIVE, `algo-trader.service` disabled and inactive, boot link removed, systemd `gate-a0.conf` guard loaded and its condition false, allow-file absent; historical mode-644 `.env` backup corrected to 600 without reading file contents. No broker calls/orders, service starts/stops, deploy, Git modifications on VPS, or alteration of pre-existing live holding. H3/H4 were **not separately executed**; this acceptance relies on the block's own final checks and `STATE AT EXIT` as reported. **Important scope**: systemd bot starts blocked; dashboard/direct `main.py` starts are *not* disabled, but IBKR bot submissions and automated flatten are blocked by the kill switch; remaining lanes remain signal-only by current policy. Do not repeat A-0, bypass controls or release the kill switch; later actions have separate approvals.

**Gate A-1 — REVIEW ONLY, NOT APPROVED OR EXECUTED (6 Nov 2026).** Package `docs/reviews/M21_Gate_A1_Review_Package_2026-11-06.md` and engineering assessment `docs/reviews/M21_Gate_A1_Engineering_Review_2026-11-06.md`, both in draft PR #34 at `86c6c0a` **at the historical v3.13 checkpoint**; the current v3.14 head is `4ad8a93`. The package pins 23 PR heads/merge trees with `main` starting at `80b4343`: proposed final tree `5b0f40b`. Optional PR #35 as step 24 leads to `f04d1d6`. Claude reports real-GitHub read-only preflight, 23/23 conflict-free local merge simulations, full offline suite on **exact proposed final trees** (134 modules/0 failures; with #35 135/0), merge driver 20 offline tests and VPS-0 9 offline tests. These are **reported test results**, not a verified live merge. The 23/24 sequential merges would trigger 23/24 public website Vercel production builds if accepted. `main` has no branch protection, so per-step immutable head/tree checks and explicit approvals matter. Gate A-2 deployment and OP2/OP3 activation remain CLOSED to execution.

**Historical v3.13 review finding (VPS-0 issue resolved by v3.14):**
- The revision-2 merge script uses a proxy-specific REST fallback when GraphQL `gh pr ready` is unavailable; it verifies `draft=false` and fails closed if unavailable. Offline-tested; do not represent it as proven to operate on an actual draft without executing the state change.
- **Narrow correction required:** the revision-2 **VPS-0** check currently computes `systemd-analyze condition ConditionPathExists=/var/lib/algo-trader/ALLOW_BOT_START` (= false) but does **not** check that `gate-a0.conf` is actually loaded in the bot systemd unit. Evaluating a standalone false condition is not proof a unit enforces it. Before approving merges, add read-only verification of the loaded drop-in, exact unit guard line, `NeedDaemonReload=no`, and the boot link absent, with fail-closed STOP and offline negative tests. Review and pin the corrected package head. No new changes to A-0 are needed.
- **Four operator decisions remain OPEN:** D-A1-1 explicit C5b protected-file approvals (#12/#15/#16; and #35 if included); D-A1-2 Vercel `accept` / independently verified `paused` / `disconnected`; D-A1-3 optional #35 included after exact-head review; D-A1-4 #33 merge-day note: reviewed-head merge plus post-A-1 status correction, or a separately reviewed mid-gate edit. Claude recommends approve C5b, accept Vercel builds, include #35, and keep #33's reviewed head; **these are proposals, not operator approvals**.
- PR #35 patch adds event-loop import and thread-loop safeguards and keeps failed probes fail-closed; its tests and baseline approval transition are on the PR, not the deployed tree. Any approval to merge is separate from permission to connect to IBKR or activate the service.
- Deployment-trigger caveat: GitHub webhooks were not fully listable by Claude's access; VPS-0 must be freshly run before *and after* the merges. No code/config deployments are approved.
- Review consequence: **HOLD A-1** until the narrow VPS-0 guard check is corrected, new exact PR #34 head is reviewed, the four decisions are provided explicitly and the operator gives separate merge authorisation.

**Task register reconciliation (no erased work):** Original 71 IDs A01–A17, B01–B24, C01–C11, D01–D10, E01–E09 remain below with historical status text. **A18** is an added, distinct M21 gateway-watchdog defect (PR #35; offline fix pending merge and runtime proof). **E07** is now CLOSED for the previously identified world-readable backup from the Gate A-0 metadata-only permission correction (other future secret-permissions inspections remain ongoing). July's older `docs/PREVIOUS_MILESTONE_DEBT.md` exists only on the unmerged `m21-docs-debt-register` branch and must be reconciled, especially **D-3 IBKR paper account/equity reader**, **D-4 backtest integrity audit**, **D-5 M1–M4 runtime re-verification**. D-1 relates to C01, D-2 to A13; do not silently declare D-3…D-5 completed. The cross-lane E2E test was included only on an integration branch, not the 23-PR final tree; keep it as a follow-up after A-1. Verify Alpaca `adjustment="raw"` against an authorised real API before running the new scanner in later operational gates.

**Historical next action (SUPERSEDED by v3.14):** ChatGPT updates this full handoff **on a separate documentation branch only** and asks Claude for an exact-SHA consistency check against PR #34, Issue #3, roadmap and the original 71 items. Claude should make the narrow VPS-0 correction in PR #34, rerun its negative tests and send the new exact head for independent review. **No Gate A-1 execution, PR merges, Vercel setting changes, VPS actions or broker actions are authorised here.**

---

### 3a-0000000. v3.12 — release preparation (29 Oct 2026, evening)

- The 16 PR heads are unchanged (re-verified on GitHub) and still merge onto `main` `80b4343` conflict-free to tree `f1ff536`.
- E1–E7 are draft PRs #27–#33 on `m21/rc-base` (= `f1ff536`); the proposed `main` tree with them is `1ed8c0f`, and `m21/integration` `b7b8a90` is that tree plus the integration-only E2E test.
- E6's protected `dashboard/app.py` change (+6 lines) is a verified patch in PR #32, **not applied**.
- OP1 is final and verified read-only on a synthetic VPS replica; it has not been run.
- Vercel creates a Preview deployment for every branch push and a Production deployment for every push to `main`.
- Details, evidence and the merge sequence: Finish Line §7d. Nothing merged; `main` unchanged; runtime flags OFF; all 71 tasks kept.

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
  - **v3.12 (29 Oct, evening):** E4 score-versus-outcome report from the ledger on draft PR #30 (read-only; classes never pooled; only IBKR-statement-matched trades can carry a verdict, never predictive). Ledger migration not deployed, writer OFF.
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
  - **v3.12 (29 Oct, evening):** E5 statement reconciliation on draft PR #31: journal claims vs the operator's IBKR Flex export (MATCHED / MISMATCH / PROBABLE / UNMATCHED_CLAIM / UNRECORDED / UNATTRIBUTED); statement commissions feed fee calibration; missing fees never zero. Needs the real statement (OP1.6/OP1.7).
- [ ] **A14 · Production monitoring for paper research.** Reliable services, restart recovery, connectivity health, stale data detection, backups/restores, structured logs, alert escalation, capacity monitoring and durable journaling, tested without live-capital exposure. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **A15 · Four original starter templates.** Implement/verify, independently, (1) trend-following multi-TF confluence; (2) mean-reversion RSI/Bollinger; (3) breakout/momentum volume/ATR; (4) pullback-in-trend. Validate exits and adjustable parameters, and make template identity explicit in outcomes. No unreviewed switch to paper/live. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** Lane D on main `80b4343`. History: Lane D @ 33a05ad: the four templates unchanged in behaviour; each now also reports its setup and the filter that rejected it. Real-data validation and multi-TF confluence on real bars (M17 scanner_replica) remain.
- [ ] **A16 · Original dashboard strategy-controls scope.** Selected template, RSI/MACD/EMA/Bollinger/ATR/volume/confluence/stop/target/trailing parameters, validation, versioning, audit, rollback, least privilege. No unrestricted free-form user code or silent live parameter changes. **Status (27 Oct 2026): NOT_STARTED.**
  - **v3.12 (29 Oct, evening):** E6 on draft PR #32: auth + CSRF blueprint over the WP9 store (OFF by default: 503 unconfigured, 403 read-only); the protected `dashboard/app.py` wiring is a prepared, verified patch, NOT applied.
- [ ] **A17 · M17/M16 real-bar parity debt.** Confirm adequate real 15m/1h/4h/1d coverage and proper corporate actions, reproducible scanner_replica parity on actual historical candidate rows (not only synthetic). Preserve missing-data fail-closed semantics. Consolidate/retire legacy backtest engines only after proved equivalence. **Status (27 Oct 2026, v3.6): CODE_MERGED (synthetic parity) / BLOCKED (real bars).** On main `80b4343`. History: PR #6: 6,122/6,122 exact counterfactual-vs-M17 parity on unambiguous synthetic paths. Finding: M17 books a stop when a bar gaps above the target then touches the stop (pessimistic tie rule) — documented, not changed. Real-bar parity needs an approved M16 export.

- [ ] **A18 · IBKR gateway watchdog event-loop defect (M21 B-6 / OP2).** VPS logs show repeated `gw-watchdog` failures to initialise an asyncio loop. PR #35 at `da56e12a9e9baec8634b618cb936be1f40a84a78` provides an offline-only fail-closed fix and regression tests, but is still **DRAFT / NOT MERGED / RUNTIME_UNVERIFIED**. Exact-head independent review, C5b protected transition approval, Gate A-1 merge permission and later controlled gateway validation are separate gates. **Added in v3.13 (6 Nov); does not replace or renumber A01–A17.**

### B. Evidence + integrity + more advanced AI, developed in parallel (M21.2 / M21.3 / M21.4–M21.7)

- [ ] **B01 · Historical data inventory.** Inspect *available* M16/M18 datasets, timestamps, frequency, gaps, market sessions, corporate actions and as-of universe membership; no private VPS export or paid vendor access without approval. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (tools) · BLOCKED (real inventory needs approval).** On main `80b4343`. History: v3.4: PR #8 @ 77139cc also rejects non-finite / non-numeric volume. PR #8 @ e813b49: quality flags now drive grades and research admissibility (warning vs blocking bits; unknown bits block); retrospective full-sample audit separated from a causal split flag usable point-in-time (causal recall 87.5 %, precision 98.2 % vs retrospective 96.9 % / 98.4 %). VPS read-only runbook needs operator approval.
  - **v3.12 (29 Oct, evening):** OP1 finalised 29 Oct (read-only scripts verified on a synthetic VPS replica: M16 inventory, parquet adjustment basis, readiness, runtime evidence). Not run. E3 (PR #29) is the converted-input path for real M17/M18 runs.
- [ ] **B02 · Data lake readiness.** Evaluate licensing-compatible survivorship-free/delisted constituent data and point-in-time fundamentals; Linux-compatible ingestion, quality/freshness audits and immutable snapshot hashes; propose vendors, do not purchase. **Status (27 Oct 2026, v3.6): CODE_MERGED (memo + manifests) · decision pending.** On main `80b4343`; nothing purchased. History: PR #8: vendor/licensing memo with sources verified 27 Oct (Norgate Platinum/Diamond delisted + constituents, Windows updater, personal-use licence; Massive ex-Polygon $29–$199/mo personal tiers keep delisted tickers; Sharadar publication-approval restriction; Alpaca Algo Trader Plus $99/mo). Nothing purchased.
- [ ] **B03 · Counterfactual evidence v0.** Label accepted, rejected, gated, near-miss, historical anchors and shadow candidates for hypothetical path outcomes. Flag unobservable stop-vs-target order, executable-fill unknowns, incomplete prices, costs, overlapping windows and clusters. Never label EOD ambiguity as certain intraday fill or treat counterfactual as realised P&L. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: v3.4: PR #6 @ ed62fdb: NO_POSITION candidates are `unfillable`, never executed, no daily capacity. PR #6 @ 969cec7: release maps fail closed for any accepted candidate without a release entry (unfillable = explicit NO_POSITION); real data must carry explicit bar opens; new `simulated_entry` stage so backtest fills are never 'executed'. 27 tests; repro mutants killed.
- [ ] **B04 · Gate-economics/regret ledger.** Report observational avoided/foregone hypothetical R by each filter, holdout, threshold and Risk Authority reason. Do not infer causal benefit absent appropriate comparison. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: PR #6: observational gate-economics ledger with date-cluster bootstrap CIs and non-causal caveat.
- [ ] **B05 · Immutable experiment/trial registry.** All tried/failed/discarded hypotheses, family IDs, code/data/config/model hashes, splits, seeds, economic assumptions, CI report cards, contamination/research access ledger and verified references. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE.** On main `80b4343`. History: PR #5 @ da6052c: trials carry outcome artifacts and real code identities; the report card verifies the candidate's gross series and every family-matrix column against them (substituted outcomes with genuine ids fail). 43 tests; repro mutants killed.
- [ ] **B06 · Sealed holdout and preregistration.** Separately permissioned holdout and access log; Git signed identity and independent/server-side timestamp attestation, frozen specifications and one-use final tests. Never rely on a mutable Git commit date or CODEOWNERS alone as security. **Status (27 Oct 2026, v3.6): CODE_MERGED (local policy model) / NOT_STARTED (operational independent authority).** On main `80b4343`. History: PR #5 @ da6052c: attestations re-verified cryptographically at consumption; holdout release needs a valid, unexpired, separately signed approval and an attestation before the forward window and the seal. The verifier is labelled a LOCAL HMAC POLICY MODEL (independent=False) everywhere it is used; an operational asymmetric signer is still required.
- [ ] **B07 · Anti-overfit testing suite.** Purging/embargo and genuinely forward splits, CPCV/CSCV PBO, deflated Sharpe with effective trial multiplicity sensitivity, White Reality Check/SPA as appropriate, reality checks for label overlap, walk-forward regime/market splits and transaction-cost stress. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE.** On main `80b4343`. History: PR #5: purged k-fold, purged walk-forward, CPCV, leakage verifier, DSR/PSR, PBO (CSCV), White RC, Hansen SPA, Holm/BH/BY, cost-stress report card.
- [ ] **B08 · Independent statistical canaries.** Large pre-registered known-null batteries, planted synthetic positives, negative controls (shuffled labels/placebos), proper uncertainty intervals on false pass/detection rates. Five nulls alone cannot estimate a ≤5% false-positive rate reliably. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE.** On main `80b4343`; no calibrated integrated canary yet. History: v3.5: PR #5 @ 3b5e8f9: null-only batteries are NULL_ONLY; PASS needs a ledger-verified battery for the exact pipeline (family + code) with re-derived positive-control power. PR #5: one-use battery plans, CP lower-bound power rule. Lane C (PR #7) adds a CALIBRATED lab canary: 0/59 null false claims (UB 5.0 %), power 20/20 at x1.5.
- [ ] **B09 · Strategy Genome v0.** Declarative, constrained JSON/schema of existing templates, typed entry/exit/risk/timeframe, semantic IDs, lineage/variant accounting, fail-closed validator, reproducible M17 parity. Offline research only; no direct broker path. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: Lane D @ 33a05ad: genome v0 unchanged; M17 trade-for-trade parity kept (legacy close labels checked equal to the new bar-close fields).
- [ ] **B10 · Multi-symbol and portfolio-aware event backtesting.** Time-aligned data, borrow/short restrictions, capital/slot/exposure and correlations, spreads/slippage/gaps, survivor-free membership, next-open/intrabar ambiguity policies, deterministic fixtures, throughput benchmarking and parity with M17 where valid. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic) · corporate actions and liquidity NOT modelled.** On main `80b4343`. History: v3.4: no corporate-action/split adjustment yet; the integration fails closed on positions crossing suspected unadjusted splits (PR #11 @ 5cc3dce). Lane D @ 33a05ad: trades now carry decision time, fill-bar open (entry/exit fill events), bar-close labels and explicit unobservable intrabar windows; one decision record per evaluated candidate (entered / rejected by rule / position open / re-entry / universe / capacity / risk / unfillable / no next bar). Borrow/short, sector caps and real-data throughput remain.
  - **v3.12 (29 Oct, evening):** E5 (PR #31) feeds statement commissions of MATCHED paper legs into `fee_calibration` (suggestions never applied).
- [ ] **B11 · M21.2 parameter optimisation.** RSI, MACD, EMA/MA, Bollinger, ATR, volume, confluence, stops, targets and trailing variants. Search parameter *surfaces and plateaus*, not best isolated backtest; count every trial and compare no-trade/simple baselines. **Status (27 Oct 2026, v3.6): PARTIAL (foundations CODE_MERGED).** On main `80b4343`. History: Lane D @ 33a05ad: search trials record real code identity, the market seed, outcome artifacts and accept the as-of universe mask. Full M21.2 surfaces on real data remain.
- [ ] **B12 · M21.3 out-of-sample walk-forward optimiser.** Train/validation/testing fully separated; embargo overlapping labels; sealed final sample; cost-adjusted net expectancy with confidence intervals, drawdown, expected shortfall/CVaR, turnover, capacity and stability across market regimes. Win rate/avg-win-to-loss remain recorded but are not sole approval metrics. **Status (27 Oct 2026, v3.6): PARTIAL (infrastructure CODE_MERGED).** On main `80b4343`. History: v3.5: PR #5: PBO / SPA / Reality Check only over the complete comparable search family. Purged walk-forward + report card in PR #5; optimiser not started.
  - **v3.12 (29 Oct, evening):** E1 (PR #27) turns walk-forward runs into report cards bound to the exact calibrated canary experiment; v3 expected digest on the proposed tree with E1–E7: `7c49b813…6b26b`. Canary NOT run.
- [ ] **B13 · M21.3 Promotion & Approval Model.** Preserve GREEN safer-only/AMBER material/RED increased risk/new champion distinctions, shadow-first, change budget, explicit reviewer/operator authorization for behaviour changes and fail-safe deactivation/rollback. Conservative changes must be proven genuinely safer (e.g. a tighter stop can increase realised losses through churn); never auto-promote risk. **Status (27 Oct 2026): NOT_STARTED.**
- [ ] **B14 · Advanced ML laboratory NOW, not deferred.** Baseline logistic/linear/random-forest/boosted trees versus XGBoost/LightGBM/CatBoost and appropriate temporal/representation challengers; deterministic offline comparison, feature-as-of audit, proper train/calibration/test, model cards and reproducible experiments. No claimed real alpha from synthetic data. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`; linear and regime benchmarks not matched. History: v3.5: PR #7 @ f3801bf: fold threshold / conformal use only resolved outcomes; unobserved barrier paths dropped; evidence regenerated. PR #7 @ 8f64a07: dataset digest covers every field (schema v2); trials carry real code identity and outcome artifacts; evidence regenerated with the same honest findings (null and interaction matched; linear and regime NOT matched, not re-tuned).
- [ ] **B15 · Ensembles / model diversity.** Independently trained feature families, out-of-fold stacking, calibrated probabilities, robustness against correlated errors, ablations, calibration quality and honest abstention thresholds. Demonstrate incremental OOS gains over simpler models before promotion. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: PR #7: mean/stacked/bagged ensembles tested; dispersion analysed.
- [ ] **B16 · M21.4 meta-labeling.** Take/skip/downsize (no authority to widen risk limits) from point-in-time candidate/outcome labels, realistic costs and selection-bias correction; probability calibration, uncertainty interval, decision-curve economics and independent OOS testing. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: PR #7: meta-labeling on primary-rule candidates vs take-all; integration branch meta-labels lane D genome candidates.
- [ ] **B17 · M21.5 regime-aware thresholds.** Bull/bear/range, volatility/breadth/dispersion/liquidity states; causal as-of features and realistic detection lag. Start as shadow conditioning and risk throttles, not brittle instant regime switches. **Status (27 Oct 2026, v3.6): CODE_MERGED / TESTED_OFFLINE (synthetic).** On main `80b4343`. History: PR #7: regime model + regime-blind twins; on the regime benchmark the context gain over logistic_noctx (raw Holm p 0.018) was withheld by the skill gate (neither beat the prior on probabilities).
- [ ] **B18 · M21.6 market context.** VIX/volatility term structure, breadth, sector rotation, index trend, macro and news/event calendars, earnings/central-bank proximity, delayed-revision control, licensing, source availability and event-time correctness. **Status (27 Oct 2026): NOT_STARTED.**
  - **v3.12 (29 Oct, evening):** E2 (PR #28) logs the WP15 context into every WS2 cycle, shadow only (`MARKET_CONTEXT_SHADOW_ENABLED` unset); context sources still decision G4.
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
  - **v3.12 (29 Oct, evening):** E3 (PR #29): real M16 bars reach M17 multi-timeframe / M18 builds only through verified completed-bar conversion with calendar evidence; the fail-closed guards are unchanged.
- [ ] **E06 · M15 deferred UX/security items.** Login latency investigation; persistent rate limiter only if warranted; multi-user/read-only roles **deferred indefinitely unless a concrete need arises**. Do not revive cosmetic dashboard work as a current priority. **Status (27 Oct 2026): NOT_STARTED.**
- [x] **E07 · Secret and backup permissions (specific identified exposure closed by Gate A-0).** Prior historical report flagged an .env backup as overly permissive; check metadata only with operator permission and remediate via separately approved controlled action; do not read or expose credentials. **Historical status (27 Oct 2026): BLOCKED (approval). Current v3.13 status: CLOSED for the known exposed backup (644→600 under approved Gate A-0); no .env contents read; future secret hygiene remains ongoing.**
  - **v3.12 (29 Oct, evening):** OP1 records `.env*` permission metadata and variable names only (never values).
- [ ] **E08 · Canonical documentation reconciliation.** Update GitHub ROADMAP.md, MILESTONE_STATUS.md, NEXT_WORK_REGISTER and status snapshot to match **actual code/activation state**, preserving original milestone numbers, exact commit references, and deliberately deferred audit items. No accidental “D3 not started” after code merge. **Status (27 Oct 2026): NOT_STARTED.**
  - **v3.12 (29 Oct, evening):** E7 (PR #33) reconciles ROADMAP.md and MILESTONE_STATUS.md; the Finish Line §6 and this register are updated on the docs branch; nothing unmerged marked complete.
- [ ] **E09 · Test and deployment governance.** Isolated no-secrets offline CI, protected paths, reviewer checks on exact head, deterministic tests and mutations, PR-only updates; no unreviewed auto-sync, service restarts or production DB writes. **Status (27 Oct 2026, v3.6): PARTIAL (inactive CI example CODE_MERGED; activation needs approval; clean runner unverified).** On main `80b4343`. History: v3.5: the inactive CI example lists all eight research suites (PR #8 @ 22d07d5); still INACTIVE. PR #8 adds an INACTIVE least-privilege research CI example (docs/research/ci, not in .github/workflows). Research suites also run under production-pinned numpy 1.26.4 / Python 3.12 (see v3.2 §3a).

**Unregistered previous-milestone debt — explicit carry-forward notes (not new A01–E09 identifiers).** The July `docs/PREVIOUS_MILESTONE_DEBT.md` is on the unmerged `m21-docs-debt-register` branch. D-1 maps to C01, and D-2 overlaps A13. The following items remain OPEN; do not confuse them with M23 tasks D01–D10:
- [ ] **Legacy D-3: IBKR PAPER account/equity reader wiring.** Safely establish provenance and integration; no broker query approved here.
- [ ] **Legacy D-4: backtest integrity audit.** Validate time alignment, corporate actions, fill assumptions, leakage and reproducibility against trustworthy evidence.
- [ ] **Legacy D-5: M1–M4 runtime re-verification.** Retest in an authorised current runtime; historical test coverage is not production proof.
The original 71 numbered tasks plus new A18 remain 72 unique numbered tasks; these are separate notes.

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

> **CURRENT RESUME POINT (8 Nov 2026, v3.24):** Gate A-1 CLOSED/PASS; main stays `963bb893`. Claude prepared Gate A-2 and IBKR PAPER operational plans on **draft PR #34 `ac3ef2b`**. Five genuine paper activation blockers confirmed in actual M14/ingestor source. Independent review found **three targeted runbook issues** (recheck full guard immediately before deployment, DB integrity failures must cause read-only inventory warning/STOP, and combined read-only wrapper must propagate VPS-C failure), and required more robust paper-only P&L design and cross-scope gates. [Issue #3 review](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6726669904), §3a-A2-PAPER-REVIEW. **No Gate A-2 deployment, credential update or PAPER activation approved yet.** Separate code-only draft PR authorisation for BLK-1…5/H-1 is the next decision; keep live isolated. Earlier resume blocks are historical.


> **CURRENT RESUME POINT (7 Nov 2026, v3.23):** Gate A-1 **FORMALLY CLOSED/PASS**. GitHub main `963bb893c81c2482a08d78a11b1aafe98f542b51`, tree `f04d1d65dd830894156425f5a870a50591a3a805`, all 24 first-parent merge chain/second-parent reviewed heads/trees verified. User-provided actual VPS-0 PRE and POST outputs both exit `0`; POST timestamp `2026-11-07T19:49:45Z`, kill switch ON, bot disabled/inactive, effective guard, no `main.py` or `sync.sh`, VPS checkout unchanged at `015d2ce`. [Formal closure Issue #3](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712228663). **Next: only Gate A-2 deployment PLANNING and IBKR PAPER activation PLANNING/read-only, separate approvals required for runtime changes.** PR #34 docs still draft. See §3a-GATE-A1-CLOSED; previous dated resume points are historical.


> **CURRENT resume point (7 Nov 2026, v3.22):** Claude completed all **24** Gate A-1 merges, now on `main=963bb893c81c2482a08d78a11b1aafe98f542b51` with expected Git tree `f04d1d65dd830894156425f5a870a50591a3a805`. ChatGPT independently fetched **each of 24 Git commits**; all first-parent chains, pinned second-parent reviewed heads and expected trees match (24/24 PASS). [Issue #3 verification](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712165676). **Gate A-1 awaits POST-MERGE operator VPS-0 output, must end with `vps0_exit=0` and VPS unchanged `015d2ce`; do not mark gate closed until then.** No Gate A-2 code deployment/bot startup/IBKR order authorised. See §3a-ALL-MERGES-VERIFIED; older dated resume points historical.


> **CURRENT resume point (7 Nov 2026, v3.21):** Gate A-1 ran and **stopped safely at step 5**. Steps #12/#1/#14/#13 merged with pinned trees; `main=fe93e9ca448f65c2c7082c921e01b75c51a7a6d7` tree `a1aa8dd...`. PR #15 unchanged at head `53948821...` but GitHub says mergeable=false after retarget to main. Claude proposes Option A: construct exact two-parent merge with expected tree `9a21419...`, direct no-force push, verify #15 merged and resume unchanged script. **Original approval forbids resume/workaround without a fresh operator decision; not yet authorised.** See [Issue #3 review](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712005085), §3a-STOP-AT-STEP5. VPS and trading inactive, post-complete VPS-0 pending; later Gate A-2 and IBKR PAPER priorities to prepare separately, no activation yet.


> **CURRENT resume point (7 Nov 2026, v3.20):** Pre-merge VPS-0 on the real VPS passed according to user's complete output at 19:14:13 UTC; `vps0_exit=0`, bot disabled, kill switch ON, loaded and effective condition, no bot/sync processes, VPS SHA `015d2cea`. Report on [Issue #3](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711844958). The exact 24-merge batch is already authorised via [Issue #3 approval](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711510167). Claude MUST now repeat fresh GitHub preflight, execute only if PASS, demand post-merge VPS-0=0, then stop before A-2. No VPS deployment or PAPER trading activation yet. See §3a-VPS0-PREPASS.


> **CURRENT resume point (7 Nov 2026, v3.19):** The operator has separately and explicitly authorised the reviewed **24-merge GitHub Gate A-1** procedure under [Issue #3 approval comment 6711510167](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711510167); this is the authoritative `A1_APPROVAL_REF`. Exact runbook is Issue #3 instructions revision 4; PR #34 head `f44ca2f`. As of the approval, main was still `80b4343`, no merges executed. **Fresh VPS-0 must exit 0, fresh 24-head GitHub preflight must pass, then exact 24 ordered merges, VPS-0 again, evidence on Issue #3, STOP ahead of Gate A-2.** No VPS deployment, trading, manual Vercel action or secret change authorised. See §3a-EXECUTION-AUTHORISED; older dated resume notes historical.


> **CURRENT resume point (7 Nov 2026, v3.18):** PR #34 `f44ca2f` package v5; Claude's Issue #3 execution instructions revision 4 have the corrected launcher returning the exact nested VPS-0 exit code; ChatGPT independently validated 0/3/1 handling. No remaining identified launcher blocker; Gate A-1 is ready for the operator's distinct explicit authorisation. No merges, VPS changes, Vercel setting changes or broker use authorised yet. Upon approval: fresh VPS-0 + preflight, 24 controlled merges, VPS-0 again, HOLD before A-2. See §3a-FINAL-READY; lower dated resume notes are historical.


> **CURRENT resume point (7 Nov 2026, v3.17):** draft PR #34 at `f44ca2f`, VPS-0 rev5 busctl effective-guard validation independently reviewed; both script fingerprints and all 24 PR heads verified. Claude reports 30 offline VPS-0 tests. One launcher-only bug remains: prints a nonzero `vps0_exit` but outer launcher returns status 0; fix and check whole-wrapper exit before final operator execution approval. Alpaca PAPER keys reportedly regenerated; any VPS stored credential update and dashboard password rotation require separate authorisation. Vercel remains linked/as-is; no Gate A-1 merges or Gate A-2 approval. See §3a-LATEST-REVIEW.


> **CURRENT resume point (7 Nov 2026, v3.16):** Gate A-1 package revision 4 at PR #34 head `edcd713`, focused review PASS; VPS-0 exit 0 on OK / 3 on STOP; 24 pinned PR rows and merge driver unchanged. Vercel stays linked and unchanged (automatic builds are normal). Three operator scope decisions approved, but **no separate Gate A-1 execution authorisation**. Run fresh VPS-0 and GitHub preflight only within a separately authorised gate; stop before A-2. See §3a-NOW. All below resume points are historical.


> **CURRENT resume point (6 Nov 2026, v3.15):** operator approved C5b, #35 inclusion and #33 reviewed-head sequence; Vercel remains connected and unchanged, so ordinary automatic Production builds may occur on GitHub main merges. No actual Gate A-1 execution authorisation or VPS deployment. Security item: rotate the exposed historical paper API credentials and any reused password; do not mistake making GitHub private for credential revocation. See §3a-CURRENT; all below resume points are historical.


> **CURRENT resume point (6 Nov 2026, v3.14):** Gate A-0 closed; Gate A-1 VPS-0 rev 3 correction reviewed; 24/24 PR heads checked on GitHub; draft PR #34 `4ad8a93`. Pending four operator decisions and explicit merge permission. No merges, VPS changes, Vercel change or broker activation permitted. See §3a-LATEST; all older resume points below are historical.


- **This full handoff:** GitHub Issue #3, and this file in its isolated documentation branch.
- **Original canonical plan:** user-supplied 29 June 2026 canonical roadmap (uploaded 30.06.2026).
- **Research discussion and proposal:** https://github.com/mikemitakidis/algotrading/issues/2
- **WS2 review:** https://github.com/mikemitakidis/algotrading/pull/1
- **Working research report:** https://github.com/mikemitakidis/algotrading/blob/research/m21-autonomous-research-engine/docs/research/M21_autonomous_research_engine_proposal.md
- **GitHub source:** ROADMAP.md; MILESTONE_STATUS.md; docs/NEXT_WORK_REGISTER.md.

**SUPERSEDED historical resume point (v3.11):** **STOPPED** after the final engineering acceptance packet (`docs/reviews/M21_Final_Engineering_Acceptance_2026-10-29.md`, posted on Issue #3).

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
