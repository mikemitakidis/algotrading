# Gate A-2: controlled deployment of M21 to the VPS (plan for independent review)

- **Prepared by:** Claude (Claude Code session), 2026-11-07, after Gate A-1 was formally closed
  ([Issue #3, comment 6712228663](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6712228663)).
- **Status:** PLAN ONLY. Nothing here has been run on the VPS. Every step needs the operator's separate approval.
- **Revision 2 (8 Nov), after ChatGPT's focused review**
  ([Issue #3, comment 6726669904](https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-6726669904)):
  1. `a2_deploy.sh` repeats the **complete** VPS-C immediately before the code switch (stage 4a). This closes the
     window during the up-to-30-minute test stage.
  2. The precheck and P0 turn every required read or integrity failure into an **attention item** (exit 3):
     a missing, unreadable or corrupt `signals.db`; a missing core table; a failed query; an unparseable `.env`;
     a check that crashed. Absent optional databases are listed as optional, not flagged.
  3. The read-only bundle launcher **stops after a VPS-C STOP**, never calls a non-zero result a pass, propagates
     its exit code, and checks checksums with `--strict`.
  Each fix has negative and failure-injection tests (§8).
- **Scripts:** `docs/runbooks/a2/` (this PR). Their sha256 values are in §9. Each one was run on an offline mock of the
  VPS (§8); none has run on the real server.
- **Evidence classes:** [GH] GitHub state; [VPS-R] reported by the operator from the VPS; [OFF] tested offline only;
  [PLAN] planned. The repository is public: no IP address, hostname, account id or credential appears here, and the
  scripts mask them in their output.

## 1. What Gate A-2 does, and what it does not do

**Does** (four steps, each with its own approval; the order is fixed):

| Step | What | Changes on the VPS |
|---|---|---|
| A-2.0 | Read-only precheck | nothing |
| A-2.2 | Back up, test, deploy `main` `963bb89` (tree `f04d1d6`), restart the **dashboard** once | `.git` objects, the checkout, one dashboard restart, a snapshot under `/var/lib/algo-trader/gate_a2/` |
| A-2.3 | Replace the Alpaca **paper** keys; prove them with two read-only Alpaca calls | two lines of `.env`; a root-only backup of `.env` |
| A-2.4 | Rotate the dashboard password (bcrypt), remove the plaintext fallback, keep Google Authenticator, restart the dashboard | three `.env` keys at most; a root-only backup of `.env` |

A-2.1 (backups) is the first stage of the A-2.2 script, not a separate run.

**Does not:**
- start, enable or restart the bot (`algo-trader.service`);
- remove the Gate A-0 guard or release the kill switch;
- start the IB Gateway or connect to IBKR;
- run `deploy.sh`, `sync.sh`, `rollback.sh` or `pip`;
- run a database migration;
- change a Vercel, DNS or Caddy setting.

After A-2 the VPS still cannot trade: the bot unit is disabled and guarded, the persistent kill switch is ON, the
gateway is down, and every broker lane is signal-only [VPS-R, Session 2].

## 2. Starting state

| Item | Value | Class |
|---|---|---|
| GitHub `main` | `963bb893c81c2482a08d78a11b1aafe98f542b51`, tree `f04d1d65dd830894156425f5a870a50591a3a805` (Gate A-1 CLOSED) | [GH] |
| VPS checkout | `015d2cea927b9b4db3ee65604e0b4ebb58211e61`, clean | [VPS-R] VPS-0 at 19:49:45Z, 7 Nov |
| Containment | kill switch on; bot unit disabled and inactive; guard loaded and effective; no `main.py`/`sync.sh` process | [VPS-R] same |
| Dashboard | active since 29 Oct, running `015d2ce` code | [VPS-R] Session 1 / P2 |
| Gateway | down since an explicit stop on 3 Sep | [VPS-R] |

**What `015d2ce..963bb89` changes** (computed from GitHub) [GH]:
- **206 files:** 183 added, 23 modified, 0 deleted.
- **Unchanged:** `requirements.txt`, `.env.example`, `main.py`, `dashboard/app.py`, `deploy.sh`, `sync.sh`,
  `start.sh`, `infra/`, `bot/risk.py`, `bot/brokers/`, `bot/database.py`, `bot/kill_switch.py`, `bot/flywheel.py` and
  `server_scripts/`. The deploy script re-checks this list and stops if any of them differs.
- **No new runtime dependency.** The new `bot/` and `tools/` modules import only packages already in
  `requirements.txt`. `catboost` and `lightgbm` appear only in `research/mllab`, which no service runs. So A-2 runs
  no `pip`. An unpinned `pip install` could also upgrade `ib_insync` or `alpaca-py` silently.
- **Dashboard:** it loads one changed module at start (`bot.runtime.paper_outcome_metrics`, through the D3 view).
  So the dashboard must be restarted right after the checkout. Without that restart it would run a mix of old and
  new code (finding F-10).
- **Defaults:** every new feature stays OFF (ledger writer, template shadow, market-context shadow, E6 view, WS2
  loop). Nothing creates a table at import.
- **Import check:** importing the 17 key modules performs no write, no database connection, no socket and no
  subprocess (tested offline with an audit hook [OFF]).

## 3. Design decisions (and why)

1. **Manual, scripted deployment; never `deploy.sh`/`sync.sh`.** `deploy.sh` restarts every enabled unit and starts
   `sync.sh`, which resets the checkout every 60 s and restarts the bot (F-8). The A-2 script does one `git fetch`,
   verifies the exact commit and tree, and moves the checkout with `git merge --ff-only` (on `main`) or
   `git checkout --detach` (when detached). Git refuses to overwrite local work.
2. **Test the exact commit with the production venv first, without touching production.**
   - A full clone goes into `/var/tmp/gate_a2_tests_<TS>`: `git clone --no-hardlinks` from `/opt/algo-trader`, then
     a checkout of `963bb89`. The suite's guards compare against git history, so a plain file copy is not enough.
     `--no-hardlinks` means production `.git` files are only read, never shared or re-owned.
   - The full suite then runs as `nobody` under C5a isolation (`_test_isolation.py --all`), in a private `/tmp`
     (`unshare --mount` + tmpfs). Some tests write fixed `/tmp` paths, which may already exist on the VPS and
     belong to root.
   - Isolation blocks the network, `.env` and production data. As `nobody`, the run cannot write to
     `/opt/algo-trader` at all.
   - Expected result: `ISOLATED_RUN modules=135 failed=0`, the result on Python 3.12.3 / NumPy 1.26.4 [OFF].
   - This is the only proof that the production venv runs the merged code. Skipping it needs an explicit operator
     flag (D-A2-2).
3. **Consistent database backups before the change.**
   - Each `data/*.db` is copied with SQLite's online backup API, then checked with `quick_check` and hashed.
   - No migration runs, so these are a safety net, not a rollback requirement.
   - `.env` is not copied by A-2.2; it records only `.env`'s size and modification time.
4. **One dashboard restart in A-2.2, and one more in A-2.4.** Keeping them separate means a login problem after
   A-2.4 cannot be confused with a code problem.
5. **Credentials are entered by the operator, never by an AI session.**
   - A-2.3 and A-2.4 refuse to run without an interactive terminal (`/dev/tty`).
   - Keys and passwords are typed at hidden prompts. They never pass through a chat, a file, the shell history or
     command-line arguments.
   - Claude Code on the VPS must not run these two steps.
6. **Containment is re-proved after every step.**
   - `a2_containment.sh` is the reviewed VPS-0 (revision 5); the only change is that the expected commit is an
     argument (`015d2ce` before, `963bb89` after).
   - Every step ends with it.

## 4. The steps

**How each script is run (the same launcher for every step).** This is a root shell on the VPS.
- `C` is the commit that adds these files. It is posted in Issue #3 with the approval.
- Every file is verified against §9 before anything runs.

```bash
( bash -euo pipefail <<'A2RUN'
[ "$(id -u)" = 0 ] || { echo "STOP: not root"; exit 1; }
C=<commit posted in Issue #3>; D=$(mktemp -d /root/gate_a2.XXXXXX); trap 'rm -rf "$D"' EXIT; cd "$D"
for f in a2_containment.sh a2_precheck.sh a2_deploy.sh a2_rollback_code.sh a2_alpaca_keys.sh a2_dashboard_password.sh; do
  curl -fsSL -o "$f" "https://raw.githubusercontent.com/mikemitakidis/algotrading/$C/docs/runbooks/a2/$f"
done
sha256sum --strict -c - <<'SUMS' || { echo "STOP: CHECKSUM MISMATCH. Run nothing."; exit 1; }
<the six lines from section 9>
SUMS
bash a2_precheck.sh < /dev/null   # <- the ONE command of the approved step (see each step below); stdin closed
A2RUN
echo "a2_exit=$?" )
```

For A-2.3 and A-2.4, the launcher must not feed the script through a heredoc, because the script needs the terminal.
Download and verify the files with the same lines, then run the script directly:
`cd "$D" && A2_APPROVAL_REF=… bash a2_alpaca_keys.sh`.

### A-2.0 Precheck (read-only; any time)

Run the three read-only checks with the **read-only bundle launcher** below. It runs them in this order:
1. **VPS-C (`a2_containment.sh 015d2ce…`).** If it does not return 0, the bundle **stops** with
   `READ-ONLY CHECKS: STOP`, exit 3, and the precheck and P0 are not run.
2. **The precheck (`a2_precheck.sh`).**
3. **The IBKR P0 inventory (`paper_p0_readonly.sh`).**

The bundle prints `READ-ONLY CHECKS: OK` (exit 0) **only** when all three return 0. Any other result prints
`READ-ONLY CHECKS: ATTENTION`, which is not a pass, with exit 3. A download or checksum failure gives exit 1 and runs
nothing. Checksums are checked with `--strict`, so a malformed line fails too. The outer subshell returns the same
exit code (`readonly_launcher_exit=`). The exact launcher, with `C` and the three checksums filled in, is posted in
Issue #3 with this revision:

```bash
( bash -euo pipefail <<'A2RO'
[ "$(id -u)" = 0 ] || { echo "STOP: not root"; exit 1; }
C=<commit posted in Issue #3>; D=$(mktemp -d /root/gate_a2_ro.XXXXXX); trap 'rm -rf "$D"' EXIT; cd "$D"
B=https://raw.githubusercontent.com/mikemitakidis/algotrading/$C/docs/runbooks
for f in a2/a2_containment.sh a2/a2_precheck.sh paper/paper_p0_readonly.sh; do curl -fsSL -o "${f##*/}" "$B/$f" || { echo "STOP: download of $f failed"; exit 1; }; done
sha256sum --strict -c - <<'SUMS' || { echo "STOP: CHECKSUM MISMATCH. Run nothing."; exit 1; }
<the a2_containment.sh, a2_precheck.sh and paper_p0_readonly.sh lines from section 9>
SUMS
v=0; bash a2_containment.sh 015d2cea927b9b4db3ee65604e0b4ebb58211e61 < /dev/null || v=$?
echo "vpsc_exit=$v"
[ "$v" = 0 ] || { echo "READ-ONLY CHECKS: STOP. Containment (VPS-C) did not pass; the precheck and P0 were NOT run."; exit 3; }
p=0; bash a2_precheck.sh < /dev/null || p=$?; echo "precheck_exit=$p"
q=0; bash paper_p0_readonly.sh < /dev/null || q=$?; echo "p0_exit=$q"
if [ "$p" = 0 ] && [ "$q" = 0 ]; then echo "READ-ONLY CHECKS: OK (nothing was changed)"; exit 0; fi
echo "READ-ONLY CHECKS: ATTENTION (precheck exit $p, P0 exit $q). Not a pass: review every item above. Nothing was changed."; exit 3
A2RO
rc=$?; echo "readonly_launcher_exit=$rc"; exit "$rc" )
```

It prints:
- **Checkout:** HEAD, branch, modified and untracked files (names only), and the origin URL with credentials masked.
- **Disk:** free space next to `data/`.
- **Virtualenv:** package versions, `pip check`, a freeze hash, and whether `nobody` can use the venv.
- **`.env`:** variable names; an allowlist of non-secret settings (`BROKER`, `IBKR_PORT`, `DATA_PROVIDER`,
  `DASHBOARD_BIND_HOST`, the feature flags, the `RISK_*` limits); whether `ALPACA_BASE_URL` equals the paper URL
  (match or differ only, never the value); duplicates; file modes.
- **Units and dashboard:** unit states; the dashboard listener and its HTTP answers (`/` 200, an authenticated route
  401; `/api/health` is 503 while the bot is stopped, which is expected); a Caddy summary.
- **IBKR ports:** listeners only; a listener on 4001 or 7496 is an attention item.
- **Schedulers:** timers, root's crontab, `needrestart` and unattended upgrades.
- **Databases:**
  - `signals.db` is **required**: it must exist, be readable and return `quick_check=ok`, and it must have
    `portfolio_risk_state`, `execution_intents`, `risk_decisions`, `daily_state_per_broker` and `broker_positions`.
  - Any other database that exists must be readable with `quick_check=ok`.
  - An absent optional database (for example `paper_lifecycles.db`) is not flagged.
  - A check that crashes is itself an attention item.

**Proceed to A-2.2 only if:**
- the precheck is OK, or each attention item has been reviewed;
- the containment check returns exit 0 (`VPS-C OK`).

Expected attention item: the plaintext `DASHBOARD_PASSWORD` (A-2.4 removes it).

**Stop (do not deploy) on any of:**
- checkout not `015d2ce`;
- modified tracked files;
- a branch other than `main` or detached;
- the venv not usable by `nobody`;
- Python ≠ 3.12 or NumPy ≠ 1.26.4;
- `pip check` failing;
- less free disk than 2 × `data/` + 2 GiB;
- `BROKER=ibkr_live`, `IBKR_LIVE_CONFIRMED=yes` or `IBKR_LIVE_ACCOUNT` set;
- a live IBKR port listening.

### A-2.2 Back up, test, deploy, restart the dashboard

Command: `A2_APPROVAL_REF='<Issue #3 approval comment URL>' bash a2_deploy.sh`

Stages:
- Any failure in stages 0–4a stops with **exit 2**, before the code changes.
- Any failure from stage 4b on stops with **exit 3**.
- Every exit prints a read-only state report.
- Nothing is rolled back or retried automatically.

| # | Stage | Checks / actions |
|---|---|---|
| 0 | preconditions | root; approval URL; a lock (`/var/lib/algo-trader/gate_a2.lock`); **VPS-C OK at `015d2ce`**; HEAD `015d2ce`; no tracked change; branch `main` or detached; dashboard active, with its port read from its own socket; free disk |
| 1 | preserve | snapshot dir (mode 700): plan, HEAD, unit states, `pip freeze`, kill-switch file, `.env` size/mtime only. Online backup of every `data/*.db` with `quick_check=ok` and sha256 |
| 2 | fetch + verify | `git fetch` of `main` only. `963bb89` exists, is on GitHub `main`, has tree `f04d1d6` and descends from `015d2ce`. The protected list (§2) is byte-identical. No untracked file at a path the commit adds. If `main` has moved past `963bb89` (for example a docs merge), only `963bb89` is deployed; that is printed |
| 3 | tests | full suite of the exact commit: a `--no-hardlinks` clone in `/var/tmp`, as `nobody`, production venv, C5a isolation, private `/tmp`. Requires `ISOLATED_RUN modules=135 failed=0` |
| 4a | pre-switch | **the complete VPS-C again** (`a2_containment.sh 015d2ce`): guard loaded and effective, allow-file absent, boot link absent, unit disabled and inactive, no `main.py`/`sync.sh` process, kill switch on, HEAD. Also: checkout clean, dashboard still active. The test stage can take 30 minutes; any change in that window stops the run **before** the switch |
| 4b | deploy | `git merge --ff-only 963bb89` (or `checkout --detach`). Then HEAD = `963bb89`, tree = `f04d1d6`, no tracked difference |
| 5 | import check | 17 modules in the production venv, `python -I -B` (no bytecode written, no side effects) |
| 6 | dashboard restart | `systemctl restart algo-trader-dashboard.service`. Active within 60 s, `GET /` = 200, PID changed, no new `Traceback` in `logs/dashboard.systemd.err`, an authenticated route answers 401 without a session |
| 7 | final verify | **VPS-C OK at `963bb89`**; `.env` size/mtime unchanged; unit states recorded |

**Acceptance (all required):**
- the script ends with `GATE A-2 CODE DEPLOYMENT DONE` and exit 0;
- VPS-C is OK at `963bb89`;
- the operator opens the dashboard and sees the D3 paper-metrics panel, which shows honest N/A values (register A06,
  runtime part);
- Issue #3 records the output, with no secrets in it.

### A-2.3 Alpaca PAPER keys (separate approval; run by the operator in an interactive root terminal)

Command: `A2_APPROVAL_REF='<Issue #3 approval comment URL>' bash a2_alpaca_keys.sh`

**Preconditions:**
- VPS-C OK at the current commit;
- `.env` is mode 600 and owned by root;
- `ALPACA_BASE_URL` appears once and equals `https://paper-api.alpaca.markets`;
- `ALPACA_KEY` and `ALPACA_SECRET` each appear exactly once.

**Steps:**
1. A root-only backup of `.env` goes to `/root/algo-trader-env-backups/env.before-alpaca-rotation.<TS>`. It is never
   displayed.
2. Hidden prompts ask for the key ID, the secret, and the secret again. The script refuses (exit 2, nothing
   changed) if:
   - the two secrets differ;
   - the key ID does not start with `PK` (Alpaca paper key IDs do; if yours does not, stop and tell us);
   - the format is unexpected;
   - the values equal the current ones.
3. The script replaces exactly those two lines atomically (mode 600, same owner) and re-reads the file. Every other
   line must be byte-identical.
4. **Read-only check 1:** the repository's D1 market-clock proof, which makes one Alpaca clock/calendar read with
   the new keys. Requires `D1_VPS_VERIFY_OK` and `live_source_proof=passed`.
5. **Read-only check 2 (finding F5 of the A-1 review):** one daily-bar request for SPY with `adjustment=raw`.
   Requires success with at least 10 bars.

No service is restarted: the bot is off, and the dashboard does not use Alpaca.

**Acceptance:**
- `A-2.3 ALPACA PAPER KEYS DONE`;
- the operator confirms in the Alpaca paper dashboard that the regenerated key is the only one listed;
- no key or secret value appears anywhere in the output.

### A-2.4 Dashboard password (separate approval; run by the operator in an interactive root terminal)

Command: `A2_APPROVAL_REF='<…>' A2_ROTATE_SESSION_KEY=yes bash a2_dashboard_password.sh`

**Preconditions:**
- VPS-C OK;
- dashboard active;
- `.env` 600/root;
- `DASHBOARD_TOTP_SECRET` set exactly once. If it is not, the script stops: Google Authenticator must stay enabled.

**Steps:**
1. Root-only backup of `.env`.
2. The repository's own `tools/set_dashboard_password.py --remove-plaintext` runs (two hidden prompts). It writes
   the bcrypt cost-12 hash and removes the plaintext `DASHBOARD_PASSWORD`.
3. The `.env.bak.<ts>` that tool creates inside `/opt/algo-trader` contains the old secrets. The script moves it
   into `/root/algo-trader-env-backups/`.
4. With `A2_ROTATE_SESSION_KEY=yes`, `DASHBOARD_SECRET_KEY` is replaced. This signs out every existing session,
   which matters because the old password was exposed.
5. The script compares old and new `.env` by key name. Only `DASHBOARD_PASSWORD_HASH`, `DASHBOARD_PASSWORD`
   (removed) and `DASHBOARD_SECRET_KEY` may differ. The new hash must be a single bcrypt cost-12 hash, different
   from the old one. **`DASHBOARD_TOTP_SECRET` must be unchanged.**
6. Dashboard restart: active, `GET /` 200, PID changed, no new traceback, authenticated route 401.
7. **Operator:** sign in at the dashboard URL with the new password **and** a Google Authenticator code.

**Acceptance:**
- the operator's successful login (password + code);
- the old password is refused. This is optional; one failed attempt counts toward the 5-per-15-minutes limit.

**Do not use the dashboard's own `/api/settings/password` route** (§7, H-3). It writes a plaintext
`DASHBOARD_PASSWORD`. That value is ignored while the hash exists, but it puts a plaintext password back on disk.

### A-2.5 Post-deployment verification (read-only)

- VPS-C: `bash a2_containment.sh 963bb893c81c2482a08d78a11b1aafe98f542b51` returns exit 0.
- `bash a2_precheck.sh`: the only expected difference from A-2.0 is HEAD = `963bb89`. That is an attention item by
  design, because the precheck pins `015d2ce`; read it as expected. No plaintext password; `.env` 600.
- **The operator, in a browser:**
  - login with password and code works;
  - the D3 panel loads;
  - the gateway-health panel shows the gateway down (expected);
  - the kill switch shows active.
- Record on Issue #3:
  - the outputs;
  - the deployed SHA and tree;
  - the snapshot path;
  - the three acceptance confirmations.

## 5. Rollback (each with its own approval; nothing is automatic)

| Step | Rollback | Notes |
|---|---|---|
| A-2.2 code | `A2_ROLLBACK_APPROVAL_REF='<…>' bash a2_rollback_code.sh` | Requires HEAD `963bb89` and VPS-C OK; `git checkout --detach 015d2ce` (no reset, no branch rewrite); dashboard restart; VPS-C OK at `015d2ce`. No migration to undo. GitHub `main` is not reverted, and nothing on the VPS fetches it (`sync.sh` stays stopped). Tested [OFF] |
| A-2.2 data | Not expected. Restore a `data/*.db` from the snapshot **only** if a `quick_check` fails later. Separate approval, with the bot and dashboard stopped | the snapshot's backups carry sha256 values |
| A-2.3 keys | Re-run A-2.3 with correct keys, or regenerate the key in Alpaca and re-run | Restoring the old file brings back keys that were regenerated (invalid) |
| A-2.4 password | Lockout: re-run `tools/set_dashboard_password.py` over SSH (the operator's Remote-SSH terminal is the out-of-band path). Corrupt `.env`: restore the A-2.4 backup with `install -m 600 <backup> /opt/algo-trader/.env`, then restart the dashboard | The backup holds the old, exposed password; use it only to regain access, then rotate again |

## 6. Migrations: only what is needed, and when

| Store | Needed for | When | Notes |
|---|---|---|---|
| none | A-2 | — | A-2 runs no migration and creates no table |
| WS2 loop journal (`data/paper_lifecycles.db`: `loop_cycles`, `loop_orders`, `loop_meta`, `loop_exec_claims`, `paper_trade_outcomes`) | the paper loop | first IBKR-paper loop run (IBKR plan P4) | created by `LoopJournal` itself (`CREATE TABLE IF NOT EXISTS`); Session 2 found it absent |
| D0 audit (`auto_trading_authorizations` in `signals.db`) | the paper-lane authorisation | the first D0 enable (IBKR plan P4) | created by the D0 CLI |
| A02 candidate ledger (`candidate_ledger*` in `signals.db`) | B-5 traceability, C-4 reports | IBKR plan P4, separate approval | `bot.candidate_ledger.schema.migrate`, additive, idempotent; `.backup` first; then `CANDIDATE_LEDGER_ENABLED=1` |
| template controls / promotion stores | OP5 governance | OP5 | created by their own modules when first used |

## 7. Findings this plan surfaced (not blockers for A-2)

- **H-1. The dashboard can still start `main.py` outside systemd** (five routes, F-7).
  - The Gate A-0 guard is systemd-only.
  - After A-2 the kill switch still blocks IBKR orders, but a dashboard click would run the scanner and its
    writes.
  - Operating rule until fixed: do not use Start/Restart, the Telegram save, or the strategy save/reset.
  - The fix (route hardening) is register **C03** (RD-10a).
  - It becomes a precondition of the IBKR plan's P3 and P4 (see that plan's §2, "Also in P1").
- **H-2. `BROKER=ibkr_paper` in `.env`** [VPS-R, July]. Once the paper lane is authorised for the WS2 loop, a
  `main.py` started from the dashboard would also be allowed to send IBKR paper orders through the older
  `IBKRBroker` path. That path sets no `orderRef`, so the loop would treat those orders as foreign. The IBKR plan
  sets `BROKER=paper` (the local simulator) before any authorisation. The loop does not depend on `BROKER`; only
  `BROKER=ibkr_live` is refused.
- **H-3. `/api/settings/password` writes a plaintext `DASHBOARD_PASSWORD`.** It is ignored while a hash exists, but
  it leaves a plaintext password on disk. To be removed or redirected to the bcrypt flow (C03).
- **H-4. Old `.env.bak*` files in `/opt/algo-trader`.** Their permissions were fixed by A-0s. They still hold old,
  partly exposed, secrets. Proposal (separate approval): move them to `/root/algo-trader-env-backups/`. Delete them
  there once A-2 is verified and the archive-branch credential item is closed.
- **H-5. The paper account id appears in the public repository** (A-1 review F8). Moving it into configuration is a
  hygiene task (C02); it is not a credential.
- **H-6. `needrestart`/unattended upgrades** may restart the dashboard (F-11, S2-U). The precheck prints the mode.
  This matters for the soak, not for A-2.

## 8. Offline tests (mock VPS in this container; not the real server)

The mock:
- a clone at `015d2ce` on `main`, with `origin` = a local bare repo whose `main` is `963bb89`;
- a stub `systemctl` that logs every call and refuses `start`/`stop`/`enable`/`disable`/`daemon-reload` and any
  restart other than the dashboard;
- a real local HTTP stand-in for the dashboard;
- `busctl` 255 against a D-Bus stand-in for systemd's `Unit.Conditions`;
- the Python 3.12.3 / NumPy 1.26.4 venv;
- fake `.env` values.

| Script | Cases | Result |
|---|---|---|
| `a2_containment.sh` | **OK:** at `015d2ce`; at `963bb89` after a checkout. **STOP:** wrong commit, kill switch released, guard not loaded, guard reset by a later drop-in, allow-file present, unit re-enabled, `python3 -u main.py` running. Usage errors | 12/12 |
| `a2_deploy.sh` (tests skipped by the flag) | **Refused before any change** (exit 2, HEAD unchanged): no or invalid approval, kill switch released, modified file, `963bb89` absent from `main`, untracked collision, lock held (the other run's lock survives). **Stopped after the change** (exit 3, report printed): the dashboard does not come back; a new traceback. **Done** (exit 0): from `main`, from detached, and again after a rollback. State-changing `systemctl` calls besides the one dashboard restart: **0**. `.env` untouched | 12/12 |
| `a2_deploy.sh` with the full test stage | **Run 1 stopped correctly at stage 3** (exit 2, code unchanged): 17 modules failed. The copy had been made with `git archive`, and the suite's guards need git history; one test also writes a fixed `/tmp` path that root already owned. **Fixed:** a `--no-hardlinks` clone plus a private `/tmp`. **Run 2:** `ISOLATED_RUN modules=135 failed=0` as `nobody` in about 13 min. The mock origin held **only `main`'s history**, the minimum the VPS can rely on. Then deploy, import check, dashboard restart, VPS-C OK at `963bb89`. Production `.git` files not owned by root: 0. The host `/tmp` file was untouched | PASS |
| `a2_rollback_code.sh` | refused without approval; done after a deployment; refused when there is nothing to roll back | 3/3 |
| `a2_precheck.sh` | runs to completion; **0 of 8 fake secret values** appear in its output; read-only `systemctl` calls only. Two attention items are mock artefacts: the venv is a symlink there, so git lists it as untracked, and the mock venv has no `pip` | PASS |
| `a2_alpaca_keys.sh` | **Refused, `.env` unchanged:** no terminal (as an automated session would run it), no approval, a live-style `AK…` key, mismatched secrets, the current key again, a live `ALPACA_BASE_URL`. **Valid keys:** exactly the two lines change, mode 600, backup made, **0 leaks** in the terminal transcript. The two Alpaca verifications need the network: here they **stopped with exit 3** as designed. **They have not run against Alpaca** | 7/7 |
| `a2_dashboard_password.sh` | **Refused:** no session-key choice; no Google Authenticator secret; mismatched confirmation (`.env` unchanged). **Rotation:** the new bcrypt hash verifies the new password; plaintext removed; TOTP secret byte-identical; session key rotated (or kept with `=no`); no other line changed; the tool's in-repo backup moved out; dashboard restarted; **0 leaks** | 5/5 |
| `paper_p0_readonly.sh` | runs to completion; `IbLoginId`/`IbPassword` printed only as `<set>`; **0 leaks**; read-only `systemctl` calls only | PASS |

Credentials were typed into a pseudo-terminal only after each hidden prompt appeared, as a person would type them.

**Revision 2: failure-injection tests (34/34)**

*Deploy.* A change made **during** the test stage stops the run at the pre-switch VPS-C. Each case exits 2, the code
is unchanged and the report says `A-2 STOPPED BEFORE THE CODE CHANGED`. The injected changes were:
- the guard drop-in no longer loaded;
- the allow-file created;
- the unit re-enabled;
- the boot link restored;
- the kill switch released;
- `sync.sh` started;
- `python3 -u main.py` started;
- HEAD moved;
- the dashboard stopped.

A failing suite also stops with exit 2. Without injection, the run completes, with three VPS-C passes: start,
pre-switch and final. The **full suite again passed (135/0) as `nobody`** with the revision-2 script, and the
deployment completed.

*Precheck.* Each of these is a specific attention item, with exit 3:
- `signals.db` missing;
- `signals.db` corrupt;
- another existing database corrupt;
- a missing core table;
- `.env` unreadable;
- a broken venv (the database check cannot run).

Absent optional databases add no item. 0 leaks.

*P0.* Each of these gives exit 3:
- `signals.db` missing or corrupt;
- `risk_decisions` or `daily_state_per_broker` missing;
- an unparseable policy row;
- a listener on LIVE port 4001;
- the gateway's IBC config at `TradingMode=live`;
- `IBKR_LIVE_ACCOUNT` set (the value masked);
- `.env` unreadable.

A clean baseline gives exit 0, and an absent D0 audit table is reported as optional. 0 leaks.

*Bundle launcher.*

| Case | Result |
|---|---|
| VPS-C STOP | `READ-ONLY CHECKS: STOP`, exit 3; the precheck and P0 **not run** |
| Precheck attention | `ATTENTION`, exit 3, never "OK" |
| Required database missing | `ATTENTION`, exit 3 |
| All clean | `OK`, exit 0 |
| One checksum altered | `CHECKSUM MISMATCH`, exit 1, nothing run |

No temporary directory was left behind. The earlier batteries were re-run on the revision-2 scripts: deploy/rollback
15/15, credentials 12/12.

## 9. Script checksums (sha256, revision 2)

The launcher in §4 checks these six lines with `--strict` (paste them between `<<'SUMS'` and `SUMS`):

```
0691641a3354d768bba6d42f01c0bef03f4cd823230a103d723fb163bb0c8263  a2_containment.sh
bde3dbf3a28fcfe577bc8d92e0fd0dfd6c8f0ed084a4ad8f08e78a318eed4fbf  a2_precheck.sh
4d0b20dd76b52459761a2f8e38dd0facba69f6bb7e04b4ac5d3afcca01569645  a2_deploy.sh
4bd4f7e2fb6432004922b73d73763b340d4bb1a0b6e7569156d762b302ea9121  a2_rollback_code.sh
6d9237b7860f336efbb512cff333c379548f87a4feacb1880bcf092ec8173362  a2_alpaca_keys.sh
ab78a574209ad64d5e6da338410f40f7a752a74a12ad2574be16e0831ee22b62  a2_dashboard_password.sh
```

The read-only bundle also uses `paper_p0_readonly.sh`:

```
16d44c737283d457181c5ad057c133d495910fee4fae5a104ef0f196bb176127  paper_p0_readonly.sh
```

## 10. Decisions for the operator

| ID | Decision | Recommendation |
|---|---|---|
| D-A2-1 | Approve A-2.2 (code + dashboard restart) | **Approve** after the independent review of this plan and an A-2.0 precheck |
| D-A2-2 | Run the full test suite on the VPS venv before switching (adds about 15–30 min) | **Yes** (never skip without a reason) |
| D-A2-3 | Root-only `.env` backups in `/root/algo-trader-env-backups/` before A-2.3/A-2.4; retention | **Yes**; delete them after A-2 is verified and the credential items are closed |
| D-A2-4 | Rotate the session key with the password (signs everyone out) | **Yes** |
| D-A2-5 | Approve A-2.3 (Alpaca paper keys) and A-2.4 (password), each run by you | **Approve**, after A-2.2 is accepted |
| D-A2-6 | Move the old in-repo `.env.bak*` files out of `/opt/algo-trader` (H-4) | Yes, as a small separate operation |
