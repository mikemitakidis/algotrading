#!/usr/bin/env bash
# Gate A-2 containment check (VPS-C): READ-ONLY. Changes nothing. Derived from the reviewed Gate A-1 VPS-0 (revision 5,
# sha256 bb5359b8...8478d); the ONLY changes are that the expected VPS commit is an argument (one of the two commits
# Gate A-2 can leave the checkout at) and the labels. Exit 0 = containment in place; 3 = STOP; 2 = usage.
#   bash a2_containment.sh 015d2cea927b9b4db3ee65604e0b4ebb58211e61   # before deployment, or after a code rollback
#   bash a2_containment.sh 963bb893c81c2482a08d78a11b1aafe98f542b51   # after deployment
set -u
EXPECT_HEAD=${1:-}
case "$EXPECT_HEAD" in
  015d2cea927b9b4db3ee65604e0b4ebb58211e61|963bb893c81c2482a08d78a11b1aafe98f542b51) ;;
  *) echo "usage: a2_containment.sh <015d2ce...|963bb89...> (the full 40-character commit)"; exit 2;;
esac
SCAN='import os, sys
try:
    me = os.getpid(); skip = set(); p = me
    while p > 1:   # this scan and the shells running this block are never the bot
        skip.add(p)
        with open("/proc/%d/status" % p) as f:
            p = int([l.split()[1] for l in f if l.startswith("PPid:")][0])
    pids = [d for d in os.listdir("/proc") if d.isdigit()]
    seen, hits = 0, []
    for d in pids:
        try:
            with open("/proc/%s/cmdline" % d, "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except (FileNotFoundError, ProcessLookupError):
            continue   # exited during the scan
        seen += 1
        if int(d) in skip:
            continue
        names = {os.path.basename(w.strip("\"`;&|()" + chr(39))) for a in argv for w in a.split()}
        if names & {"main.py", "sync.sh"}:
            hits.append("bot process: %s %s" % (d, " ".join(argv).strip()[:200]))
    if str(me) not in pids or seen < 2:
        raise RuntimeError("process list unreadable")
except Exception as e:
    print("process scan FAILED: %r" % (e,)); sys.exit(2)
print("\n".join(hits) if hits else "processes: no main.py or sync.sh (%d checked)" % seen)
sys.exit(3 if hits else 0)'
echo "== VPS-C read-only containment check at $(date -u +%Y-%m-%dT%H:%M:%SZ), expected VPS commit ${EXPECT_HEAD:0:12}"
P=$(python3 -I -c "$SCAN" 2>&1); rc=$?; echo "$P"
KS=$(python3 -I -c 'import json; print(json.load(open("/opt/algo-trader/data/kill_switch.json")).get("active"))' 2>&1)
EN=$(systemctl is-enabled algo-trader.service 2>&1); AC=$(systemctl is-active algo-trader.service 2>&1)
if systemd-analyze condition 'ConditionPathExists=/var/lib/algo-trader/ALLOW_BOT_START' >/dev/null 2>&1; then G=PASSES; else G=FALSE; fi
if [ -e /var/lib/algo-trader/ALLOW_BOT_START ]; then AL=PRESENT; else AL=absent; fi
HD=$(git -C /opt/algo-trader rev-parse HEAD 2>&1)
# The Gate A-0 guard must be part of the bot unit systemd has LOADED, not only true as a standalone condition:
DIP=$(systemctl show -p DropInPaths --value algo-trader.service 2>&1)
case " $DIP " in *" /etc/systemd/system/algo-trader.service.d/gate-a0.conf "*) DL=loaded;; *) DL=NOT_LOADED;; esac
GL=$(systemctl cat algo-trader.service 2>/dev/null | grep -cxF 'ConditionPathExists=/var/lib/algo-trader/ALLOW_BOT_START')
NDR=$(systemctl show -p NeedDaemonReload --value algo-trader.service 2>&1)
if [ -e /etc/systemd/system/multi-user.target.wants/algo-trader.service ] || [ -L /etc/systemd/system/multi-user.target.wants/algo-trader.service ]; then BL=PRESENT; else BL=absent; fi
# The guard must also be EFFECTIVE: a later empty "ConditionPathExists=" (or any empty Condition*=) resets every earlier
# condition. Read the condition list systemd actually holds: D-Bus property org.freedesktop.systemd1.Unit.Conditions,
# a(sbbsi) = (type, trigger, negate, parameter, state). `systemctl show -p Conditions` cannot print this array type
# (systemd 255: it is "[unprintable]"), so it is read with busctl in JSON. Anything unexpected -> UNREADABLE -> STOP.
CJ=$(busctl --json=short get-property org.freedesktop.systemd1 /org/freedesktop/systemd1/unit/algo_2dtrader_2eservice org.freedesktop.systemd1.Unit Conditions 2>&1)
CE=$(python3 -I -c 'import json, sys
try:
    j = json.loads(sys.argv[1])
    assert isinstance(j, dict) and j.get("type") == "a(sbbsi)" and isinstance(j.get("data"), list)
    found = False
    for c in j["data"]:
        assert isinstance(c, list) and len(c) == 5
        t, trig, neg, par, st = c
        assert isinstance(t, str) and type(trig) is bool and type(neg) is bool and isinstance(par, str) and type(st) is int
        if t == "ConditionPathExists" and trig is False and neg is False and par == "/var/lib/algo-trader/ALLOW_BOT_START":
            found = True
    print("effective" if found else "NOT_EFFECTIVE")
except Exception:
    print("UNREADABLE")' "$CJ" 2>/dev/null)
echo "kill_switch_active=$KS bot_unit=$EN/$AC guard_condition=$G allow_file=$AL vps_head=${HD:0:12}"
echo "guard_drop_in=$DL guard_line_in_loaded_unit=$GL need_daemon_reload=$NDR boot_link=$BL guard_condition_effective=${CE:-UNREADABLE}"
if [ "$rc" = 0 ] && [ "$KS" = True ] && [ "$EN" = disabled ] && [ "$AC" = inactive ] && [ "$G" = FALSE ] && [ "$AL" = absent ] && [ "$HD" = "$EXPECT_HEAD" ] \
   && [ "$DL" = loaded ] && [ "$GL" -ge 1 ] 2>/dev/null && [ "$NDR" = no ] && [ "$BL" = absent ] && [ "$CE" = effective ]; then
  echo "VPS-C OK: no main.py or sync.sh process, Gate A-0 containment in place (guard loaded and effective in the bot unit), VPS code is ${EXPECT_HEAD:0:7} as expected."
  exit 0
else
  echo "VPS-C STOP: containment is not as expected. Do nothing further; send this output."
  exit 3
fi
