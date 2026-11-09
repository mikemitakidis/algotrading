#!/usr/bin/env bash
# Gate A-2.4: rotate the dashboard password (bcrypt, the repository's own tools/set_dashboard_password.py), remove the
# plaintext fallback, optionally rotate the session key (signs every existing session out), keep Google Authenticator
# (DASHBOARD_TOTP_SECRET byte-identical) and restart the DASHBOARD. Only with its own approval, run BY THE OPERATOR in
# an interactive root terminal on the VPS (the password is typed at hidden prompts):
#   A2_APPROVAL_REF='<Issue #3 comment URL>' A2_ROTATE_SESSION_KEY=yes bash a2_dashboard_password.sh
# Writes only: a root-only backup of .env in /root/algo-trader-env-backups/, .env (atomic, mode 600) and moves the
# tool's own .env.bak.* copy out of the repository directory into that backup folder. The bot unit, kill switch, guard,
# gateway and brokers are not touched. Exit 0 done; 2 refused before any change; 3 stopped after a change.
set -uo pipefail
APP=/opt/algo-trader
ENVF=$APP/.env
PY=$APP/venv/bin/python3
HERE=$(cd "$(dirname "$0")" && pwd)
TS=$(date -u +%Y%m%dT%H%M%SZ)
BKD=/root/algo-trader-env-backups
BK=$BKD/env.before-password-rotation.$TS
DU=algo-trader-dashboard.service
CHANGED=""
stop() { echo "STOP: $*"; [ -n "$CHANGED" ] && { echo "A change was made; the pre-change backup is $BK (restoring it needs approval)."; exit 3; }; exit 2; }

[ "$(id -u)" = 0 ] || stop "run as root"
case "${A2_APPROVAL_REF:-}" in https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-*) ;; *) stop "A2_APPROVAL_REF must be the approving Issue #3 comment";; esac
case "${A2_ROTATE_SESSION_KEY:-}" in yes|no) ;; *) stop "set A2_ROTATE_SESSION_KEY=yes (recommended: signs out every existing session) or no";; esac
{ : < /dev/tty; } 2>/dev/null || stop "run this yourself in an interactive terminal (the password is entered at hidden prompts)"
bash "$HERE/a2_containment.sh" "$(git -C $APP rev-parse HEAD)" || stop "containment check did not pass"
[ "$(systemctl is-active $DU)" = active ] || stop "the dashboard is not active"
[ "$(stat -c '%a %U' "$ENVF" 2>/dev/null)" = "600 root" ] || stop ".env is not mode 600 / owner root"
"$PY" -I - "$ENVF" <<'PY' || stop "precondition on .env failed"
import re, sys
keys = {}
for l in open(sys.argv[1], encoding="utf-8"):
    m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", l.rstrip("\n"))
    if m: keys.setdefault(m.group(1), []).append(m.group(2).strip().strip("'\""))
tot = keys.get("DASHBOARD_TOTP_SECRET", [])
if len(tot) != 1 or not tot[0]:
    sys.exit("DASHBOARD_TOTP_SECRET must be set exactly once: Google Authenticator must stay enabled")
if len(keys.get("DASHBOARD_SECRET_KEY", [])) > 1 or len(keys.get("DASHBOARD_PASSWORD_HASH", [])) > 1:
    sys.exit("a dashboard key appears more than once")
print("before: password_hash=%s plaintext_password=%s totp=set session_key=%s (values not shown)" % (
    "set" if keys.get("DASHBOARD_PASSWORD_HASH", [""])[0] else "absent",
    "present" if "DASHBOARD_PASSWORD" in keys else "absent",
    "set" if keys.get("DASHBOARD_SECRET_KEY", [""])[0] else "absent"))
PY
install -d -m 0700 $BKD && install -m 0600 "$ENVF" "$BK" && cmp -s "$ENVF" "$BK" || stop "backup failed"
echo "backup: $BK (root only; never displayed)"
OLDBAKS=$(ls -1 $APP/.env.bak.* 2>/dev/null | sort)

echo "== set the new password (the tool prompts twice; nothing is echoed)"
( cd $APP && "$PY" tools/set_dashboard_password.py --remove-plaintext ); trc=$?
NEWBAKS=$(comm -13 <(echo "$OLDBAKS") <(ls -1 $APP/.env.bak.* 2>/dev/null | sort) | grep -v '^$')
for f in $NEWBAKS; do install -m 0600 "$f" "$BKD/$(basename "$f").password-tool" && rm -f "$f" && echo "moved the tool's backup $(basename "$f") to $BKD"; done
if ! cmp -s "$ENVF" "$BK"; then CHANGED=1; fi
[ "$trc" = 0 ] || stop "set_dashboard_password.py exited $trc"
[ -n "$CHANGED" ] || stop "the password tool did not change .env"

if [ "$A2_ROTATE_SESSION_KEY" = yes ]; then
  "$PY" -I - "$ENVF" <<'PY' || stop "session key rotation failed"
import os, re, secrets, sys, tempfile
p = sys.argv[1]
lines = open(p, encoding="utf-8").read().split("\n")
ix = [i for i, l in enumerate(lines) if re.match(r"\s*(export\s+)?DASHBOARD_SECRET_KEY=", l)]
if len(ix) != 1:
    sys.exit("DASHBOARD_SECRET_KEY must appear exactly once")
lines[ix[0]] = "DASHBOARD_SECRET_KEY=" + secrets.token_urlsafe(48)
st = os.stat(p)
fd, tmp = tempfile.mkstemp(prefix=".env.a2tmp.", dir=os.path.dirname(p))
try:
    os.fchmod(fd, 0o600); os.fchown(fd, st.st_uid, st.st_gid)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines)); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)
except BaseException:
    if os.path.exists(tmp): os.unlink(tmp)
    raise
print("rotated DASHBOARD_SECRET_KEY (value not shown): every existing dashboard session is signed out")
PY
fi

echo "== verify .env (key names only)"
"$PY" -I - "$BK" "$ENVF" "$A2_ROTATE_SESSION_KEY" <<'PY' || stop ".env is not in the expected state"
import re, sys
def parse(path):
    d = {}
    for l in open(path, encoding="utf-8"):
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", l.rstrip("\n"))
        if m: d.setdefault(m.group(1), []).append(m.group(2).strip().strip("'\""))
    return d
old, new, rot = parse(sys.argv[1]), parse(sys.argv[2]), sys.argv[3] == "yes"
changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
allowed = {"DASHBOARD_PASSWORD_HASH", "DASHBOARD_PASSWORD", "DASHBOARD_SECRET_KEY"}
errs = []
if not set(changed) <= allowed: errs.append("unexpected keys changed: %s" % sorted(set(changed) - allowed))
h = new.get("DASHBOARD_PASSWORD_HASH", [""])
if len(h) != 1 or not re.fullmatch(r"\$2[aby]\$12\$[./A-Za-z0-9]{53}", h[0]): errs.append("DASHBOARD_PASSWORD_HASH is not one bcrypt cost-12 hash")
if h == old.get("DASHBOARD_PASSWORD_HASH"): errs.append("the password hash did not change")
if "DASHBOARD_PASSWORD" in new: errs.append("the plaintext DASHBOARD_PASSWORD is still present")
if new.get("DASHBOARD_TOTP_SECRET") != old.get("DASHBOARD_TOTP_SECRET"): errs.append("DASHBOARD_TOTP_SECRET changed")
sk_changed = new.get("DASHBOARD_SECRET_KEY") != old.get("DASHBOARD_SECRET_KEY")
if not new.get("DASHBOARD_SECRET_KEY", [""])[0]: errs.append("DASHBOARD_SECRET_KEY missing")
if rot and old.get("DASHBOARD_SECRET_KEY") and not sk_changed: errs.append("session key not rotated")
if not rot and old.get("DASHBOARD_SECRET_KEY") and sk_changed: errs.append("session key changed although not requested")
print("changed keys: %s; Google Authenticator secret unchanged: %s" % (", ".join(changed), "DASHBOARD_TOTP_SECRET" not in changed))
for e in errs: print("ERROR: " + e)
sys.exit(1 if errs else 0)
PY
[ "$(stat -c '%a %U' "$ENVF")" = "600 root" ] || stop ".env mode/owner changed"

echo "== restart the dashboard (loads the new hash; EnvironmentFile is read at start)"
P0=$(systemctl show -p MainPID --value $DU); ERR0=$(stat -c %s $APP/logs/dashboard.systemd.err 2>/dev/null || echo 0)
DPORT=$(ss -ltnpH 2>/dev/null | grep "pid=$P0," | awk '{print $4}' | sed -E 's/.*:([0-9]+)$/\1/' | head -n1); DPORT=${DPORT:-8080}
systemctl restart $DU || stop "dashboard restart failed"
ok=""; for _ in $(seq 1 30); do
  [ "$(systemctl is-active $DU)" = active ] && [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:$DPORT/)" = 200 ] && { ok=1; break; }; sleep 2; done
[ -n "$ok" ] || stop "the dashboard did not come back"
[ "$(systemctl show -p MainPID --value $DU)" != "$P0" ] || stop "the dashboard PID did not change"
NEWTB=$(tail -c +$((ERR0 + 1)) $APP/logs/dashboard.systemd.err 2>/dev/null | grep -c Traceback)
[ "$NEWTB" = 0 ] || stop "$NEWTB new Traceback(s) in logs/dashboard.systemd.err since the restart"
[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:$DPORT/api/gateway/health)" = 401 ] || stop "auth not enforced after the restart"
echo "A-2.4 DASHBOARD PASSWORD ROTATED: bcrypt hash set, plaintext removed, Google Authenticator unchanged, dashboard restarted."
echo "NOW (operator): sign in at the dashboard URL with the NEW password and a Google Authenticator code, and confirm it works."
