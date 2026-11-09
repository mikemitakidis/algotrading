#!/usr/bin/env bash
# IBKR PAPER plan, phase P0: READ-ONLY inventory of the gateway, LIVE isolation, broker policy, M14 state and the loop
# journal. Run as root on the VPS (root reads the IBC config and gateway log). It changes nothing: no service action,
# no IB API connection (TCP listener checks only), no Alpaca/Internet call, databases opened read-only.
# Prints only allowlisted IBC keys and the presence (never the value) of IbLoginId/IbPassword; masks account ids,
# IP addresses, URL credentials and the hostname; prints no P&L, position or exposure values.
# Exit 0 = inventory complete, nothing needs attention; 3 = attention items (listed at the end; a required read that
# failed is always one); 1 = could not run.
set -u
ATT=()
att() { ATT+=("$*"); echo "  ATTENTION: $*"; }
APP=/opt/algo-trader
PY=$APP/venv/bin/python3
mask() { python3 -I -c 'import re, socket, sys
h = socket.gethostname().split(".")[0]
keep = ("127.0.0.1", "0.0.0.0")
for l in sys.stdin:
    l = re.sub(r"\b(DU|DF|U)\d{5,}\b", lambda m: m.group(1) + "***", l)
    l = re.sub(r"//[^/@\s]+@", "//***@", l)
    l = re.sub(r"(?i)\b(IbPassword|IbLoginId|password|passwd|token|secret)(\s*[=:]\s*)\S+", r"\1\2<masked>", l)
    l = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", lambda m: m.group(0) if m.group(0) in keep else "<ip>", l)
    if len(h) > 2: l = l.replace(h, "<host>")
    sys.stdout.write(l)'; }
echo "== IBKR PAPER P0 read-only inventory at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
[ "$(id -u)" = 0 ] || { echo "run as root (read-only)"; exit 1; }

echo "== 1. gateway unit"
systemctl show ibgateway.service -p UnitFileState,ActiveState,SubState,Result,ExecMainStatus,NRestarts,ActiveEnterTimestamp,InactiveEnterTimestamp,FragmentPath,DropInPaths --no-pager 2>&1 | sed 's/^/  /'
systemctl cat ibgateway.service 2>/dev/null | grep -E '^(ExecStart|ExecStop|Restart|RestartSec|StartLimit|User|TimeoutStopSec)=' | sed 's/^/  /' | mask
echo "  Xvfb: $(pgrep -a Xvfb 2>/dev/null | cut -c1-120 | tr '\n' ';' || true)"
echo "  gateway/IBC java processes: $(pgrep -a java 2>/dev/null | grep -ciE 'ibgateway|ibcalpha|IBC\.jar')"

echo "== 2. IBC start script and configs (allowlisted keys only)"
SS=$(systemctl show -p ExecStart --value ibgateway.service 2>/dev/null | grep -oE 'path=[^ ;]+' | head -n1 | cut -d= -f2)
SS=${SS:-/opt/ibc/start_ibgateway.sh}
echo "  start script: $SS ($(stat -c 'mode=%a owner=%U' "$SS" 2>&1))"
grep -hE '^\s*(export\s+)?(TWS_MAJOR_VRSN|IBC_INI|TRADING_MODE|TWOFA_TIMEOUT_ACTION|IBC_PATH|TWS_PATH|TWS_SETTINGS_PATH|LOG_PATH|JAVA_PATH)=' "$SS" 2>/dev/null | sed 's/^/    /' | mask
grep -qiE '^\s*(export\s+)?TRADING_MODE=["\x27]?live' "$SS" 2>/dev/null && att "the gateway start script sets TRADING_MODE=live"
INI=$(grep -hE '^\s*(export\s+)?IBC_INI=' "$SS" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d "\"\x27")
[ -n "$INI" ] && grep -qiE '^\s*TradingMode=live' "$INI" 2>/dev/null && att "the IBC config the gateway uses ($INI) has TradingMode=live"
for f in /opt/ibc/*.ini /opt/ibc/config/*.ini; do
  [ -e "$f" ] || continue
  echo "  $f ($(stat -c 'mode=%a owner=%U' "$f"))"
  grep -hE '^\s*(TradingMode|AcceptIncomingConnectionAction|ReadOnlyApi|ReadOnlyLogin|OverrideTwsApiPort|OverrideTwsMaster|ExistingSessionDetectedAction|AutoRestartTime|ColdRestartTime|ClosedownAt|AllowBlindTrading|AcceptNonBrokerageAccountWarning|CommandServerPort|BindAddress|ControlFrom|TrustedTwsApiClientIPs|StoreSettingsOnServer|ReloginAfterSecondFactorAuthenticationTimeout|SecondFactorAuthenticationExitInterval|SecondFactorDevice|FIX)=' "$f" | sed 's/^/    /' | mask
  for k in IbLoginId IbPassword FIXLoginId FIXPassword; do
    v=$(grep -E "^\s*$k=" "$f" | tail -n1 | cut -d= -f2-); [ -n "$(grep -E "^\s*$k=" "$f")" ] && echo "    $k=$([ -n "$v" ] && echo '<set>' || echo '<empty>')"
  done
done

echo "== 3. gateway log (patterns and recent lifecycle; masked)"
GL=/var/log/ibgateway/ibgateway.log
if [ -r "$GL" ]; then
  echo "  $GL: $(du -h "$GL" | cut -f1), last write $(stat -c %y "$GL")"
  for p in 'Unrecognized Username or Password' 'Login has completed' 'API server listening' 'Second Factor' 'Existing session detected' 'Closing|Shutdown|shutdown' 'Exception|ERROR'; do
    echo "    count '$p': $(grep -cE "$p" "$GL")"
  done
  grep -E 'Login|login|API|listening|Unrecognized|Second Factor|session|Shutdown|restart|Exception|ERROR' "$GL" | tail -n 25 | cut -c1-200 | sed 's/^/    /' | mask
else echo "  $GL: not readable"; fi
journalctl -u ibgateway.service --since 2026-08-25 --no-pager -o short-iso 2>/dev/null \
  | grep -E 'Started|Stopping|Stopped|Failed|Main process exited|Deactivated' | tail -n 20 | sed 's/^/    /' | mask

echo "== 4. LIVE isolation (listeners only; no API call)"
ss -ltnpH '( sport = :4001 or sport = :4002 or sport = :7496 or sport = :7497 )' 2>/dev/null | awk '{print "  listener " $4 " " $6}' | mask
[ -z "$(ss -ltnH '( sport = :4001 or sport = :4002 or sport = :7496 or sport = :7497 )' 2>/dev/null)" ] && echo "  no IBKR API listener"
ss -ltnH '( sport = :4001 or sport = :7496 )' 2>/dev/null | grep -q . && att "a LIVE IBKR API port (4001/7496) is listening"
ENVOUT=$("$PY" -I - "$APP/.env" <<'PY' 2>&1 | mask
import re, sys
sys.path.insert(0, "/opt/algo-trader")
d = {}
for l in open(sys.argv[1], encoding="utf-8"):
    m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", l.rstrip("\n"))
    if m: d[m.group(1)] = m.group(2).strip().strip("'\"")
try:
    from bot.paper_loop_d2.ibkr_adapter import EXPECTED_PAPER_ACCOUNT as exp
except Exception as e:
    exp = None
print("  BROKER=%s IBKR_HOST=%s IBKR_PORT=%s IBKR_LIVE_PORT=%s IBKR_LIVE_CONFIRMED=%s" % tuple(
    d.get(k, "<absent>") for k in ("BROKER", "IBKR_HOST", "IBKR_PORT", "IBKR_LIVE_PORT", "IBKR_LIVE_CONFIRMED")))
print("  IBKR_ACCOUNT %s; matches the loop's expected paper account: %s; IBKR_LIVE_ACCOUNT %s" % (
    "set" if d.get("IBKR_ACCOUNT") else "absent (code default used)",
    "n/a" if exp is None else ("yes" if d.get("IBKR_ACCOUNT", exp) == exp else "NO"),
    "SET" if d.get("IBKR_LIVE_ACCOUNT") else "absent"))
if d.get("BROKER", "").lower() == "ibkr_live": print("P0ATT: BROKER=ibkr_live")
if d.get("IBKR_LIVE_CONFIRMED", "").lower() == "yes": print("P0ATT: IBKR_LIVE_CONFIRMED=yes")
if d.get("IBKR_LIVE_ACCOUNT"): print("P0ATT: IBKR_LIVE_ACCOUNT is set")
if exp is not None and d.get("IBKR_ACCOUNT", exp) != exp: print("P0ATT: IBKR_ACCOUNT is not the loop's expected paper account")
print("P0_ENV_DONE")
PY
)
grep -vE "^(P0ATT: |P0_ENV_DONE$)" <<< "$ENVOUT"
grep -qx "P0_ENV_DONE" <<< "$ENVOUT" || att ".env could not be read or parsed completely (see the lines above)"
while IFS= read -r l; do att "${l#P0ATT: }"; done < <(grep "^P0ATT: " <<< "$ENVOUT")

echo "== 5. broker policy and M14 state (signals.db, read-only; flags, windows, statuses and ages only)"
DBOUT=$("$PY" -I - "$APP/data/signals.db" <<'PY' 2>&1
import json, sqlite3, sys
# REQUIRED reads (a failure is an attention item): the policy row's table, daily_state_per_broker, risk_decisions.
# OPTIONAL: auto_trading_authorizations (absent until the first D0 action).
try:
    c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True, timeout=10)
    c.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
except Exception as e:
    print("  signals.db not readable: %s" % type(e).__name__)
    print("P0ATT: signals.db could not be opened or read (%s)" % type(e).__name__); print("P0_DB_DONE"); sys.exit(0)
def q(sql, *a, required=True, label=""):
    try:
        return c.execute(sql, a).fetchall()
    except Exception as e:
        msg = str(e)
        if not required and "no such table" in msg:
            return "ABSENT"
        print("P0ATT: required read failed: %s (%s)" % (label or sql.split(" FROM ")[-1].split()[0], type(e).__name__))
        return "ERR:%s" % type(e).__name__
r = q("SELECT value FROM portfolio_risk_state WHERE key='broker_allocation_policy'", label="portfolio_risk_state")
if isinstance(r, list) and r:
    try:
        p = json.loads(r[0][0])
        assert isinstance(p, dict)
    except Exception as e:
        p = {}; print("P0ATT: broker_allocation_policy is not valid JSON (%s)" % type(e).__name__)
    for blk in ("global", "ibkr_paper", "ibkr_live", "ibkr", "etoro"):
        b = p.get(blk) if isinstance(p.get(blk), dict) else {}
        print("  policy %-10s auto=%r kill=%r until=%r caps(capital,single,daily_loss,positions)=%r" % (blk,
              b.get("auto_trading_enabled"), b.get("kill_switch"), b.get("auto_trading_enabled_until_utc"),
              tuple(b.get(k) for k in ("max_auto_trading_capital", "max_single_trade_amount", "max_daily_loss", "max_open_positions"))))
    print("  routing.allowed_brokers=%r" % ((p.get("routing") or {}).get("allowed_brokers"),))
else:
    print("  broker_allocation_policy: %s" % (r if r else "absent (DEFAULT_POLICY: auto-trading off everywhere)"))
a = q("SELECT COUNT(*), MAX(created_at_utc) FROM auto_trading_authorizations", required=False)
print("  D0 authorisation audit (rows, last): %s" % ("table absent (no D0 action recorded yet)" if a == "ABSENT" else a))
rows = q("SELECT broker_scope, MAX(date) FROM daily_state_per_broker GROUP BY broker_scope ORDER BY 1", label="daily_state_per_broker")
print("  M14 daily_state_per_broker latest day per scope: %s" % (rows,))
for scope in ("ibkr_paper", "ibkr_live", "etoro_paper", "etoro_real"):
    r = q("SELECT date, daily_pnl_source, daily_pnl_available, fresh_reads_count, last_ingested_at, lifecycle_json "
          "FROM daily_state_per_broker WHERE broker_scope=? ORDER BY date DESC LIMIT 1", scope, label="daily_state_per_broker")
    if not isinstance(r, list) or not r:
        print("    %-11s %s" % (scope, r if r else "no row (M14 treats P&L and exposure as UNKNOWN)")); continue
    date, src, avail, fr, ing, lj = r[0]
    try: exp = (json.loads(lj) or {}).get("exposure_status") if lj else None
    except ValueError: exp = "unparseable"
    print("    %-11s date=%s pnl_source=%s pnl_available=%s exposure_status=%s fresh_reads=%s last_ingested=%s" % (
          scope, date, src, avail, exp, fr, ing))
print("  M14 risk_decisions since 2026-06-01 (scope, result, n): %s" % (q(
    "SELECT broker_scope, result, COUNT(*) FROM risk_decisions WHERE taken_at >= '2026-06-01' GROUP BY 1, 2 ORDER BY 1, 2",
    label="risk_decisions"),))
c.close()
print("P0_DB_DONE")
PY
)
grep -vE "^(P0ATT: |P0_DB_DONE$)" <<< "$DBOUT"
grep -qx "P0_DB_DONE" <<< "$DBOUT" || att "the signals.db inventory did not complete (see the lines above)"
while IFS= read -r l; do att "${l#P0ATT: }"; done < <(grep "^P0ATT: " <<< "$DBOUT")
if [ -e "$APP/data/paper_lifecycles.db" ]; then
  O=$("$PY" -I -c 'import sqlite3, sys
c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
t = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type=? ORDER BY 1", ("table",))]
print("  paper_lifecycles.db tables: %s" % t)' "$APP/data/paper_lifecycles.db" 2>&1); r=$?
  echo "$O" | tail -n 2; [ "$r" = 0 ] || att "paper_lifecycles.db exists but could not be read"
else echo "  paper_lifecycles.db: absent (optional: the loop journal is created on the first loop run)"; fi
echo "  loop lock data/d2_loop.lock: $([ -e $APP/data/d2_loop.lock ] && echo present || echo absent)"

echo "== 6. M15.4 gateway health (systemd + TCP connect-and-close + config + log patterns; no IB API call)"
HOUT=$(cd $APP && "$PY" -I -c 'import json, sys
sys.path.insert(0, "/opt/algo-trader")
from bot.gateway_health import assemble_health
h = assemble_health()
print("  " + json.dumps({k: h.get(k) for k in ("status", "ready_for_ibkr_trading", "mode", "expected_port", "login_error_detected")}, default=str))
if h.get("mode") == "live": print("P0ATT: the gateway health layer reports LIVE mode")
print("P0_HEALTH_DONE")' 2>&1 | mask)
grep -vE "^(P0ATT: |P0_HEALTH_DONE$)" <<< "$HOUT" | tail -n 4
grep -qx "P0_HEALTH_DONE" <<< "$HOUT" || att "the M15.4 gateway health check did not complete (see the lines above)"
while IFS= read -r l; do att "${l#P0ATT: }"; done < <(grep "^P0ATT: " <<< "$HOUT")

echo "== 7. universe and focus (committed registry files; no network)"
UOUT=$(cd $APP && "$PY" -I -B -c 'import sys
sys.path.insert(0, "/opt/algo-trader")
from bot.universe.active_selection import get_scan_ready_symbols
s = get_scan_ready_symbols()
assert s, "no scan-ready symbols"
print("  scan-ready symbols: %d; the loop default --focus-size 150 scans the first 150 alphabetically (%s..%s)" % (len(s), s[0], s[min(149, len(s) - 1)]))
print("P0_UNIVERSE_DONE")' 2>&1)
grep -v "^P0_UNIVERSE_DONE$" <<< "$UOUT" | tail -n 3
grep -qx "P0_UNIVERSE_DONE" <<< "$UOUT" || att "the universe check did not complete (see the lines above)"

echo "== RESULT"
if [ ${#ATT[@]} -eq 0 ]; then echo "P0 INVENTORY COMPLETE: no attention items. Nothing was changed. Send this output."; exit 0; fi
echo "P0 INVENTORY: ${#ATT[@]} attention item(s). Nothing was changed. Send this output:"
printf '  - %s\n' "${ATT[@]}"
exit 3
