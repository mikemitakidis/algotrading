# M21 operational runbooks OP1–OP5 (preparation only)

**Status:** prepared by Claude on 28 Oct 2026; revised the same evening after the independent review (OP1.6, the abort criteria and the rollback steps for OP2/OP3, and promotion reconciliation in OP5); revised 29 Oct (OP1.6 nine evidence categories, OP1.7 statement reconciliation, OP2/OP3 acceptance gate). Nothing here has been run. Every step needs **your explicit approval at the gate shown**.

**Rule for every runbook:**
- Stop at the first unexpected output.
- Never paste secret values into a ticket or chat.
- Anything that can only be confirmed on the VPS is marked **VPS VERIFICATION REQUIRED**.

**Host facts used** (from the earlier read-only checkpoint scripts; re-check them on the day):
- repository at `/opt/algo-trader`;
- project Python `/opt/algo-trader/venv/bin/python3`;
- IBKR paper API port `4002`.

---

## OP1: one read-only VPS session (gate G2)

**Purpose:**
- data inventory and real-data readiness (B01/E05, WP11);
- a copy of `signals.db` for the A02 legacy replay;
- the M14 `RISK_*` environment check (names only, no values);
- `.env` permission metadata (E07).

**Writes:** only under `/tmp/m21_op1/`.

**Never:**
- writes to `/opt/algo-trader`;
- reads `.env` values;
- restarts a service;
- connects a broker.

### OP1.1 Get the exact code into /tmp (no git operations on the VPS)
```bash
SHA=<head of m21/wp11-readiness (PR #20), or of main once #19/#20 are merged>
mkdir -p /tmp/m21_op1 && cd /tmp/m21_op1
curl -fsSL -o src.tgz https://codeload.github.com/mikemitakidis/algotrading/tar.gz/$SHA
sha256sum src.tgz            # record in the OP1 log
mkdir -p src && tar -xzf src.tgz -C src --strip-components=1
```

### OP1.2 Exchange calendar rows (VPS VERIFICATION REQUIRED)
- WP11 needs a JSON list of `{date, open, close}` rows covering the data window (Alpaca `/v2/calendar` shape).
- Two acceptable sources:
  - (a) the WP5 calendar fetcher run in a separate, approved step (it uses the Alpaca keys);
  - (b) an export you download yourself.
- Save the rows as `/tmp/m21_op1/calendar.json`.
- Do **not** hand-type a calendar.

### OP1.3 Readiness report (read-only)
```bash
cd /tmp/m21_op1/src
PYTHONDONTWRITEBYTECODE=1 /opt/algo-trader/venv/bin/python3 -I -c "import sys; sys.path.insert(0, '.'); \
from research.dataqa.readiness import main; raise SystemExit(main(sys.argv[1:]))" \
  --store /opt/algo-trader/data/historical --provider yfinance --calendar /tmp/m21_op1/calendar.json \
  --symbols "$(ls /opt/algo-trader/data/historical/yfinance/1D | sed 's/.parquet//' | head -50 | paste -sd,)" \
  --timeframes 1D,4H,1H,15m --convention start --out /tmp/m21_op1/readiness
```

**Accept:**
- `report.json` and `report.md` exist;
- `origin=real`;
- the per-symbol convention is `start` (M16 stamps bar starts), so conversion runs.

Expect raw-basis rows to be **INCONCLUSIVE** until corporate-action records are fetched (WP5). That is by design.

**Stop if:**
- any write lands outside `/tmp/m21_op1`;
- the store path is missing.

### OP1.4 Copy of `signals.db` for the A02 replay
```bash
sqlite3 "file:/opt/algo-trader/data/signals.db?mode=ro" ".backup /tmp/m21_op1/signals_copy.db"
sha256sum /tmp/m21_op1/signals_copy.db
```
Transfer the copy off the VPS only by your chosen route. It contains no secrets, but it is production data, so you decide where it goes.

### OP1.5 Environment names and permission metadata (no values)
```bash
stat -c '%a %U %G %n' /opt/algo-trader/.env
grep -oE '^RISK_[A-Z_]+' /opt/algo-trader/.env | sort -u        # names only
```

### OP1.6 Old paper system since June: what really happened (prepared, NOT run)

**Why.** Before any new paper evidence is trusted, we need to know what the old system actually did.

**Rule: a database row is not proof of execution.** These queries only *classify our own records*. Proof needs a broker record:

- **Genuine IBKR paper fills and fees** come only from an IBKR paper-account statement export (Activity or Flex: execution id, time, quantity, price, commission). You download it from Client Portal.
  - The local schema stores **no commissions**.
  - Claude reconciles each claimed row against that export offline.
- **Live-account activity** can only be ruled out from the live account's own statement. The queries can only show whether our code ever *attempted* a live-scope action.

**How.**
- Save the two SQL blocks below as `/tmp/m21_op1/old_paper_signals.sql` and `/tmp/m21_op1/old_paper_journal.sql`. They are also `docs/runbooks/sql/*.sql` on this branch.
- Run them against the OP1.4 copy and a copy of the loop journal, never the live files:

```bash
cd /tmp/m21_op1
# the loop journal, only if it exists (default data/paper_lifecycles.db or $PAPER_LIFECYCLES_DB_PATH)
[ -f /opt/algo-trader/data/paper_lifecycles.db ] && \
  sqlite3 "file:/opt/algo-trader/data/paper_lifecycles.db?mode=ro" ".backup /tmp/m21_op1/lifecycles_copy.db"
sqlite3 -readonly -header -column /tmp/m21_op1/signals_copy.db < old_paper_signals.sql > old_paper_signals.txt
[ -f lifecycles_copy.db ] && sqlite3 -readonly -header -column lifecycles_copy.db < old_paper_journal.sql > old_paper_journal.txt
```

`old_paper_signals.sql` (revised 29 Oct: one section per evidence category). On 29 Oct both files were executed statement by statement through a read-only SQLite connection against empty databases created by the real code (`bot.database.init_db`, `bot.flywheel` tables and migrations, `LoopJournal`): 10/10 and 2/2 statements ran, and a write through the same connection was refused. They have **not** been run on any real database.

```sql
-- OP1.6a  Old paper system since 1 June 2026 — signals.db COPY, read-only (sqlite3 -readonly).
-- A row is a CLAIM made by our own code, never proof of execution. Proof = the IBKR statement (OP1.7).
-- The nine categories are reported separately, no section adds one category to another.
.print '== A. tables present'
SELECT name FROM sqlite_master WHERE type='table' AND name IN
  ('signals','candidate_snapshots','execution_intents','signal_outcomes','risk_decisions','broker_positions',
   'daily_state') ORDER BY name;
.print '== 2. CANDIDATE SIGNALS (scanner output; no order implied)'
SELECT direction, COALESCE(NULLIF(route,''),'-') AS route, strategy_version, COUNT(*) AS n,
       MIN(timestamp) AS first_utc, MAX(timestamp) AS last_utc
FROM signals WHERE timestamp >= '2026-06-01' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
SELECT stage, COUNT(*) AS n, MIN(timestamp) AS first_utc, MAX(timestamp) AS last_utc
FROM candidate_snapshots WHERE timestamp >= '2026-06-01' GROUP BY 1 ORDER BY 1;
.print '== 3. ORDER INTENTS by broker and status (what our code decided to attempt)'
SELECT broker, status, COUNT(*) AS n, MIN(timestamp) AS first_utc, MAX(timestamp) AS last_utc
FROM execution_intents WHERE timestamp >= '2026-06-01' GROUP BY 1, 2 ORDER BY 1, 2;
.print '== 1/4/5/6/9. classification of intents (claims only)'
SELECT CASE
    WHEN lower(broker) LIKE '%live%' OR lower(broker) IN ('etoro_real','etoro_live') THEN '9_LIVE_SCOPE_ATTEMPT_ESCALATE'
    WHEN status = 'paper_logged' OR lower(broker) = 'paper' THEN '1_LOCAL_SIMULATION_NO_BROKER'
    WHEN lower(broker) LIKE 'ibkr%' AND status = 'filled' AND fill_price IS NOT NULL THEN '6_EXECUTION_CLAIMED_NEEDS_STATEMENT'
    WHEN lower(broker) LIKE 'ibkr%' AND COALESCE(broker_order_id,'') <> '' THEN '5_BROKER_ACK_CLAIMED (order id recorded)'
    WHEN lower(broker) LIKE 'ibkr%' AND submitted_at IS NOT NULL THEN '4_SUBMISSION_REPORTED_NO_BROKER_ID'
    WHEN status IN ('risk_rejected','broker_unready','live_safety_blocked','account_mismatch',
                    'connection_failed','not_implemented') THEN '3b_INTENT_NOT_SUBMITTED'
    ELSE '0_UNCLASSIFIED_REVIEW' END AS category,
  broker, status, COUNT(*) AS n, MIN(timestamp) AS first_utc, MAX(timestamp) AS last_utc
FROM execution_intents WHERE timestamp >= '2026-06-01' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
.print '== 5/6. broker identity evidence recorded by our code (counts only, no values)'
SELECT broker, status, COUNT(*) AS n,
       SUM(COALESCE(broker_order_id,'') <> '') AS with_broker_order_id,
       SUM(submitted_at IS NOT NULL) AS with_submitted_at,
       SUM(fill_price IS NOT NULL) AS with_fill_price, SUM(filled_at IS NOT NULL) AS with_filled_at,
       SUM(lifecycle_json LIKE '%permId%' OR lifecycle_json LIKE '%perm_id%') AS mentions_perm_id,
       SUM(lifecycle_json LIKE '%execId%' OR lifecycle_json LIKE '%exec_id%') AS mentions_exec_id,
       SUM(lifecycle_json LIKE '%commission%') AS mentions_commission
FROM execution_intents WHERE timestamp >= '2026-06-01' GROUP BY 1, 2 ORDER BY 1, 2;
.print '== 7. CLOSED PAPER TRADES? signal_outcomes are PRICE-PATH outcomes, NOT fills'
SELECT COALESCE(o.resolution_method,'-') AS method, COALESCE(o.outcome,'-') AS outcome,
       (o.intent_id IS NOT NULL) AS linked_to_intent, COALESCE(i.broker,'-') AS intent_broker,
       COALESCE(i.status,'-') AS intent_status, COUNT(*) AS n
FROM signal_outcomes o LEFT JOIN execution_intents i ON i.id = o.intent_id
WHERE COALESCE(o.resolved_at,'') >= '2026-06-01' OR o.outcome = 'OPEN'
GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5;
.print '== 8. FEES / COMMISSIONS: this schema stores none; commissions come only from the statement (OP1.7)'
.print '== 9. LIVE-SCOPE activity recorded by our code (positions fetched and M14 decisions per scope)'
SELECT broker_scope, COUNT(DISTINCT exposure_batch_id) AS batches, MIN(fetched_at_utc) AS first_utc,
       MAX(fetched_at_utc) AS last_utc FROM broker_positions WHERE date >= '2026-06-01' GROUP BY 1 ORDER BY 1;
SELECT p.broker_scope, p.symbol, p.side, p.qty, ROUND(p.exposure_usd, 2) AS exposure_usd, p.fetched_at_utc
FROM broker_positions p JOIN (SELECT broker_scope, MAX(fetched_at_utc) AS m FROM broker_positions GROUP BY 1) l
  ON l.broker_scope = p.broker_scope AND l.m = p.fetched_at_utc ORDER BY 1, 2;
SELECT broker_scope, requested_action, result, source, COUNT(*) AS n, MIN(taken_at) AS first_utc,
       MAX(taken_at) AS last_utc FROM risk_decisions WHERE taken_at >= '2026-06-01' GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4;
```

`old_paper_journal.sql`:

```sql
-- OP1.6b  WS2/D2 loop journal (paper_lifecycles.db COPY), read-only. data_source separates simulation from IBKR paper.
.print '== 1/3/4/5/6. loop orders by data_source and state (simulated vs IBKR-paper claims)'
SELECT data_source, state, COUNT(*) AS n, MIN(planned_at_utc) AS first_utc, MAX(planned_at_utc) AS last_utc,
       SUM(broker_perm_id IS NOT NULL) AS with_perm_id, SUM(entry_fill_price IS NOT NULL) AS with_entry_fill,
       SUM(exit_fill_price IS NOT NULL) AS with_exit_fill, SUM(entry_fees IS NOT NULL OR exit_fees IS NOT NULL) AS with_fees
FROM loop_orders GROUP BY 1, 2 ORDER BY 1, 2;
.print '== 7/8. closed-trade outcomes by source (fees as recorded by our code; statement is the proof)'
SELECT data_source, record_kind, is_edge_outcome, COUNT(*) AS n, SUM(fees IS NOT NULL) AS with_fees,
       ROUND(SUM(gross_pnl), 2) AS gross_pnl, ROUND(SUM(realized_pnl), 2) AS realized_pnl,
       MIN(exit_filled_at_utc) AS first_exit_utc, MAX(exit_filled_at_utc) AS last_exit_utc
FROM paper_trade_outcomes GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
```

**The nine evidence categories (reported separately, never added together):**

| # | Category | Where it shows | What it proves |
|---|---|---|---|
| 1 | Local simulations | intents `1_LOCAL_SIMULATION_NO_BROKER`; journal `data_source='simulated'` | Nothing reached a broker |
| 2 | Candidate signals | `signals`, `candidate_snapshots` | Scanner output only; no order implied |
| 3 | Order intents | intents by broker/status (3b = blocked before submission) | What our code decided to attempt |
| 4 | Journal-reported submissions | `4_SUBMISSION_REPORTED_NO_BROKER_ID`; journal SUBMITTED without perm id | Our code says it sent an order; no broker id |
| 5 | Broker acknowledgements | `5_BROKER_ACK_CLAIMED`; journal `broker_perm_id` | An id was recorded by our code; proof needs the statement (OP1.7) |
| 6 | Real IBKR paper executions | `6_EXECUTION_CLAIMED_NEEDS_STATEMENT`; journal fills | **Only** statement executions matched in OP1.7 count |
| 7 | Closed paper trades | journal `paper_trade_outcomes` (`ibkr_paper`); `signal_outcomes` are price paths, not trades | Genuine only when both legs match statement executions |
| 8 | Fees / commissions | statement only (`signals.db` stores none; journal fees are our record) | Statement commission per execution |
| 9 | Unexpected live activity | `9_LIVE_SCOPE_ATTEMPT_ESCALATE`; non-paper scopes in positions/M14 | Our code's attempts only; ruling live activity out needs the **live** statement |

`0_UNCLASSIFIED_REVIEW` rows must each be explained.

### OP1.7 IBKR statement reconciliation (prepared, NOT run; needs your exports)

**Inputs you download yourself** (Claude never logs in to IBKR):
- the **paper** account Activity or Flex statement from 1 June 2026, with the Trades/Executions section (execution id, order id/permId, orderRef, symbol, side, quantity, price, time, commission);
- the **live** account Activity statement for the same period, Trades section only, to rule out category 9.

**Procedure (offline, on copies, in this workspace or `/tmp/m21_op1`):**
1. Remove account identifiers from the exports before sharing; keep execution ids, order ids, orderRef, times, quantities, prices and commissions.
2. Match each category-5/6 row and each journal order to statement executions: first on `orderRef` (our `client_ref` / `order_ref`), then on permId, then on symbol + side + quantity within ±2 minutes. One execution can match at most one row.
3. Classify every row: **MATCHED** (category 6 proven; price and quantity agree), **MISMATCH** (matched, different price or quantity), **UNMATCHED_CLAIM** (our record, no execution), **UNATTRIBUTED_EXECUTION** (execution without our orderRef: manual or foreign; never counted as ours).
4. Closed trades (7) = MATCHED entry + MATCHED exit. Fees (8) = statement commissions of those executions.
5. Live statement: **any** execution in the period is reported to you, with whether our code shows a matching category-9 attempt.

**Report:** counts per category and per class; every MISMATCH and UNMATCHED_CLAIM listed; fee totals from the statement. This report, not the database, is the record of what the old paper system did.

**Stop if:**
- any `9_LIVE_SCOPE_ATTEMPT_ESCALATE` row;
- section 9 shows a non-paper scope holding positions;
- any query writes, or errors with anything other than "no such table".

**Done when:**
- the outputs are attached to Issue #3: the four OP1 outputs, the two OP1.6 files and, once you provide the exports, the OP1.7 report;
- the readiness verdicts are reviewed;
- the `RISK_*` names match the M14 expectation.

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
