# M13.5 — eToro Live Write Operator Runbook

This is the numbered checklist for placing the **first** real-money eToro
order through the M13.5.B live writer. Read it fully before starting.

> ⚠️ A real order spends real money. Every step here is deliberate. The
> system is designed so that skipping or fat-fingering any step results
> in **no order**, not a wrong order.

---

## 0. Preconditions (verify once)

- [ ] Funded eToro account, logged in to the eToro **web UI** in a
      browser you control (you will close the position manually there).
- [ ] eToro public API keys issued (real-money keys).
- [ ] `.env` on the server contains `ETORO_REAL_API_KEY` and
      `ETORO_REAL_USER_KEY`.
- [ ] You know the eToro **InstrumentID** of the symbol you intend to buy.
- [ ] The amount you intend to spend is within all dashboard caps
      (single-trade, broker capital, global capital) and ≥ the eToro
      platform minimum.

## 1. Enable the policy flag (dashboard)

1. Open the dashboard → **Risk** page.
2. Set eToro **auto-trading enabled** = ON and global auto-trading = ON.
3. Add `etoro_real` to **allowed brokers**.
4. Set **etoro_live_enabled = true**.
5. Confirm the caps (single-trade, broker capital, daily loss, open
   positions) are what you intend. Save.

## 2. Enable the env flag (server, one line)

This is the single manual terminal action this runbook requires. It sets
the second of the two live flags. Replace nothing — copy-paste exactly:

```
cd /opt/algo-trader && grep -q '^ETORO_LIVE_ENABLED=' .env && sed -i 's/^ETORO_LIVE_ENABLED=.*/ETORO_LIVE_ENABLED=true/' .env || echo 'ETORO_LIVE_ENABLED=true' >> .env
```

> To **disable** live writes again afterwards, set it back to `false`
> (same command with `false`), or just remove the line.

## 3. Place the order (operator CLI, single process)

The `oneshot` subcommand prepares the intent, runs the 16-gate preflight,
prints a per-payload **nonce**, waits for you to confirm, issues exactly
one POST, then polls up to 5×2s for the result. Example (1 share-ish of a
$10 position, no leverage, manual close plan):

```
cd /opt/algo-trader && python3 tools/etoro_live_write.py oneshot \
    --instrument-id <INSTRUMENT_ID> --amount 10.0 --symbol <SYMBOL> \
    --leverage 1 --market-open --quote-age-sec 2 --spread-bps 5 \
    --open-positions 0 --realised-daily-loss 0 \
    --close-plan "manual close via eToro web UI"
```

- The CLI prints a confirmation block with a `NONCE:` value.
- At the `CONFIRM>` prompt, type exactly: `CONFIRM <that-nonce>`.
- Anything else (wrong nonce, blank, Ctrl-C) → the intent is recorded as
  `cancelled` and **no POST is made**.

### What each outcome means

| Printed result | DB status | Your next action |
|---|---|---|
| `FILLED. positionID=…` | `filled` | Done. Manage/close in eToro web UI per your plan. |
| `BROKER REJECTED` | `broker_rejected` | Inspect `data/etoro_audit.log`. No position opened. |
| `CANCELLED by broker` | `cancelled` | No position. Investigate before retrying. |
| `⚠ UNVERIFIED` | `unverified` | **Do NOT re-run.** Go to step 4. |
| `ABORT (preflight)` | `policy_rejected`/`risk_rejected` | Fix the named gate, start over. |

## 4. If the result is UNVERIFIED

The POST may or may not have reached eToro. **Never re-run the command** —
that risks a duplicate order. Instead:

1. Open the eToro web UI and check your open positions / order history.
2. Decide the true outcome, then record it with the reconciliation tool
   (this NEVER places or cancels an order — it only writes the verified
   status into the existing intent row):

   If it actually filled:
   ```
   cd /opt/algo-trader && python3 tools/etoro_reconcile.py mark-filled <INTENT_ID> --evidence '{"position_id": <PID>, "fill_price": <PRICE>, "fill_qty": <QTY>}'
   ```

   If it was rejected / never placed:
   ```
   cd /opt/algo-trader && python3 tools/etoro_reconcile.py mark-rejected <INTENT_ID> --note "verified in web UI: no position"
   ```

3. Inspect any intent at any time:
   ```
   cd /opt/algo-trader && python3 tools/etoro_reconcile.py show <INTENT_ID>
   ```

## 5. Closing the position

For the first live write the close plan is **manual**: close the position
yourself in the eToro web UI. Then record it:

```
cd /opt/algo-trader && python3 tools/etoro_reconcile.py mark-closed-manual <INTENT_ID> --note "closed in web UI"
```

## 6. Stand down

When finished testing, set `ETORO_LIVE_ENABLED=false` in `.env` (step 2,
with `false`) and/or set `etoro_live_enabled = false` in the dashboard.
Either one alone is sufficient to hard-disable all future live writes.

---

### Safety reminders

- No automatic retries; no second POST on uncertainty — ever.
- The scanner can never trigger a live write. Only this CLI can.
- Credentials are never printed and never written to the audit log.
- The audit log lives at `data/etoro_audit.log` (rotating, redacted).
