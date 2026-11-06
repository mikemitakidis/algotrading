# M21 Gate A-1: Claude's engineering and safety review of the package

- **Reviewed:** the Gate A-1 package at PR #34 head `578af1e` (`docs/reviews/M21_Gate_A1_Review_Package_2026-11-06.md`), including optional PR #35.
- **Written:** 2026-11-06T18:29Z UTC (same in London).
- **Method:** everything re-derived from GitHub. PR refs were fetched fresh, the merges re-simulated, the full suite run on the exact final trees, the protected diffs read line by line, and the deployment records and API access checked.
- **Nothing merged, deployed or changed** on the VPS, Vercel or any broker.

## Verdict

- **At `578af1e`: BLOCK, on one necessary correction (F1).** Run from this Claude session, the merge script would stop at step 1 with 0 merges: safe, but it cannot run.
- **Corrected in package revision 2** (this commit): the correction, one hardening (F2), and full-suite evidence on the exact final trees. Merge order, heads and expected trees are unchanged.
- **With revision 2: PASS** for independent review and the operator's decisions.

## 1. What was verified

| Check | Result |
|---|---|
| 23 PR heads + #35 vs GitHub (full SHAs) | **24/24** match the script's pinned SHAs; all open, unmerged, draft, mergeable |
| Finish Line §7e.4 A3 table vs the package table vs the script rows | **23/23** agree (PR, head, expected tree) |
| Re-simulation from `main` `80b4343` with PR refs fetched on 6 Nov | conflict-free; final tree **`5b0f40b`**; with #35, **`f04d1d6`** |
| Expected trees distinct (the script's position and resume logic needs this) | 25/25 distinct (start + 24 steps); `TREE0` = tree of `80b4343` |
| GitHub's merge engine vs local `git merge` | the 8 GitHub-made merge commits on `main` (27 Oct) reproduce exactly with a local merge (**8/8**) |
| **Full suite on the exact final `main` trees** (Python 3.12.3, NumPy 1.26.4, C5a) | `5b0f40b` (commit `834a3d9`): **134 modules, 0 failed**. `f04d1d6` with #35 (`e1a9a56`): **135, 0 failed**. Same skips as before (m16 1, m17 2, m18 3, m20j 1; `test_m12` has no tests by design). |
| Integration tree vs final tree | the only differences are `test_m21_e2e_cross_lane.py` and its two G10 allow-list lines (integration-only). `6c82b1a`: 135/0; with #35, 136/0. **The gap is closed** by the row above. |
| Protected files (blob transitions vs Final Engineering Acceptance §14a) | `bot/scanner.py` `4317e57` → `08dc751` (#12) → `581f2a7` (#16); `alpaca_provider.py` `944f391` → `0d756dd`. Unchanged: `gates.py` `7c94181`, `ibkr_broker.py` `08640cd`, `dashboard/app.py` `564fc81`. #35: `gateway_watchdog.py` `6393afb` → `3b799e1`. |
| Runtime and deployment files | unchanged by all 24: `main.py`, `dashboard/app.py`, `bot/risk.py`, `bot/brokers/*`, `bot/database.py`, `bot/flywheel.py`, `bot/kill_switch.py`, `requirements.txt`, `sync.sh`, `deploy.sh`, `server_scripts/`, `.github/` |
| Merge script and VPS-0 | revision 2: **20** offline cases (`gate_a1.sh`) and **9** (VPS-0) pass. The real preflight passes, read-only. |

## 2. Protected-file review (C5b)

- **`bot/scanner.py`:**
  - The default path is unchanged: same bars, same `now_utc` from the wall clock, same decisions.
  - It adds one metadata key, `bar_asof`. `main.py` reads the scan metadata only via `.get('tfs_available' | 'tfs_list' | 'symbols_scanned')`, so the new key cannot break it.
  - The opt-in `bar_basis="completed"` needs an injected policy; without one the cycle returns no signals (fail closed).
  - The new import `bot.bar_completion` is stdlib-only (`dataclasses`, `typing`).
- **`alpaca_provider.py`:** passes `adjustment="raw"` to both `alpaca-py` `StockBarsRequest` calls. That is Alpaca's documented default.
- **`alpaca_corporate_actions.py` (new):** imported only by tests and `tools/paper_loop/m21_1extra_d2_3_scheduler.py`, never by the bot runtime. It makes read-only GETs through an injectable HTTP client.
- **#35:** no order, flatten, cancel or broker call is added; it only changes how the probe thread imports and creates its event loop.
- **Conclusion:** no new execution or order path. **Approve.**

## 3. Findings

| # | Kind | Finding | Status |
|---|---|---|---|
| **F1** | **Necessary correction** | This session's GitHub access has **no GraphQL**, and `gh pr ready` uses GraphQL. The revision-1 script exits at step 1 with 0 merges. Demonstrated with the stand-in GitHub CLI; confirmed by a real refusal (HTTP 403). The merge call (`PUT …/merge`) and the retarget (`PATCH …/pulls/{n}`) do pass through. Probed with a non-existent PR number, so nothing changed. | **Corrected in revision 2:** try `gh pr ready`, otherwise the proxy's REST route `POST …/pulls/{n}/ccr/ready_for_review`, then verify `draft=false` (STOP otherwise). A refused retarget is also a clean STOP now. New tests T17–T19. |
| F2 | Hardening | VPS-0 checked processes and containment, but not the VPS code itself. | **Included in revision 2:** VPS-0 also requires the VPS checkout to be `015d2ce`. It runs before step 1 **and after `GATE A-1 DONE`**, as direct evidence that nothing reached the VPS. 9 cases. |
| F3 | Note | A GitHub CLI error outside the script's checks (API unreachable) ends the run with exit 1 rather than `STOP`. | Documented in package §4. It is still a stop: no further merge; the preflight shows the position. |
| F4 | Residual, not A-1 | The cross-lane E2E test exists only on `m21/integration`; it will not be on `main`. | Follow-up: carry it into `main` after A-1, with its allow-list lines. |
| F5 | Residual, before A-2/OP3 | `adjustment="raw"` was meant to be verified once against the live Alpaca API (§14a, "VPS VERIFICATION REQUIRED"). It has not been done. | Not an A-1 item; nothing is deployed. Verify before the scanner runs on the merged code. |
| F6 | Risk, covered | If A-1 stops partway, `main` is at an intermediate tree, and Vercel deploys it. The VPS is unaffected. | Gate A-2 must not start unless A-1 is DONE. Finish Line A4 already pins the final tree. |
| F7 | No branch protection | Anyone with write access could push to `main` during the run. | Covered: every merge is pinned to the reviewed head (`sha=`), and `main`'s tree is checked before and after each merge; any foreign change is a STOP. Branch protection would not add safety for this run. Optional, after A-1 and the operator's setting: block force-push and deletion on `main`. |
| F8 | Hygiene, pre-existing | The IBKR **paper** account id appears 41 times in the public repo (code guards, reports, status files). The merges keep the count the same: #1 adds it to one paper-account guard constant and one test, and E7 removes two from status text. | Not an A-1 blocker. Track as a security-hygiene item (move to configuration). The value is not repeated here. |

## 4. Deployment triggers

- **Vercel:**
  - one **Production** deployment per push to `main`. Evidence: the 8 merge commits of 27 Oct produced exactly 8 Production deployments, all `success`;
  - one **Preview** per branch push (for example every PR #34 push);
  - A-1 makes 23 (24) pushes to `main`; retargeting a PR and marking it ready push nothing;
  - the Vercel builds come from this public repository, carry no VPS access and cannot deploy, restart or configure the VPS.
- **GitHub Actions:** the only workflow is `m21u4_europe_source_audit.yml` (`workflow_dispatch` only); none of the 24 PRs changes `.github/`.
- **Webhooks:** not listable from this session (the proxy refuses the path). Session 1 found nothing on the VPS that receives or acts on one: no daemon, timer, system cron, root cron entry, git hook or Actions runner.
- **Dashboard:** no git, pull or deploy route. It can only start or stop `main.py`, and that code is unchanged by A-1.
- **VPS:** `sync.sh` is the only pull path, and it was not running at A-0. VPS-0 re-checks this at run time, and with F2 confirms the VPS code is unchanged afterwards.

## 5. Recommended decisions

| Decision | Recommendation | Reasons |
|---|---|---|
| **D-A1-1** C5b | **Approve** the #12, #15, #16 and `_protected_baselines.py` transitions | The diffs match §14a exactly; the default path is unchanged; there is no new order or network path in the runtime; the guards pass on the final trees. The approval is recorded in the step 1, 5 and 6 merge commits and on Issue #3. The "PENDING" text is corrected in a text-only follow-up PR, because changing #12 or #16 now would void every downstream tree. |
| **D-A1-2** Vercel | **Accept** the 23 (24) Production deployments | This behaviour is proven (27 Oct: 8 merges, 8 successful deployments). The builds cannot reach the VPS and expose nothing that is not already in the public repo. Pausing or disconnecting adds two manual settings changes and no safety. Choose `paused` only if the Vercel site serves something that must not show intermediate states. |
| **D-A1-3** #35 | **Include as step 24**, provided the independent review covers #35 at exact head `da56e12` | The watchdog fix is required before OP2. It merges conflict-free, the full suite passes on `f04d1d6`, it adds no broker or order code, and the bot is contained by A-0. If it is not reviewed in this round, run the 23 steps and add step 24 later (`--with-35`; the preflight then shows only step 24). |
| **D-A1-4** #33 merge-day rule | **Option A:** merge #33 at its reviewed head, then bring the status current in PR #34 right after A-1 | It keeps the reviewed final tree `5b0f40b` and avoids an unreviewed mid-gate change. #33 only under-states the state for a while (it says "draft, not merged" and dates itself 29–30 Oct). It never over-states it. Option B needs a pause, a new head, a new tree and a script change. |

## 6. Canonical records check

| Record | State | Missing or outdated |
|---|---|---|
| Full canonical handoff **v3.12** (29 Oct), `docs/handoffs/Algo_Trading_Full_Canonical_Handoff_2026-10-26.md` | preserved on `docs/full-canonical-handoff-2026-10-26` (tip `7d19f39`, 5 Nov). **Not on `main`** (RD-5 pending). | No entries after 29 Oct: Gate A design (30 Oct), Gate B Sessions 1–2, Gate A-0, A18, the E07 fix and roadmap v3 live only in PR #34, Issue #3 and the Session 1 report. **Needs a v3.13 refresh** (docs-only). |
| **71 tasks** | **intact**: A01–A17, B01–B24, C01–C11, D01–D10, E01–E09 | **A18** is approved (RD-9, PR #35) but not yet in the register. **E07** is still unchecked although Gate A-0 fixed it on 6 Nov. |
| **M1–M23 roadmap** | all present in `ROADMAP.md` (`main` and PR #34); M24/M25 UNDEFINED (RD-3) | `main` still has the pre-v3 roadmap; v3.0 is a draft in PR #34 |
| Technical debt | handoff §4 E01–E09 + A17; `docs/NEXT_WORK_REGISTER.md` (June) on `main` | **`docs/PREVIOUS_MILESTONE_DEBT.md`** (5 Jul; D-1…D-5) exists only on the unmerged branch `m21-docs-debt-register`, and the handoff and roadmap v3 do not reference it. D-1 = C01 and D-2 ≈ A13, but **D-3** (IBKR paper account/equity reader not wired), **D-4** (backtest integrity audit) and **D-5** (M1–M4 runtime re-verification) have no explicit task. Carry them into the register as notes (no renumbering). |
| Progress since 26 Oct | Issue #3: 36 comments, continuous from 28 Oct to 6 Nov (including A-0 and A-1); `ROADMAP.md` R16 entries 1–12 and `MILESTONE_STATUS.md` 3.0.8 (PR #34) | The **Finish Line** (last changed 30 Oct) has no Session 2, A-0 or A18 entries, and its §6 checklist is not updated. Session 2's results exist only in PR #34 (report §5). None of PR #34 or the handoff branch is on `main`. |

None of these is a Gate A-1 blocker; they are documentation. Recommended: after A-1, one docs-only update covering the handoff v3.13, register notes (A18, E07, D-3…D-5), the Finish Line §6 checklist, and the status with the merge SHAs. Then decide the docs merge (RD-5, #34) as its own gate.

## 7. Next action

1. ChatGPT reviews this review and package revision 2 at the new PR #34 head (exact head in the PR record and Issue #3), including #35 at `da56e12`.
2. The operator decides D-A1-1…4 and approves Gate A-1, recording the approval on Issue #3. That comment is the `A1_APPROVAL_REF`.
3. Execution:
   1. VPS-0 (Claude Code on the VPS);
   2. the GitHub preflight;
   3. `gate_a1.sh execute [--with-35]`;
   4. VPS-0 again;
   5. the Issue #3 record;
   6. stop for review before Gate A-2.
