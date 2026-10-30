#!/bin/bash
# OP1.4b — deployment discovery: would a merge to main reach the VPS, how, and what would a restart do?
# Prepared 30 Oct 2026; NOT run on the VPS. Read-only.
#
# Usage: bash op1_deploy_discovery.sh [OUT_DIR]      (default /tmp/m21_op1; env OP1_ROOT, default /opt/algo-trader)
#
# Reads only: process tables, /proc/<pid>/{cgroup,cmdline}, systemd unit PROPERTIES (never unit files or
# environments), cron and timer listings, the deployed checkout via read-only git queries (no fetch; status
# with GIT_OPTIONAL_LOCKS=0 so the index is never rewritten), the deployed sync.sh/deploy.sh text, the kill
# switch state file, and journal lines that pass a keyword filter and a redaction filter.
# Never: starts, stops, restarts, kills, enables or edits anything; reads .env values; fetches from GitHub.
set -u
OUT="${1:-/tmp/m21_op1}"
ROOT="${OP1_ROOT:-/opt/algo-trader}"
mkdir -p "$OUT" || exit 2
R="$OUT/deploy_discovery.txt"
redact() {
  sed -E \
    -e 's/[0-9]{8,10}:[A-Za-z0-9_-]{30,}/<telegram-token>/g' \
    -e 's#://[^/@ ]+@#://<redacted>@#g' \
    -e 's/(gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}/<github-token>/g' \
    -e 's/((api|secret|access)?_?(key|token|secret|password|passwd|pwd))([=: ]+)[^ ,;&"]+/\1\4<redacted>/Ig' \
    -e 's/\b(DU|DF|U)[0-9]{5,}\b/\1***/g'
}
# Read-only git with no lock files; safe.directory for this one invocation only (the checkout may be owned by
# another account; nothing is written to any git config).
G() { GIT_OPTIONAL_LOCKS=0 git -c safe.directory="$ROOT" -C "$ROOT" "$@"; }
units() { systemctl list-units --type=service --all --no-legend --plain 2>/dev/null | awk '{print $1}'; }

{
echo "== generated $(date -u +%FT%TZ) on $(hostname -s) as $(id -un)"

echo; echo "== Q1/Q2 sync.sh: running? who manages it? (pid, parent, user, start, cgroup = systemd unit or login session)"
# Only a shell RUNNING a script named sync.sh counts (argv[0] a shell and argv[1] ending in sync.sh, or the
# script executed directly). Other arguments are never printed: a command line can hold anything.
PIDS=$(python3 -I -c '
import os
for p in os.listdir("/proc"):
    if not p.isdigit() or int(p) == os.getpid():
        continue
    try:
        a = open("/proc/%s/cmdline" % p, "rb").read().split(b"\0")
    except OSError:
        continue
    a = [x.decode("utf-8", "replace") for x in a if x]
    if not a:
        continue
    sh = os.path.basename(a[0]) in ("bash", "sh", "dash")
    if (sh and len(a) > 1 and a[1].endswith("sync.sh")) or a[0].endswith("sync.sh"):
        print(p, a[1] if sh else a[0])
' 2>/dev/null)
# A non-root account may not see other users' processes (/proc mounted with hidepid): then "no" would be a guess.
PROCOPT=$( (findmnt -no OPTIONS /proc 2>/dev/null || awk '$2 == "/proc" {print $4}' /proc/mounts 2>/dev/null) \
          | paste -sd' ')
echo "proc_mount_options=$PROCOPT session_uid=$(id -u)"
if [ -z "$PIDS" ] && [ "$(id -u)" != "0" ] && echo "$PROCOPT" | grep -qE 'hidepid=(1|2|invisible|noaccess)'; then
  echo "sync_sh_running=UNKNOWN (this account cannot see other users' processes; re-check as the service account)"
elif [ -z "$PIDS" ]; then echo "sync_sh_running=no"; else
  echo "sync_sh_running=yes count=$(echo "$PIDS" | wc -l)"
  echo "$PIDS" | while read -r p script; do
    ps -o pid=,ppid=,user=,lstart=,etimes=,comm= -p "$p" 2>/dev/null | sed 's/^/process: /'
    pp=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
    [ -n "$pp" ] && ps -o pid=,comm= -p "$pp" 2>/dev/null | sed 's/^/parent:  /'
    echo "script:  $script"
    echo "cgroup:  $(cut -d: -f3 /proc/$p/cgroup 2>/dev/null | tr '\n' ' ')"
    echo "cwd:     $(readlink /proc/$p/cwd 2>/dev/null)"
  done
fi
echo "-- deployed sync.sh: branch it follows, and whether it matches main 80b4343's copy (sha256 of the file)"
grep -nE '^(BRANCH|BASE|VENV)=' "$ROOT/sync.sh" 2>/dev/null
sha256sum "$ROOT/sync.sh" "$ROOT/deploy.sh" 2>/dev/null
echo "-- sync.log: start/deploy/restart events (last 40, redacted)"
grep -E 'Sync daemon started|New commit detected|Updated to|pip done|import check|Restarted|restart via|ERROR' \
  "$ROOT/logs/sync.log" 2>/dev/null | redact | tail -n 40 || echo "(no logs/sync.log)"

echo; echo "== Q3 other automated deployment or restart mechanisms"
echo "-- systemd units/timers whose NAME mentions sync/deploy/git/update/algo/trader/ibkr/gateway/paper"
units | grep -Ei 'sync|deploy|git|update|algo|trader|ibkr|gateway|ibc|paper|runner' || echo "(none)"
systemctl list-timers --all --no-pager --no-legend 2>/dev/null | grep -Ei 'sync|deploy|git|update|algo|trader|ibkr|paper' || echo "(no matching timers)"
echo "-- unit FILES that mention sync.sh / deploy.sh / git (file names only)"
grep -lE 'sync\.sh|deploy\.sh|git (pull|fetch|reset)' /etc/systemd/system/*.service /etc/systemd/system/*.timer \
  /lib/systemd/system/*.service 2>/dev/null || echo "(none readable)"
echo "-- cron: current user's crontab lines that mention sync/deploy/git/main.py/scheduler (redacted)"
crontab -l 2>/dev/null | grep -nE 'sync|deploy|git|main\.py|scheduler|paper|@reboot' | redact || echo "(no matching user cron)"
echo "-- system cron files that mention them (file names only; unreadable ones are reported)"
grep -lE 'sync\.sh|deploy\.sh|git (pull|fetch|reset)|main\.py|scheduler' /etc/crontab /etc/cron.d/* 2>/dev/null || echo "(none)"
ls /var/spool/cron/crontabs 2>&1 | sed 's/^/spool: /'
echo "-- rc.local / profile hooks that start sync.sh"
grep -lE 'sync\.sh|deploy\.sh' /etc/rc.local /root/.bashrc /root/.profile "$HOME/.bashrc" "$HOME/.profile" 2>/dev/null || echo "(none)"
echo "-- GitHub Actions self-hosted runner or webhook listener"
pgrep -c -f '[R]unner\.Listener' 2>/dev/null | sed 's/^/actions_runner_processes=/'
ls -d /opt/actions-runner "$HOME/actions-runner" /home/*/actions-runner 2>/dev/null || echo "(no runner directory)"
ss -ltn 2>/dev/null | awk 'NR>1{print $4}' | sed 's/.*://' | sort -n | uniq | tr '\n' ' ' | sed 's/^/listening_ports: /'; echo
echo "-- non-sample git hooks in the deployed checkout"
ls "$ROOT/.git/hooks" 2>/dev/null | grep -v '\.sample$' || echo "(none)"

echo; echo "== Q4 bot and dashboard services (properties only)"
for u in algo-trader.service algo-trader-dashboard.service ibgateway.service; do
  systemctl show "$u" --no-pager -p Id,LoadState,ActiveState,SubState,UnitFileState,Restart,RestartUSec,NRestarts,\
ExecMainStartTimestamp,ExecMainPID 2>/dev/null; echo "--"
done
echo "-- python processes: name, start time and the systemd unit/session that owns them (no arguments)"
for p in $(pgrep -f '[m]ain\.py|[d]ashboard/app\.py|[s]cheduler' 2>/dev/null); do
  echo "$(ps -o pid=,lstart=,comm= -p $p 2>/dev/null) | $(cut -d: -f3 /proc/$p/cgroup 2>/dev/null | tr '\n' ' ')"
done

echo; echo "== Q5 does the deployed working tree track main? (read-only git; no fetch)"
G rev-parse --abbrev-ref HEAD 2>&1 | sed 's/^/branch: /'
G rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>&1 | sed 's/^/upstream: /'
G log -1 --format='HEAD=%H committed=%cI subject=%s' 2>&1 | redact
G log -1 --format='origin/main(local ref, as last fetched)=%H' origin/main 2>&1
G remote -v 2>&1 | redact
echo "-- tracked files modified on the server (names; a git reset --hard would discard these)"
G status --porcelain --untracked-files=no 2>&1 | head -50

echo; echo "== Q6 dependency install: what sync.sh does on failure (from the DEPLOYED file, static)"
grep -nE 'pip install|import check|Not restarting|sleep 60|continue' "$ROOT/sync.sh" 2>/dev/null

echo; echo "== Q7 what a restart would do: broker mode the bot last started with, kill switch, gateway ports"
journalctl -u algo-trader.service --no-pager -o short-iso 2>&1 | grep -E '\[STARTUP\] (Broker|Live|LIVE)|KILL_SWITCH\] State on startup|PAPER_LOOP|GW-WATCHDOG\]' \
  | redact | tail -n 12
python3 -I -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print("kill_switch:", {k: d.get(k) for k in ("active","reason","updated_at","activated_at") if k in d})
except Exception as e:
    print("kill_switch: unreadable or absent (%s)" % type(e).__name__)' "$ROOT/data/kill_switch.json"
ss -ltn 2>/dev/null | awk '/:(4001|4002|7496|7497) /{print "ibkr_api_listening:", $4}'
echo "-- WS2 paper loop scheduled anywhere? (cron/timers mentioning the D2 scheduler)"
{ crontab -l 2>/dev/null; systemctl list-timers --all --no-pager 2>/dev/null; } | grep -ciE 'd2_3_scheduler|paper_loop' \
  | sed 's/^/ws2_schedule_mentions=/'
} > "$R" 2>&1
echo "written: $R"
