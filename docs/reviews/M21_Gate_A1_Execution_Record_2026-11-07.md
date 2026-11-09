# Gate A-1 execution record (7 Nov 2026): CLOSED / PASS

- **Result:** GitHub `main` = `963bb893c81c2482a08d78a11b1aafe98f542b51`, tree
  `f04d1d65dd830894156425f5a870a50591a3a805`. This is exactly the reviewed final tree of the 24-merge plan (23 + #35).
- **VPS:** unchanged at `015d2ce`; Gate A-0 containment in place before and after.
  - VPS-0 before the merges: `vps0_exit=0` at 19:14:13Z.
  - VPS-0 after the merges: `vps0_exit=0` at 19:49:45Z.
- **Approvals and records (Issue #3):**
  - Gate A-1 approval: [comment 6711510167](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711510167).
  - VPS-0 before: [6711844958](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711844958).
  - Stop at step 5: [6711951855](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6711951855).
  - Independent review of the stop: [6712005085](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712005085).
  - Step-5 authorisation (Option A): [6712054723](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712054723).
  - 24 merges done: [6712104253](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712104253).
  - Independent verification of the 24 merges: [6712165676](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712165676).
  - Formal closure with the post-merge VPS-0: [6712228663](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712228663).
- **Tools:** the pinned merge script `gate_a1.sh` (sha256 `3c991a14…c8adc`) from the review package at PR #34 head
  `f44ca2f`. VPS-0 revision 5 (sha256 `bb5359b8…8478d`).

## What happened

1. **Preflight and the first run.**
   - The fresh preflight (19:19:31Z) passed for 24 merges.
   - The run (19:19:36Z) merged steps 1–4, each with its exact pinned tree.
   - At step 5 GitHub reported #15 `mergeable=false` (`dirty`). The script stopped as designed (exit 2): nothing was
     reverted and nothing retried.
2. **The cause.**
   - #15's head and `main` had **three merge bases** (`68ca7b4`, `6a8a690`, `0f3f9e6`), from the stacked branches.
   - A standard `git` merge-ort merge was clean. Its virtual merge base produced exactly the pinned tree `9a21419`.
   - A merge against any single base conflicted in 4 files.
   - GitHub's verdict matched a single-base evaluation.
3. **Option A (approved as a narrow exception).**
   - The two-parent merge commit was built locally with merge-ort, with no manual resolution. Parents: `fe93e9c`
     and `53948821`; tree `9a21419`.
   - It was pushed **without force** as a fast-forward (19:35:37Z). This ran after a pre-push check of `main` and
     #15's head.
   - GitHub then showed #15 `merged=true`, with `main`'s tree = `9a21419`.
4. **Resume.**
   - The unchanged script's preflight passed (19 to go).
   - Steps 6–24 then merged through the normal API (19:36:00Z–19:38:58Z), each with its pinned tree:
     `GATE A-1 DONE`, `gate_a1_exit=0`.
5. **Independent checks.**
   - Each of the 24 commits is on `main`'s first-parent chain from `80b4343`.
   - Each has the reviewed head as its second parent and its pinned tree.
   - GitHub shows each PR merged with that commit.
   - The checks were done by Claude and, separately, by ChatGPT.

## The 24 merge commits

| Step | PR | Merge commit | Reviewed head | Tree |
|---|---|---|---|---|
| 1 | #12| `28cbcd933517cce0436bfb35f7decd6ebb9707cb` | `47e9057` | `a31b018` |
| 2 | #1| `e5547bc418f2b9199ae3e00fe429742eba2adffa` | `0f3f9e6` | `9c57eb8` |
| 3 | #14| `1ed27b52e60cc0f6c0bc153ae47fcded4dfc325b` | `68ca7b4` | `1da598c` |
| 4 | #13| `fe93e9ca448f65c2c7082c921e01b75c51a7a6d7` | `6a8a690` | `a1aa8dd` |
| 5 | #15| `30957ffa695c8503360ff6a23baca1da88221f8b` | `5394882` | `9a21419` |
| 6 | #16| `469c69be741e153d3fc68e3c5dcda13a18fd5702` | `bf92678` | `ab74aab` |
| 7 | #17| `1934aee31717c164b696ea87e2ac357d7dc6782c` | `bf94f71` | `afa9322` |
| 8 | #21| `b09e14f40275d89313c205cd0591f76028fe68c5` | `fbfc815` | `6dac788` |
| 9 | #22| `f05c20be4f085a0501cb7d0cebc44134165671df` | `b4e9955` | `f08b79e` |
| 10 | #23| `36a4a3a7fa35bcd2243045d700be071a5fbe5d7f` | `04bf5ca` | `fa238da` |
| 11 | #18| `0d9a23b5f9033ae7449cac6ea5d68c6ca2459a9d` | `9012711` | `fc763db` |
| 12 | #19| `6f12bc57ea2198b363724117d804a0fb671a26e1` | `d7b8e66` | `c1ff0ca` |
| 13 | #20| `99ecb99e42635aa7c162bb8f26efb34cb0fdc254` | `4924844` | `2fe8a31` |
| 14 | #24| `e9eb12eb55312da82102f85a488afeef849b1d66` | `4b677bc` | `4959afc` |
| 15 | #26| `4e4a355f18ae609c9488fd5a10595d0c3fab0164` | `89bc874` | `1153db9` |
| 16 | #25| `b2c784791c60b45b93131745ad6080e0034f69eb` | `f05ed84` | `f1ff536` |
| 17 | #27| `8ec3ec33a690d57d8b49321089ae761f0362854b` | `c4a5ef1` | `b44a49c` |
| 18 | #28| `ba4083cfa9a90a7ab2ddc2c6a02114983dc67377` | `f366f05` | `fc4f456` |
| 19 | #29| `a848efbcaeb01b87eed227733078aa41d9807087` | `4cff094` | `ac37c94` |
| 20 | #30| `ac69fb6c57d317eef612b6f2a8ec0da172dc9308` | `14daa55` | `5006311` |
| 21 | #31| `8f489599ec114f7699323ff49d9dc28b56568709` | `d99f127` | `c7c320a` |
| 22 | #32| `30a8bf1e79cf15ef14c16dea8be03d614b2bc73c` | `a409b37` | `ce279c4` |
| 23 | #33| `f008b806ec580d6bd0e1c1a63cbea78dae63a750` | `6540e41` | `5b0f40b` |
| 24 | #35| `963bb893c81c2482a08d78a11b1aafe98f542b51` | `da56e12` | `f04d1d6` |

## Lessons kept for the next merge gate

- **Simulate merges the way GitHub evaluates them.** The preflight simulated each merge locally (clean). It did not
  check GitHub's `mergeable` for steps whose base was still a stacked branch. A future gate should flag any step with
  multiple merge bases where a single-base merge conflicts, and decide its handling in the review. Of the 24 steps,
  only #15 had that property; #27 had four bases, and each single-base merge was clean.
- The fail-closed design worked:
  - the stop happened before any unreviewed content could land;
  - the exception was approved and recorded before it ran;
  - the unchanged script resumed only after re-checking every earlier step.

## Not done by Gate A-1 (and still not authorised)

- No VPS deployment (Gate A-2).
- No service restart.
- No kill-switch or guard change.
- No broker connection.
- No credential change.
- No Vercel setting change. Vercel built each push to `main`, as accepted by the operator.
