#!/usr/bin/env bash
# Gate A-2 code rollback: put /opt/algo-trader back on 015d2ce and restart the DASHBOARD. Only with its own approval:
#   A2_ROLLBACK_APPROVAL_REF='<Issue #3 comment URL>' bash a2_rollback_code.sh
# No migration exists to undo (A-2 runs none). It never touches .env, the kill switch, the guard, the bot unit, the
# gateway, pip or a broker, and never runs deploy.sh/sync.sh/rollback.sh. Uses `git checkout --detach` (no reset, no
# branch rewrite); git refuses if it would overwrite local work. Exit 0 done; 2 refused before any change; 3 stopped.
set -uo pipefail
APP=/opt/algo-trader
FROM=015d2cea927b9b4db3ee65604e0b4ebb58211e61
FROM_TREE=$(git -C $APP rev-parse "$FROM^{tree}" 2>/dev/null)
TO=963bb893c81c2482a08d78a11b1aafe98f542b51
HERE=$(cd "$(dirname "$0")" && pwd)
DU=algo-trader-dashboard.service
CHANGED=""
# shellcheck disable=SC2154  # rc is assigned inside the trap string
trap 'rc=$?; echo "----- STATE AT EXIT (exit=$rc) head=$(git -C $APP rev-parse --short=12 HEAD 2>&1) dashboard=$(systemctl is-active $DU 2>&1) bot_unit=$(systemctl is-enabled algo-trader.service 2>&1)/$(systemctl is-active algo-trader.service 2>&1)"' EXIT
stop() { echo "STOP: $*"; [ -n "$CHANGED" ] && exit 3; exit 2; }

[ "$(id -u)" = 0 ] || stop "run as root"
case "${A2_ROLLBACK_APPROVAL_REF:-}" in https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-*) ;; *) stop "A2_ROLLBACK_APPROVAL_REF must be the approving Issue #3 comment";; esac
cd $APP || stop "$APP missing"
H=$(git rev-parse HEAD)
[ "$H" = "$TO" ] || stop "the checkout is $H, not the deployed $TO (nothing to roll back automatically)"
bash "$HERE/a2_containment.sh" "$TO" || stop "containment check did not pass; do not roll back on top of an unknown state"
[ -z "$(git status --porcelain --untracked-files=no)" ] || stop "modified tracked files: copy them first"
[ -n "$FROM_TREE" ] || stop "$FROM is not available locally"
COLL=$(git diff --name-only --diff-filter=D "$FROM" "$TO" | while read -r f; do [ -e "$f" ] && echo "$f"; done)
[ -z "$COLL" ] || stop "untracked files at paths $FROM has: $COLL"
CHANGED=1
git checkout --quiet --detach "$FROM" || stop "checkout refused"
[ "$(git rev-parse HEAD)" = "$FROM" ] && [ "$(git rev-parse 'HEAD^{tree}')" = "$FROM_TREE" ] && [ -z "$(git status --porcelain --untracked-files=no)" ] \
  || stop "HEAD/tree not as expected after the checkout"
echo "code: HEAD=$FROM (detached)"
P0=$(systemctl show -p MainPID --value $DU)
systemctl restart $DU || stop "dashboard restart failed"
for _ in $(seq 1 30); do [ "$(systemctl is-active $DU)" = active ] && [ "$(systemctl show -p MainPID --value $DU)" != "$P0" ] && break; sleep 2; done
[ "$(systemctl is-active $DU)" = active ] || stop "the dashboard is not active after the restart"
bash "$HERE/a2_containment.sh" "$FROM" || stop "containment check after the rollback did not pass"
echo "A-2 CODE ROLLBACK DONE: /opt/algo-trader = $FROM; dashboard restarted; bot disabled; kill switch untouched."
