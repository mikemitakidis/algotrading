#!/usr/bin/env bash
# Gate A-2.1 + A-2.2: back up, test, then deploy the reviewed GitHub main 963bb89 (tree f04d1d6) to /opt/algo-trader,
# and restart the DASHBOARD once. Run as root on the VPS, only with the operator's approval:
#   A2_APPROVAL_REF='<Issue #3 comment URL>' bash a2_deploy.sh
# It never runs deploy.sh, sync.sh, rollback.sh or pip; it never starts the bot, never touches the kill switch, the
# Gate A-0 guard, .env, the gateway or a broker. Every check stops the run (exit 2 before any change, 3 after), with a
# read-only state report. Nothing is ever rolled back or retried automatically.
set -uo pipefail
APP=/opt/algo-trader
FROM=015d2cea927b9b4db3ee65604e0b4ebb58211e61
TO=963bb893c81c2482a08d78a11b1aafe98f542b51
TREE=f04d1d65dd830894156425f5a870a50591a3a805
EXPECT_MODULES=135
HERE=$(cd "$(dirname "$0")" && pwd)
TS=$(date -u +%Y%m%dT%H%M%SZ)
SNAP=/var/lib/algo-trader/gate_a2/$TS
TESTDIR=/var/tmp/gate_a2_tests_$TS
LOCK=/var/lib/algo-trader/gate_a2.lock
PY=$APP/venv/bin/python3
DU=algo-trader-dashboard.service
STAGE=preconditions
LOCKED=""

report() {
  rc=$?
  echo "----- STATE AT EXIT (exit=$rc, stage=$STAGE) -----"
  echo "head=$(git -C $APP rev-parse --short=12 HEAD 2>&1) tree=$(git -C $APP rev-parse --short=12 'HEAD^{tree}' 2>&1) tracked_changes=$(git -C $APP status --porcelain --untracked-files=no 2>/dev/null | wc -l)"
  echo "dashboard=$(systemctl is-active $DU 2>&1) pid=$(systemctl show -p MainPID --value $DU 2>&1) bot_unit=$(systemctl is-enabled algo-trader.service 2>&1)/$(systemctl is-active algo-trader.service 2>&1)"
  "$PY" -I -c 'import json, sys; print("kill_switch_active=%s" % json.load(open(sys.argv[1])).get("active"))' $APP/data/kill_switch.json 2>&1
  [ -d "$SNAP" ] && echo "snapshot: $SNAP"
  rm -rf "$TESTDIR" 2>/dev/null
  [ -n "$LOCKED" ] && rmdir "$LOCK" 2>/dev/null
  if [ "$rc" = 0 ]; then return; fi
  case "$STAGE" in
    preconditions|preserve|fetch|tests|pre-switch) echo "A-2 STOPPED BEFORE THE CODE CHANGED (the VPS still runs ${FROM:0:7}). Send this output.";;
    *) echo "A-2 DID NOT COMPLETE (stage $STAGE). Nothing was rolled back automatically. The bot unit is disabled and the kill switch was not touched."
       echo "Send this output; a code rollback (a2_rollback_code.sh) needs its own approval.";;
  esac
}
trap report EXIT
stop() { echo "STOP: $*"; case "$STAGE" in preconditions|preserve|fetch|tests|pre-switch) exit 2;; *) exit 3;; esac; }

echo "== Gate A-2 code deployment ${FROM:0:7} -> ${TO:0:7} at $TS"
# 0. PRECONDITIONS (read-only)
[ "$(id -u)" = 0 ] || stop "run as root"
case "${A2_APPROVAL_REF:-}" in https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-*) ;; *) stop "A2_APPROVAL_REF must be the Issue #3 comment that approves Gate A-2";; esac
mkdir -p /var/lib/algo-trader && mkdir "$LOCK" 2>/dev/null || stop "another Gate A-2 run holds $LOCK"
LOCKED=1
bash "$HERE/a2_containment.sh" "$FROM" || stop "containment check (VPS-C) did not pass"
cd $APP || stop "$APP missing"
[ "$(git rev-parse HEAD)" = "$FROM" ] || stop "the checkout is not $FROM"
[ -z "$(git status --porcelain --untracked-files=no)" ] || stop "modified tracked files (copy them first; review)"
BR=$(git symbolic-ref -q --short HEAD || echo DETACHED)
case "$BR" in main|DETACHED) ;; *) stop "the checkout is on branch '$BR'";; esac
[ "$(systemctl is-active $DU)" = active ] || stop "the dashboard is not active before the deployment"
DPORT=$(ss -ltnpH 2>/dev/null | grep "pid=$(systemctl show -p MainPID --value $DU)," | awk '{print $4}' | sed -E 's/.*:([0-9]+)$/\1/' | head -n1)
[ -n "$DPORT" ] || stop "cannot find the dashboard's listening port"
FREE=$(df -Pk $APP | awk 'NR==2{print $4}'); DATA=$(du -sk $APP/data | cut -f1)
[ "$FREE" -ge $(( DATA * 2 + 2097152 )) ] || stop "not enough free disk (need 2 x data/ + 2 GiB)"
id nobody >/dev/null 2>&1 || stop "user nobody missing"
echo "preconditions: OK (branch=$BR, approval=$A2_APPROVAL_REF)"

# 1. PRESERVE: evidence and consistent database backups. .env is never read or copied (size and mtime only).
STAGE=preserve
install -d -m 0700 "$SNAP" || stop "cannot create $SNAP"
{ echo "approval=$A2_APPROVAL_REF"; echo "from=$FROM"; echo "to=$TO"; echo "tree=$TREE"; echo "branch=$BR"; } > "$SNAP/plan.txt"
git rev-parse HEAD > "$SNAP/head.before"
systemctl show algo-trader.service $DU ibgateway.service -p Id,UnitFileState,ActiveState,SubState,MainPID,NRestarts,ActiveEnterTimestamp,DropInPaths > "$SNAP/units.before"
"$PY" -m pip freeze > "$SNAP/pip_freeze.before" 2>&1
stat -c '%s %Y %n' .env > "$SNAP/env.stat"
cp -p data/kill_switch.json "$SNAP/kill_switch.json.before"
for db in data/*.db; do
  [ -e "$db" ] || continue
  "$PY" -I - "$db" "$SNAP/$(basename "$db").backup" <<'PY' || stop "backup of $db failed"
import hashlib, os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect("file:%s?mode=ro" % src, uri=True, timeout=30)
d = sqlite3.connect(dst)
s.backup(d)                      # online, transactionally consistent copy (WAL included)
s.close(); d.close()
c = sqlite3.connect("file:%s?mode=ro" % dst, uri=True)
qc = c.execute("PRAGMA quick_check").fetchone()[0]; c.close()
h = hashlib.sha256()
with open(dst, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
h = h.hexdigest()
print("backup %s -> %s (%.1f MiB) quick_check=%s sha256=%s" % (os.path.basename(src), os.path.basename(dst),
      os.path.getsize(dst) / 1048576, qc, h[:16]))
sys.exit(0 if qc == "ok" else 4)
PY
done
chmod 600 "$SNAP"/*
echo "preserved: $SNAP"

# 2. FETCH and VERIFY the exact reviewed commit (writes only .git objects and refs/remotes/origin/main)
STAGE=fetch
git fetch --no-tags origin "+refs/heads/main:refs/remotes/origin/main" || stop "git fetch failed"
git cat-file -e "$TO^{commit}" 2>/dev/null || stop "$TO not found after the fetch"
git merge-base --is-ancestor "$TO" origin/main || stop "$TO is not on GitHub main"
[ "$(git rev-parse "$TO^{tree}")" = "$TREE" ] || stop "tree of $TO is not $TREE"
git merge-base --is-ancestor "$FROM" "$TO" || stop "$FROM is not an ancestor of $TO"
[ "$(git rev-parse origin/main)" = "$TO" ] && echo "GitHub main = $TO" || echo "note: GitHub main has moved past ${TO:0:7} to $(git rev-parse --short origin/main); only ${TO:0:7} is deployed"
# Files this gate must NOT change (Finish Line 7e.4 A6): verified, not assumed.
PROT="main.py dashboard/app.py requirements.txt deploy.sh sync.sh start.sh infra bot/risk.py bot/brokers bot/database.py bot/kill_switch.py bot/flywheel.py server_scripts .env.example"
git diff --quiet "$FROM" "$TO" -- $PROT || stop "a file expected to be unchanged differs: $(git diff --name-only "$FROM" "$TO" -- $PROT | tr '\n' ' ')"
COLL=$(git diff --name-only --diff-filter=A "$FROM" "$TO" | while read -r f; do [ -e "$f" ] && echo "$f"; done)
[ -z "$COLL" ] || stop "untracked files at paths the new commit adds: $COLL"
echo "verified: $TO tree $TREE; $(git diff --shortstat "$FROM" "$TO"); protected files unchanged; no path collisions"

# 3. TESTS of the exact commit with the PRODUCTION venv, in a separate clone, as nobody, with a private /tmp
#    (C5a isolation: no network, no .env, no production data). The clone is a full copy (--no-hardlinks: production
#    .git files are only read, never shared or re-owned) because the guards compare against git history.
#    Nothing in /opt/algo-trader is used except the venv (read-only) and .git (read by the clone).
STAGE=tests
if [ "${A2_SKIP_TESTS:-}" = approved-by-operator ]; then
  echo "tests: SKIPPED by operator decision (A2_SKIP_TESTS)" | tee "$SNAP/tests.txt"
else
  command -v unshare >/dev/null || stop "unshare (util-linux) is missing: the tests need a private /tmp"
  git clone --quiet --no-hardlinks --no-checkout "$APP" "$TESTDIR" && git -C "$TESTDIR" checkout --quiet --detach "$TO" \
    || stop "could not create the test clone"
  [ "$(git -C "$TESTDIR" rev-parse 'HEAD^{tree}')" = "$TREE" ] || stop "the test clone is not at tree $TREE"
  chown -R nobody "$TESTDIR"
  echo "tests: running the full suite as nobody with a private /tmp (about 15-30 minutes)..."
  ( cd "$TESTDIR" && unshare --mount --propagation private sh -c \
      'mount -t tmpfs -o mode=1777,size=1g tmpfs /tmp && exec runuser -u nobody -- env -i HOME=/tmp PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 OMP_NUM_THREADS=1 timeout 5400 "$0" _test_isolation.py --all' "$PY" ) \
    > "$SNAP/tests.txt" 2>&1
  trc=$?; chmod 600 "$SNAP/tests.txt"
  grep -E '  FAIL|ISOLATED_RUN' "$SNAP/tests.txt" | tail -n 20
  grep -qxE "ISOLATED_RUN modules=$EXPECT_MODULES failed=0( no_tests=[0-9]+)? *" "$SNAP/tests.txt" && [ "$trc" = 0 ] \
    || stop "the test suite did not pass on the VPS venv (exit $trc; see $SNAP/tests.txt)"
  rm -rf "$TESTDIR"
fi

# 4a. PRE-SWITCH: the COMPLETE containment check again, immediately before the code changes (the test stage can
#     take 30 minutes; anything that changed meanwhile - guard, allow-file, boot link, a started bot or sync.sh,
#     the kill switch - stops the run here, before any change).
STAGE=pre-switch
bash "$HERE/a2_containment.sh" "$FROM" || stop "containment changed during the run (VPS-C did not pass immediately before the switch)"
[ "$(git rev-parse HEAD)" = "$FROM" ] && [ -z "$(git status --porcelain --untracked-files=no)" ] || stop "checkout changed during the run"
[ "$(systemctl is-active $DU)" = active ] || stop "the dashboard is no longer active"

# 4b. DEPLOY the code (the only change to /opt/algo-trader; git refuses to overwrite local work)
STAGE=deploy
if [ "$BR" = main ]; then git merge --ff-only --quiet "$TO" || stop "fast-forward refused"
else git checkout --quiet --detach "$TO" || stop "checkout refused"; fi
[ "$(git rev-parse HEAD)" = "$TO" ] && [ "$(git rev-parse 'HEAD^{tree}')" = "$TREE" ] || stop "HEAD/tree not as expected after the checkout"
[ -z "$(git status --porcelain --untracked-files=no)" ] || stop "tracked files differ from $TO after the checkout"
git rev-parse HEAD > "$SNAP/head.after"
echo "deployed: HEAD=$TO tree=$TREE (branch=$BR)"

# 5. IMPORT CHECK in the production venv: no bytecode written, nothing executed beyond module import
STAGE=import-check
"$PY" -I -B -c 'import importlib, sys
sys.path.insert(0, "/opt/algo-trader")
mods = ["bot.scanner", "bot.gateway_watchdog", "bot.kill_switch", "bot.risk_authority", "bot.broker_allocation",
        "bot.runtime.paper_outcome_metrics", "bot.paper_loop_d2.scheduler", "bot.paper_loop_d2.ibkr_adapter",
        "bot.market_clock", "bot.candidate_ledger", "bot.monitoring", "bot.scoring_truth", "bot.templates",
        "bot.promotion", "bot.market_context", "dashboard.paper_metrics_view", "tools.paper_loop.m21_1extra_d2_3_scheduler"]
for m in mods:
    importlib.import_module(m)
print("import check: %d modules OK" % len(mods))' || stop "import check failed"

# 6. DASHBOARD RESTART (loads the new code; the bot unit is not touched)
STAGE=dashboard-restart
DPID0=$(systemctl show -p MainPID --value $DU); ERR0=$(stat -c %s logs/dashboard.systemd.err 2>/dev/null || echo 0)
systemctl restart $DU || stop "systemctl restart $DU failed"
ok=""; for _ in $(seq 1 30); do
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:$DPORT/ 2>/dev/null)
  [ "$(systemctl is-active $DU)" = active ] && [ "$c" = 200 ] && { ok=1; break; }; sleep 2; done
[ -n "$ok" ] || stop "the dashboard did not come back (active=$(systemctl is-active $DU), GET / = ${c:-none})"
DPID1=$(systemctl show -p MainPID --value $DU)
[ "$DPID1" != "$DPID0" ] || stop "the dashboard PID did not change"
NEWTB=$(tail -c +$((ERR0 + 1)) logs/dashboard.systemd.err 2>/dev/null | grep -c Traceback)
[ "$NEWTB" = 0 ] || stop "$NEWTB new Traceback(s) in logs/dashboard.systemd.err since the restart"
GH=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:$DPORT/api/gateway/health)
[ "$GH" = 401 ] || stop "an authenticated route answered $GH without a session (expected 401)"
echo "dashboard: restarted (pid $DPID0 -> $DPID1), GET / = 200, auth enforced (401), no new traceback"

# 7. FINAL VERIFY (read-only)
STAGE=final-verify
bash "$HERE/a2_containment.sh" "$TO" || stop "post-deployment containment check did not pass"
systemctl show algo-trader.service $DU ibgateway.service -p Id,UnitFileState,ActiveState,SubState,MainPID,NRestarts,ActiveEnterTimestamp,DropInPaths > "$SNAP/units.after"
[ "$(stat -c '%s %Y %n' .env)" = "$(cat "$SNAP/env.stat")" ] || stop ".env changed during the run"
chmod 600 "$SNAP"/*
echo "GATE A-2 CODE DEPLOYMENT DONE: /opt/algo-trader = $TO (tree $TREE); dashboard restarted; bot disabled; kill switch on. Snapshot $SNAP"
