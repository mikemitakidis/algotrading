# M21 Gate A-1: controlled merge of the M21 PRs — review package

- **Prepared:** 2026-11-06T17:52Z UTC (same in London).
- **Status:** NOT approved, NOT executed. Nothing has been merged; `main` is `80b4343`.
- **Scope:** GitHub merges only. No VPS change, deployment, service action, broker connection, Vercel setting or CI change.
- **Revision 2:** 2026-11-06T18:29Z UTC, after Claude's own engineering review (`docs/reviews/M21_Gate_A1_Engineering_Review_2026-11-06.md`):
  - one necessary correction: the ready-for-review step now also works through the Claude Code GitHub proxy (finding F1);
  - one hardening: VPS-0 also checks the VPS checkout (F2);
  - the full suite now also runs on the exact final trees.

  The merge order, heads and expected trees are unchanged.
- **Revision 4:** 2026-11-06T19:48Z UTC, after the independent review of the execution instructions: VPS-0 now exits 0 on OK and 3 on STOP. The 19 offline cases assert the exit code as well as the output. `gate_a1.sh`, the merge order, heads and expected trees are unchanged.
- **Revision 3:** 2026-11-06T18:46Z UTC, after the independent review: VPS-0 now proves the Gate A-0 guard is loaded in the bot unit (Appendix B; 19 offline cases). `gate_a1.sh`, the merge order, heads and expected trees are unchanged. The Gate A-0 execution date is corrected to 5 Nov, about 19:31 UTC (the attestation time in the accepted run report; recorded on GitHub 6 Nov).
- **Design reused, not changed:** Finish Line §7e.4 (A0 preconditions, A2 Vercel, A3 merge order, A6 protected files), from `docs/handoffs/M21_Finish_Line_2026-10-28.md` on `docs/full-canonical-handoff-2026-10-26`. Gate A-0 (6 Nov) replaces §7e.4 A1 "pause". Gate A-2 (deployment) is a separate gate.

## 0. On one screen

- **What Gate A-1 does:** it makes 23 merge commits into `main`, in the A3 order. Each merge is pinned to the reviewed head SHA, and `main`'s tree is checked against the expected tree after every step. The final tree is **`5b0f40b`**. An optional step 24 adds #35 (A18, the watchdog fix), giving final tree `f04d1d6`.
- **Why merging is safe now:**
  - Gate A-0 is complete: the kill switch is active, and the bot unit is disabled with its start guard loaded and the allow-file absent.
  - The VPS auto-deploy daemon `sync.sh` was not running at A-0. A merge to `main` changes nothing on the VPS until a deliberate Gate A-2 deployment.
  - The VPS-0 check (§4) confirms this read-only, immediately before step 1.
- **What it does not do:**
  - no deployment, no dashboard or bot restart;
  - no E6 `dashboard/app.py` patch, no flag change, no Vercel setting change;
  - it does not merge the docs PRs (#34 and the canonical-handoff branch).
- **Verified on 2026-11-06:**
  - all 23 heads are unchanged;
  - the re-simulation matches the A3 table at **23/23** steps;
  - the protected-file changes are exactly the C5b set;
  - the full suite was run on the exact tested tree (§2.4);
  - the full suite passes on the exact final `main` trees: `5b0f40b` 134/0, `f04d1d6` (with #35) 135/0;
  - the merge script passes 20 offline cases;
  - the real GitHub preflight passes (read-only).
- **Decisions needed before execution (§3):**
  - D-A1-1: approve C5b;
  - D-A1-2: the Vercel choice;
  - D-A1-3: whether to include #35;
  - D-A1-4: the #33 merge-day rule.

## 1. Preconditions (Finish Line §7e.4 A0) and their status

| Precondition | Status |
|---|---|
| Gate B (Sessions 1 and 2) reviewed | **DONE**: Session 2 closed on 5 Nov |
| Containment before merging (§7e.4 A1) | **DONE**: Gate A-0 revision 6, executed 5 Nov, about 19:31 UTC (the attestation time in the accepted run report; recorded on GitHub 6 Nov), `gate_a0_exit=0` (Issue #3) |
| Exact-head reviews of #27–#33, including #30 E4 `14daa55` and #31 E5 `d99f127` | **part of this review** (the heads are unchanged since 30 Oct) |
| C5b protected-file approval (§7e.4 A6) | **NEEDED**: D-A1-1 |
| Vercel choice (§7e.4 A2) | **NEEDED**: D-A1-2 |
| E6 `app.py` patch not part of this gate | confirmed: `dashboard/app.py` is unchanged by all 23 merges (§2.3) |
| No auto-deploy reaches the VPS | verified at A-0 (no `sync.sh` process); **re-checked read-only by VPS-0 immediately before step 1** |
| Operator available during the run | recommended. Merges don't touch the VPS, so a US-market-hours restriction is not needed |

## 2. Evidence

### 2.1 PR heads (read on GitHub, 2026-11-06T17:52Z)
All 23 PRs, and #35, are open, draft, mergeable and at the heads below. These are the A3 heads, unchanged since 30 Oct.

### 2.2 Merge re-simulation from `main` `80b4343` (start tree `d2e473c`)

| Step | PR | Reviewed head | Base at review | Expected `main` tree (A3) |
|---|---|---|---|---|
| 1 | #12 | `47e9057` | `main` | `a31b018` |
| 2 | #1 | `0f3f9e6` | `main` | `9c57eb8` |
| 3 | #14 | `68ca7b4` | `m21.1/a01-scoring-truth` | `1da598c` |
| 4 | #13 | `6a8a690` | `main` | `a1aa8dd` |
| 5 | #15 | `5394882` | `m21/stack-base` | `9a21419` |
| 6 | #16 | `bf92678` | `m21/wp5-evidence` | `ab74aab` |
| 7 | #17 | `bf94f71` | `m21/wp6-completed-candles` | `afa9322` |
| 8 | #21 | `fbfc815` | `m21/wp7-ledger-wiring` | `6dac788` |
| 9 | #22 | `b4e9955` | `m21/wp8-templates` | `f08b79e` |
| 10 | #23 | `04bf5ca` | `m21/wp9-template-controls` | `fa238da` |
| 11 | #18 | `9012711` | `m21/stack-base` | `fc763db` |
| 12 | #19 | `d7b8e66` | `m21/stack-base` | `c1ff0ca` |
| 13 | #20 | `4924844` | `m21/wp12-economics` | `2fe8a31` |
| 14 | #24 | `4b677bc` | `m21/wp11-readiness` | `4959afc` |
| 15 | #26 | `89bc874` | `m21/wp13-walkforward` | `1153db9` |
| 16 | #25 | `f05ed84` | `m21/stack-base` | `f1ff536` |
| 17 | #27 | `c4a5ef1` | `m21/rc-base` | `b44a49c` |
| 18 | #28 | `f366f05` | `m21/rc-base` | `fc4f456` |
| 19 | #29 | `4cff094` | `m21/rc-base` | `ac37c94` |
| 20 | #30 | `14daa55` | `m21/rc-base` | `5006311` |
| 21 | #31 | `d99f127` | `m21/rc-base` | `c7c320a` |
| 22 | #32 | `a409b37` | `m21/rc-base` | `ce279c4` |
| 23 | #33 | `6540e41` | `m21/rc-base` | `5b0f40b` |
| 24 | #35 | `da56e12` | `m21/rc-base` | `f04d1d6` |

- **Simulated trees:** all 23 equal the A3 table, and the merges are conflict-free.
- **Step 24 (#35):** merges conflict-free on top. It adds or changes `bot/gateway_watchdog.py` and `_protected_baselines.py` (its ApprovedChange), `test_m21_a18_gw_watchdog.py`, the C5b guard test, and the G10 allow lists.

### 2.3 Files

The 23 merges change **123 files**.
- **Protected files changed:** only the C5b set:
  - `bot/scanner.py` (+43/−1, #12 metadata and #16 seam);
  - `bot/providers/alpaca_provider.py` (+8, #16 `adjustment=raw`);
  - new `bot/providers/alpaca_corporate_actions.py` (#15);
  - `_protected_baselines.py`.
- **Unchanged:**
  - the runtime: `main.py`, `dashboard/app.py`, `bot/risk.py`, `bot/brokers/*`, `bot/database.py`, `bot/flywheel.py`, `bot/kill_switch.py`, `bot/gateway_watchdog.py` (without #35);
  - the deployment and configuration files: `requirements.txt`, `sync.sh`, `deploy.sh`, `server_scripts/`, `.github/`, `vercel.json`.

### 2.4 Tests (Python 3.12.3, NumPy 1.26.4, C5a isolation, checkout outside `/tmp`)

| Tree tested | What it is | Result |
|---|---|---|
| `m21/integration` **`6c82b1a`** (tree `04d3231`) | the final tree `5b0f40b` + the cross-lane E2E test + one G10 allow-list line each in `test_m17_backtesting.py` and `test_m18_ml.py` (integration-only; no production file differs) | **full suite: 135 modules, 0 failed**, 1 without tests (`test_m12`, by design). Expected skips: m16 1, m17 2, m18 3, m20j 1, the same as the 30 Oct full run on `15bb236`. |
| `6c82b1a` + #35 → `ae803b1` (tree `20405d2`) | the same + step 24 (A18) | **full suite: 136 modules, 0 failed**, the same skips. Includes `test_m21_a18_gw_watchdog` and the C5b guard. |

| **Simulated final `main` (revision 2): merge commit `834a3d9`, tree `5b0f40b`** | exactly what `main` will be after step 23 (merges of the PR refs fetched from GitHub on 6 Nov) | **full suite: 134 modules, 0 failed**, the same skips |
| **Simulated final `main` with #35: `e1a9a56`, tree `f04d1d6`** | exactly what `main` will be after step 24 | **full suite: 135 modules, 0 failed**, the same skips |

On 30 Oct the full suite was claimed only for `15bb236`. It now covers the exact tested tree **and the exact final merged trees**. The only module in the integration run that is not on the final tree is `test_m21_e2e_cross_lane` (integration-only); carrying it into `main` is a follow-up.

### 2.5 Real GitHub preflight (read-only; `bash gate_a1.sh preflight --with-35`, 2026-11-06T17:52Z)

```
main: 80b4343db4a22413aa670efd32e24952d53e532e tree d2e473c124ef7e69b8cab0a11966d742e85d2097 = expected tree after step 0
repo: allow_merge_commit=true delete_branch_on_merge=false
step  1  #12  head 47e9057  base main                             draft=true  expected tree a31b018  OK
step  2  #1   head 0f3f9e6  base main                             draft=true  expected tree 9c57eb8  OK
step  3  #14  head 68ca7b4  base m21.1/a01-scoring-truth          draft=true  expected tree 1da598c  OK
step  4  #13  head 6a8a690  base main                             draft=true  expected tree a1aa8dd  OK
step  5  #15  head 5394882  base m21/stack-base                   draft=true  expected tree 9a21419  OK
step  6  #16  head bf92678  base m21/wp5-evidence                 draft=true  expected tree ab74aab  OK
step  7  #17  head bf94f71  base m21/wp6-completed-candles        draft=true  expected tree afa9322  OK
step  8  #21  head fbfc815  base m21/wp7-ledger-wiring            draft=true  expected tree 6dac788  OK
step  9  #22  head b4e9955  base m21/wp8-templates                draft=true  expected tree f08b79e  OK
step 10  #23  head 04bf5ca  base m21/wp9-template-controls        draft=true  expected tree fa238da  OK
step 11  #18  head 9012711  base m21/stack-base                   draft=true  expected tree fc763db  OK
step 12  #19  head d7b8e66  base m21/stack-base                   draft=true  expected tree c1ff0ca  OK
step 13  #20  head 4924844  base m21/wp12-economics               draft=true  expected tree 2fe8a31  OK
step 14  #24  head 4b677bc  base m21/wp11-readiness               draft=true  expected tree 4959afc  OK
step 15  #26  head 89bc874  base m21/wp13-walkforward             draft=true  expected tree 1153db9  OK
step 16  #25  head f05ed84  base m21/stack-base                   draft=true  expected tree f1ff536  OK
step 17  #27  head c4a5ef1  base m21/rc-base                      draft=true  expected tree b44a49c  OK
step 18  #28  head f366f05  base m21/rc-base                      draft=true  expected tree fc4f456  OK
step 19  #29  head 4cff094  base m21/rc-base                      draft=true  expected tree ac37c94  OK
step 20  #30  head 14daa55  base m21/rc-base                      draft=true  expected tree 5006311  OK
step 21  #31  head d99f127  base m21/rc-base                      draft=true  expected tree c7c320a  OK
step 22  #32  head a409b37  base m21/rc-base                      draft=true  expected tree ce279c4  OK
step 23  #33  head 6540e41  base m21/rc-base                      draft=true  expected tree 5b0f40b  OK
step 24  #35  head da56e12  base m21/rc-base                      draft=true  expected tree f04d1d6  OK
PREFLIGHT OK: 24 merge(s) to go (steps 1-24). Nothing was changed.
```

**Repository settings:**
- `main` has **no branch protection** (HTTP 404), so the script's own checks are the only guard;
- merge commits are allowed;
- `delete_branch_on_merge=false`, so head branches are kept and stacked bases are retargeted explicitly by the script.

## 3. Decisions for the operator

- **D-A1-1, C5b (§7e.4 A6, Final Engineering Acceptance §14a).** Approve the protected-file transitions:
  - #12: scan-cycle bar-as-of metadata, with no signal or decision change;
  - #16: the opt-in completed-bar seam (the default is unchanged);
  - #16: `adjustment="raw"`, Alpaca's documented default;
  - #15: the new corporate-actions provider;
  - `_protected_baselines.py`.

  **How the approval is recorded:**
  - the script writes the approval reference into the merge commit messages of steps 1 (#12), 5 (#15) and 6 (#16), and the step is also recorded on Issue #3;
  - the in-file note "PENDING independent review" for `bot/scanner.py` and `alpaca_provider.py` is replaced in a small follow-up PR after A-1, with text only and the guards re-run.

  Changing #12 or #16 now would change every downstream head and tree, and the reviewed table would be void.
- **D-A1-2, Vercel (§7e.4 A2).** Merging one by one creates **23 Vercel production deployments** of the website; with #35, 24. These cannot reach the VPS. The choices:
  - `accept`;
  - `paused`: an Ignored Build Step for the window, re-enabled afterwards;
  - `disconnected`.

  It is the operator's setting; Claude changes nothing. The script refuses to run without `A1_VERCEL_CHOICE` and records the choice in every merge commit.
- **D-A1-3, include #35 (A18) as step 24?** **Recommended: yes.**
  - The fix is needed before OP2, and it merges conflict-free.
  - The full suite passes with it (§2.4).
  - It touches no M21 PR file except the shared baselines and allow lists.
  - Its exact-head review (`da56e12`) becomes part of this review.

  If excluded, it follows later as its own merge.
- **D-A1-4, the #33 (E7) merge-day rule.** #33's text says that on merge day it is updated to the merged SHAs before it merges.
  - **Option A (recommended):** merge #33 at its reviewed head. Its own text states that it describes 29–30 Oct. PR #34, which is stacked on #33 and already supersedes its status block, is then updated with the 23 merge SHAs right after A-1. The final tree stays the reviewed `5b0f40b`, and there is no unreviewed change mid-gate.
  - **Option B (as designed in E7):** pause after step 22, commit the merged SHAs to #33's two files, review that two-file diff, then run step 23 with a recomputed tree (the script's step 23 row changes).

## 4. Execution procedure (after approval)

1. **VPS-0 (Claude Code on the VPS, read-only, immediately before step 1).** Expected: `VPS-0 OK`. Any `VPS-0 STOP`: do not start A-1.
2. **Preflight (GitHub, read-only):** `bash gate_a1.sh preflight [--with-35]`. Expected: `PREFLIGHT OK: 23 (or 24) merge(s) to go`.
3. **Execute:** `A1_APPROVAL_REF='<operator approval: date/time UTC, Issue #3 comment link>' A1_VERCEL_CHOICE=<accept|paused|disconnected> bash gate_a1.sh execute [--with-35]`. Claude runs it in this session, or the operator runs it on their own machine. In this session GitHub GraphQL is not available, so `gh pr ready` is refused. The script then uses the proxy's REST ready-for-review route and verifies the result (F1).

   For each step, the script:
   - checks that `main`'s tree is the previous expected tree;
   - checks that the PR head is the reviewed SHA;
   - retargets a stacked PR to `main`;
   - marks it ready for review;
   - waits for GitHub's mergeability;
   - merges with a merge commit pinned to the reviewed SHA (`sha=`);
   - checks that `main`'s tree equals the expected tree.
4. **Expected:** 23 (or 24) `step N #P merged as … OK` lines, then `GATE A-1 DONE: main <sha> tree 5b0f40bdf6618ebaccfba31195918885f4d3e379` (or `f04d1d65dd830894156425f5a870a50591a3a805` with #35).
5. **Stop conditions:** any of the following prints `STOP: …` with the current `main` (exit 2):
   - a moved head, an unexpected base or tree, or `main` moved outside the gate;
   - a PR that is not mergeable, a merge refused by GitHub, or a tree difference after a merge;
   - a missing approval reference or Vercel choice.

   On a STOP, nothing is reverted and no further merge is made. A re-run resumes from the step whose expected tree `main` has, and only after review and approval.

   A GitHub CLI error outside these checks (for example the API being unreachable) ends the run with exit 1 and the CLI's message. That is also a stop: no further merge is made, and the preflight shows where `main` is.
6. **Afterwards:** run **VPS-0 again**. It must be `VPS-0 OK` with `vps_head=015d2cea927b`, the direct evidence that nothing reached the VPS. Then record the merge SHAs and the final tree on Issue #3, update PR #34's status, and hold an independent review before Gate A-2. Gate A-2 must not start unless Gate A-1 is DONE; a partial `main` is never deployed.

**After A-1:**
- the VPS still runs `015d2ce`, unchanged;
- the bot stays contained by A-0;
- the GitHub PR file lists of retargeted stacked PRs (#27–#33) may show integration merges from `m21/rc-base`. The per-step tree check is the authority.

**Rollback:** nothing is deployed, so a rollback has no runtime effect. Reverting `main` is a separate PR (`git revert -m 1` of the merge commits in reverse order; never a force-push) and needs approval. With Vercel `accept`, the website redeploys.

## Appendix A: `gate_a1.sh`, revision 2 (sha256 `3c991a141e6a4e59941bffd40a205548bfca7d53a74b0cd85798a7d1c92c8adc`)

```bash
#!/usr/bin/env bash
# Gate A-1: the 23 M21 merges in the Finish Line §7e.4 A3 order (+ optional step 24: #35 A18). GitHub only; no VPS action.
#   bash gate_a1.sh preflight [--with-35]      read-only: checks main, every PR head/base/state and the repo settings
#   A1_APPROVAL_REF='<who/when/where approved>' A1_VERCEL_CHOICE=accept|paused|disconnected bash gate_a1.sh execute [--with-35]
# Merge commits only, each pinned to the reviewed head SHA. After every merge, main's tree must equal the expected tree.
# Any difference or error STOPS the run: nothing is reverted, no further merge is made. A re-run resumes from the step
# whose expected tree main currently has (only after review).
set -euo pipefail
REPO=mikemitakidis/algotrading
MAIN0=80b4343db4a22413aa670efd32e24952d53e532e
# step  PR  reviewed head SHA                          stacked base at review  expected main tree after the merge
STEPS='
1 12 47e905709919ed11f3ab31bc4be0e058df2fce8e main a31b0183cc2efb337bbdf034a7f4420cce08323f
2 1 0f3f9e6db860bb2359238c8b83283d0c93d76a4e main 9c57eb85402e8f50cc7efca6f0261ab8d3323bc6
3 14 68ca7b4750829364a0f4df05dd207e25265176f1 m21.1/a01-scoring-truth 1da598c61115487209f0b24b01536b711b4d4dda
4 13 6a8a690eea14a639836af3fbe48f563c83288d73 main a1aa8dd666fdb603efdd398b89b6730a700c62f2
5 15 53948821caacbeed91a62bb83ff399b2dcb974e8 m21/stack-base 9a21419ea8cb0925467cfd32edbcfa8e1264b739
6 16 bf92678683b7191f95879c864cdbe93deb38cdf2 m21/wp5-evidence ab74aab13106502fd7b959864f4f42286ca1a8a7
7 17 bf94f7121ee2ed6c2c1f0cfd0e164d75d0488a1f m21/wp6-completed-candles afa93221e9a4f3953bd0ce4f831378459b8c3a59
8 21 fbfc815a436e4b3fdb4c1c575599af480de4a082 m21/wp7-ledger-wiring 6dac7881611838170aa6c99e3e5ac628c3a83e4d
9 22 b4e9955c8fd3a65a5d7702070189f8fb0efac5c1 m21/wp8-templates f08b79e167b1cde91faa5b7a99b38627f69fd6fe
10 23 04bf5cac4b9508d44d70e7432c3e98d54996f59b m21/wp9-template-controls fa238daab683f45f2999f96295525f7ddb9e62de
11 18 9012711d221f960a5a29c1f759bffa39f0b86a3a m21/stack-base fc763db479d5a1524fbf1b62e0cf89abf6a6f33c
12 19 d7b8e6675feacf52969fc46ab3fb7b0e7ffac460 m21/stack-base c1ff0ca9b474d67983b30905f9eca4aa59f26e9b
13 20 4924844810d7870b8b51249d40285b770544c99e m21/wp12-economics 2fe8a31240cd01946a21fd61c4f3237d330f8da7
14 24 4b677bc1dd942f08bc97dc2dc0b62ba5b69ecdb5 m21/wp11-readiness 4959afc5e655441d5c9294e92ced69fa1f712a56
15 26 89bc8744e297b41a35a064d49457877e481aada1 m21/wp13-walkforward 1153db9a0f295a5db8bf1cb49eb93a7cb88dacf8
16 25 f05ed84dc65188f186357dc658b7f4170217bfb4 m21/stack-base f1ff536e62e7c6479d07d7f404db49c4cce342e8
17 27 c4a5ef12175aea5032761891ef3869d139d0e3d5 m21/rc-base b44a49ca800a5a9492199db0b99320c1d013cee5
18 28 f366f0535de7cb5341c5a11e9d65e0f4a4f0c2b6 m21/rc-base fc4f4567d227d2ac53d4d9156437b270fd4292e0
19 29 4cff0945c320bcbd4203d1587efc218b258f86f7 m21/rc-base ac37c94883cde181d96b8b3d057c5d38a4492e7a
20 30 14daa55c0d7a30cef9316c4323210b988293a118 m21/rc-base 5006311e9fb26ec894f5665c9434afc137622a61
21 31 d99f127fc65d6f3b809c1159287bcfde16e89f3e m21/rc-base c7c320a1c54cc7a4405f49a559bd3197f6afa613
22 32 a409b37b2a18c021cde57157609b1ff69835c09d m21/rc-base ce279c4876f6692f69d38910d277fe80952d8595
23 33 6540e4136f2758c5ecbe2cdbe758019b5090fbee m21/rc-base 5b0f40bdf6618ebaccfba31195918885f4d3e379
24 35 da56e12a9e9baec8634b618cb936be1f40a84a78 m21/rc-base f04d1d65dd830894156425f5a870a50591a3a805
'
TREE0=d2e473c124ef7e69b8cab0a11966d742e85d2097
MODE=${1:-}; WITH35=${2:-}
case "$MODE" in preflight|execute) ;; *) echo "usage: gate_a1.sh preflight|execute [--with-35]"; exit 64;; esac
case "$WITH35" in ""|--with-35) ;; *) echo "unknown option $WITH35"; exit 64;; esac
LAST=23; [ "$WITH35" = --with-35 ] && LAST=24
stop() { echo "STOP: $*"; echo "main now: $(main_sha) tree $(main_tree)"; echo "Nothing was reverted and no further merge was made. Send this output."; exit 2; }
pr()        { gh api "repos/$REPO/pulls/$1" --jq "$2"; }
main_sha()  { gh api "repos/$REPO/branches/main" --jq .commit.sha; }
main_tree() { gh api "repos/$REPO/branches/main" --jq .commit.commit.tree.sha; }
row()       { awk -v s="$1" '$1==s' <<< "$STEPS"; }
tree_of()   { if [ "$1" = 0 ]; then echo "$TREE0"; else row "$1" | awk '{print $5}'; fi; }

# Where is main? It must have exactly one expected tree (step k); otherwise stop.
MT=$(main_tree); K=-1
for s in $(seq 0 $LAST); do [ "$(tree_of $s)" = "$MT" ] && K=$s; done
[ "$K" -ge 0 ] || stop "main tree $MT is not one of the expected step trees (main moved outside Gate A-1)"
[ "$K" -gt 0 ] || [ "$(main_sha)" = "$MAIN0" ] || stop "main has the start tree but is not $MAIN0"
echo "main: $(main_sha) tree $MT = expected tree after step $K"
for s in $(seq 1 $K); do read -r _ n h _ _ <<< "$(row $s)"; [ "$(pr $n .merged)" = true ] || stop "main is at step $K but #$n (step $s) is not merged"; done

# Repo settings that would change what a merge does.
[ "$(gh api repos/$REPO --jq .allow_merge_commit)" = true ] || stop "merge commits are not allowed on the repository"
echo "repo: allow_merge_commit=true delete_branch_on_merge=$(gh api repos/$REPO --jq .delete_branch_on_merge)"

# Every remaining PR: open, unmerged, at its reviewed head, base main or its reviewed stacked base.
for s in $(seq $((K+1)) $LAST); do
  read -r _ n h b t <<< "$(row $s)"
  st=$(pr $n '"\(.state) \(.merged) \(.head.sha) \(.base.ref) \(.draft)"'); read -r state merged head base draft <<< "$st"
  [ "$state" = open ] && [ "$merged" = false ] || stop "step $s #$n is $state/merged=$merged"
  [ "$head" = "$h" ] || stop "step $s #$n head moved: $head (reviewed $h)"
  [ "$base" = main ] || [ "$base" = "$b" ] || stop "step $s #$n base is $base (reviewed $b or main)"
  printf 'step %2s  #%-3s head %s  base %-32s draft=%s  expected tree %s  OK\n' $s $n ${h:0:7} $base $draft ${t:0:7}
done
if [ "$MODE" = preflight ]; then echo "PREFLIGHT OK: $((LAST-K)) merge(s) to go (steps $((K+1))-$LAST). Nothing was changed."; exit 0; fi

# EXECUTE: explicit approval reference and Vercel choice required.
[ -n "${A1_APPROVAL_REF:-}" ] || stop "A1_APPROVAL_REF is not set (record who approved Gate A-1, when, and where)"
case "${A1_VERCEL_CHOICE:-}" in accept|paused|disconnected) ;; *) stop "A1_VERCEL_CHOICE must be accept, paused or disconnected";; esac
for s in $(seq $((K+1)) $LAST); do
  read -r _ n h b t <<< "$(row $s)"
  prev=$(tree_of $((s-1)))
  [ "$(main_tree)" = "$prev" ] || stop "before step $s: main tree is not the expected $prev"
  [ "$(pr $n .head.sha)" = "$h" ] || stop "step $s #$n head moved"
  if [ "$(pr $n .base.ref)" != main ]; then gh api -X PATCH "repos/$REPO/pulls/$n" -f base=main --jq .base.ref >/dev/null 2>&1 || true; [ "$(pr $n .base.ref)" = main ] || stop "step $s #$n could not be retargeted to main"; fi
  # Ready for review: 'gh pr ready' (GraphQL) where available; otherwise the REST route of the Claude Code GitHub proxy
  # (GraphQL is not available there). The result is verified either way.
  if [ "$(pr $n .draft)" = true ]; then gh pr ready "$n" -R "$REPO" >/dev/null 2>&1 || gh api -X POST "repos/$REPO/pulls/$n/ccr/ready_for_review" >/dev/null 2>&1 || true; [ "$(pr $n .draft)" = false ] || stop "step $s #$n is still a draft (could not mark it ready for review)"; fi
  m=null; for _ in 1 2 3 4 5 6 7 8 9 10; do m=$(pr $n .mergeable); [ "$m" != null ] && break; sleep 3; done
  [ "$m" = true ] || stop "step $s #$n is not mergeable (mergeable=$m)"
  c5b=""; case $n in 12|15|16|35) c5b=" C5b: the protected-file change in this PR is approved under this Gate A-1 approval (Finish Line 7e.4 A6).";; esac
  title="Merge pull request #$n (Gate A-1 step $s/$LAST)"
  msg="Gate A-1 step $s/$LAST, Finish Line 7e.4 A3 order. Reviewed head $h; expected main tree after this merge $t. Approval: $A1_APPROVAL_REF. Vercel production builds: $A1_VERCEL_CHOICE.$c5b"
  res=$(gh api -X PUT "repos/$REPO/pulls/$n/merge" -f merge_method=merge -f sha="$h" -f commit_title="$title" -f commit_message="$msg" --jq .merged 2>&1) || stop "step $s #$n merge refused: $res"
  [ "$res" = true ] || stop "step $s #$n merge not confirmed: $res"
  nt=""; for _ in 1 2 3 4 5 6 7 8 9 10; do nt=$(main_tree); [ "$nt" != "$prev" ] && break; sleep 3; done
  [ "$nt" = "$t" ] || stop "TREE DIFFERS after step $s #$n: main tree $nt, expected $t"
  printf 'step %2s  #%-3s merged as %s  main tree %s  OK\n' $s $n "$(main_sha | cut -c1-7)" "${t:0:7}"
done
echo "GATE A-1 DONE: main $(main_sha) tree $(main_tree) (steps 1-$LAST)."
```

## Appendix B: VPS-0, revision 4 (sha256 `2b90968deb51ccfa09caa4d22a2142a1f59e758b7ea7499c684395b5d9cdc750`)

Run it before step 1 and again after `GATE A-1 DONE`. The `SCAN` code is byte-identical to the approved Gate A-0 revision 6 process scan.
- Revision 2 also requires the VPS checkout to be `015d2ce`.
- **Revision 4:** **exit status 0 on `VPS-0 OK`, 3 on `VPS-0 STOP`**. Revision 3 printed STOP but exited 0, so a calling `bash -e` or a script could not detect the failure.
- **Revision 3** requires the Gate A-0 guard to be part of the bot unit systemd has **loaded**: `gate-a0.conf` is in `DropInPaths`; the exact line `ConditionPathExists=/var/lib/algo-trader/ALLOW_BOT_START` is in `systemctl cat`; `NeedDaemonReload=no`, so the files on disk are the loaded ones; and the boot link is absent (including a dangling link).
- A standalone `systemd-analyze condition` that evaluates false is not proof that the unit enforces it.

```bash
bash -u <<'VPS0'
# Gate A-1 VPS-0: read-only. Run on the VPS immediately before step 1, and again after GATE A-1 DONE. Changes nothing.
SCAN='import os, sys
try:
    me = os.getpid(); skip = set(); p = me
    while p > 1:   # this scan and the shells running this block are never the bot
        skip.add(p)
        with open("/proc/%d/status" % p) as f:
            p = int([l.split()[1] for l in f if l.startswith("PPid:")][0])
    pids = [d for d in os.listdir("/proc") if d.isdigit()]
    seen, hits = 0, []
    for d in pids:
        try:
            with open("/proc/%s/cmdline" % d, "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except (FileNotFoundError, ProcessLookupError):
            continue   # exited during the scan
        seen += 1
        if int(d) in skip:
            continue
        names = {os.path.basename(w.strip("\"`;&|()" + chr(39))) for a in argv for w in a.split()}
        if names & {"main.py", "sync.sh"}:
            hits.append("bot process: %s %s" % (d, " ".join(argv).strip()[:200]))
    if str(me) not in pids or seen < 2:
        raise RuntimeError("process list unreadable")
except Exception as e:
    print("process scan FAILED: %r" % (e,)); sys.exit(2)
print("\n".join(hits) if hits else "processes: no main.py or sync.sh (%d checked)" % seen)
sys.exit(3 if hits else 0)'
echo "== VPS-0 read-only pre-merge check at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
P=$(python3 -I -c "$SCAN" 2>&1); rc=$?; echo "$P"
KS=$(python3 -I -c 'import json; print(json.load(open("/opt/algo-trader/data/kill_switch.json")).get("active"))' 2>&1)
EN=$(systemctl is-enabled algo-trader.service 2>&1); AC=$(systemctl is-active algo-trader.service 2>&1)
if systemd-analyze condition 'ConditionPathExists=/var/lib/algo-trader/ALLOW_BOT_START' >/dev/null 2>&1; then G=PASSES; else G=FALSE; fi
if [ -e /var/lib/algo-trader/ALLOW_BOT_START ]; then AL=PRESENT; else AL=absent; fi
HD=$(git -C /opt/algo-trader rev-parse HEAD 2>&1)
# The Gate A-0 guard must be part of the bot unit systemd has LOADED, not only true as a standalone condition:
DIP=$(systemctl show -p DropInPaths --value algo-trader.service 2>&1)
case " $DIP " in *" /etc/systemd/system/algo-trader.service.d/gate-a0.conf "*) DL=loaded;; *) DL=NOT_LOADED;; esac
GL=$(systemctl cat algo-trader.service 2>/dev/null | grep -cxF 'ConditionPathExists=/var/lib/algo-trader/ALLOW_BOT_START')
NDR=$(systemctl show -p NeedDaemonReload --value algo-trader.service 2>&1)
if [ -e /etc/systemd/system/multi-user.target.wants/algo-trader.service ] || [ -L /etc/systemd/system/multi-user.target.wants/algo-trader.service ]; then BL=PRESENT; else BL=absent; fi
echo "kill_switch_active=$KS bot_unit=$EN/$AC guard_condition=$G allow_file=$AL vps_head=${HD:0:12}"
echo "guard_drop_in=$DL guard_line_in_loaded_unit=$GL need_daemon_reload=$NDR boot_link=$BL"
if [ "$rc" = 0 ] && [ "$KS" = True ] && [ "$EN" = disabled ] && [ "$AC" = inactive ] && [ "$G" = FALSE ] && [ "$AL" = absent ] && [ "$HD" = 015d2cea927b9b4db3ee65604e0b4ebb58211e61 ] \
   && [ "$DL" = loaded ] && [ "$GL" -ge 1 ] 2>/dev/null && [ "$NDR" = no ] && [ "$BL" = absent ]; then
  echo "VPS-0 OK: no main.py or sync.sh process, Gate A-0 containment in place (guard loaded in the bot unit), VPS code still 015d2ce; a merge to GitHub main cannot reach the VPS."
  exit 0
else
  echo "VPS-0 STOP: do not start Gate A-1. Send this output."
  exit 3
fi
VPS0
```

## Appendix C: offline tests

**`gate_a1.sh`, 20 cases (revision 2).** The tests use a GitHub CLI stand-in that keeps PR state and a local `main` and performs real `git` merges.

| Case | Result |
|---|---|
| T1 preflight at the start | `PREFLIGHT OK: 23 merge(s)`, nothing changed |
| T2/T3/T4: no approval reference, an invalid Vercel choice, a bad mode | refused, 0 merges |
| T5a a head moved before the start | STOP before **any** merge (all heads are checked first) |
| T5b a head moves mid-run (#17 after 6 merges) | STOP at step 7, before merging it; `main` at the step-6 tree |
| T6/T7 preflight and resume after review | finds step 6; merges 7–23; final tree `5b0f40b` |
| T7 merge-commit messages | all 23 carry the approval reference; the C5b note is on exactly #12, #15 and #16 |
| T8 re-run after completion | nothing to do |
| T9/T10 `--with-35` | only step 24 left; final tree `f04d1d6`; C5b note on #35 |
| T11/T12 tree differs after step 10 | STOP right after it; the preflight then refuses (`main` matches no expected tree) |
| T13 GitHub refuses the merge at step 3 | STOP; `main` at the step-2 tree |
| T14 a PR not mergeable at step 5 | STOP before merging it |
| T15 `main` moved outside the gate | STOP |
| T16 full run in one go | `GATE A-1 DONE`, tree `5b0f40b`; 23 first-parent merge commits |
| T17 no GraphQL (the Claude Code proxy): `gh pr ready` refused | the proxy's ready route is used; full run, `GATE A-1 DONE`, tree `5b0f40b` |
| T18 no way to mark a PR ready | STOP at step 1 before any merge ("still a draft") |
| T19 retarget refused at step 3 | STOP with a message (not a raw exit); `main` at the step-2 tree |
| (revision 1 script, proxy without GraphQL) | exit 1 at step 1 with 0 merges: safe, but it could not run from this session. This is F1. |

**VPS-0, 19 cases (revision 4; every case asserts both the verdict and the exit code: OK = 0, STOP = 3).** The stub `systemctl` is the one from the Gate A-0 offline tests, which models drop-ins, `daemon-reload`, `NeedDaemonReload` and `systemctl cat`; `systemd-analyze` 255 is real. Every case starts from the state that the exact approved Gate A-0 revision 6 block (downloaded and hash-verified, `86f9f752…`) produces, then changes one thing.
- **P0, the post-A-0 state:** OK (revision 2 also OK).
- **N1–N9, each STOP:**
  - N1: `gate-a0.conf` removed and reloaded;
  - N2: the drop-in on disk but not loaded;
  - N3: the guard line changed;
  - N3b: the guard line commented out;
  - N4: `NeedDaemonReload=yes`;
  - N5: the boot link back;
  - N6: a dangling boot link;
  - N7: the guard line only in a different drop-in;
  - N8: `systemctl show` fails;
  - N9: `systemctl cat` fails.

  **Revision 2 said OK in N1–N7**: that is the gap the reviewer found.
- **Exit-code defect demonstrated:** in the N1 state, revision 3 prints STOP and exits 0; revision 4 exits 3. Result: **19 passed, 0 failed**.
- **E1–E8, re-run, each STOP:** the earlier cases (the VPS checkout moved, `sync.sh` running, `python3 -u main.py` running, the kill switch released, the unit re-enabled, the unit active, the allow-file present, the kill-switch file missing).
- **Read-only:** in P0, VPS-0's only `systemctl` calls are `cat`, `is-active`, `is-enabled` and `show -p DropInPaths|NeedDaemonReload`.

The earlier revision-2 list (STOP on its own for each of: the VPS checkout moved; `sync.sh` running; `python3 -u main.py` running; the kill switch released; the unit re-enabled; the unit active; the allow-file present; the kill-switch file missing) is covered by E1–E8.

<details><summary>Test harness (stand-in GitHub CLI, reset, cases)</summary>

```python
import json, os, subprocess, sys
D = os.environ["STUB_DIR"]; R = f"{D}/state"
prs = json.load(open(f"{D}/prs.json")); faults = json.load(open(f"{D}/faults.json")) if os.path.exists(f"{D}/faults.json") else {}
def on(kind, n):   # a fault "N" fires always; "N@k" fires once at least k PRs are merged (i.e. mid-run)
    v = str(faults.get(kind, "")); pr_, _, k = v.partition("@")
    return pr_ == n and (not k or sum(p["merged"] for p in prs.values()) >= int(k))
def save(): json.dump(prs, open(f"{D}/prs.json", "w"))
def git(*a): return subprocess.run(["git", "-C", R, *a], check=True, capture_output=True, text=True).stdout.strip()
def out(obj, jq):
    s = json.dumps(obj)
    if jq: s = subprocess.run(["jq", "-r", jq], input=s, capture_output=True, text=True, check=True).stdout.rstrip("\n")
    print(s)
def err(msg, code): sys.stderr.write(f"gh: {msg} (HTTP {code})\n"); sys.exit(1)
a = sys.argv[1:]
if a[:2] == ["pr", "ready"]:
    if os.environ.get("STUB_NO_GRAPHQL"): err("GitHub GraphQL is not available from Claude Code sessions", 403)
    prs[a[2]]["draft"] = False; save(); sys.exit(0)
assert a[0] == "api"; a = a[1:]
method, fields, jq, path = "GET", {}, None, None
i = 0
while i < len(a):
    if a[i] == "-X": method = a[i+1]; i += 2
    elif a[i] == "-f": k, v = a[i+1].split("=", 1); fields[k] = v; i += 2
    elif a[i] == "--jq": jq = a[i+1]; i += 2
    else: path = a[i]; i += 1
p = path.split("/")
if p[-2:] == ["branches", "main"]:
    out({"commit": {"sha": git("rev-parse", "main"), "commit": {"tree": {"sha": git("rev-parse", "main^{tree}")}}}}, jq)
elif len(p) == 3 and p[0] == "repos":
    out({"allow_merge_commit": True, "delete_branch_on_merge": False}, jq)
elif p[3] == "pulls":
    n = p[4]; pr = prs[n]
    if len(p) == 5 and method == "GET":
        head = "d" * 40 if on("head_moved", n) else pr["head"]
        out({"number": int(n), "state": pr["state"], "merged": pr["merged"], "draft": pr["draft"], "head": {"sha": head},
             "base": {"ref": pr["base"]}, "mergeable": (False if on("mergeable_false", n) else (None if pr["merged"] else True))}, jq)
    elif len(p) == 5 and method == "PATCH":
        if on("patch_fail", n): err("Validation Failed", 422)
        pr["base"] = fields["base"]; save(); out({"base": {"ref": pr["base"]}}, jq)
    elif p[5:] == ["ccr", "ready_for_review"] and method == "POST":
        if not os.environ.get("STUB_NO_GRAPHQL") or os.environ.get("STUB_NO_READY"): err("Not Found", 404)
        pr["draft"] = False; save(); out({"draft": False}, jq)
    elif p[5:] == ["merge"] and method == "PUT":
        if pr["draft"]: err("Pull Request is still a draft", 405)
        if fields.get("sha") != pr["head"]: err("Head branch was modified. Review and try the merge again.", 409)
        if on("merge_refused", n): err("Base branch was modified. Review and try the merge again.", 405)
        assert fields.get("merge_method") == "merge"
        git("checkout", "-q", "--detach", "main")
        git("merge", "-q", "--no-ff", "-m", fields["commit_title"] + "\n\n" + fields["commit_message"], pr["head"])
        if on("tree_diff", n):
            open(f"{R}/UNEXPECTED.txt", "w").write("x\n"); git("add", "UNEXPECTED.txt"); git("commit", "-q", "--amend", "--no-edit")
        git("update-ref", "refs/heads/main", "HEAD")
        pr["merged"] = True; pr["state"] = "closed"; save()
        with open(f"{D}/merge_messages.log", "a") as f: f.write(json.dumps({"pr": n, "title": fields["commit_title"], "msg": fields["commit_message"]}) + "\n")
        out({"merged": True, "sha": git("rev-parse", "main")}, jq)
    else: err("not found", 404)
else: err("not found", 404)
```

```bash
# fresh stub GitHub: main at 80b4343, every PR open/draft at its reviewed head and stacked base
D=$STUB_DIR; rm -rf $D; mkdir -p $D
git clone -q --shared --no-checkout /home/claude/algotrading $D/state
git -C $D/state update-ref refs/heads/main 80b4343db4a22413aa670efd32e24952d53e532e
git -C $D/state config user.name stub; git -C $D/state config user.email stub@x
python3 -c 'import json,sys; rows=[l.split() for l in open(sys.argv[1]) if l.strip() and not l.startswith("0 ")]; json.dump({r[1]:{"head":r[2],"base":r[3],"draft":True,"merged":False,"state":"open"} for r in rows}, open(sys.argv[2],"w"))' /tmp/claude-0/-home-claude/09235587-458f-5ea0-b28e-ee7bedbe67c2/scratchpad/a1_steps.txt $D/prs.json
```

```bash
SP=/tmp/claude-0/-home-claude/09235587-458f-5ea0-b28e-ee7bedbe67c2/scratchpad
export STUB_DIR=$SP/a1/stubstate PATH=$SP/a1/stub:$PATH
G=$SP/a1/gate_a1.sh
APP='operator approval TEST, Issue #3 comment TEST'
r() { echo "== $1"; shift; ( "$@" ) > $SP/a1/out.txt 2>&1; echo "   rc=$? | $(grep -E 'STOP|PREFLIGHT OK|GATE A-1 DONE|usage' $SP/a1/out.txt | head -1 | cut -c1-150)"; echo "   main tree $(git -C $STUB_DIR/state rev-parse --short=7 'main^{tree}'); merged PRs: $(python3 -c 'import json,os;print(sum(p["merged"] for p in json.load(open(os.environ["STUB_DIR"]+"/prs.json")).values()))')"; }
fault() { echo "$1" > $STUB_DIR/faults.json; }
bash $SP/a1/stub/reset.sh; r "T1 preflight at the start (read-only)" bash $G preflight; grep -c "  OK$" $SP/a1/out.txt | sed 's/^/   steps checked: /'
r "T2 execute without A1_APPROVAL_REF (must refuse, nothing merged)" env A1_VERCEL_CHOICE=accept bash $G execute
r "T3 execute with an invalid Vercel choice (must refuse)" env A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=maybe bash $G execute
r "T4 bad mode" bash $G merge-all
fault '{"head_moved": "17"}'; r "T5a a head already moved before the start (#17): STOP before ANY merge" env A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=accept bash $G execute
fault '{"head_moved": "17@6"}'; r "T5b #17 head moves mid-run, after 6 merges: STOP at step 7, before merging it" env A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=accept bash $G execute
fault '{}'; r "T6 preflight after the stop finds main at step 6" bash $G preflight; grep -m1 "^main:" $SP/a1/out.txt | sed 's/^/   /'
r "T7 resume (after review): execute continues from step 7 to 23" env A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=paused bash $G execute; grep -c "merged as" $SP/a1/out.txt | sed 's/^/   merged in this run: /'
echo "   final tree: $(git -C $STUB_DIR/state rev-parse 'main^{tree}') (expected 5b0f40bdf6618ebaccfba31195918885f4d3e379)"
python3 - <<'PY'
import json, os
L = [json.loads(l) for l in open(os.environ["STUB_DIR"] + "/merge_messages.log")]
print("   merge commits:", len(L), "| all carry the approval ref:", all("operator approval TEST" in x["msg"] for x in L),
      "| C5b note on exactly #12, #15, #16:", sorted(x["pr"] for x in L if "C5b" in x["msg"]) == ["12", "15", "16"])
PY
r "T8 execute again after completion: nothing left to do" env A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=accept bash $G execute
r "T9 preflight --with-35 after completion: only step 24 left" bash $G preflight --with-35
r "T10 execute --with-35: step 24 (#35)" env A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=accept bash $G execute --with-35; echo "   final tree $(git -C $STUB_DIR/state rev-parse --short=7 'main^{tree}') (expected f04d1d6); C5b note on #35: $(tail -1 $STUB_DIR/merge_messages.log | grep -c C5b)"
bash $SP/a1/stub/reset.sh; fault '{"tree_diff": "23"}'; r "T11 merged tree differs at step 10 (#23): STOP right after it" env A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=accept bash $G execute
r "T12 preflight after a tree difference: main matches no expected tree" bash $G preflight
bash $SP/a1/stub/reset.sh; fault '{"merge_refused": "14"}'; r "T13 GitHub refuses the merge at step 3 (#14)" env A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=accept bash $G execute
bash $SP/a1/stub/reset.sh; fault '{"mergeable_false": "15"}'; r "T14 #15 not mergeable at step 5: STOP before merging" env A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=accept bash $G execute
bash $SP/a1/stub/reset.sh; fault '{}'; git -C $STUB_DIR/state update-ref refs/heads/main 6c82b1a; r "T15 main moved outside the gate before the start" bash $G preflight
bash $SP/a1/stub/reset.sh; r "T16 full run in one go (23 steps)" env A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=accept bash $G execute; echo "   final tree $(git -C $STUB_DIR/state rev-parse --short=7 'main^{tree}'); merge commits on main: $(git -C $STUB_DIR/state rev-list --merges --count 80b4343..main) (first-parent: $(git -C $STUB_DIR/state rev-list --first-parent --count 80b4343..main))"
bash $SP/a1/stub/reset.sh; fault '{}'; r "T17 no GraphQL (Claude Code proxy): 'gh pr ready' refused, proxy ready route used; full run" env STUB_NO_GRAPHQL=1 A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=accept bash $G execute; echo "   final tree $(git -C $STUB_DIR/state rev-parse --short=7 'main^{tree}')"
bash $SP/a1/stub/reset.sh; r "T18 no way to mark ready: STOP before any merge" env STUB_NO_GRAPHQL=1 STUB_NO_READY=1 A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=accept bash $G execute
bash $SP/a1/stub/reset.sh; fault '{"patch_fail": "14"}'; r "T19 retarget of #14 refused at step 3: STOP (no raw exit)" env A1_APPROVAL_REF="$APP" A1_VERCEL_CHOICE=accept bash $G execute
rm -rf $STUB_DIR
```

</details>
