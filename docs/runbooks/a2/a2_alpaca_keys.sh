#!/usr/bin/env bash
# Gate A-2.3: replace ALPACA_KEY / ALPACA_SECRET in /opt/algo-trader/.env with the operator's regenerated Alpaca PAPER
# keys, then prove them with read-only Alpaca calls. Only with its own approval, run BY THE OPERATOR in an
# interactive root terminal on the VPS (the keys are typed or pasted at hidden prompts; they must never pass through
# a chat, an AI session, a file or the shell history):
#   A2_APPROVAL_REF='<Issue #3 comment URL>' bash a2_alpaca_keys.sh
# Writes only: a root-only backup of .env in /root/algo-trader-env-backups/ and the two lines in .env (atomic replace,
# mode 600, every other line byte-identical). No service is restarted (the bot is off; the dashboard does not use
# Alpaca). Read-only checks afterwards: the D1 market-clock proof (Alpaca clock/calendar) and one daily-bar request
# with adjustment=raw (finding F5). No order, no account change. Exit 0 done; 2 refused before any change; 3 stopped
# after the change.
set -uo pipefail
APP=/opt/algo-trader
ENVF=$APP/.env
PY=$APP/venv/bin/python3
HERE=$(cd "$(dirname "$0")" && pwd)
TS=$(date -u +%Y%m%dT%H%M%SZ)
BK=/root/algo-trader-env-backups/env.before-alpaca-rotation.$TS
CHANGED=""
stop() { echo "STOP: $*"; [ -n "$CHANGED" ] && { echo "The .env change was made; the backup is $BK (restoring it needs approval)."; exit 3; }; exit 2; }

[ "$(id -u)" = 0 ] || stop "run as root"
case "${A2_APPROVAL_REF:-}" in https://github.com/mikemitakidis/algotrading/issues/3#issuecomment-*) ;; *) stop "A2_APPROVAL_REF must be the approving Issue #3 comment";; esac
{ : < /dev/tty; } 2>/dev/null || stop "run this yourself in an interactive terminal (the keys are entered at hidden prompts)"
HEAD=$(git -C $APP rev-parse HEAD)
bash "$HERE/a2_containment.sh" "$HEAD" || stop "containment check did not pass"
[ "$(stat -c '%a %U' "$ENVF" 2>/dev/null)" = "600 root" ] || stop ".env is not mode 600 / owner root"
install -d -m 0700 /root/algo-trader-env-backups && install -m 0600 "$ENVF" "$BK" || stop "backup failed"
cmp -s "$ENVF" "$BK" || stop "backup differs from .env"
echo "backup: $BK (root only; never displayed)"

"$PY" -I - "$ENVF" <<'PY'
import getpass, os, re, sys, tempfile
def refuse(msg):
    print("REFUSED: " + msg, file=sys.stderr); sys.exit(10)
p = sys.argv[1]
orig = open(p, encoding="utf-8").read()
lines = orig.split("\n")
def idx(k):
    return [i for i, l in enumerate(lines) if re.match(r"\s*(export\s+)?%s=" % k, l)]
ik, isec, iurl = idx("ALPACA_KEY"), idx("ALPACA_SECRET"), idx("ALPACA_BASE_URL")
if len(ik) != 1 or len(isec) != 1:
    refuse("ALPACA_KEY and ALPACA_SECRET must each appear exactly once (found %d and %d)" % (len(ik), len(isec)))
if len(iurl) != 1 or lines[iurl[0]].split("=", 1)[1].strip().strip("'\"") != "https://paper-api.alpaca.markets":
    refuse("ALPACA_BASE_URL must appear once and be https://paper-api.alpaca.markets (the PAPER trading API)")
cur_key = lines[ik[0]].split("=", 1)[1].strip().strip("'\"")
cur_sec = lines[isec[0]].split("=", 1)[1].strip().strip("'\"")
key = getpass.getpass("New Alpaca PAPER API key ID (hidden): ").strip()
sec = getpass.getpass("New Alpaca PAPER API secret key (hidden): ").strip()
if getpass.getpass("Repeat the secret key (hidden): ").strip() != sec:
    refuse("the two secret entries differ; nothing was changed")
if not re.fullmatch(r"PK[A-Z0-9]{10,40}", key):
    refuse("the key ID is not a PAPER key ID (paper key IDs start with PK); nothing was changed")
if not re.fullmatch(r"[A-Za-z0-9+/=_-]{20,100}", sec) or sec == key:
    refuse("the secret has an unexpected format; nothing was changed")
if key == cur_key or sec == cur_sec:
    refuse("the new key ID or secret equals the current one (not a rotation); nothing was changed")
def setv(i, k, v):
    pre = "export " if lines[i].lstrip().startswith("export ") else ""
    lines[i] = "%s%s=%s" % (pre, k, v)
setv(ik[0], "ALPACA_KEY", key); setv(isec[0], "ALPACA_SECRET", sec)
new = "\n".join(lines)
st = os.stat(p)
fd, tmp = tempfile.mkstemp(prefix=".env.a2tmp.", dir=os.path.dirname(p))
try:
    os.fchmod(fd, 0o600); os.fchown(fd, st.st_uid, st.st_gid)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)
except BaseException:
    if os.path.exists(tmp): os.unlink(tmp)
    raise
back = open(p, encoding="utf-8").read().split("\n")
old = orig.split("\n")
same = sum(1 for i, l in enumerate(back) if i not in (ik[0], isec[0]) and l == old[i])
assert len(back) == len(old) and same == len(old) - 2, "unexpected change to other lines"
print("updated ALPACA_KEY and ALPACA_SECRET (values not shown); the other %d lines are unchanged" % same)
PY
urc=$?
[ "$urc" = 10 ] && stop "nothing was changed (see the reason above)"
[ "$urc" = 0 ] || { CHANGED=1; stop "the update failed part-way (exit $urc): compare .env with the backup before anything else"; }
CHANGED=1
[ "$(stat -c '%a %U' "$ENVF")" = "600 root" ] || stop ".env mode/owner changed"

echo "== read-only verification 1: D1 market-clock proof (one Alpaca clock/calendar read)"
OUT=$(cd $APP && "$PY" -I -c 'import runpy, sys
sys.path.insert(0, "/opt/algo-trader")
from dotenv import load_dotenv
load_dotenv("/opt/algo-trader/.env", override=True)
sys.argv = ["m21_1extra_d1_market_clock_proof.py"]
runpy.run_path("/opt/algo-trader/tools/paper_loop/m21_1extra_d1_market_clock_proof.py", run_name="__main__")' 2>&1)
grep -E '^(D1_VPS_VERIFY_OK|D1_PROOF_FAILED|M21_1EXTRA_D1_VPS_VERIFY_RC)' <<< "$OUT"
grep -o '"live": {[^}]*}' <<< "$OUT" | head -n1
grep -qx 'D1_VPS_VERIFY_OK' <<< "$OUT" && grep -q '"live_source_proof": "passed"' <<< "$OUT" || stop "the Alpaca clock did not answer with the new keys"

echo "== read-only verification 2 (F5): one daily-bar request with adjustment=raw"
"$PY" -I -c 'import sys
sys.path.insert(0, "/opt/algo-trader")
from dotenv import load_dotenv
load_dotenv("/opt/algo-trader/.env", override=True)
from datetime import date, timedelta
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from bot.providers.alpaca_provider import AlpacaProvider, BAR_PRICE_ADJUSTMENT
assert BAR_PRICE_ADJUSTMENT == "raw"
end = date.today(); start = end - timedelta(days=40)
try:
    df = AlpacaProvider()._client().get_stock_bars(StockBarsRequest(symbol_or_symbols=["SPY"], timeframe=TimeFrame.Day,
                                                                    start=start.isoformat(), end=end.isoformat(),
                                                                    adjustment=BAR_PRICE_ADJUSTMENT)).df
except Exception as e:
    print("F5 bars request FAILED: %s" % type(e).__name__); sys.exit(1)
print("F5: adjustment=%s accepted; SPY daily bars returned: %d" % (BAR_PRICE_ADJUSTMENT, len(df)))
sys.exit(0 if len(df) >= 10 else 1)' || stop "the adjustment=raw bars request failed with the new keys"
echo "A-2.3 ALPACA PAPER KEYS DONE (values never shown). In the Alpaca paper dashboard, confirm the old key no longer exists."
