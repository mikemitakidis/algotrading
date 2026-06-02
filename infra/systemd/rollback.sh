#!/bin/bash
# M15.0 rollback — restore the pre-install (nohup + sync.sh) state.
#
# Usage: sudo bash rollback.sh <snapshot_dir>
#   where <snapshot_dir> is one of /var/lib/algo-trader/m15_0_snapshots/<ts>/
#
# The snapshot was created by install.sh. If you don't have a snapshot, run:
#   ls -la /var/lib/algo-trader/m15_0_snapshots/
# and pick the most recent timestamp.

set -euo pipefail

BASE=/opt/algo-trader
VENV=$BASE/venv
SNAP_DIR="${1:-}"
UNITS=(algo-trader.service algo-trader-dashboard.service)
DST=/etc/systemd/system

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: rollback.sh must run as root. Try: sudo bash $0 $SNAP_DIR" >&2
    exit 1
fi

if [ -z "$SNAP_DIR" ] || [ ! -d "$SNAP_DIR" ]; then
    echo "Usage: sudo bash $0 <snapshot_dir>" >&2
    echo ""
    echo "Available snapshots:" >&2
    ls -1d /var/lib/algo-trader/m15_0_snapshots/*/ 2>/dev/null | tail -5 >&2 || \
        echo "  (none — was install.sh ever run?)" >&2
    exit 1
fi

echo "[rollback] using snapshot $SNAP_DIR"

# ── 1. Stop and disable the M15.0 systemd units ─────────────────────────────
for u in "${UNITS[@]}"; do
    if systemctl is-active "$u" >/dev/null 2>&1; then
        echo "[rollback] stopping $u..."
        systemctl stop "$u"
    fi
    if systemctl is-enabled "$u" >/dev/null 2>&1; then
        echo "[rollback] disabling $u..."
        systemctl disable "$u" >/dev/null 2>&1
    fi
done

# ── 2. Remove M15.0 unit files. Restore the previous file if one was snapshotted. ──
for u in "${UNITS[@]}"; do
    if [ -f "$SNAP_DIR/$u.previous" ]; then
        echo "[rollback] restoring previous $DST/$u from snapshot"
        install -m 0644 "$SNAP_DIR/$u.previous" "$DST/$u"
    elif [ -f "$DST/$u" ]; then
        echo "[rollback] removing M15.0-installed $DST/$u (no previous version to restore)"
        rm -f "$DST/$u"
    fi
done

systemctl daemon-reload

# ── 3. Restart legacy nohup-owned processes via deploy.sh's path ─────────────
# We DO NOT call deploy.sh directly because it does too much (creates .env,
# installs deps, sets cron). The audit confirmed the only thing that owns
# main.py + dashboard is nohup; reproduce that minimally.
mkdir -p $BASE/logs

# Kill anything that the M15.0 install may have left running (defensive).
pkill -f "python.*main\.py"          2>/dev/null || true
pkill -f "python.*dashboard/app\.py" 2>/dev/null || true
sleep 1

# Relaunch nohup-style, matching the pre-M15.0 deploy.sh shape.
nohup $VENV/bin/python3 $BASE/main.py          > /dev/null 2>&1 &
echo "[rollback] main.py relaunched nohup PID=$!"
nohup $VENV/bin/python3 $BASE/dashboard/app.py >> $BASE/logs/dashboard.log 2>&1 &
echo "[rollback] dashboard relaunched nohup PID=$!"

# Restart the legacy sync.sh daemon so auto-update keeps working.
pkill -f "bash.*sync\.sh" 2>/dev/null || true
sleep 1
nohup bash $BASE/sync.sh >> $BASE/logs/sync.log 2>&1 &
echo "[rollback] sync.sh daemon relaunched nohup PID=$!"

# ── 4. Verify ─────────────────────────────────────────────────────────────────
echo ""
echo "[rollback] === Post-rollback verification ==="
sleep 3
for i in $(seq 1 20); do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/api/health 2>&1)
    if [ "$code" = "200" ]; then
        break
    fi
    sleep 1
done
echo "  /api/health: HTTP $code"

if pgrep -f "python.*main\.py" >/dev/null 2>&1; then
    echo "  main.py: running (PID(s) $(pgrep -f 'python.*main\.py' | tr '\n' ' '))"
else
    echo "  main.py: NOT RUNNING — investigate logs"
fi
if pgrep -f "python.*dashboard/app\.py" >/dev/null 2>&1; then
    echo "  dashboard: running (PID(s) $(pgrep -f 'python.*dashboard/app\.py' | tr '\n' ' '))"
else
    echo "  dashboard: NOT RUNNING — investigate logs"
fi

for u in "${UNITS[@]}"; do
    state=$(systemctl is-active "$u" 2>/dev/null || echo not-found)
    echo "  $u: $state (should be inactive or not-found)"
done

echo ""
echo "[rollback] ✅ rollback complete. Bot is back on nohup-managed processes."
echo "[rollback] If main.py / dashboard / sync.sh are NOT running, run:"
echo "             sudo bash $BASE/deploy.sh"
