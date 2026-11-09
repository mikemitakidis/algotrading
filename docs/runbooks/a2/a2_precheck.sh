#!/usr/bin/env bash
# Gate A-2.0 precheck: READ-ONLY. Run as root on the VPS (root is needed only to read unit state, socket owners and the
# variable NAMES in .env). It changes nothing: no git fetch, no pip install, no service action, no broker or Internet
# call. Its only network use is HTTP requests to the dashboard on 127.0.0.1.
# It prints .env variable NAMES plus an allowlist of non-secret settings, and compares ALPACA_BASE_URL with the expected
# value without printing it. It never prints, copies or hashes a secret value.
# Account ids, IP addresses, URL credentials and the hostname are masked.
# Exit 0 = no attention items; 3 = attention items (listed at the end); 2 = could not run.
set -u
APP=/opt/algo-trader
DEPLOYED=015d2cea927b9b4db3ee65604e0b4ebb58211e61
ATT=()
att() { ATT+=("$*"); echo "  ATTENTION: $*"; }
mask() { python3 -I -c 'import re, socket, sys
h = socket.gethostname().split(".")[0]
keep = ("127.0.0.1", "0.0.0.0")
for l in sys.stdin:
    l = re.sub(r"\b(DU|DF|U)\d{5,}\b", lambda m: m.group(1) + "***", l)
    l = re.sub(r"//[^/@\s]+@", "//***@", l)
    l = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", lambda m: m.group(0) if m.group(0) in keep else "<ip>", l)
    if len(h) > 2: l = l.replace(h, "<host>")
    sys.stdout.write(l)'; }

echo "== Gate A-2.0 precheck (read-only) at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
[ "$(id -u)" = 0 ] || { echo "STOP: run as root (the check is read-only)"; exit 2; }
cd "$APP" 2>/dev/null || { echo "STOP: $APP not found"; exit 2; }

echo "== 1. checkout"
HEAD=$(git rev-parse HEAD 2>&1); BR=$(git symbolic-ref -q --short HEAD || echo DETACHED)
echo "head=${HEAD:0:12} branch=$BR last_move=$(git reflog -1 --date=iso --format='%gd %gs' 2>/dev/null | cut -c1-90 | mask)"
[ "$HEAD" = "$DEPLOYED" ] || att "the VPS checkout is not 015d2ce"
case "$BR" in main|DETACHED) ;; *) att "the checkout is on branch '$BR' (A-2 expects main or a detached HEAD)";; esac
NT=$(git status --porcelain --untracked-files=no | wc -l)
echo "modified tracked files: $NT"; [ "$NT" = 0 ] || { git status --porcelain --untracked-files=no | head -20 | mask; att "modified tracked files"; }
NU=$(git ls-files --others --exclude-standard | wc -l)
echo "untracked, not ignored: $NU (names only)"; git ls-files --others --exclude-standard | head -40 | sed 's/^/    /' | mask
[ "$NU" = 0 ] || att "untracked files: git refuses a checkout that would overwrite one of them (review the list)"
echo "origin: $(git remote get-url origin 2>&1 | mask)"
git remote get-url origin 2>/dev/null | grep -qE '//[^/@]+@' && att "the origin URL embeds credentials (a token may be expired or exposed); the A-2 fetch needs none for this public repository"

echo "== 2. disk"
df -Pk "$APP" /var/lib /var/tmp 2>/dev/null | awk 'NR>1 && !seen[$1]++ {printf "  %-28s free=%d MiB  mount=%s\n", $1, $4/1024, $6}' | mask
FREE=$(df -Pk "$APP" | awk 'NR==2{print $4}'); DATA=$(du -sk "$APP/data" 2>/dev/null | cut -f1)
echo "  data/ uses $(( ${DATA:-0} / 1024 )) MiB; free next to it $(( ${FREE:-0} / 1024 )) MiB"
[ "${FREE:-0}" -ge $(( ${DATA:-0} * 2 + 2097152 )) ] || att "less free space than 2 x data/ + 2 GiB (database backups need it)"
for d in logs venv .git; do echo "  $d: $(du -sh "$APP/$d" 2>/dev/null | cut -f1)"; done

echo "== 3. virtualenv (versions only; no install)"
PY=$APP/venv/bin/python3
"$PY" -I -c 'import sys, importlib.metadata as m
print("  python", sys.version.split()[0])
for p in ("numpy", "pandas", "flask", "python-dotenv", "ib_insync", "eventkit", "alpaca-py", "bcrypt", "pyotp",
          "yfinance", "scikit-learn", "xgboost", "pyarrow", "requests", "joblib", "qrcode"):
    try: print("  %-14s %s" % (p, m.version(p)))
    except m.PackageNotFoundError: print("  %-14s MISSING" % p)' 2>&1 | mask
PV=$("$PY" -I -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
NV=$("$PY" -I -c 'import importlib.metadata as m; print(m.version("numpy"))' 2>/dev/null)
[ "$PV" = 3.12 ] || att "venv Python is '$PV' (the merged tree was tested on 3.12)"
[ "$NV" = 1.26.4 ] || att "venv numpy is '$NV' (the merged tree was tested on 1.26.4)"
PC=$("$PY" -m pip check 2>&1); echo "  pip check: $(tail -n1 <<< "$PC" | cut -c1-120)"
grep -qi "no broken requirements" <<< "$PC" || att "pip check reports a problem"
echo "  pip freeze sha256: $("$PY" -m pip freeze 2>/dev/null | sha256sum | cut -c1-16)"
if runuser -u nobody -- "$PY" -I -c 'import numpy, pandas, flask, ib_insync' >/dev/null 2>&1; then echo "  readable by nobody (needed for the A-2.2 test run): yes"
else att "the venv cannot be used by 'nobody' (A-2.2 runs the test suite as nobody)"; fi

echo "== 4. .env (names; allowlisted non-secret values only)"
stat -c '  .env mode=%a owner=%U size=%s mtime=%y' .env 2>&1
[ "$(stat -c %a .env 2>/dev/null)" = 600 ] || att ".env is not mode 600"
for f in .env?*; do [ -e "$f" ] && stat -c '  %n mode=%a owner=%U' "$f"; done
[ -d /root/algo-trader-env-backups ] && echo "  /root/algo-trader-env-backups: $(find /root/algo-trader-env-backups -maxdepth 1 -type f | wc -l) file(s), dir mode $(stat -c %a /root/algo-trader-env-backups)"
ENVOUT=$("$PY" -I - "$APP/.env" <<'PY' 2>&1 | mask
import re, sys
SHOW = {"BROKER", "IBKR_HOST", "IBKR_PORT", "IBKR_LIVE_PORT", "IBKR_LIVE_CONFIRMED", "DATA_PROVIDER", "DASHBOARD_ENV",
        "DASHBOARD_BIND_HOST", "DASHBOARD_PORT", "DASHBOARD_ACCEPT_PLAINTEXT_EXPOSURE", "TELEGRAM_ENABLED",
        "CANDIDATE_LEDGER_ENABLED", "TEMPLATE_SHADOW_ENABLED", "MARKET_CONTEXT_SHADOW_ENABLED", "PAPER_LOOP_ENABLED",
        "TEMPLATE_CONTROLS_DB_PATH", "USE_REGISTRY_UNIVERSE", "GATEWAY_WATCHDOG_MODE", "BOT_MODE", "SENTIMENT_MODE",
        "FOCUS_SIZE", "SCAN_INTERVAL_SECS", "SIGNALS_DB_PATH", "PAPER_LIFECYCLES_DB_PATH", "ETORO_LIVE_ENABLED",
        "ETORO_ROUTING_LIVE_ENABLED"}
EXPECT = {"ALPACA_BASE_URL": "https://paper-api.alpaca.markets"}
REQUIRED = ("ALPACA_KEY", "ALPACA_SECRET", "ALPACA_BASE_URL", "DASHBOARD_PASSWORD_HASH", "DASHBOARD_TOTP_SECRET",
            "DASHBOARD_SECRET_KEY")
vals, count = {}, {}
for raw in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("export "):
        line = line[7:].lstrip()
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
    if not m:
        print("  (one line is not KEY=VALUE; not shown)"); continue
    k, v = m.group(1), m.group(2).strip().strip("'\"")
    count[k] = count.get(k, 0) + 1
    vals[k] = v
att = []
for k in sorted(vals):
    v = vals[k]
    if k in SHOW or k.startswith("RISK_"):
        shown = v
    elif k in EXPECT:
        shown = "<matches expected>" if v == EXPECT[k] else "<DIFFERS from expected>"
        if v != EXPECT[k]: att.append("%s is not the expected paper URL" % k)
    else:
        shown = "<set>" if v else "<empty>"
    print("  %s=%s%s" % (k, shown, "  (x%d)" % count[k] if count[k] > 1 else ""))
for k in REQUIRED:
    if not vals.get(k): att.append("%s is missing or empty" % k)
for k, n in count.items():
    if n > 1: att.append("%s appears %d times" % (k, n))
if vals.get("BROKER", "").lower() == "ibkr_live": att.append("BROKER=ibkr_live")
if vals.get("IBKR_LIVE_CONFIRMED", "").lower() == "yes": att.append("IBKR_LIVE_CONFIRMED=yes")
if vals.get("IBKR_LIVE_ACCOUNT"): att.append("IBKR_LIVE_ACCOUNT is set")
if "DASHBOARD_PASSWORD" in vals: att.append("plaintext DASHBOARD_PASSWORD present (A-2.4 removes it)")
for a in att: print("ATTENTION_ENV: " + a)
print("ENV_PARSE_OK")
PY
)
grep -vE "^(ATTENTION_ENV: |ENV_PARSE_OK$)" <<< "$ENVOUT"
grep -qx "ENV_PARSE_OK" <<< "$ENVOUT" || att ".env could not be read or parsed completely (see the lines above)"
while IFS= read -r l; do att "${l#ATTENTION_ENV: }"; done < <(grep "^ATTENTION_ENV: " <<< "$ENVOUT")

echo "== 5. services and dashboard"
for u in algo-trader algo-trader-dashboard ibgateway caddy; do
  echo "  $u: $(systemctl show "$u.service" -p UnitFileState,ActiveState,SubState,NRestarts,ActiveEnterTimestamp --no-pager 2>&1 | tr '\n' ' ')"
done
[ "$(systemctl is-active algo-trader-dashboard.service 2>/dev/null)" = active ] || att "the dashboard is not active"
DP=$(grep -E '^DASHBOARD_PORT=' .env 2>/dev/null | tail -n1 | cut -d= -f2 | tr -dc 0-9); DP=${DP:-8080}
echo "  dashboard listener: $(ss -ltnH "sport = :$DP" 2>/dev/null | awk '{print $4}' | tr '\n' ' ' | mask)"
ss -ltnH "sport = :$DP" 2>/dev/null | awk '{print $4}' | grep -qvE '^127\.0\.0\.1:' && att "the dashboard listens beyond 127.0.0.1 (check the firewall; M15.3 recommends 127.0.0.1 behind Caddy)"
c() { curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:$DP$1" 2>/dev/null; }
echo "  GET /=$(c /) (200 expected)  GET /api/gateway/health=$(c /api/gateway/health) (401 expected: auth enforced)  GET /api/health=$(c /api/health) (503 is expected while the bot is stopped)"
[ "$(c /)" = 200 ] || att "the dashboard login page does not answer 200 on localhost"
[ "$(c /api/gateway/health)" = 401 ] || att "an authenticated route did not answer 401 without a session"
if [ -r /etc/caddy/Caddyfile ]; then
  echo "  caddy: site blocks=$(grep -cE '^[^#[:space:]].*\{' /etc/caddy/Caddyfile) reverse_proxy_to_localhost=$(grep -cE 'reverse_proxy[[:space:]]+(127\.0\.0\.1|localhost):'"$DP" /etc/caddy/Caddyfile)"
fi
ERRN=$(tail -n 2000 "$APP/logs/dashboard.systemd.err" 2>/dev/null | grep -c Traceback)
echo "  dashboard.systemd.err: $(stat -c %s "$APP/logs/dashboard.systemd.err" 2>/dev/null || echo 0) bytes, Traceback lines in the last 2000: $ERRN"

echo "== 6. kill switch, bot unit and processes (the full check is a2_containment.sh)"
"$PY" -I -c 'import json, sys; s = json.load(open(sys.argv[1])); print("  kill_switch active=%s reason=%s" % (s.get("active"), str(s.get("reason"))[:80]))' "$APP/data/kill_switch.json" 2>&1
[ "$("$PY" -I -c 'import json, sys; print(json.load(open(sys.argv[1])).get("active"))' "$APP/data/kill_switch.json" 2>/dev/null)" = True ] || att "the kill switch is not active"
[ "$(systemctl is-enabled algo-trader.service 2>/dev/null)/$(systemctl is-active algo-trader.service 2>/dev/null)" = disabled/inactive ] || att "the bot unit is not disabled/inactive"

echo "== 7. IBKR ports (no API call; a listener only)"
L=$(ss -ltnH '( sport = :4001 or sport = :4002 or sport = :7496 or sport = :7497 )' 2>/dev/null | awk '{print $4}' | tr '\n' ' ')
echo "  listeners on 4001/4002/7496/7497: ${L:-none}" | mask
grep -qE ':(4001|7496)$' <<< "$(tr ' ' '\n' <<< "$L")" && att "a LIVE IBKR port is listening"

echo "== 8. schedulers that could act during a deployment"
systemctl list-timers --all --no-pager 2>/dev/null | grep -iE 'algo|paper|trader|ibg|gateway|apt-daily|unattended' | awk '{print "  " $0}' | cut -c1-160
echo "  root crontab lines mentioning algo/deploy/sync/git/main.py: $(crontab -l -u root 2>/dev/null | grep -cE 'algo|deploy|sync|git|main\.py')"
echo "  needrestart restart mode: $(grep -hE '^\s*\$nrconf\{restart\}' /etc/needrestart/needrestart.conf /etc/needrestart/conf.d/*.conf 2>/dev/null | tail -n1 | tr -d ' ' || true)"
echo "  unattended upgrades: $(grep -hE 'Unattended-Upgrade|Update-Package-Lists' /etc/apt/apt.conf.d/20auto-upgrades 2>/dev/null | tr -s ' ' | tr '\n' ' ')"

echo "== 9. databases (read-only: existence, read, quick_check, required tables)"
# signals.db is REQUIRED (exists, readable, quick_check ok, its core tables present). paper_lifecycles.db is optional
# (created by the first loop run). Any other database that exists must be readable with quick_check ok.
DBOUT=$("$PY" -I - "$APP/data" <<'PY' 2>&1
import glob, os, sqlite3, sys
d = sys.argv[1]
REQUIRED_DB = "signals.db"
REQUIRED_TABLES = {"signals.db": ["portfolio_risk_state", "execution_intents", "risk_decisions",
                                  "daily_state_per_broker", "broker_positions"]}
INFO_TABLES = {"signals.db": ["risk_snapshots", "candidate_ledger", "candidate_ledger_meta"],
               "paper_lifecycles.db": ["loop_cycles", "loop_orders", "paper_trade_outcomes"]}
paths = sorted(glob.glob(os.path.join(d, "*.db")))
if not os.path.exists(os.path.join(d, REQUIRED_DB)):
    print("DBATT: %s is missing" % REQUIRED_DB)
for p in paths:
    n = os.path.basename(p)
    try:
        c = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=10)
        qc = c.execute("PRAGMA quick_check").fetchone()[0]
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.close()
    except Exception as e:                      # any failure to read is an attention item, never silent
        print("  %-28s READ FAILED (%s)" % (n, type(e).__name__))
        print("DBATT: %s could not be read (%s)" % (n, type(e).__name__)); continue
    req, info = REQUIRED_TABLES.get(n, []), INFO_TABLES.get(n, [])
    print("  %-28s %8.1f MiB quick_check=%s %s" % (n, os.path.getsize(p) / 1048576, qc,
          " ".join("%s=%s" % (t, "yes" if t in have else "no") for t in req + info)))
    if qc != "ok":
        print("DBATT: %s quick_check is not ok" % n)
    for t in req:
        if t not in have:
            print("DBATT: %s has no table %s" % (n, t))
print("DBCHECK_DONE")
PY
)
grep -vE "^(DBATT: |DBCHECK_DONE$)" <<< "$DBOUT"
grep -qx "DBCHECK_DONE" <<< "$DBOUT" || att "the database check did not complete (see the lines above)"
while IFS= read -r l; do att "${l#DBATT: }"; done < <(grep "^DBATT: " <<< "$DBOUT")
for f in "$APP"/logs/*.log; do [ -e "$f" ] && echo "  $(basename "$f"): $(du -h "$f" | cut -f1)"; done

echo "== RESULT"
if [ ${#ATT[@]} -eq 0 ]; then echo "A-2.0 PRECHECK OK: nothing needs attention. Nothing was changed."; exit 0; fi
echo "A-2.0 PRECHECK: ${#ATT[@]} attention item(s). Nothing was changed. Send this output:"
printf '  - %s\n' "${ATT[@]}"
exit 3
