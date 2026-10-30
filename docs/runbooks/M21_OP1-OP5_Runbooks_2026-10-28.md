# M21 operational runbooks OP1–OP5 (preparation only)

**Status:** prepared by Claude on 28 Oct 2026; revised the same evening after the independent review (OP1.6, the abort criteria and the rollback steps for OP2/OP3, and promotion reconciliation in OP5); revised 29 Oct (OP1.6 nine evidence categories, OP1.7 statement reconciliation, OP2/OP3 acceptance gate); **OP1 finalised 29 Oct evening** (scripts in `docs/runbooks/op1/`, verified read-only on a synthetic replica; IBKR Flex specification; E5 reconciliation). Nothing here has been run. Every step needs **your explicit approval at the gate shown**.

**Rule for every runbook:**
- Stop at the first unexpected output.
- Never paste secret values into a ticket or chat.
- Anything that can only be confirmed on the VPS is marked **VPS VERIFICATION REQUIRED**.

**Host facts used** (from the earlier read-only checkpoint scripts; re-check them on the day):
- repository at `/opt/algo-trader`;
- project Python `/opt/algo-trader/venv/bin/python3`;
- IBKR paper API port `4002`.

---

## OP1: one read-only VPS session (gate G2) — FINAL, 29 Oct 2026

**Status:** final for approval. Prepared and verified offline; **not run on the VPS**. Claude does not connect to the
VPS or to IBKR: you run OP1 and download the IBKR statements yourself.

### OP1.0 What OP1 establishes, and what counts as proof

| # | Question | Evidence (output file) | Proof level |
|---|---|---|---|
| 1 | Historical data available | `m16_inventory.txt` §B–C, `parquet_basis.csv` | our store, read-only |
| 2 | Symbols and timeframes | `m16_inventory.txt` §C/E, `parquet_basis_summary.txt` | our store |
| 3 | Coverage and gaps | `m16_inventory.txt` §C–G (bars, window, missing, duplicates, quality events), `readiness/report.md` | our store + exchange calendar |
| 4 | Adjustment basis and corporate actions | `parquet_basis.csv` (is_adjusted share, distinct ratios, ratio change points), `readiness/` | our store; raw-basis verdicts stay INCONCLUSIVE until corporate-action records exist (WP5) |
| 5 | Was the old paper system running | `runtime_status.txt` (units, timers, cron, process names, ports, bot_state keys), `journal_<unit>.txt` (activity per day, redacted) | host evidence |
| 6 | Signals since 1 June 2026 | `old_paper_signals.txt` §2 | our records (claims) |
| 7 | Orders planned and submitted | `old_paper_signals.txt` §3–4, `old_paper_journal.txt` | claims |
| 8 | Broker acknowledgements | `old_paper_signals.txt` §5/6 (ids recorded, counts only), journal perm ids | claims → proof only via OP1.7 |
| 9 | Claimed fills vs confirmed broker fills | OP1.7 reconciliation (E5) | **the IBKR statement** |
| 10 | Open / closed paper positions | journal states + `old_paper_signals.txt` §9 (latest broker_positions batch); statement Open Positions section | statement for proof |
| 11 | Fees / commissions | OP1.7 (statement commissions; missing = unknown, never zero) | **statement only** |
| 12 | Unexpected live-account activity | `9_LIVE_SCOPE_ATTEMPT_ESCALATE` rows; live-scope positions; E5 `LIVE_ACCOUNT_ACTIVITY` on the **live** statement | live statement |
| 13 | Current M14 exposure / P&L evidence collection | `old_paper_signals.txt` §10 (daily_state_per_broker last 14 days, risk_snapshots, risk_decisions last 7 days), `env_metadata.txt` (RISK_* names) | our records |
| 14 | **Would merging to `main` auto-deploy to the VPS?** | `runtime_status.txt`: `sync_sh_processes`, `sync.log` deploy/restart events, deployed `HEAD` and modified-file count | host evidence — **merge blocker until answered** |

**Rules:** a database row is a claim, never proof of execution. Local simulations (`paper_logged`, `data_source='simulated'`)
are reported separately from IBKR paper activity and never counted as executions. The nine evidence categories
below are never added together.

### OP1.1 Minimum permissions

| Where | Needed | Not needed |
|---|---|---|
| VPS | SSH as the account that already runs the bot (read access to `/opt/algo-trader/data`, `stat` of `.env*`); write access **only** to `/tmp/m21_op1`; `journalctl -u` for the bot units needs membership of `systemd-journal` (or `adm`); otherwise that one step reports "access denied" and nothing else changes | sudo for anything else; any write under `/opt/algo-trader`; reading `.env` values; service start/stop/restart; package installs (if `sqlite3` is absent the script uses `op1_sql.py`, standard library) |
| Network (VPS) | HTTPS to `codeload.github.com` for the two tarballs (or copy them in with `scp`) | broker, Alpaca or any data vendor |
| IBKR | your own Client Portal login, to create and run a Flex Query (reporting only) for the **paper** account and for the **live** account | API keys, TWS/Gateway sessions, trading permissions, sharing credentials |
| Claude | the output files and statement CSVs you upload | VPS or IBKR access of any kind |

### OP1.2 Fetch code and runbook (no git operations on the VPS)
```bash
CODE=<code SHA: head of m21/wp11-readiness (PR #20), or main once merged>
DOCS=<head of docs/full-canonical-handoff-2026-10-26 (this runbook's commit)>
mkdir -p /tmp/m21_op1 && cd /tmp/m21_op1
curl -fsSL -o src.tgz  https://codeload.github.com/mikemitakidis/algotrading/tar.gz/$CODE
curl -fsSL -o docs.tgz https://codeload.github.com/mikemitakidis/algotrading/tar.gz/$DOCS
sha256sum src.tgz docs.tgz | tee -a op1.log
mkdir -p src docs && tar -xzf src.tgz -C src --strip-components=1 && tar -xzf docs.tgz -C docs --strip-components=1
```

### OP1.3 Exchange calendar rows (VPS VERIFICATION REQUIRED)
WP11 needs a JSON list of `{date, open, close}` rows covering the data window (Alpaca `/v2/calendar` shape), saved as
`/tmp/m21_op1/calendar.json`: either (a) the WP5 calendar fetcher in a separate approved step (it uses the Alpaca keys)
or (b) an export you download. Never hand-type a calendar. Without it, OP1.4 skips only the readiness report.

### OP1.4 Run the read-only session
```bash
cd /tmp/m21_op1 && bash docs/docs/runbooks/op1/op1_readonly.sh
```
It copies `signals.db`, `paper_lifecycles.db` and `historical.db` through read-only connections (`.backup` from a
`mode=ro` URI, which takes a shared lock so a database the bot is writing is copied consistently; `immutable=1` only
for a WAL-format file with no `-wal` file, so SQLite creates no side files; `PRAGMA quick_check` on every copy), runs
`docs/runbooks/sql/{op1_m16_inventory,old_paper_signals,old_paper_journal}.sql` on the **copies** with
`sqlite3 -readonly`, summarises the parquet store (`op1_parquet_basis.py`), runs the WP11 readiness report when the
calendar is present, records `.env*` permissions and `RISK_*`/broker variable **names**, collects runtime evidence
(`op1_runtime_status.sh`: unit properties, timers, cron, process **names** — never arguments — ports, the
`sync.sh` auto-deploy daemon (process count, deploy/restart events from `logs/sync.log`), the deployed checkout's
`HEAD` (read-only `git log`; `git status` with `GIT_OPTIONAL_LOCKS=0` so the index is not rewritten), `bot_state.json`
keys, journal lines filtered and redacted), screens every output for secret-like strings and writes `MANIFEST.sha256`.

**Verified offline (29 Oct):** on a synthetic replica of the VPS layout built with the real code (`init_db`, flywheel
tables and migrations, `LoopJournal`, M16 `apply_schema` + a parquet file written by the store), with planted fake
secrets in `.env`, `bot_state.json` and `logs/sync.log`, and the replica as a git checkout: exit 0; the replica tree
hash (including `.git`) identical before and after; no side files
created; zero planted secrets in any output; all SQL ran without errors; the same with the `sqlite3` CLI absent
(Python fallback); the readiness step ran read-only against a code checkout. `bash -n` clean.

**Re-verified 30 Oct, under `strace -f`** (`docs/runbooks/op1/verify/`: `op1_replica_check.py` builds the replica and
runs the session with the code tarball and a calendar file, so every step including readiness runs;
`op1_trace_audit.py` reads the system-call log): 0 violations — no program outside the read-only set was executed (no
`pip`, `kill`/`pkill`, `sudo`, `curl`, `rm`, `mv`, `chmod`; `systemctl` only `show`/`list-units`/`list-timers`; `git`
only `rev-parse`/`log`/`remote`/`status`; `sqlite3` only with `mode=ro` or `-readonly`; `journalctl -u` reads), every
file written is under `/tmp/m21_op1`, no signal was sent to any process, and the only socket connections were local
`AF_UNIX` ones (no TCP/UDP: no broker, gateway or vendor connection). Replica tree including `.git` and
`data/kill_switch.json` unchanged; `quick_check` ok on all copies; secret screen 0 (the unpacked `src/` and `docs/`
tarballs are excluded from it, as they are from `MANIFEST.sha256`); the stand-in `sync.sh` daemon found, the decoy not
matched.

**Merge blocker (item 14).** `sync.sh` polls GitHub every 60 s; on a new `main` commit it runs `git reset --hard
origin/main` in `/opt/algo-trader`, `pip install -r requirements.txt` and restarts **both** `algo-trader.service` and
`algo-trader-dashboard.service`. If `sync_sh_processes` is above 0, every merge would deploy and restart production.
Before any merge, decide (separate approval): stop the daemon for the merge window and deploy once, deliberately
(recommended), or accept automatic deployment. Do not merge while this is unknown.

**Stop if** `deploy_discovery.txt` shows the deployed `HEAD` is not `80b4343`, or tracked files are modified on the
server: report before any merge. **Stop if:** `op1.log` shows `COPY FAILED`, any path outside `/tmp/m21_op1` being written, a secret-screen count
above 0 (review before sharing), any `9_LIVE_SCOPE_ATTEMPT_ESCALATE` row, a non-paper scope holding positions in §9,
or any SQL error other than `no such table`.

**Share:** `MANIFEST.sha256` and the `.txt`, `.csv` and `readiness/` outputs. Keep the `*_copy.db` files; transfer them
only by the route you choose (they are production data, no secrets): E5 needs `paper_lifecycles_copy.db` and
`signals_copy.db`.

### OP1.4b Deployment discovery (part of OP1.4; added 30 Oct)

`op1_readonly.sh` also runs `op1_deploy_discovery.sh` → `deploy_discovery.txt`. It answers, read-only:

| # | Question | Where in the output | How to read it |
|---|---|---|---|
| D1 | Is `sync.sh` running? | `sync_sh_running=` | Only a shell **running a script named `sync.sh`** counts; no other argument of any process is printed |
| D2 | Who manages it? | `cgroup:` and `parent:` of each daemon | `/system.slice/<x>.service` = a systemd unit; `/user.slice/…/session-N.scope` = started from a login shell (`nohup … &`, as `deploy.sh` does); parent PID 1 = orphaned after that shell ended |
| D3 | Which branch does it follow? | `BRANCH=` from the deployed `sync.sh`; its sha256 vs `main` | `main` means every merge to `main` is picked up within ~60 s |
| D4 | Other deployment or restart mechanisms? | Q3: unit/timer names, unit files and cron entries mentioning sync/deploy/git, `@reboot`, rc.local/profile hooks, a GitHub Actions self-hosted runner, listening ports, non-sample git hooks | Any hit is a second path to production that must be paused too |
| D5 | Are the bot and dashboard active? | Q4: `ActiveState`, `UnitFileState`, `Restart=`, `NRestarts`, start time; python processes with their owning unit | `Restart=always/on-failure` means a crash reloads whatever code is on disk |
| D6 | Does the server's tree track `main`? | Q5: branch, upstream, deployed `HEAD`, the local `origin/main` ref, remote URL (credentials redacted), modified tracked files | Modified tracked files would be **discarded** by the daemon's `git reset --hard` |
| D7 | What happens if dependency installation fails? | Q6: the deployed `sync.sh` lines | See below |
| D8 | Could a restart resume trading? | Q7: the bot's last `[STARTUP] Broker:` line, `KILL_SWITCH` state on startup, kill-switch file, IBKR API ports listening, any schedule of the WS2 loop | See below |

**D7, from `sync.sh` on `main` (static reading; confirm against the deployed copy):** after `git reset --hard
origin/main` the new code is **already on disk** — before any restart. `pip install -r requirements.txt` runs with
its output appended to `sync.log`; a pip failure does **not** stop the script. Only the import check
(`yfinance, pandas, numpy, flask, dotenv, requests`) can prevent the restart; then it logs `ERROR … Not
restarting`, sleeps and, because local `HEAD` now equals `origin/main`, **never retries**. The running processes
keep old code in memory while the disk holds new code and possibly half-installed packages; any lazy import inside
the running bot (`main.py` imports several modules inside functions), a crash with `Restart=` or a reboot then
loads the new code without a deliberate deployment.

**D8, from `main.py` on `main`:** on start the bot runs the scanner loop and, for each signal that passes
`RiskManager` and the M14 portfolio gate, calls `broker.submit()` on the broker selected by `BROKER` in `.env`
(IBKR paper per the July configuration; `ibkr_live` refuses to start without its live safety configuration;
`etoro_real` cannot be selected). So a restart resumes **automatic paper order submission** as before, unless the
kill switch is active: its state lives in `data/kill_switch.json` and survives restarts (start-up never clears it).
No M21 code path is enabled by a restart: the WS2 loop, the candidate-ledger writer
(`CANDIDATE_LEDGER_ENABLED`), template shadow (`TEMPLATE_SHADOW_ENABLED`), market-context shadow
(`MARKET_CONTEXT_SHADOW_ENABLED`) and the template view (`TEMPLATE_CONTROLS_DB_PATH`) all need explicit
configuration.

**Limits of a non-root session:** root's crontab, the working directory of root-owned processes and some unit files
are not readable; the report shows `Permission denied` or a blank, and D2/D4 stay partial. If `/proc` hides other
users' processes (`hidepid`), D1 is reported as `sync_sh_running=UNKNOWN`, never as a false "no".

**Stop rules (30 Oct):** a database copy that fails stops the session (exit 3); a copy whose `PRAGMA quick_check` is
not exactly `ok` stops it (exit 4); an output directory that already holds `*_copy.db` is refused (exit 2). No report
or later step runs after a stop, and reports read only copies made and checked in the same run
(`verify/op1_failure_injection.py`). Execution: `docs/runbooks/M21_Gate_B_Packet_2026-10-30.md`. Then the cgroup line
(world-readable) still tells systemd from login session, and the remaining question goes into Gate A as a check you
run yourself (`sudo crontab -l -u root`, read-only), not as an assumption.

**Verified offline (30 Oct):** on the synthetic replica (now a git checkout with a credential-bearing remote URL,
a deployed `sync.sh`, a kill-switch file, a stand-in daemon process and a decoy process whose arguments mention
`sync.sh` and a fake password): the daemon was found and attributed, the decoy was not matched and its arguments
never printed, the remote URL credential was redacted, the replica (including `.git`) was unchanged.

**Vercel is a different thing.** The Vercel project builds this GitHub repository on Vercel's servers
(Production for `main`, Preview for every branch). It does not run on the VPS and cannot deploy to, restart or
reconfigure the trading bot. The VPS deploys itself only through `sync.sh` (or whatever D4 finds). What the Vercel
site serves could not be checked from here (the project settings are not readable with the available access and
its URL is not reachable from this workspace).

### OP1.5 The nine evidence categories (reported separately, never added together)

| # | Category | Where it shows | What it proves |
|---|---|---|---|
| 1 | Local simulations | intents `1_LOCAL_SIMULATION_NO_BROKER`; journal `data_source='simulated'` | Nothing reached a broker |
| 2 | Candidate signals | `signals`, `candidate_snapshots` | Scanner output only; no order implied |
| 3 | Order intents | intents by broker/status (3b = blocked before submission) | What our code decided to attempt |
| 4 | Journal-reported submissions | `4_SUBMISSION_REPORTED_NO_BROKER_ID`; journal SUBMITTED without perm id | Our code says it sent an order; no broker id |
| 5 | Broker acknowledgements | `5_BROKER_ACK_CLAIMED`; journal `broker_perm_id` | An id was recorded by our code; proof needs the statement |
| 6 | Real IBKR paper executions | `6_EXECUTION_CLAIMED_NEEDS_STATEMENT`; journal fills | **Only** statement executions MATCHED in OP1.7 count |
| 7 | Closed paper trades | journal `paper_trade_outcomes` (`ibkr_paper`); `signal_outcomes` are price paths, not trades | Genuine only when entry and every exit leg are MATCHED |
| 8 | Fees / commissions | statement only (`signals.db` stores none; journal fees are our record) | Statement commission per execution |
| 9 | Unexpected live activity | `9_LIVE_SCOPE_ATTEMPT_ESCALATE`; non-paper scopes in positions/M14 | Our code's attempts only; ruling it out needs the **live** statement |

`0_UNCLASSIFIED_REVIEW` rows must each be explained. Section 10 of `old_paper_signals.txt` is the current M14
exposure/P&L evidence collection (not an execution category).

### OP1.6 IBKR statements you export (matching information for E5)

Create one Flex Query (Client Portal → Performance & Reports → Flex Queries → Activity Flex Query), run it once for
the **paper** account and once for the **live** account, period **1 June 2026 → today**:
- **Sections:** *Trades* with Options = **Execution** (level of detail). Fields: ClientAccountID, AssetClass,
  CurrencyPrimary, Symbol, Buy/Sell, Quantity, TradePrice, IBCommission, IBCommissionCurrency, DateTime,
  OrderReference, IBOrderID, IBExecID, LevelOfDetail, Notes/Codes (TradeDate, OrderType, Exchange optional).
  Also *Open Positions* (Summary): ClientAccountID, Symbol, Position, MarkPrice, CostBasisPrice, ReportDate.
- **Delivery:** format CSV, include column headers **yes**, section code/line descriptor either way (both layouts are
  parsed); date format `yyyyMMdd`, time format `HHmmss`, date/time separator `;`.
- **Time zone:** note the time zone the statement uses (shown in the query's settings; usually US/Eastern) and pass it
  as `--statement-tz`. E5 assumes none. A constant 4- or 5-hour `time_differs_by_…` on every matched leg means the
  wrong zone was given.
- **Account ids:** leave `ClientAccountID` exactly as exported. E5 requires every matched execution to be on the
  same `DU…` paper account the journal recorded for the order, so a placeholder would make every trade a MISMATCH.
  E5 never prints it: outputs show a masked id (`DU***NN`) and a digest. The statement files themselves contain the
  id: transfer them by the route you choose.
- An Activity Statement CSV is also accepted, but it carries no orderRef or execution id, so it can only produce
  PROBABLE_MATCH (never proof). Use the Flex export.

### OP1.7 Reconciliation (offline, in Claude's workspace; nothing connects to IBKR)
```bash
S="--statement paper_trades.csv --statement live_trades.csv"
D="--journal-db paper_lifecycles_copy.db --legacy-signals-db signals_copy.db"
# 1. reconcile: recon.json carries the evidence manifest (schema m21-e5-reconciliation/3)
python -m research.economics.statement $S --statement-tz America/New_York $D --out-json recon.json --out-md recon.md
# 2. re-derive: the stored report must be exactly what these inputs produce (prints VERIFIED report_sha256=...)
python -m research.economics.statement $S $D --verify-evidence recon.json
# 3. later (after OP3, when the ledger exists): E4 with the SAME statement files and journal copy
python -m tools.candidate_ledger.score_outcome --ledger-db <ledger copy> --statement-matches recon.json \
  $S --journal-db paper_lifecycles_copy.db --out-json score_outcome.json --out-md score_outcome.md
```
- **Matching:** first every identifier link — claimed execution ids (journal `loop_exec_claims`), then orderRef
  (`order_ref`, `order_ref:FLAT`), then perm id vs `IBOrderID` — for all claims; only then symbol + side + quantity
  within ±120 s for what is left, which is never more than PROBABLE_MATCH. Each statement execution is assigned to at
  most one claim (an internal check refuses the report otherwise). Old-system intents recorded no ids: PROBABLE at best.
- **MATCHED also needs:** a stock; the `DU…` paper account the journal recorded for the order (an order without a
  recorded account, a blank account, or another account is never MATCHED); no cancelled (`Ca`), corrected (`Co`) or
  adjusted (`Adj`) row for that account and symbol (those rows are excluded and the claim needs manual review); an
  order key no other claim uses; an entry from one broker order.
- **Closed trades (7)** = entry and every exit leg MATCHED, and all matched trades on ONE paper account
  (`matched_trade_ids`; otherwise none counts: `UNSUPPORTED_ACCOUNT_SCOPE`). **Fees (8)** = statement commissions of
  MATCHED legs; a missing commission or currency is unknown, never zero: the trade's `fees_known` is false and E4
  reports its fees as unconfirmed; fee calibration only suggests a multiplier.
- **Evidence:** `recon.json` records the generation time, the reconciler's sha256 and parameters, the sha256 of every
  input, the account scope (masked id + digest) and, per matched trade, the identifiers and statement rows linking
  each leg; `report_sha256` covers the rest. Step 2 re-derives it. E4 (step 3) refuses an altered report, other or
  changed inputs, evidence older than 7 days or from the future, a scope that is not one verified paper account, a
  trade that is not a CLOSED `ibkr_paper` journal order on that account, an identifier that is not in the statement or
  not that order's, a statement row supporting two legs, and a ledger copy that disagrees with the journal.
  **Digests are not signatures:** that the statement came from IBKR rests on your export (OP1.6); record who
  exported it and when.
- **Acceptance evidence (OP4/C-4):** a trade counts as statement-confirmed only when steps 2 and 3 both pass on the same
  inputs; the acceptance record lists `report_sha256` and the input digests from `score_outcome.json`
  (`statement_evidence`). Re-run step 1 rather than raising `--max-evidence-age-days`.
- **Live statement:** every execution on a non-`DU` account raises `LIVE_ACCOUNT_ACTIVITY` → stop and escalate.

**Done when:** the OP1 outputs and, once you provide the exports, `recon.md` are attached to Issue #3; the readiness
verdicts are reviewed; the `RISK_*` names match the M14 expectation; every UNMATCHED_CLAIM, MISMATCH and
UNATTRIBUTED_EXECUTION is explained.

---

## OP2: IBKR paper gateway acceptance (gate G3; needs #1 merged and WP5)

**Last known state:** the gateway **failed** on the VPS. Treat it as down until proven otherwise.

1. Read-only health first: `python3 tools/paper_loop/m21_1extra_b2a_gateway_readiness.py`.
   - Expected `service_active_api_port_open`, `expected_port=4002`.
   - Anything else → stop.
2. Contract dry run (no order): `python3 tools/paper_loop/m21_1extra_b1_ibkr_paper_contract_dryrun.py`.
3. **Operator-only, explicit approval:** one PAPER single submit with `tools/paper_loop/m21_1extra_b2b_single_submit.py`. Then the flatten check, `m21_1extra_d2_flat_check.py`.
4. Monitoring dry check with the WP10 checks against the journal: `bot.monitoring.probe.run_checks`, read-only.

**Accept:**
- port 4002 (paper);
- order acknowledged and flattened;
- no NEEDS_REVIEW;
- a duplicate-order check of OK.

**Stop if:**
- any live port (4001/7496) appears;
- an order is unattributed.

**Abort criteria (stop immediately, do not retry, report):**
- the connected account is not the paper account;
- the port is not 4002;
- any order without our `orderRef`;
- an order acknowledged but missing a protective leg;
- a fill that cannot be matched to an order;
- a second order for the same `client_ref`;
- the flatten check reporting residual exposure;
- any NEEDS_REVIEW;
- any CRITICAL monitoring alarm.

**Rollback.** OP2 changes no code and no configuration.
1. Run `m21_1extra_d2_flat_check.py` to confirm the account is flat.
2. If it is not flat, close the paper position with the same operator-only paper tool, under your approval.
3. Cancel any working paper order from the IBKR paper account UI.
4. Stop the gateway session.
5. Record everything in Issue #3.

Nothing touches the live account.

---

## OP3: activation in paper (gate G3; needs WP5–WP7 and WP10 merged)

Each step is a separate approval.
1. Deploy the merged `main` (your existing deploy procedure; Claude never runs `deploy.sh`).
2. **Dashboard restart** (A06). Confirm the D3 panel shows `by_strategy` and `by_template`.
3. **A02 ledger migration** on `data/signals.db`: run `bot.candidate_ledger.migrate`, then set `CANDIDATE_LEDGER_ENABLED=1`. Before enabling, back up the DB with `.backup`. Rollback = unset the flag; the schema is additive.
4. **Scheduled unattended loop** (A12):
   ```
   tools/paper_loop/m21_1extra_d2_3_scheduler.py --broker ibkr-paper --interval-minutes 15
   ```
   Run it under your process manager. Optional flags stay **off** unless approved: `TEMPLATE_SHADOW_ENABLED` and the WP15 context shadow.
5. **Monitoring** (WP10). Schedule the checks every 5 minutes with the `LogSink` only. A messaging sink needs its own approval.

**Accept:**
- the first unattended day has a heartbeat OK;
- no CRITICAL alarm;
- the ledger `journal_consistency` report is clean, including an **empty `non_authorising_risk` list**: every journal order is backed by live or verified M14 evidence;
- D3 is populating.

**Abort criteria (stop the scheduler, engage the kill switch if exposure is uncertain, report):**
- any OP2 abort criterion;
- `ledger_audit_unavailable` blocks on more than one tick (the mandatory intent audit is failing);
- any `non_authorising_risk` entry;
- any `contradiction:*` count from `LoopLedger.reconcile`;
- the ledger `account()` reporting violations;
- the market clock not trusted at entry time;
- the dashboard failing its health check after restart.

**Rollback, per step** (each is reversible on its own; Claude never runs `deploy.sh` or `rollback.sh`):

| Step | Rollback |
|---|---|
| 1. Deploy | Your existing rollback procedure to the previous release. |
| 2. Dashboard | Restart the previous release. |
| 3. Ledger | Unset `CANDIDATE_LEDGER_ENABLED`; the loop then runs exactly as before. The schema is additive, so it stays in place. Restore the `.backup` copy only if `account()` shows corruption. |
| 4. Scheduler | Stop the process-manager unit. Open paper positions keep their broker-side brackets. Run the flat check before restarting. |
| 5. Monitoring | Remove the schedule. |

---

## OP2/OP3 acceptance gate (one page; nothing here is started)

Every line must be **YES with evidence** before the step it guards. One NO stops the run.

| # | Check | Evidence | Guards |
|---|---|---|---|
| 1 | Account and port isolation | readiness tool: paper account, port 4002; no 4001/7496 listener used | OP2.1 |
| 2 | Broker readiness | `service_active_api_port_open`; contract dry run OK | OP2.2 |
| 3 | Trusted market clock | D1 guard + Alpaca clock agree, fresh (< 30 s), session open | every entry |
| 4 | M14 preflight | `risk_decisions` row per entry: `ibkr_paper`, `trade_open`, `allow`; ledger evidence `M14_LIVE` | every entry |
| 5 | Kill switch | engaged → next tick blocks; disengaged → resumes (drill) | OP3 day 1 |
| 6 | Order reconciliation | journal vs broker open orders after each tick; no NEEDS_REVIEW | every tick |
| 7 | No duplicates | `duplicate_orders` OK (one active order per symbol, one broker order per ref/leg) | every tick |
| 8 | Partial fills | an entry still partially filled after the fill timeout goes to NEEDS_REVIEW (`scheduler._entry_not_filled`); no offline test asserts this path, so it is RUNTIME_UNVERIFIED until the paper drill | OP2.3 |
| 9 | Bracket references | every leg carries our `orderRef`; permIds recorded | OP2.3 |
| 10 | Foreign / manual orders | a manual paper order (no `M21L:` orderRef) is never attributed or cancelled; entry on that contract is refused (`foreign_orders_on_contract`; WS2 tests `test_foreign_executions_are_ignored_not_attributed`, `test_refuses_and_never_cancels_a_foreign_order_on_the_contract`) | OP2 drill |
| 11 | Exposure and P&L freshness | M14 exposure/P&L snapshots fresh for `ibkr_paper` before each entry | every entry |
| 12 | Monitoring | WP10 checks every 5 min with `LogSink`; heartbeat and gateway alarms fire in a drill | OP3.5 |
| 13 | Ledger migration | your approval; `.backup` taken; migrate; flag on; `account()` zero violations | OP3.3 |
| 14 | Dashboard restart | your approval; health check OK; D3 shows `by_strategy`/`by_template` | OP3.2 |
| 15 | Abort conditions | the OP2 and OP3 abort lists above, printed at the console before start | all |
| 16 | Rollback | the per-step rollback table above; flat check passes before any restart | all |

Operating parameter to confirm at OP3: the scheduler's `--min-score` (default 60) now gates the **M19 research rank** (0–100). A clean fixture signal ranks about 33, so with 60 it is not traded (integration test E4). Choose the threshold from the OP1 rank distribution; candidates below it are recorded, not traded.

---

## OP4: 10-trading-day soak (B-3)

**Daily, read-only:**
- WP10 open alarms, which must all be acknowledged or recovered;
- the journal vs broker duplicate check;
- the ledger consistency report;
- D3 metrics per template;
- data-freshness share.

**Stop and escalate on:**
- any NEEDS_REVIEW;
- an unattributed broker order;
- a CRITICAL alarm open for more than one session.

**Exit:** 10 consecutive trading days with no unresolved CRITICAL. Attach the daily summary to Issue #3.

**C-5 forward holdout.** Before the soak starts, pre-register (and have attested, `research.integrity.prereg`) the forward window for the template under evaluation. This makes the soak's data **provably** out-of-sample.

---

## OP5: deploy M21 features to the paper lane, then one governance cycle (C-6)

1. Deploy merged WP8, WP9 and WP14 (and WP15 shadow if approved). Templates 2–4 stay **shadow-only**.
2. Create the baseline controls version: `bot.templates.controls`, propose → **you approve** → activate.
   On every start, call `PromotionBook.reconcile(controls)`:
   - it must report no `blocked` and no `diverged` entries;
   - any BLOCKED operation stops OP5 until you resolve it with `resolve_block`, which needs a named human and stores that already show the outcome.
3. One governance cycle with `bot.promotion.PromotionBook`:
   - submit a proposal backed by WP13 / WP16–18 evidence;
   - SHADOW until its signal minimum;
   - ELIGIBLE;
   - **your token** (the mock service today; the TOTP dashboard path once approved);
   - APPLY;
   - MONITORING;
   - CONFIRMED or auto-REVERTED.
4. Confirm the audit trail and the cooling-off rules.

**Accept (C-6):**
- every transition and refusal is in `promotion_events`;
- every cross-store change has a COMPLETED or ABORTED `promotion_ops` row, and none is left PENDING or BLOCKED;
- no AMBER/RED change applied without your token;
- a simulated decay triggers the auto-revert **without touching orders**.
