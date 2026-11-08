#!/bin/bash
# M21 PR-d (BLK-5) - install the paper-loop and monitoring units DISABLED.
#
#   sudo bash infra/systemd/install_paper_units.sh            # install, disabled
#   sudo bash infra/systemd/install_paper_units.sh --uninstall # stop, disable, remove
#
# NEVER run automatically (not by deploy.sh, sync.sh, the bot or a test).
# Installing never enables or starts anything and refuses while either
# allow-file exists. Activation is a separate, explicitly approved operator
# step (operations plan P4): create the allow-file(s), then enable the timer(s).
#
# Exit: 0 done; 1 refused (nothing changed); 3 post-install verification failed.

set -euo pipefail

BASE=/opt/algo-trader
SRC="$BASE/infra/systemd"
DST=/etc/systemd/system
STATE=/var/lib/algo-trader
SNAP="$STATE/paper_units_snapshots/$(date -u +%Y%m%dT%H%M%SZ)"
UNITS=(algo-paper-loop.service algo-paper-loop.timer algo-paper-monitor.service algo-paper-monitor.timer)
TIMERS=(algo-paper-loop.timer algo-paper-monitor.timer)
ALLOW=("$STATE/ALLOW_PAPER_LOOP" "$STATE/ALLOW_PAPER_MONITOR")

refuse() { echo "REFUSED: $*" >&2; exit 1; }

[ "$EUID" -eq 0 ] || refuse "must run as root"

if [ "${1:-}" = "--uninstall" ]; then
    # Rollback: stop and disable the timers, stop any running tick, remove the
    # allow-files and the unit files. Leaves the journal, alarms and logs.
    for t in "${TIMERS[@]}"; do systemctl disable --now "$t" 2>/dev/null || true; done
    systemctl stop algo-paper-loop.service algo-paper-monitor.service 2>/dev/null || true
    rm -f "${ALLOW[@]}"
    for u in "${UNITS[@]}"; do rm -f "$DST/$u"; done
    systemctl daemon-reload
    echo "uninstalled: timers disabled and stopped, allow-files and unit files removed"
    exit 0
fi
[ -z "${1:-}" ] || refuse "unknown argument: $1"

# 0. Preconditions: nothing may be armed or running.
for a in "${ALLOW[@]}"; do
    [ ! -e "$a" ] || refuse "allow-file present ($a); installing must leave the units unable to run"
done
for u in "${UNITS[@]}"; do
    [ -f "$SRC/$u" ] || refuse "$SRC/$u missing - is the reviewed commit checked out?"
    if [ "$(systemctl is-active "$u" 2>/dev/null || true)" = active ]; then
        refuse "$u is active"
    fi
    if [ "$(systemctl is-enabled "$u" 2>/dev/null || true)" = enabled ]; then
        refuse "$u is enabled"
    fi
done
[ -x "$BASE/venv/bin/python3" ] || refuse "$BASE/venv/bin/python3 not executable"

# 1. Snapshot any previous copies, then install (files only).
mkdir -p "$SNAP"
for u in "${UNITS[@]}"; do
    [ ! -f "$DST/$u" ] || cp -p "$DST/$u" "$SNAP/$u.previous"
    install -m 0644 -o root -g root "$SRC/$u" "$DST/$u"
done
systemctl daemon-reload
echo "installed (disabled); snapshot: $SNAP"

# 2. Verify: every unit loaded, none enabled, none active, guards in place.
fail=0
for u in "${UNITS[@]}"; do
    load=$(systemctl show -p LoadState --value "$u")
    en=$(systemctl is-enabled "$u" 2>/dev/null || true)
    act=$(systemctl is-active "$u" 2>/dev/null || true)
    printf "  %-28s load=%s enabled=%s active=%s\n" "$u" "$load" "$en" "$act"
    [ "$load" = loaded ] || fail=1
    [ "$en" != enabled ] || fail=1
    [ "$act" != active ] || fail=1
done
grep -qx 'ConditionPathExists=/var/lib/algo-trader/ALLOW_PAPER_LOOP' "$DST/algo-paper-loop.service" || fail=1
grep -qx 'ConditionPathExists=/var/lib/algo-trader/ALLOW_PAPER_MONITOR' "$DST/algo-paper-monitor.service" || fail=1
for a in "${ALLOW[@]}"; do [ ! -e "$a" ] || fail=1; done
if [ "$fail" -ne 0 ]; then
    echo "VERIFY FAILED: run: sudo bash $0 --uninstall" >&2
    exit 3
fi
echo "verified: installed, disabled, inactive; allow-files absent"
