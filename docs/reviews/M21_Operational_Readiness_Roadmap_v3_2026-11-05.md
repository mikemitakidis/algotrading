# M21 operational readiness + roadmap v3.0: consolidated report for independent review

**For independent review (ChatGPT).**

- **Prepared by:** Claude (Claude Code session).
- **Written:** 2026-11-05T12:04Z UTC (12:04 GMT Europe/London).
- **Revision 2:** 2026-11-05T15:24Z UTC (same in Europe/London, GMT). Adds the H1/P1/P2 evidence, the `gw-watchdog` analysis and the corrections to Gate A-0.
- **GitHub `main`:** `80b4343db4a22413aa670efd32e24952d53e532e` [GH].
- **VPS deployed:** `015d2cea927b9b4db3ee65604e0b4ebb58211e61` [VPS-R].
- **Roadmap:** v3.0 (DRAFT); status 3.0.2.
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
- recorded as a **new register task** (decision D-9).

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

## 4. Gate A-0 proposal: containment, no deployment (NOT executed)

**Approval and scope:**
- Run only after the operator's separate written approval, in the root terminal.
- A-0 changes three things, all reversible: the kill-switch file (A-0a), the bot unit's boot link (A-0b) and a drop-in start guard (A-0b2).
- **A-0c is now a no-op.** P1 found no `deploy.sh` line in root's crontab. The block checks again and prints "nothing changed".
- It does **not** stop or start any process. It does **not** touch `.env`, the databases, git, the dashboard, the gateway, `sync.sh` or Vercel.

**Purpose (from the assignment):**
- kill switch on;
- no boot-time bot activation;
- the legacy cron entry removed if it exists;
- `sync.sh` kept stopped;
- the state preserved;
- verified rollback.

**Correction to the Session 1 report §12-3b:** `systemctl disable` alone is **not enough**.
- `deploy.sh` respects a disabled unit (CONFIRMED, `deploy.sh` at `015d2ce` lines ~104–117).
- But `deploy.sh` then launches `sync.sh`.
- `sync.sh` at `015d2ce` runs `systemctl restart algo-trader.service` whenever `origin/main` differs from the checkout. It does this **regardless of the enabled state** (CONFIRMED, `sync.sh` lines ~45–62).
- `origin/main` (`80b4343`) already differs from the checkout (`015d2ce`). So if `deploy.sh` runs for any reason, the bot starts within about 60 s on `80b4343`.
- `systemctl mask` is not available either: the unit file is installed in `/etc/systemd/system`, and `mask` refuses to replace it.

A-0 therefore proposes **A-0b2**, a drop-in `ConditionPathExists=` guard. Every systemd start of the bot is then *skipped* until an allow-file exists: boot, `deploy.sh`, the `sync.sh` restart, and a manual `systemctl start`. Nothing fails and nothing else changes.

**What the kill switch covers (A-0a), in light of the new evidence:**
- It blocks every IBKR submission **and** the IBKR flatten path (CONFIRMED, `bot/brokers/ibkr_broker.py` ~240 and ~640).
- If IBKR paper positions are open, they must be closed manually in the Gateway/TWS while it is on.

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

**A-0's scope:** A-0 protects against **automatic and accidental** activation (reboot, `deploy.sh`/`sync.sh`, `needrestart`, a stray `systemctl start`). It **does not** protect against an authenticated dashboard user, who can start the bot (five routes) and deactivate the kill switch. Two ways to close that:
- the operator's rule, until OP3: do not use those five dashboard actions, the kill-switch release, or the broker-allocation controls;
- optionally **A-0f**: stop the dashboard as well (`systemctl stop algo-trader-dashboard.service`; undo with `systemctl start`). The cost is losing the dashboard as the operator's view until Gate A-2.

**Not recommended by default.** Hardening the dashboard (start routes, root service, exposure) is M22 C03 / M15.3 scope, and a code change.

### 4.1 Commands (one block; runs in a child shell, so a STOP or a failure ends only the block, not the terminal)

```bash
bash -euo pipefail <<'GATEA0'
cd /opt/algo-trader
TS=$(date -u +%Y%m%dT%H%M%SZ)
SNAP=/var/lib/algo-trader/gate_a0/$TS
install -d -m 0700 "$SNAP"
echo "snapshot dir: $SNAP"

# 0. Preconditions (read-only). Any STOP means: do nothing more, report the output.
RUN_RE='[p]ython3?[^ ]* +([^ ]*/)?main\.py( |$)|[b]ash +([^ ]*/)?sync\.sh( |$)'   # bot (any launcher) or sync.sh
if ps -eo pid,args | grep -E "$RUN_RE"; then echo "STOP: main.py or sync.sh is running"; exit 2; fi
[ "$(systemctl is-active algo-trader.service || true)" = inactive ] || { echo "STOP: bot unit not inactive"; exit 2; }
git rev-parse HEAD > "$SNAP/head.txt"
[ "$(cat "$SNAP/head.txt")" = 015d2cea927b9b4db3ee65604e0b4ebb58211e61 ] || { echo "STOP: HEAD is not 015d2ce"; exit 2; }
git status --porcelain --untracked-files=no > "$SNAP/git_status.txt"
[ ! -s "$SNAP/git_status.txt" ] || { echo "STOP: modified tracked files"; exit 2; }

# 1. Preserve (nothing is printed; .env is hashed, not copied)
cp -p data/kill_switch.json "$SNAP/kill_switch.json.before"
[ -f data/bot_state.json ] && cp -p data/bot_state.json "$SNAP/bot_state.json.before" || echo absent > "$SNAP/bot_state.absent"
sha256sum .env > "$SNAP/env.sha256"
stat -c '%a %U %G %s %y %n' .env data/kill_switch.json > "$SNAP/files.stat"
systemctl show algo-trader.service algo-trader-dashboard.service ibgateway.service \
  -p Id,UnitFileState,ActiveState,SubState,FragmentPath,DropInPaths > "$SNAP/units.before"
ls -l /etc/systemd/system/multi-user.target.wants/ > "$SNAP/wants.before"
crontab -l -u root > "$SNAP/root_crontab.before" 2> "$SNAP/root_crontab.err" || true
chmod 600 "$SNAP"/*

# A-0a. Kill switch ON (the repository's own function; writes data/kill_switch.json only)
venv/bin/python3 -I -c 'import sys; sys.path.insert(0, "/opt/algo-trader")
from bot.kill_switch import activate_kill_switch, get_kill_switch_state
activate_kill_switch("Gate A-0 containment, operator-approved, " + sys.argv[1])
s = get_kill_switch_state(); print("kill_switch_active=%s" % s["active"]); sys.exit(0 if s["active"] is True else 3)' "$TS"

# A-0b. No start at boot
systemctl disable algo-trader.service
[ "$(systemctl is-enabled algo-trader.service || true)" = disabled ] || { echo "STOP: unit still enabled"; exit 3; }

# A-0b2. No systemd start by any path until an allow-file exists (covers deploy.sh -> sync.sh -> systemctl restart)
install -d -m 0755 /etc/systemd/system/algo-trader.service.d
printf '[Unit]\n# Gate A-0 %s: the bot starts only while this file exists.\nConditionPathExists=/var/lib/algo-trader/ALLOW_BOT_START\n' "$TS" \
  > /etc/systemd/system/algo-trader.service.d/gate-a0.conf
[ ! -e /var/lib/algo-trader/ALLOW_BOT_START ] || { echo "STOP: allow-file exists"; exit 3; }
systemctl daemon-reload

# A-0c. Only if P1 found a deploy.sh line in root's crontab
if grep -q 'deploy\.sh' "$SNAP/root_crontab.before"; then
  grep -v 'deploy\.sh' "$SNAP/root_crontab.before" > "$SNAP/root_crontab.after" || true
  chmod 600 "$SNAP/root_crontab.after"
  crontab -u root "$SNAP/root_crontab.after"
  echo "A-0c: removed $(grep -c 'deploy\.sh' "$SNAP/root_crontab.before") crontab line(s)"
else
  echo "A-0c: no deploy.sh line in root's crontab; nothing changed"
fi

# A-0d. sync.sh stays stopped: no action (checked in step 0 and again below). A-0e. Gateway: no action.

# 2. Verify (read-only)
python3 -I -c 'import json; print("kill_switch_active=%s" % json.load(open("/opt/algo-trader/data/kill_switch.json"))["active"])'
echo "bot enabled: $(systemctl is-enabled algo-trader.service || true)"
systemctl show -p DropInPaths algo-trader.service
systemctl is-active algo-trader.service algo-trader-dashboard.service ibgateway.service || true
echo "crontab deploy.sh lines: $(crontab -l -u root 2>/dev/null | grep -c 'deploy\.sh' || true)"
ps -eo pid,args | grep -E "$RUN_RE" || echo "no main.py or sync.sh process"
[ "$(git rev-parse HEAD)" = "$(cat "$SNAP/head.txt")" ] && echo "HEAD unchanged" || echo "HEAD CHANGED"
sha256sum -c "$SNAP/env.sha256"
echo "GATE A-0 DONE, snapshot $SNAP"
GATEA0
echo "gate_a0_exit=$?"
```

**Expected result:**
- `kill_switch_active=True`;
- `bot enabled: disabled`;
- `DropInPaths=/etc/systemd/system/algo-trader.service.d/gate-a0.conf`;
- `inactive` / `active` / `failed` for bot / dashboard / gateway (unchanged);
- `crontab deploy.sh lines: 0`;
- no `main.py`/`sync.sh` process;
- `HEAD unchanged`;
- `.env: OK`;
- `gate_a0_exit=0`.

Claude has **not** run this block on the VPS. It was exercised in a mock of the VPS layout, with stub `systemctl`/`crontab` and the real `bot/kill_switch.py` from `015d2ce`; results are in §4.2. It must be reviewed before use.

### 4.2 Offline test of the block (mock, not the VPS)

**Mock layout:**
- `/opt/algo-trader` was a detached checkout of `015d2ce`, carrying the real `bot/kill_switch.py`.
- `data/kill_switch.json` matched the observed content (inactive, "test cleanup").
- `systemctl` and `crontab` were stubs that record calls and state.
- Every test ran from a script file, so the test harness's own command line could not match the process check.

| Test | Expected | Result |
|---|---|---|
| T1: cron has `@reboot … deploy.sh` and an unrelated line | `exit=0`; kill switch True; disabled; drop-in present; one line removed, the other kept; HEAD unchanged; `.env: OK` | pass |
| T2: rollback in reverse order | crontab byte-identical; drop-in removed; enabled; `kill_switch.json` byte-identical (inactive) | pass |
| T3: a `python3 …/main.py` process running | `STOP`, `exit=2`, nothing changed | pass |
| T4: a `bash …/sync.sh` process running | `STOP`, `exit=2`, nothing changed | pass |
| T5: HEAD ≠ `015d2ce` | `STOP`, `exit=2`, nothing changed | pass |
| T6: a modified tracked file | `STOP`, `exit=2`, nothing changed | pass |
| T7: no root crontab | `A-0c: … nothing changed`, `exit=0` | pass |
| T8: crontab without `deploy.sh` | crontab untouched, `exit=0` | pass |
| T9: a `dashboard/app.py` process running | not treated as the bot, `exit=0` | pass |

**Not testable offline:** systemd's behaviour when a condition fails, because the stubs cannot show it.
- Per systemd's documentation, a failing `Condition…=` *skips* the start silently and does not fail the unit. An `Assert…=` would fail the start job ([systemd-devel thread quoting systemd.unit(5)](https://lists.freedesktop.org/archives/systemd-devel/2022-March/047575.html)).
- After A-0 the operator verifies only with `systemctl show -p DropInPaths`. **No test start is attempted.**

### 4.3 Rollback (each step separately; reverse order; each needs approval)

| Step | Undo | Restore only when |
|---|---|---|
| A-0c | `crontab -u root "$SNAP/root_crontab.before"` (the exact previous crontab) | not recommended. The legacy `@reboot deploy.sh` path is superseded by systemd (M15.0). Only if A-0c demonstrably caused a problem. |
| A-0b2 | `rm /etc/systemd/system/algo-trader.service.d/gate-a0.conf && rmdir --ignore-fail-on-non-empty /etc/systemd/system/algo-trader.service.d && systemctl daemon-reload`. For a single supervised start instead: `touch /var/lib/algo-trader/ALLOW_BOT_START`, then remove it after. | at OP3 activation (approved), or for a supervised start the operator approves |
| A-0b | `systemctl enable algo-trader.service` (re-creates the boot link; starts nothing) | at OP3, when the bot is meant to run unattended |
| A-0a | `cp -p "$SNAP/kill_switch.json.before" data/kill_switch.json` (exact restore), or `deactivate_kill_switch("…")` | **only at OP3**, after OP2 gateway acceptance, with explicit approval. Never as part of a merge or deployment. |
| Partial failure | if the block stops after A-0a or A-0b, undo the completed steps in reverse order, or keep them and report (all of them are safe states) | — |

**Restore conditions:** the operator approves OP3, the gateway passes OP2, and the deployed tree is the reviewed merge.

**What A-0 changes:** with A-0 in place, no automatic path can start the bot: reboot, `deploy.sh`/`sync.sh`, `needrestart`, or a stray `systemctl start`. Gate A-1's merges cannot reach the VPS either. The ways that remain:
- the five dashboard routes (F-7);
- a manual `main.py` launch.

On any of these, **every IBKR order and flatten is refused** while the kill switch is on. A dashboard user can still release the kill switch; that is out of A-0's scope (see A-0f).

## 5. Session 2 readiness (NOT executed)

**What is ready:**
- the command block in `docs/reviews/M21_Gate_B_Session1_Report_2026-11-05.md` §13 (docs branch @ `7d19f39`), unchanged;
- the source pinned at `6c82b1a`, with an eight-file sha256 manifest;
- runs as `nobody`;
- writes only `/tmp/m21_op1`;
- stops with exit 2/3/4 on a refusal, a failed copy or a failed integrity check.

**What it answers:**
- historical coverage and completed-bar/timestamp integrity (`m16_inventory.txt`, `parquet_basis.*`);
- old paper activity since 1 June: signals, orders, claimed fills, positions/outcomes and M14 records (`old_paper_signals.txt`, `old_paper_journal.txt`);
- inputs for the E5 Flex reconciliation (`signals_copy.db`, `paper_lifecycles_copy.db`).

**Operator inputs:** two IBKR Flex exports (paper and live, 1 June → the export date) as specified in runbook OP1.6, with `ClientAccountID` kept as exported. Claude does not request or store credentials.

**Independent of A-0:** Session 2 can run before or after it, and it changes nothing.

**Add-ons recommended with Session 2.** All are read-only; each prints only flags, counts and fixed log formats.

- **S2-P: the broker allocation policy** (the second barrier, F-5). Run as `nobody` on the **copy** Session 2 makes. It prints only the gating flags, no amounts:

```bash
runuser -u nobody -- env HOME=/tmp PATH=/usr/bin:/bin python3 -I - <<'PY'
import sqlite3, json
c = sqlite3.connect("file:/tmp/m21_op1/signals_copy.db?mode=ro", uri=True)
r = c.execute("SELECT value FROM portfolio_risk_state WHERE key='broker_allocation_policy'").fetchone()
if r is None:
    print("broker_allocation_policy: ABSENT (the bot would use the runtime fail-safe)")
else:
    p = json.loads(r[0]); F = ("auto_trading_enabled", "auto_trading_enabled_until_utc", "kill_switch")
    for blk in ("global", "ibkr_paper", "ibkr_live", "ibkr", "etoro"):
        b = p.get(blk) or {}; print(blk, {k: b.get(k) for k in F if k in b})
    rt = p.get("routing") or {}; print("routing", {k: rt.get(k) for k in ("allowed_brokers", "etoro_live_enabled", "default_broker")})
PY
```

- **S2-W: the watchdog error count, its period, and why the bot was signal-only on 30 Jun.** Run as `nobody` on `bot.log`:

```bash
runuser -u nobody -- env HOME=/tmp PATH=/usr/bin:/bin bash -c '
L=/opt/algo-trader/logs/bot.log
echo "tick_errors=$(grep -c "\[GW-WATCHDOG\] tick error" $L) no_loop=$(grep -c "no current event loop in thread .gw-watchdog." $L)"
echo "first=$(grep -m1 "\[GW-WATCHDOG\] tick error" $L | cut -c1-23) last=$(grep "\[GW-WATCHDOG\] tick error" $L | tail -n1 | cut -c1-23)"
grep -m1 -A12 "\[GW-WATCHDOG\] tick error" $L | cut -c1-160   # the full first traceback: shows whether it passes through api_probe → IB()
grep -E "\[BROKER\] (wrapping|policy unavailable)" $L | tail -n 2 | cut -c1-200
'
```

- **S2-U (optional; root; read-only): were the 06:xx UTC restarts unattended upgrades?** It answers Q7 and matters for the OP4 soak, not for A-0:

```bash
grep -h -A3 -E "^Start-Date: 2026-(06-26|06-30|09-01|09-03|10-02|10-22|10-24|10-29)" /var/log/apt/history.log* 2>/dev/null | grep -E "Start-Date|Commandline" | cut -c1-160
zgrep -h -iE "restarting services|systemctl restart" /var/log/apt/term.log* 2>/dev/null | tail -n 12 | cut -c1-200
grep -hE "nrconf\{restart\}" /etc/needrestart/needrestart.conf /etc/needrestart/conf.d/*.conf 2>/dev/null
```

**Approval:** it needs its own approval (Session 1 report §14, row 3).

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
  - operator decisions D-1…D-8;
  - discrepancies;
  - the append-only changelog.
- **`MILESTONE_STATUS.md`:**
  - a status 3.0.1 block (the 29 Oct block is kept as history);
  - a vocabulary note;
  - a 24/25 row;
  - the pre-June future table labelled historical.
- **Untouched:** the 71 register tasks, the M21 Finish Line, every historical milestone requirement. No renumbering.

## 7. Open questions for the reviewer

1. A-0 is scoped to automatic and accidental activation. Should it also include **A-0f** (stop the dashboard until Gate A-2), given that F-7 lets an authenticated dashboard user start the bot and release the kill switch?
2. Is the A-0b2 drop-in guard acceptable as "prevent boot-time bot activation"? With P1 clean, the `deploy.sh` path needs a manual run.
3. Is the `gw-watchdog` analysis (§2a) sound, and should its fix (D-9) be required before OP2?
4. Should the hardening items be added to the register? They are: the five dashboard start routes (F-7); no working copy in `/opt/algo-trader` while the bot runs (F-10); unattended restarts during the soak (F-11).
5. The roadmap questions are unchanged: the M22 sub-number proposal (D-2); merging C01 after the soak; the M18–M20 meanings (D-1).

## 8. Returned items

| Item | Value |
|---|---|
| Exact UTC timestamp | 2026-11-05T15:24Z UTC (same in Europe/London, GMT) |
| Latest GitHub `main` SHA | `80b4343db4a22413aa670efd32e24952d53e532e` [GH] |
| VPS deployed SHA | `015d2cea927b9b4db3ee65604e0b4ebb58211e61` [VPS-R]. The bot last started on 30 Jun with HEAD `04bf872`. |
| Roadmap version | **v3.0** (DRAFT); status 3.0.2 |
| Documentation PR | https://github.com/mikemitakidis/algotrading/pull/34 (draft). Head SHA is in the PR record and Issue #3 (R11). |
| M21 status | **engineering:** ENGINEERING_COMPLETE on 23 draft PRs; A16 NOT_STARTED; new defect D-9 (`gw-watchdog`) not yet fixed. **Merge:** not merged (BLOCKED_BY_APPROVAL). **Deployment:** none of the M21 PR code. **Paper:** NOT_STARTED; the gateway is stopped. **Acceptance:** not FULLY_ACCEPTED. |
| M22–M25 | M22 (M22.1–6, C01–C11; sub-number mapping proposed); M23 (M23.1–5, D01–D10); **M24/M25 UNDEFINED** |
| Unresolved operator decisions | **roadmap:** D-1…D-10 (`ROADMAP.md` R14). **Operational:** Gate A-0 (± A-0f), Session 2 + S2-P/S2-W (± S2-U) + Flex exports, Gate A-1, Gate A-2, OP2–OP5. |
| Next recommended operational step | **Gate A-0 (a, b, b2; c no-op; d, e)**; then Session 2 with S2-P and S2-W |
| Next recommended engineering task | **D-9:** fix the `gw-watchdog` event-loop defect offline (small; needed for a meaningful B-6/OP2). Then **C01** (Track B). Both on isolated branches, draft PRs, no deployment. |
