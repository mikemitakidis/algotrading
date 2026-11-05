# M21 Gate B — Session 1 report: deployment and runtime state of the VPS

**For independent review (ChatGPT).** Prepared by Claude on 5 Nov 2026, from the read-only output of Gate B Session 1.

**The repository is public.** This report leaves out the server's IP address and hostname. Nothing in it is a credential.

---

## 1. Date and time

| | UTC |
|---|---|
| Session 1 ran (`deploy_discovery.txt` header) | **2026-11-05T10:00:48Z** |
| GitHub `main` read for this report | 2026-11-05T10:17:31Z |
| Report written | 2026-11-05 (this commit) |

## 2. Deployed commit (VPS)

- Branch **`main`**, upstream `origin/main`.
- `HEAD` = **`015d2cea927b9b4db3ee65604e0b4ebb58211e61`**, committed 2026-10-26T11:05:52Z, "test(C5b+D3b): pin the reviewed dashboard/app.py wiring as a fourth approved transition".
- The local `origin/main` ref (the last fetched state of `main`) is also `015d2ce`.
- **No tracked file is modified** on the server: `git status --porcelain --untracked-files=no` printed nothing.

## 3. GitHub `main`

- `main` = **`80b4343db4a22413aa670efd32e24952d53e532e`**, read through the GitHub API at 10:17:31Z.
- `015d2ce` is an ancestor of `80b4343`. Eight first-parent merges separate them: #4, #5, #6, #8, #7, #10, #9, #11.
- Those merges change only:
  - `research/` (62 files);
  - `docs/` (17 files);
  - ten root test files: eight `test_m21_research_*` suites, plus one allow-list addition each in `test_m17_backtesting.py` and `test_m18_ml.py`.
- No runtime file differs: `bot/`, `dashboard/`, `tools/`, `main.py`, `requirements.txt`, `sync.sh`, `deploy.sh`, `server_scripts/` and `_protected_baselines.py` are identical. Production never imports `research/` (guarded by `test_m21_research_foundation`).
- The 23 draft PRs (proposed `main` tree `5b0f40b`, Finish Line §7e.4) are all still unmerged drafts.

## 4. Deployment mechanisms and their evidence

| Mechanism | State | Class | Evidence |
|---|---|---|---|
| `sync.sh` auto-deploy daemon | **not running** | **CONFIRMED** | `sync_sh_running=no`. `/proc` is mounted without `hidepid` (`rw,nosuid,nodev,noexec,relatime`), so a non-root account sees every process, and this "no" is conclusive. No `python3` bot process and no shell running `sync.sh` was found. |
| `sync.sh` last observed activity | last logged event **Tue 2 Jun 2026 21:03:16 UTC**: "New commit detected → Updated to `597635d` → pip done → Restarted. Bot:3756400 Dash:3756399" | **CONFIRMED** (log content) | the last 40 deploy/restart events of `logs/sync.log` run from 29 May 12:44 to 2 Jun 21:03. Within that window there is no later event and no "Sync daemon started" line. |
| How the checkout moved after 2 Jun (`597635d` → `015d2ce`, 268 commits) | not through `sync.sh` | **INFERRED** | `sync.sh` logs "New commit detected" before every reset, and no such line follows 2 Jun. When or how the daemon stopped, and who moved the checkout, is **UNKNOWN**. The reflog and the `FETCH_HEAD`/`ORIG_HEAD` timestamps would show it (check H1, §9). |
| The deployed `sync.sh` / `deploy.sh` | identical to the repository copies | **CONFIRMED** | sha256 `3dd56546…` / `6d338ba5…`, equal to `sync.sh` / `deploy.sh` at `015d2ce`, `80b4343` and `5b0f40b`. `BRANCH=main`, `BASE=/opt/algo-trader`. |
| Restart path of the deployed `sync.sh` | systemd path (root, both units present), else `pkill`/`nohup` | **CONFIRMED** (code) | the deployed file is the M15.0 version. The 29 May – 2 Jun log lines come from the older `pkill`/`nohup` version, so the M15.0 version has **never logged a deployment** on this server. |
| `deploy.sh` (manual) | not scheduled | **CONFIRMED** (code), schedule **UNKNOWN** | if anyone runs it as root (M15.0 path), it: runs `pip install`; **starts every enabled unit that is not active (that includes `algo-trader.service`)**; removes any `@reboot` entry for `deploy.sh`; **starts `sync.sh` with `nohup`**. It does not start a unit an operator has disabled. |
| systemd units and timers | only `algo-trader`, `algo-trader-dashboard`, `ibgateway`, plus stock Ubuntu units; timers only `update-notifier-download`/`-motd` | **CONFIRMED** | Q3 |
| Unit files naming `sync.sh`/`deploy.sh`/`git` | none found | **INFERRED** | the check prints "(none readable)" for both "no match" and "unreadable". Unit files are normally world-readable. |
| System cron (`/etc/crontab`, `/etc/cron.d`) | no entry naming sync/deploy/git/`main.py`/scheduler | **CONFIRMED** | Q3 |
| **Root's crontab** (`/var/spool/cron/crontabs/root`) | — | **UNKNOWN** | "Permission denied" for a non-root account. This is where the pre-M15.0 `@reboot … deploy.sh` entry lived (check P1). |
| `/etc/rc.local`; profile hooks | none in readable files | **CONFIRMED** for `/etc/rc.local`; **UNKNOWN** for `/root/.bashrc`/`.profile` (unreadable) | Q3 |
| GitHub Actions self-hosted runner | none | **CONFIRMED** | no `Runner.Listener` process, no runner directory |
| Git hooks in the checkout | none (only samples) | **CONFIRMED** | Q3 |
| Dashboard bot control | the running dashboard has authenticated `POST /api/start`, `/api/restart`, `/api/stop` | **CONFIRMED** (code at `015d2ce`) | `start` launches `venv/bin/python3 main.py` with `Popen`, outside systemd, as the dashboard's user (root). **Anyone logged in to the dashboard can start the trading bot.** |
| Vercel | separate from the VPS | **CONFIRMED** (records, 29 Oct) | builds this repository on Vercel's servers; cannot reach or restart the VPS |

**Conclusion:** right now a merge to `main` is **not** deployed to the VPS automatically (**CONFIRMED** for `sync.sh`, systemd, system cron, hooks and runners). Two gaps remain:
- root's crontab is **UNKNOWN**;
- `deploy.sh` and the dashboard's `/api/start` are manual paths that a person could use.

## 5. Services and reboot behaviour

| Unit | Runs as | State | Enabled at boot | Restart policy | Last main-process start | Class |
|---|---|---|---|---|---|---|
| `algo-trader.service` (bot, `main.py`) | root | **inactive (dead)** | **yes** | `on-failure`, 10 s; `NRestarts=0` | Tue 2026-06-30 06:37:17 UTC (last PID 1030717) | CONFIRMED |
| `algo-trader-dashboard.service` | root | **active (running)**, PID 92343 | yes | `on-failure`, 10 s; `NRestarts=0` | Thu 2026-10-29 06:46:24 UTC | CONFIRMED |
| `ibgateway.service` (IB Gateway via IBC) | root | **failed** | yes | `always`, 30 s; `NRestarts=0` | Thu 2026-09-03 06:45:25 UTC (last PID 3070395) | CONFIRMED |

- **Not established:**
  - when and why the bot stopped (clean stop vs exit);
  - when and why the gateway failed (for example, the start-rate limit);
  - who started the dashboard on 29 Oct;
  - when the server last booted.

  A start time does not say who started a unit. Check H1 (`InactiveEnterTimestamp`, `Result`, `ExecMainStatus`, boot time) and check P2 (journal) cover these.
- **The bot has never run the deployed code.** Its last start (30 Jun) predates `015d2ce` (26 Oct). Which commit it last ran is **UNKNOWN** (the reflog would show the `HEAD` at 30 Jun). The dashboard has run `015d2ce` since 29 Oct (INFERRED: started after the 26 Oct checkout, and no fetch since).
- **Listening ports:** 22, 53, 80, 443, 2019, 8080 and five high ports. **No IBKR API port** (4001/4002/7496/7497) is listening — **CONFIRMED**, consistent with the failed gateway.
  - 8080 is the dashboard; its process is the only Python process found (INFERRED).
  - 80/443 with 2019 suggest a reverse proxy (2019 is Caddy's default admin port), and the high ports look like VS Code's remote server. Both are INFERRED, not verified.
  - Whether the dashboard is reachable from the internet is **UNKNOWN**.
- **WS2 paper loop:** not scheduled in any readable cron or timer — **CONFIRMED** for those. Root's crontab is unknown.

## 6. Broker and kill-switch safety

| Item | Finding | Class |
|---|---|---|
| Kill switch (`data/kill_switch.json`) | **inactive**: `active: False`, reason "test cleanup", updated 2026-05-27T18:53:18Z | CONFIRMED |
| What the kill switch blocks | the IBKR broker's `submit()` checks it **first**, before any network call, and returns `kill_switch_active`; the IBKR flatten path checks it too | CONFIRMED (code at `015d2ce`) |
| Broker mode (`BROKER` in `.env`) | not read; default when unset is `paper` (local simulation). `ibkr`/`ibkr_paper` = IBKR paper; `ibkr_live` refuses to start without its live safety configuration | **UNKNOWN** (value); code CONFIRMED |
| The broker the bot last started with | journal `[STARTUP] Broker:` line, not readable as non-root | **UNKNOWN** (check P2) |
| IBC/gateway target (paper or live account, API port) | gateway config not inspected | **UNKNOWN** |
| Live safety configuration present | not inspected | **UNKNOWN** |

### What happens if …

| Event | What follows | Depends on (UNKNOWN) |
|---|---|---|
| **1. The VPS reboots** | systemd starts **all three enabled units**: the bot, the dashboard and the gateway. The bot runs `main.py` at `015d2ce` (code it has never run) with `.env`. The kill switch is off, so it does not block. If root's crontab still has `@reboot … deploy.sh`, then `deploy.sh` also runs: `pip install`, start enabled units, **start `sync.sh`**. `sync.sh` would fetch `80b4343` within ~60 s, reset the checkout and restart both services. | `BROKER`; whether the gateway logs in (it fails today; cause unknown); root's crontab; live safety configuration |
| **2. IB Gateway becomes healthy** | the gateway alone sends no orders. Nothing that submits is running: no bot process, and no WS2 loop scheduled in readable cron or timers. | root's crontab |
| **3. The bot service is (re)started** (systemd, `deploy.sh`, or dashboard `/api/start`) | the scanner loop resumes. For each signal that passes risk, the M14 gate and the gateway watchdog, it submits to the `BROKER` broker. **While the gateway is down, IBKR submissions are blocked** (`broker_unready`) and only intents are recorded. With `BROKER=paper`, local simulated orders are recorded. Telegram alerts may be sent. | `BROKER`; gateway health; risk/M14 configuration; Telegram settings |
| **4. The 23 PRs are merged** | **no VPS change** (no daemon). Vercel creates a Production deployment per merge commit. | root's crontab (a reboot with `@reboot deploy.sh` would start `sync.sh`, which deploys `main`) |
| **5. The new M21 code is deliberately deployed** | `015d2ce` → the merged tree. Runtime changes on the default path: `bot/scanner.py` (bar as-of metadata; opt-in completed-bar seam, default off), new `bot/bar_completion.py` (standard library only), `bot/providers/alpaca_provider.py` (explicit `adjustment=raw`), new `bot/providers/alpaca_corporate_actions.py`. Unchanged: `main.py`, `dashboard/app.py`, `bot/risk.py`, `bot/brokers/*`, `bot/database.py`, `bot/flywheel.py`, `requirements.txt`. No migration runs; every new flag stays OFF. **The new code only runs once a process restarts.** Restarting the dashboard alone does not start trading; restarting the bot is a trading activation in itself. | Python version and packages in `venv` (not yet read); `.env` |

## 7. Complete Session 1 findings

| # | Finding | Class |
|---|---|---|
| F1 | The inspection ran as `nobody` (uid 65534, started with `runuser` from the root SSH login); the script's sha256 matched (`e2c5c256…4fc29`); all six probed paths were readable | CONFIRMED |
| F2 | `sync.sh` is not running; process visibility is unrestricted | CONFIRMED |
| F3 | Last `sync.sh` log activity: 2 Jun 2026 21:03:16 UTC (deploy of `597635d`, restart via the old `pkill`/`nohup` version) | CONFIRMED |
| F4 | The checkout advanced from `597635d` to `015d2ce` (268 commits) without `sync.sh` log entries | CONFIRMED (log and git); how it advanced: INFERRED (not `sync.sh`) |
| F5 | Deployed `015d2ce`, branch `main`, no modified tracked files; last fetched `main` = `015d2ce` | CONFIRMED |
| F6 | `main` is `80b4343`: eight merges of offline research code, docs and tests only | CONFIRMED |
| F7 | No timer, system cron, rc hook, git hook or Actions runner deploys or restarts the bot | CONFIRMED |
| F8 | Root's crontab and root's profile files | UNKNOWN |
| F9 | Bot inactive, enabled at boot, last started 30 Jun | CONFIRMED; stop time and cause UNKNOWN |
| F10 | Dashboard active since 29 Oct 06:46 UTC, runs as root, can start the bot (`/api/start`) | CONFIRMED; who started it UNKNOWN; internet exposure UNKNOWN |
| F11 | IB Gateway failed, enabled at boot; no IBKR API port listening | CONFIRMED; failure cause UNKNOWN |
| F12 | Kill switch inactive since 27 May | CONFIRMED |
| F13 | Broker mode (`BROKER`), gateway target account and live safety configuration | UNKNOWN |
| F14 | A reboot starts the bot with the kill switch off; whether it can then submit depends on F13 and gateway health | CONFIRMED (units) + UNKNOWN (outcome) |
| F15 | No WS2 loop scheduled in readable cron/timers | CONFIRMED (root's crontab UNKNOWN) |
| F16 | Nothing was changed on the server; the only new files are under `/tmp/m21_op1`, owned by `nobody` | CONFIRMED (by construction: all commands were read-only and ran as `nobody`, except the `cat`) |

## 8. Unknowns and unresolved blockers

| Unknown | Why it matters | Resolved by |
|---|---|---|
| Root's crontab (`@reboot … deploy.sh`?) | decides whether a reboot restarts `sync.sh`, which would auto-deploy `main` | **P1** (root, read-only) |
| `BROKER` mode last used; kill-switch state at the bot's last start; how and when the bot stopped; why the gateway failed | decides what a bot start or a reboot would do | **P2** (root, journal) + **H1** |
| When and how the checkout moved after 2 Jun; when the last fetch happened; last boot | establishes the deployment history and the commit the bot last ran (the rollback context) | **H1** (non-root) |
| Gateway target (paper/live) and live safety configuration | needed before any bot or gateway start | later (OP2), via the gateway configuration — not in Session 1 or 2 |
| Dashboard exposure (reverse proxy on 80/443) | `/api/start` can start the bot | later review (Gate A / hardening); not needed for Session 2 |
| Python and package versions in `venv` | deployment compatibility | Session 2 logs the Python version; packages at deployment (Gate A) |

**Blockers:** none for Session 2, which is read-only and starts nothing. Before **Gate A** (any merge, deployment or service change), P1 and P2 are essential: they decide whether a reboot or a bot start could deploy code or trade.

## 9. Exact read-only commands

**Used in Session 1.** Run in the root SSH terminal; every command ran as `nobody`:

```bash
runuser -u nobody -- env HOME=/tmp PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin bash -c '
id; hostname; date -u; findmnt -no OPTIONS /proc
for p in /opt/algo-trader /opt/algo-trader/.git /opt/algo-trader/sync.sh /opt/algo-trader/deploy.sh \
         /opt/algo-trader/logs/sync.log /opt/algo-trader/data/kill_switch.json; do
  [ -r "$p" ] && echo "readable      $p" || echo "NOT readable  $p"; done
DOCS=14b641c635fe3cb80815292b8bba940f5885b7f0
mkdir -p /tmp/m21_op1 && cd /tmp/m21_op1 || exit 1
curl -fsSL -o docs.tgz https://codeload.github.com/mikemitakidis/algotrading/tar.gz/$DOCS || exit 1
mkdir -p docs && tar -xzf docs.tgz -C docs --strip-components=1 || exit 1
echo "e2c5c25609f0f3391a282b1db78c335ab6c667d80c09a65048cc352182b4fc29  docs/docs/runbooks/op1/op1_deploy_discovery.sh" \
  | sha256sum -c - || { echo "CHECKSUM MISMATCH - STOPPED"; exit 1; }
bash docs/docs/runbooks/op1/op1_deploy_discovery.sh /tmp/m21_op1
systemctl show algo-trader.service algo-trader-dashboard.service ibgateway.service -p Id,User,Group --no-pager
grep -E "^(proc_mount_options|sync_sh_running|process|script|cgroup|parent|branch|upstream):?|^HEAD=|^origin/main|^Id=|^(Active|Sub|UnitFile)State=|^Restart=|^NRestarts=|^ExecMainStartTimestamp=|^kill_switch:" \
  /tmp/m21_op1/deploy_discovery.txt
'
cat /tmp/m21_op1/deploy_discovery.txt      # read the report file (root, read-only)
```

Before that, a separate Claude session on the operator's laptop tried only `ssh … id` as root. The server refused the key, and nothing ran.

**Proposed, not run — each needs separate approval.**

H1 — history and lifecycle, non-root (as `nobody`). Read-only; prints no names, emails or secrets:

```bash
runuser -u nobody -- env HOME=/tmp PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin bash -c '
uptime -s
stat -c "%y %n" /opt/algo-trader/.git/FETCH_HEAD /opt/algo-trader/.git/ORIG_HEAD /opt/algo-trader/logs/sync.log 2>&1
python3 -I -c "import sys, datetime as d
for line in open(sys.argv[1]).read().splitlines()[-15:]:
    head, _, msg = line.partition(chr(9)); p = head.split()
    print(p[0][:7], p[1][:7], d.datetime.fromtimestamp(int(p[-2]), d.timezone.utc).isoformat(), msg[:90])" /opt/algo-trader/.git/logs/HEAD
systemctl show algo-trader.service algo-trader-dashboard.service ibgateway.service --no-pager \
  -p Id,ActiveEnterTimestamp,InactiveEnterTimestamp,ExecMainExitTimestamp,ExecMainStatus,Result
last -x reboot shutdown 2>/dev/null | head -n 5
'
```

P1 — root's crontab, root, read-only. Only matching lines are printed, with secret-like values and account ids redacted:

```bash
crontab -l -u root 2>&1 | grep -nE 'sync|deploy|git|main\.py|scheduler|paper|@reboot|algo' \
  | sed -E 's#://[^/@ ]+@#://<redacted>@#g; s/((key|token|secret|password|passwd|pwd)[A-Za-z_]*[=: ]+)[^ ;&"]+/\1<redacted>/Ig; s/\b(DU|U)[0-9]{5,}\b/\1***/g'
echo "root_crontab_matches_rc=${PIPESTATUS[1]}"      # 1 = no matching line
ls /var/spool/cron/crontabs/ 2>&1                     # whose crontabs exist (names only)
```

P2 — journal lifecycle and start-up lines, root, read-only. Only keyword-filtered lines are printed (the last 15 per unit), with secrets and account ids redacted:

```bash
R='s#://[^/@ ]+@#://<redacted>@#g; s/[0-9]{8,10}:[A-Za-z0-9_-]{30,}/<telegram-token>/g; s/((key|token|secret|password|passwd|pwd)[A-Za-z_]*[=: ]+)[^ ;&"]+/\1<redacted>/Ig; s/\b(DU|DF|U)[0-9]{5,}\b/\1***/g'
for u in algo-trader algo-trader-dashboard ibgateway; do
  echo "== $u"
  journalctl -u "$u.service" --no-pager -o short-iso 2>&1 \
    | grep -E 'Started|Stopped|Stopping|Failed|failed|Main process exited|Start request repeated|Deactivated|\[STARTUP\] (Broker|Live|LIVE)|KILL_SWITCH\] State on startup|GW-WATCHDOG\]' \
    | sed -E "$R" | tail -n 15
done
journalctl --list-boots --no-pager 2>&1 | tail -n 5
```

P1 and P2 need root because a non-root account cannot read root's crontab or the system journal. H1 does not.

## 10. Evidence (sanitised output)

The Session 1 summary and the full `deploy_discovery.txt` as pasted by the operator. The hostname is replaced by `<vps-host>`; nothing else is changed.

```
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)
<vps-host>
Thu Nov  5 10:00:48 AM UTC 2026
rw,nosuid,nodev,noexec,relatime
readable      /opt/algo-trader
readable      /opt/algo-trader/.git
readable      /opt/algo-trader/sync.sh
readable      /opt/algo-trader/deploy.sh
readable      /opt/algo-trader/logs/sync.log
readable      /opt/algo-trader/data/kill_switch.json
docs/docs/runbooks/op1/op1_deploy_discovery.sh: OK
written: /tmp/m21_op1/deploy_discovery.txt
User=root  Group=root  Id=algo-trader.service
User=root  Group=root  Id=algo-trader-dashboard.service
User=root  Group=      Id=ibgateway.service
```

```
== generated 2026-11-05T10:00:48Z on <vps-host> as nobody

== Q1/Q2 sync.sh: running? who manages it?
proc_mount_options=rw,nosuid,nodev,noexec,relatime session_uid=65534
sync_sh_running=no
-- deployed sync.sh: branch it follows, and whether it matches main 80b4343's copy (sha256 of the file)
7:BASE=/opt/algo-trader
8:VENV=$BASE/venv
10:BRANCH=main
3dd56546c7af0f877245282e0f90989ba037849da2c5b1e7f0f1b20b60b296a5  /opt/algo-trader/sync.sh
6d338ba56d49f21b800de9e3435a4836e203484677d5d5469fd33b3a750e5d78  /opt/algo-trader/deploy.sh
-- sync.log: start/deploy/restart events (last 40, redacted)
Fri May 29 12:44:14 PM UTC 2026: New commit detected (e569d43… -> d9c53eb…) / Updated to d9c53eb / pip done / Restarted. Bot:3611001 Dash:3611000
Fri May 29 06:55:59 PM UTC 2026: … Updated to 7aa1082 … Restarted. Bot:3618778 Dash:3618777
Fri May 29 07:17:34 PM UTC 2026: … Updated to 729ad2d … Restarted. Bot:3619779 Dash:3619778
Fri May 29 07:41:11 PM UTC 2026: … Updated to c5536b5 … Restarted. Bot:3620730 Dash:3620729
Tue Jun  2 07:31:38 PM UTC 2026: … Updated to ace0fda … Restarted. Bot:3751938 Dash:3751937
Tue Jun  2 07:54:14 PM UTC 2026: … Updated to 2e20b52 … Restarted. Bot:3753791 Dash:3753790
Tue Jun  2 08:12:45 PM UTC 2026: … Updated to 71e893a … Restarted. Bot:3754488 Dash:3754487
Tue Jun  2 08:32:17 PM UTC 2026: … Updated to e0d4999 … Restarted. Bot:3755565 Dash:3755564
Tue Jun  2 08:55:53 PM UTC 2026: … Updated to 57dc200 … Restarted. Bot:3756216 Dash:3756215
Tue Jun  2 09:03:12 PM UTC 2026: New commit detected (57dc200… -> 597635d…) / Updated to 597635d / pip done / Restarted. Bot:3756400 Dash:3756399
   (each event is four log lines; condensed here, full lines in the operator's paste)

== Q3 other automated deployment or restart mechanisms
units: algo-trader-dashboard.service, algo-trader.service, ibgateway.service, systemd-hwdb-update, systemd-journal-catalog-update,
       systemd-timesyncd, systemd-update-done, systemd-update-utmp-runlevel, systemd-update-utmp, update-notifier-download, update-notifier-motd
timers: update-notifier-download.timer (next Thu 2026-11-05 20:03:37 UTC), update-notifier-motd.timer (next Sun 2026-11-08 12:48:07 UTC)
unit FILES that mention sync.sh / deploy.sh / git: (none readable)
current user's crontab: (no lines)
system cron files: (none)
spool: ls: cannot open directory '/var/spool/cron/crontabs': Permission denied
rc.local / profile hooks: (none)
actions_runner_processes=0; (no runner directory)
listening_ports: 22 53 80 443 2019 8080 34715 35759 36603 37693 59758
non-sample git hooks: (none)

== Q4 services (properties only)
algo-trader.service:            Restart=on-failure RestartUSec=10s NRestarts=0 ExecMainStartTimestamp=Tue 2026-06-30 06:37:17 UTC ExecMainPID=1030717
                                LoadState=loaded ActiveState=inactive SubState=dead UnitFileState=enabled
algo-trader-dashboard.service:  Restart=on-failure RestartUSec=10s NRestarts=0 ExecMainStartTimestamp=Thu 2026-10-29 06:46:24 UTC ExecMainPID=92343
                                LoadState=loaded ActiveState=active SubState=running UnitFileState=enabled
ibgateway.service:              Restart=always RestartUSec=30s NRestarts=0 ExecMainStartTimestamp=Thu 2026-09-03 06:45:25 UTC ExecMainPID=3070395
                                LoadState=loaded ActiveState=failed SubState=failed UnitFileState=enabled
python processes: 92343 Thu Oct 29 06:46:24 2026 python3 | /system.slice/algo-trader-dashboard.service

== Q5 deployed working tree
branch: main / upstream: origin/main
HEAD=015d2cea927b9b4db3ee65604e0b4ebb58211e61 committed=2026-10-26T11:05:52+00:00
origin/main(local ref, as last fetched)=015d2cea927b9b4db3ee65604e0b4ebb58211e61
origin git@github.com:mikemitakidis/algotrading.git (fetch/push)
tracked files modified on the server: (none)

== Q6 deployed sync.sh on failure: line 33 pip install … ; 39 "ERROR — import check failed after update. Not restarting." ; 40 sleep 60 ; 41 continue ; 76 sleep 60

== Q7 restart: kill_switch: {'active': False, 'reason': 'test cleanup', 'updated_at': '2026-05-27T18:53:18.900995+00:00'}
   journal start-up lines: none (not readable as nobody); IBKR API ports listening: none; ws2_schedule_mentions=0
```

## 11. Rollback and deployment baseline

- **Difference:** `015d2ce` → `80b4343` is offline research code, docs and tests only (§3). `015d2ce` → `5b0f40b` (after the 23 PRs) adds the runtime changes listed in §6, event 5.
- **Uncommitted changes:** none in tracked files (**CONFIRMED**). `git reset --hard` does not touch untracked files such as `.env`, `data/`, `logs/` and `venv/`, but a deployment must still not depend on that.
- **Preserve before any deployment:**
  - `.env`: a byte copy outside the repository, plus its sha256. An earlier backup location exists, but it is not verified here.
  - `data/*.db`: consistent copies (Session 2's copies serve once checked) and their sha256.
  - `data/kill_switch.json`, `data/bot_state.json`, `logs/`.
  - `venv/bin/pip freeze` and its sha256.
  - `git rev-parse HEAD` (expected `015d2ce`) and the unit properties above.
- **Why roll back to `015d2ce`, not to "main":**
  - `015d2ce` is what is on disk, and it is the code the dashboard has run since 29 Oct.
  - `80b4343` has never been deployed here.
  - `main` after the merges is the thing being rolled back.

  A rollback restores the recorded pre-deployment state: the same commit, the same `venv`, the same files. Even that has a limit: the bot itself last ran on 30 Jun with an older commit, which is **UNKNOWN** (H1). The bot therefore has no verified operational baseline until it is started under a controlled gate.
- **Compatibility checks needed at deployment:**
  1. `requirements.txt` is unchanged between `015d2ce` and `5b0f40b`; confirm with `pip install -r … --dry-run`, or compare `pip freeze` before and after (expected: no change).
  2. Python version: the tests ran on 3.12.3. Read the `venv` version (Session 2 logs it).
  3. An import check of the default path (`bot.scanner`, `bot.bar_completion`, the dashboard imports) in the `venv` before any restart.
  4. No database migration runs by default. The candidate-ledger migration stays undeployed and its writer OFF. Confirm afterwards that the schemas match the Session 2 copies (no new tables).
  5. Restart **only the dashboard**. The bot stays stopped: starting it is a separate activation gate (OP3).

## 12. Recommended next actions (in order)

1. **P1 + P2 + H1** (read-only): close the reboot, broker-mode and history unknowns. They are essential before any Gate A action and are not needed for Session 2.
2. **Session 2** (read-only, as `nobody`; §13): data coverage, old paper activity, order/fill and M14 records, inputs for statement reconciliation. It can run before or after item 1.
3. **Gate A-0: safety hardening, no deployment** (proposed; each change separately approved and reversible):
   - a. **Engage the kill switch** (dashboard → kill switch, or `bot.kill_switch.activate_kill_switch(reason)`). Every later bot start, by reboot, `deploy.sh` or `/api/start`, then blocks IBKR submissions. Undo: deactivate.
   - b. **`systemctl disable algo-trader.service`**: the bot no longer starts at boot, and `deploy.sh` respects a disabled unit. Nothing stops (it is already inactive). Undo: `enable`.
   - c. If P1 shows an `@reboot … deploy.sh` entry: remove that one line. Undo: re-add it.
   - d. Leave `sync.sh` stopped; deploy only by the deliberate procedure.
   - e. Leave the gateway as it is. It sends no orders by itself, and its failure is examined in OP2.
4. **Gate A-1: merges** (23 PRs, after reviews and C5b), with the Vercel choice. After A-0 they cannot reach the VPS by themselves.
5. **Gate A-2: deliberate deployment** (§11): preserve, update the checkout to the merged SHA, compatibility checks, restart the dashboard only, verify; rollback to `015d2ce`.
6. **Later: OP2/OP3**: gateway (paper) acceptance and bot activation, each with its own approval.
7. **Hardening for later:** the dashboard runs as root and can start the bot by itself. Record this in the register; no change now.

## 13. Session 2: prepared for approval, not run

Same terminal, same `/tmp/m21_op1`, and again run as `nobody`. It reads data and copies databases read-only, and starts nothing.

```bash
runuser -u nobody -- env HOME=/tmp PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin bash -c '
id
cd /tmp/m21_op1 || exit 1
CODE=6c82b1a6117213efbf8e2bc2f8c52335ea18f83d
curl -fsSL -o src.tgz https://codeload.github.com/mikemitakidis/algotrading/tar.gz/$CODE || exit 1
mkdir -p src && tar -xzf src.tgz -C src --strip-components=1 || exit 1
sha256sum -c - <<SUMS || { echo "CHECKSUM MISMATCH - STOPPED"; exit 1; }
e2c5c25609f0f3391a282b1db78c335ab6c667d80c09a65048cc352182b4fc29  docs/docs/runbooks/op1/op1_deploy_discovery.sh
3df72bc0a788d3587ccd2fe60b43e9453dc3af7058f655fff41255819e16e8ce  docs/docs/runbooks/op1/op1_readonly.sh
f3de98868ae779c11c6f8b991ac6dd5ba1cf925549aee7ae6e2a58811c250d32  docs/docs/runbooks/op1/op1_runtime_status.sh
dcaf78169bfadd1aacf9e2f0275deef4ba73f05d58624284d14052957f9f800e  docs/docs/runbooks/op1/op1_parquet_basis.py
f7022b398dafe2e11a2007092b90a602abe18cdc908fd615db2ac5b397f21f64  docs/docs/runbooks/op1/op1_sql.py
eec185efca64b310b60e2ea981270839fbe8dbfaf3b76dddf12f8aedbd7634ab  docs/docs/runbooks/sql/old_paper_journal.sql
fe4195c5d50d7173e9b70d4e1ef3d2766b52bc765a2f9f908bcd290cf46d2f36  docs/docs/runbooks/sql/old_paper_signals.sql
030df4bb55d5ae8a4dcfbde04da4c80b7235b52d8fade83925bf34cf2cc48abb  docs/docs/runbooks/sql/op1_m16_inventory.sql
SUMS
bash docs/docs/runbooks/op1/op1_readonly.sh; echo "exit=$?"
'
```

- `exit=0` is required. Exit 2, 3 or 4 means the session stopped by itself (refused, a copy failed, or an integrity check failed). In that case send `op1.log`, and do not use or share any `*_copy.db`.
- **As `nobody`:**
  - an unreadable database stops the session with exit 3;
  - `.env` variable names and journal lines will be empty or "permission denied" (expected);
  - the readiness step runs only if you add an exported calendar file (runbook OP1.3).
- **What it answers:**
  - historical coverage and timestamps (`m16_inventory.txt`, `parquet_basis.*`);
  - old paper activity, order intents, fills, outcomes and M14 records since 1 June (`old_paper_signals.txt`, `old_paper_journal.txt`);
  - the reconciliation inputs (`signals_copy.db`, `paper_lifecycles_copy.db`).
- **Sharing:** read the outputs first. Package them without databases (packet §4). Share database copies only after review, by your chosen route.
- **IBKR Flex exports:** your own two exports (paper and live account, 1 June → today), as specified in runbook OP1.6. Keep `ClientAccountID` as exported.

## 14. Approvals required (each separate)

| # | Approval | Changes anything? |
|---|---|---|
| 1 | Run **H1** (non-root history and lifecycle) | no |
| 2 | Run **P1** (root crontab) and **P2** (journal) as root | no |
| 3 | Run **Session 2** (§13) as `nobody`; produce your two Flex exports | no (writes only `/tmp/m21_op1`) |
| 4 | **Gate A-0a**: engage the kill switch | yes — reversible |
| 5 | **Gate A-0b**: `systemctl disable algo-trader.service` | yes — reversible |
| 6 | **Gate A-0c**: remove an `@reboot deploy.sh` line, only if P1 finds one | yes — reversible |
| 7 | **Gate A-1**: merge the 23 PRs; the Vercel choice | yes |
| 8 | **Gate A-2**: deliberate deployment (dashboard restart only) | yes |
| 9 | **OP2/OP3**: gateway acceptance and bot activation | yes — later |

Not part of any approval above: starting the bot, starting `sync.sh`, IBKR connections, orders, `.env` changes, database writes, Vercel changes.
