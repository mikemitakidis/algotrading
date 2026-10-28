# M21 operational runbooks OP1–OP5 (preparation only)

**Status:** prepared by Claude on 28 Oct 2026. Nothing here has been run. Every step needs **your explicit approval at the gate shown**.

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

**Done when:**
- the four outputs are attached to Issue #3;
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

**Accept:** first unattended day with a heartbeat OK, no CRITICAL alarm, the ledger `journal_consistency` clean, and D3 populating.

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
- no AMBER/RED change applied without your token;
- a simulated decay triggers the auto-revert **without touching orders**.
