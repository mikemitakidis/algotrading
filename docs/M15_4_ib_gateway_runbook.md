# M15.4 — IB Gateway Operator Runbook

**Status:** ✅ M15.4 CLOSED — VPS-verified 2026-06-02 at commit `073a8bd`.
**Test count on closeout:** `test_m15_4_gateway_health.py` 47/47.
**Live classification on the day of closeout** (recorded for future drift checks):
- `algo-trader.service` — active/enabled.
- `algo-trader-dashboard.service` — active/enabled.
- `ibgateway.service` — active/enabled.
- TCP listeners on 4001 / 4002 — **none** (both ports refused connection).
- Gateway log tail — contains a `Unrecognized Username or Password` style line.
- `bot.gateway_health.assemble_health()` therefore returns: `status = service_active_login_error`, `ready_for_ibkr_trading = False`.
- `/api/health` HTTP 200; `/api/gateway/health` HTTP 401 to unauthenticated callers (expected — see §4).
- **No IB API call was added** by M15.4. AST-asserted absent on every commit.

The headline takeaway: `ibgateway.service` being "active" no longer means the IB API is ready. M15.4 closes that gap.

For the broader project status, see [`../MILESTONE_STATUS.md`](../MILESTONE_STATUS.md)
and [`M14_FINAL_AUDIT.md`](M14_FINAL_AUDIT.md).

---

## §1 — What M15.4 does

M15.4 adds a **read-only point-in-time truth layer** for IB Gateway
state. It does not install, modify, restart, stop, or in any way mutate
`ibgateway.service`. It does not call the IB API. It combines five
read-only sources into a single classified status:

1. `systemctl is-active|is-enabled|show ibgateway.service`
2. TCP connect-and-close probe on ports 4001 (live) and 4002 (paper).
   The connection is closed immediately after acceptance — no IB API
   bytes are sent.
3. Trading mode discovery from `/opt/ibc/start_ibgateway.sh` and
   `/opt/ibc/config*.ini` (read-only inspection).
4. Tail of `/var/log/ibgateway/ibgateway.log` — login/credential
   failure patterns are matched against the last 64 KB.
5. `journalctl -u ibgateway.service --since "30 days ago"` for the
   last 10 lifecycle events plus restart/failure counts.

The endpoint surface is `GET /api/gateway/health`. It is auth-protected
like every other `/api/*` route. The existing `/api/gateway/state` (the
M15.1 historical-events view) is **unchanged** by M15.4.

---

## §2 — Status classification (closed set)

The `status` field is one of:

| Status | Meaning |
|---|---|
| `service_down` | systemd reports inactive / failed / activating / deactivating |
| `service_active_port_closed` | systemd active, no login error in log, expected port not listening |
| `service_active_login_error` | systemd active, log tail matches a credential-failure pattern. **Returned regardless of TCP port state** — see precedence note below. |
| `service_active_api_port_open` | systemd active, expected port has a TCP listener, AND no login error in log |
| `unknown` | one or more required sources were unreadable |

**Login-error precedence (post-56bb5ce patch).** When `login_error_detected=True`, `_classify` returns `service_active_login_error` regardless of whether the TCP listener is accepting connections. Rationale: VPS evidence showed that the gateway can simultaneously (a) leave the TCP socket on 4002 listening AND (b) display a blocking "Unrecognized Username or Password" dialog. In that state, `ib_insync.connect(readonly=True)` times out at the API handshake even though raw TCP works. The earlier behaviour — where port-open won over login-error — produced false `ready_for_ibkr_trading=True` readings and allowed M15.5 to attempt connects that would time out 10+ seconds later. The new behaviour fails closed: `ready_for_ibkr_trading=False` whenever any login-error pattern matches the log tail.

The boolean `ready_for_ibkr_trading` is **true only** when status is
`service_active_api_port_open`. **Even then, the bot must still do its
own IB API negotiation before any order is placed** — M15.4 confirms
the port accepts TCP and no auth dialog is showing, not that the API
session is logged in or healthy. For full session health, the M15.1
watchdog (`bot/gateway_watchdog.py`) does an actual `reqCurrentTime`
ping in a separate background loop; that's the authoritative "API is
up" signal and lives in `/api/gateway/state`.

---

## §3 — Known failure modes (from the M15.4 audit)

### Failure mode A — Login error after restart
**Symptom:** systemd reports `active`, the Java/IBC process is running,
but the API port is not listening. Log tail contains `Unrecognized
Username or Password` or similar.

**Root cause:** IBC successfully launched the Gateway JVM, but the
credentials embedded in `/opt/ibc/config*.ini` are wrong or the account
is locked out (e.g. after a manual web-login that invalidated the
saved session). The gateway window is open but the login dialog is
showing an error, so the API server never starts listening.

**Detection in M15.4:** `status = service_active_login_error`.

**Operator action:**
1. Open the IBC config file: `cat /opt/ibc/config.ini` (paper) or
   `/opt/ibc/config.live.ini` (live).
2. Verify `IbLoginId=` and `IbPassword=` are still correct.
3. If a manual web-login was done recently, sign out of the web first
   so the gateway can establish a fresh session.
4. Restart the gateway with: `sudo systemctl restart ibgateway.service`
   (the only allowed restart action — see §5).
5. Wait 30 s, then re-check `/api/gateway/health`. Status should
   transition to `service_active_api_port_open`.

### Failure mode B — Port closed without log evidence
**Symptom:** systemd active, port closed, log tail has no login-error
match.

**Possible causes:** gateway in startup (still loading), IBC config
disables the API (`IbAutoClosedown=yes` interaction), or the API port
clashes with another listener.

**Detection in M15.4:** `status = service_active_port_closed`.

**Operator action:**
1. Wait 30 s. If the gateway just restarted, the API needs time to
   come up.
2. Check `ss -ltnp | grep -E ':4001|:4002'` — if any other process
   is bound, it's the conflict.
3. Tail the gateway log: `tail -f /var/log/ibgateway/ibgateway.log`.
4. If still stuck after 5 minutes: `sudo systemctl restart ibgateway.service`.

### Failure mode C — Service down
**Symptom:** `systemctl is-active ibgateway.service` returns anything
other than `active`. `NRestarts` may have hit `StartLimitBurst=3`,
giving up.

**Detection in M15.4:** `status = service_down`.

**Operator action:**
1. Check the unit state: `systemctl status ibgateway.service`.
2. Reset the restart counter if the limit was hit:
   `sudo systemctl reset-failed ibgateway.service`.
3. Start: `sudo systemctl start ibgateway.service`.
4. Watch the log for the actual failure: `journalctl -u ibgateway.service -f`.

---

## §4 — Reading the endpoint

```bash
curl -s -b "$COOKIE_JAR" http://138.199.196.95:8080/api/gateway/health | jq .
```

Key fields the operator should check:

| Field | Meaning |
|---|---|
| `status` | The classified state (one of §2 values). |
| `ready_for_ibkr_trading` | True only when status is `service_active_api_port_open`. |
| `systemd_active` | Boolean. False ⇒ gateway not running. |
| `mode` | `paper` / `live` / `unknown`. Comes from start script + IBC config. |
| `expected_port` | 4002 (paper) / 4001 (live) / null (unknown). |
| `tcp.paper_4002`, `tcp.live_4001` | Per-port listener state. True/False/null. |
| `login_error_detected` | Boolean. True ⇒ log tail matched a credential-failure pattern. |
| `login_error_pattern` | The exact substring that matched (handy for debugging). |
| `lifecycle.events` | Last 10 lifecycle events from the unit's journal. |
| `lifecycle.n_restarts_30d`, `n_failures_30d` | Restart-cadence summary. |

Unauthenticated callers receive `{"error":"Unauthorized"}` (the same
`@require_auth` shape every dashboard `/api/*` route uses).

---

## §5 — What M15.4 does NOT do

Explicitly out of scope:

- **No automatic restart of the gateway.** systemd's `Restart=always`
  handles failure-driven restart. The dashboard does not have a
  restart button. There is no Python code anywhere in the M15.4
  surface that calls `systemctl start/stop/restart/enable/disable`.
- **No IB API call.** M15.4 does not call `reqCurrentTime`,
  `ib.connect`, or any other IB API method. The TCP probe opens a
  socket and immediately closes it without sending IB protocol bytes.
  (M15.1's `bot/gateway_watchdog.py` does its own API probe in a
  background loop; that's separate, predates M15.4, and is unchanged.)
- **No order paths.** No `placeOrder`, `cancelOrder`, `modifyOrder`,
  `reqGlobalCancel`. AST-asserted in `test_m15_4_gateway_health.py`.
- **No dashboard restart button.** Not in the HTML, not in the JS,
  not in any new route.
- **No credential auto-recovery.** If credentials are wrong, M15.4
  surfaces the fact via `status = service_active_login_error`. Fixing
  them is an operator action against `/opt/ibc/config*.ini`.
- **No changes to `ibgateway.service` itself.** The
  `infra/systemd/ibgateway.service.documented` file is a **reference
  mirror** for drift detection — it is not installed by any script.
  Modifying the live unit remains a separately approved operator step.
- **No changes to `/api/gateway/state`** (M15.1 historical view) or
  any M14 / eToro / scanner / strategy code.

---

## §6 — Drift detection (next audit checklist)

Whenever the next M15.x audit runs, cross-check these against the
reference mirror at `infra/systemd/ibgateway.service.documented`:

- `ExecStart=` matches `/opt/ibc/start_ibgateway.sh`.
- `Environment=DISPLAY=:99` is still present.
- `Restart=always`, `RestartSec=30`, `StartLimitBurst=3`.
- `StandardOutput=` / `StandardError=` route to
  `/var/log/ibgateway/ibgateway.log` (M15.4 tails this file).
- `User=root` (M15.4 was reconciled against this).

If any of these have drifted, update the mirror in the same commit
that updates this runbook.

---

*M15.4 runbook end.*
