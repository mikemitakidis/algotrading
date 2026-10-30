# Gate B — read-only VPS inspection (OP1): execution packet

**Final, 30 Oct 2026 (evening). Not run.** This packet asks for **inspection only**, in two sessions. Permission to
inspect is not permission to change: stopping `sync.sh`, any restart, merge or deployment belongs to Gate A
(Finish Line §7e.4) and is asked for separately, after these results.

- **Session 1 — deployment state** (first): is `sync.sh` running and who manages it, any other deployment path,
  service and restart state, the deployed Git SHA, the kill-switch state. No database, no `.env`.
- **Session 2 — data and the old paper system** (only after we have reviewed Session 1, on your separate go-ahead):
  read-only database copies, historical-data coverage, old paper activity since June, M14 freshness.

## 1. What Gate B permits, and what it does not

| Permits | Does not permit |
|---|---|
| SSH sessions running exactly the commands below | `sudo` (except the optional one-time account setup in §2, done by you) |
| Writing only under `/tmp/m21_op1` | `systemctl` start/stop/restart/enable/disable/edit/daemon-reload; `kill`/`pkill` of anything, including `sync.sh` |
| Downloading public tarballs of this repository from `codeload.github.com` (or copying them in with `scp`) | `git fetch/pull/reset/checkout` in `/opt/algo-trader`; `pip`; `crontab -e` |
| Session 2 only: your own two IBKR Flex exports from Client Portal (reporting only; runbook OP1.6) | editing `.env`, `data/kill_switch.json`, any database or any file under `/opt/algo-trader`; changing permissions there |
| | IBKR API or gateway connections; orders; Telegram; deployment; Vercel; CI |

**No production file, service or broker configuration needs to change for Gate B.** The only change you may
choose to make is creating the read-only login account in §2 (a system user; nothing under `/opt/algo-trader`).

## 2. Account: non-root, minimum read access

**Use a non-root account.** It needs:

| | Session 1 | Session 2 (adds) |
|---|---|---|
| Read | `/opt/algo-trader/{sync.sh,deploy.sh,logs/sync.log,data/kill_switch.json}` and `/opt/algo-trader/.git` (for the deployed SHA) | `/opt/algo-trader/data/*.db`, `data/historical/`; `.env` is only `stat`-ed and its variable NAMES listed (if unreadable that section stays empty, which is fine) |
| See processes | other users' processes (`/proc` without `hidepid`); otherwise the script reports `sync_sh_running=UNKNOWN`, never a false "no" | — |
| Journal | membership of `systemd-journal` (or `adm`) for the start-up lines; without it that part says "access denied" | same |
| Write | `/tmp/m21_op1` only (created by the commands) | same |
| Network | HTTPS to `codeload.github.com` (or `scp` the tarball in) | same |

Options, in order of preference:
1. **An existing non-root login that can read the paths above** — the Step-0 probe in §3 shows whether it can.
2. **A dedicated read-only login, set up once by you as root.** It changes no production file or permission; undo
   with `userdel -r m21ro`:
   ```bash
   useradd -m -s /bin/bash m21ro
   usermod -aG systemd-journal m21ro                       # only for the journal lines; optional
   install -d -m 700 -o m21ro -g m21ro /home/m21ro/.ssh
   # put your SSH public key in /home/m21ro/.ssh/authorized_keys (owner m21ro, mode 600)
   ```
   If the probe then shows `/opt/algo-trader` or `.git` NOT readable, **do not change permissions there**: tell me,
   and we decide (for example, run the same audited commands as the service account).

## 3. Session 1 — deployment state (do this first)

```bash
# 0. who and where; can this account see what it needs? (prints names and yes/no only)
id; hostname; date -u
findmnt -no OPTIONS /proc
for p in /opt/algo-trader /opt/algo-trader/.git /opt/algo-trader/sync.sh /opt/algo-trader/deploy.sh \
         /opt/algo-trader/logs/sync.log /opt/algo-trader/data/kill_switch.json; do
  [ -r "$p" ] && echo "readable      $p" || echo "NOT readable  $p"; done

# 1. fetch the reviewed scripts (writes only /tmp/m21_op1)
DOCS=14b641c635fe3cb80815292b8bba940f5885b7f0
mkdir -p /tmp/m21_op1 && cd /tmp/m21_op1
curl -fsSL -o docs.tgz https://codeload.github.com/mikemitakidis/algotrading/tar.gz/$DOCS
mkdir -p docs && tar -xzf docs.tgz -C docs --strip-components=1
sha256sum docs/docs/runbooks/op1/op1_deploy_discovery.sh
#   must print e2c5c25609f0f3391a282b1db78c335ab6c667d80c09a65048cc352182b4fc29 — stop if not

# 2. deployment discovery (read-only)
bash docs/docs/runbooks/op1/op1_deploy_discovery.sh /tmp/m21_op1
systemctl show algo-trader.service algo-trader-dashboard.service ibgateway.service -p Id,User,Group --no-pager

# 3. the lines to send me first
grep -E '^(proc_mount_options|sync_sh_running|process|script|cgroup|parent|branch|upstream):?|^HEAD=|^origin/main|^Id=|^(Active|Sub|UnitFile)State=|^Restart=|^NRestarts=|^ExecMainStartTimestamp=|^kill_switch:' \
  /tmp/m21_op1/deploy_discovery.txt
```

Send me the step-0 and step-3 output (and the `systemctl show ... User` lines). Then read `deploy_discovery.txt`
yourself and share it if you are comfortable: it holds the hostname, process ids, unit names, cron and timer lines
that mention sync/deploy/git, the redacted remote URL, redacted start-up journal lines and listening ports — no
`.env` value, no database content.

**Session 1 findings are inputs to Gate A, not actions:** `sync_sh_running=yes`, `UNKNOWN`, a `HEAD` other than
`80b4343` or modified tracked files mean **report, change nothing** (do not stop the daemon or touch the checkout).

## 4. Session 2 — data and the old paper system (only after we review Session 1)

```bash
CODE=6c82b1a6117213efbf8e2bc2f8c52335ea18f83d    # m21/integration, the tested tree (used only by the readiness step)
cd /tmp/m21_op1
curl -fsSL -o src.tgz https://codeload.github.com/mikemitakidis/algotrading/tar.gz/$CODE
mkdir -p src && tar -xzf src.tgz -C src --strip-components=1
sha256sum docs/docs/runbooks/op1/*.sh docs/docs/runbooks/op1/*.py docs/docs/runbooks/sql/*.sql   # must equal §5
# optional: /tmp/m21_op1/calendar.json, only an EXPORTED exchange calendar (runbook OP1.3); else readiness is skipped
bash docs/docs/runbooks/op1/op1_readonly.sh; echo "exit=$?"
#   exit=0 required. 2 = refused before copying; 3 = a database copy failed; 4 = a copy failed its integrity check.
#   On 2/3/4 the session has already stopped: send me op1.log; do not use or share any *_copy.db;
#   a re-run needs a new directory: OP1_OUT=/tmp/m21_op1_run2 OP1_SRC=/tmp/m21_op1/src bash docs/docs/runbooks/op1/op1_readonly.sh
tar -czf /tmp/m21_op1/op1_outputs.tgz -C /tmp/m21_op1 --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude='*.tgz' --exclude=src --exclude=docs .
sha256sum /tmp/m21_op1/op1_outputs.tgz
```

Share `op1_outputs.tgz` (after reading it). Database copies (`paper_lifecycles_copy.db`, `signals_copy.db`) only
after `exit=0`, after we have looked at the outputs, by a route you choose; with your two Flex CSV exports they feed
the offline reconciliation (runbook OP1.7). `historical_copy.db` is needed only for the later real-data runs.

## 5. Expected sha256 of the scripts (commit `14b641c`; checked against the GitHub tarball)

```
e2c5c25609f0f3391a282b1db78c335ab6c667d80c09a65048cc352182b4fc29  docs/docs/runbooks/op1/op1_deploy_discovery.sh
3df72bc0a788d3587ccd2fe60b43e9453dc3af7058f655fff41255819e16e8ce  docs/docs/runbooks/op1/op1_readonly.sh
f3de98868ae779c11c6f8b991ac6dd5ba1cf925549aee7ae6e2a58811c250d32  docs/docs/runbooks/op1/op1_runtime_status.sh
dcaf78169bfadd1aacf9e2f0275deef4ba73f05d58624284d14052957f9f800e  docs/docs/runbooks/op1/op1_parquet_basis.py
f7022b398dafe2e11a2007092b90a602abe18cdc908fd615db2ac5b397f21f64  docs/docs/runbooks/op1/op1_sql.py
eec185efca64b310b60e2ea981270839fbe8dbfaf3b76dddf12f8aedbd7634ab  docs/docs/runbooks/sql/old_paper_journal.sql
fe4195c5d50d7173e9b70d4e1ef3d2766b52bc765a2f9f908bcd290cf46d2f36  docs/docs/runbooks/sql/old_paper_signals.sql
030df4bb55d5ae8a4dcfbde04da4c80b7235b52d8fade83925bf34cf2cc48abb  docs/docs/runbooks/sql/op1_m16_inventory.sql
```

## 6. Verified before execution (offline, 30 Oct)

**Stop rules** (`docs/runbooks/op1/verify/op1_failure_injection.py`, 13 tests, all pass; with the sqlite3 CLI and
with the Python fallback): a source that is not a database → exit 3 at once; a database that copies but fails
`PRAGMA quick_check` → exit 4 at once; a failure in the third database after two good copies → still exit 3 with no
report on the good ones; `*_copy.db` left by an earlier run → exit 2 before anything is copied; in every case no SQL
report, summary or later step was produced and the source databases were byte-identical afterwards; healthy
databases → exit 0 with all reports. On the previous script the failure cases did not stop: a non-database source
logged "COPY FAILED", the empty copy passed `quick_check`, the report ran on it and the session exited 0.

**Process visibility** (same file): as a non-root account the daemon is found when processes are visible, reported
`UNKNOWN` when `/proc` hides other users' processes (the previous script said "no"), and found by root either way.

**Read-only** — the whole Session 2 run (which includes Session 1's discovery and the readiness step) on a synthetic
replica of the VPS built with the real code, **under `strace -f`**: 0 violations.

| Must not | Evidence |
|---|---|
| Stop or restart services | `systemctl` ran only `show`, `list-units`, `list-timers` |
| Modify the kill switch | no file written outside `/tmp/m21_op1`; replica tree (incl. `data/kill_switch.json`) unchanged |
| Connect to a broker / place orders | no TCP or UDP connect or send (only local `AF_UNIX`); no broker or order module imported |
| Reset Git | `git` ran only `rev-parse`, `log`, `remote`, `status` (`GIT_OPTIONAL_LOCKS=0`); `.git` unchanged |
| Install dependencies | no `pip` or package manager executed |
| Change files outside the output directory | every file opened for writing is under `/tmp/m21_op1`; no rename, unlink, chmod or chown elsewhere |
| Expose `.env` values or credentials | `.env`: permissions and variable NAMES only; planted password, Telegram token, GitHub token and account id appear in no output; process arguments never printed |
| Modify existing databases | `sqlite3` only with `mode=ro` URIs or `-readonly`; reports read checked copies only |
| Signal any process | no `kill`, `tkill` or `tgkill` system call |

Limits: a container replica is not the VPS. There, `systemctl show/list-*` query systemd over D-Bus (read-only) and
real permissions decide what is readable; any "Permission denied" is reported, never worked around. The only
network access is the tarball download by `curl`, from GitHub.

## 7. What the outputs answer

| Question | Where |
|---|---|
| Is `sync.sh` running, and who manages it? | `deploy_discovery.txt`: `sync_sh_running=` (yes / no / UNKNOWN); `cgroup:` (systemd unit or login session), `parent:`, `script:` |
| Any other mechanism that deploys `main` or restarts the bot? | Q3: units/timers, unit files and cron entries naming sync/deploy/git, `@reboot`, rc/profile hooks, an Actions runner, git hooks |
| Deployed Git SHA; would a reset discard local changes? | Q5: `HEAD=` (expected `80b4343`), branch, upstream, `tracked files modified` |
| Services and restart policies | Q4: `ActiveState`, `UnitFileState`, `Restart`, `NRestarts`, start time; python processes by owning unit; plus `User`/`Group` from step 2 |
| Broker mode, kill switch, IBKR API ports | Q7: journal `[STARTUP] Broker:` line; `kill_switch:`; listening 4001/4002/7496/7497 |
| Broker configuration names, no secrets (Session 2) | `env_metadata.txt`: names of `BROKER`, `IBKR_*`, `PAPER_*`, `LIVE_*`, `RISK_*` |
| Historical-data coverage (Session 2) | `m16_inventory.txt`, `parquet_basis.csv` / `_summary.txt`, `readiness/` |
| Old paper-system evidence since June; M14 freshness (Session 2) | `old_paper_signals.txt` (incl. §9/§10 latest M14 records), `old_paper_journal.txt`, `journal_*.txt` |

## 8. Stop and report (change nothing)

- a script sha256 differs from §3/§5;
- Session 2 `exit` is not 0 (2, 3 or 4), or the secret screen count is above 0;
- any `Permission denied` (report it; do not change permissions or escalate);
- live-scope activity in `old_paper_signals.txt` §9.

## 9. After Gate B

Claude reads the outputs, runs the statement reconciliation offline (runbook OP1.7) once you share the copies and
exports, and completes the Gate A packet with the facts found (which mechanism to pause, how, deployment and rollback).
