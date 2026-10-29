#!/bin/bash
# OP1.5b — was the old paper system running? Read-only service, timer, cron and process evidence.
# Usage: bash op1_runtime_status.sh [OUT_DIR]          (default /tmp/m21_op1)
# Reads only. Never starts, stops, restarts, enables or edits anything. Never prints unit files or
# environments (they can hold secrets): only named properties, process NAMES (not arguments), and
# journal lines that pass a keyword filter and a redaction filter.
set -u
OUT="${1:-/tmp/m21_op1}"
mkdir -p "$OUT" || exit 2
R="$OUT/runtime_status.txt"
SINCE="${OP1_SINCE:-2026-06-01}"

redact() {
  sed -E \
    -e 's/[0-9]{8,10}:[A-Za-z0-9_-]{30,}/<telegram-token>/g' \
    -e 's/((api|secret|access)?_?(key|token|secret|password|passwd|pwd))([=: ]+)[^ ,;&"]+/\1\4<redacted>/Ig' \
    -e 's/\b(DU|DF|U)[0-9]{5,}\b/\1***/g' \
    -e 's/(Bearer|Basic) [A-Za-z0-9._~+\/=-]+/\1 <redacted>/g'
}

{
  echo "== generated $(date -u +%FT%TZ) on $(hostname -s); since=$SINCE"
  echo "== units mentioning algo/trader/ibkr/gateway/ibc/paper/scanner/dashboard (state only)"
  UNITS=$(systemctl list-units --type=service --all --no-legend --plain 2>/dev/null \
          | awk '{print $1}' | grep -Ei 'algo|trader|ibkr|gateway|ibc|paper|scanner|dashboard|bot' || true)
  for u in $UNITS; do
    systemctl show "$u" --no-pager -p Id,LoadState,ActiveState,SubState,UnitFileState,ActiveEnterTimestamp,\
InactiveEnterTimestamp,ExecMainStartTimestamp,NRestarts,Result 2>/dev/null
    echo "--"
  done
  echo "== timers"
  systemctl list-timers --all --no-pager 2>/dev/null | grep -Ei 'algo|trader|ibkr|gateway|paper|scan|bot|NEXT' || true
  echo "== cron (names of cron.d files; the current user's crontab with values redacted)"
  ls -l /etc/cron.d 2>/dev/null | awk '{print $1, $3, $9}'
  crontab -l 2>/dev/null | grep -v '^#' | redact || echo "(no user crontab)"
  echo "== processes (name and start time only; arguments are NOT printed)"
  ps -eo pid,lstart,etimes,comm --no-headers 2>/dev/null | grep -Ei 'python|java|ibgateway|ibc|gunicorn|flask' || true
  echo "== listening ports 4001/4002/7496/7497/8080 (IBKR live/paper API, TWS, dashboard)"
  ss -ltn 2>/dev/null | awk 'NR==1 || /:(4001|4002|7496|7497|8080) /'
  ROOT="${OP1_ROOT:-/opt/algo-trader}"
  echo "== GitHub auto-sync daemon (sync.sh: on a new main commit it resets the checkout, pip-installs and"
  echo "   restarts BOTH services; a merge to main would then deploy within ~60 s). Count of processes only:"
  n=$(pgrep -fc '[s]ync\.sh' 2>/dev/null); echo "sync_sh_processes=${n:-0}"
  echo "== sync.log: start/deploy/restart events only (redacted)"
  if [ -f "$ROOT/logs/sync.log" ]; then
    grep -E 'Sync daemon started|New commit detected|Updated to|Restarted|restart via|ERROR' "$ROOT/logs/sync.log" \
      | redact | tail -n 40
  else
    echo "(no logs/sync.log)"
  fi
  echo "== deployed checkout (read-only git queries; no fetch)"
  git -C "$ROOT" log -1 --format='HEAD=%H committed=%cI' 2>/dev/null || echo "(not a git checkout or not readable)"
  GIT_OPTIONAL_LOCKS=0 git -C "$ROOT" status --porcelain --untracked-files=no 2>/dev/null | wc -l | sed 's/^/tracked_files_modified=/'
  echo "== bot_state.json: keys and timestamps only"
  if [ -f "$ROOT/data/bot_state.json" ]; then
    python3 -I -c 'import json,sys
d=json.load(open(sys.argv[1]))
SECRET=("token","key","secret","pass","pwd","account","chat","auth","cookie")
SHOWN=("_at","_ts","_utc","time","phase","mode","state","count","cycle")
for k in sorted(d):
    v=d[k]; lk=k.lower()
    if not any(s in lk for s in SECRET) and any(lk.endswith(s) or lk == s.strip("_") for s in SHOWN) \
            and isinstance(v,(int,float,bool,str)) and len(str(v)) <= 40:
        print(k, "=", v)
    else:
        print(k, "=", "<%s>" % type(v).__name__)' "$ROOT/data/bot_state.json"
  else
    echo "(no bot_state.json)"
  fi
} > "$R" 2>&1

# Journal excerpts: activity keywords only, redacted. journalctl for system units may need the
# systemd-journal (or adm) group; if access is denied the file says so and nothing else happens.
for u in $(grep -E '^Id=' "$R" | cut -d= -f2); do
  f="$OUT/journal_${u%.service}.txt"
  {
    echo "== $u since $SINCE (counts per day of activity keywords)"
    journalctl -u "$u" --since "$SINCE" --no-pager -o short-iso 2>&1 \
      | grep -Ei 'cycle|scan|signal|order|submit|fill|paper|ibkr|live|risk|reject|error|kill' \
      | redact | awk '{print substr($1,1,10)}' | sort | uniq -c
    echo "== last 200 matching lines (redacted)"
    journalctl -u "$u" --since "$SINCE" --no-pager -o short-iso 2>&1 \
      | grep -Ei 'order|submit|fill|paper|ibkr|live|reject|error|kill' | redact | tail -n 200
  } > "$f"
done
ls -l "$OUT"
