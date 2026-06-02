#!/bin/bash
# M15.0 — explicit operator install script for systemd units.
#
# This script is NEVER auto-run by sync.sh or by the bot itself. The
# operator runs it once, by hand, with root privileges, to move the
# bot+dashboard from session-owned nohup processes to systemd-managed
# services.
#
# Safe to re-run (idempotent).
#
# Snapshots the pre-change state to /var/lib/algo-trader/m15_0_snapshots/
# so the rollback script can restore the previous setup.

set -euo pipefail

BASE=/opt/algo-trader
SRC="$BASE/infra/systemd"
DST=/etc/systemd/system
SNAP_DIR=/var/lib/algo-trader/m15_0_snapshots/$(date -u +%Y%m%dT%H%M%SZ)
UNITS=(algo-trader.service algo-trader-dashboard.service)

require_root() {
    if [ "$EUID" -ne 0 ]; then
        echo "ERROR: install.sh must run as root. Try: sudo bash $0" >&2
        exit 1
    fi
}

# ── 0. Sanity checks ──────────────────────────────────────────────────────────
require_root

for u in "${UNITS[@]}"; do
    [ -f "$SRC/$u" ] || { echo "ERROR: $SRC/$u missing — is the repo synced?" >&2; exit 1; }
done

if [ ! -x "$BASE/venv/bin/python3" ]; then
    echo "ERROR: $BASE/venv/bin/python3 not executable — run deploy.sh first to set up venv." >&2
    exit 1
fi

for f in "$BASE/main.py" "$BASE/dashboard/app.py"; do
    [ -f "$f" ] || { echo "ERROR: $f missing — repo not fully synced?" >&2; exit 1; }
done

# ── 1. Snapshot the pre-change state ─────────────────────────────────────────
mkdir -p "$SNAP_DIR"
echo "[install] snapshot dir: $SNAP_DIR"

# Snapshot existing unit files (if any), even though audit showed none —
# defense in depth in case the operator runs this script in a different
# environment.
for u in "${UNITS[@]}"; do
    if [ -f "$DST/$u" ]; then
        cp "$DST/$u" "$SNAP_DIR/$u.previous"
        echo "[install] snapshotted existing $DST/$u"
    fi
done

# Snapshot the currently running PIDs and their cgroups.
{
    echo "=== M15.0 pre-install snapshot ==="
    echo "timestamp_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "hostname:      $(hostname)"
    echo ""
    echo "── running python processes for main.py / dashboard ──"
    ps -eo pid,ppid,user,etime,cmd | grep -E "python.*(main\.py|dashboard/app\.py)" | grep -v grep || true
    echo ""
    echo "── cgroup trace ──"
    for pid in $(pgrep -f "python.*main\.py" 2>/dev/null) $(pgrep -f "python.*dashboard/app\.py" 2>/dev/null); do
        [ -d "/proc/$pid" ] || continue
        echo "PID=$pid  cgroup=$(cat /proc/$pid/cgroup 2>/dev/null | head -1)"
    done
    echo ""
    echo "── systemd state (algo-trader family) ──"
    for u in algo-trader algo-trader-dashboard scanner algo-scanner; do
        printf "  %-30s active=%s enabled=%s\n" "$u" \
            "$(systemctl is-active "$u" 2>/dev/null || echo not-found)" \
            "$(systemctl is-enabled "$u" 2>/dev/null || echo n/a)"
    done
    echo ""
    echo "── /api/health probe ──"
    curl -s -o /dev/null -w "  http://127.0.0.1:8080/api/health -> HTTP %{http_code}\n" \
        http://127.0.0.1:8080/api/health 2>&1 || echo "  curl failed"
    echo ""
    echo "── git state at /opt/algo-trader ──"
    cd "$BASE" && {
        echo "  HEAD:   $(git rev-parse HEAD 2>/dev/null)"
        echo "  branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
    }
} > "$SNAP_DIR/pre_install_state.txt" 2>&1
echo "[install] pre-install state recorded at $SNAP_DIR/pre_install_state.txt"

# ── 2. Stop the legacy sync.sh daemon (it competes with systemd-managed
#      restarts; the new sync.sh will detect systemd and use systemctl
#      restart instead, but we need a clean transition window).
#      Note: this is done BEFORE we install the units so we don't have a
#      race where sync.sh kills the systemd-spawned process.
echo "[install] stopping legacy sync.sh daemon (if present)..."
pkill -f "bash.*sync\.sh" 2>/dev/null || true
sleep 1
if pgrep -f "bash.*sync\.sh" >/dev/null 2>&1; then
    echo "ERROR: legacy sync.sh still running after pkill. Refusing to continue." >&2
    exit 2
fi
echo "[install] legacy sync.sh stopped."

# ── 3. Stop the legacy nohup-owned bot and dashboard ──────────────────────────
#      We stop them now so that when systemctl start brings up the units, the
#      ports/PIDs are free.
echo "[install] stopping legacy nohup processes..."
pkill -f "python.*main\.py"          2>/dev/null || true
pkill -f "python.*dashboard/app\.py" 2>/dev/null || true
sleep 2
# Validate: no leftover processes.
if pgrep -f "python.*main\.py" >/dev/null 2>&1; then
    echo "ERROR: legacy main.py still running. Stop manually and re-run install.sh." >&2
    exit 3
fi
if pgrep -f "python.*dashboard/app\.py" >/dev/null 2>&1; then
    echo "ERROR: legacy dashboard still running. Stop manually and re-run install.sh." >&2
    exit 3
fi
echo "[install] legacy processes stopped."

# ── 4. Install the unit files ─────────────────────────────────────────────────
echo "[install] copying unit files to $DST..."
for u in "${UNITS[@]}"; do
    install -m 0644 "$SRC/$u" "$DST/$u"
    echo "[install]   $DST/$u"
done

# ── 5. Reload daemon, enable + start both units ───────────────────────────────
echo "[install] systemctl daemon-reload..."
systemctl daemon-reload

for u in "${UNITS[@]}"; do
    echo "[install] systemctl enable $u..."
    systemctl enable "$u" >/dev/null 2>&1
    echo "[install] systemctl start $u..."
    systemctl start "$u"
done

# ── 6. Wait for services to come up, then verify ──────────────────────────────
echo "[install] waiting up to 30s for services to become active..."
for i in $(seq 1 30); do
    bot_state=$(systemctl is-active algo-trader.service          2>/dev/null || true)
    dash_state=$(systemctl is-active algo-trader-dashboard.service 2>/dev/null || true)
    if [ "$bot_state" = "active" ] && [ "$dash_state" = "active" ]; then
        break
    fi
    sleep 1
done

# Final state report
echo ""
echo "[install] === Post-install state ==="
for u in "${UNITS[@]}"; do
    state=$(systemctl is-active "$u" 2>/dev/null || echo unknown)
    enabled=$(systemctl is-enabled "$u" 2>/dev/null || echo unknown)
    echo "  $u: active=$state enabled=$enabled"
done

# Health probe
for i in $(seq 1 20); do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/api/health 2>&1)
    if [ "$code" = "200" ]; then
        break
    fi
    sleep 1
done
echo "  /api/health: HTTP $code"

# cgroup verification — bot and dashboard must be under their respective units.
echo ""
echo "[install] cgroup verification:"
ok=1
for pid in $(pgrep -f "python.*main\.py"); do
    cg=$(cat "/proc/$pid/cgroup" 2>/dev/null | head -1)
    expected="algo-trader.service"
    if [[ "$cg" == *"$expected"* ]]; then
        echo "  main.py PID=$pid  cgroup OK ($cg)"
    else
        echo "  main.py PID=$pid  cgroup MISMATCH expected=$expected got=$cg"
        ok=0
    fi
done
for pid in $(pgrep -f "python.*dashboard/app\.py"); do
    cg=$(cat "/proc/$pid/cgroup" 2>/dev/null | head -1)
    expected="algo-trader-dashboard.service"
    if [[ "$cg" == *"$expected"* ]]; then
        echo "  dashboard PID=$pid  cgroup OK ($cg)"
    else
        echo "  dashboard PID=$pid  cgroup MISMATCH expected=$expected got=$cg"
        ok=0
    fi
done

echo ""
if [ "$ok" = "1" ] && [ "$bot_state" = "active" ] && [ "$dash_state" = "active" ] && [ "$code" = "200" ]; then
    echo "[install] ✅ M15.0 install complete. Both services are active and healthy."
    echo "[install] Snapshot at: $SNAP_DIR"
    echo "[install] To roll back: sudo bash $BASE/infra/systemd/rollback.sh $SNAP_DIR"
    exit 0
else
    echo "[install] ❌ M15.0 install incomplete. Inspect logs:"
    echo "  journalctl -u algo-trader.service --no-pager -n 50"
    echo "  journalctl -u algo-trader-dashboard.service --no-pager -n 50"
    echo "[install] To roll back: sudo bash $BASE/infra/systemd/rollback.sh $SNAP_DIR"
    exit 4
fi
