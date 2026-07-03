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
- **ibkr_paper lane only** — `enable` sets `global` + `ibkr_paper` authorization;
  it **never** enables `ibkr_live` and **never** touches any eToro/live flag;
- **refuses if any NON-TARGET authorization is active** — because writing
  `global=True` could otherwise make a pre-existing non-target lane *become
  allowed* by the reader (e.g. `etoro_paper` if `etoro.auto_trading_enabled` was
  already on). Before enabling, `enable` inspects the **raw** loaded policy and
  refuses (audit + no change) if any of these is active:
  `ibkr_live.auto_trading_enabled`, `etoro.auto_trading_enabled`,
  `routing.etoro_live_enabled`. Refusal (not silent disabling) — the operator
  resolves the other authorization explicitly;
- **no fake ENABLED** — normalisation guarantees `ibkr_paper` is in
  `routing.allowed_brokers` (and keeps `route_overrides`/`default_broker`
  consistent with it), so a successful `enable` genuinely allows `ibkr_paper`
  under the real reader — the CLI never prints ENABLED while the reader would
  still block it;
- **never copies** the legacy `ibkr.auto_trading_enabled` into a lane;
- **safety booleans use `is True`**, never `bool(...)` (so a truthy string like
  `"false"` can never be read as enabled);
- **bounded window**: duration must be `0 < h <= 8.0` (hard cap in code);
- writes **genuine UTC `+00:00`** expiry strings;
- **old-policy safe (Blocker 1 fix):** a `_normalise_policy_for_d0` step rebuilds
  the policy on the `DEFAULT_POLICY` structure before mutation, so an old
  pre-D0 `signals.db` row missing `ibkr_paper` / `ibkr_live` /
  `global.auto_trading_enabled_until_utc` is handled without `KeyError`.
  Normalisation preserves capital/limits/kill_switch but forces every
  authorization field disabled/null — it **never** carries a legacy enable into
  a lane and **never** enables `ibkr_live`;
- validates via `validate_policy` before writing (rejects malformed/non-UTC
  expiry);
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
`test_m21_1extra_d0_auth_cli.py` (27): enable requires confirmation; duration
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
