#!/bin/bash
# M21 PR-d (BLK-5) - install the paper-loop and monitoring units DISABLED.
#
#   sudo bash infra/systemd/install_paper_units.sh --approval-ref <ref>
#   sudo bash infra/systemd/install_paper_units.sh --uninstall [--approval-ref <ref>]
#
# NEVER run automatically (not by deploy.sh, sync.sh, the bot or a test). Installing is
# itself a separately approved operator step: --approval-ref names that approval (e.g. a
# GitHub comment URL or id) and is recorded with the snapshot. Installing never enables
# or starts anything and refuses while either allow-file exists. Activation is a further,
# separately approved step (operations plan P4).
#
# After copying, the script verifies what systemd has actually LOADED, not only the
# files: each service is loaded from the expected file with no drop-in, needs no daemon
# reload, is static and inactive, and its allow-file guard is in the EFFECTIVE condition
# list (D-Bus org.freedesktop.systemd1.Unit.Conditions, as Gate A-0 VPS-C does: a later
# empty "ConditionPathExists=" in a drop-in would reset it) and currently evaluates
# false; each timer is disabled, inactive and points at its service; every installed file
# passes systemd-analyze verify. Any failure, error or interruption after the first file
# is written restores the previous unit files (or removes the new ones) and reloads.
#
# Exit: 0 done; 1 refused (nothing changed); 3 verification failed (rolled back);
#       4 error or interruption during install (rolled back); 5 rollback failed (STOP).

set -euo pipefail

BASE=/opt/algo-trader
SRC="$BASE/infra/systemd"
DST=/etc/systemd/system
STATE=/var/lib/algo-trader
GUARD_DIR=/var/lib/algo-trader            # as written in the unit files
SNAP="$STATE/paper_units_snapshots/$(date -u +%Y%m%dT%H%M%SZ)"
SERVICES=(algo-paper-loop.service algo-paper-monitor.service)
TIMERS=(algo-paper-loop.timer algo-paper-monitor.timer)
UNITS=("${SERVICES[@]}" "${TIMERS[@]}")
ALLOW=("$STATE/ALLOW_PAPER_LOOP" "$STATE/ALLOW_PAPER_MONITOR")

guard_of() {
    case "$1" in
        algo-paper-loop.service) echo "$GUARD_DIR/ALLOW_PAPER_LOOP" ;;
        algo-paper-monitor.service) echo "$GUARD_DIR/ALLOW_PAPER_MONITOR" ;;
    esac
}
service_of() { echo "${1%.timer}.service"; }
dbus_path() {
    python3 -I -c 'import sys
print("/org/freedesktop/systemd1/unit/" + "".join(
    c if c.isalnum() else "_%02x" % ord(c) for c in sys.argv[1]))' "$1"
}

ARMED=0          # 1 once the first unit file has been written
DONE=0           # 1 once verification has passed
refuse() { echo "REFUSED: $*" >&2; exit 1; }
rollback() {
    local ok=0 u
    for u in "${UNITS[@]}"; do
        if [ -f "$SNAP/$u.previous" ]; then
            cp -p "$SNAP/$u.previous" "$DST/$u" || ok=1
        else
            rm -f "$DST/$u" || ok=1
        fi
    done
    systemctl daemon-reload || ok=1
    return "$ok"
}
# Every way out of the script after the first write (an error under set -e, a signal,
# a failed verification) passes here and is rolled back unless verification passed.
on_exit() {
    local rc=$?
    trap - EXIT INT TERM HUP
    if [ "$ARMED" = 1 ] && [ "$DONE" = 0 ]; then
        if rollback; then
            [ "$rc" = 3 ] || rc=4
            echo "ROLLED BACK (exit $rc): previous unit files restored, daemon reloaded" >&2
        else
            rc=5
            echo "ROLLBACK FAILED: STOP. Inspect $DST and $SNAP before anything else." >&2
        fi
    fi
    exit "$rc"
}
trap on_exit EXIT
trap 'exit 4' INT TERM HUP

[ "$EUID" -eq 0 ] || refuse "must run as root"

MODE=install
REF=""
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) MODE=uninstall; shift ;;
        --approval-ref) [ $# -ge 2 ] || refuse "--approval-ref needs a value"; REF="$2"; shift 2 ;;
        *) refuse "unknown argument: $1" ;;
    esac
done
if [ -n "$REF" ] && ! [[ "$REF" =~ ^[A-Za-z0-9#:/._?=-]{6,200}$ ]]; then
    refuse "approval reference has unexpected characters or length"
fi

if [ "$MODE" = uninstall ]; then
    # Rollback of an earlier install: stop and disable the timers, stop any running
    # tick, remove the allow-files and the unit files. Leaves journal, alarms, logs.
    for t in "${TIMERS[@]}"; do systemctl disable --now "$t" 2>/dev/null || true; done
    systemctl stop "${SERVICES[@]}" 2>/dev/null || true
    rm -f "${ALLOW[@]}"
    for u in "${UNITS[@]}"; do rm -f "$DST/$u"; done
    systemctl daemon-reload
    echo "uninstalled (ref: ${REF:-none}): timers disabled and stopped, allow-files and unit files removed"
    exit 0
fi

[ -n "$REF" ] || refuse "installing needs --approval-ref <the operator's approval reference>"

# 0. Preconditions: nothing may be armed or running; the tools must exist.
for a in "${ALLOW[@]}"; do
    [ ! -e "$a" ] || refuse "allow-file present ($a); installing must leave the units unable to run"
done
for u in "${UNITS[@]}"; do
    [ -f "$SRC/$u" ] || refuse "$SRC/$u missing - is the reviewed commit checked out?"
    [ "$(systemctl is-active "$u" 2>/dev/null || true)" != active ] || refuse "$u is active"
    [ "$(systemctl is-enabled "$u" 2>/dev/null || true)" != enabled ] || refuse "$u is enabled"
done
[ -x "$BASE/venv/bin/python3" ] || refuse "$BASE/venv/bin/python3 not executable"
for tool in busctl systemd-analyze python3; do
    command -v "$tool" >/dev/null 2>&1 || refuse "$tool not available; effective guards cannot be verified"
done

# 1. Snapshot previous copies and the approval, then install (files only).
mkdir -p "$SNAP"
for u in "${UNITS[@]}"; do
    if [ -f "$DST/$u" ]; then cp -p "$DST/$u" "$SNAP/$u.previous"; fi
done
{
    echo "approval_ref=$REF"
    echo "installed_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "repo_head=$(git -C "$BASE" rev-parse HEAD 2>/dev/null || echo unknown)"
} > "$SNAP/approval.txt"
ARMED=1
for u in "${UNITS[@]}"; do
    install -m 0644 -o root -g root "$SRC/$u" "$DST/$u"
done
systemctl daemon-reload
echo "installed (disabled); snapshot: $SNAP"

# 2. Verify what systemd loaded. Every check prints; any failure rolls back (exit 3).
fail=0
show() { systemctl show -p "$1" --value "$2" 2>/dev/null || echo "?"; }
for u in "${SERVICES[@]}"; do
    g=$(guard_of "$u")
    load=$(show LoadState "$u"); frag=$(show FragmentPath "$u"); dip=$(show DropInPaths "$u")
    ndr=$(show NeedDaemonReload "$u"); ufs=$(show UnitFileState "$u"); act=$(show ActiveState "$u")
    cj=$(busctl --json=short get-property org.freedesktop.systemd1 "$(dbus_path "$u")" \
         org.freedesktop.systemd1.Unit Conditions 2>&1 || true)
    eff=$(python3 -I -c 'import json, sys
try:
    j = json.loads(sys.argv[1])
    assert isinstance(j, dict) and j.get("type") == "a(sbbsi)" and isinstance(j.get("data"), list)
    found = False
    for c in j["data"]:
        assert isinstance(c, list) and len(c) == 5
        t, trig, neg, par, st = c
        assert isinstance(t, str) and type(trig) is bool and type(neg) is bool and isinstance(par, str)
        if t == "ConditionPathExists" and trig is False and neg is False and par == sys.argv[2]:
            found = True
    print("effective" if found else "NOT_EFFECTIVE")
except Exception:
    print("UNREADABLE")' "$cj" "$g" 2>/dev/null || echo UNREADABLE)
    if systemd-analyze condition "ConditionPathExists=$g" >/dev/null 2>&1; then now=PASSES; else now=false; fi
    if systemd-analyze verify "$DST/$u" >/dev/null 2>&1; then ver=ok; else ver=FAILED; fi
    printf "  %-28s load=%s fragment=%s drop_ins=%s need_reload=%s file_state=%s active=%s guard=%s guard_now=%s verify=%s\n" \
        "$u" "$load" "$frag" "${dip:-none}" "$ndr" "$ufs" "$act" "$eff" "$now" "$ver"
    [ "$load" = loaded ] || fail=1
    [ "$frag" = "$DST/$u" ] || fail=1
    [ -z "$dip" ] || fail=1
    [ "$ndr" = no ] || fail=1
    [ "$ufs" = static ] || fail=1
    [ "$act" = inactive ] || fail=1
    [ "$eff" = effective ] || fail=1
    [ "$now" = false ] || fail=1
    [ "$ver" = ok ] || fail=1
done
for t in "${TIMERS[@]}"; do
    load=$(show LoadState "$t"); frag=$(show FragmentPath "$t"); dip=$(show DropInPaths "$t")
    ndr=$(show NeedDaemonReload "$t"); en=$(systemctl is-enabled "$t" 2>/dev/null || true)
    act=$(show ActiveState "$t"); tgt=$(show Unit "$t")
    if systemd-analyze verify "$DST/$t" >/dev/null 2>&1; then ver=ok; else ver=FAILED; fi
    printf "  %-28s load=%s fragment=%s drop_ins=%s need_reload=%s enabled=%s active=%s unit=%s verify=%s\n" \
        "$t" "$load" "$frag" "${dip:-none}" "$ndr" "$en" "$act" "$tgt" "$ver"
    [ "$load" = loaded ] || fail=1
    [ "$frag" = "$DST/$t" ] || fail=1
    [ -z "$dip" ] || fail=1
    [ "$ndr" = no ] || fail=1
    [ "$en" = disabled ] || fail=1
    [ "$act" = inactive ] || fail=1
    [ "$tgt" = "$(service_of "$t")" ] || fail=1
    [ "$ver" = ok ] || fail=1
done
for a in "${ALLOW[@]}"; do [ ! -e "$a" ] || fail=1; done
if [ "$fail" -ne 0 ]; then
    echo "VERIFY FAILED" >&2
    exit 3
fi
DONE=1
echo "verified: installed, disabled, inactive; guards effective and false; allow-files absent (ref: $REF)"
