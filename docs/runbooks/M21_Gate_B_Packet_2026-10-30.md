# Gate B — read-only VPS inspection (OP1): execution packet

**Prepared 30 Oct 2026. Not run.** This packet asks for **inspection only**. Permission to inspect the VPS is not
permission to change it: stopping `sync.sh`, any restart, any merge or deployment belongs to Gate A (Finish Line
§7e.4) and is asked for separately, after these results.

## 1. What Gate B permits, and what it does not

| Permits | Does not permit |
|---|---|
| One SSH session running exactly the commands in §3 | `sudo`; `systemctl` start/stop/restart/enable/disable/edit/daemon-reload |
| Writing only under `/tmp/m21_op1` | `kill`/`pkill` of anything, including `sync.sh` |
| Downloading two public tarballs from `codeload.github.com` (or copying them in with `scp`) | `git fetch/pull/reset/checkout` in `/opt/algo-trader`; `pip`; `crontab -e` |
| Your own two IBKR Flex exports from Client Portal (reporting only; OP1.6) | editing `.env`, `data/kill_switch.json` or any database; dashboard writes |
| | IBKR API or gateway connections; orders; Telegram; deployment; Vercel; CI |

## 2. Permissions

- **VPS:** SSH as the account the bot runs as, or a read-only account that can read `/opt/algo-trader/{data,logs,.git}`,
  `sync.sh`, `deploy.sh` and `stat` `.env*`. Write access only to `/tmp/m21_op1`. Journal lines need membership of
  `systemd-journal` (or `adm`); without it that section says "access denied" and nothing else changes. No sudo.
  If the services run as root and the files are readable only by root, prefer granting a separate account group read
  access; if you run the session as root instead, run only these commands (they were audited read-only, §5).
  Limits of a non-root session: root's crontab and root processes' working directories are unreadable; the report
  says so and Gate A checks them.
- **Network (VPS):** outbound HTTPS to `codeload.github.com` for step 1 only (the repository is public), or `scp` the
  two tarballs in from your computer. The scripts themselves open no network connection.
- **IBKR:** your Client Portal login; one Flex Query run for the paper and for the live account, 1 June 2026 → today,
  as specified in runbook OP1.6 (keep `ClientAccountID` as exported). No API keys, no gateway session.
- **Claude:** nothing on the VPS or at IBKR; only the files you share.

## 3. Exact commands

```bash
# 0. where and who (names only)
id; hostname; date -u

# 1. fetch the reviewed code and scripts (writes only /tmp/m21_op1)
CODE=6c82b1a6117213efbf8e2bc2f8c52335ea18f83d    # m21/integration, the tested tree (used only by the readiness step)
DOCS=20cd99962a4b02f776f31845875efd2818308e18    # docs branch commit holding the final OP1 scripts
mkdir -p /tmp/m21_op1 && cd /tmp/m21_op1
curl -fsSL -o src.tgz  https://codeload.github.com/mikemitakidis/algotrading/tar.gz/$CODE
curl -fsSL -o docs.tgz https://codeload.github.com/mikemitakidis/algotrading/tar.gz/$DOCS
sha256sum src.tgz docs.tgz | tee -a op1.log
mkdir -p src docs && tar -xzf src.tgz -C src --strip-components=1 && tar -xzf docs.tgz -C docs --strip-components=1
sha256sum docs/docs/runbooks/op1/*.sh docs/docs/runbooks/op1/*.py docs/docs/runbooks/sql/*.sql   # must equal §4

# 2. deployment discovery FIRST: does anything deploy main, and what would a restart do?
bash docs/docs/runbooks/op1/op1_deploy_discovery.sh /tmp/m21_op1
grep -E '^sync_sh_running=|^(branch|upstream): |^HEAD=|^tracked|^Id=|^ActiveState=|^Restart=|^NRestarts=|^kill_switch:' \
  /tmp/m21_op1/deploy_discovery.txt

# 3. optional: /tmp/m21_op1/calendar.json, only an EXPORTED exchange calendar (runbook OP1.3), never hand-typed;
#    without it the readiness report is skipped and everything else runs

# 4. the full read-only session (runs the discovery again, plus data, old paper system and runtime evidence)
bash docs/docs/runbooks/op1/op1_readonly.sh

# 5. package what you share (no databases, no tarballs)
tar -czf /tmp/m21_op1/op1_outputs.tgz -C /tmp/m21_op1 --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude='*.tgz' --exclude=src --exclude=docs .
sha256sum /tmp/m21_op1/op1_outputs.tgz
```

Share `op1_outputs.tgz`. By a route you choose, also `paper_lifecycles_copy.db` and `signals_copy.db` (production
data, no secrets; E5 needs them) and your two Flex CSV exports. `historical_copy.db` is needed only for the later
real-data runs.

## 4. Expected sha256 of the scripts (commit `20cd999`)

```
776d81690f550d802d3a47139e6b8849f034ee4b441fb3da7e2703bcd602c819  docs/docs/runbooks/op1/op1_deploy_discovery.sh
d0c54830492d0a5168dfc276edb7c7ba4894280fc46546cc3a22f8024e0f7e84  docs/docs/runbooks/op1/op1_readonly.sh
f3de98868ae779c11c6f8b991ac6dd5ba1cf925549aee7ae6e2a58811c250d32  docs/docs/runbooks/op1/op1_runtime_status.sh
dcaf78169bfadd1aacf9e2f0275deef4ba73f05d58624284d14052957f9f800e  docs/docs/runbooks/op1/op1_parquet_basis.py
f7022b398dafe2e11a2007092b90a602abe18cdc908fd615db2ac5b397f21f64  docs/docs/runbooks/op1/op1_sql.py
eec185efca64b310b60e2ea981270839fbe8dbfaf3b76dddf12f8aedbd7634ab  docs/docs/runbooks/sql/old_paper_journal.sql
fe4195c5d50d7173e9b70d4e1ef3d2766b52bc765a2f9f908bcd290cf46d2f36  docs/docs/runbooks/sql/old_paper_signals.sql
030df4bb55d5ae8a4dcfbde04da4c80b7235b52d8fade83925bf34cf2cc48abb  docs/docs/runbooks/sql/op1_m16_inventory.sql
```

## 5. Verified before execution (offline, 30 Oct)

The whole session (step 4, which includes step 2 and the readiness step with the code tarball and a calendar file)
was run on a synthetic replica of the VPS built with the real code (signals, journal and M16 databases with their
production schemas, a parquet store, `.env`, `bot_state.json`, `logs/sync.log`, `data/kill_switch.json`, the
deployed `sync.sh`/`deploy.sh`, the checkout as a git repository with a credential-bearing remote URL, a running
stand-in `sync.sh` daemon and a decoy process whose arguments mention `sync.sh` and a password) **under
`strace -f`** (`docs/runbooks/op1/verify/`). The system-call audit found **0 violations**:

| Must not | Evidence |
|---|---|
| Stop or restart services | `systemctl` ran only `show`, `list-units`, `list-timers`; no `service`, `systemd-run`, `reboot` |
| Modify the kill switch | no file written outside `/tmp/m21_op1`; replica tree hash (incl. `data/kill_switch.json`) unchanged |
| Connect to a broker / place orders | no TCP or UDP connect or send at all (only local `AF_UNIX`); no broker or order module is imported |
| Reset Git | `git` ran only `rev-parse`, `log`, `remote`, `status` (`GIT_OPTIONAL_LOCKS=0`); `.git` unchanged |
| Install dependencies | no `pip` or package manager executed |
| Change files outside the output directory | every file opened for writing is under `/tmp/m21_op1`; no rename, unlink, chmod or chown outside it |
| Expose `.env` values or credentials | `.env`: permissions and variable NAMES only; the planted password, Telegram token, GitHub token and account id appear in no output; process arguments are never printed |
| Modify existing databases | `sqlite3` ran only with `mode=ro` URIs or `-readonly`; reports run on copies; `quick_check` ok; tree hash unchanged |
| Signal any process | no `kill`, `tkill` or `tgkill` system call |

Limits: a replica in a container is not the VPS. There, `systemctl show/list-*` query systemd over D-Bus (read-only)
and real permissions decide what is readable; any "Permission denied" in the outputs is reported, never worked
around. The fetch in step 1 is the only network access, to GitHub, by `curl`.

## 6. What the outputs answer

| Question | Where |
|---|---|
| Is `sync.sh` active, and who manages it? | `deploy_discovery.txt`: `sync_sh_running=`; `cgroup:` (systemd unit or login session), `parent:`, `script:` |
| Any other mechanism that deploys `main` or restarts the bot? | `deploy_discovery.txt` Q3: units/timers, unit files and cron entries naming sync/deploy/git, `@reboot`, rc/profile hooks, an Actions runner, git hooks |
| Current server commit; would a reset discard local changes? | Q5: `HEAD=` (expected `80b4343`), branch, upstream, `tracked files modified` |
| Services and restart policies | Q4: `ActiveState`, `UnitFileState`, `Restart`, `NRestarts`, start time; python processes by owning unit |
| Broker configuration, without secrets | `env_metadata.txt`: names of `BROKER`, `IBKR_*`, `PAPER_*`, `LIVE_*`, `RISK_*` (no values); journal `[STARTUP] Broker:` line (the mode the bot started with) |
| Kill switch, IBKR API ports | Q7: `kill_switch:` (active, reason, time); listening ports 4001/4002/7496/7497 |
| Historical-data coverage | `m16_inventory.txt`, `parquet_basis.csv` / `_summary.txt`, `readiness/` (with a calendar) |
| Old paper-system evidence since June | `old_paper_signals.txt`, `old_paper_journal.txt`, `journal_*.txt` (activity per day since 1 June) |
| M14 data freshness | `old_paper_signals.txt` §9/§10: latest `risk_decisions`, `risk_snapshots`, `broker_positions` |
| Statement reconciliation inputs | the two `*_copy.db` files and your Flex exports → runbook OP1.7 (offline) |

## 7. Stop and report (change nothing)

- a script sha256 in step 1 differs from §4;
- `COPY FAILED`, or `quick_check` not `ok`;
- the secret screen count is above 0 (review before sharing);
- any `Permission denied` (report it; do not escalate privileges);
- live-scope activity in `old_paper_signals.txt` §9 or a `LIVE_ACCOUNT_ACTIVITY` alert later in OP1.7.

`sync_sh_running=yes`, a `HEAD` other than `80b4343` or modified tracked files are **findings for Gate A**, not
reasons to act in Gate B: do not stop the daemon or touch the checkout in this session.

## 8. After Gate B

Claude reads the outputs, runs the statement reconciliation offline (runbook OP1.7, steps 1–2) and completes the
Gate A packet with the facts found (which mechanism to pause, how, and the deployment and rollback steps).
