# M15.0 — Canonical Systemd Service Map

This is the **authoritative operator reference** for how the bot, dashboard,
and ancillary processes are supervised on the Hetzner VPS as of M15.0.

For the M14 closeout audit and broader project status, see
[`M14_FINAL_AUDIT.md`](M14_FINAL_AUDIT.md) and [`../MILESTONE_STATUS.md`](../MILESTONE_STATUS.md).

---

## §1 — Audit finding (pre-M15.0)

The M15.0 read-only VPS audit (2026-06-02) found:

- `main.py` and `dashboard/app.py` were **running but NOT under systemd**.
- Both processes were owned by a **login/session cgroup**: `/user.slice/user-0.slice/session-1984.scope`.
- **No** `algo-trader` / `scanner` / `algo-scanner` / `bot` / `trader` unit files existed in `/etc/systemd/system/`.
- `systemctl list-unit-files` and `systemctl list-units` returned no matches for any of those names.
- No cron entries, no tmux sessions, no screen sessions.
- The dashboard was listening on `0.0.0.0:8080` and `/api/health` returned HTTP 200.
- Only `ibgateway.service` was active/enabled (an IB Gateway / IBC unit, out of M15.0 scope).

**Conclusion:** the bot was being launched via `nohup` (by `deploy.sh` at first boot and by `sync.sh` on every pushed commit) and inherited its parent's session cgroup. This is why earlier VPS verifications saw `algo-trader` / `scanner` / `algo-scanner` all report `inactive` — those unit names never existed; the processes were never under systemd at all.

---

## §2 — Canonical service map (post-M15.0)

| Unit | Script | Purpose |
|---|---|---|
| `algo-trader.service` | `main.py` | Bot / scanner main loop |
| `algo-trader-dashboard.service` | `dashboard/app.py` | Flask dashboard on port 8080 |

The unit files are version-controlled at:
- `infra/systemd/algo-trader.service`
- `infra/systemd/algo-trader-dashboard.service`

**Note on `ibgateway.service`:** out of M15.0 scope. The existing IB Gateway / IBC unit continues to work as-is. M15.x (a later sub-milestone) will harden it.

**Note on `sync.sh`:** continues to run as a `nohup` background daemon launched by `deploy.sh`. It is **not** a systemd unit. After M15.0 install, `sync.sh` detects the canonical units and uses `systemctl restart` instead of the legacy `pkill + nohup` path; if the units are absent (pre-install or post-rollback), it falls back to the legacy path. The detection is automatic; the operator does not need to set a flag.

---

## §3 — Install (one-time, operator-only)

On the VPS, after the M15.0 commit has been synced into `/opt/algo-trader`:

```
sudo bash /opt/algo-trader/infra/systemd/install.sh
```

What `install.sh` does:

1. **Sanity checks.** Refuses to run as non-root. Verifies the venv exists and the canonical scripts (`main.py`, `dashboard/app.py`) are present.
2. **Snapshot.** Creates `/var/lib/algo-trader/m15_0_snapshots/<timestamp>/` containing the pre-change state: running PIDs, cgroups, systemd state, and any pre-existing unit file with the canonical name (none in the M15.0 audit; defensive for re-runs).
3. **Stop the legacy `sync.sh` daemon**, then stop the nohup-owned `main.py` and `dashboard/app.py`. Refuses to continue if any of those processes survive (operator must investigate).
4. **Install unit files** to `/etc/systemd/system/` (mode 0644).
5. **`systemctl daemon-reload`**, then `systemctl enable` + `systemctl start` each unit.
6. **Verify** both units reach `active=active` within 30s; verify `/api/health` returns HTTP 200; verify each canonical process's cgroup matches its expected unit (e.g. `main.py`'s cgroup contains `algo-trader.service`).
7. **Exit 0** on success, **exit ≥1** on any check failure, with a printed rollback command pointing at the snapshot.

The script is **idempotent** — re-running it after a successful install is safe and produces the same final state.

**Auto-restart on reboot:** the existing `@reboot` crontab entry (`bash deploy.sh`) continues to fire. After M15.0 install, `deploy.sh`'s nohup-launch block becomes a no-op because `systemctl` has already started the canonical units. **Tracked open item:** `deploy.sh` should detect M15.0 install and skip its nohup-launch block; this is a follow-up patch in M15.0 closeout if the audit window allows, otherwise M15.x.

---

## §4 — Operator commands

After install, normal operator actions:

| Action | Command |
|---|---|
| Check both services' state | `systemctl status algo-trader algo-trader-dashboard` |
| Restart only the dashboard | `sudo systemctl restart algo-trader-dashboard.service` |
| Restart only the bot | `sudo systemctl restart algo-trader.service` |
| Stop the bot (keep dashboard up) | `sudo systemctl stop algo-trader.service` |
| Tail bot log | `journalctl -u algo-trader.service -f` |
| Tail dashboard log | `journalctl -u algo-trader-dashboard.service -f` |
| Disable auto-start at boot | `sudo systemctl disable algo-trader algo-trader-dashboard` |
| Roll back to nohup-managed state | `sudo bash /opt/algo-trader/infra/systemd/rollback.sh <snapshot_dir>` |

The dashboard exposes a read-only view of the canonical map and live state at `GET /api/system/services` (auth-gated like every other dashboard route).

---

## §5 — Drain / restart independence

The two services are independent on purpose. Verified by the M15.0 acceptance criteria:

- Stopping `algo-trader-dashboard.service` takes the dashboard down (port 8080 stops listening) and brings it back on the next start. The bot keeps running through this window — scanner heartbeat does not pause.
- Stopping `algo-trader.service` stops the bot. The dashboard stays up and continues to serve `/api/health` (and the various `/api/risk-authority/*` endpoints), reporting whatever state the bot left in the DB.

This independence is the reason for two units instead of one.

---

## §6 — Rollback

If the M15.0 install causes problems, restore the pre-install state with:

```
sudo bash /opt/algo-trader/infra/systemd/rollback.sh /var/lib/algo-trader/m15_0_snapshots/<timestamp>
```

What `rollback.sh` does:

1. **Stops** both M15.0 units (`systemctl stop`).
2. **Disables** both units (`systemctl disable`).
3. **Restores** any previous unit file that was snapshotted (none expected on the first install, since the audit showed no pre-existing units).
4. **Removes** the M15.0-installed unit files from `/etc/systemd/system/`.
5. **`systemctl daemon-reload`**.
6. **Relaunches** `main.py`, `dashboard/app.py`, and `sync.sh` via `nohup`, exactly matching the pre-M15.0 shape that `deploy.sh` produces.
7. **Verifies** `/api/health` returns HTTP 200 within 20s; reports each canonical process's PID.

Trading state (`signals.db`, `risk_decisions`, `risk_snapshots`) is filesystem-resident and survives both install and rollback. No M14 audit row is at risk.

---

## §7 — What M15.0 does NOT do

Explicitly out of scope, preserved for later:

- **IB Gateway reliability hardening.** The existing `ibgateway.service` continues to run. Restart-on-stale-heartbeat, socket health monitoring, and disconnect alerts are M15.x.
- **`ingest_ibkr_exposure.py` wiring.** Still a `NotImplementedError` stub; engine returns `exposure_unknown` for IBKR scopes. M15.x.
- **Dashboard auth/security hardening.** The dashboard remains bound to `0.0.0.0:8080` with the existing session-based `@require_auth`. M15.3.
- **`manual_reset` operator flow.** Design-only. M15.3 or later.
- **`deploy.sh` cleanup.** Continues to call nohup unconditionally at first boot. After M15.0 install, the nohup-launch becomes a no-op because the systemd units are already started. A follow-up patch will add an explicit "skip nohup if systemd units are loaded" guard; tracked as an M15.0 follow-up or M15.1 cleanup.
- **No trading logic changes** anywhere. `main.py`, `bot/scanner.py`, `bot/strategy.py`, `bot/risk.py`, all `bot/risk_authority/*.py`, `tools/etoro_live_write.py` are protected.

---

*M15.0 doc end.*
