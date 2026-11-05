# M21 operational readiness + roadmap v3.0: consolidated report for independent review

**For independent review (ChatGPT).**

- **Prepared by:** Claude (Claude Code session).
- **Written:** 2026-11-05T12:04Z UTC (2026-11-05 12:04 GMT Europe/London, GMT).
- **GitHub `main`:** `80b4343db4a22413aa670efd32e24952d53e532e` [GH].
- **VPS deployed:** `015d2cea927b9b4db3ee65604e0b4ebb58211e61` [VPS-R].
- **Roadmap:** v3.0 (DRAFT); status 3.0.1.
- **PR:** #34 (draft; this report is part of it). Checkpoint in Issue #3.

**Nothing in this report was executed on the VPS by Claude. Nothing was merged, deployed, started, stopped or changed.**

The repository is public. This report omits the server's IP address and hostname, the account identifiers and every credential. Evidence classes are as in `ROADMAP.md` (top of "Roadmap v3.0"):
- [GH] verified GitHub state;
- [VPS-R] reported VPS observation;
- [OFF] tested offline only;
- [PR] unmerged PR work;
- [PLAN] planned;
- [RT-U] runtime unverified;
- [DEC] operator decision.

Every VPS finding is also classed **CONFIRMED** (seen in output or code), **INFERRED** (follows from confirmed facts) or **UNKNOWN**.

---

## 1. H1 / P1 / P2 read-only checks

**Status:** the operator approved them on 5 Nov.
- Claude cannot reach the VPS. The laptop bridge offers no shell to the server, and the laptop's key is refused.
- The operator runs the block in the VS Code Remote-SSH root terminal:
  - H1 runs as `nobody`;
  - P1 and P2 run as root, read-only;
  - an H1b addition reads `logs/boot.log` as `nobody`.
- The output is redacted at source:
  - URL credentials, Telegram tokens, `key/token/secret/password` values and IBKR account identifiers are masked;
  - P2b masks client IPs, ports and key fingerprints.
- The block is unchanged from the reviewed version except for narrowing:
  - P2 prints only `systemd[…]`, `[STARTUP]`, `KILL_SWITCH]` and `GW-WATCHDOG]` lines;
  - H1 adds the bot-log start-up lines and the reflog around the bot's last start.

**PENDING.** At the time above, the operator's H1/P1/P2/H1b output had not been received. A follow-up commit on this PR will replace this section with the interpreted output. Each question will be classed CONFIRMED / INFERRED / UNKNOWN:
- the last broker mode;
- when and why the bot stopped;
- the gateway failure;
- the cause of the dashboard start;
- root's crontab and other automation;
- boot history;
- checkout and fetch history;
- the commit the bot last ran (proof vs inference).

§2 and §3 will be updated to match.

## 2. VPS safety findings (concise)

The Session 1 observations are kept as observations [VPS-R]. **They do not establish that the server is safe.**

| # | Finding | Class |
|---|---|---|
| F-1 | **Nothing automatic is deploying now:** `sync.sh` is not running; no timer, system cron, `rc.local`, git hook or Actions runner exists. Root's crontab was unreadable in Session 1 (P1 settles it). | CONFIRMED (crontab: UNKNOWN) |
| F-2 | The checkout is `015d2ce`, clean. `main` (`80b4343`) has never been deployed; the difference is research/docs/tests only. | CONFIRMED |
| F-3 | **The bot is inactive but enabled at boot:** a reboot starts it on code it has never run. | CONFIRMED |
| F-4 | **The kill switch is off:** a bot that starts while the gateway is healthy is not blocked by it. | CONFIRMED |
| F-5 | The gateway has failed and no API port is open, so IBKR submissions would be refused today (`broker_unready`). This is an accident of the current state, **not a control**. | CONFIRMED |
| F-6 | The dashboard runs as root and can start the bot outside systemd (authenticated `/api/start`). | CONFIRMED (code) |
| F-7 | **The most consequential latent path:** `deploy.sh` → `sync.sh` → reset to `main` → `systemctl restart` of the bot, regardless of the enabled state. This corrects the Session 1 report §12-3b. | CONFIRMED (code) |

**Assessment:** the server is idle, but it is **not in a safe resting state**. Three latent paths can start the bot with the kill switch off: a reboot (F-3), `deploy.sh` (F-7) and the dashboard (F-6). Gate A-0 (§4) closes the first two and neutralises the third at the broker. The unknowns that H1/P1/P2 resolve stay open until their output is read.

## 3. What happens if … (A3)

The scenarios describe the state at the time above (before Gate A-0). The last column shows the effect of the A-0 proposal (§4).

| Event | What follows today (before A-0) | Class | With A-0 in place |
|---|---|---|---|
| **1. The VPS reboots** | systemd starts the three enabled units: the bot, the dashboard and the gateway. The bot runs `main.py` from the checkout (`015d2ce`), code it has never run, with `.env` as its environment. The kill switch is off, so it blocks nothing. **If** root's crontab has `@reboot … deploy.sh`, then `deploy.sh` also runs (event 2). | units enabled: CONFIRMED; kill switch off: CONFIRMED; the bot runs `015d2ce`: INFERRED; the `BROKER` value, gateway login and the cron line: UNKNOWN (pending H1/P1) | the bot start is skipped (disabled + condition guard); the dashboard and gateway start; a cron line, if found, is removed; the kill switch is on |
| **2. `deploy.sh` executes** (by hand or by cron) | `pip install --upgrade pip` and `-r requirements.txt` (modifies `venv`); `daemon-reload`; **starts every enabled, inactive unit** (so the bot starts now); removes crontab lines containing `deploy.sh`; restarts `sync.sh`. Within about 60 s `sync.sh` resets the checkout to `origin/main` (`80b4343`), runs pip again, and **restarts the dashboard and the bot whatever their enabled state**. | CONFIRMED from `deploy.sh`/`sync.sh` at `015d2ce` (sha-identical to the deployed copies) | `deploy.sh` skips the disabled bot. `sync.sh` still resets the checkout to `main` and restarts the dashboard. Its bot restart is skipped by the guard (systemd semantics per docs; not tested on real systemd). **The checkout moves**, so `deploy.sh` must not be run. |
| **3. The bot is started from the dashboard** (`/api/start` or `/api/restart`, authenticated POST) | `Popen(venv/bin/python3 main.py)` in a new session, **outside systemd**. The scanner loop runs. Signals that pass risk, M14 and the watchdog go to the `BROKER` broker. While the gateway is down, IBKR submissions are refused (`broker_unready`). With `BROKER=paper`, simulated orders are recorded locally. Telegram alerts may be sent. | code: CONFIRMED; `BROKER`: UNKNOWN (pending H1); dashboard exposure: INFERRED from the reverse proxy on 80/443 (not verified) | the process still starts (the guard is systemd-only), but **every IBKR submission and flatten is refused** (`kill_switch_active`). Signals, DB writes and alerts may still happen. Residual: the dashboard runs as root and can start the bot (hardening, M22 C03). |
| **4. The IB Gateway reconnects** | The gateway alone sends no orders. No bot process is running and no WS2 loop is scheduled. The unit is `failed`; systemd will not restart it by itself, but a reboot will. | no submitter: CONFIRMED (readable cron/timers, processes); the gateway's failure cause: UNKNOWN (pending P2) | the same; and if a bot is started anyway, the kill switch blocks IBKR |
| **5. `main` receives new merges** | **No VPS change**: `sync.sh` is not running, and no timer, runner or hook exists. Vercel creates a Production deployment per merge (Finish Line §7d/§7e.4 A2). Latent: the next `deploy.sh` run or `sync.sh` start deploys whatever `main` is then. | no daemon: CONFIRMED; root's crontab: UNKNOWN (pending P1) | merges still cannot reach the VPS automatically. If `sync.sh` were started, the dashboard would restart on new code but the bot would not start, so **`sync.sh` stays stopped** (A-0d). |

## 4. Gate A-0 proposal: containment, no deployment (NOT executed)

**Approval and scope:**
- Run only after the operator's separate written approval, in the root terminal.
- A-0 changes three things, all reversible: the kill-switch file, the bot unit's boot/start behaviour, and (only if present) one root crontab line.
- It does **not** stop or start any process. It does **not** touch `.env`, the databases, git, the dashboard, the gateway, `sync.sh` or Vercel.

**Purpose (from the assignment):**
- kill switch on;
- no boot-time bot activation;
- the legacy cron entry removed if it exists;
- `sync.sh` kept stopped;
- the state preserved;
- verified rollback.

**Correction to the Session 1 report §12-3b:** `systemctl disable` alone is **not enough**.
- `deploy.sh` respects a disabled unit (CONFIRMED, `deploy.sh` at `015d2ce` lines ~104–117).
- But `deploy.sh` then launches `sync.sh`.
- `sync.sh` at `015d2ce` runs `systemctl restart algo-trader.service` whenever `origin/main` differs from the checkout. It does this **regardless of the enabled state** (CONFIRMED, `sync.sh` lines ~45–62).
- `origin/main` (`80b4343`) already differs from the checkout (`015d2ce`). So if `deploy.sh` runs for any reason, the bot starts within about 60 s on `80b4343`.
- `systemctl mask` is not available either: the unit file is installed in `/etc/systemd/system`, and `mask` refuses to replace it.

A-0 therefore proposes **A-0b2**, a drop-in `ConditionPathExists=` guard. Every systemd start of the bot is then *skipped* until an allow-file exists: boot, `deploy.sh`, the `sync.sh` restart, and a manual `systemctl start`. Nothing fails and nothing else changes.

**What the kill switch covers (A-0a):**
- It blocks every IBKR submission **and** the IBKR flatten path (CONFIRMED, `bot/brokers/ibkr_broker.py` ~240 and ~640).
- If IBKR paper positions are open, they must be closed manually in the Gateway/TWS while it is on.
- It is the only control that also covers a dashboard `/api/start`, which launches `main.py` outside systemd.
- It does not affect `BROKER=paper` local simulation, which places no broker orders.

### 4.1 Commands (one block; runs in a child shell, so a STOP or a failure ends only the block, not the terminal)

```bash
bash -euo pipefail <<'GATEA0'
cd /opt/algo-trader
TS=$(date -u +%Y%m%dT%H%M%SZ)
SNAP=/var/lib/algo-trader/gate_a0/$TS
install -d -m 0700 "$SNAP"
echo "snapshot dir: $SNAP"

# 0. Preconditions (read-only). Any STOP means: do nothing more, report the output.
RUN_RE='[p]ython3?[^ ]* +([^ ]*/)?main\.py( |$)|[b]ash +([^ ]*/)?sync\.sh( |$)'   # bot (any launcher) or sync.sh
if ps -eo pid,args | grep -E "$RUN_RE"; then echo "STOP: main.py or sync.sh is running"; exit 2; fi
[ "$(systemctl is-active algo-trader.service || true)" = inactive ] || { echo "STOP: bot unit not inactive"; exit 2; }
git rev-parse HEAD > "$SNAP/head.txt"
[ "$(cat "$SNAP/head.txt")" = 015d2cea927b9b4db3ee65604e0b4ebb58211e61 ] || { echo "STOP: HEAD is not 015d2ce"; exit 2; }
git status --porcelain --untracked-files=no > "$SNAP/git_status.txt"
[ ! -s "$SNAP/git_status.txt" ] || { echo "STOP: modified tracked files"; exit 2; }

# 1. Preserve (nothing is printed; .env is hashed, not copied)
cp -p data/kill_switch.json "$SNAP/kill_switch.json.before"
[ -f data/bot_state.json ] && cp -p data/bot_state.json "$SNAP/bot_state.json.before" || echo absent > "$SNAP/bot_state.absent"
sha256sum .env > "$SNAP/env.sha256"
stat -c '%a %U %G %s %y %n' .env data/kill_switch.json > "$SNAP/files.stat"
systemctl show algo-trader.service algo-trader-dashboard.service ibgateway.service \
  -p Id,UnitFileState,ActiveState,SubState,FragmentPath,DropInPaths > "$SNAP/units.before"
ls -l /etc/systemd/system/multi-user.target.wants/ > "$SNAP/wants.before"
crontab -l -u root > "$SNAP/root_crontab.before" 2> "$SNAP/root_crontab.err" || true
chmod 600 "$SNAP"/*

# A-0a. Kill switch ON (the repository's own function; writes data/kill_switch.json only)
venv/bin/python3 -I -c 'import sys; sys.path.insert(0, "/opt/algo-trader")
from bot.kill_switch import activate_kill_switch, get_kill_switch_state
activate_kill_switch("Gate A-0 containment, operator-approved, " + sys.argv[1])
s = get_kill_switch_state(); print("kill_switch_active=%s" % s["active"]); sys.exit(0 if s["active"] is True else 3)' "$TS"

# A-0b. No start at boot
systemctl disable algo-trader.service
[ "$(systemctl is-enabled algo-trader.service || true)" = disabled ] || { echo "STOP: unit still enabled"; exit 3; }

# A-0b2. No systemd start by any path until an allow-file exists (covers deploy.sh -> sync.sh -> systemctl restart)
install -d -m 0755 /etc/systemd/system/algo-trader.service.d
printf '[Unit]\n# Gate A-0 %s: the bot starts only while this file exists.\nConditionPathExists=/var/lib/algo-trader/ALLOW_BOT_START\n' "$TS" \
  > /etc/systemd/system/algo-trader.service.d/gate-a0.conf
[ ! -e /var/lib/algo-trader/ALLOW_BOT_START ] || { echo "STOP: allow-file exists"; exit 3; }
systemctl daemon-reload

# A-0c. Only if P1 found a deploy.sh line in root's crontab
if grep -q 'deploy\.sh' "$SNAP/root_crontab.before"; then
  grep -v 'deploy\.sh' "$SNAP/root_crontab.before" > "$SNAP/root_crontab.after" || true
  chmod 600 "$SNAP/root_crontab.after"
  crontab -u root "$SNAP/root_crontab.after"
  echo "A-0c: removed $(grep -c 'deploy\.sh' "$SNAP/root_crontab.before") crontab line(s)"
else
  echo "A-0c: no deploy.sh line in root's crontab; nothing changed"
fi

# A-0d. sync.sh stays stopped: no action (checked in step 0 and again below). A-0e. Gateway: no action.

# 2. Verify (read-only)
python3 -I -c 'import json; print("kill_switch_active=%s" % json.load(open("/opt/algo-trader/data/kill_switch.json"))["active"])'
echo "bot enabled: $(systemctl is-enabled algo-trader.service || true)"
systemctl show -p DropInPaths algo-trader.service
systemctl is-active algo-trader.service algo-trader-dashboard.service ibgateway.service || true
echo "crontab deploy.sh lines: $(crontab -l -u root 2>/dev/null | grep -c 'deploy\.sh' || true)"
ps -eo pid,args | grep -E "$RUN_RE" || echo "no main.py or sync.sh process"
[ "$(git rev-parse HEAD)" = "$(cat "$SNAP/head.txt")" ] && echo "HEAD unchanged" || echo "HEAD CHANGED"
sha256sum -c "$SNAP/env.sha256"
echo "GATE A-0 DONE, snapshot $SNAP"
GATEA0
echo "gate_a0_exit=$?"
```

**Expected result:**
- `kill_switch_active=True`;
- `bot enabled: disabled`;
- `DropInPaths=/etc/systemd/system/algo-trader.service.d/gate-a0.conf`;
- `inactive` / `active` / `failed` for bot / dashboard / gateway (unchanged);
- `crontab deploy.sh lines: 0`;
- no `main.py`/`sync.sh` process;
- `HEAD unchanged`;
- `.env: OK`;
- `gate_a0_exit=0`.

Claude has **not** run this block on the VPS. It was exercised in a mock of the VPS layout, with stub `systemctl`/`crontab` and the real `bot/kill_switch.py` from `015d2ce`; results are in §4.2. It must be reviewed before use.

### 4.2 Offline test of the block (mock, not the VPS)

**Mock layout:**
- `/opt/algo-trader` was a detached checkout of `015d2ce`, carrying the real `bot/kill_switch.py`.
- `data/kill_switch.json` matched the observed content (inactive, "test cleanup").
- `systemctl` and `crontab` were stubs that record calls and state.
- Every test ran from a script file, so the test harness's own command line could not match the process check.

| Test | Expected | Result |
|---|---|---|
| T1: cron has `@reboot … deploy.sh` and an unrelated line | `exit=0`; kill switch True; disabled; drop-in present; one line removed, the other kept; HEAD unchanged; `.env: OK` | pass |
| T2: rollback in reverse order | crontab byte-identical; drop-in removed; enabled; `kill_switch.json` byte-identical (inactive) | pass |
| T3: a `python3 …/main.py` process running | `STOP`, `exit=2`, nothing changed | pass |
| T4: a `bash …/sync.sh` process running | `STOP`, `exit=2`, nothing changed | pass |
| T5: HEAD ≠ `015d2ce` | `STOP`, `exit=2`, nothing changed | pass |
| T6: a modified tracked file | `STOP`, `exit=2`, nothing changed | pass |
| T7: no root crontab | `A-0c: … nothing changed`, `exit=0` | pass |
| T8: crontab without `deploy.sh` | crontab untouched, `exit=0` | pass |
| T9: a `dashboard/app.py` process running | not treated as the bot, `exit=0` | pass |

**Not testable offline:** systemd's behaviour when a condition fails, because the stubs cannot show it.
- Per systemd's documentation, a failing `Condition…=` *skips* the start silently and does not fail the unit. An `Assert…=` would fail the start job ([systemd-devel thread quoting systemd.unit(5)](https://lists.freedesktop.org/archives/systemd-devel/2022-March/047575.html)).
- After A-0 the operator verifies only with `systemctl show -p DropInPaths`. **No test start is attempted.**

### 4.3 Rollback (each step separately; reverse order; each needs approval)

| Step | Undo | Restore only when |
|---|---|---|
| A-0c | `crontab -u root "$SNAP/root_crontab.before"` (the exact previous crontab) | not recommended. The legacy `@reboot deploy.sh` path is superseded by systemd (M15.0). Only if A-0c demonstrably caused a problem. |
| A-0b2 | `rm /etc/systemd/system/algo-trader.service.d/gate-a0.conf && rmdir --ignore-fail-on-non-empty /etc/systemd/system/algo-trader.service.d && systemctl daemon-reload`. For a single supervised start instead: `touch /var/lib/algo-trader/ALLOW_BOT_START`, then remove it after. | at OP3 activation (approved), or for a supervised start the operator approves |
| A-0b | `systemctl enable algo-trader.service` (re-creates the boot link; starts nothing) | at OP3, when the bot is meant to run unattended |
| A-0a | `cp -p "$SNAP/kill_switch.json.before" data/kill_switch.json` (exact restore), or `deactivate_kill_switch("…")` | **only at OP3**, after OP2 gateway acceptance, with explicit approval. Never as part of a merge or deployment. |
| Partial failure | if the block stops after A-0a or A-0b, undo the completed steps in reverse order, or keep them and report (all of them are safe states) | — |

**Restore conditions:** the operator approves OP3, the gateway passes OP2, and the deployed tree is the reviewed merge.

**What A-0 changes:** with A-0 in place, the 23 merges (Gate A-1) cannot start the bot by any automatic path. The only ways remaining are:
- `/api/start` from the authenticated dashboard (blocked at IBKR by the kill switch);
- a manual `main.py` launch.

## 5. Session 2 readiness (NOT executed)

**What is ready:**
- the command block in `docs/reviews/M21_Gate_B_Session1_Report_2026-11-05.md` §13 (docs branch @ `7d19f39`), unchanged;
- the source pinned at `6c82b1a`, with an eight-file sha256 manifest;
- runs as `nobody`;
- writes only `/tmp/m21_op1`;
- stops with exit 2/3/4 on a refusal, a failed copy or a failed integrity check.

**What it answers:**
- historical coverage and completed-bar/timestamp integrity (`m16_inventory.txt`, `parquet_basis.*`);
- old paper activity since 1 June: signals, orders, claimed fills, positions/outcomes and M14 records (`old_paper_signals.txt`, `old_paper_journal.txt`);
- inputs for the E5 Flex reconciliation (`signals_copy.db`, `paper_lifecycles_copy.db`).

**Operator inputs:** two IBKR Flex exports (paper and live, 1 June → the export date) as specified in runbook OP1.6, with `ClientAccountID` kept as exported. Claude does not request or store credentials.

**Independent of H1/P1/P2 and A-0:** Session 2 can run before or after them, and it changes nothing.

**Approval:** it needs its own approval (Session 1 report §14, row 3).

## 6. Roadmap reconciliation (in this PR)

- **`ROADMAP.md` "Roadmap v3.0" (R1–R16):**
  - authority;
  - sources;
  - the numbering reconciliation table;
  - the M21 dimension summary;
  - M22 (M22.1–M22.6; C01–C11; the sub-number mapping is a proposal);
  - M23 (M23.1–M23.5; D01–D10);
  - **M24/M25 UNDEFINED**;
  - Track A / Track B with dependencies;
  - the status vocabulary;
  - the version/timestamp convention;
  - the update protocol;
  - the handoff block;
  - operator decisions D-1…D-8;
  - discrepancies;
  - the append-only changelog.
- **`MILESTONE_STATUS.md`:**
  - a status 3.0.1 block (the 29 Oct block is kept as history);
  - a vocabulary note;
  - a 24/25 row;
  - the pre-June future table labelled historical.
- **Untouched:** the 71 register tasks, the M21 Finish Line, every historical milestone requirement. No renumbering.

## 7. Open questions for the reviewer

1. Is the A-0b2 drop-in guard acceptable as "prevent boot-time bot activation"? Or should A-0 be limited to `disable`, accepting the `deploy.sh → sync.sh → restart` path while the kill switch blocks IBKR?
2. Is the R6 placement of C01–C11 under M22.1–M22.6 acceptable as a *proposal*? Should a dedicated M22.5 kill-switch task be added (D-2b)?
3. Is the Track B rule acceptable: C01 is developed now, but merged only after the OP4 soak?
4. Are the R3 rows for M18–M20 right in treating the delivered meaning as canonical (D-1)?

## 8. Returned items

| Item | Value |
|---|---|
| Exact UTC timestamp | 2026-11-05T12:04Z UTC (2026-11-05 12:04 GMT Europe/London) |
| Latest GitHub `main` SHA | `80b4343db4a22413aa670efd32e24952d53e532e` [GH] |
| VPS deployed SHA | `015d2cea927b9b4db3ee65604e0b4ebb58211e61` [VPS-R, Session 1] |
| Roadmap version | **v3.0** (DRAFT); status 3.0.1 |
| Documentation PR | https://github.com/mikemitakidis/algotrading/pull/34 (draft). Head SHA is in the PR record and Issue #3 (R11). |
| M21 status | **engineering:** ENGINEERING_COMPLETE on 23 draft PRs, A16 NOT_STARTED. **Merge:** not merged (BLOCKED_BY_APPROVAL). **Deployment:** none of the M21 PR code; VPS on `015d2ce`. **Paper:** NOT_STARTED; gateway failed. **Acceptance:** not FULLY_ACCEPTED (R5). |
| M22–M25 | **M22:** controlled live readiness + production hardening (M22.1–M22.6; C01–C11; sub-number mapping proposed, D-2). **M23:** advanced tier M23.1–M23.5 (D01–D05) + extended D06–D10. **M24, M25: UNDEFINED** (D-3). |
| Unresolved operator decisions | **roadmap:** D-1…D-8 (`ROADMAP.md` R14). **Operational:** H1/P1/P2 output, Session 2 + Flex exports, Gate A-0, Gate A-1 (+ C5b, Vercel), Gate A-2, OP2–OP5, G4, the E6 `app.py` patch. |
| Next recommended operational step | interpret H1/P1/P2; then **Gate A-0** (containment, §4); Session 2 in parallel (read-only) |
| Next recommended engineering task | **C01** M14 scanner-path Risk Authority parity, implemented offline on an isolated branch behind a default-OFF flag (`legacy AND M14`), with a draft PR. Merge only after the OP4 soak. |
