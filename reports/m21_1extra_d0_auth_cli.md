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
- **never copies** the legacy `ibkr.auto_trading_enabled` into a lane;
- **bounded window**: duration must be `0 < h <= 8.0` (hard cap in code);
- writes **genuine UTC `+00:00`** expiry strings;
- persists through the existing `broker_allocation.save_policy`, which
  **validates** — a malformed or non-UTC expiry would be rejected by the schema;
- `disable` **immediately** clears both `global` and `ibkr_paper` authorization
  (no confirmation needed to revoke);
- if `ibkr_live` is already enabled out-of-band, `enable` **refuses** rather than
  proceed alongside a live authorization.

## Writer / enforcer separation
The CLI is a **writer only**. Enforcement stays in the reader
(`determine_signal_only_reason`), `is_auto_trading_allowed`, and the runtime
cache. The audit table is **evidence only** — nothing reads it to make an
authorization decision (proven by a test asserting the CLI never calls the
reader to gate on audit data).

## Audit table schema (`auto_trading_authorizations`, append-only)
| column           | meaning                                        |
|------------------|------------------------------------------------|
| audit_id (PK)    | sha256(content)[:32] — identical replay dedups |
| action           | `enable` \| `disable` \| `refused`             |
| lane             | `ibkr_paper`                                   |
| operator         | operator identifier                            |
| expiry_utc       | UTC `+00:00` expiry (enable) / null            |
| duration_hours   | requested window / null                        |
| reason           | human-readable reason                          |
| created_at_utc   | UTC `+00:00` timestamp                          |

Append-only via `INSERT OR IGNORE` on the content hash; never updated or
deleted by the CLI.

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
`test_m21_1extra_d0_auth_cli.py` (13): enable requires confirmation; duration
cap enforced; enable writes UTC expiry + authorizes paper (validated policy);
enable never enables ibkr_live; never touches eToro; never copies the legacy
ibkr flag; refuses if live already enabled; disable clears authorization
immediately; real reader allows after enable and blocks after disable; audit
records all actions; audit rows carry UTC `created_at`; audit append-only
(identical replay dedups); audit not read for enforcement.

All Commit 1 safety tests remain green.

## Explicitly NOT in Commit 2
No `ibkr_live` enablement, no eToro/live writes, no scheduler (D2), no
market-clock check (D1), no dashboard (D3), no adapter/B/C edits, no M21.2. The
CLI cannot itself place a trade.
