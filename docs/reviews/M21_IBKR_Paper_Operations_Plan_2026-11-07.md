# IBKR PAPER automated trading: operational plan (for independent review)

- **Prepared by:** Claude (Claude Code session), 2026-11-07, after Gate A-1 was closed. It follows the Gate A-2
  deployment plan (`M21_Gate_A2_Deployment_Plan_2026-11-07.md`).
- **Status:** PLAN ONLY. Nothing connects to IBKR, nothing is started and no order is placed.
- **Scope:** **IBKR PAPER only.** No IBKR LIVE or eToro LIVE action is authorised or planned. The operator's existing
  IBKR LIVE holding stays untouched and manually managed.
- **Builds on:** the reviewed runbooks OP2–OP5 (`docs/runbooks/M21_OP1-OP5_Runbooks_2026-10-28.md`, canonical
  handoff branch) and the Finish Line gates B and C. Their acceptance and abort lists stay in force; this plan adds
  what the code review below found, and the exact order.
- **Evidence classes:** [GH] code on `main` `963bb89`; [OFF] proved offline in this session; [VPS-R] reported VPS
  observation; [PLAN] planned.

## 0. Bottom line

**Automated paper trading cannot start after Gate A-2 alone.** Five code gaps on `main` block or endanger every
automated entry. Two of them (BLK-1, BLK-4) are proved offline with the real M14 engine; the other three are read
directly from the code. They are small and well defined, but they are code, so they need development, review, a merge
and a deployment before the loop can trade.

| ID | Gap | Effect today | Class |
|---|---|---|---|
| **BLK-1** | M14 refuses an `ibkr_paper` entry unless exposure **and** P&L are fresh for **all four** scopes, including `ibkr_live` and both eToro scopes (combined caps) | every loop entry is blocked with `combined_exposure_unknown`; satisfying it as designed means reading the LIVE IBKR account every 2 minutes | [OFF] probe |
| **BLK-2** | No production IBKR **paper P&L** reader: `tools/ingest_risk_state.py` returns UNKNOWN by design (`NotImplementedError` stub) | `daily_pnl_unknown` blocks every entry (register Legacy D-3 / R2-3) | [GH] code |
| **BLK-3** | Nothing keeps M14 state fresh: exposure must be ≤ 120 s old and P&L ≤ 300 s, but the ingest CLIs are operator-run ("no scheduler") | entries are blocked even after a manual ingest, once the data is a few minutes old | [GH] code |
| **BLK-4** | M14 takes the auto-trading flag from the shared **`ibkr` family** block, not from the D0 `ibkr_paper` lane | enabling paper entries means setting `ibkr.auto_trading_enabled=true`, which M14 also applies to `ibkr_live`. Live stays blocked by other layers, but M14 should never imply live | [OFF] probe |
| **BLK-5** | No process manager for the loop and no runner for the WP10 monitoring checks: the scheduler "installs no service", and nothing imports `bot.monitoring` | the loop cannot run unattended; monitoring cannot fire | [GH] code |

Once these are fixed and deployed, the path to unattended paper trading is:
1. **P0** read-only inventory;
2. **P2** gateway recovery;
3. **P3** supervised first paper trades and drills (OP2);
4. **P4** activation (OP3);
5. **P5** a 10-trading-day soak (OP4);
6. **P6** at least 20 trading days of outcomes, reconciliation and one governance cycle (C-4, OP5).

P0 and P2 can run while P1 (the code fixes) is in review.

## 1. What will place paper orders, and what protects each order

**The engine is the WS2 paper learning loop**, not `main.py`:
- `tools/paper_loop/m21_1extra_d2_3_scheduler.py --broker ibkr-paper --i-understand-this-runs-unattended-ibkr-paper-orders`;
- which drives `bot.paper_loop_d2.scheduler.PaperLearningLoop`;
- which drives `bot.paper_loop_d2.ibkr_adapter.IbkrLoopBroker`;
- which talks to IB Gateway paper on port 4002.

`main.py` stays disabled. Its IBKR path has no M14 preflight (C01 is not merged) and sets no broker-visible order
reference.

**Every entry must pass all of these, each failing closed** [GH]:

| # | Layer | Where | What it enforces |
|---|---|---|---|
| 1 | Persistent kill switch | `bot.kill_switch`, read by the loop and again inside the adapter on every call | any doubt = active |
| 2 | D0 authorisation | `tools/paper_loop/m21_1extra_d0_auth_cli.py` | the `ibkr_paper` lane only; time-boxed, **at most 8 h per enable**; audited; never enables `ibkr_live` |
| 3 | D1 trusted market clock | A09, `bot.paper_loop_d2.risk_gate.verify_market_open` | an Alpaca clock/calendar decision: regular session, fetched less than 30 s ago, a future session close |
| 4 | M14 Risk Authority | `run_risk_preflight(scope=ibkr_paper, AUTO_ALLOWED)` | policy, kill switches, caps, freshness; writes `risk_snapshots`/`risk_decisions` |
| 5 | Paper isolation in the adapter | `_assert_paper` / `_preflight` / `_session` | not live mode; port = 4002 ≠ 4001; account = the expected paper account; `managedAccounts()` must contain it in every session |
| 6 | Loop limits | `LoopConfig` | 1 open trade, 3 entries a day, 1 share, notional ≤ USD 5,000 and ≤ 5 % of equity, equity ≥ USD 1,000, quote ≤ 60 s old, drift ≤ 2 %, no delayed quotes |
| 7 | Ownership and duplicates | journal + adapter | a single-loop lock; `client_ref` primary key; `orderRef` on all three bracket legs, with permIds recorded; crash recovery by `orderRef` (never a resubmit); a foreign order on the contract blocks the entry and is never cancelled; the time-stop closes only the trade's own quantity |
| 8 | Monitoring | WP10 `bot.monitoring` | heartbeat, scheduler liveness, data freshness, gateway, unresolved executions, duplicate orders (needs BLK-5) |

## 2. The blockers in detail (and the proposed fixes)

### BLK-1: M14 needs all four scopes fresh, including IBKR LIVE

**Evidence [OFF].** The real `run_risk_preflight` ran on a temporary DB with a valid policy, for one `ibkr_paper` entry
of USD 100. Fresh M14 state was seeded only for the scopes listed:

| Fresh scopes | Result |
|---|---|
| all four | allowed |
| `ibkr_paper` only | blocked `combined_exposure_unknown` |
| `ibkr_paper` + `ibkr_live` | blocked `combined_exposure_unknown` |
| `ibkr_paper` + both eToro scopes | blocked `combined_exposure_unknown` |
| none | blocked `exposure_unknown` |

**Why.**
- `policy_view_from_allocation_policy` always sets a combined-exposure cap (default USD 50,000), a global
  open-positions cap (10) and a global daily-loss cap (USD 3,000).
- `snapshot._build_global_view` makes the combined value `None` when any scope's exposure or P&L is unknown.
- The engine treats `None` as a block.
- The loop's own tests reach `allow` only through a helper that seeds all four scopes (`_seed_fresh_all_scopes`).

**Why "just ingest all four" is the wrong fix here:**
- `ibkr_live` would need a live gateway session and a live P&L reader, which is not wired. It would also break the
  isolation the operator asked for.
- eToro would need API reads with keys.
- Attesting "zero" for the live scope would be fabricated state, because a live holding exists.

**Proposed fix: paper-domain aggregation in M14.**
- For a **paper** scope, the cross-scope combined/global caps are computed over the paper scopes only.
- Real-money scopes neither gate paper entries nor are consumed by them.
- For real-money scopes nothing changes: all real scopes stay required, and unknown still blocks.
- The paper scope's own exposure and P&L stay mandatory and fresh (fail-closed).
- Tests: the probe above becomes a regression test (paper-only fresh → allow; paper unknown → block; a live entry
  with one real scope unknown → block); plus mutation tests on the scope split.
- **Protected files:** `bot/risk_authority/engine.py` and `snapshot.py` are in the C5b protected list, so this needs
  a pinned protected transition (operator approval).

**Alternative.** Keep M14 unchanged and read the live account read-only every 2 minutes, plus eToro. **Not
recommended** (live isolation; extra credentials; more failure modes).

### BLK-2: IBKR paper P&L is never known

- `tools/ingest_risk_state.py::_build_ibkr_adapter` installs a reader that raises `NotImplementedError` "honestly".
  So `ibkr_paper` P&L is always UNKNOWN, and `_gate_broker_daily_loss` / `_gate_data_staleness` return
  `daily_pnl_unknown`.
- **Fix:** a read-only paper executions reader, built like the reviewed M15.5 positions reader:
  - `readonly=True`, the paper port and the paper account only;
  - `reqExecutions` filtered to the paper account;
  - realised P&L from commission reports, with a missing report = unknown (never 0);
  - the M15.4 health gate first.
- **Tests:** AST-proven no order methods; incomplete data → UNKNOWN; the live scope refused.
- **Register:** Legacy D-3 (account/equity reader) and R2-3.

### BLK-3: freshness is never maintained

- The engine requires exposure ≤ `RISK_EXPOSURE_MAX_AGE_SEC` (120 s) and P&L ≤ `RISK_PNL_MAX_AGE_SEC` (300 s).
- Only `tools/ingest_exposure_state.py` and `tools/ingest_risk_state.py` write `daily_state_per_broker`, and both are
  operator CLIs.
- **Fix (recommended):** in the loop's IBKR wiring, run the `ibkr_paper` exposure and P&L ingestion
  (read-only, a dedicated client id) **immediately before** the risk gate on each entry attempt. If either fails,
  the entry is blocked. Freshness then holds by construction, with no extra scheduler.
- **Alternative:** a 1-minute timer during market hours.
- **Not acceptable:** raising the age limits.

### BLK-4: M14 implies `ibkr_live` auto when paper is enabled

**Evidence [OFF].** With all four scopes fresh:

| Policy | Result |
|---|---|
| D0-style (`ibkr_paper` lane authorised, `ibkr` family auto OFF) | **blocked `broker_auto_disabled`** |
| `ibkr` family auto ON | allowed |

The family flag is fanned out to `ibkr_paper` **and** `ibkr_live` (`_M13_4A_FAMILY_MAP`).

**Live is still blocked today by:**
- the D0 `ibkr_live` lane (absent or expired = deny);
- `BROKER≠ibkr_live`;
- `IBKR_LIVE_CONFIRMED`;
- no live port listening;
- the adapter's paper assertions.

**Fix (same PR as BLK-1):** M14 takes paper authorisation from the D0 `ibkr_paper` lane window. `ibkr_live` needs its
own lane **and** `IBKR_LIVE_CONFIRMED=yes`. The family flag alone never enables either scope.

### BLK-5: no unattended runner and no monitoring runner

Deliverables (reviewed infra, installed by the operator):
1. `algo-paper-loop.service` (oneshot `--once`) and `.timer`:
   - every 15 minutes on weekdays across the US session; the loop's D1 gate still decides whether a session is open;
   - `ConditionPathExists=/var/lib/algo-trader/ALLOW_PAPER_LOOP`, its own allow-file, separate from the bot's;
   - the loop's own lock prevents overlap.
2. A small read-only runner, `tools/monitoring/run_checks.py`:
   - inputs: the journal (read-only), `assemble_health()` and the market clock;
   - output: `LogSink` to `logs/monitoring.log`;
   - plus `algo-paper-monitor.service`/`.timer` every 5 minutes.
3. A daily read-only summary (alarms, journal vs broker duplicates, ledger `account()`, D3 counts) for Issue #3.

### Also in P1 (not blockers, but required before P4)

- **H-1: dashboard bot-start routes.** Five routes start `main.py` outside systemd (F-7). Harden them before any
  kill-switch release: refuse while `/var/lib/algo-trader/ALLOW_BOT_START` is absent, or go through `systemctl`.
  `dashboard/app.py` is protected, so this needs a C5b transition. Register C03 / RD-10a.
- **H-2: `BROKER=paper` for `main.py`.** A `.env` change, applied in P4 before the D0 enable. Once the lane is
  authorised, a `main.py` started by any route can then never reach IBKR. The loop is unaffected.
- **Focus selection (evidence quality, not safety).**
  - The loop scans `get_scan_ready_symbols()[:150]`.
  - That is the **first 150 of 536** scan-ready tickers **in alphabetical order** (A … EA) [OFF].
  - Decide: all 536 (check yfinance/Alpaca request rates), or a reviewed focus ranking (M20.UE / M21.U).
- **Carry-forward:** the cross-lane E2E test (A-1 review F4) moves to `main` with its allow-list lines.

## 3. LIVE isolation (what keeps the live account untouched)

| Control | Status | Verified by |
|---|---|---|
| The VPS gateway runs the **paper** IBC config (`TradingMode=paper`, API port 4002) | to verify | P0 §2 (allowlisted keys only) |
| No listener on 4001 / 7496, ever | today: none [VPS-R] | P0 §4, A-2.0, the WP10 gateway check |
| No live account or live confirmation in `.env`; `BROKER≠ibkr_live` | July clean-up commented them out [VPS-R] | A-2.0 §4, P0 §4 |
| The adapter refuses live mode, a non-4002 port, another account, and a session without the paper account | [GH] tests | P3 drills |
| The readers are paper-only (`ibkr_paper_reader` raises for live) | [GH] | — |
| M14 never implies live (BLK-4 fix) | to build | P1 tests |
| M14 never needs live data for paper (BLK-1 fix) | to build | P1 tests |
| Reconciliation (E5) uses the paper account only; a different `DU…` account → MISMATCH | [GH] PR #31 | P6 |
| The operator's live holding is managed only outside the VPS | operator rule | — |

**Decision D-P-6:** if `/opt/ibc/config.live.ini` holds live credentials (P0 prints only `<set>`/`<empty>`), remove it
from the VPS while no live trading is planned. This is optional and separately approved.

## 4. Phases, commands, acceptance, abort and rollback

Every phase needs its own approval. Claude never runs `deploy.sh` or `rollback.sh`, never handles credentials, and
never places an order. Order-placing steps are run by the operator.

### P0: read-only inventory (any time after A-2.0; repeat after A-2)

`bash paper_p0_readonly.sh` (root; sha256 in §9). It reports:
- gateway unit and processes;
- IBC start-script variables and allowlisted config keys (credentials as `<set>`/`<empty>` only);
- gateway log pattern counts and lifecycle (masked);
- IBKR listeners;
- the `.env` IBKR settings (account match yes/no, never the id);
- the broker policy lanes and caps;
- the M14 `daily_state_per_broker` statuses per scope (flags and ages, no values);
- `risk_decisions` counts;
- the loop journal tables;
- M15.4 health;
- the universe size.

**Look for:**
- `TradingMode=paper`;
- the API port;
- `IbLoginId`/`IbPassword` present;
- the cause of the 1 Sep crash and the 3 Sep exit-1-on-stop;
- login-error patterns;
- the current `ibkr` family caps.

### P1: code fixes (development; offline; draft PRs; independent review; then a merge gate and a deployment)

**PRs, each with tests and mutation checks on the safety guards:**
1. M14 paper-domain aggregation + lane-based auto (BLK-1 + BLK-4, protected transition).
2. IBKR paper P&L reader (BLK-2).
3. In-loop M14 refresh (BLK-3).
4. Units + monitoring runner (BLK-5).
5. Dashboard route hardening (H-1, protected transition).
6. The cross-lane E2E test (F4).

**Integration proof (offline):** the full loop with a fake IB and the real M14 engine. A fresh paper scope gives
SUBMITTED; a stale one gives a block; live is untouched. C5a isolation; the full suite green.

**Then:** a merge gate like A-1 (pinned heads and trees), and a deployment with the A-2 scripts (new pins).

### P2: IBKR Paper Gateway recovery (OP2-0; approval; no order)

1. **Operator.** Confirm the PAPER credentials still work, for example by signing in once with the **paper** user
   (web or TWS login) and signing out again. A lingering session can displace the gateway's. Never use the live user.
2. **Start.**
   ```bash
   systemctl reset-failed ibgateway.service; systemctl start ibgateway.service
   ```
3. **Read-only readiness (after the login completes; up to about 3 minutes):**
   ```bash
   cd /opt/algo-trader
   venv/bin/python3 tools/paper_loop/m21_1extra_b2a_gateway_readiness.py
   ```
   Expect `service_active_api_port_open`, `expected_port=4002`, the paper account verified, and the known open
   orders and positions (0/0 as attested on 5 Nov).
4. **Read-only exposure:**
   ```bash
   venv/bin/python3 tools/ingest_exposure_state.py --scope ibkr_paper --dry-run
   ```
   Expect a known result, not UNKNOWN.
5. **Contract and quote, no order:**
   ```bash
   venv/bin/python3 tools/paper_loop/m21_1extra_b1_ibkr_paper_contract_dryrun.py
   ```
   It must show **live, not delayed**, market data on the paper account. The loop refuses delayed quotes.
   If the data is delayed, that is decision D-P-4.
6. **Stability window:**
   - at least 2 trading days, including one nightly IBC auto-restart and one weekend;
   - P0 §1/§4 and the readiness check each morning;
   - **no 4001/7496 listener at any time**.

**Accept:** every check above passes, with no login error.

**Abort:**
- any live port;
- a different account;
- a login loop;
- `StartLimitBurst` reached.

**Rollback:** `systemctl stop ibgateway.service`. No code or configuration changes.

### P3: supervised first paper trades and drills (OP2.3; B-2: A10, A11, A13)

**Preconditions:**
- P1 deployed and P2 accepted;
- H-1 deployed and `BROKER=paper` set;
- the operator present, in US market hours.

**Steps** (the kill-switch release is an explicit operator action, with the reason recorded):
1. D0 enable for at most 8 hours, by the operator:
   ```bash
   venv/bin/python3 tools/paper_loop/m21_1extra_d0_auth_cli.py enable --duration-hours 6 --operator <name> \
     --i-understand-this-authorizes-unattended-paper-trading
   ```
2. Release the kill switch from the dashboard (authenticated) or the repository function. Record the reason.
3. **One supervised loop tick:**
   ```bash
   venv/bin/python3 tools/paper_loop/m21_1extra_d2_3_scheduler.py --broker ibkr-paper \
     --i-understand-this-runs-unattended-ibkr-paper-orders --once --min-score <D-P-2>
   ```
   Repeat by hand at the 15-minute slots until one entry happens.
4. **Verify the entry:**
   - `orderRef` (`M21L:`) on all three legs; permIds recorded;
   - a `risk_decisions` row `ibkr_paper`/`trade_open`/`allow` for the entry;
   - the journal state;
   - the IBKR paper UI matches.
5. **Drills:**
   - kill switch engaged → the next tick blocks the entry; released → it resumes;
   - restart the gateway during an open position → recovery by `orderRef`, no duplicate;
   - a manual paper order on another contract → never attributed or cancelled;
   - a manual order on the same contract → the entry is refused (`foreign_orders_on_contract`);
   - a late or partial fill if one occurs (otherwise it stays RUNTIME_UNVERIFIED and is noted).
6. **Exit:** a take-profit, stop or time-stop close. Then `tools/paper_loop/m21_1extra_d2_flat_check.py`. Fees are
   final within 72 h.
7. Engage the kill switch and run the D0 disable at the end of the session.

**Accept:** every item of the OP2/OP3 acceptance gate rows 1–10 is YES with evidence.

**Abort:** the OP2 abort list (account, port, missing `orderRef`, missing protective leg, an unmatched fill, a second
order for a `client_ref`, residual exposure, NEEDS_REVIEW, CRITICAL).

**Rollback:** kill switch on; D0 disable; flat check; close the trade's own paper position with the operator tool or
the IBKR paper UI; never the live account.

### P4: activation (OP3; each step its own approval)

1. **Backups:** `.backup` of `signals.db` (the online backup API, as in A-2).
2. `BROKER=paper` in `.env` (if not already done in P3).
3. **A02 ledger migration:**
   ```bash
   venv/bin/python3 -I -c 'import sqlite3,datetime,sys; sys.path.insert(0,"/opt/algo-trader"); from bot.candidate_ledger.schema import migrate; c=sqlite3.connect("data/signals.db"); print(migrate(c, now_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())); c.commit()'
   ```
   Then `CANDIDATE_LEDGER_ENABLED=1`. Rollback: unset the flag (the schema is additive).
4. **Install the BLK-5 units.** Leave the timers disabled and `ALLOW_PAPER_LOOP` absent. Verify they cannot start.
5. **Daily operator routine:**
   - before the US open: D0 enable (≤ 8 h);
   - the kill switch stays released during the soak; if an incident engages it, it is released again only after a
     review;
   - `touch /var/lib/algo-trader/ALLOW_PAPER_LOOP`;
   - `systemctl enable --now algo-paper-loop.timer algo-paper-monitor.timer`;
   - after the close: the D0 window expires by itself, and the operator may also disable it.
6. **First unattended day:**
   - heartbeat OK;
   - no CRITICAL;
   - ledger `journal_consistency` clean, with an empty `non_authorising_risk` list;
   - D3 populating.

**Abort and rollback:** the OP3 lists, plus: stop the timer, remove the allow-file, kill switch on, flat check.

### P5: 10-trading-day soak (OP4, B-3)

**Daily, read-only:**
- WP10 alarms (all acknowledged or recovered);
- the journal vs broker duplicate check;
- ledger `account()` with zero violations;
- D3 per template;
- data freshness;
- no live listener;
- a short summary posted to Issue #3.

**Exit:**
- 10 consecutive trading days with no unresolved CRITICAL;
- zero duplicates and zero unknown exposure;
- one kill-switch drill;
- the market-clock gate observed across a closure or half day if the window allows (US markets: closed 26 Nov,
  half day 27 Nov 2026).

**Before the soak:**
- pre-register the forward holdout (C-5, `research.integrity.prereg`);
- check `needrestart`/unattended upgrades (S2-U).

**Freeze:** no code change during the soak (C01's merge comes after it); a change restarts the count.

### P6: outcomes, reconciliation and governance (C-4, C-6, OP5)

- **After at least 20 trading days of the loop:**
  - the E4 score-vs-outcome report (sample size stated);
  - paper fills vs simulated fills;
  - the E5 fee reconciliation from the operator's IBKR **paper** Flex statements (OP1.6/OP1.7).
- **OP5:** WP8/WP9/WP14 templates (2–4 shadow-only), one Promotion & Approval cycle and an auto-revert drill.
- **M21 is fully accepted only when Finish Line gates A + B + C all hold.** A strategy that fails validation is a
  valid result.

## 5. Emergency procedures (any time)

| Need | Action |
|---|---|
| Stop new entries now | Kill switch ON (dashboard, or as root in `/opt/algo-trader`: `venv/bin/python3 -c 'from bot.kill_switch import activate_kill_switch; activate_kill_switch("operator stop")'`). The adapter re-reads it on every call |
| Stop the loop | `systemctl disable --now algo-paper-loop.timer`; `rm /var/lib/algo-trader/ALLOW_PAPER_LOOP` |
| End the authorisation | `tools/paper_loop/m21_1extra_d0_auth_cli.py disable --operator <name>` |
| Exposure | `m21_1extra_d2_flat_check.py`. Open paper trades keep their GTC take-profit/stop legs at the broker. Close only the loop's own quantity, with the operator tool or the IBKR **paper** UI |
| Gateway | `systemctl stop ibgateway.service` |
| Anything touching LIVE | Not part of this plan. Stop and escalate |

## 6. Monitoring, logging and dashboard visibility

| Need | Exists | Gap / proposal |
|---|---|---|
| Per-strategy and per-template paper metrics | D3 panel (A05/A06) | live after A-2; A06 runtime acceptance in A-2.5 |
| Gateway state | `/api/gateway/health` (M15.4) | — |
| Kill switch, policy | dashboard | — |
| Loop and order journal | `paper_lifecycles.db` | created on the first loop run |
| Risk decisions | `risk_decisions` / `risk_snapshots` | — |
| Candidate traceability | A02 ledger | migration + flag in P4 |
| Alarms (heartbeat, scheduler, data, gateway, executions, duplicates) | WP10 library | **runner + timer (BLK-5)**; `LogSink` first |
| Alerts to the phone | Telegram code (A07) | A08 decision: separate credentials; CRITICAL only during the soak (recommended, separate approval) |
| A dashboard view of alarms and loop status | — | later, read-only (A14/C03). For the soak, the daily Issue #3 summary |

## 7. Operational and performance acceptance

| Gate | Requirement | Evidence | Phase |
|---|---|---|---|
| B-2 | Paper adapter in a real session: account and port isolation; `orderRef` and permIds; real fills and fees; crash/restart; partial and late fills; foreign orders | P3 records + IBKR paper UI + statements | P3 |
| B-3 | 10 consecutive unattended trading days; zero duplicates and zero unknown exposure; NEEDS_REVIEW resolved; kill-switch drill; clock gate across a closure | daily summaries | P5 |
| B-4 | D3 live behind authentication with honest N/A; Phase-1 controls (A16) | A-2.5; A16 is NOT_STARTED (needs WP14 flow) | A-2 / later |
| B-5 | Ledger migrated and on; `account()` 0 violations daily; a full trace candidate → outcome | ledger reports | P4–P5 |
| B-6 | Heartbeat, gateway-down and stale-data alarms fire in a drill; the service restarts and recovers | drill log | P4 |
| C-4 | At least 20 trading days: score vs outcome, fills vs simulated, fees, sample size | E4/E5 reports | P6 |
| C-5 | Forward holdout pre-registered; no synthetic result presented as real | prereg attestation | before P5 |
| C-6 | One governance cycle + an auto-revert drill | `promotion_events` / `promotion_ops` | P6 |

## 8. Register mapping (nothing removed or renumbered)

| Item | Register task |
|---|---|
| BLK-1, BLK-4 | C01 (VPS verification of M14 ingestion; parity), C02 (paper/live separation) |
| BLK-2 | Legacy D-3 (paper account/equity reader), R2-3 |
| BLK-3 | A12 (account equity and source freshness), C01 |
| BLK-5 | A12 (scheduler), A14 (monitoring) |
| H-1 | C03 (RD-10a) |
| P2 | A18 (fix merged; runtime unverified), A10 |
| P3 | A10, A11, A13 |
| P4–P5 | A06, A12, A02, B-3/B-5/B-6 |
| P6 | A13, E4/E5 (A02/A13 extensions), C-4/C-6 |
| Focus selection | M21.U / M20.UE follow-up (evidence quality) |
| F4 cross-lane E2E | A-1 review F4 |

The original 71 tasks plus A18 (72) and the Legacy D-3/D-4/D-5 notes are unchanged. This plan adds no new numbered
task; the blockers map onto existing ones.

## 9. Script checksums

```
4e7187c6338b3fc739f36889ad47d23eb5efbdd2ca92b16609ef76786a06d3fd  paper_p0_readonly.sh
```

It is fetched and verified like the A-2 scripts (A-2 plan §4), from `docs/runbooks/paper/`.

## 10. Decisions for the operator

| ID | Decision | Recommendation |
|---|---|---|
| D-P-1 | Approve the P1 code work (BLK-1…5, H-1, F4) as draft PRs, offline only | **Approve.** Nothing can trade without it |
| D-P-2 | `--min-score` for the M19 research rank (0–100; default 60; a clean fixture ranks about 33) | decide after P3's supervised ticks show the real rank distribution; start conservative |
| D-P-3 | Focus: 150 alphabetical (default), all 536, or a reviewed ranking | a reviewed ranking; all 536 only if request rates allow |
| D-P-4 | If paper market data is delayed: subscribe or share the live account's data with the paper account, or keep the loop blocked | share or subscribe; never allow delayed quotes silently |
| D-P-5 | D0 windows of at most 8 h mean a daily operator enable during the soak | keep (one command a day) for P4/P5; revisit after acceptance |
| D-P-6 | Remove live IBC credentials from the VPS while no live trading is planned | yes, if present |
| D-P-7 | Telegram CRITICAL alerts during the soak (A08) | yes, with separate credentials |
| D-P-8 | Run P0 and P2 while P1 is in review | yes (both are read-only or reversible, with no orders) |

## Appendix A: the two offline probes (reproducible on `main` `963bb89`, Python 3.12.3)

Run from the repository root, offline. They use the M14 test fixtures and the real `run_risk_preflight` on a
temporary database; nothing else is touched.

**BLK-1: which scopes must be fresh for one `ibkr_paper` entry**

```python
# Offline: the real M14 engine on a temp DB. Which scopes must be fresh for an ibkr_paper entry?
import os, sys, sqlite3, itertools
sys.path.insert(0, os.getcwd())
os.environ.setdefault("ETORO_LIVE_ENABLED", "false")
import test_m14_f_preflight as F
from bot.broker_allocation import save_policy
from bot.risk_authority import Authority, run_risk_preflight
from bot.risk_authority.engine import TradeRequest
SC = ("ibkr_paper", "ibkr_live", "etoro_paper", "etoro_real")
def run(fresh):
    fx = F._DB()
    try:
        with fx.conn() as c:
            save_policy(c, F._valid_policy())
            for s in fresh: F._seed_fresh_state(c, scope=s, trading_day=F._today_utc())
        c = sqlite3.connect(fx.path)
        r = run_risk_preflight(c, broker_scope="ibkr_paper",
            request=TradeRequest(symbol="AAA", amount_usd=100.0, side="long", sl=95.0, tp=110.0),
            current_authority=Authority.from_string("AUTO_ALLOWED"), actor="probe",
            audit_source="auto", market_open=True)
        return r.allowed, list(r.reason_codes)[:3]
    finally:
        fx.cleanup()
for fresh in [SC, ("ibkr_paper",), ("ibkr_paper","ibkr_live"), ("ibkr_paper","etoro_paper","etoro_real"), ()]:
    print("fresh=%-45s -> allowed=%s reasons=%s" % (",".join(fresh) or "(none)", *run(fresh)))
```

Output:

```
fresh=ibkr_paper,ibkr_live,etoro_paper,etoro_real   -> allowed=True reasons=[]
fresh=ibkr_paper                                    -> allowed=False reasons=['combined_exposure_unknown']
fresh=ibkr_paper,ibkr_live                          -> allowed=False reasons=['combined_exposure_unknown']
fresh=ibkr_paper,etoro_paper,etoro_real             -> allowed=False reasons=['combined_exposure_unknown']
fresh=(none)                                        -> allowed=False reasons=['exposure_unknown']
```

**BLK-4: the D0 paper lane versus the shared `ibkr` family flag** (all four scopes fresh)

```python
import os, sys, sqlite3
sys.path.insert(0, os.getcwd())
import test_m14_f_preflight as F
from bot.broker_allocation import save_policy
from bot.risk_authority import Authority, run_risk_preflight
from bot.risk_authority.engine import TradeRequest
def run(**ov):
    fx = F._DB()
    try:
        with fx.conn() as c:
            save_policy(c, F._valid_policy(**ov)); F._seed_fresh_all_scopes(c)
        r = run_risk_preflight(sqlite3.connect(fx.path), broker_scope="ibkr_paper",
            request=TradeRequest(symbol="AAA", amount_usd=100.0, side="long", sl=95.0, tp=110.0),
            current_authority=Authority.from_string("AUTO_ALLOWED"), actor="probe", audit_source="auto", market_open=True)
        return r.allowed, list(r.reason_codes)[:2]
    finally: fx.cleanup()
lane = {"ibkr_paper.auto_trading_enabled": True, "ibkr_paper.auto_trading_enabled_until_utc": F._future_utc_iso()}
print("test policy (ibkr family auto ON, paper lane OFF):", run())
print("D0-style (paper lane ON, ibkr family auto OFF):   ", run(**lane, **{"ibkr.auto_trading_enabled": False}))
print("paper lane ON + ibkr family auto ON:               ", run(**lane))
```

Output:

```
test policy (ibkr family auto ON, paper lane OFF): (True, [])
D0-style (paper lane ON, ibkr family auto OFF):    (False, ['broker_auto_disabled'])
paper lane ON + ibkr family auto ON:                (True, [])
```
