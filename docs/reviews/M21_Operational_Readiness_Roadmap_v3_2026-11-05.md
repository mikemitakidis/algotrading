# M21 operational readiness + roadmap v3.0: consolidated report for independent review

**For independent review (ChatGPT).**

- **Prepared by:** Claude (Claude Code session).
- **Written:** 2026-11-05T12:04Z UTC (12:04 GMT Europe/London).
- **Revision 2:** 2026-11-05T15:24Z UTC (same in Europe/London, GMT). Adds the H1/P1/P2 evidence, the `gw-watchdog` analysis and the corrections to Gate A-0.
- **Revision 3:** 2026-11-05T16:13Z UTC (same in London). Records the operator decisions of 15:48 UTC (§0), the A18 fix (PR #35), the corrected Gate A-0 procedure (§4) and the approved Session 2 command (§5). Roadmap decision ids are renamed `RD-n` (§0).
- **Revision 4:** 2026-11-05T17:37Z UTC (same in London). Records the Session 2 results (§5, CLOSED). Gate A-0 gets an IBKR position/working-order attestation precondition, the `.env`-backup permission fix (A-0s, register task E07) and a completed containment claim (§4).
- **GitHub `main`:** `80b4343db4a22413aa670efd32e24952d53e532e` [GH].
- **VPS deployed:** `015d2cea927b9b4db3ee65604e0b4ebb58211e61` [VPS-R].
- **Roadmap:** v3.0 (DRAFT); status 3.0.4.
- **PR:** #34 (draft; this report is part of it). Checkpoint in Issue #3.

**Nothing in this report was executed on the VPS by Claude. Nothing was merged, deployed, started, stopped or changed.**

The repository is public. This report omits the server's IP address and hostname, the account identifiers and every credential. Evidence classes are as in `ROADMAP.md` (top of "Roadmap v3.0"):
- [GH] verified GitHub state;
- [VPS-R] reported VPS observation;
- [OFF] tested offline only;
- [PR] unmerged PR work;
- [PLAN] planned;
- [RT-U] runtime unverified;
- [DEC] operator decision.

Every VPS finding is also classed **CONFIRMED** (seen in output or code), **INFERRED** (follows from confirmed facts) or **UNKNOWN**.

---

## 0. Operator decisions, 2026-11-05 15:48 UTC (after an independent review of PR #34 head `7802442`)

| Decision | What it allows / requires | Where it is handled | Status at 2026-11-05T16:13Z |
|---|---|---|---|
| **APPROVED: Gate B Session 2** (read-only, as `nobody`, pinned and checksum-verified scripts, with S2-P and S2-W; S2-U **not** yet) | no production DB changes, no service/broker/git/deployment actions; review the output before sharing; never publish DB copies or statements; stop on any failed copy or integrity check | §5 (the exact command, stop conditions, offline test) | command sent to the operator about 16:00 UTC; **output pending** |
| **APPROVED: offline watchdog fix** with regression tests; draft PR; no merge or deployment; **not** called "D-9" (conflicts with M23 task D09) | a new register task **A18**, linked to M21 B-6 / OP2 | PR **#35** (`m21/a18-gw-watchdog-event-loop`, base `m21/rc-base`) | opened; head `da56e12`; tests in the PR |
| **NOT YET APPROVED: Gate A-0 execution**; correct the procedure first (7 points) | precondition order; effective-condition and disabled-state verification; no automatic release of the kill switch; systemd vs dashboard/direct starts; keep the dashboard running and don't use its start routes or kill-switch release; document the flatten block; check the broker policy before claiming containment | §4 (the corrections table, the corrected block, 17 offline tests, rollback, scope) | corrected and tested offline; **final review after the Session 2 results** |
| Keep all PRs as drafts; keep `ROADMAP.md` v3.0, `MILESTONE_STATUS.md` and this report current; don't merge #34 or change the approved roadmap baseline | — | this PR | done; nothing merged |

**Identifier change:**
- The roadmap decisions formerly written `D-1`…`D-10` are now **`RD-1`…`RD-10`**.
- They used to look like the register's M23 tasks `D01`–`D10` and the C01 design's decisions `D-1`…`D-6`.
- The watchdog task is **A18**. RD-9 is the decision to add A18 to the register.
- The changelog's earlier entries keep their original wording (append-only); entry 7 records the rename.

## 1. H1 / P1 / P2 read-only checks

**Status:** approved on 5 Nov and run by the operator; output received 5 Nov, about 15:12 UTC.
- Claude cannot reach the VPS. The laptop bridge offers no shell to the server, and the laptop's key is refused.
- The operator runs the block in the VS Code Remote-SSH root terminal:
  - H1 runs as `nobody`;
  - P1 and P2 run as root, read-only;
  - an H1b addition reads `logs/boot.log` as `nobody`.
- The output is redacted at source:
  - URL credentials, Telegram tokens, `key/token/secret/password` values and IBKR account identifiers are masked;
  - P2b masks client IPs, ports and key fingerprints.
- The block is unchanged from the reviewed version except for narrowing:
  - P2 prints only `systemd[…]`, `[STARTUP]`, `KILL_SWITCH]` and `GW-WATCHDOG]` lines;
  - H1 adds the bot-log start-up lines and the reflog around the bot's last start.

**Received:** 2026-11-05, about 15:12 UTC (the last journal entry is 15:12:23 UTC). H1 ran as `nobody`; P1 and P2 ran as root, read-only. H1b (`boot.log`) was not run, and it is **not needed** (see the note after the table). The output is kept as observations [VPS-R]. "Who did it" is never inferred from timestamps.

**Journal retention:** the system journal holds a single boot (index 0). Its first entry is **2026-06-20 15:22:11 UTC**, so lifecycle events before 20 Jun are not in the journal.

| # | Question | Evidence | Answer | Class |
|---|---|---|---|---|
| Q1 | Last reboot | `uptime -s` 2026-03-05 19:23:23; `last -x`: boot on 5 Mar 19:23, "still running"; the previous boot was 5 Mar 19:05 | **No reboot since 5 Mar 2026.** The systemd units were installed on 2 Jun (M15.0), so **they have never been through a boot.** Boot behaviour follows from the unit configuration and has not been observed. | CONFIRMED |
| Q2 | Root crontab | `root_crontab_matches_rc=1`: no line matches `sync`, `deploy`, `git`, `main.py`, `scheduler`, `paper`, `@reboot` or `algo`. `/var/spool/cron/crontabs/` holds `root`, so root does have a crontab, with other content. | **No legacy `@reboot deploy.sh` entry, and no algo automation in cron.** A-0c becomes a no-op. | CONFIRMED (the other cron lines were not printed, by design) |
| Q3 | Last bot start and broker mode | Journal: restarts on 26 Jun 06:30:04 and 30 Jun 06:37:17 (Stopping → Deactivated successfully → Stopped → Started within one second). `bot.log` on 30 Jun 06:37:18: `[STARTUP] Broker: signal_only:ibkr_paper`; `[KILL_SWITCH] State on startup: active=False reason=test cleanup`. | The last start was **30 Jun 06:37:17 UTC** with **`signal_only:ibkr_paper`**: the IBKR paper broker wrapped in `SignalOnlyBroker`, whose `submit()` **never** calls the wrapped broker (code: `bot/etoro/signal_only_broker.py`). **The scanner could not place IBKR orders in that run.** The wrap reason (which M13.4A/D0 policy gate) is not in the output. | mode: CONFIRMED; wrap reason: UNKNOWN (S2-W gives it) |
| Q4 | Commit the bot last started with (proof vs inference) | Reflog: the last HEAD move at or before 30 Jun 06:37:17 is **00:21:06**, `85d882f → 04bf872` ("checkout … to m21-1extra-a-paper-order-contract", then "reset: moving to origin/…"). The next move is at 10:11:45. | **HEAD on disk at the start was `04bf872`** (on GitHub; an ancestor of `main`; "M21.1extra-A follow-up: live path respects real kill switch"). The 26 Jun start ran with HEAD `af6a46a`. | HEAD at start: CONFIRMED (the reflog does not record uncommitted edits: UNKNOWN). **What the process actually executed: INFERRED, mixed** (see below). |
| Q5 | When and why the bot stopped | Journal 6 Jul 14:22:43: Stopping → Deactivated successfully → Stopped, no Started. `systemctl show`: `InactiveEnterTimestamp` 14:22:43, `ExecMainStatus=15` (SIGTERM), `Result=success`. `bot.log` was last written 14:22:04. | **An explicit `systemctl stop`-style stop on 6 Jul 14:22:43 UTC. It was not a crash, not a restart and not an automatic restart.** Who issued it is not recorded. It falls on the same day as the D2 supervised IBKR proof and the `.env` clean-up recorded in `MILESTONE_STATUS.md`. | stop type and time: CONFIRMED; initiator: UNKNOWN; context: INFERRED |
| Q6 | Gateway failure | Journal: 1 Sep 06:48:58 "Failed with result 'exit-code'", then Stopped and Started (consistent with `Restart=always`). 3 Sep: stop/restart requests at 06:44:53, 06:45:12 and 06:45:25 (each Stopping; the main process exited 1; Failed; Started), then **06:46:27 Stopping with no Started**. `ExecMainExitTimestamp` 06:46:28, `ExecMainStatus=1`, `Result=exit-code`. | **The gateway's last transition was a stop request (3 Sep 06:46:27). It did not crash by itself at the end.** systemd shows it as `failed` only because the process exited with status 1 while stopping. After an explicit stop, `Restart=always` does not apply, so it stays down until it is started or the server reboots. The 1 Sep crash cause and the 3 Sep exit-1-on-stop are not explained (the gateway's own log was not read). | sequence: CONFIRMED; causes and initiator: UNKNOWN |
| Q7 | Who or what restarted the dashboard on 29 Oct | Journal 29 Oct 06:46:24: a restart within one second. P2b (06:30–06:50): **no** `sshd` Accepted/session, `systemd-logind`, `sudo`, "Startup finished" or "Reached target" lines; only the restart. | **No new SSH login, logind session or sudo was logged in that window.** The initiator is UNKNOWN: an already-open session, or an automatic restart. | CONFIRMED (absence in those patterns); initiator: UNKNOWN |
| Q8 | Checkout, fetch and sync history | Reflog: 439 entries. 2 Jun 19:31–21:03: `reset: moving to origin/main` ×6, the last at 21:03:12, matching `sync.log`'s last write at 21:03:16. After that, every move is a manual-style `pull`/`checkout`/`merge`/`reset`/`am`: feature branches checked out in `/opt/algo-trader` throughout June–July, `main` merges on 6 Jul, then **23 Oct 19:39** (`0d9d77a`), **26 Oct 08:24** (`git am` → `0492dcf`), 10:56 (`aa69928`) and **20:44:14** (`015d2ce`; `FETCH_HEAD` 20:44:00). Every one of these commits is on GitHub and is an ancestor of `015d2ce`. | **`sync.sh` has not run since 2 Jun 21:03** (no `sync.log` write since; `sync.sh` logs on start and on every update). The 3–8 Jun `reset: moving to origin/main` entries were not written by `sync.sh`. **The production checkout was operated by hand and repeatedly switched branches while the bot was running.** | CONFIRMED (the reflog and mtimes); who ran the commands: UNKNOWN |

**Restart timing pattern:** every bot, dashboard and gateway restart seen since 20 Jun fell between **06:14 and 06:48 UTC**: 26 Jun, 30 Jun, 1 Sep, 3 Sep, 2 Oct, 22 Oct, 24 Oct and 29 Oct.
- The window matches Ubuntu's stock `apt-daily-upgrade.timer` (06:00 UTC plus a random delay of up to 60 min). On Ubuntu 24.04 (kernel 6.8, Python 3.12), `needrestart` restarts services whose libraries were upgraded.
- This is a **hypothesis (INFERRED, unverified)** for the unexplained restarts. The 6 Jul 14:22 stop and the 3 Sep 06:46:27 final stop do not fit it, because they were stops, not restarts.
- S2-U (optional, §5) checks it. It does not change A-0. It matters for OP3/OP4: an unattended restart during the soak.

**What the bot process actually ran (Q4):** every `ib_insync` import and several `bot.*` imports (for example `from bot.kill_switch import …` inside `submit()`) are **lazy**. The process ran from 30 Jun to 6 Jul, and in that time the checkout moved many times: 30 Jun 10:11–15:10, the unprinted entries #238–#424, and 6 Jul. Any module first imported after a move was loaded from whatever was on disk then. So the 30 Jun–6 Jul process **cannot be attributed to a single commit**: start-time imports came from `04bf872`, later lazy imports from later checkouts (INFERRED from code + reflog). Two consequences:
- this period is not a clean operational baseline;
- `/opt/algo-trader` must not be used as a working copy while the bot runs (R15-11; a new hardening item).

**`boot.log` (H1b) is not needed:**
- No reboot has happened since 5 Mar (Q1), and root's crontab has no `deploy.sh` line (Q2).
- `deploy.sh` always ends by starting `sync.sh`, which writes to `sync.log` on start. `sync.log` has not changed since 2 Jun 21:03:16 (Q8), so no complete `deploy.sh` run has happened since then.
- Gate A-0 covers the `deploy.sh` path in any case.

## 2. VPS safety findings (concise)

The Session 1 and H1/P1/P2 outputs are kept as observations [VPS-R]. **Neither establishes that the server is safe.**

| # | Finding | Class |
|---|---|---|
| F-1 | **Nothing automatic deploys code:** `sync.sh` has not run since 2 Jun 21:03; there is no algo line in root's crontab; no timer, `rc.local`, hook or runner. | CONFIRMED |
| F-2 | Checkout `015d2ce`, clean. `main` (`80b4343`) has never been deployed (difference: research/docs/tests only). All of `/opt/algo-trader`'s history matches GitHub. | CONFIRMED |
| F-3 | **The bot is inactive (since 6 Jul 14:22:43, an explicit stop) but enabled at boot.** No reboot since 5 Mar, so the boot start has never been exercised. A reboot would start `main.py` from `015d2ce`, code it has never run. | CONFIRMED (the boot effect from unit configuration) |
| F-4 | **The file kill switch is off** (since 27 May, "test cleanup"). | CONFIRMED |
| F-5 | **At its last start the bot was signal-only for IBKR paper** (`signal_only:ibkr_paper`): no broker submission was possible. Today's allocation policy (D0 authorization, kill switches, expiry) is **not verified**. It is a second, independent barrier only if it is still closed (S2-P reads it). | mode on 30 Jun: CONFIRMED; today: UNKNOWN |
| F-6 | The gateway has been down since an explicit stop on 3 Sep 06:46:27 (exit status 1). No API port is open. It returns only on a manual start or a reboot (enabled). | CONFIRMED |
| F-7 | **The dashboard (root, active) can start the bot outside systemd from five authenticated routes:** `/api/start`, `/api/restart`, `/api/telegram/save`, `/api/strategy/save`, `/api/strategy/reset`. The last three start it **whether or not it was running**. Three of them are settings saves, where the bot start is a side effect; the Telegram save also rewrites `.env`. The same dashboard can deactivate the kill switch and change the allocation policy. | CONFIRMED (code at `015d2ce`; unchanged in `5b0f40b`) |
| F-8 | `deploy.sh` → `sync.sh` → reset to `main` → `systemctl restart` of the bot, regardless of the enabled state (corrects Session 1 §12-3b). | CONFIRMED (code) |
| F-9 | **The `gw-watchdog` defect (§2a):** the scanner's IBKR path fails closed, and the gateway monitoring is misleading. Present in the current and proposed trees. | CONFIRMED mechanism; INFERRED VPS frequency |
| F-10 | The production checkout was used as a working copy (branch switches, merges, `git am`) while the bot ran, so the 30 Jun–6 Jul process ran mixed code versions. | CONFIRMED (reflog); effect INFERRED |
| F-11 | Restarts cluster between 06:14 and 06:48 UTC, consistent with unattended upgrades plus `needrestart` | INFERRED (S2-U) |

**Assessment:** the server is idle and **nothing automatic is reaching it**. The controls that keep it idle are thin:
- a stopped bot;
- a stopped gateway;
- a broker policy that was signal-only on 30 Jun and is unverified today.

**The latent activation paths:**
- reboot (F-3);
- `deploy.sh` (F-8);
- the five dashboard routes (F-7).

Of these, Gate A-0 closes the systemd paths and blocks IBKR orders on every path. **It cannot stop an authenticated dashboard user**, who can start the bot and also undo A-0a. Its scope is automatic and accidental activation.

### 2a. The `gw-watchdog` RuntimeError: an independent, recurring defect, not the cause of the 6 Jul stop

**Seen on the VPS:** the last six lines of `bot.log` are a traceback ending in `RuntimeError: There is no current event loop in thread 'gw-watchdog'`, raised from `eventkit/util.py` line 21 (`asyncio.get_event_loop_policy().get_event_loop()`). The earlier frames are not in the output.

**Reproduced offline [OFF]:**
- Setup: Python 3.12.3, `ib_insync` 0.9.86, `eventkit` 1.0.3 (`requirements.txt`: `ib_insync>=0.9`). In a thread named `gw-watchdog`, run `from ib_insync import IB; IB()`. **No `connect()` is called.**
- Result: the same `RuntimeError`, and **the same last six lines, frame for frame**.
- Cause: `eventkit/util.py` runs `main_event_loop = get_event_loop()` at import time. In a non-main thread that has no event loop, that call raises.
- The error repeats on **every** call (three consecutive "ticks": RuntimeError ×3), because a failed import is not cached.
- If the main thread has imported `ib_insync` first, `IB()` constructs fine.

**Why the bot hits it (code at `04bf872`, unchanged in `015d2ce`, `80b4343` and the proposed tree `5b0f40b`; no open PR touches it):**
- `GatewayWatchdog._tick()` calls `api_probe()` only when the gateway's TCP port is open. `api_probe()` does `from ib_insync import IB` and `ib = IB()` **outside** its `try`.
- Every `ib_insync` import in the bot is lazy. Under `signal_only:ibkr_paper` nothing in the main thread imports it.
- So on every tick while the port is open, `_tick()` raises **before** it records the probe. `_loop()` catches the exception, logs `[GW-WATCHDOG] tick error` with the traceback (default interval 60 s) and carries on.

**Effects (INFERRED from code; the VPS output shows only the last traceback):**
1. **Fail-closed for orders.** The watchdog never reaches `api_up_healthy`; it starts at `STATE_UNKNOWN`. `main.py` refuses IBKR submissions as `broker_unready` (`is_healthy_for_submission()` stays False). The scanner's IBKR path could not have submitted even without the signal-only wrap.
2. **Misleading monitoring.** On those ticks the gateway state is not written (`write_gateway_state` is never reached), so the stored state goes stale. Up→down transitions and their alerts cannot happen. This bears on M21 **B-6** (the gateway-down alarm drill) and on OP2.
3. **Log growth.** About one traceback a minute while the port is open, consistent with an 88 MB `bot.log` (the count is not verified; S2-W counts it).

**Relation to the 6 Jul stop:**
- **It was not produced by the shutdown.** `main.py` installs no SIGTERM handler and logs nothing at shutdown. The last write (14:22:04) **precedes** the stop (14:22:43) by 39 s, consistent with the 60 s tick. CONFIRMED (timestamps + code).
- **It did not stop the process.** The exception is caught inside the thread loop, and the process ended by an external SIGTERM with `Result=success`. CONFIRMED.
- Whether it *motivated* someone to stop the bot: UNKNOWN.
- A logged traceback means the gateway's TCP port was open at 14:22:04 on 6 Jul (INFERRED; consistent with that day's D2 IBKR session).

**Not affected:** the WS2 paper loop (#1) and `bot/paper_loop_d2` do not use the watchdog. M21 activation (OP3) runs WS2, not `main.py`'s IBKR submit path.

**Proposed handling (not done; code change needs approval):**
- a small offline fix on an isolated branch: give the watchdog thread its own event loop at thread start, and move `IB()` inside the `try`;
- a regression test that constructs the probe in a named non-main thread;
- recorded as the new register task **A18** (decision RD-9). **Approved and implemented offline: PR #35.**

Fix it before OP2 so that the B-6 drill is meaningful. It does not block Gate A-0.

## 3. What happens if … (A3)

The scenarios describe the state after H1/P1/P2 (before Gate A-0). The last column shows the effect of the A-0 proposal (§4).

| Event | What follows today (before A-0) | Class | With A-0 in place |
|---|---|---|---|
| **1. The VPS reboots** | systemd starts the three enabled units: the bot, the dashboard and the gateway. **This has never been observed**: no reboot since 5 Mar, and the units were installed on 2 Jun. The bot runs `main.py` from `015d2ce` with `.env`. Root's crontab has **no** `deploy.sh` line, so nothing else runs. If the allocation policy still disables IBKR paper, the bot comes up `signal_only` (no submissions). If it does not, the gateway's login state decides whether submissions are even attempted. The `gw-watchdog` defect (§2a) would block them in any case while the port is open. | units enabled, no cron line, no reboot since 5 Mar: CONFIRMED; policy today: UNKNOWN (S2-P); gateway login at boot: UNKNOWN | the bot start is skipped (disabled + condition guard). The dashboard and gateway start. The kill switch is on. |
| **2. `deploy.sh` executes** (by hand only; no cron entry) | `pip install --upgrade pip` and `-r requirements.txt` (modifies `venv`); `daemon-reload`; starts every enabled, inactive unit (**the bot now**); restarts `sync.sh`. Within about 60 s, `sync.sh` resets the checkout to `origin/main` (`80b4343`), runs pip, and **restarts the dashboard and the bot whatever their enabled state**. | CONFIRMED (code, sha-identical to the deployed copies) | `deploy.sh` skips the disabled bot. `sync.sh` still resets the checkout and restarts the dashboard. Its bot restart is skipped by the guard (systemd semantics per docs; not tested on real systemd). The checkout moves, so **do not run `deploy.sh`**. |
| **3. The bot is started from the dashboard** | Any of **five** authenticated POST routes (F-7) runs `Popen(venv/bin/python3 /opt/algo-trader/main.py)` in a new session, **outside systemd**. `systemctl` then still shows the unit as inactive. The scanner runs. Signals go to the `BROKER` broker, possibly wrapped `signal_only` by the policy. IBKR submissions also need a healthy watchdog (fails closed, §2a) and a running gateway (down). Telegram alerts and DB writes happen. | routes: CONFIRMED; broker on 30 Jun `signal_only:ibkr_paper`: CONFIRMED; today: UNKNOWN | the process still starts (the guard is systemd-only). **Every IBKR submission and flatten is refused** by the file kill switch, whatever the policy and watchdog say. Signals, DB writes and alerts can still happen. A dashboard user can also deactivate the kill switch (outside A-0's scope; see §4). |
| **4. The IB Gateway comes back** | It was stopped explicitly on 3 Sep. Only a manual start or a reboot restarts it. The gateway alone sends no orders; no bot or WS2 loop is running or scheduled. | CONFIRMED | the same. If a bot is started, the kill switch blocks IBKR. |
| **5. `main` receives new merges** | **No VPS change**: no `sync.sh`, no cron entry, no timer, runner or hook. Vercel makes a Production deployment per merge. Latent: a manual `deploy.sh` or `sync.sh` start deploys `main`. An unattended `needrestart` restart (F-11) reloads whatever is **on disk**; it does not fetch. | CONFIRMED (needrestart: INFERRED) | merges cannot reach the VPS automatically. **`sync.sh` stays stopped** (A-0d). |

## 4. Gate A-0: containment procedure (revision 4, NOT approved, NOT executed)

**Readiness at 2026-11-05T17:37Z UTC: ready for the operator's approval once two things are done.**
1. **IBKR position and working-order confirmation** (paper **and** live), made by the operator in the IBKR Client Portal or the TWS/IBKR mobile app (step 1 below).
2. The independent review of this revision.

No other blocker remains. Session 2 is closed; S2-P is verified; the seven revision-3 corrections are in place.

**What Session 2 changed for A-0:**
- **S2-P:** the stored policy makes **all five broker lanes signal-only** (paper, ibkr_paper, ibkr_live, etoro_paper, etoro_real). That is a second, independent barrier for every broker, including eToro, which the file kill switch does not cover.
- **A-0 is therefore defence in depth, not an emergency stop.** Its purpose is to keep everything closed through Gate A-1/A-2, when code changes on disk and policy settings may be touched. It holds even if the policy is changed by mistake.

**Position confirmation (required; Session 2 cannot provide it):**
- **Why Session 2 can't answer this:** `broker_positions` and the M14 daily state hold only what our own code last ingested. The gateway has been down since 3 Sep, so these rows say nothing about today. **Missing or zero rows are not proof of zero exposure.**
- **Why it matters:** once A-0a is on, the bot cannot flatten anything automatically. Resting GTC/bracket (OCA) orders at IBKR can still fill whatever the kill switch says.
- **What the operator checks**, for **both** the IBKR paper account and the IBKR live account (M12 proved a live account exists):
  - open positions (count);
  - working orders, including GTC, stop and OCA legs (count).
- **Where to check:** the IBKR Client Portal or the TWS/IBKR mobile app, at a stated UTC time. A Flex statement is end-of-day, does not list working orders, and is **not** sufficient for this.
- **How to record it:** in `A0_POSITIONS`, as counts only, with no account numbers. The block refuses to run without it and stores the text in the snapshot.
- **If anything is open:** decide **before** A-0, either to close or cancel it manually at IBKR, or to keep it. The kill switch blocks only the bot's own automated flatten and submissions, never a manual action at IBKR.

**Status:**
- Revision 2 was reviewed independently on 5 Nov. On **2026-11-05 15:48 UTC** the operator found the approach reasonable but **did not approve execution**.
- This revision makes the corrections that were asked for. It comes back for a final independent review **after the Session 2 results**, because one claim depends on them (row 7 below).

**Corrections asked for, and where they are made:**

| # | Correction | Where |
|---|---|---|
| 1 | Check that `ALLOW_BOT_START` is absent in the preconditions, before anything is changed | §4.1 step 0. A leftover `gate-a0.conf` drop-in also stops the run there. |
| 2 | Verify the *effective* systemd `ConditionPathExists` and the disabled state | §4.1 A-0b checks `is-enabled=disabled` and that the boot link is gone. A-0b2 checks, after `daemon-reload`: `NeedDaemonReload=no`; the drop-in listed in `DropInPaths`; the condition present in `systemctl cat` (the loaded unit); and `systemd-analyze condition` evaluating it **false**. No test start is attempted. |
| 3 | Partial-failure recovery never auto-releases the kill switch | §4.1 `report()` runs on every exit and is read-only. On a failure it names the stage. **Nothing is rolled back automatically, and the kill switch is never released.** §4.3: undo only with approval. |
| 4 | Keep systemd-controlled starts distinct from dashboard/direct Python starts | §4.4 and the final verification lines. The guard covers systemd starts only; the kill switch covers IBKR orders on every path. |
| 5 | Keep the dashboard running; do not use its five start routes or the kill-switch release | §4.4: an operator rule from now until OP3. **A-0f is withdrawn**: the operator decided to keep the dashboard running. |
| 6 | State that an active kill switch also blocks IBKR automated flattening | §4.4 |
| 7 | Check the current broker allocation policy before claiming all broker execution is contained | §4.4. **Completed in revision 4** with the verified S2-P result. |

**Approval and scope:**
- A-0 changes three things, all reversible: the kill-switch file (A-0a), the bot unit's boot link (A-0b) and a drop-in start guard (A-0b2).
- A-0c is a no-op. P1 found no `deploy.sh` line; the block checks again.
- It does **not** stop or start any process. It does **not** touch `.env`, the databases, git, the dashboard, the gateway, `sync.sh`, the broker policy or Vercel.

**Why `systemctl disable` alone is not enough** (correction to the Session 1 report §12-3b, kept from revision 2):
- `deploy.sh` skips a disabled unit, but it then launches `sync.sh`.
- `sync.sh` runs `systemctl restart algo-trader.service` whenever `origin/main` differs from the checkout, regardless of the enabled state.
- `systemctl mask` cannot be used: the unit file lives in `/etc/systemd/system`.
- So A-0b2 adds `ConditionPathExists=/var/lib/algo-trader/ALLOW_BOT_START`, and every systemd start is *skipped* while that file is absent. An `Assert…=` would fail the start job instead; a `Condition…=` skips it silently ([systemd-devel](https://lists.freedesktop.org/archives/systemd-devel/2022-March/047575.html)).

### 4.1 Commands

**Step 1 (operator, after the IBKR check):** type this with your own counts and time. It records no account numbers:

```bash
export A0_POSITIONS='paper positions=0 working_orders=0 | live positions=0 working_orders=0 | checked_at_utc=2026-11-06T09:00Z'
```

**Step 2:** paste the block. It runs in a child shell, so a STOP or a failure ends only the block, not the terminal.

```bash
# Requires A0_POSITIONS to be exported first (step 1); the block refuses to run without it.
bash -euo pipefail <<'GATEA0'
cd /opt/algo-trader
TS=$(date -u +%Y%m%dT%H%M%SZ)
UNIT=algo-trader.service
UNIT_FILE=/etc/systemd/system/algo-trader.service
DROPIN=/etc/systemd/system/algo-trader.service.d/gate-a0.conf
ALLOW=/var/lib/algo-trader/ALLOW_BOT_START
KS=/opt/algo-trader/data/kill_switch.json
RUN_RE='[p]ython3?[^ ]* +([^ ]*/)?main\.py( |$)|[b]ash +([^ ]*/)?sync\.sh( |$)'   # bot (any launcher) or sync.sh
SNAP=/var/lib/algo-trader/gate_a0/$TS
STAGE=preconditions

# Read-only state report on EVERY exit. It never changes, undoes or releases anything.
report() {
  rc=$?
  echo "----- STATE AT EXIT (exit=$rc) -----"
  python3 -I -c 'import json,sys; s=json.load(open(sys.argv[1])); print("kill_switch_active=%s reason=%s" % (s.get("active"), s.get("reason")))' "$KS" 2>&1 || true
  echo "unit_file_state=$(systemctl is-enabled $UNIT 2>&1 || true) active=$(systemctl is-active $UNIT 2>&1 || true)"
  echo "drop_in=$([ -e $DROPIN ] && echo present || echo absent) allow_file=$([ -e $ALLOW ] && echo PRESENT || echo absent)"
  ps -eo pid,args | grep -E "$RUN_RE" || echo "processes: no main.py or sync.sh"
  stat -c 'env_file_mode=%a %n' /opt/algo-trader/.env* 2>/dev/null | sed 's#/opt/algo-trader/##' || true
  if [ "$rc" != 0 ] && [ "$STAGE" = preconditions ]; then
    echo "A-0 STOPPED BEFORE ANY CHANGE. Send this output."
  elif [ "$rc" != 0 ]; then
    echo "A-0 DID NOT COMPLETE (failed during: $STAGE). Nothing was rolled back automatically and the kill switch was NOT released."
    echo "Every partial state is safe. Send this output; undo nothing without approval."
  fi
}
trap report EXIT

# 0. PRECONDITIONS (read-only). Any STOP happens before anything is changed.
stop() { echo "STOP (nothing changed): $*"; exit 2; }
case "${A0_POSITIONS:-}" in ""|*REPLACE*) stop "A0_POSITIONS not set: confirm IBKR paper AND live positions and working orders first";; esac
if ps -eo pid,args | grep -E "$RUN_RE"; then stop "main.py or sync.sh is running"; fi
[ "$(systemctl is-active $UNIT || true)" = inactive ]                  || stop "bot unit is not inactive"
[ "$(systemctl show -p FragmentPath --value $UNIT)" = "$UNIT_FILE" ]   || stop "unexpected unit file location"
[ "$(systemctl show -p NeedDaemonReload --value $UNIT)" = no ]         || stop "unit files changed on disk but not loaded"
[ ! -e "$ALLOW" ]                                                      || stop "allow-file $ALLOW already exists"
[ ! -e "$DROPIN" ]                                                     || stop "a gate-a0 drop-in already exists"
command -v systemd-analyze >/dev/null                                  || stop "systemd-analyze missing (needed to verify the guard)"
python3 -I -c 'import json,sys; json.load(open(sys.argv[1]))' "$KS"    || stop "kill_switch.json missing or unreadable"
[ "$(git rev-parse HEAD)" = 015d2cea927b9b4db3ee65604e0b4ebb58211e61 ] || stop "HEAD is not 015d2ce"
[ -z "$(git status --porcelain --untracked-files=no)" ]                || stop "modified tracked files"
echo "preconditions: OK"

# 1. PRESERVE (nothing printed; .env is never read or copied: only its size and mtime are recorded)
STAGE=preserve
install -d -m 0700 "$SNAP"
git rev-parse HEAD > "$SNAP/head.txt"
cp -p "$KS" "$SNAP/kill_switch.json.before"
if [ -f data/bot_state.json ]; then cp -p data/bot_state.json "$SNAP/bot_state.json.before"; else echo absent > "$SNAP/bot_state.absent"; fi
stat -c '%s %Y %n' .env > "$SNAP/env.stat"      # size + mtime only; .env is never read
printf '%s\n' "$A0_POSITIONS" > "$SNAP/positions_attestation.txt"
systemctl show $UNIT algo-trader-dashboard.service ibgateway.service \
  -p Id,UnitFileState,ActiveState,SubState,FragmentPath,DropInPaths > "$SNAP/units.before"
ls -l /etc/systemd/system/multi-user.target.wants/ > "$SNAP/wants.before"
crontab -l -u root > "$SNAP/root_crontab.before" 2> "$SNAP/root_crontab.err" || true
chmod 600 "$SNAP"/*
echo "preserved: $SNAP"

# A-0s. .env BACKUP PERMISSIONS: owner-only. chmod/stat only; no .env file is read, shown or copied.
STAGE=A-0s
for f in /opt/algo-trader/.env*; do
  [ -e "$f" ] || continue
  rel=${f#/opt/algo-trader/}
  if git ls-files --error-unmatch -- "$rel" >/dev/null 2>&1; then echo "A-0s: $rel is a tracked template; left as is"; continue; fi
  if [ -L "$f" ] || [ ! -f "$f" ]; then echo "A-0s: $rel is not a regular file; left as is (report it)"; continue; fi
  before=$(stat -c '%a %s %Y' "$f")
  [ $((8#${before%% *} & 077)) -eq 0 ] || chmod go-rwx "$f"
  after=$(stat -c '%a %s %Y' "$f")
  [ "${before#* }" = "${after#* }" ] || { echo "A-0s: size or mtime of $rel changed"; exit 3; }
  [ $((8#${after%% *} & 077)) -eq 0 ] || { echo "A-0s: $rel is still group/world accessible"; exit 3; }
  echo "A-0s: $rel mode ${before%% *} -> ${after%% *} (owner $(stat -c %U "$f"))" | tee -a "$SNAP/env_modes.txt"
done
chmod 600 "$SNAP"/*

STAGE=A-0a
# A-0a. KILL SWITCH ON (the repository's own function; writes data/kill_switch.json only). Verified by re-reading.
venv/bin/python3 -I -c 'import sys; sys.path.insert(0, "/opt/algo-trader")
from bot.kill_switch import activate_kill_switch, get_kill_switch_state
reason = "Gate A-0 containment, operator-approved, " + sys.argv[1]
activate_kill_switch(reason)
s = get_kill_switch_state(); print("A-0a: kill_switch_active=%s" % s["active"])
sys.exit(0 if (s["active"] is True and s.get("reason") == reason) else 3)' "$TS"

# A-0b. NO START AT BOOT. Verified: disabled and the boot link gone.
STAGE=A-0b
systemctl disable $UNIT
[ "$(systemctl is-enabled $UNIT || true)" = disabled ]                  || { echo "A-0b: still enabled"; exit 3; }
[ ! -e /etc/systemd/system/multi-user.target.wants/$UNIT ]             || { echo "A-0b: boot link still present"; exit 3; }
echo "A-0b: disabled; boot link removed"

# A-0b2. NO SYSTEMD START BY ANY PATH until the allow-file exists. Verified as LOADED by systemd, and evaluated.
STAGE=A-0b2
install -d -m 0755 "$(dirname "$DROPIN")"
printf '[Unit]\n# Gate A-0 %s: systemd starts this unit only while %s exists.\nConditionPathExists=%s\n' "$TS" "$ALLOW" "$ALLOW" > "$DROPIN"
systemctl daemon-reload
[ "$(systemctl show -p NeedDaemonReload --value $UNIT)" = no ]         || { echo "A-0b2: reload not effective"; exit 3; }
DIP=$(systemctl show -p DropInPaths --value $UNIT)
case " $DIP " in *" $DROPIN "*) ;; *) echo "A-0b2: drop-in not loaded"; exit 3;; esac
LOADED=$(systemctl cat $UNIT)
grep -qxF "ConditionPathExists=$ALLOW" <<< "$LOADED"                    || { echo "A-0b2: condition not in the loaded unit"; exit 3; }
if systemd-analyze condition "ConditionPathExists=$ALLOW" >/dev/null 2>&1; then echo "A-0b2: condition would PASS"; exit 3; fi
[ "$(systemctl is-active $UNIT || true)" = inactive ]                  || { echo "A-0b2: unit not inactive"; exit 3; }
echo "A-0b2: guard loaded; ConditionPathExists evaluates FALSE (a systemd start would be skipped)"

STAGE=A-0c
# A-0c. Only if root's crontab has a deploy.sh line (P1 found none: expected no-op).
if grep -q 'deploy\.sh' "$SNAP/root_crontab.before"; then
  grep -v 'deploy\.sh' "$SNAP/root_crontab.before" > "$SNAP/root_crontab.after" || true
  chmod 600 "$SNAP/root_crontab.after"; crontab -u root "$SNAP/root_crontab.after"
  echo "A-0c: removed $(grep -c 'deploy\.sh' "$SNAP/root_crontab.before") line(s)"
else
  echo "A-0c: no deploy.sh line in root's crontab; nothing changed"
fi

# A-0d (sync.sh stays stopped) and A-0e (gateway untouched): no action.

# 2. FINAL VERIFY (read-only)
STAGE=final-verify
[ "$(git rev-parse HEAD)" = "$(cat "$SNAP/head.txt")" ] && echo "HEAD unchanged" || { echo "HEAD CHANGED"; exit 3; }
[ "$(stat -c '%s %Y %n' .env)" = "$(cat "$SNAP/env.stat")" ] && echo ".env: size and mtime unchanged" || { echo ".env CHANGED"; exit 3; }
echo "crontab deploy.sh lines: $(crontab -l -u root 2>/dev/null | grep -c 'deploy\.sh' || true)"
echo "systemd-controlled starts (boot, deploy.sh, sync.sh restart, needrestart, systemctl start): SKIPPED by the guard; boot link removed"
echo "dashboard routes and direct 'python3 main.py': NOT stopped by A-0b/A-0b2; IBKR orders and flatten refused by the kill switch"
echo "GATE A-0 DONE, snapshot $SNAP"
GATEA0
echo "gate_a0_exit=$?"
unset A0_POSITIONS
```

**Expected on success:**
- `preconditions: OK`;
- `A-0s: …` one line per `.env*` file: the backup `644 -> 600`, `.env` unchanged, `.env.example` (tracked template) left as is;
- `A-0a: kill_switch_active=True`;
- `A-0b: disabled; boot link removed`;
- `A-0b2: guard loaded; ConditionPathExists evaluates FALSE …`;
- `A-0c: … nothing changed`;
- `HEAD unchanged`;
- `.env: size and mtime unchanged`;
- the two scope lines;
- `GATE A-0 DONE`;
- the `STATE AT EXIT (exit=0)` summary;
- `gate_a0_exit=0`.

### 4.2 Offline tests of the corrected block (mock, not the VPS)

**Mock layout:**
- `/opt/algo-trader` was a detached checkout of `015d2ce`, with the real `bot/kill_switch.py` and the real `infra/systemd/algo-trader.service` installed at `/etc/systemd/system`.
- `systemctl` and `crontab` were stubs. The `systemctl` stub refuses `start`/`restart`/`stop` and records every call.
- `systemd-analyze` was the **real systemd 255** (the same major version as Ubuntu 24.04): it evaluated the condition, and `verify` loaded the drop-in.
- The tests ran from a script file, so the harness's own command line could not match the process check.

| Test | Expected | Result |
|---|---|---|
| T1: normal run | `exit=0`; the kill switch is on with this run's reason; `disabled`; boot link gone; drop-in loaded; the condition evaluates false (real `systemd-analyze`); no start/restart/stop attempted | pass |
| T2: rollback in reverse order (§4.3) | `kill_switch.json` byte-identical; boot link restored; `DropInPaths` empty | pass |
| T3: `ALLOW_BOT_START` exists | STOP before any change (no snapshot, kill switch untouched, still enabled) | pass |
| T4: a `gate-a0.conf` already exists | STOP before any change | pass |
| T5–T6: `python3 …/main.py` (direct start) or `bash …/sync.sh` running | STOP before any change | pass |
| T7: unit changed on disk, not loaded (`NeedDaemonReload=yes`) | STOP before any change | pass |
| T8: the unit is loaded from an unexpected path | STOP before any change | pass |
| T9–T10: HEAD ≠ `015d2ce`; a modified tracked file | STOP before any change | pass |
| T11: the kill-switch write fails | stage `A-0a`; **nothing else changed**; still enabled; no drop-in | pass |
| T12: `systemctl disable` fails | stage `A-0b`; **the kill switch stays ON**; no drop-in; nothing undone | pass |
| T13: `daemon-reload` fails | stage `A-0b2`; kill switch ON; disabled; nothing undone | pass |
| T14: the drop-in was written but not loaded | `A-0b2: drop-in not loaded`; kill switch ON; nothing undone | pass |
| T15: the condition would evaluate true | `A-0b2: condition would PASS`; kill switch ON; nothing undone | pass |
| T16: a `dashboard/app.py` process is running | not treated as the bot; `exit=0` | pass |
| T17: the crontab has an `@reboot deploy.sh` line | only that line removed; the other kept | pass |
| T0a/T0b (rev 4): `A0_POSITIONS` unset, or still `REPLACE` | STOP before any change; the backup stays 644 | pass |
| T1b (rev 4): normal run with the attestation | the backup `644 -> 600`; `.env` unchanged; the tracked `.env.example` and a symlink left as is; size and mtime unchanged; the attestation stored in the snapshot; then A-0a/b/b2 as T1. The block reads no `.env` content (0 references). | pass |
| T19 (rev 4): `chmod` of the backup fails | stage `A-0s`; the kill switch **not** yet engaged; nothing undone | pass |

**Incidental finding (no change proposed now):** `systemd-analyze verify` reports that the deployed `infra/systemd/algo-trader.service` puts `StartLimitIntervalSec` in `[Service]`, where systemd 255 **ignores** it ("Unknown key name … ignoring"). Its restart rate limit therefore uses the default interval. This is recorded as a hardening item under RD-10 (M22 C03).

### 4.3 Rollback and restore (each step separately, in reverse order, each with its own approval)

| Step | Undo | Restore only when |
|---|---|---|
| A-0s | not recommended: re-opening a secrets backup to other users is never needed. The previous modes are in `$SNAP/env_modes.txt`. | — |
| A-0c | `crontab -u root "$SNAP/root_crontab.before"` (the exact previous crontab) | not expected: P1 found no line, so nothing was removed |
| A-0b2 | `rm /etc/systemd/system/algo-trader.service.d/gate-a0.conf && rmdir --ignore-fail-on-non-empty /etc/systemd/system/algo-trader.service.d && systemctl daemon-reload`. For one supervised start instead: `touch /var/lib/algo-trader/ALLOW_BOT_START`, then remove it after. | at OP3 activation (approved), or for a supervised start the operator approves |
| A-0b | `systemctl enable algo-trader.service` (re-creates the boot link; starts nothing) | at OP3, when the bot is meant to run unattended |
| A-0a | `cp -p "$SNAP/kill_switch.json.before" data/kill_switch.json` (exact restore), or `deactivate_kill_switch("…")` | **only at OP3**, after OP2 gateway acceptance, with explicit approval. Never as part of a merge, a deployment or a partial-failure recovery. |
| **Partial failure** | **none automatic.** The block stops, prints the read-only `STATE AT EXIT` and exits. Every partial state is safe: an active kill switch, a disabled unit or an unloaded drop-in each make a start *less* likely. The operator sends the output; any undo of A-0b/A-0b2 is a separate approval. **The kill switch is never released as recovery.** | — |

**Restore conditions:** the operator approves OP3, the gateway passes OP2, and the deployed tree is the reviewed merge.

### 4.4 What A-0 contains and what it does not (systemd vs direct starts)

**Which control covers which order path (code at `015d2ce`; unchanged in `5b0f40b`):**

| Order path | File kill switch `data/kill_switch.json` (A-0a) | M13.4A allocation policy + D0 time-boxed authorization (signals.db `broker_allocation_policy`) | Other gates |
|---|---|---|---|
| Scanner → IBKR paper / IBKR live (`IBKRBroker.submit`), from systemd **or the dashboard** | **YES**: checked first in `submit()`, and in `flatten` | YES: `get_broker()` wraps it in `SignalOnlyBroker`; `submit()` also checks the runtime policy (fails closed) | gateway watchdog (`broker_unready`; fails closed, §2a); `bot/risk.py` gates (M14 parity is C01) |
| Scanner → local `paper` broker | no (no broker contact) | YES (runtime policy in `submit()`) | — |
| Scanner → `etoro_paper` | **no** | YES (runtime policy in `submit()`) | — |
| eToro **live** (`tools/etoro_live_write.py` → `bot/etoro/live_broker.py`, operator CLI only; `BROKER=etoro_real` is refused by the bot) | **no** | YES: global and eToro kill switches, `etoro_live_enabled` | operator CLI confirmations (M13) |
| M21.1extra run-once / D2 supervised tools | run-once reads it (simulation only). D2 takes the state as an input; its callers were not re-audited here. | D0 authorization | supervised, not scheduled |

So **A-0a covers every IBKR order path, including one started from the dashboard. It does not cover eToro paper or eToro live.** The allocation policy covers those, and eToro live is reachable only through the operator's own CLI.

A-0 does not change the policy. It lives in a production database, and the dashboard's broker-allocation control is the audited way to change it. Instead, **S2-P reads it** (§5) to confirm whether the second barrier is closed.

| Start path | After A-0 | Control |
|---|---|---|
| Boot; `deploy.sh`; the `sync.sh` restart; `needrestart`; a manual `systemctl start`/`restart` (**systemd-controlled**) | **skipped**: the unit does not start | A-0b (no boot link) + A-0b2 (`ConditionPathExists` false) |
| The five dashboard routes (`/api/start`, `/api/restart`, `/api/telegram/save`, `/api/strategy/save`, `/api/strategy/reset`) and a direct `python3 main.py` (**outside systemd**) | **not prevented**: the process can start | for IBKR orders: the kill switch (A-0a) refuses every submission **and every automated flatten**. For any broker: the allocation policy (S2-P) decides whether a submission is even attempted. |

**Operator rule from now until OP3:**
- keep the dashboard running (A-0f withdrawn);
- do **not** use the five bot-start routes;
- do **not** use the kill-switch release, or change the broker-allocation controls.

**The kill switch also blocks IBKR automated flattening:** `IBKRBroker.flatten()` checks it before any broker action (CONFIRMED in code at `015d2ce`). While A-0a is on, any open IBKR paper position can only be closed manually in TWS/Gateway or the IBKR web portal. Session 2 (the latest recorded `broker_positions`) and the Flex statements show whether any position is open. Review them before approving A-0.

**Containment claim (completed with S2-P):**
- **IBKR paper and live:** every order and every automated flatten is refused by A-0a on every start path. Independently, the stored policy makes both lanes signal-only (S2-P).
- **eToro paper and live:** not covered by the file kill switch. They are signal-only by the stored policy (S2-P), and eToro live is reachable only through the operator's own CLI.
- **Local `paper`:** signal-only by policy; it contacts no broker.
- **Limits of the claim:**
  - the policy reading is as of the Session 2 copy (5 Nov), and an authenticated dashboard user can change it (operator rule §4.4);
  - nothing here covers orders already resting at IBKR (see the position confirmation above).

## 5. Session 2: CLOSED (run by the operator; independently reviewed and accepted)

**Approved:** Gate B Session 2, read-only, as `nobody`, using the pinned, checksum-verified scripts, with S2-P and S2-W.

**Restrictions** (from the approval):
- no production DB changes;
- no service starts, stops or restarts;
- no broker API connections or orders;
- no git reset, deployment or merges;
- review the output before sharing;
- never publish DB copies or IBKR statements in the public repo;
- stop immediately on a failed copy or a failed integrity check.

**S2-U (unattended upgrades) is not approved yet.**

**The exact command, revision 2** (2026-11-05T16:46Z). It carries three corrections from the independent review:
1. **Sanitised output.** Only aggregate counts and date ranges are printed. Positions, quantities, exposures, P&L and journal rows are never printed; the detailed reports stay private in `/tmp/m21_op1_s2`.
2. **Explicit S2-P states.** `NOT_VERIFIED table_absent | row_absent | invalid_json | not_an_object | query_failed` when the policy cannot be read. Authorisation windows are evaluated against the current UTC time, with the deployed rules: `missing`, `malformed`, non-UTC or `expired` deny. Any lane still `AUTHORISED` is flagged.
3. **Disk-space preflight.** At least 2 × the database size + 1 GiB must be free under `/tmp`, or it stops before anything is downloaded or copied.

The run ends `SESSION 2 COMPLETE` only if every step succeeded and no lane is authorised. Otherwise it ends `FINISHED WITH WARNINGS` (exit 3).

```bash
runuser -u nobody -- env HOME=/tmp PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin bash -s <<'S2_EOF'
umask 077
O=/tmp/m21_op1_s2
H=$(hostname -s)
mask() { sed -E -e "s/\b(DU|DF|U)[0-9]{5,}\b/\1***/g" -e "s/$H/<host>/g" "$@"; }
WARN=""

echo "===== S2-0 preflight: account, fresh directory, disk space (as $(id -un), read-only)"
[ "$(id -un)" = nobody ] || { echo "STOP: not running as nobody"; exit 1; }
[ -e "$O" ] && { echo "STOP: $O already exists (an earlier Session 2 run): report it, do not reuse it"; exit 1; }
D=/opt/algo-trader/data
total=0
for f in signals paper_lifecycles historical; do
  for p in "$D/$f.db" "$D/$f.db-wal"; do
    if [ -e "$p" ]; then
      s=$(stat -c %s "$p" 2>/dev/null) || { echo "STOP: cannot read the size of $p as nobody - nothing was copied"; exit 1; }
      total=$((total + s)); echo "size $(basename "$p") = $((s / 1048576)) MiB"
    fi
  done
done
avail_kb=$(df -Pk /tmp | awk 'NR==2 {print $4}')
case "$avail_kb" in ''|*[!0-9]*) echo "STOP: cannot read free space under /tmp - nothing was copied"; exit 1;; esac
avail=$((avail_kb * 1024)); need=$((2 * total + 1073741824))
echo "databases=$((total / 1048576)) MiB  free_under_/tmp=$((avail / 1048576)) MiB  required=$((need / 1048576)) MiB (2 x databases + 1 GiB margin)"
[ "$avail" -ge "$need" ] || { echo "STOP: not enough free space under /tmp - nothing was downloaded or copied"; exit 1; }
mkdir "$O" && cd "$O" || { echo "STOP: cannot create $O"; exit 1; }

echo "===== S2-1 Session 2: pinned scripts, database copies, private reports"
DOCS=14b641c635fe3cb80815292b8bba940f5885b7f0
curl -fsSL -o docs.tgz "https://codeload.github.com/mikemitakidis/algotrading/tar.gz/$DOCS" || { echo "STOP: DOWNLOAD FAILED"; exit 1; }
mkdir docs && tar -xzf docs.tgz -C docs --strip-components=1 || { echo "STOP: UNPACK FAILED"; exit 1; }
sha256sum -c - <<SUMS || { echo "STOP: CHECKSUM MISMATCH - nothing was run"; exit 1; }
e2c5c25609f0f3391a282b1db78c335ab6c667d80c09a65048cc352182b4fc29  docs/docs/runbooks/op1/op1_deploy_discovery.sh
3df72bc0a788d3587ccd2fe60b43e9453dc3af7058f655fff41255819e16e8ce  docs/docs/runbooks/op1/op1_readonly.sh
f3de98868ae779c11c6f8b991ac6dd5ba1cf925549aee7ae6e2a58811c250d32  docs/docs/runbooks/op1/op1_runtime_status.sh
dcaf78169bfadd1aacf9e2f0275deef4ba73f05d58624284d14052957f9f800e  docs/docs/runbooks/op1/op1_parquet_basis.py
f7022b398dafe2e11a2007092b90a602abe18cdc908fd615db2ac5b397f21f64  docs/docs/runbooks/op1/op1_sql.py
eec185efca64b310b60e2ea981270839fbe8dbfaf3b76dddf12f8aedbd7634ab  docs/docs/runbooks/sql/old_paper_journal.sql
fe4195c5d50d7173e9b70d4e1ef3d2766b52bc765a2f9f908bcd290cf46d2f36  docs/docs/runbooks/sql/old_paper_signals.sql
030df4bb55d5ae8a4dcfbde04da4c80b7235b52d8fade83925bf34cf2cc48abb  docs/docs/runbooks/sql/op1_m16_inventory.sql
SUMS
OP1_OUT="$O" bash docs/docs/runbooks/op1/op1_readonly.sh > /dev/null < /dev/null; rc=$?
echo "op1_exit=$rc"
if [ "$rc" != 0 ]; then
  echo "STOP: Session 2 stopped itself (exit $rc). Nothing else runs. Send only this op1.log; do not use or share any *_copy.db:"
  mask op1.log; exit 1
fi
mask op1.log

echo "===== S2-P broker allocation policy (verified copy only; rules mirrored from the deployed code at 015d2ce)"
if grep -qE "copied signals\.db -> signals_copy\.db .*quick_check=ok" op1.log; then
python3 -I - "$O/signals_copy.db" <<'PY'
import json, sqlite3, sys
from datetime import datetime, timedelta, timezone
NOW = datetime.now(timezone.utc)
print("evaluated_at_utc=%s, against the policy as stored when this session copied signals.db"
      % NOW.isoformat(timespec="seconds"))
def done(status, code):
    print("S2P_STATUS=%s" % status); sys.exit(code)
try:
    c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
    t = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='portfolio_risk_state'").fetchone()
    if t is None:
        print("portfolio_risk_state table ABSENT: the bot would create it and use DEFAULT_POLICY (auto-trading disabled everywhere)")
        done("NOT_VERIFIED table_absent", 10)
    r = c.execute("SELECT value FROM portfolio_risk_state WHERE key='broker_allocation_policy'").fetchone()
except sqlite3.Error as e:
    done("NOT_VERIFIED query_failed %s" % type(e).__name__, 10)
if r is None or r[0] is None:
    print("broker_allocation_policy row ABSENT: the bot would use DEFAULT_POLICY (auto-trading disabled everywhere)")
    done("NOT_VERIFIED row_absent", 10)
try:
    p = json.loads(r[0])
except (TypeError, ValueError) as e:
    print("broker_allocation_policy JSON INVALID (%s): the bot would use DEFAULT_POLICY" % type(e).__name__)
    done("NOT_VERIFIED invalid_json", 10)
if not isinstance(p, dict):
    print("broker_allocation_policy is not a JSON object: the bot would use DEFAULT_POLICY")
    done("NOT_VERIFIED not_an_object", 10)

def window(b):  # same rules as _authorization_expiry_reason
    raw = b.get("auto_trading_enabled_until_utc")
    if raw is None: return "missing", None
    if not isinstance(raw, str): return "malformed", repr(raw)[:40]
    try: u = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError: return "malformed", raw[:40]
    if u.tzinfo is None or u.utcoffset() != timedelta(0): return "malformed", raw[:40]
    return ("active" if u > NOW else "expired"), raw

for blk in ("global", "ibkr_paper", "ibkr_live", "ibkr", "etoro"):
    b = p.get(blk)
    if not isinstance(b, dict):
        print("%-10s ABSENT or not an object" % blk); continue
    w = "%s (until %s)" % window(b) if blk in ("global", "ibkr_paper", "ibkr_live") else "no time box"
    print("%-10s auto_trading_enabled=%r kill_switch=%r window=%s"
          % (blk, b.get("auto_trading_enabled"), b.get("kill_switch"), w))
rt = p.get("routing")
print("routing    allowed_brokers=%r etoro_live_enabled=%r" % (rt.get("allowed_brokers"), rt.get("etoro_live_enabled"))
      if isinstance(rt, dict) else "routing    ABSENT or not an object")

def decide(name):  # mirror of determine_signal_only_reason (015d2ce)
    g = p.get("global")
    if not isinstance(g, dict): return "SIGNAL_ONLY policy_missing_or_invalid"
    if g.get("kill_switch") is True: return "SIGNAL_ONLY kill_switch_active_global"
    if g.get("auto_trading_enabled") is not True: return "SIGNAL_ONLY auto_trading_disabled_global"
    w = window(g)[0]
    if w != "active": return "SIGNAL_ONLY auto_trading_global_" + ("expired" if w == "expired" else "expiry_" + w)
    rt = p.get("routing")
    if not isinstance(rt, dict): return "SIGNAL_ONLY policy_missing_or_invalid"
    al = rt.get("allowed_brokers")
    if not isinstance(al, list) or name not in al: return "SIGNAL_ONLY broker_not_in_allowed_brokers"
    if name == "etoro_real" and rt.get("etoro_live_enabled") is not True: return "SIGNAL_ONLY etoro_live_disabled_policy"
    blk = name if name in ("ibkr_paper", "ibkr_live") else ("etoro" if name.startswith("etoro") else None)
    if blk:
        b = p.get(blk)
        if not isinstance(b, dict): return "SIGNAL_ONLY policy_missing_or_invalid"
        if b.get("kill_switch") is True: return "SIGNAL_ONLY kill_switch_active_broker"
        if b.get("auto_trading_enabled") is not True: return "SIGNAL_ONLY auto_trading_disabled_broker"
        if blk != "etoro":
            w = window(b)[0]
            if w != "active": return "SIGNAL_ONLY auto_trading_lane_" + ("expired" if w == "expired" else "expiry_" + w)
    return "AUTHORISED: submissions would be attempted"

authorised = []
for name in ("paper", "ibkr_paper", "ibkr_live", "etoro_paper", "etoro_real"):
    d = decide(name); print("policy decision for %-11s %s" % (name + ":", d))
    if d.startswith("AUTHORISED"): authorised.append(name)
if authorised:
    print("WARNING: auto-trading is AUTHORISED by the stored policy for: %s" % ", ".join(authorised))
    done("READ_OK authorised_lanes=%s" % ",".join(authorised), 12)
done("READ_OK no lane authorised (every broker signal-only by policy)", 0)
PY
  prc=$?
  [ "$prc" = 0 ] || WARN="$WARN S2-P(exit $prc)"
else
  echo "S2P_STATUS=NOT_VERIFIED no verified signals_copy.db"; WARN="$WARN S2-P(no copy)"
fi

echo "===== S2-S sanitised aggregates (counts and date ranges only; no symbols, positions, quantities, exposures or P&L)"
python3 -I - "$O" <<'PY'
import os, sqlite3, sys
O = sys.argv[1]; FAIL = []
def open_copy(n):
    p = os.path.join(O, n)
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True) if os.path.exists(p) else None
def show(c, need, title, sql, args=()):
    print("-- " + title)
    if c is None: print("   copy absent"); return
    try:
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        miss = [t for t in need if t not in have]
        if miss: print("   table(s) absent: " + ", ".join(miss)); return
        cur = c.execute(sql, args); rows = cur.fetchall()
    except sqlite3.Error as e:
        FAIL.append(title); print("   QUERY_FAILED %s: %s" % (type(e).__name__, str(e)[:100])); return
    print("   " + " | ".join(d[0] for d in cur.description))
    for r in rows[:40]: print("   " + " | ".join("-" if v is None else str(v) for v in r))
    if len(rows) > 40: print("   ... %d more rows in the private report" % (len(rows) - 40))
    if not rows: print("   (no rows)")
J = "2026-06-01"
h = open_copy("historical_copy.db")
print("## HISTORICAL DATA (historical_copy.db)")
show(h, ["historical_symbols"], "symbols by asset class and active flag",
     "SELECT asset_class, is_active, COUNT(*) AS symbols FROM historical_symbols GROUP BY 1, 2 ORDER BY 1, 2")
show(h, ["historical_coverage"], "coverage by provider and timeframe",
     "SELECT provider, timeframe, COUNT(*) AS symbols, SUM(bar_count) AS bars, MIN(first_ts_utc) AS earliest_utc, "
     "MAX(last_ts_utc) AS latest_utc, SUM(missing_count) AS missing_bars, SUM(duplicate_count) AS duplicate_bars, "
     "SUM(missing_count > 0) AS symbols_with_gaps, COALESCE(source_timeframe, '-') AS derived_from "
     "FROM historical_coverage GROUP BY provider, timeframe, source_timeframe ORDER BY 1, 2")
show(h, ["historical_coverage"], "quality and freshness status counts",
     "SELECT provider, timeframe, quality_status, freshness_status, COUNT(*) AS symbols, MAX(last_refresh_at_utc) AS last_refresh_utc "
     "FROM historical_coverage GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4")
show(h, ["historical_quality_events"], "quality events by severity and kind",
     "SELECT severity, kind, COUNT(*) AS n, SUM(created_at_utc >= ?) AS since_june, MAX(created_at_utc) AS last_utc "
     "FROM historical_quality_events GROUP BY 1, 2 ORDER BY 1, 2", (J,))
show(h, ["historical_refresh_runs"], "refresh runs",
     "SELECT provider, mode, status, COUNT(*) AS runs, SUM(symbols_failed) AS symbols_failed, "
     "SUM(symbols_rate_limited) AS rate_limited, MAX(started_at_utc) AS last_utc FROM historical_refresh_runs GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")
show(h, ["historical_refresh_lock"], "refresh lock held now",
     "SELECT COUNT(*) AS rows, SUM(owner_host IS NOT NULL) AS held FROM historical_refresh_lock")
s = open_copy("signals_copy.db")
print("## OLD PAPER ACTIVITY SINCE %s (signals_copy.db; rows are our code's CLAIMS, not broker proof)" % J)
show(s, ["signals"], "candidate signals by direction, route and strategy version",
     "SELECT direction, COALESCE(NULLIF(route, ''), '-') AS route, strategy_version, COUNT(*) AS n, MIN(timestamp) AS first_utc, "
     "MAX(timestamp) AS last_utc FROM signals WHERE timestamp >= ? GROUP BY 1, 2, 3 ORDER BY 1, 2, 3", (J,))
show(s, ["candidate_snapshots"], "candidate snapshots by stage",
     "SELECT stage, COUNT(*) AS n, MIN(timestamp) AS first_utc, MAX(timestamp) AS last_utc FROM candidate_snapshots "
     "WHERE timestamp >= ? GROUP BY 1 ORDER BY 1", (J,))
show(s, ["execution_intents"], "order intents by broker and status, with identity-evidence counts",
     "SELECT broker, status, COUNT(*) AS n, SUM(COALESCE(broker_order_id, '') <> '') AS with_broker_order_id, "
     "SUM(submitted_at IS NOT NULL) AS with_submitted_at, SUM(fill_price IS NOT NULL) AS with_fill_price, "
     "MIN(timestamp) AS first_utc, MAX(timestamp) AS last_utc FROM execution_intents WHERE timestamp >= ? "
     "GROUP BY 1, 2 ORDER BY 1, 2", (J,))
show(s, ["execution_intents"], "intents with a live-scope broker name (must be zero)",
     "SELECT COUNT(*) AS live_scope_intents FROM execution_intents WHERE timestamp >= ? AND "
     "(lower(broker) LIKE '%live%' OR lower(broker) IN ('etoro_real', 'etoro_live'))", (J,))
show(s, ["signal_outcomes"], "signal outcomes (price-path outcomes, not fills)",
     "SELECT COALESCE(resolution_method, '-') AS method, COALESCE(outcome, '-') AS outcome, (intent_id IS NOT NULL) AS linked_to_intent, "
     "COUNT(*) AS n FROM signal_outcomes WHERE COALESCE(resolved_at, '') >= ? OR outcome = 'OPEN' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3", (J,))
show(s, ["broker_positions"], "broker position snapshots per scope (counts only)",
     "SELECT broker_scope, COUNT(DISTINCT exposure_batch_id) AS batches, MIN(fetched_at_utc) AS first_utc, MAX(fetched_at_utc) AS last_utc "
     "FROM broker_positions WHERE date >= ? GROUP BY 1 ORDER BY 1", (J,))
show(s, ["broker_positions"], "latest snapshot per scope: does it record any non-zero position? (no symbols, quantities or exposure)",
     "SELECT p.broker_scope, p.fetched_at_utc AS latest_utc, COUNT(*) AS rows, SUM(COALESCE(p.qty, 0) <> 0) AS nonzero_positions "
     "FROM broker_positions p JOIN (SELECT broker_scope, MAX(fetched_at_utc) AS m FROM broker_positions GROUP BY 1) l "
     "ON l.broker_scope = p.broker_scope AND l.m = p.fetched_at_utc GROUP BY 1, 2 ORDER BY 1")
show(s, ["risk_decisions"], "M14 risk decisions by scope, action, result and source",
     "SELECT broker_scope, requested_action, result, source, COUNT(*) AS n, MAX(taken_at) AS last_utc FROM risk_decisions "
     "WHERE taken_at >= ? GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4", (J,))
show(s, ["daily_state_per_broker"], "M14 daily state per scope (flags only; no P&L or capital values)",
     "SELECT d.broker_scope, COUNT(*) AS days_since_june, MAX(d.date) AS latest_date, "
     "(SELECT open_positions > 0 FROM daily_state_per_broker x WHERE x.broker_scope = d.broker_scope ORDER BY x.date DESC LIMIT 1) AS latest_has_open_positions, "
     "(SELECT daily_loss_block_active FROM daily_state_per_broker x WHERE x.broker_scope = d.broker_scope ORDER BY x.date DESC LIMIT 1) AS latest_daily_loss_block "
     "FROM daily_state_per_broker d WHERE d.date >= ? GROUP BY 1 ORDER BY 1", (J,))
show(s, ["risk_snapshots"], "M14 risk snapshots",
     "SELECT source, policy_version, COUNT(*) AS snapshots, MIN(taken_at) AS first_utc, MAX(taken_at) AS last_utc FROM risk_snapshots "
     "WHERE taken_at >= ? GROUP BY 1, 2 ORDER BY 1, 2", (J,))
l = open_copy("paper_lifecycles_copy.db")
print("## WS2/D2 LOOP JOURNAL (paper_lifecycles_copy.db)")
show(l, ["loop_orders"], "loop orders by data source and state (no prices)",
     "SELECT data_source, state, COUNT(*) AS n, SUM(broker_perm_id IS NOT NULL) AS with_perm_id, SUM(entry_fill_price IS NOT NULL) AS with_entry_fill, "
     "SUM(exit_fill_price IS NOT NULL) AS with_exit_fill, MIN(planned_at_utc) AS first_utc, MAX(planned_at_utc) AS last_utc "
     "FROM loop_orders GROUP BY 1, 2 ORDER BY 1, 2")
show(l, ["paper_trade_outcomes"], "closed-trade outcome records (counts only; no P&L)",
     "SELECT data_source, record_kind, is_edge_outcome, COUNT(*) AS n, SUM(fees IS NOT NULL) AS with_fees, "
     "MIN(exit_filled_at_utc) AS first_exit_utc, MAX(exit_filled_at_utc) AS last_exit_utc FROM paper_trade_outcomes GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")
print("S2S_STATUS=%s" % ("OK" if not FAIL else "QUERY_FAILED in: " + "; ".join(FAIL)))
sys.exit(0 if not FAIL else 11)
PY
src=$?; [ "$src" = 0 ] || WARN="$WARN S2-S(exit $src)"

echo "===== S2-W watchdog evidence and the 30 Jun signal-only reason (bot.log, read-only)"
L=/opt/algo-trader/logs/bot.log
if [ -r "$L" ]; then
  echo "tick_errors=$(grep -c '\[GW-WATCHDOG\] tick error' "$L") no_loop=$(grep -c "no current event loop in thread .gw-watchdog." "$L")"
  echo "first=$(grep -m1 '\[GW-WATCHDOG\] tick error' "$L" | cut -c1-23) last=$(grep '\[GW-WATCHDOG\] tick error' "$L" | tail -n1 | cut -c1-23)"
  grep -m1 -A12 '\[GW-WATCHDOG\] tick error' "$L" | cut -c1-160 | mask
  grep -E '\[BROKER\] (wrapping|policy unavailable)' "$L" | tail -n 2 | cut -c1-200 | mask
else
  echo "S2W_STATUS=NOT_AVAILABLE bot.log not readable as nobody"; WARN="$WARN S2-W"
fi

echo "===== S2-R non-sensitive status files (masked; first 150 lines each)"
for f in parquet_basis_summary.txt env_metadata.txt runtime_status.txt deploy_discovery.txt; do
  [ -f "$f" ] || { echo "===== $f: absent"; continue; }
  echo "===== $f ($(wc -l < "$f") lines)"; mask "$f" | head -n 150
done
echo "===== private (NOT printed, not to be shared or uploaded): detailed reports and database copies in $O"
ls -l "$O" | awk 'NR > 1 && $NF !~ /^docs/ {print "   " $1, $5, $NF}'
if [ -z "$WARN" ]; then
  echo "===== SESSION 2 COMPLETE: every step finished; policy read and parsed; no lane authorised"
else
  echo "===== SESSION 2 FINISHED WITH WARNINGS:$WARN (S2-P exit 10 = policy NOT verified, 12 = a lane is AUTHORISED; S2-S exit 11 = a summary query failed). Review the lines above."
  exit 3
fi
S2_EOF
echo "session2_exit=$?"
```

**It stops by itself, and nothing further runs, when:**
- the account is not `nobody`, or the free space under `/tmp` is below the requirement (checked before any download or copy);
- the download or unpack fails, or any checksum is wrong (nothing is run);
- `/tmp/m21_op1_s2` already exists (each run needs a fresh directory);
- `op1_readonly.sh` exits with 2 (refused), 3 (a DB copy failed) or 4 (`quick_check` not ok). Then only the masked `op1.log` is printed, and no `*_copy.db` may be used or shared;
- S2-P is about to run without a verified `signals_copy.db`.

**Offline test of this exact block** (mock VPS; the `curl` stub serves the real `14b641c` tarball from `git archive`):

| Case | Result |
|---|---|
| normal run | `op1_exit=0`; three copies made and checked; S2-P, S2-W, S2-R printed; outputs mode 700/600, owned by `nobody` |
| run again | STOP: the directory exists |
| a database copy fails | `op1_exit=3`; STOP; only `op1.log` printed |
| a tampered script in the tarball | `CHECKSUM MISMATCH`; nothing run |

**What it answers:**
- historical coverage and completed-bar/timestamp integrity;
- old paper activity since 1 June: signals, intents, claimed fills, positions/outcomes, M14 records;
- inputs for the E5 Flex reconciliation;
- S2-P: the current allocation-policy gates;
- S2-W: the watchdog-error count, its period and the first full traceback, plus the reason the bot was signal-only on 30 Jun.

**Operator inputs:** the two IBKR Flex exports (paper and live, 1 June → the export date), provided separately, as specified in runbook OP1.6.

**Results** (sanitised aggregates as reported by the operator after an independent review; recorded 2026-11-05T17:37Z UTC). The detailed reports and DB copies stay private on the VPS in `/tmp/m21_op1_s2`.

| Item | Result | Class |
|---|---|---|
| Run | `session2_exit=0`; 8/8 checksums; all three DB copies passed `quick_check` | CONFIRMED |
| Broker policy (S2-P) | `READ_OK`: **all five lanes signal-only** (no lane authorised) | CONFIRMED (as of the copy) |
| Watchdog (S2-W) | **27,994** `no current event loop in thread 'gw-watchdog'` errors in `bot.log`. This confirms §2a; the fix is A18, PR #35. | CONFIRMED |
| Historical data | **very thin**: one symbol in the M16 store, last refreshed in June 2026 | CONFIRMED. M21 B-1 and C-1…C-3 need a real data backfill (WP11/E3 path); this is not an A-0 item. |
| WS2/D2 journal (`paper_lifecycles.db`) | the journal tables are absent, so the 6 Jul supervised D2 records are **NOT_VERIFIED** from this database | evidence gap; the Flex statement is the proof for 6 Jul |
| Runtime | bot inactive and still enabled at boot; gateway failed (stopped); dashboard active; kill switch inactive | CONFIRMED (matches Session 1 and H1/P2) |
| Secrets hygiene | a `.env` backup is **world-readable (644)** | CONFIRMED. Register task **E07**; fixed by A-0s (permissions only). |
| Current IBKR positions | **not established** by Session 2 | must be confirmed at IBKR before A-0 (§4) |

Session 2 is closed and will not be repeated.

## 6. Roadmap reconciliation (in this PR)

- **`ROADMAP.md` "Roadmap v3.0" (R1–R16):**
  - authority;
  - sources;
  - the numbering reconciliation table;
  - the M21 dimension summary;
  - M22 (M22.1–M22.6; C01–C11; the sub-number mapping is a proposal);
  - M23 (M23.1–M23.5; D01–D10);
  - **M24/M25 UNDEFINED**;
  - Track A / Track B with dependencies;
  - the status vocabulary;
  - the version/timestamp convention;
  - the update protocol;
  - the handoff block;
  - operator decisions RD-1…RD-8;
  - discrepancies;
  - the append-only changelog.
- **`MILESTONE_STATUS.md`:**
  - a status 3.0.1 block (the 29 Oct block is kept as history);
  - a vocabulary note;
  - a 24/25 row;
  - the pre-June future table labelled historical.
- **Untouched:** the 71 register tasks, the M21 Finish Line, every historical milestone requirement. No renumbering.

## 7. Open questions for the review of revision 4

1. Is the attestation gate (`A0_POSITIONS`, counts only, both IBKR accounts, positions **and** working orders) sufficient as the position confirmation?
2. Is A-0s (owner-only modes on untracked `.env*` files, with no content read) the right minimal E07 fix?
3. Any objection to approving A-0 as defence in depth, given that S2-P already makes all five lanes signal-only?

## 8. Returned items

| Item | Value |
|---|---|
| Exact UTC timestamp | 2026-11-05T17:37Z UTC (same in Europe/London, GMT) |
| GitHub `main` | `80b4343db4a22413aa670efd32e24952d53e532e` [GH] |
| VPS deployed | `015d2cea927b9b4db3ee65604e0b4ebb58211e61` [VPS-R] |
| Roadmap / status | v3.0 (DRAFT) / status 3.0.4 |
| Documentation PR | #34 (draft); head SHA in the PR record and Issue #3 (R11) |
| Watchdog fix | **A18**, PR #35 (draft), head `da56e12a9e9baec8634b618cb936be1f40a84a78` |
| Session 2 | **CLOSED**: exit 0, 8/8 checksums, three copies OK, all five lanes signal-only |
| Gate A-0 | revision 4, **ready for approval after the IBKR position/working-order confirmation**; 21 offline cases pass |
| Next operational step | the operator confirms IBKR positions and working orders (paper + live), then approves Gate A-0 rev 4 |
| Next engineering task | the review of PR #35 (A18); then C01 (Track B) after approval to start |
