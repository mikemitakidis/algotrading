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
`mode=ro` URI; `immutable=1` when no `-wal` file exists, so SQLite creates no side files), runs
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

**Merge blocker (item 14).** `sync.sh` polls GitHub every 60 s; on a new `main` commit it runs `git reset --hard
origin/main` in `/opt/algo-trader`, `pip install -r requirements.txt` and restarts **both** `algo-trader.service` and
`algo-trader-dashboard.service`. If `sync_sh_processes` is above 0, every merge would deploy and restart production.
Before any merge, decide (separate approval): stop the daemon for the merge window and deploy once, deliberately
(recommended), or accept automatic deployment. Do not merge while this is unknown.

**Stop if:** `op1.log` shows `COPY FAILED`, any path outside `/tmp/m21_op1` being written, a secret-screen count
above 0 (review before sharing), any `9_LIVE_SCOPE_ATTEMPT_ESCALATE` row, a non-paper scope holding positions in §9,
or any SQL error other than `no such table`.

**Share:** `MANIFEST.sha256` and the `.txt`, `.csv` and `readiness/` outputs. Keep the `*_copy.db` files; transfer them
only by the route you choose (they are production data, no secrets): E5 needs `paper_lifecycles_copy.db` and
`signals_copy.db`.

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
- **Account ids:** leave `ClientAccountID` (E5 masks it in every output) or replace each id with a placeholder that
  keeps its first two letters (`DU…` paper, `U…` live): E5 uses the `DU` prefix to separate paper from live.
- An Activity Statement CSV is also accepted, but it carries no orderRef or execution id, so it can only produce
  PROBABLE_MATCH (never proof). Use the Flex export.

### OP1.7 Reconciliation (offline, in Claude's workspace; nothing connects to IBKR)
```bash
python -m research.economics.statement \
  --statement paper_trades.csv --statement live_trades.csv --statement-tz America/New_York \
  --journal-db paper_lifecycles_copy.db --legacy-signals-db signals_copy.db \
  --out-json recon.json --out-md recon.md
```
- **Matching order:** claimed execution ids (journal `loop_exec_claims`) → orderRef (`order_ref`, `order_ref:FLAT`) →
  perm id vs `IBOrderID` → otherwise symbol + side + quantity within ±120 s, which is only PROBABLE_MATCH. One
  execution matches at most one claim. Old-system intents recorded no ids: PROBABLE at best.
- **Classes:** MATCHED · MISMATCH (each reason listed: quantity, price, time, fees, non-paper account, claimed
  execution id absent) · PROBABLE_MATCH · UNMATCHED_CLAIM · UNRECORDED_FILL_OF_KNOWN_ORDER · UNATTRIBUTED_EXECUTION.
- **Closed trades (7)** = entry and every exit leg MATCHED (`matched_trade_ids`). **Fees (8)** = statement commissions
  of MATCHED legs; missing commissions are counted, never zero; fee calibration only suggests a multiplier.
- **Live statement:** every execution on a non-`DU` account raises `LIVE_ACCOUNT_ACTIVITY` → stop and escalate.
- Later (after OP3, when the ledger exists): `python -m tools.candidate_ledger.score_outcome --ledger-db <copy>
  --statement-matches recon.json` — only statement-matched trades can carry a score-vs-outcome verdict.

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
