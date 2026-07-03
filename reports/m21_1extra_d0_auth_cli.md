# M21.1extra-D0 Commit 2 — operator authorization CLI + audit trail

## What this adds
The **authorized, auditable** way to turn unattended **paper** auto-trading on
and off. Commit 1 built the enforcement (the reader + `is_auto_trading_allowed`
+ the runtime cache all require a valid, unexpired, per-lane, time-boxed
authorization). Commit 2 builds the **writer** that sets that authorization —
replacing the ad-hoc `/tmp` isolated-DB hack used during the B2b proof with a
real operator control.

`tools/paper_loop/m21_1extra_d0_auth_cli.py`:
- `enable_ibkr_paper(...)` / `disable_ibkr_paper(...)` — writers
- `_audit_write` / `read_audit` — append-only audit trail
- `main()` — `enable` / `disable` / `status` subcommands

## Hard safety properties (enforced in code)
- **operator-only**, behind an ugly confirmation flag on `enable`;
- **refuses on any hostile/non-target state** (checked on the raw loaded
  policy, before any write): `global.kill_switch`, `ibkr_paper.kill_switch`
  (else a "successful" enable would be silently blocked by the reader — a fake
  ENABLED), `ibkr_live.auto_trading_enabled`, `etoro.auto_trading_enabled`,
  `routing.etoro_live_enabled`. Refusal writes an audit row and leaves the
  policy unchanged. Never silently clears a kill switch or a non-target flag;
- **non-destructive by construction** — `_ensure_d0_fields` is *add-missing-only*
  (adds absent D0 schema fields for old pre-D0 DBs) and **never modifies an
  existing value**. `enable` sets only its four target fields; `disable` clears
  only its four target fields. Non-target flags (eToro, ibkr_live, kill
  switches, capital/limits) are always preserved;
- **disable preserves non-target state** — clears only
  `global.auto_trading_enabled(+expiry)` and `ibkr_paper.auto_trading_enabled
  (+expiry)`; if the resulting policy can't validate, it **refuses** rather than
  silently rewriting non-target fields;
- **no `assert` for safety** — all pre-write safety checks are explicit runtime
  checks that raise `AuthorizationRefused` (asserts are stripped under `python
  -O`, so they must never guard safety);
- **no fake ENABLED** — `enable` guarantees `ibkr_paper` is in
  `routing.allowed_brokers` (keeping `route_overrides`/`default_broker`
  consistent) so the real reader genuinely allows `ibkr_paper` afterwards;
- **never copies** the legacy `ibkr.auto_trading_enabled` into a lane;
- **safety booleans use `is True`**, never `bool(...)` (a truthy string like
  `"false"` is never read as enabled; a malformed non-boolean causes a clean
  validation refusal);
- **bounded window**: duration `0 < h <= 8.0` (hard cap); genuine UTC `+00:00`
  expiries;
- **old-policy safe (add-missing-only):** an `_ensure_d0_fields` step adds any
  missing D0 schema fields (`ibkr_paper` / `ibkr_live` /
  `global.auto_trading_enabled_until_utc`) to an old pre-D0 `signals.db` row so
  it can be mutated + validated without `KeyError`. It is **add-missing-only**:
  it **never modifies an existing value**, so no non-target flag is ever
  silently changed. `enable` sets only its four target fields and refuses
  hostile/non-target states first; `disable` clears only its four target
  fields;
- validates via `validate_policy` before writing (rejects malformed/non-UTC
  expiry, or a malformed non-target field, as a clean audited refusal);
- **atomic + auditable by construction (Blocker 2 fix):** on `enable`/`disable`
  the audit row and the policy row are written on the **same `signals.db`
  connection in one transaction and committed together**. If the audit write
  fails, the transaction rolls back and authorization is left unchanged — the
  system can never be enabled without a matching audit row;
- `disable` **immediately** clears both `global` and `ibkr_paper` authorization
  (no confirmation needed to revoke);
- if `ibkr_live` is already enabled in the current policy, `enable` **refuses**
  (checked on the raw loaded policy, before normalisation).

## Writer / enforcer separation
The CLI is a **writer only**. Enforcement stays in the reader
(`determine_signal_only_reason`), `is_auto_trading_allowed`, and the runtime
cache. The audit table is **evidence only** — nothing reads it to make an
authorization decision (AST-proven test).

## Audit table (`auto_trading_authorizations`, in signals.db, append-only)
Lives in the same `signals.db` as the policy (so enable/disable are atomic with
audit). Columns: `audit_id` (PK, sha256(content)[:32] — identical replay
dedups), `action` (`enable`/`disable`/`refused`), `lane` (`ibkr_paper`),
`operator`, `expiry_utc` (UTC `+00:00` or null), `duration_hours`, `reason`,
`created_at_utc` (UTC `+00:00`). `INSERT OR IGNORE`; never updated/deleted.
Refusals commit on their own (no policy change); enable/disable commit the
audit row + policy row together.

## CLI usage
```
# authorize paper auto-trading for 2 hours (ugly confirm flag required)
python3 tools/paper_loop/m21_1extra_d0_auth_cli.py enable \
    --duration-hours 2 --operator mike \
    --i-understand-this-authorizes-unattended-paper-trading

# revoke immediately
python3 tools/paper_loop/m21_1extra_d0_auth_cli.py disable --operator mike

# show current authorization + audit tail
python3 tools/paper_loop/m21_1extra_d0_auth_cli.py status
```

## Tests
`test_m21_1extra_d0_auth_cli.py` (41): enable requires confirmation; duration
cap enforced; enable writes UTC expiry + authorizes paper (validated policy);
enable never enables ibkr_live; never touches eToro; refuses if live already
enabled; **works on old pre-D0 policy rows (normalisation) — Blocker 1**;
**legacy ibkr enable not copied into paper or live; normalised policy
validates**; **audit failure during enable leaves policy unchanged (atomic) —
Blocker 2**; successful enable writes both policy and audit; disable clears
authorization immediately; real reader allows after enable and blocks after
disable; audit records all actions; audit rows carry UTC `created_at`; audit
append-only (identical replay dedups); audit not read for enforcement.

All Commit 1 safety tests remain green.

## Explicitly NOT in Commit 2
No `ibkr_live` enablement, no eToro/live writes, no scheduler (D2), no
market-clock check (D1), no dashboard (D3), no adapter/B/C edits, no M21.2. The
CLI cannot itself place a trade.

---

## Real VPS proof (2026-07-03, authorization gate only — no broker, no orders)

The D0 authorization gate was proven on the real VPS `signals.db` at commit
`314524a36af896ce84c7e99079b22d1565ce5767`. The proof is scoped to the
**authorization gate only** — it does not exercise a broker connection, order
placement, a scheduler, or any market-clock logic. Those belong to later
milestones (D1 online market-clock guard, D2 unattended loop, D3 dashboard) and
are explicitly **not** covered here.

**Method.** On the real `/opt/algo-trader/data/signals.db`: back up the DB;
enable `ibkr_paper` for a real ~7.2s bounded window (`duration_hours=0.002`);
verify both readers immediately; sleep 12s past the window; re-verify both
readers; disable; verify final safe state; then restore the DB from the backup
and confirm it is byte-identical (`cmp -s`). Status reads used raw read-only SQL
so the context reads never create the audit table. Both enforcement paths were
checked at each step: `determine_signal_only_reason(policy, lane)` and
`runtime_policy.get_signal_only_reason(lane, db_path=DB, ttl_sec=300)`.

**Result — all checks true; `M21_1EXTRA_D0_VPS_VERIFY_RC=0`:**

- After enable, `ibkr_paper` is allowed by **both** readers; `ibkr_live`,
  `etoro_paper`, `etoro_real`, and bare `ibkr` are blocked by **both** readers.
- Legacy `ibkr.auto_trading_enabled=True` (with both lanes disabled and no
  expiry) authorizes **nothing**: `ibkr_paper`, `ibkr_live`, and bare `ibkr`
  all blocked.
- After the real window elapsed (12s sleep, runtime cache **not** cleared),
  `ibkr_paper` is blocked by **both** readers — proving the time-box is enforced
  at read time and the runtime cache does not serve a stale allowed result past
  expiry.
- After `disable`, `ibkr_paper` is blocked by both readers and both
  `global.auto_trading_enabled` and `ibkr_paper.auto_trading_enabled` are false.
- The enable audit row was written into the same `signals.db`.
- The DB was restored byte-identical to the pre-proof backup
  (`RESTORE_VERIFIED`).

**Scope reminder.** This proves the gate decides authorization correctly and
fail-closed. It is not evidence about market-open logic, unattended scheduling,
or dashboards — D0 is the master switch only.
